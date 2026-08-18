"""The middle tier: real binaries, invented material.

The tier below this one never starts a subprocess, so it cannot tell whether
the wrappers actually drive dovi_tool correctly. The tier above needs tens of
gigabytes of films. This one sits between: ``dovi_tool generate`` writes an RPU
from a JSON config with no video involved at all, ``ffmpeg`` synthesises a
ten-second HDR10 clip in a fifth of a second, and the two together exercise the
whole chain — extract, edit, inject, remux, verify — in a few seconds and a
quarter of a megabyte.

That makes it the tier a continuous integration runner can hold, which matters:
without it, every line that shells out to an external tool would be covered only
on one developer's machine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hibrit.hdr10plus import Hdr10PlusMismatch, Hdr10PlusTool, read_json
from hibrit.matroska import extract_video, remux
from hibrit.probe import probe
from hibrit.rpu import DoviTool, FrameCountMismatch, parse_info
from hibrit.tools import ToolFailed
from hibrit.verify import sha256_file, verify

pytestmark = pytest.mark.tools

#: Ten seconds at 24 fps. Long enough to hold an edit that removes 200 frames
#: and still leave something behind.
FRAMES = 240
FPS = 24
WIDTH, HEIGHT = 320, 240

#: SMPTE ST 2086 mastering display, as x265 wants it: BT.2020 primaries, D65
#: white point, 1000 nit peak. Without this the clip is not HDR10 and a profile
#: 8.1 RPU would have no base layer to describe.
MASTER_DISPLAY = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)"


def _generate_config(frames: int) -> dict:
    return {
        "length": frames,
        "source_min_pq": 0,
        "source_max_pq": 3079,
        "level5": {
            "active_area_left_offset": 0,
            "active_area_right_offset": 0,
            "active_area_top_offset": 0,
            "active_area_bottom_offset": 0,
        },
        "level6": {
            "max_display_mastering_luminance": 1000,
            "min_display_mastering_luminance": 1,
            "max_content_light_level": 1000,
            "max_frame_average_light_level": 400,
        },
    }


def _hdr10plus_json(frames: int) -> dict:
    """A minimal but valid HDR10+ profile B metadata set.

    The per-frame value that varies is ``AverageRGB``; everything else is held
    constant so that a comparison after a round-trip is checking transport
    rather than arithmetic.

    ``SceneInfoSummary`` and ``ToolInfo`` are not decoration: hdr10plus_tool
    1.7.2 refuses a file without either of them, one at a time (``missing field
    `SceneInfoSummary```, then ``missing field `ToolInfo```). One scene covering
    every frame is the honest description of a clip with no cuts in it.
    """
    return {
        "JSONInfo": {"HDR10plusProfile": "B", "Version": "1.0"},
        "SceneInfoSummary": {
            "SceneFirstFrameIndex": [0],
            "SceneFrameNumbers": [frames],
        },
        "ToolInfo": {"Tool": "hibrit test fixture", "Version": "0.1.0"},
        "SceneInfo": [
            {
                "BezierCurveData": {
                    "Anchors": [102, 205, 307, 410, 512, 614, 717, 819, 922],
                    "KneePointX": 0,
                    "KneePointY": 0,
                },
                "LuminanceParameters": {
                    "AverageRGB": 1 + (index % 97),
                    "LuminanceDistributions": {
                        "DistributionIndex": [1, 5, 10, 25, 50, 75, 90, 95, 99],
                        "DistributionValues": [0, 1171, 100, 0, 0, 0, 0, 0, 1155],
                    },
                    "MaxScl": [1324, 1324, 1324],
                },
                "NumberOfWindows": 1,
                "TargetedSystemDisplayMaximumLuminance": 400,
                "SceneFrameIndex": index,
                "SceneId": 0,
                "SequenceFrameIndex": index,
            }
            for index in range(frames)
        ],
    }


@pytest.fixture(scope="session")
def synthetic_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("synthetic")


@pytest.fixture(scope="session")
def hdr10_clip(synthetic_dir: Path, toolbox) -> Path:
    """A bare HDR10 HEVC bitstream: the thing metadata gets injected into."""
    out = synthetic_dir / "hdr10.hevc"
    toolbox.run(
        "ffmpeg",
        [
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FPS}:duration={FRAMES // FPS}",
            "-c:v",
            "libx265",
            "-pix_fmt",
            "yuv420p10le",
            "-preset",
            "ultrafast",
            "-crf",
            "30",
            "-color_primaries",
            "bt2020",
            "-color_trc",
            "smpte2084",
            "-colorspace",
            "bt2020nc",
            "-x265-params",
            f"log-level=none:master-display={MASTER_DISPLAY}:max-cll=1000,400",
            "-f",
            "hevc",
            str(out),
        ],
    )
    assert out.stat().st_size > 0
    return out


@pytest.fixture(scope="session")
def synthetic_rpu(synthetic_dir: Path, toolbox) -> Path:
    """An RPU built from a config file. No video was harmed."""
    config = synthetic_dir / "generate.json"
    config.write_text(json.dumps(_generate_config(FRAMES)), encoding="utf-8")
    out = synthetic_dir / "generated.bin"
    toolbox.run("dovi_tool", ["generate", "-j", str(config), "-o", str(out)])
    return out


@pytest.fixture(scope="session")
def synthetic_hdr10plus(synthetic_dir: Path) -> Path:
    out = synthetic_dir / "generated_hdr10plus.json"
    out.write_text(json.dumps(_hdr10plus_json(FRAMES), indent=2), encoding="utf-8")
    return out


class TestTheParserAgainstTheTool:
    """The samples in test_metadata_tools.py are transcripts. This checks them.

    A transcript goes stale the moment the tool changes its wording, and the
    tests that read it keep passing — which is exactly how the HDR10+ mismatch
    guard stayed dead for weeks. So one test parses what the tool says right
    now, and fails when that stops matching what the transcripts assume.
    """

    def test_info_output_still_has_the_shape_the_samples_record(
        self, synthetic_rpu, toolbox
    ) -> None:
        raw = toolbox.run("dovi_tool", ["info", "-i", str(synthetic_rpu), "-s"]).stdout
        parsed = parse_info(raw)

        assert parsed.frames == FRAMES
        assert parsed.profile == 8
        assert parsed.scene_count is not None
        assert parsed.l5_offsets == (0, 0, 0, 0)
        assert parsed.dm_version is not None

        # The specific strings the samples are built from.
        assert "Summary:" in raw
        assert "Frames:" in raw
        assert "Scene/shot count:" in raw
        assert "L5 offsets:" in raw

    def test_a_generated_profile_5_reports_profile_5(self, toolbox, tmp_path: Path) -> None:
        config = tmp_path / "gen.json"
        config.write_text(json.dumps(_generate_config(FRAMES)), encoding="utf-8")
        rpu = tmp_path / "p5.bin"
        toolbox.run("dovi_tool", ["generate", "-j", str(config), "-p", "5", "-o", str(rpu)])
        raw = toolbox.run("dovi_tool", ["info", "-i", str(rpu), "-s"]).stdout
        assert parse_info(raw).profile == 5


class TestGeneratedMetadata:
    def test_the_generated_rpu_says_what_was_asked_for(self, synthetic_rpu, toolbox) -> None:
        info = DoviTool(toolbox).info(synthetic_rpu)
        assert info.frames == FRAMES
        assert info.profile == 8
        assert info.l5_offsets == (0, 0, 0, 0)

    def test_the_clip_is_hdr10_and_carries_nothing_else(self, hdr10_clip, toolbox) -> None:
        """The starting point has to be a clean target, or the round-trips
        below would be measuring metadata that was there to begin with."""
        info = probe(hdr10_clip, toolbox)
        assert info.is_hevc
        assert not info.has_dv
        assert not info.has_hdr10plus


class TestRoundTrips:
    def test_dolby_vision_survives_injection(
        self, hdr10_clip, synthetic_rpu, toolbox, tmp_path: Path
    ) -> None:
        dovi = DoviTool(toolbox)
        injected = dovi.inject_rpu(hdr10_clip, synthetic_rpu, tmp_path / "with_dv.hevc")
        recovered = dovi.extract_rpu(injected, tmp_path / "back.bin")
        assert sha256_file(recovered) == sha256_file(synthetic_rpu)

    def test_hdr10plus_survives_injection(
        self, hdr10_clip, synthetic_hdr10plus, toolbox, tmp_path: Path
    ) -> None:
        """Every per-frame value comes back; the scene numbering does not.

        hdr10plus_tool decides where a scene starts by noticing the luminance
        values changed, so metadata written as one 240-frame scene reads back as
        240 scenes of one frame. The payload is identical either way, which is
        why :mod:`hibrit.verify` compares the payload rather than the file.
        """
        from hibrit.verify import _hdr10plus_payload

        tool = Hdr10PlusTool(toolbox)
        injected = tool.inject(hdr10_clip, synthetic_hdr10plus, tmp_path / "with_h10p.hevc")
        recovered = tool.extract(injected, tmp_path / "back.json")

        original = json.loads(synthetic_hdr10plus.read_text(encoding="utf-8"))["SceneInfo"]
        returned = json.loads(recovered.read_text(encoding="utf-8"))["SceneInfo"]

        assert len(returned) == len(original) == FRAMES
        assert _hdr10plus_payload(returned) == _hdr10plus_payload(original)
        # And the thing that legitimately differs, differs.
        assert [s["SceneId"] for s in returned] != [s["SceneId"] for s in original]

    def test_both_at_once_do_not_disturb_each_other(
        self, hdr10_clip, synthetic_rpu, synthetic_hdr10plus, toolbox, tmp_path: Path
    ) -> None:
        dovi = DoviTool(toolbox)
        h10p = Hdr10PlusTool(toolbox)

        step = h10p.inject(hdr10_clip, synthetic_hdr10plus, tmp_path / "a.hevc")
        both = dovi.inject_rpu(step, synthetic_rpu, tmp_path / "b.hevc")

        assert sha256_file(dovi.extract_rpu(both, tmp_path / "rpu.bin")) == sha256_file(
            synthetic_rpu
        )
        assert read_json(h10p.extract(both, tmp_path / "meta.json")).frames == FRAMES

    def test_injection_order_makes_no_difference(
        self, hdr10_clip, synthetic_rpu, synthetic_hdr10plus, toolbox, tmp_path: Path
    ) -> None:
        """Measured rather than assumed, and re-measured here so a future
        version of either tool cannot quietly make it untrue."""
        dovi = DoviTool(toolbox)
        h10p = Hdr10PlusTool(toolbox)

        first = h10p.inject(hdr10_clip, synthetic_hdr10plus, tmp_path / "h1.hevc")
        first = dovi.inject_rpu(first, synthetic_rpu, tmp_path / "hd.hevc")

        second = dovi.inject_rpu(hdr10_clip, synthetic_rpu, tmp_path / "d1.hevc")
        second = h10p.inject(second, synthetic_hdr10plus, tmp_path / "dh.hevc")

        assert sha256_file(first) == sha256_file(second)


class TestRefusals:
    def test_a_short_rpu_is_refused_and_leaves_no_file(
        self, hdr10_clip, synthetic_rpu, toolbox, tmp_path: Path
    ) -> None:
        dovi = DoviTool(toolbox)
        short = dovi.editor(
            synthetic_rpu, {"remove": ["0-99"]}, tmp_path / "short.bin", workdir=tmp_path
        )
        assert dovi.info(short).frames == FRAMES - 100

        out = tmp_path / "bad.hevc"
        with pytest.raises(FrameCountMismatch) as caught:
            dovi.inject_rpu(hdr10_clip, short, out)
        assert caught.value.delta == -100
        assert not out.exists()

    def test_a_long_rpu_is_refused_too(
        self, hdr10_clip, synthetic_rpu, toolbox, tmp_path: Path
    ) -> None:
        """Padding is the failure people notice; truncation is the one they do
        not, because the file still ends where the film ends."""
        dovi = DoviTool(toolbox)
        longer = dovi.editor(
            synthetic_rpu,
            {"duplicate": [{"source": 0, "offset": 0, "length": 60}]},
            tmp_path / "long.bin",
            workdir=tmp_path,
        )
        assert dovi.info(longer).frames == FRAMES + 60
        with pytest.raises(FrameCountMismatch):
            dovi.inject_rpu(hdr10_clip, longer, tmp_path / "bad.hevc")

    def test_a_couple_of_spare_frames_are_tolerated(
        self, hdr10_clip, synthetic_rpu, toolbox, tmp_path: Path
    ) -> None:
        """An encoder leaving two frames over is a real thing and not a
        misalignment. The guard is a tolerance, not an equality test."""
        dovi = DoviTool(toolbox)
        nudged = dovi.editor(
            synthetic_rpu,
            {"duplicate": [{"source": 0, "offset": 0, "length": 2}]},
            tmp_path / "nudged.bin",
            workdir=tmp_path,
        )
        out = dovi.inject_rpu(hdr10_clip, nudged, tmp_path / "ok.hevc")
        assert out.exists()

    @staticmethod
    def _short_metadata(source: Path, out: Path, frames: int) -> Path:
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["SceneInfo"] = payload["SceneInfo"][:frames]
        payload["SceneInfoSummary"] = {
            "SceneFirstFrameIndex": [0],
            "SceneFrameNumbers": [frames],
        }
        out.write_text(json.dumps(payload), encoding="utf-8")
        return out

    def test_hdr10plus_length_is_checked_before_the_rewrite(
        self, hdr10_clip, synthetic_hdr10plus, toolbox, tmp_path: Path
    ) -> None:
        short = self._short_metadata(synthetic_hdr10plus, tmp_path / "short.json", 100)
        out = tmp_path / "bad.hevc"
        with pytest.raises(Hdr10PlusMismatch):
            Hdr10PlusTool(toolbox).inject(hdr10_clip, short, out, video_frames=FRAMES)
        assert not out.exists()

    def test_hdr10plus_is_refused_even_without_a_frame_count_to_check(
        self, hdr10_clip, synthetic_hdr10plus, toolbox, tmp_path: Path
    ) -> None:
        """The guard that catches it after the fact, driven by the real tool.

        Without *video_frames* there is nothing to check up front, so the only
        protection is reading what hdr10plus_tool says — and it says the same
        thing dovi_tool does: a warning, a padded stream, and exit 0.

        This assertion is here because the pattern that reads that warning was
        written by analogy and matched nothing for weeks. Feeding it a synthetic
        string proved only that the string I imagined parses. This feeds it the
        tool.
        """
        short = self._short_metadata(synthetic_hdr10plus, tmp_path / "short.json", 150)
        out = tmp_path / "bad.hevc"
        with pytest.raises(Hdr10PlusMismatch) as caught:
            Hdr10PlusTool(toolbox).inject(hdr10_clip, short, out)

        assert caught.value.video_frames == FRAMES
        assert caught.value.meta_frames == 150
        assert not out.exists(), "a padded stream was left behind"


class TestProfileFive:
    """What ``-m 3`` actually does to a profile 5 RPU.

    No profile 5 release was available to develop against — every WEB-DL on
    hand had already been converted to 8.1 by its release group — so this path
    was documented from reading rather than measurement until
    ``dovi_tool generate -p 5`` turned out to make one. That is worth a test of
    its own, because what everybody says about this conversion is wrong.
    """

    @pytest.fixture
    def p5_rpu(self, synthetic_dir: Path, toolbox, tmp_path: Path) -> Path:
        config = tmp_path / "generate.json"
        config.write_text(json.dumps(_generate_config(FRAMES)), encoding="utf-8")
        out = tmp_path / "p5.bin"
        toolbox.run("dovi_tool", ["generate", "-j", str(config), "-p", "5", "-o", str(out)])
        return out

    @staticmethod
    def _export(rpu: Path, out: Path, toolbox) -> list[dict]:
        DoviTool(toolbox).export(rpu, out)
        return json.loads(out.read_text(encoding="utf-8"))

    def test_generate_really_produces_profile_5(self, p5_rpu, toolbox) -> None:
        info = DoviTool(toolbox).info(p5_rpu)
        assert info.profile == 5
        assert info.frames == FRAMES

    def test_conversion_reaches_profile_8(self, p5_rpu, toolbox, tmp_path: Path) -> None:
        dovi = DoviTool(toolbox)
        converted = dovi.editor(p5_rpu, {"mode": 3}, tmp_path / "p81.bin", workdir=tmp_path)
        after = dovi.info(converted)
        assert after.profile == 8
        assert after.frames == FRAMES

    def test_the_trims_and_the_mapping_curves_survive(
        self, p5_rpu, toolbox, tmp_path: Path
    ) -> None:
        """The claim that -m 3 discards the mapping is not what happens.

        Both the tone-mapping curves and every display-management block — the
        CM v2.9 and v4.0 trims, which is where L1 content light level, L2 trims,
        L5 active area and L6 mastering display live — come through unchanged.
        """
        dovi = DoviTool(toolbox)
        converted = dovi.editor(p5_rpu, {"mode": 3}, tmp_path / "p81.bin", workdir=tmp_path)

        before = self._export(p5_rpu, tmp_path / "before.json", toolbox)[0]
        after = self._export(converted, tmp_path / "after.json", toolbox)[0]

        assert after["rpu_data_mapping"] == before["rpu_data_mapping"]
        for block in ("cmv29_metadata", "cmv40_metadata"):
            assert after["vdr_dm_data"][block] == before["vdr_dm_data"][block], block

    def test_what_changes_is_the_colour_space_the_numbers_mean(
        self, p5_rpu, toolbox, tmp_path: Path
    ) -> None:
        """And this is the actual hazard.

        The curves were authored against an IPT-PQ-C2 base. After conversion
        they are unchanged but declared to apply to a BT.2020 PQ one, and the
        matrices that get there are swapped wholesale. Nothing is lost; what the
        surviving numbers describe is a different picture.
        """
        dovi = DoviTool(toolbox)
        converted = dovi.editor(p5_rpu, {"mode": 3}, tmp_path / "p81.bin", workdir=tmp_path)

        before = self._export(p5_rpu, tmp_path / "before.json", toolbox)[0]
        after = self._export(converted, tmp_path / "after.json", toolbox)[0]

        # IPT (2) becomes YCbCr (0).
        assert before["vdr_dm_data"]["signal_color_space"] == 2
        assert after["vdr_dm_data"]["signal_color_space"] == 0

        # Profile 5 is full range; an HDR10 base layer is not.
        assert before["header"]["bl_video_full_range_flag"] is True
        assert after["header"]["bl_video_full_range_flag"] is False

        for prefix in ("rgb_to_lms_coef", "ycc_to_rgb_coef"):
            changed = [
                index
                for index in range(9)
                if before["vdr_dm_data"][f"{prefix}{index}"]
                != after["vdr_dm_data"][f"{prefix}{index}"]
            ]
            assert changed, f"{prefix} matrix was left alone"


class TestRetiming:
    def test_an_offset_is_applied_and_the_result_then_fits(
        self, hdr10_clip, synthetic_rpu, toolbox, tmp_path: Path
    ) -> None:
        """The whole point of the editor: metadata that did not fit, made to.

        A 40-frame head trim plus a tail pad turns a 240-frame RPU meant for a
        longer source into one that matches this clip exactly, and the injection
        that was refused above now succeeds.
        """
        from hibrit.align import edit_config_for_offset

        dovi = DoviTool(toolbox)
        config = edit_config_for_offset(40, source_frames=FRAMES, target_frames=FRAMES)
        assert config["remove"] == ["0-39"]

        retimed = dovi.editor(synthetic_rpu, config, tmp_path / "retimed.bin", workdir=tmp_path)
        assert dovi.info(retimed).frames == FRAMES

        out = dovi.inject_rpu(hdr10_clip, retimed, tmp_path / "fitted.hevc")
        assert dovi.info(dovi.extract_rpu(out, tmp_path / "check.bin")).frames == FRAMES


class TestRetimingThroughThePipeline:
    """Both directions of the retiming, driven by the pipeline itself.

    ``align()`` cannot measure an offset on this material and should not be
    asked to — a clip cut at a metronomic rhythm gives a near-periodic signal
    that it refuses, correctly. So the alignment is supplied rather than
    measured, which is the same door the window uses after a person has
    approved a figure, and leaves the correlation to the tests that use real
    footage.
    """

    @staticmethod
    def _pair(clip: Path, rpu_frames: int, toolbox, tmp_path: Path) -> tuple[Path, Path]:
        """A source carrying *rpu_frames* of metadata, and a full-length target."""
        config = tmp_path / f"gen{rpu_frames}.json"
        config.write_text(json.dumps(_generate_config(rpu_frames)), encoding="utf-8")
        rpu = tmp_path / f"rpu{rpu_frames}.bin"
        toolbox.run("dovi_tool", ["generate", "-j", str(config), "-o", str(rpu)])

        short_video = tmp_path / "short.hevc"
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
                str(rpu_frames),
                "-c",
                "copy",
                "-f",
                "hevc",
                str(short_video),
            ],
        )
        source_stream = DoviTool(toolbox).inject_rpu(short_video, rpu, tmp_path / "src.hevc")

        source_mkv = tmp_path / "source.mkv"
        target_mkv = tmp_path / "target.mkv"
        toolbox.run("mkvmerge", ["-q", "-o", str(source_mkv), str(source_stream)], check=False)
        toolbox.run("mkvmerge", ["-q", "-o", str(target_mkv), str(clip)], check=False)
        return source_mkv, target_mkv

    @staticmethod
    def _alignment(offset: int):
        from hibrit.align import Alignment, Verdict

        return Alignment(
            offset=offset,
            verdict=Verdict.RELIABLE,
            confidence=9.0,
            windows=(),
            reason="supplied by the test",
        )

    @pytest.mark.parametrize(
        ("offset", "expected_edit"),
        [
            (0, "duplicate"),  # source ends early: pad the tail
            (-40, "duplicate"),  # source starts late: pad the head as well
        ],
    )
    def test_short_metadata_is_padded_to_fit(
        self, hdr10_clip, toolbox, tmp_path: Path, offset: int, expected_edit: str
    ) -> None:
        from hibrit.align import edit_config_for_offset
        from hibrit.pipeline import run
        from hibrit.planner import build_plan
        from hibrit.verify import verify

        short = FRAMES - 40
        source_mkv, target_mkv = self._pair(hdr10_clip, short, toolbox, tmp_path)

        config = edit_config_for_offset(offset, short, FRAMES)
        assert expected_edit in config

        plan = build_plan(probe(source_mkv, toolbox), probe(target_mkv, toolbox))
        assert plan.needs_alignment

        workdir = tmp_path / "work"
        result = run(
            plan,
            tmp_path / "out.mkv",
            workdir=workdir,
            toolbox=toolbox,
            alignment=self._alignment(offset),
        )

        # The whole point: the metadata now matches the video exactly, so the
        # injection could not have been the padded-and-shrugged kind.
        assert DoviTool(toolbox).info(result.rpu).frames == FRAMES

        report = verify(
            result.output,
            target=target_mkv,
            rpu=result.rpu,
            clean_target_stream=result.clean_target_stream,
            workdir=workdir,
            toolbox=toolbox,
        )
        assert report.passed, report.describe()

    def test_an_unusable_alignment_writes_nothing(
        self, hdr10_clip, toolbox, tmp_path: Path
    ) -> None:
        """Supplying a measurement is not the same as overriding the verdict.

        And the refusal has to come before the target's stream is extracted.
        Alignment reads the two original files and nothing the pipeline
        produces, so measuring it after a 68 GB extraction would charge ten
        minutes and a full copy for an answer that was available first.
        """
        from hibrit.align import Alignment, Verdict
        from hibrit.pipeline import PipelineError, run
        from hibrit.planner import build_plan

        source_mkv, target_mkv = self._pair(hdr10_clip, FRAMES - 40, toolbox, tmp_path)
        plan = build_plan(probe(source_mkv, toolbox), probe(target_mkv, toolbox))

        refused = Alignment(
            offset=-40,
            verdict=Verdict.NO_MATCH,
            confidence=1.02,
            windows=(),
            reason="not distinguishable from noise",
        )
        out = tmp_path / "out.mkv"
        workdir = tmp_path / "work"
        with pytest.raises(PipelineError, match="not accepted"):
            run(plan, out, workdir=workdir, toolbox=toolbox, alignment=refused)

        assert not out.exists()
        assert not (workdir / "target.hevc").exists(), (
            "the target stream was extracted before the offset was settled"
        )

    def test_an_offset_of_none_is_refused_before_any_work(
        self, hdr10_clip, toolbox, tmp_path: Path
    ) -> None:
        """Windows that disagreed leave no offset at all, only a verdict.

        The window can be made to hand one of these over — its override box
        forces a *figure* through, and a refusal with no figure has none.
        """
        from hibrit.align import Alignment, Verdict
        from hibrit.pipeline import PipelineError, run
        from hibrit.planner import build_plan

        source_mkv, target_mkv = self._pair(hdr10_clip, FRAMES - 40, toolbox, tmp_path)
        plan = build_plan(probe(source_mkv, toolbox), probe(target_mkv, toolbox))

        no_offset = Alignment(
            offset=None,
            verdict=Verdict.NO_MATCH,
            confidence=1.1,
            windows=(),
            reason="windows disagree",
        )
        workdir = tmp_path / "work"
        with pytest.raises(PipelineError, match="not accepted"):
            run(
                plan,
                tmp_path / "out.mkv",
                workdir=workdir,
                toolbox=toolbox,
                alignment=no_offset,
                approve=lambda _: True,  # as the window supplies it
            )
        assert not (workdir / "target.hevc").exists()


class TestHdr10PlusRetiming:
    """The half of the retiming nothing had ever run.

    Every retiming test moved Dolby Vision only, so ``Hdr10PlusTool.editor``
    was reached from exactly one place — ``pipeline._retime`` — and no test
    went through it. That is the shape the dead mismatch guard had: written,
    plausible, never executed. The guide asserted that hdr10plus_tool "uses the
    same JSON schema", which came from reading rather than from running it.
    """

    def test_the_editor_takes_the_same_config_dovi_tool_does(
        self, synthetic_hdr10plus, toolbox, tmp_path: Path
    ) -> None:
        tool = Hdr10PlusTool(toolbox)
        trimmed = tool.editor(
            synthetic_hdr10plus, {"remove": ["0-39"]}, tmp_path / "cut.json", workdir=tmp_path
        )
        assert read_json(trimmed).frames == FRAMES - 40

        padded = tool.editor(
            synthetic_hdr10plus,
            {"duplicate": [{"source": 0, "offset": 0, "length": 60}]},
            tmp_path / "pad.json",
            workdir=tmp_path,
        )
        assert read_json(padded).frames == FRAMES + 60

    def test_the_retimed_metadata_then_injects_without_a_refusal(
        self, hdr10_clip, synthetic_hdr10plus, toolbox, tmp_path: Path
    ) -> None:
        """The point of retiming: metadata that did not fit, made to.

        Trimming 40 frames off a 240-frame set gives 200, which the guard
        refuses against a 240-frame video. Padding it back to 240 is accepted —
        so the edit is doing what the pipeline needs it to, not merely running.
        """
        tool = Hdr10PlusTool(toolbox)
        short = tool.editor(
            synthetic_hdr10plus, {"remove": ["0-39"]}, tmp_path / "short.json", workdir=tmp_path
        )
        with pytest.raises(Hdr10PlusMismatch):
            tool.inject(hdr10_clip, short, tmp_path / "bad.hevc", video_frames=FRAMES)

        fitted = tool.editor(
            short,
            {"duplicate": [{"source": 0, "offset": 0, "length": 40}]},
            tmp_path / "fitted.json",
            workdir=tmp_path,
        )
        assert read_json(fitted).frames == FRAMES
        out = tool.inject(hdr10_clip, fitted, tmp_path / "ok.hevc", video_frames=FRAMES)
        assert read_json(tool.extract(out, tmp_path / "back.json")).frames == FRAMES

    def test_both_kinds_retime_together_through_the_pipeline(
        self, hdr10_clip, synthetic_rpu, synthetic_hdr10plus, toolbox, tmp_path: Path
    ) -> None:
        """A source carrying both, shorter than its target, run end to end.

        This is the path _retime edits two files on, and until now it had only
        ever edited one.
        """
        from hibrit.align import Alignment, Verdict
        from hibrit.pipeline import run
        from hibrit.planner import Kind, build_plan
        from hibrit.verify import verify

        short = FRAMES - 40
        dovi = DoviTool(toolbox)
        h10p = Hdr10PlusTool(toolbox)

        # A source with both kinds, forty frames shorter than the target.
        clipped = tmp_path / "short.hevc"
        toolbox.run(
            "ffmpeg",
            [
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(hdr10_clip),
                "-map",
                "0:v:0",
                "-frames:v",
                str(short),
                "-c",
                "copy",
                "-f",
                "hevc",
                str(clipped),
            ],
        )
        short_rpu = dovi.editor(
            synthetic_rpu,
            {"remove": [f"{short}-{FRAMES - 1}"]},
            tmp_path / "r.bin",
            workdir=tmp_path,
        )
        short_meta = h10p.editor(
            synthetic_hdr10plus,
            {"remove": [f"{short}-{FRAMES - 1}"]},
            tmp_path / "m.json",
            workdir=tmp_path,
        )
        step = h10p.inject(clipped, short_meta, tmp_path / "a.hevc", video_frames=short)
        both = dovi.inject_rpu(step, short_rpu, tmp_path / "b.hevc")

        source_mkv = tmp_path / "source.mkv"
        target_mkv = tmp_path / "target.mkv"
        toolbox.run("mkvmerge", ["-q", "-o", str(source_mkv), str(both)], check=False)
        toolbox.run("mkvmerge", ["-q", "-o", str(target_mkv), str(hdr10_clip)], check=False)

        plan = build_plan(probe(source_mkv, toolbox), probe(target_mkv, toolbox))
        assert set(plan.transfer) == {Kind.DV, Kind.HDR10PLUS}
        assert plan.needs_alignment

        workdir = tmp_path / "work"
        result = run(
            plan,
            tmp_path / "out.mkv",
            workdir=workdir,
            toolbox=toolbox,
            alignment=Alignment(
                offset=0, verdict=Verdict.RELIABLE, confidence=9.0, windows=(), reason="supplied"
            ),
        )

        # Both were stretched to the target's length, not one of them.
        assert dovi.info(result.rpu).frames == FRAMES
        assert read_json(result.hdr10plus).frames == FRAMES

        report = verify(
            result.output,
            target=target_mkv,
            rpu=result.rpu,
            hdr10plus=result.hdr10plus,
            clean_target_stream=result.clean_target_stream,
            workdir=workdir,
            toolbox=toolbox,
        )
        assert report.passed, report.describe()


class TestContainer:
    def test_a_stream_survives_a_matroska_round_trip(
        self, hdr10_clip, synthetic_rpu, toolbox, tmp_path: Path
    ) -> None:
        """mkvmerge in, mkvextract out, and the metadata still reads back."""
        dovi = DoviTool(toolbox)
        stream = dovi.inject_rpu(hdr10_clip, synthetic_rpu, tmp_path / "dv.hevc")

        wrapped = tmp_path / "wrapped.mkv"
        toolbox.run("mkvmerge", ["-q", "-o", str(wrapped), str(stream)], check=False)
        info = probe(wrapped, toolbox)
        assert info.has_dv
        assert info.frame_count == FRAMES

        unwrapped = extract_video(wrapped, tmp_path / "unwrapped.hevc", toolbox)
        recovered = dovi.extract_rpu(unwrapped, tmp_path / "back.bin")
        assert sha256_file(recovered) == sha256_file(synthetic_rpu)

    def test_the_tools_refuse_to_rewrite_a_matroska_file(
        self, hdr10_clip, synthetic_rpu, toolbox, tmp_path: Path
    ) -> None:
        """The reason the unwrap step exists at all, held in place by a test.

        Every command that rewrites a bitstream is handed a ``.mkv`` and has to
        refuse it. If a future release of either tool learns to write into a
        container, this test fails — and the unwrap step becomes optional, which
        would take a copy the size of a film out of every run. That is worth
        being told about rather than discovering years later.
        """
        wrapped = tmp_path / "wrapped.mkv"
        toolbox.run("mkvmerge", ["-q", "-o", str(wrapped), str(hdr10_clip)], check=False)
        assert wrapped.exists()

        dovi, plus = DoviTool(toolbox), Hdr10PlusTool(toolbox)
        attempts = {
            "dovi_tool remove": lambda: dovi.remove(wrapped, tmp_path / "a.hevc"),
            "dovi_tool inject-rpu": lambda: dovi.inject_rpu(
                wrapped, synthetic_rpu, tmp_path / "b.hevc"
            ),
            "hdr10plus_tool remove": lambda: plus.remove(wrapped, tmp_path / "c.hevc"),
        }
        for name, attempt in attempts.items():
            with pytest.raises(ToolFailed) as excinfo:
                attempt()
            # Each command refuses in its own words, so match on the shared
            # subject rather than pinning three exact strings that are not ours.
            message = str(excinfo.value).lower()
            assert "matroska" in message or "raw hevc bitstream" in message, f"{name}: {message}"

    def test_remux_keeps_the_donor_s_other_tracks(
        self, hdr10_clip, synthetic_rpu, toolbox, tmp_path: Path
    ) -> None:
        """Only the video is replaced; audio has to come through untouched."""
        audio = tmp_path / "tone.flac"
        toolbox.run(
            "ffmpeg",
            [
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:duration={FRAMES // FPS}",
                "-c:a",
                "flac",
                str(audio),
            ],
        )
        donor_path = tmp_path / "donor.mkv"
        toolbox.run(
            "mkvmerge",
            ["-q", "-o", str(donor_path), str(hdr10_clip), str(audio)],
            check=False,
        )
        donor = probe(donor_path, toolbox)

        stream = DoviTool(toolbox).inject_rpu(hdr10_clip, synthetic_rpu, tmp_path / "dv.hevc")
        out = remux(stream, donor, tmp_path / "out.mkv", toolbox)

        payload = json.loads(toolbox.run("mediainfo", ["--Output=JSON", str(out)]).stdout)
        kinds = [t.get("@type") for t in payload["media"]["track"]]
        assert "Audio" in kinds
        assert probe(out, toolbox).has_dv


class TestPipelineEndToEnd:
    def test_plan_run_verify_on_synthetic_material(
        self, hdr10_clip, synthetic_rpu, toolbox, tmp_path: Path
    ) -> None:
        """The whole program, start to finish, without a film on disk.

        The source is the clip with metadata injected; the target is the same
        clip without. So the correct output is known exactly: the target's
        picture carrying the source's RPU, and nothing else changed.
        """
        from hibrit.pipeline import run
        from hibrit.planner import Kind, build_plan

        dovi = DoviTool(toolbox)
        source_stream = dovi.inject_rpu(hdr10_clip, synthetic_rpu, tmp_path / "source.hevc")

        source_mkv = tmp_path / "source.mkv"
        target_mkv = tmp_path / "target.mkv"
        toolbox.run("mkvmerge", ["-q", "-o", str(source_mkv), str(source_stream)], check=False)
        toolbox.run("mkvmerge", ["-q", "-o", str(target_mkv), str(hdr10_clip)], check=False)

        plan = build_plan(probe(source_mkv, toolbox), probe(target_mkv, toolbox))
        assert plan.transfer == (Kind.DV,)
        assert plan.ok
        assert not plan.needs_alignment

        workdir = tmp_path / "work"
        result = run(plan, tmp_path / "out.mkv", workdir=workdir, toolbox=toolbox)

        report = verify(
            result.output,
            target=target_mkv,
            rpu=result.rpu,
            clean_target_stream=result.clean_target_stream,
            workdir=workdir,
            toolbox=toolbox,
        )
        assert report.passed, report.describe()
        assert not report.unmeasured, "the pixel check should have run"

    def test_a_track_name_that_no_longer_describes_the_file_is_reported(
        self, hdr10_clip, synthetic_rpu, toolbox, tmp_path: Path
    ) -> None:
        """The warning existed, was unit-tested, and had never once fired.

        Release groups write the metadata into the video track's name. hibrit
        keeps that name -- editing someone's label is not its business -- so
        after a transfer the label can be quietly wrong. matroska.py has tests
        for the wording; nothing checked that the pipeline actually asks, and
        coverage showed the ``say(f"note: {stale}")`` line never executed.

        The name here claims HDR10 and nothing else, and the file is about to
        gain Dolby Vision it does not mention. A name that claims nothing at
        all -- "Blu-ray Remux" -- is deliberately left alone instead.
        """
        from hibrit.pipeline import run
        from hibrit.planner import build_plan

        dovi = DoviTool(toolbox)
        source_stream = dovi.inject_rpu(hdr10_clip, synthetic_rpu, tmp_path / "source.hevc")

        source_mkv = tmp_path / "source.mkv"
        target_mkv = tmp_path / "target.mkv"
        toolbox.run("mkvmerge", ["-q", "-o", str(source_mkv), str(source_stream)], check=False)
        toolbox.run(
            "mkvmerge",
            [
                "-q",
                "-o",
                str(target_mkv),
                "--track-name",
                "0:UHD BluRay Remux / HDR10",
                str(hdr10_clip),
            ],
            check=False,
        )

        plan = build_plan(probe(source_mkv, toolbox), probe(target_mkv, toolbox))
        result = run(plan, tmp_path / "out.mkv", workdir=tmp_path / "work", toolbox=toolbox)

        # The name came across untouched...
        produced = probe(result.output, toolbox)
        assert produced.track.get("Title") == "UHD BluRay Remux / HDR10"

        # ...and the run said so rather than leaving it to be discovered.
        notes = [line for line in result.log if line.startswith("note:")]
        assert notes, f"the stale label was never mentioned: {result.log}"

    def test_a_job_that_does_not_fit_the_disk_never_starts(
        self, hdr10_clip, toolbox, tmp_path: Path, monkeypatch
    ) -> None:
        """Checked before the first command, not after the third."""
        from hibrit import pipeline
        from hibrit.planner import build_plan

        source_mkv = tmp_path / "s.mkv"
        target_mkv = tmp_path / "t.mkv"
        dovi = DoviTool(toolbox)
        config = tmp_path / "gen.json"
        config.write_text(json.dumps(_generate_config(FRAMES)), encoding="utf-8")
        rpu = tmp_path / "rpu.bin"
        toolbox.run("dovi_tool", ["generate", "-j", str(config), "-o", str(rpu)])
        stream = dovi.inject_rpu(hdr10_clip, rpu, tmp_path / "s.hevc")
        toolbox.run("mkvmerge", ["-q", "-o", str(source_mkv), str(stream)], check=False)
        toolbox.run("mkvmerge", ["-q", "-o", str(target_mkv), str(hdr10_clip)], check=False)

        plan = build_plan(probe(source_mkv, toolbox), probe(target_mkv, toolbox))
        monkeypatch.setattr(pipeline, "free_space", lambda _: 1024)

        with pytest.raises(pipeline.NotEnoughSpace):
            pipeline.run(plan, tmp_path / "out.mkv", workdir=tmp_path / "w", toolbox=toolbox)


class TestSkipReorder:
    """A flag that existed, could not be reached, and would have failed.

    ``extract`` took a *skip_validation* argument that passed
    ``--skip-validation``. hdr10plus_tool 1.7.2 answers that with
    ``error: unexpected argument '--skip-validation' found``. Nothing in the
    program could reach it, so nobody found out — an invented flag is the same
    failure as invented output: plausible, unrun, wrong.

    The real one is ``--skip-reorder``, which the tool describes as a
    workaround for misauthored HEVC files.
    """

    def test_the_flag_the_tool_actually_has(
        self, hdr10_clip, synthetic_hdr10plus, toolbox, tmp_path: Path
    ) -> None:
        tool = Hdr10PlusTool(toolbox)
        injected = tool.inject(hdr10_clip, synthetic_hdr10plus, tmp_path / "h.hevc")

        plain = tool.extract(injected, tmp_path / "plain.json")
        skipped = tool.extract(injected, tmp_path / "skipped.json", skip_reorder=True)

        assert read_json(plain).frames == FRAMES
        assert read_json(skipped).frames == FRAMES

    def test_the_invented_flag_would_have_been_rejected(self, hdr10_clip, toolbox) -> None:
        """Kept as evidence rather than as a story: this is what the old code
        would have produced the first time anyone reached it."""
        from hibrit.tools import ToolFailed

        with pytest.raises(ToolFailed) as caught:
            toolbox.run(
                "hdr10plus_tool",
                ["extract", "--skip-validation", str(hdr10_clip), "-o", "nowhere.json"],
            )
        assert "unexpected argument" in str(caught.value)

    def test_a_file_with_no_hdr10plus_fails_rather_than_writing_nothing(
        self, hdr10_clip, toolbox, tmp_path: Path
    ) -> None:
        """The other way extraction ends. It exits 1 and writes no file, so the
        wrapper raises instead of handing back a path to something absent."""
        from hibrit.tools import ToolFailed

        out = tmp_path / "none.json"
        with pytest.raises(ToolFailed):
            Hdr10PlusTool(toolbox).extract(hdr10_clip, out)
        assert not out.exists()


class TestTrackLabelling:
    """What ``--no-video`` takes away with the donor's video track.

    The new video track is built from a raw Annex B stream and carries no name,
    no language and no flags. Dropping the donor's video track drops its
    labelling too, so a remux whose video track was called "Blu-ray Remux" came
    out with an unnamed one. Measured on a target built for the purpose, since
    none of the test clips had a label to lose.
    """

    @staticmethod
    def _properties(path: Path, toolbox) -> dict:
        payload = json.loads(toolbox.run("mkvmerge", ["-J", str(path)], check=False).stdout)
        video = next(t for t in payload["tracks"] if t["type"] == "video")
        return video["properties"]

    @pytest.fixture
    def labelled_target(self, hdr10_clip, toolbox, tmp_path: Path) -> Path:
        out = tmp_path / "labelled.mkv"
        toolbox.run(
            "mkvmerge",
            [
                "-q",
                "-o",
                str(out),
                "--track-name",
                "0:Blu-ray Remux",
                "--language",
                "0:eng",
                str(hdr10_clip),
            ],
            check=False,
        )
        return out

    def test_the_name_and_language_survive_the_remux(
        self, labelled_target, synthetic_rpu, toolbox, tmp_path: Path
    ) -> None:
        from hibrit.matroska import extract_video, remux

        stream = extract_video(labelled_target, tmp_path / "t.hevc", toolbox)
        injected = DoviTool(toolbox).inject_rpu(stream, synthetic_rpu, tmp_path / "dv.hevc")
        out = remux(injected, probe(labelled_target, toolbox), tmp_path / "out.mkv", toolbox)

        before = self._properties(labelled_target, toolbox)
        after = self._properties(out, toolbox)
        assert after["track_name"] == before["track_name"] == "Blu-ray Remux"
        assert after["language"] == before["language"] == "eng"

    def test_nothing_is_invented_for_a_target_that_had_none(
        self, hdr10_clip, synthetic_rpu, toolbox, tmp_path: Path
    ) -> None:
        """Supplying a default for something the target did not set would be
        inventing metadata, which is the one thing this program never does."""
        from hibrit.matroska import remux, video_track_properties

        plain = tmp_path / "plain.mkv"
        toolbox.run("mkvmerge", ["-q", "-o", str(plain), str(hdr10_clip)], check=False)
        assert video_track_properties(plain, toolbox) == []

        injected = DoviTool(toolbox).inject_rpu(hdr10_clip, synthetic_rpu, tmp_path / "dv.hevc")
        out = remux(injected, probe(plain, toolbox), tmp_path / "out.mkv", toolbox)
        assert not self._properties(out, toolbox).get("track_name")

    def test_the_donor_s_other_tracks_and_chapters_still_come_across(
        self, hdr10_clip, synthetic_rpu, toolbox, tmp_path: Path
    ) -> None:
        """The half that already worked, asserted so a fix to the other half
        cannot quietly break it."""
        from hibrit.matroska import remux

        audio = tmp_path / "tone.flac"
        toolbox.run(
            "ffmpeg",
            [
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=10",
                "-c:a",
                "flac",
                str(audio),
            ],
        )
        chapters = tmp_path / "chapters.txt"
        chapters.write_text(
            "CHAPTER01=00:00:00.000\nCHAPTER01NAME=One\n"
            "CHAPTER02=00:00:05.000\nCHAPTER02NAME=Two\n",
            encoding="utf-8",
        )
        donor_path = tmp_path / "donor.mkv"
        toolbox.run(
            "mkvmerge",
            [
                "-q",
                "-o",
                str(donor_path),
                str(hdr10_clip),
                "--track-name",
                "0:Turkish",
                "--language",
                "0:tur",
                str(audio),
                "--chapters",
                str(chapters),
            ],
            check=False,
        )

        injected = DoviTool(toolbox).inject_rpu(hdr10_clip, synthetic_rpu, tmp_path / "dv.hevc")
        out = remux(injected, probe(donor_path, toolbox), tmp_path / "out.mkv", toolbox)

        payload = json.loads(toolbox.run("mkvmerge", ["-J", str(out)], check=False).stdout)
        audio_tracks = [t for t in payload["tracks"] if t["type"] == "audio"]
        assert len(audio_tracks) == 1
        assert audio_tracks[0]["properties"]["track_name"] == "Turkish"
        assert audio_tracks[0]["properties"]["language"] == "tur"
        assert payload.get("chapters"), "the donor's chapters were dropped"


class TestVerificationCosts:
    """What the picture check actually spends, as opposed to what it claimed.

    verify.py used to state that neither kind of check rewrites anything. The
    metadata checks do not. The picture check does: the result is Matroska,
    picture_digest needs Annex B, so the finished file's video track is
    extracted first — another full copy. On a 70 GB output that is 68 GB, and
    it lands in the same working directory the run just used.
    """

    def test_the_picture_check_writes_a_copy_of_the_stream(
        self, hdr10_clip, synthetic_rpu, toolbox, tmp_path: Path
    ) -> None:
        from hibrit.verify import verify

        dovi = DoviTool(toolbox)
        injected = dovi.inject_rpu(hdr10_clip, synthetic_rpu, tmp_path / "dv.hevc")
        result = tmp_path / "out.mkv"
        toolbox.run("mkvmerge", ["-q", "-o", str(result), str(injected)], check=False)

        workdir = tmp_path / "work"
        workdir.mkdir()
        report = verify(
            result,
            rpu=synthetic_rpu,
            clean_target_stream=hdr10_clip,
            workdir=workdir,
            toolbox=toolbox,
        )
        assert report.passed, report.describe()

        # It cleans up after itself, so measure the peak by watching the call
        # rather than the aftermath.
        assert not list(workdir.glob("verify_result.hevc"))

    def test_the_peak_is_what_the_space_factor_was_sized_for(
        self, hdr10_clip, synthetic_rpu, toolbox, tmp_path: Path
    ) -> None:
        """The claim SPACE_FACTOR now records, checked rather than asserted.

        Anyone trimming the factor because "verification is only reads" would
        make a job that runs and then cannot verify itself.
        """
        from hibrit import verify as verify_module
        from hibrit.verify import verify

        dovi = DoviTool(toolbox)
        injected = dovi.inject_rpu(hdr10_clip, synthetic_rpu, tmp_path / "dv.hevc")
        result = tmp_path / "out.mkv"
        toolbox.run("mkvmerge", ["-q", "-o", str(result), str(injected)], check=False)

        workdir = tmp_path / "work"
        workdir.mkdir()
        seen: list[int] = []
        real_digest = verify_module.picture_digest

        def watching(path, **kwargs):
            # Called once the extraction has happened; this is the moment the
            # working directory is fullest.
            seen.append(sum(p.stat().st_size for p in workdir.glob("*") if p.is_file()))
            return real_digest(path, **kwargs)

        verify_module.picture_digest = watching
        try:
            verify(
                result,
                clean_target_stream=hdr10_clip,
                workdir=workdir,
                toolbox=toolbox,
            )
        finally:
            verify_module.picture_digest = real_digest

        assert seen, "the picture check did not run"
        assert max(seen) >= result.stat().st_size * 0.5, (
            "the extraction did not happen — if this is now free, SPACE_FACTOR "
            "and the comment on it can both be revisited"
        )
