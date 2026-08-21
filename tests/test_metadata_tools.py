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
from hibrit.tools import UnreadableMismatch

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

#: A real **pillarboxed** remux: a 1.66:1 film in a 3840x2160 frame, so its
#: black bars are at the sides. Every other sample here has left=0, right=0,
#: which means a bug that swapped the two pairs would have passed all of them.
#: 3840 - 127 - 127 = 3586, and 3586/2160 = 1.660:1 — the film's own ratio.
#: That arithmetic is how the offsets were confirmed to be plain pixel counts
#: in the source frame, which is why the planner treats a resolution mismatch
#: as a blocker rather than a warning.
DOVI_INFO_PILLARBOX = """\
Parsing RPU file...

Summary:
  Frames: 2500
  Profile: 8
  DM version: 1 (CM v2.9)
  Scene/shot count: 5
  RPU mastering display: 0.0050/4000 nits
  RPU content light level (L1): MaxCLL: 3015.34 nits, MaxFALL: 164.74 nits
  L6 metadata: Mastering display: 0.0050/23040 nits. MaxCLL: 6987 nits, MaxFALL: 1259 nits
  L5 offsets: top=0, bottom=0, left=127, right=127
  L2 trims: 100 nits, 600 nits, 1000 nits, 2000 nits
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

#: A film whose active area **changes**, which dovi_tool reports as a range on
#: the fields that move and a plain number on the ones that do not. Every other
#: sample here holds one shape for its whole runtime, so nothing distinguished a
#: parser that reads this line from one that silently gives up on it — and
#: giving up looked exactly like the sample below, which has no L5 at all.
#:
#: Built rather than found. Four excerpts were checked for a film that changes
#: shape — 1917, 2001, Blade Runner 2049, A Clockwork Orange — and each reported
#: one set of offsets, but that is a statement about four excerpts rather than
#: about the library: an excerpt from the middle of such a film looks the same,
#: and no whole-film RPU was extracted. So this is `dovi_tool editor` given two
#: active_area presets over one real RPU (`prestige.bin`, 3000 frames: 281 for
#: the first 1500 frames, 0 for the rest) and then `dovi_tool info -s` on the
#: result. The tool wrote the line, not me.
DOVI_INFO_VARIABLE_L5 = """\
Parsing RPU file...

Summary:
  Frames: 3000
  Profile: 8
  DM version: 1 (CM v2.9)
  Scene/shot count: 27
  RPU mastering display: 0.0050/4000 nits
  RPU content light level (L1): MaxCLL: 851.47 nits, MaxFALL: 65.45 nits
  L6 metadata: Mastering display: 0.0050/4000 nits. MaxCLL: 0 nits, MaxFALL: 0 nits
  L5 offsets: top=0..281, bottom=0..281, left=0, right=0
"""

#: A real cropped WEB-DL: 3840x1606, already at its own picture, so there are no
#: bars to mask and the RPU carries no level 5 at all. `dovi_tool export
#: --levels level5=` on this file writes an empty table, which is the
#: independent confirmation that N/A means absent rather than unreported.
DOVI_INFO_NO_L5 = """\
Parsing RPU file...

