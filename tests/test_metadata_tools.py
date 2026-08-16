"""Parsing and guard behaviour for the two external metadata tools.

The mismatch regexes matter more than they look. They are what turns
dovi_tool's warning — printed to stdout, followed by exit code 0 — into a
refusal.
"""

from __future__ import annotations

import json

import pytest

from hibrit import hdr10plus as h10p
from hibrit import rpu as rpu_mod

DOVI_INFO_P7 = """
Parsing RPU file...
Summary:
  Frames: 1000
  Profile: 7 (FEL)
  DM version: 1 (CM v2.9)
  Scene/shot count: 41
  RPU mastering display: 1000/0.0001 nits
  RPU content light level (L1 MaxCLL/MaxFALL): 853.38/43.60
  L5 offsets: top=276, bottom=277, left=0, right=0
"""

DOVI_INFO_P81 = """
Summary:
  Frames: 147624
  Profile: 8
  DM version: 2 (CM v4.0)
  Scene/shot count: 2891
"""


class TestParseInfo:
    def test_reads_a_profile_7_summary(self) -> None:
        info = rpu_mod.parse_info(DOVI_INFO_P7)
        assert info.frames == 1000
        assert info.profile == 7
        assert info.layer_kind == "FEL"
        assert info.is_fel and not info.is_mel
        assert info.scene_count == 41
        assert info.l5_offsets == (276, 277, 0, 0)

    def test_reads_a_single_layer_summary(self) -> None:
        info = rpu_mod.parse_info(DOVI_INFO_P81)
        assert (info.frames, info.profile) == (147624, 8)
        assert info.layer_kind is None
        assert info.l5_offsets is None

    def test_unparseable_output_raises_rather_than_guessing(self) -> None:
        with pytest.raises(ValueError, match="could not parse"):
            rpu_mod.parse_info("dovi_tool: command not found")

    def test_describe_is_readable(self) -> None:
        assert "P7 (FEL)" in rpu_mod.parse_info(DOVI_INFO_P7).describe()


class TestMismatchDetection:
    def test_finds_the_warning_dovi_tool_exits_zero_on(self) -> None:
        text = (
            "Warning: mismatched lengths. video 173802, RPU 5735\n"
            "Metadata will be duplicated at the end to match video length\n"
        )
        assert rpu_mod.find_mismatch(text) == (173802, 5735)

    def test_clean_output_reports_nothing(self) -> None:
        assert rpu_mod.find_mismatch("Rewriting HEVC file...\nDone.") is None
        assert rpu_mod.find_mismatch("") is None

    def test_hdr10plus_warning_is_recognised_too(self) -> None:
        assert h10p.find_mismatch("mismatched lengths. video 1000, metadata 800") == (1000, 800)

    def test_mismatch_error_explains_the_consequence(self) -> None:
        error = rpu_mod.FrameCountMismatch(1000, 800)
        assert error.delta == -200
        assert "misaligned" in str(error)
        assert "hibrit align" in str(error)


class TestHdr10PlusJson:
    def test_counts_scene_entries(self, tmp_path) -> None:
        path = tmp_path / "meta.json"
        path.write_text(
            json.dumps({"SceneInfo": [{"SequenceFrameIndex": i} for i in range(42)]}),
            encoding="utf-8",
        )
        info = h10p.read_json(path)
        assert info.frames == 42
        assert "42 frames" in info.describe()

    def test_reads_the_profile_when_present(self, tmp_path) -> None:
        path = tmp_path / "meta.json"
        path.write_text(json.dumps({"SceneInfo": [], "Profile": "B"}), encoding="utf-8")
        assert h10p.read_json(path).profile == "B"


class TestInjectGuard:
    def test_refuses_before_running_when_the_count_is_known(self, tmp_path) -> None:
        """The point of the pre-check: fail in a second, not after 68 GB."""
        meta = tmp_path / "meta.json"
        meta.write_text(
            json.dumps({"SceneInfo": [{"SequenceFrameIndex": i} for i in range(800)]}),
            encoding="utf-8",
        )
        tool = h10p.Hdr10PlusTool.__new__(h10p.Hdr10PlusTool)
        tool.box = None  # never reached; the guard fires first
        with pytest.raises(h10p.Hdr10PlusMismatch) as caught:
            tool.inject(tmp_path / "in.hevc", meta, tmp_path / "out.hevc", video_frames=1000)
        assert caught.value.delta == -200

    def test_tolerates_a_couple_of_spare_frames(self, tmp_path, monkeypatch) -> None:
        meta = tmp_path / "meta.json"
        meta.write_text(
            json.dumps({"SceneInfo": [{"SequenceFrameIndex": i} for i in range(999)]}),
            encoding="utf-8",
        )
        out = tmp_path / "out.hevc"
        out.write_bytes(b"stub")

        class FakeBox:
            def run(self, name, args, **kwargs):
                import subprocess

                return subprocess.CompletedProcess(args, 0, "", "")

        tool = h10p.Hdr10PlusTool.__new__(h10p.Hdr10PlusTool)
        tool.box = FakeBox()
        assert tool.inject(tmp_path / "in.hevc", meta, out, video_frames=1000) == out
