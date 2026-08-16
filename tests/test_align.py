"""The measurement has to be right, and it has to be able to say it is not.

Half of these tests feed the aligner something it should not find an answer in.
That half matters more: a correlation always returns a peak, so a test suite
that only ever supplies matching pairs never observes the failure mode that the
tool will actually meet.
"""

from __future__ import annotations

import numpy as np
import pytest
from conftest import make_info, shot_curve

from hibrit.align import (
    CONFIDENCE_RELIABLE,
    Verdict,
    align,
    correlate,
    cut_signal,
    edit_config_for_offset,
    window_plan,
)


class TestCutSignal:
    def test_marks_cuts_and_nothing_else(self) -> None:
        curve = np.array([10.0] * 20 + [200.0] * 20)
        signal = cut_signal(curve)
        assert signal.size == curve.size - 1
        # Exactly one non-zero sample, at the cut.
        assert int(np.count_nonzero(signal)) == 1
        assert int(np.argmax(signal)) == 19

    def test_clips_the_tallest_jumps(self) -> None:
        """A few enormous cuts must not outweigh the pattern of ordinary ones."""
        curve = shot_curve(600, seed=3)
        raw = np.abs(np.diff(curve))
        clipped = cut_signal(curve)
        assert clipped.max() < raw.max()
        assert clipped.max() == pytest.approx(np.percentile(raw, 99))

    def test_flat_input_is_survivable(self) -> None:
        assert cut_signal(np.zeros(50)).size == 49
        assert cut_signal(np.zeros(1)).size == 0


class TestCorrelate:
    @pytest.mark.parametrize("offset", [0, 1, 37, 137, -64])
    def test_recovers_a_known_offset(self, offset: int) -> None:
        """A positive offset means *a* starts earlier: ``b[i]`` is ``a[i + offset]``."""
        base = shot_curve(4000, seed=1)
        pad = 200
        a = base[pad:]
        b = base[pad + offset :]
        found, confidence, _ = correlate(a, b, max_shift=300)
        assert found == offset
        assert confidence > CONFIDENCE_RELIABLE

    def test_survives_a_different_encode(self) -> None:
        """Two encodes of the same frames differ slightly; the offset must not."""
        base = shot_curve(4000, seed=2)
        rng = np.random.default_rng(9)
        a = base + rng.normal(0, 2.0, size=base.size)
        b = base[91:] + rng.normal(0, 2.0, size=base.size - 91)
        found, confidence, _ = correlate(a, b, max_shift=300)
        assert found == 91
        assert confidence > CONFIDENCE_RELIABLE

    def test_unrelated_content_scores_near_one(self) -> None:
        a = shot_curve(4000, seed=11)
        b = shot_curve(4000, seed=12)
        _, confidence, _ = correlate(a, b, max_shift=300)
        assert confidence < 2.0

    def test_empty_input_raises(self) -> None:
        from hibrit.align import AlignError

        with pytest.raises(AlignError):
            correlate(np.zeros(0), np.zeros(10), max_shift=5)


class TestWindowPlan:
    def test_thresholds_scale_with_the_material(self) -> None:
        """A fixed window that suits a feature is the whole of a test clip."""
        small_shift, small_window, _ = window_plan(1000, 1000)
        big_shift, big_window, _ = window_plan(223_615, 223_615)
        assert small_shift < big_shift
        assert small_window <= 1000
        assert big_window > small_window

    def test_never_exceeds_the_shorter_file(self) -> None:
        shift, window, starts = window_plan(500, 400)
        assert window <= 400
        assert shift <= 400
        for start, length in starts:
            assert start + length <= 400 + shift

    def test_second_window_lands_elsewhere(self) -> None:
        _, window, starts = window_plan(223_615, 223_615, windows=2)
        assert len(starts) == 2
        assert abs(starts[0][0] - starts[1][0]) > window // 2