Summary:
  Frames: 3000
  Profile: 8
  DM version: 2 (CM v4.0)
  Scene/shot count: 28
  RPU mastering display: 0.0001/1000 nits
  RPU content light level (L1): MaxCLL: 1000.60 nits, MaxFALL: 19.28 nits
  L6 metadata: Mastering display: 0.0001/1000 nits. MaxCLL: 0 nits, MaxFALL: 0 nits
  L5 offsets: top=N/A, bottom=N/A, left=N/A, right=N/A
  L2 trims: 100 nits, 600 nits, 1000 nits
  L8 trims: 100 nits, 600 nits
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
        assert info.l5_offsets is not None
        assert info.l5_offsets.fixed == (276, 277, 0, 0)
        assert info.dm_version == "1 (CM v2.9)"

    def test_side_offsets_are_not_confused_with_top_and_bottom(self) -> None:
        """A pillarboxed film puts its bars in the other pair of fields.

        Worth its own test because until this sample was added every fixture
        had left=0 and right=0, so nothing distinguished a correct parser from
        one that read the four numbers in the wrong order.
        """
        info = rpu_mod.parse_info(DOVI_INFO_PILLARBOX)
        assert info.l5_offsets is not None
        assert info.l5_offsets.fixed == (0, 0, 127, 127)

        top, bottom, left, right = info.l5_offsets.fixed
        assert (3840 - left - right, 2160 - top - bottom) == (3586, 2160)

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
        assert info.l5_offsets is not None
        assert info.l5_offsets.fixed == (0, 0, 0, 0)

    def test_a_film_that_changes_shape_is_not_reported_as_having_no_offsets(self) -> None:
        """The two ways this line stops being four plain numbers, told apart.

        `top=0..281` and `top=N/A` used to arrive at the same place: the regex
        wanted `(\\d+),` in every field, matched neither, and returned None for
        both. So an RPU whose active area moves through the film — the case
        where placing its offsets in another release's frame is least
        defensible — reported exactly what an RPU with no level 5 at all
        reports, which is nothing.

        Nothing downstream was wrong: the planner blocks on a resolution
        mismatch and never reads this field. What was wrong is what the user
        was shown before deciding.
        """
        variable = rpu_mod.parse_info(DOVI_INFO_VARIABLE_L5).l5_offsets
        absent = rpu_mod.parse_info(DOVI_INFO_NO_L5).l5_offsets

        assert absent is None
        assert variable is not None
        assert variable.variable
        assert variable.top == (0, 281)
        assert variable.bottom == (0, 281)
        assert variable.left == (0, 0)
        assert variable.right == (0, 0)

    def test_a_range_has_no_single_answer_to_give(self) -> None:
        """`fixed` is the one number an offset has, or None when it has none.

        A caller that wants to place bars needs one set of offsets and must not
        be handed an end of the range as though it were the answer.
        """
        variable = rpu_mod.parse_info(DOVI_INFO_VARIABLE_L5).l5_offsets
        steady = rpu_mod.parse_info(DOVI_INFO_PILLARBOX).l5_offsets
        assert variable is not None and steady is not None

        assert variable.fixed is None
        assert steady.fixed == (0, 0, 127, 127)
        assert not steady.variable

    def test_a_variable_active_area_says_so_when_printed(self) -> None:
        """What `hibrit probe` puts in front of the user.

        The offsets and the fact that they move belong on the same line: a
        reader who sees `0..281` and does not know it is a range reads it as
        one shape.
        """
        variable = rpu_mod.parse_info(DOVI_INFO_VARIABLE_L5).l5_offsets
        steady = rpu_mod.parse_info(DOVI_INFO_PILLARBOX).l5_offsets
        assert variable is not None and steady is not None

        assert str(variable) == "top=0..281, bottom=0..281, left=0, right=0 (varies)"
        assert str(steady) == "top=0, bottom=0, left=127, right=127"

    def test_an_unreadable_l5_line_raises_rather_than_reporting_no_offsets(self) -> None:
        """A fourth form must not become "no offsets" the way the first two did.

        The sample here is **deliberately invented**, which everything else in
        this file forbids — and the reason it is allowed is that the subject is
        not a form dovi_tool prints. It is any form dovi_tool does not print
        today. Three known shapes are covered by transcripts above; this covers
        the shape that has not happened yet, and the only honest way to write
        one of those is to make it up.

        Reporting None here would repeat exactly the bug this file documents:
        a line the parser could not read, arriving as a confident "there are no
        offsets". The tool said something; failing to understand it is not the
        same as it having said nothing.
        """
        unknown = DOVI_INFO_P7.replace(
            "L5 offsets: top=276, bottom=277, left=0, right=0",
            "L5 offsets: top=0-281, bottom=0-281, left=0, right=0",
        )
        assert "top=0-281" in unknown, "the sample did not actually change"

        with pytest.raises(ValueError, match="L5 offsets"):
            rpu_mod.parse_info(unknown)

    def test_a_summary_without_an_l5_line_at_all_is_not_an_error(self) -> None:
        """Absent is still absent: only an unreadable line is a failure.

        `dovi_tool` omits the line entirely for some RPUs, and that has always
        meant "nothing to report". The refusal above must not turn a quiet
        summary into a crash.
        """
        without = "\n".join(line for line in DOVI_INFO_P7.splitlines() if "L5 offsets:" not in line)
        info = rpu_mod.parse_info(without)
        assert info.l5_offsets is None
        assert info.frames == 1000

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
        assert info.l5_offsets is not None
        assert info.l5_offsets.fixed == (276, 277, 0, 0)

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

    def test_a_dovi_warning_whose_counts_cannot_be_read_is_not_silence(self) -> None:
        """The guard must not answer "no mismatch" to a mismatch it saw.

        Both samples here are **deliberately invented**, for the same reason the
        unreadable L5 line is: the subject is not a wording dovi_tool uses, it
        is any wording dovi_tool does not use yet. Reordering the two counts is
        the cheapest example of a change that leaves the warning perfectly
        legible to a human and invisible to the pattern.

        This matters more than the L5 case. `inject_rpu` has no other defence:
        if this returns None the file it just wrote is handed back as finished,
        with metadata misaligned for the whole runtime -- which is the exact
        failure the project exists to prevent, and the README's claim that
        hibrit refuses it would be quietly false.
        """
        swapped = "Warning: mismatched lengths. RPU 5735, video 173802\n"
        with pytest.raises(UnreadableMismatch, match="mismatched lengths"):
            rpu_mod.find_mismatch(swapped)

        wordier = "Warning: mismatched lengths. video 173802 frames, RPU 5735 frames\n"
        with pytest.raises(UnreadableMismatch):
            rpu_mod.find_mismatch(wordier)

    def test_an_hdr10plus_warning_whose_counts_cannot_be_read_is_not_silence(self) -> None:
        """The same hole on the other side, reached by a likelier route.

        hdr10plus_tool's counts are read as "the number after video" and "the
        last number on the line". A single trailing word defeats the second one,
        and returning None then says the tool never warned.
        """
        trailing_word = "Warning: mismatched lengths. video 240, HDR10+ JSON 150 frames\n"
        with pytest.raises(UnreadableMismatch):
            h10p.find_mismatch(trailing_word)

    def test_a_warning_that_reads_cleanly_is_still_just_a_pair(self) -> None:
        """Refusing the unreadable must not start refusing the readable."""
        assert rpu_mod.find_mismatch("mismatched lengths. video 1000, RPU 800") == (1000, 800)
        assert h10p.find_mismatch("mismatched lengths. video 240, HDR10+ JSON 150") == (240, 150)
        assert rpu_mod.find_mismatch("Rewriting HEVC file...\nDone.") is None
        assert h10p.find_mismatch("Done.") is None


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

    def test_an_unreadable_warning_takes_the_written_file_with_it(self, tmp_path) -> None:
        """Refusing is half the job; the file the tool already wrote is the rest.

        By the time the warning is read, hdr10plus_tool has finished writing.
        Raising without deleting leaves a misaligned stream on disk that looks
        exactly like a good one — the state this whole guard exists to avoid,
        reached by the guard itself.

        The reworded warning is invented on purpose: the point is any wording
        the patterns do not cover, and a trailing word after the count is enough.
        """
        meta = tmp_path / "meta.json"
        meta.write_text(
            json.dumps({"SceneInfo": [{"SequenceFrameIndex": i} for i in range(1000)]}),
            encoding="utf-8",
        )
        out = tmp_path / "out.hevc"
        out.write_bytes(b"a misaligned stream nobody should keep")

        class FakeBox:
            def run(self, name, args, **kwargs):
                import subprocess

                return subprocess.CompletedProcess(
                    args,
                    0,
                    "Warning: mismatched lengths. video 1000, HDR10+ JSON 800 frames\n",
                    "",
                )

        tool = h10p.Hdr10PlusTool.__new__(h10p.Hdr10PlusTool)
        tool.box = FakeBox()
        with pytest.raises(UnreadableMismatch):
            tool.inject(tmp_path / "in.hevc", meta, out, video_frames=1000)
        assert not out.exists()

    def test_the_rpu_side_deletes_its_file_too(self, tmp_path) -> None:
        """The same on the Dolby Vision side, where it matters most.

        Nothing else was watching here: without a frame count to check up front,
        an unreadable warning was the difference between a refusal and a
        finished-looking file with every frame's metadata belonging to another.
        """
        out = tmp_path / "out.hevc"
        out.write_bytes(b"a misaligned stream nobody should keep")

        class FakeBox:
            def run(self, name, args, **kwargs):
                import subprocess

                return subprocess.CompletedProcess(
                    args, 0, "Warning: mismatched lengths. RPU 5735, video 173802\n", ""
                )

        tool = rpu_mod.DoviTool.__new__(rpu_mod.DoviTool)
        tool.box = FakeBox()
        with pytest.raises(UnreadableMismatch):
            tool.inject_rpu(tmp_path / "in.hevc", tmp_path / "rpu.bin", out)
        assert not out.exists()
