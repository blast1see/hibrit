"""What the planner refuses is the interesting half."""

from __future__ import annotations

from fractions import Fraction

from conftest import make_info

from hibrit.planner import Kind, Level, build_plan, parse_kinds


def _blocker_texts(plan) -> str:
    return " ".join(n.text for n in plan.blockers)


def _all_texts(plan) -> str:
    return " ".join(n.text for n in plan.notes)


class TestWhatMoves:
    def test_dv_from_webdl_to_hdr10_bluray(self) -> None:
        source = make_info("web.mkv", dv=True, dv_profile=8, frames=147_624)
        target = make_info("bluray.mkv", frames=148_000)
        plan = build_plan(source, target)
        assert plan.transfer == (Kind.DV,)
        assert plan.ok
        assert plan.convert_mode is None

    def test_hdr10plus_from_webdl_to_dv_bluray(self) -> None:
        source = make_info("web.mkv", hdr10plus=True, frames=1000)
        target = make_info("bluray.mkv", dv=True, dv_profile=7, frames=1000)
        plan = build_plan(source, target)
        assert plan.transfer == (Kind.HDR10PLUS,)
        assert not plan.needs_alignment

    def test_both_kinds_at_once(self) -> None:
        source = make_info("web.mkv", dv=True, dv_profile=8, hdr10plus=True, frames=1000)
        target = make_info("bluray.mkv", frames=1000)
        plan = build_plan(source, target)
        assert set(plan.transfer) == {Kind.DV, Kind.HDR10PLUS}

    def test_metadata_the_target_already_has_is_left_alone(self) -> None:
        source = make_info("web.mkv", dv=True, dv_profile=8, frames=1000)
        target = make_info("bluray.mkv", dv=True, dv_profile=7, frames=1000)
        plan = build_plan(source, target)
        assert plan.transfer == ()
        assert not plan.ok

    def test_nothing_left_to_do_is_not_the_same_as_files_swapped(self) -> None:
        """Two different dead ends deserve two different sentences.

        Telling someone whose target already has the metadata to check the file
        order sends them hunting for a mistake they did not make. The fix is a
        flag, and the message should say so.
        """
        source = make_info("web.mkv", dv=True, dv_profile=8, frames=1000)
        target = make_info("bluray.mkv", dv=True, dv_profile=7, frames=1000)
        blocked = _blocker_texts(build_plan(source, target))
        assert "--replace" in blocked
        assert "right way round" not in blocked

        # And the genuinely-swapped case still says what it said.
        swapped = _blocker_texts(build_plan(make_info("a.mkv"), make_info("b.mkv")))
        assert "right way round" in swapped

    def test_replace_forces_the_overwrite_and_warns(self) -> None:
        source = make_info("web.mkv", dv=True, dv_profile=8, frames=1000)
        target = make_info("bluray.mkv", dv=True, dv_profile=7, frames=1000)
        plan = build_plan(source, target, replace_existing=True)
        assert plan.transfer == (Kind.DV,)
        assert any(n.level is Level.WARNING for n in plan.notes)


