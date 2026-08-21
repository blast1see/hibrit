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
``align_other.mkv``   the profile 7 clip in Matroska — a different film
``variable_l5.hevc``  a clip straddling an aspect-ratio change, so one RPU
                      carries two different sets of level 5 offsets

Those six are the whole list. Everything else these tests need they build,
because a suite that leans on whatever is left lying in the media directory
breaks the day somebody tidies it up.

The last one cannot be synthesised the way the others can. ``dovi_tool editor``
writes a moving active area on request and the tools tier uses that, but a
made-to-order RPU only proves the tool round-trips what it was told. This clip
is cut from a UHD remux of a film whose IMAX sequences open the frame from
2.40:1 to 1.78:1 and close it again: 12 seconds spanning the first change, which
in that release falls at frame 1274. Across the whole film the split is 179,353
frames of scope against 39,602 of IMAX.

It is a raw stream rather than Matroska on purpose. Cutting Matroska to Matroska
with ``-c copy`` leaves a container MediaInfo cannot make sense of: the first
attempt at this clip reported 17,382 FPS and the source film's whole frame count
for twelve seconds of footage, and `-avoid_negative_ts` did not help. An Annex B
stream carries no container timing to get wrong, and MediaInfo reads 23.976 off
it — which is why the two clips that had to be exact were always ``.hevc``.

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


