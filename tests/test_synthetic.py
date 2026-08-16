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
from hibrit.rpu import DoviTool, FrameCountMismatch
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

    def test_hdr10plus_length_is_checked_before_the_rewrite(
        self, hdr10_clip, synthetic_hdr10plus, toolbox, tmp_path: Path
    ) -> None:
        short = tmp_path / "short.json"
        payload = json.loads(synthetic_hdr10plus.read_text(encoding="utf-8"))
        payload["SceneInfo"] = payload["SceneInfo"][:100]
        short.write_text(json.dumps(payload), encoding="utf-8")

        out = tmp_path / "bad.hevc"
        with pytest.raises(Hdr10PlusMismatch):
            Hdr10PlusTool(toolbox).inject(hdr10_clip, short, out, video_frames=FRAMES)
        assert not out.exists()


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