class TestEditConfig:
    def test_trims_the_head_when_source_starts_earlier(self) -> None:
        config = edit_config_for_offset(137, source_frames=1000, target_frames=863)
        assert config["remove"] == ["0-136"]
        assert "duplicate" not in config

    def test_pads_the_head_when_source_starts_later(self) -> None:
        config = edit_config_for_offset(-10, source_frames=100, target_frames=110)
        assert config["duplicate"] == [{"source": 0, "offset": 0, "length": 10}]
        assert "remove" not in config

    def test_trims_the_tail_to_the_exact_target_length(self) -> None:
        config = edit_config_for_offset(10, source_frames=1000, target_frames=900)
        assert config["remove"] == ["0-9", "900-989"]

    def test_pads_the_tail_when_source_runs_short(self) -> None:
        config = edit_config_for_offset(0, source_frames=900, target_frames=1000)
        assert config["duplicate"] == [{"source": 899, "offset": 900, "length": 100}]

    def test_identical_lengths_need_no_edit(self) -> None:
        assert edit_config_for_offset(0, source_frames=500, target_frames=500) == {}


class TestAlign:
    """The decision layer, with decoding replaced by a known signal.

    ``align()`` is where the refusals live, and they are pure logic once the
    luma curves exist. Driving it with synthetic curves lets the refusals be
    checked in milliseconds on a machine with no media and no ffmpeg — the real
    footage versions live in ``test_real.py``.
    """

    @staticmethod
    def _patch_curves(monkeypatch, curve_a, curve_b) -> None:
        def fake(info, toolbox, *, start_frame=0, frames=None, **_kwargs):
            source = curve_a if info.name == "a.mkv" else curve_b
            window = source[start_frame:]
            return window[:frames] if frames is not None else window

        monkeypatch.setattr("hibrit.align.luma_curve", fake)

    def test_agreeing_windows_are_reliable(self, monkeypatch) -> None:
        base = shot_curve(30_000, seed=5)
        offset = 137
        self._patch_curves(monkeypatch, base, base[offset:])

        result = align(
            make_info("a.mkv", frames=30_000),
            make_info("b.mkv", frames=30_000 - offset),
            toolbox=None,
            windows=2,
        )
        assert result.verdict is Verdict.RELIABLE
        assert result.offset == offset
        assert len(result.windows) == 2

    def test_windows_that_disagree_produce_a_refusal_not_an_average(self, monkeypatch) -> None:
        """Two different offsets mean no single offset exists. Averaging them
        would produce a number that is wrong in both places.

        Both shifts stay inside the search range on purpose: a shift outside it
        is a window that could not measure, which is a different situation with
        a different answer.
        """
        base = shot_curve(30_000, seed=6)
        # Head shifted by 100, tail by 250 — a scene missing from the middle.
        spliced = np.concatenate([base[100:12_000], base[12_150:]])
        self._patch_curves(monkeypatch, base, spliced)

        result = align(
            make_info("a.mkv", frames=30_000),
            make_info("b.mkv", frames=spliced.size),
            toolbox=None,
            windows=2,
        )
        assert result.verdict is Verdict.NO_MATCH
        assert result.offset is None
        assert "disagree" in result.reason

    def test_a_window_that_could_not_measure_is_not_a_window_that_disagreed(
        self, monkeypatch
    ) -> None:
        """Different failures deserve different sentences.

        A window whose correlation surface is flat has not contradicted
        anything — it read nothing. Folding its argmax into the agreement check
        reports "the releases differ structurally", which sends the reader to
        look for a different cut that is not there. The honest answer is that
        one place in the film could not be read.
        """
        base = shot_curve(30_000, seed=13)
        # The tail is replaced with unrelated footage, so the second window has
        # nothing to lock onto while the first matches cleanly.
        noise = shot_curve(16_000, seed=14)
        spliced = np.concatenate([base[100:14_000], noise])
        self._patch_curves(monkeypatch, base, spliced)

        result = align(
            make_info("a.mkv", frames=30_000),
            make_info("b.mkv", frames=spliced.size),
            toolbox=None,
            windows=2,
        )
        assert result.verdict is Verdict.NO_MATCH
        assert "could not measure" in result.reason
        assert "differ structurally" not in result.reason
        # The one window that did read something is still reported.
        assert "+100" in result.reason

    def test_unrelated_content_is_refused(self, monkeypatch) -> None:
        """Two films give two windows two unrelated answers, so the refusal
        arrives as a disagreement rather than as a weak peak. Either route is
        correct; what matters is that no offset comes back."""
        self._patch_curves(monkeypatch, shot_curve(30_000, seed=7), shot_curve(30_000, seed=8))
        result = align(
            make_info("a.mkv", frames=30_000),
            make_info("b.mkv", frames=30_000),
            toolbox=None,
        )
        assert result.verdict is Verdict.NO_MATCH
        assert result.offset is None

    def test_a_single_window_still_refuses_a_weak_peak(self, monkeypatch) -> None:
        """With only one window there is nothing to disagree with, so the
        confidence threshold has to carry the refusal on its own."""
        self._patch_curves(monkeypatch, shot_curve(30_000, seed=7), shot_curve(30_000, seed=8))
        result = align(
            make_info("a.mkv", frames=30_000),
            make_info("b.mkv", frames=30_000),
            toolbox=None,
            windows=1,
        )
        assert not result.usable
        assert "noise floor" in result.reason

    def test_an_offset_at_the_edge_of_the_search_is_not_an_answer(self, monkeypatch) -> None:
        """The bug this caught: a search too narrow to contain the real offset
        returned the wall of the search range as though it were a measurement."""
        base = shot_curve(30_000, seed=9)
        self._patch_curves(monkeypatch, base, base[2_000:])
        result = align(
            make_info("a.mkv", frames=30_000),
            make_info("b.mkv", frames=28_000),
            toolbox=None,
            max_shift=200,
        )
        assert not result.usable

    def test_progress_names_each_window_as_it_starts(self, monkeypatch) -> None:
        """Four and a half minutes of silence reads as a hung command."""
        base = shot_curve(30_000, seed=5)
        self._patch_curves(monkeypatch, base, base[137:])
        said: list[str] = []

        align(
            make_info("a.mkv", frames=30_000),
            make_info("b.mkv", frames=30_000 - 137),
            toolbox=None,
            windows=2,
            progress=said.append,
        )

        starts = [line for line in said if "decoding" in line]
        assert len(starts) == 2
        assert "window 1 of 2" in starts[0]
        # And the answer for each window arrives as it is found, not only at the
        # end, so a slow second window does not hide a finished first one.
        assert any("window 1: offset" in line for line in said)

    def test_unknown_frame_count_stops_before_decoding_anything(self) -> None:
        result = align(
            make_info("a.mkv", frames=None),
            make_info("b.mkv", frames=1000),
            toolbox=None,
        )
        assert result.verdict is Verdict.NO_MATCH
        assert result.windows == ()
        assert "frame count unknown" in result.reason