class TestVariableActiveArea:
    def test_a_films_own_aspect_ratio_change_is_read_as_a_range(
        self, media: Path, toolbox, tmp_path: Path
    ) -> None:
        """Read off a real film rather than an RPU built to order.

        Everything else that exercises this path either holds one shape or was
        edited into changing one. This clip changes because the film does, and
        the numbers are the release's own: 280 rows masked top and bottom for
        2.40:1, none at all for the IMAX frames.

        The old parser reported None here — the same answer it gives for a
        release with no level 5 whatsoever — which is what this is here to stop
        coming back.
        """
        source = _need(media, "variable_l5.hevc")
        info = DoviTool(toolbox).info(
            DoviTool(toolbox).extract_rpu(source, tmp_path / "variable.bin")
        )

        assert info.l5_offsets is not None
        assert info.l5_offsets.variable
        assert info.l5_offsets.fixed is None
        assert info.l5_offsets.top == (0, 280)
        assert info.l5_offsets.bottom == (0, 280)
        # The sides never move: a scope film masks rows, not columns.
        assert info.l5_offsets.left == (0, 0)
        assert info.l5_offsets.right == (0, 0)
        assert "(varies)" in str(info.l5_offsets)

    def test_the_clip_really_does_hold_both_shapes(
        self, media: Path, toolbox, tmp_path: Path
    ) -> None:
        """Confirm the material, not just the parse.

        A clip that had been trimmed down to one shape would make the test above
        pass for the wrong reason — or rather fail, but the day it was replaced
        with a shorter cut nobody would know why. So the per-frame table is read
        directly, by a route parse_info has no part in.
        """
        source = _need(media, "variable_l5.hevc")
        dovi = DoviTool(toolbox)
        rpu = dovi.extract_rpu(source, tmp_path / "variable.bin")
        csv = dovi.export_levels(rpu, tmp_path / "l5.csv", level="level5")

        rows = csv.read_text(encoding="utf-8").splitlines()[1:]
        shapes = {tuple(row.split(",")[1:]) for row in rows if row.strip()}
        assert len(shapes) == 2, f"expected two active areas in the clip, got {shapes}"


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

    def test_seeking_into_the_middle_lands_on_the_right_frame(self, media: Path, toolbox) -> None:
        """Everything the two-window rule does rests on this.

        ffmpeg has no frame-index seek, so a window starting at frame N is
        reached by timestamp. If that landed a frame or two off, the second
        window would report a different offset than the first and the tool would
        refuse every real film for a reason that was never true.
        """
        import numpy as np

        info = probe(_need(media, "align_a.mkv"), toolbox)
        whole = luma_curve(info, toolbox, frames=900)

        for start in (1, 7, 40, 137, 200, 401):
            seeked = luma_curve(info, toolbox, start_frame=start, frames=120)
            assert np.allclose(seeked[:120], whole[start : start + 120]), f"start={start}"

    def test_a_window_that_reads_nothing_is_reported_as_such(self, media: Path, toolbox) -> None:
        """A short window over a quiet stretch measures nothing, and saying the
        releases differ structurally would be a diagnosis of something else."""
        a = probe(_need(media, "align_a.mkv"), toolbox)
        b = probe(_need(media, "align_b.mkv"), toolbox)

        result = align(a, b, toolbox, windows=2, max_shift=300, window_frames=200)
        assert len(result.windows) == 2
        assert not result.usable
        assert "could not measure" in result.reason

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

    def test_hdr10plus_into_a_dual_layer_dolby_vision_target(
        self, media: Path, toolbox, tmp_path: Path
    ) -> None:
        """The second scenario this project was asked for, on the shape it
        actually occurs in.

        A UHD Blu-ray remux carries Dolby Vision profile 7 with a separate
        enhancement layer, and sometimes lacks the HDR10+ a streaming release
        has. So the target here is dual-layer profile 7 rather than the
        single-layer 8.1 every other end-to-end test uses.

        The ground truth is exact: the target is the source with its HDR10+
        removed, so putting it back must reproduce the source's picture, its
        HDR10+ and its untouched profile 7 RPU.
        """
        from hibrit.matroska import extract_video
        from hibrit.pipeline import run
        from hibrit.planner import Kind, build_plan
        from hibrit.verify import picture_digest, verify

        original = _need(media, "p7_ff.hevc")
        h10p = Hdr10PlusTool(toolbox)
        dovi = DoviTool(toolbox)

        stripped = h10p.remove(original, tmp_path / "no_hdr10plus.hevc")
        source_mkv = tmp_path / "source.mkv"
        target_mkv = tmp_path / "target.mkv"
        toolbox.run("mkvmerge", ["-q", "-o", str(source_mkv), str(original)], check=False)
        toolbox.run("mkvmerge", ["-q", "-o", str(target_mkv), str(stripped)], check=False)

        source = probe(source_mkv, toolbox)
        target = probe(target_mkv, toolbox)
        assert source.has_hdr10plus and source.has_dv and source.is_dual_layer
        assert target.has_dv and target.is_dual_layer and not target.has_hdr10plus

        plan = build_plan(source, target)
        # Only HDR10+ moves: the target's own Dolby Vision is left alone.
        assert plan.transfer == (Kind.HDR10PLUS,)
        assert plan.convert_mode is None

        workdir = tmp_path / "work"
        result = run(plan, tmp_path / "out.mkv", workdir=workdir, toolbox=toolbox)

        report = verify(
            result.output,
            target=target_mkv,
            hdr10plus=result.hdr10plus,
            clean_target_stream=result.clean_target_stream,
            workdir=workdir,
            toolbox=toolbox,
        )
        assert report.passed, report.describe()

        # Against the file that had both to begin with, which is the strongest
        # statement available: same picture, same HDR10+, same profile 7 RPU.
        produced = extract_video(result.output, tmp_path / "out.hevc", toolbox)
        assert picture_digest(produced) == picture_digest(original)
        assert sha256_file(h10p.extract(produced, tmp_path / "back.json")) == sha256_file(
            h10p.extract(original, tmp_path / "orig.json")
        )
        assert sha256_file(dovi.extract_rpu(produced, tmp_path / "back.bin")) == sha256_file(
            dovi.extract_rpu(original, tmp_path / "orig.bin")
        )

        # And the whole-file hashes differ, which is why the picture check
        # compares picture units rather than files.
        assert sha256_file(produced) != sha256_file(original)

        after = probe(result.output, toolbox)
        assert after.has_hdr10plus and after.has_dv and after.is_dual_layer