class TestRefusals:
    def test_avc_target_cannot_receive_metadata(self) -> None:
        source = make_info("web.mkv", dv=True, dv_profile=8, frames=1000)
        target = make_info("old.mkv", codec="AVC", frames=1000)
        plan = build_plan(source, target)
        assert not plan.ok
        assert "HEVC" in _blocker_texts(plan)

    def test_resolution_mismatch_is_refused(self) -> None:
        source = make_info("web.mkv", dv=True, dv_profile=8, width=1920, height=1080, frames=1000)
        target = make_info("bluray.mkv", frames=1000)
        plan = build_plan(source, target)
        assert not plan.ok
        assert "level 5" in _blocker_texts(plan)

    def test_the_refusal_gives_the_reason_that_applies_to_this_transfer(self) -> None:
        """A cropped WEB-DL donating only HDR10+ used to be refused with a
        sentence about RPU level 5 offsets, which were not being transferred.

        This is the shape of a real pair: an iTunes WEB-DL is cropped to its
        2.40:1 picture, the Blu-ray remux keeps the bars in the frame, and the
        remux already has its own Dolby Vision — so HDR10+ is the only thing
        moving. The mismatch still matters, but for a different reason, and
        saying the wrong one teaches the reader something untrue.
        """
        source = make_info(
            "web.mkv", width=3840, height=1606, frames=1000, hdr10plus=True, dv=True, dv_profile=8
        )
        target = make_info("remux.mkv", width=3840, height=2160, frames=1000, dv=True, dv_profile=7)
        plan = build_plan(source, target)

        assert [k.label for k in plan.transfer] == ["HDR10+"]
        text = _blocker_texts(plan)
        assert not plan.ok
        assert "level 5" not in text
        assert "per-frame measurements of the frame the encoder saw" in text
        assert "--allow-crop" in text  # the way out is named where it exists

    def test_allow_crop_downgrades_the_hdr10plus_case_to_a_warning(self) -> None:
        source = make_info(
            "web.mkv", width=3840, height=1606, frames=1000, hdr10plus=True, dv=True, dv_profile=8
        )
        target = make_info("remux.mkv", width=3840, height=2160, frames=1000, dv=True, dv_profile=7)
        plan = build_plan(source, target, allow_crop=True)

        assert plan.ok
        assert plan.transfer == (Kind.HDR10PLUS,)
        warned = " ".join(n.text for n in plan.warnings)
        assert "--allow-crop was passed" in warned
        # The warning has to say the checks cannot catch this, or a green
        # verification report reads as confirmation that it was fine.
        assert "every check will pass" in warned

    def test_allow_crop_does_not_wave_through_a_moving_rpu(self) -> None:
        """The flag accepts measurements taken elsewhere, not misplaced geometry.

        A cropped source's level 5 offsets point at rows the target frame does
        not have. There is no sense in which the user can knowingly accept that
        — the result is simply wrong — so the flag deliberately stops short.
        """
        source = make_info("web.mkv", width=3840, height=1606, frames=1000, dv=True, dv_profile=8)
        target = make_info("remux.mkv", width=3840, height=2160, frames=1000)
        plan = build_plan(source, target, allow_crop=True)

        assert not plan.ok
        text = _blocker_texts(plan)
        assert "does not cover this" in text
        assert "level 5" in text

    def test_the_rpu_refusal_does_not_advertise_a_flag_that_would_not_help(self) -> None:
        source = make_info("web.mkv", width=3840, height=1606, frames=1000, dv=True, dv_profile=8)
        target = make_info("remux.mkv", width=3840, height=2160, frames=1000)
        assert "--allow-crop" not in _blocker_texts(build_plan(source, target))

    def test_a_blocked_rpu_names_what_can_still_move(self) -> None:
        """Refusing without saying so leaves a usable job looking impossible.

        A cropped source cannot donate its RPU -- the geometry rules it out for
        good -- but its HDR10+ is a judgement the user is allowed to make. The
        refusal used to stop at the RPU and leave them there.
        """
        source = make_info(
            "web.mkv", width=3840, height=1598, frames=1000, dv=True, dv_profile=8, hdr10plus=True
        )
        target = make_info("remux.mkv", width=3840, height=2160, frames=1000)
        plan = build_plan(source, target)

        assert set(plan.transfer) == {Kind.DV, Kind.HDR10PLUS}
        assert not plan.ok
        text = _blocker_texts(plan)
        assert "--only hdr10plus" in text
        assert "--allow-crop" in text

    def test_the_hint_is_absent_when_only_the_rpu_was_moving(self) -> None:
        """Nothing else was going anywhere, so there is nothing to suggest."""
        source = make_info("web.mkv", width=3840, height=1598, frames=1000, dv=True, dv_profile=8)
        target = make_info("remux.mkv", width=3840, height=2160, frames=1000)
        assert "--only" not in _blocker_texts(build_plan(source, target))

    def test_only_narrows_the_transfer(self) -> None:
        source = make_info(
            "web.mkv", width=3840, height=1598, frames=1000, dv=True, dv_profile=8, hdr10plus=True
        )
        target = make_info("remux.mkv", width=3840, height=2160, frames=1000)

        plan = build_plan(source, target, kinds=(Kind.HDR10PLUS,), allow_crop=True)
        assert plan.transfer == (Kind.HDR10PLUS,)
        assert plan.ok, [n.text for n in plan.blockers]

    def test_kind_names_are_what_a_person_would_type(self) -> None:
        assert parse_kinds(["dv"]) == (Kind.DV,)
        assert parse_kinds(["hdr10plus"]) == (Kind.HDR10PLUS,)
        assert parse_kinds(["hdr10+"]) == (Kind.HDR10PLUS,)
        # Order is fixed and duplicates collapse, so two spellings of one kind
        # cannot produce a transfer list that repeats it.
        assert parse_kinds(["hdr10+", "dv", "DV"]) == (Kind.DV, Kind.HDR10PLUS)

    def test_an_unknown_kind_is_refused_with_the_list(self) -> None:
        import pytest as _pytest

        with _pytest.raises(ValueError, match="dv, hdr10plus"):
            parse_kinds(["hdr10"])

    def test_nothing_to_transfer_is_not_buried_under_how_to_transfer_it(self) -> None:
        """A Hybrid remux against a WEB-DL of the same film: both already have both.

        The one thing worth saying is that there is nothing to move. What came
        out instead was that line plus a resolution blocker offering
        --allow-crop, a frame-rate blocker, and a retiming warning -- three
        answers to a question nobody asked, and the flag could not have helped,
        because no metadata was moving for it to apply to.
        """
        source = make_info(
            "web.mkv",
            width=3840,
            height=1598,
            frames=187_412,
            rate=Fraction(24000, 1001),
            hdr10plus=True,
            dv=True,
            dv_profile=8,
        )
        target = make_info(
            "remux.mkv",
            width=3840,
            height=2160,
            frames=187_467,
            rate=Fraction(24000, 1001),
            hdr10plus=True,
            dv=True,
            dv_profile=8,
        )
        plan = build_plan(source, target)

        assert plan.transfer == ()
        assert not plan.ok
        assert len(plan.blockers) == 1, [n.text for n in plan.blockers]

        text = _blocker_texts(plan)
        assert "nothing left to transfer" in text
        assert "--allow-crop" not in text
        assert "resolution differs" not in text

        everything = _all_texts(plan)
        assert "frame rate differs" not in everything
        assert "frame counts differ" not in everything
        assert not plan.needs_alignment

    def test_the_mechanics_still_apply_once_something_moves(self) -> None:
        """The guard must not silence the checks when they are the point."""
        source = make_info(
            "web.mkv", width=3840, height=1598, frames=1000, hdr10plus=True, dv=True, dv_profile=8
        )
        target = make_info("remux.mkv", width=3840, height=2160, frames=1008, dv=True, dv_profile=7)
        plan = build_plan(source, target)

        assert plan.transfer == (Kind.HDR10PLUS,)
        assert "resolution differs" in _blocker_texts(plan)
        assert "frame counts differ" in _all_texts(plan)

    def test_frame_rate_mismatch_is_refused(self) -> None:
        source = make_info("web.mkv", dv=True, dv_profile=8, rate=Fraction(25), frames=1000)
        target = make_info("bluray.mkv", rate=Fraction(24000, 1001), frames=1000)
        plan = build_plan(source, target)
        assert not plan.ok
        assert "one entry per frame" in _blocker_texts(plan)

    def test_source_with_nothing_to_give(self) -> None:
        plan = build_plan(make_info("a.mkv"), make_info("b.mkv"))
        assert not plan.ok
        assert "the right way round" in _blocker_texts(plan)

    def test_wildly_different_runtimes_are_refused(self) -> None:
        """108k frames apart is not a trimmed logo; it is a different film."""
        source = make_info("a.mkv", dv=True, dv_profile=8, frames=147_624)
        target = make_info("b.mkv", frames=256_391)
        plan = build_plan(source, target)
        assert not plan.ok
        assert "different cut" in _blocker_texts(plan)
        assert "76 minutes" in _blocker_texts(plan)

    def test_a_big_fraction_of_a_short_clip_is_not_a_different_cut(self) -> None:
        """A percentage on its own is the wrong test.

        200 frames is 20% of a thousand-frame clip and still only eight
        seconds — a logo, not a different edit. Blocking on the fraction alone
        refuses a job that is perfectly ordinary; the gap has to be large *and*
        long enough to be structural.
        """
        source = make_info("a.mkv", dv=True, dv_profile=8, frames=1000)
        plan = build_plan(source, make_info("b.mkv", frames=800))
        assert plan.ok
        assert plan.needs_alignment

    def test_a_gap_that_is_both_large_and_long_is_refused(self) -> None:
        source = make_info("a.mkv", dv=True, dv_profile=8, frames=100_000)
        plan = build_plan(source, make_info("b.mkv", frames=80_000))
        assert not plan.ok
        assert "14 minutes" in _blocker_texts(plan)

    def test_a_long_gap_that_is_a_small_fraction_is_allowed(self) -> None:
        """Two minutes of credits on a three-hour film is 1% and normal."""
        source = make_info("a.mkv", dv=True, dv_profile=8, frames=260_000)
        plan = build_plan(source, make_info("b.mkv", frames=257_000))
        assert plan.ok
        assert plan.needs_alignment

    def test_unhandled_dv_profile_is_refused(self) -> None:
        source = make_info("odd.mkv", dv=True, dv_profile=4, frames=1000)
        target = make_info("bluray.mkv", frames=1000)
        plan = build_plan(source, target)
        assert not plan.ok
        assert "not handled" in _blocker_texts(plan)


