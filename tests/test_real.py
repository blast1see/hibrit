"""The tier that can actually fail.

Everything above this file tests that the code does what it says. Only these
tests answer the question the project exists for: do the underlying tools move
this metadata correctly, and does hibrit notice when they do not?

They need real HEVC clips carrying real metadata. Point ``HIBRIT_MEDIA`` at a
directory holding them:

``p8_clip.hevc``      1000 frames, Dolby Vision profile 8.1, single layer
``p7_ff.hevc``        1000 frames, Dolby Vision profile 7 plus HDR10+
``align_a.mkv``       the profile 8.1 clip in Matroska
``align_b.mkv``       a re-encode of ``align_a`` starting at frame 137

The clips are cut with ``ffmpeg -c copy``, which preserves the metadata NAL
units exactly, so a second of real footage tests the same code path a
three-hour remux does in a fraction of the time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hibrit.align import align, correlate, luma_curve
from hibrit.hdr10plus import Hdr10PlusTool, read_json
from hibrit.probe import probe
from hibrit.rpu import DoviTool, FrameCountMismatch
from hibrit.verify import sha256_file

pytestmark = pytest.mark.real

#: The offset align_b.mkv was built with. The answer is known in advance, which
#: is the only reason this test can fail.
KNOWN_OFFSET = 137


def _need(media: Path, name: str) -> Path:
    path = media / name
    if not path.exists():
        pytest.skip(f"{name} not in {media}")
    return path


class TestRoundTrip:
    def test_strip_and_reinject_reproduces_the_original_byte_for_byte(
        self, media: Path, toolbox, tmp_path: Path
    ) -> None:
        """The strongest available proof that nothing is lost or invented.

        Extract the RPU, strip it out, put it back: if the result hashes the
        same as the input, the metadata is carried exactly and the picture is
        untouched. Anything less than byte equality would mean the tool is
        re-serialising something.
        """
        source = _need(media, "p8_clip.hevc")
        dovi = DoviTool(toolbox)

        rpu = dovi.extract_rpu(source, tmp_path / "rpu.bin")
        clean = dovi.remove(source, tmp_path / "clean.hevc")
        rebuilt = dovi.inject_rpu(clean, rpu, tmp_path / "rebuilt.hevc")

        assert sha256_file(rebuilt) == sha256_file(source)

    def test_both_kinds_survive_each_other(self, media: Path, toolbox, tmp_path: Path) -> None:
        """Dolby Vision and HDR10+ in one stream, each readable back unchanged."""
        source = _need(media, "p7_ff.hevc")
        dovi = DoviTool(toolbox)
        h10p = Hdr10PlusTool(toolbox)

        rpu = dovi.extract_rpu(source, tmp_path / "rpu.bin", mode=2)
        meta = h10p.extract(source, tmp_path / "hdr10plus.json")

        base = dovi.convert(source, tmp_path / "base.hevc", mode=2)
        stripped = h10p.remove(base, tmp_path / "stripped.hevc")
        stripped = dovi.remove(stripped, tmp_path / "clean.hevc")

        with_h10p = h10p.inject(stripped, meta, tmp_path / "a.hevc")
        both = dovi.inject_rpu(with_h10p, rpu, tmp_path / "b.hevc")

        assert sha256_file(dovi.extract_rpu(both, tmp_path / "rpu2.bin")) == sha256_file(rpu)
        assert sha256_file(h10p.extract(both, tmp_path / "meta2.json")) == sha256_file(meta)

    def test_profile_7_to_81_keeps_the_metadata(self, media: Path, toolbox, tmp_path: Path) -> None:
        """Converting the layer structure must not change what the RPU says."""
        source = _need(media, "p7_ff.hevc")
        dovi = DoviTool(toolbox)

        before = dovi.info(dovi.extract_rpu(source, tmp_path / "p7.bin"))
        after = dovi.info(dovi.extract_rpu(source, tmp_path / "p81.bin", mode=2))

        assert before.profile == 7
        assert after.profile == 8
        assert after.frames == before.frames
        assert after.scene_count == before.scene_count
        assert after.dm_version == before.dm_version
        assert after.l5_offsets == before.l5_offsets


class TestSilentPaddingIsCaught:
    def test_a_short_rpu_is_refused_instead_of_padded(
        self, media: Path, toolbox, tmp_path: Path
    ) -> None:
        """dovi_tool warns and exits 0 here; hibrit must not.

        This is the failure the whole project is built around: the output looks
        finished, plays, and shows a Dolby Vision badge, while every frame after
        the cut carries the wrong metadata.
        """
        source = _need(media, "p8_clip.hevc")
        dovi = DoviTool(toolbox)

        rpu = dovi.extract_rpu(source, tmp_path / "rpu.bin")
        short = dovi.editor(rpu, {"remove": ["0-199"]}, tmp_path / "short.bin", workdir=tmp_path)
        assert dovi.info(short).frames == dovi.info(rpu).frames - 200

        clean = dovi.remove(source, tmp_path / "clean.hevc")
        out = tmp_path / "bad.hevc"
        with pytest.raises(FrameCountMismatch) as caught:
            dovi.inject_rpu(clean, short, out)

        assert caught.value.delta == -200
        # The half-written file must be gone: a misaligned output that looks
        # finished is worse than no output at all.
        assert not out.exists()

    def test_the_editor_can_pad_as_well_as_cut(self, media: Path, toolbox, tmp_path: Path) -> None:
        source = _need(media, "p8_clip.hevc")
        dovi = DoviTool(toolbox)
        rpu = dovi.extract_rpu(source, tmp_path / "rpu.bin")
        frames = dovi.info(rpu).frames

        longer = dovi.editor(
            rpu,
            {"duplicate": [{"source": 0, "offset": 0, "length": 200}]},
            tmp_path / "long.bin",
            workdir=tmp_path,
        )
        assert dovi.info(longer).frames == frames + 200


class TestAlignmentOnRealFootage:
    def test_recovers_the_offset_from_a_different_encode(self, media: Path, toolbox) -> None:
        """align_b is a 960x540 CRF 32 re-encode — a genuinely different picture."""
        a = probe(_need(media, "align_a.mkv"), toolbox)
        b = probe(_need(media, "align_b.mkv"), toolbox)

        curve_a = luma_curve(a, toolbox, frames=900)
        curve_b = luma_curve(b, toolbox, frames=900)
        offset, confidence, _ = correlate(curve_a, curve_b, max_shift=300)

        assert offset == KNOWN_OFFSET
        assert confidence > 2.5

    def test_full_align_agrees_and_calls_it_reliable(self, media: Path, toolbox) -> None:
        a = probe(_need(media, "align_a.mkv"), toolbox)
        b = probe(_need(media, "align_b.mkv"), toolbox)
        result = align(a, b, toolbox, max_shift=300, window_frames=500)
        assert result.usable
        assert result.offset == KNOWN_OFFSET

    def test_refuses_two_unrelated_films(self, media: Path, toolbox) -> None:
        """The mistake a user is most likely to make: the wrong pair of files."""
        a = probe(_need(media, "align_a.mkv"), toolbox)
        other = probe(_need(media, "align_other.mkv"), toolbox)
        result = align(a, other, toolbox, max_shift=300, window_frames=500)
        assert not result.usable

    def test_refuses_when_the_offset_lies_outside_the_search(self, media: Path, toolbox) -> None:
        """A peak on the wall of the search range is not an answer."""
        a = probe(_need(media, "align_a.mkv"), toolbox)
        b = probe(_need(media, "align_b.mkv"), toolbox)
        result = align(a, b, toolbox, max_shift=120, window_frames=500)
        assert not result.usable


class TestEndToEnd:
    def test_full_run_transfers_dv_and_verifies_clean(
        self, media: Path, toolbox, tmp_path: Path
    ) -> None:
        """Plan, run and verify against a target built by stripping a real clip.

        The target is made by a route the pipeline does not use, so the correct
        output is known before the run starts: the target's picture with the
        source's metadata, and nothing else different.
        """
        from hibrit.pipeline import run
        from hibrit.planner import build_plan
        from hibrit.verify import verify

        source_mkv = _need(media, "align_a.mkv")
        dovi = DoviTool(toolbox)

        stripped = dovi.remove(_need(media, "p8_clip.hevc"), tmp_path / "clean.hevc")
        target_mkv = tmp_path / "target.mkv"
        toolbox.run("mkvmerge", ["-q", "-o", str(target_mkv), str(stripped)], check=False)

        source = probe(source_mkv, toolbox)
        target = probe(target_mkv, toolbox)
        plan = build_plan(source, target)
        assert plan.transfer and plan.ok
        assert not plan.needs_alignment

        result = run(plan, tmp_path / "out.mkv", workdir=tmp_path, toolbox=toolbox)

        report = verify(
            result.output,
            target=target_mkv,
            rpu=result.rpu,
            clean_target_stream=result.clean_target_stream,
            workdir=tmp_path,
            toolbox=toolbox,
        )
        assert report.passed, report.describe()

        after = probe(result.output, toolbox)
        assert after.has_dv
        assert after.frame_count == target.frame_count

    def test_metadata_is_retimed_to_a_shorter_target(
        self, media: Path, toolbox, tmp_path: Path
    ) -> None:
        """The hardest path, on real footage: measure, retime, inject, verify.

        The target is the first 960 frames of the source with its Dolby Vision
        stripped — built by stream copy, a route the pipeline never takes. So
        the correct answer is known before the run: offset 0, an RPU trimmed
        from 1000 frames to 960, and a picture identical to the target's.

        Without the retiming step this is precisely the case dovi_tool would
        accept: it would pad the 1000-frame RPU against a 960-frame video and
        exit successfully.
        """
        from hibrit.pipeline import run
        from hibrit.planner import build_plan
        from hibrit.verify import verify

        source_mkv = _need(media, "align_a.mkv")
        clip = _need(media, "p8_clip.hevc")
        dovi = DoviTool(toolbox)

        trimmed = tmp_path / "trimmed.hevc"
        toolbox.run(
            "ffmpeg",
            [
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(clip),
                "-map",
                "0:v:0",
                "-frames:v",
                "960",
                "-c",
                "copy",
                "-f",
                "hevc",
                str(trimmed),
            ],
        )
        clean = dovi.remove(trimmed, tmp_path / "clean.hevc")
        target_mkv = tmp_path / "target.mkv"
        toolbox.run("mkvmerge", ["-q", "-o", str(target_mkv), str(clean)], check=False)

        source = probe(source_mkv, toolbox)
        target = probe(target_mkv, toolbox)
        assert (source.frame_count, target.frame_count) == (1000, 960)

        plan = build_plan(source, target)
        assert plan.ok
        assert plan.needs_alignment, "a 40-frame gap has to be retimed"

        workdir = tmp_path / "work"
        result = run(plan, tmp_path / "out.mkv", workdir=workdir, toolbox=toolbox)

        assert result.alignment is not None
        assert result.alignment.usable
        assert result.alignment.offset == 0
        assert dovi.info(result.rpu).frames == 960, "the RPU was not trimmed"

        report = verify(
            result.output,
            target=target_mkv,
            rpu=result.rpu,
            clean_target_stream=result.clean_target_stream,
            workdir=workdir,
            toolbox=toolbox,
        )
        assert report.passed, report.describe()
        assert not report.unmeasured


class TestProbeReadsRealFiles:
    def test_reads_dolby_vision_and_hdr10plus_together(self, media: Path, toolbox) -> None:
        info = probe(_need(media, "align_a.mkv"), toolbox)
        assert info.is_hevc
        assert info.has_dv
        assert info.frame_count == 1000
        assert "DV" in info.describe()

    def test_hdr10plus_json_frame_count_matches_the_video(
        self, media: Path, toolbox, tmp_path: Path
    ) -> None:
        source = _need(media, "p7_ff.hevc")
        meta = Hdr10PlusTool(toolbox).extract(source, tmp_path / "meta.json")
        assert read_json(meta).frames == 1000