class TestAwkwardFilenames:
    """Names with characters outside ASCII, on the platform that mangles them.

    Release names carry them constantly — Turkish, Nordic, Central European —
    and every one of these tools is a separate
    program receiving the path as an argument and printing it back in its own
    encoding. A failure here would meet the user on their first real file.
    """

    #: Dotted and dotless i, ş, ğ, ü, plus the spaces and parentheses that
    #: release names carry anyway.
    AWKWARD = "Dağ Şölen (2012) çıkış İĞÜ.mkv"  # noqa: RUF001

    @pytest.fixture
    def awkward_copy(self, media: Path, tmp_path: Path) -> Path:
        import shutil

        source = _need(media, "align_a.mkv")
        target = tmp_path / self.AWKWARD
        shutil.copy2(source, target)
        return target

    def test_mediainfo_reads_it_and_gives_the_name_back_intact(
        self, awkward_copy: Path, toolbox
    ) -> None:
        info = probe(awkward_copy, toolbox)
        assert info.name == awkward_copy.name
        assert info.frame_count == 1000
        assert info.has_dv

    def test_dovi_tool_and_mkvextract_take_the_path(
        self, awkward_copy: Path, toolbox, tmp_path: Path
    ) -> None:
        from hibrit.matroska import extract_video

        rpu = DoviTool(toolbox).extract_rpu(awkward_copy, tmp_path / "rpu.bin")
        assert DoviTool(toolbox).info(rpu).frames == 1000

        stream = extract_video(awkward_copy, tmp_path / "stream.hevc", toolbox)
        assert stream.stat().st_size > 0

    def test_the_whole_job_runs_on_one(
        self, awkward_copy: Path, media: Path, toolbox, tmp_path: Path
    ) -> None:
        """Source and target both awkwardly named, output too."""
        from hibrit.pipeline import run
        from hibrit.planner import build_plan
        from hibrit.verify import verify

        clip = _need(media, "p8_clip.hevc")
        clean = DoviTool(toolbox).remove(clip, tmp_path / "clean.hevc")
        target = tmp_path / f"hedef {self.AWKWARD}"
        toolbox.run("mkvmerge", ["-q", "-o", str(target), str(clean)], check=False)

        plan = build_plan(probe(awkward_copy, toolbox), probe(target, toolbox))
        assert plan.ok

        out = tmp_path / f"çıktı {self.AWKWARD}"  # noqa: RUF001
        workdir = tmp_path / "çalışma"  # noqa: RUF001
        result = run(plan, out, workdir=workdir, toolbox=toolbox)

        report = verify(
            result.output,
            target=target,
            rpu=result.rpu,
            clean_target_stream=result.clean_target_stream,
            workdir=workdir,
            toolbox=toolbox,
        )
        assert report.passed, report.describe()
        assert out.exists()


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


class TestFullEnhancementLayer:
    """A real FEL remux, which the library turned out to contain five of.

    Set HIBRIT_FEL to one of them to run these. Not part of the documented
    fixture set because it is a 60 GB film rather than a clip — but converting
    FEL to 8.1 discards a genuine enhancement layer, and that path had never
    been near a file that has one.
    """

    @pytest.fixture
    def fel_source(self) -> Path:
        import os

        configured = os.environ.get("HIBRIT_FEL")
        if not configured:
            pytest.skip("set HIBRIT_FEL to a profile 7 FEL remux")
        path = Path(configured)
        if not path.exists():
            pytest.skip(f"{path} not present")
        return path

    def test_it_really_is_fel(self, fel_source: Path, toolbox, tmp_path: Path) -> None:
        dovi = DoviTool(toolbox)
        toolbox.run(
            "dovi_tool",
            ["extract-rpu", "-l", "400", str(fel_source), "-o", str(tmp_path / "fel.bin")],
        )
        info = dovi.info(tmp_path / "fel.bin")
        assert info.profile == 7
        assert info.is_fel, "HIBRIT_FEL points at a MEL file"

    def test_the_planner_does_not_claim_to_know_this_is_fel(
        self, fel_source: Path, toolbox
    ) -> None:
        """Handed a genuine FEL film, the planner still has to say it cannot tell.

        This test used to assert the opposite — that the plan names the loss
        outright. It was written when the planner did claim to know, kept its
        old assertion when that claim was removed, and never failed, because
        HIBRIT_FEL was unset and a skipped test reports nothing. It only spoke
        up the first time a real FEL film was pointed at it.

        What it locks now is the honest behaviour. MediaInfo reports MEL and FEL
        identically as ``BL+EL+RPU``, so at planning time — before a single byte
        of RPU has been read — the difference is genuinely unknown, and saying
        so is the correct answer. The loss is named later, by the pipeline,
        after the RPU has been extracted: see
        :meth:`TestProfile7Reporting.test_a_fel_source_is_reported_as_a_loss`.
        """
        from conftest import make_info

        from hibrit.planner import build_plan

        source = probe(fel_source, toolbox)
        assert source.is_dual_layer  # true of MEL as well, which is the point

        target = make_info(
            "target.mkv",
            frames=source.frame_count,
            rate=source.frame_rate,
            width=source.width,
            height=source.height,
        )
        plan = build_plan(source, target)
        assert plan.convert_mode == 2

        text = " ".join(n.text for n in plan.notes)
        assert "depends on the enhancement layer" in text
        assert "will be reported once extracted" in text
        # Both outcomes are described; neither is asserted as this file's.
        assert "MEL" in text and "FEL" in text