class TestVerdict:
    def test_no_match_is_not_usable(self) -> None:
        from hibrit.align import Alignment

        result = Alignment(
            offset=5, verdict=Verdict.NO_MATCH, confidence=1.0, windows=(), reason=""
        )
        assert not result.usable

    def test_suspect_is_not_usable_either(self) -> None:
        """A weak peak still has to be confirmed by a human, not by the caller."""
        from hibrit.align import Alignment

        result = Alignment(offset=5, verdict=Verdict.SUSPECT, confidence=2.0, windows=(), reason="")
        assert not result.usable

    def test_a_rejected_result_does_not_lead_with_its_number(self) -> None:
        """Whatever is printed first is what gets used.

        A refusal that opens with "offset -115 frames" hands the reader the
        figure and files the refusal underneath it — which is the habit this
        whole project is arranged against.
        """
        from hibrit.align import Alignment

        rejected = Alignment(
            offset=-115, verdict=Verdict.NO_MATCH, confidence=1.04, windows=(), reason="x"
        ).describe()
        assert rejected.startswith("no_match")
        assert "best candidate" in rejected

        trusted = Alignment(
            offset=137, verdict=Verdict.RELIABLE, confidence=3.74, windows=(), reason="x"
        ).describe()
        assert trusted.startswith("offset +137")
