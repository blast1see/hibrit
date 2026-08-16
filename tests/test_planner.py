"""What the planner refuses is the interesting half."""

from __future__ import annotations

from fractions import Fraction

from conftest import make_info

from hibrit.planner import Kind, Level, build_plan


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
        source = make_info("barbarian.mkv", dv=True, dv_profile=8, frames=147_624)
        target = make_info("casino.mkv", frames=256_391)
        plan = build_plan(source, target)
        assert not plan.ok
        assert "different cut" in _blocker_texts(plan)

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

    def test_profile_7_converts_with_mode_2(self) -> None:
        source = make_info("uhd.mkv", dv=True, dv_profile=7, dv_layers="BL+EL+RPU", frames=1000)
        target = make_info("bluray.mkv", frames=1000)
        plan = build_plan(source, target)
        assert plan.convert_mode == 2
        assert "enhancement layer" in _all_texts(plan)

    def test_mel_source_says_nothing_is_lost(self) -> None:
        source = make_info("uhd.mkv", dv=True, dv_profile=7, dv_layers="BL+RPU", frames=1000)
        target = make_info("bluray.mkv", frames=1000)
        plan = build_plan(source, target)
        assert "MEL" in _all_texts(plan)

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