class TestProfile7Reporting:
    """The pipeline says which kind of enhancement layer it just converted.

    Added in the same commit that stopped the planner guessing — and then not
    exercised, which is the exact pattern this run has been hunting. It needs a
    real profile 7 source: dovi_tool generate makes 5, 8.1 and 8.4, not 7.
    """

    def test_a_mel_source_is_reported_as_losing_nothing(
        self, media: Path, toolbox, tmp_path: Path
    ) -> None:
        from hibrit.pipeline import run
        from hibrit.planner import build_plan

        source_mkv = _need(media, "align_other.mkv")  # the profile 7 clip
        clip = _need(media, "p8_clip.hevc")

        clean = DoviTool(toolbox).remove(clip, tmp_path / "clean.hevc")
        target_mkv = tmp_path / "target.mkv"
        toolbox.run("mkvmerge", ["-q", "-o", str(target_mkv), str(clean)], check=False)

        source = probe(source_mkv, toolbox)
        assert source.dv_profile == 7

        plan = build_plan(source, probe(target_mkv, toolbox))
        assert plan.convert_mode == 2

        result = run(plan, tmp_path / "out.mkv", workdir=tmp_path / "work", toolbox=toolbox)

        layer_lines = [
            line for line in result.log if "profile 7 M" in line or "profile 7 F" in line
        ]
        assert layer_lines, f"the pipeline said nothing about the layer: {result.log}"
        assert "MEL" in layer_lines[0]
        assert "nothing was lost" in layer_lines[0]

        # And the conversion really happened.
        assert DoviTool(toolbox).info(result.rpu).profile == 8

    def test_a_fel_source_is_reported_as_a_loss(self, toolbox, tmp_path: Path) -> None:
        """HIBRIT_FEL points at a real profile 7 FEL remux."""
        import os

        from hibrit.pipeline import run
        from hibrit.planner import build_plan

        configured = os.environ.get("HIBRIT_FEL")
        if not configured:
            pytest.skip("set HIBRIT_FEL to a profile 7 FEL remux")
        fel = Path(configured)
        if not fel.exists():
            pytest.skip(f"{fel} not present")

        # A short target of matching shape, built from the FEL film itself.
        clip = tmp_path / "clip.hevc"
        toolbox.run(
            "ffmpeg",
            [
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(fel),
                "-map",
                "0:v:0",
                "-frames:v",
                "600",
                "-c",
                "copy",
                "-bsf:v",
                "hevc_mp4toannexb",
                "-f",
                "hevc",
                str(clip),
            ],
        )
        source_mkv = tmp_path / "source.mkv"
        toolbox.run("mkvmerge", ["-q", "-o", str(source_mkv), str(clip)], check=False)

        clean = DoviTool(toolbox).remove(clip, tmp_path / "clean.hevc")
        target_mkv = tmp_path / "target.mkv"
        toolbox.run("mkvmerge", ["-q", "-o", str(target_mkv), str(clean)], check=False)

        plan = build_plan(probe(source_mkv, toolbox), probe(target_mkv, toolbox))
        assert plan.convert_mode == 2

        result = run(plan, tmp_path / "out.mkv", workdir=tmp_path / "work", toolbox=toolbox)
        notes = [line for line in result.log if "profile 7 FEL" in line]
        assert notes, f"a FEL source was converted without saying so: {result.log}"
        assert "discarded" in notes[0]
        assert "not the full grade" in notes[0]
