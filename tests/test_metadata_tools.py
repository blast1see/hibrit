"""Parsing and guard behaviour for the two external metadata tools.

Every sample below is real output, copied from a run of dovi_tool 2.3.3. The
ones that used to be here were written from memory and got the shape wrong in
ways that mattered — the content-light-level line, the sub-lines under a
two-version DM header — so they proved that my recollection parses rather than
that the tool's output does. The same mistake in the HDR10+ mismatch pattern
left that guard dead for weeks.

The mismatch regexes matter more than they look. They are what turns a warning
— printed to stdout, followed by exit code 0 — into a refusal.
"""

from __future__ import annotations

import json

import pytest

from hibrit import hdr10plus as h10p
from hibrit import rpu as rpu_mod

#: `dovi_tool info -s` on the RPU of a real profile 7 remux clip.
DOVI_INFO_P7 = """\
Parsing RPU file...

Summary:
  Frames: 1000
  Profile: 7 (MEL)
  DM version: 1 (CM v2.9)
  Scene/shot count: 4
  RPU mastering display: 0.0050/4000 nits
  RPU content light level (L1): MaxCLL: 853.38 nits, MaxFALL: 43.60 nits
  L6 metadata: Mastering display: 0.0050/4000 nits. MaxCLL: 787 nits, MaxFALL: 239 nits
  L5 offsets: top=276, bottom=277, left=0, right=0
  L2 trims: 100 nits, 600 nits, 1000 nits
"""

#: A real profile 8.1 remux clip. Note the DM header naming two versions, with
#: indented counts underneath, and an L6 block spread over several lines — a
#: shape the invented sample did not have.
DOVI_INFO_P81 = """\
Parsing RPU file...

Summary:
  Frames: 1000
  Profile: 8
  DM version: 1 + 2 (CM 2.9 and 4.0)
    v2.9 count: 1000
    v4.0 count: 2
  Scene/shot count: 4
  RPU mastering display: 0.0001/1000 nits
  RPU content light level (L1): MaxCLL: 124.11 nits, MaxFALL: 10.05 nits
  L6 metadata
    Mastering display: 0.0001/1000 nits. MaxCLL: 0 nits, MaxFALL: 0 nits
    Mastering display: 0.0001/1000 nits. MaxCLL: 470 nits, MaxFALL: 151 nits
  L5 offsets: top=0, bottom=0, left=0, right=0
  L2 trims: 100 nits, 600 nits, 1000 nits
  L9 MDP: DCI-P3 D65
"""

#: A real profile 7 **FEL** remux. Note the single L2 target where the MEL clip
#: has three and a MaxCLL of 10000 nits, which is what a full
#: enhancement layer looks like.
DOVI_INFO_P7_FEL = """\
Parsing RPU file...

Summary:
  Frames: 400
  Profile: 7 (FEL)
  DM version: 1 (CM v2.9)
  Scene/shot count: 2
  RPU mastering display: 0.0050/4000 nits
  RPU content light level (L1): MaxCLL: 10000.00 nits, MaxFALL: 2.43 nits
  L6 metadata: Mastering display: 0.0050/4000 nits. MaxCLL: 9994 nits, MaxFALL: 412 nits
  L5 offsets: top=276, bottom=277, left=0, right=0
  L2 trims: 100 nits
"""

#: `dovi_tool generate -p 5`. Kept because a survey of 302 real remuxes and
#: WEB-DLs turned up no profile 5 at all — release groups convert it before
#: packaging — so a generated one is the only sample there is.
DOVI_INFO_P5 = """\
Parsing RPU file...

Summary:
  Frames: 240
  Profile: 5
  DM version: 2 (CM v4.0)
  Scene/shot count: 1
  RPU mastering display: 0.0000/1000 nits
  RPU content light level (L1): MaxCLL: 100.10 nits, MaxFALL: 10.05 nits
  L6 metadata: Mastering display: 0.0001/1000 nits. MaxCLL: 1000 nits, MaxFALL: 400 nits
  L5 offsets: top=0, bottom=0, left=0, right=0
  L9 MDP: DCI-P3 D65
"""