class TestProfileHandling:
    def test_profile_5_converts_with_mode_3_and_warns(self) -> None:
        source = make_info("web.mkv", dv=True, dv_profile=5, hdr10=False, frames=1000)
        target = make_info("bluray.mkv", frames=1000)
        plan = build_plan(source, target)
        assert plan.convert_mode == 3
        assert "IPT-PQ-C2" in _all_texts(plan)

    def test_the_profile_5_warning_describes_what_was_measured(self) -> None:
        """It is widely repeated that -m 3 drops the mapping. It does not: the
        curves and trims come through untouched and the colour space they are
        read in is rewritten. See test_synthetic.TestProfileFive."""
        source = make_info("web.mkv", dv=True, dv_profile=5, hdr10=False, frames=1000)
        plan = build_plan(source, make_info("bluray.mkv", frames=1000))
        text = _all_texts(plan)
        assert "keeps every trim and every mapping curve" in text
        assert "rewrites the colour space" in text

    def test_profile_7_converts_with_mode_2(self) -> None:
        source = make_info("uhd.mkv", dv=True, dv_profile=7, dv_layers="BL+EL+RPU", frames=1000)
        target = make_info("bluray.mkv", frames=1000)
        plan = build_plan(source, target)
        assert plan.convert_mode == 2
        assert "enhancement layer" in _all_texts(plan)

    def test_it_does_not_claim_to_know_mel_from_fel(self) -> None:
        """MediaInfo reports both as "BL+EL+RPU".

        This note used to promise that the mapping would be discarded whenever
        the source was dual-layer — which is every real profile 7 file, MEL
        included, where nothing is lost at all. The container cannot tell them
        apart; the RPU can, and the pipeline says so once it has read it.
        """
        source = make_info("uhd.mkv", dv=True, dv_profile=7, dv_layers="BL+EL+RPU", frames=1000)
        text = _all_texts(build_plan(source, make_info("bluray.mkv", frames=1000)))
        assert "does not record which" in text
        assert "MEL" in text and "FEL" in text

    def test_the_profile_7_note_is_a_warning_because_the_loss_is_likely(self) -> None:
        """Measured across a sample of real remuxes: 64 FEL against 42 MEL."""
        source = make_info("uhd.mkv", dv=True, dv_profile=7, dv_layers="BL+EL+RPU", frames=1000)
        plan = build_plan(source, make_info("bluray.mkv", frames=1000))
        levels = {n.level for n in plan.notes if "profile 7" in n.text}
        assert Level.WARNING in levels

    def test_target_without_hdr10_base_is_flagged(self) -> None:
        source = make_info("web.mkv", dv=True, dv_profile=8, frames=1000)
        target = make_info("bluray.mkv", hdr10=False, frames=1000)
        plan = build_plan(source, target)
        assert plan.ok
        assert "nothing to fall back to" in _all_texts(plan)


