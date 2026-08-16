"""The measurement has to be right, and it has to be able to say it is not.

Half of these tests feed the aligner something it should not find an answer in.
That half matters more: a correlation always returns a peak, so a test suite
that only ever supplies matching pairs never observes the failure mode that the
tool will actually meet.
"""

from __future__ import annotations

import numpy as np
import pytest
from conftest import shot_curve

from hibrit.align import (
    CONFIDENCE_RELIABLE,
    Verdict,
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