class TestParseInfo:
    def test_reads_a_profile_7_summary(self) -> None:
        info = rpu_mod.parse_info(DOVI_INFO_P7)
        assert info.frames == 1000
        assert info.profile == 7
        assert info.layer_kind == "MEL"
        assert info.is_mel and not info.is_fel
        assert info.scene_count == 4
        assert info.l5_offsets == (276, 277, 0, 0)
        assert info.dm_version == "1 (CM v2.9)"

    def test_a_two_version_dm_header_with_sub_lines(self) -> None:
        """The shape the invented sample got wrong.

        A profile 8.1 remux reports both CM versions and indents a count under
        each. The header line has to be read without swallowing what follows.
        """
        info = rpu_mod.parse_info(DOVI_INFO_P81)
        assert (info.frames, info.profile) == (1000, 8)
        assert info.layer_kind is None
        assert info.dm_version == "1 + 2 (CM 2.9 and 4.0)"
        assert info.scene_count == 4
        assert info.l5_offsets == (0, 0, 0, 0)

    def test_reads_a_generated_profile_5(self) -> None:
        info = rpu_mod.parse_info(DOVI_INFO_P5)
        assert (info.frames, info.profile) == (240, 5)
        assert info.layer_kind is None
        assert not info.is_fel and not info.is_mel

    def test_unparseable_output_raises_rather_than_guessing(self) -> None:
        with pytest.raises(ValueError, match="could not parse"):
            rpu_mod.parse_info("dovi_tool: command not found")

    def test_describe_is_readable(self) -> None:
        assert "P7 (MEL)" in rpu_mod.parse_info(DOVI_INFO_P7).describe()
        assert "4 scenes" in rpu_mod.parse_info(DOVI_INFO_P7).describe()

    def test_fel_is_read_from_a_real_fel_file(self) -> None:
        """This used to be a hand-edited word, and said so.

        The claim was that every profile 7 sample available was MEL — true of
        the one clip on hand. Surveying 302 real files found 64 FEL against 42
        MEL, so the sample above is dovi_tool's output on one of them rather
        than an edit of the MEL one.

        It matters beyond tidiness: converting FEL to 8.1 discards a real
        enhancement layer, and it is the majority case rather than the corner
        it was assumed to be.
        """
        info = rpu_mod.parse_info(DOVI_INFO_P7_FEL)
        assert info.profile == 7
        assert info.layer_kind == "FEL"
        assert info.is_fel and not info.is_mel
        assert info.frames == 400
        assert info.l5_offsets == (276, 277, 0, 0)

    def test_the_two_profile_7_kinds_are_told_apart(self) -> None:
        mel = rpu_mod.parse_info(DOVI_INFO_P7)
        fel = rpu_mod.parse_info(DOVI_INFO_P7_FEL)
        assert (mel.is_mel, mel.is_fel) == (True, False)
        assert (fel.is_mel, fel.is_fel) == (False, True)
        assert "P7 (MEL)" in mel.describe()
        assert "P7 (FEL)" in fel.describe()


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
        """The exact line hdr10plus_tool 1.7.2 prints, copied from a run.

        The first pattern here was written by analogy with dovi_tool's and
        expected a number straight after "HDR10+". The real message says
        "HDR10+ JSON 150", so it matched nothing and the guard was dead code —
        which is the failure this whole project exists to catch, sitting in the
        half of it that handles HDR10+.
        """
        real = (
            "Parsing JSON file...\n"
            "Processing input video for frame order info...\n\n"
            "Warning: mismatched lengths. video 240, HDR10+ JSON 150\n"
            "Metadata will be duplicated at the end to match video length\n"
        )
        assert h10p.find_mismatch(real) == (240, 150)

    def test_the_dovi_wording_is_still_recognised(self) -> None:
        """Loosening the pattern must not lose the case it already handled."""
        assert h10p.find_mismatch("mismatched lengths. video 1000, metadata 800") == (1000, 800)

    def test_a_longer_metadata_stream_is_caught_as_well(self) -> None:
        assert h10p.find_mismatch("mismatched lengths. video 240, HDR10+ JSON 900") == (240, 900)

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


class TestHdr10PlusComparison:
    """Comparing per-frame metadata without materialising it twice."""

    @staticmethod
    def _scene(index: int, average: int = 5) -> dict:
        return {
            "SequenceFrameIndex": index,
            "SceneFrameIndex": index,
            "SceneId": index // 10,
            "LuminanceParameters": {"AverageRGB": average},
        }

    def test_identical_payloads_match(self) -> None:
        from hibrit.verify import _payloads_match

        left = [self._scene(i) for i in range(50)]
        right = [self._scene(i) for i in range(50)]
        assert _payloads_match(left, right) is None

    def test_derived_bookkeeping_is_ignored(self) -> None:
        """The tool recomputes SceneId and SceneFrameIndex on extraction, so a
        difference there is not a difference in the metadata."""
        from hibrit.verify import _payloads_match

        left = [self._scene(i) for i in range(50)]
        right = [{**self._scene(i), "SceneId": 0, "SceneFrameIndex": 0} for i in range(50)]
        assert _payloads_match(left, right) is None

    def test_a_real_difference_is_located(self) -> None:
        from hibrit.verify import _payloads_match

        left = [self._scene(i) for i in range(50)]
        right = [self._scene(i) for i in range(50)]
        right[37]["LuminanceParameters"] = {"AverageRGB": 999}
        assert _payloads_match(left, right) == 37

    def test_mismatched_lengths_raise_rather_than_stopping_early(self) -> None:
        """Walking to the end of the shorter list would call a truncated
        metadata stream a match — the failure this project is built around."""
        from hibrit.verify import _payloads_match

        left = [self._scene(i) for i in range(50)]
        with pytest.raises(ValueError):
            _payloads_match(left, left[:30])


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