class TestAlignmentFlag:
    def test_equal_frame_counts_need_no_alignment(self) -> None:
        source = make_info("a.mkv", dv=True, dv_profile=8, frames=1000)
        target = make_info("b.mkv", frames=1000)
        assert not build_plan(source, target).needs_alignment

    def test_small_difference_needs_alignment_and_warns_about_padding(self) -> None:
        source = make_info("a.mkv", dv=True, dv_profile=8, frames=1137)
        target = make_info("b.mkv", frames=1000)
        plan = build_plan(source, target)
        assert plan.needs_alignment
        assert "exits successfully" in _all_texts(plan)

    def test_unknown_frame_count_is_reported_not_assumed(self) -> None:
        source = make_info("a.mkv", dv=True, dv_profile=8, frames=None)
        target = make_info("b.mkv", frames=1000)
        plan = build_plan(source, target)
        assert "could not be read" in _all_texts(plan)


def test_same_master_assumption_is_always_stated() -> None:
    """The one failure mode no check can catch has to be said out loud."""
    source = make_info("a.mkv", dv=True, dv_profile=8, frames=1000)
    target = make_info("b.mkv", frames=1000)
    assert "same master" in _all_texts(build_plan(source, target))


def test_describe_mentions_the_blockers_instead_of_the_steps() -> None:
    plan = build_plan(make_info("a.mkv"), make_info("b.mkv"))
    assert "cannot run" in plan.describe()
