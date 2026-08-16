"""Disk arithmetic and report semantics — the parts that must not be optimistic."""

from __future__ import annotations

import pytest
from conftest import make_info

from hibrit import pipeline
from hibrit.planner import build_plan
from hibrit.verify import Check, Report, sha256_file


class TestSpaceCheck:
    def test_refuses_before_the_first_command(self, tmp_path, monkeypatch) -> None:
        target = tmp_path / "big.mkv"
        target.write_bytes(b"x" * 1000)
        monkeypatch.setattr(pipeline, "free_space", lambda _: 100)
        with pytest.raises(pipeline.NotEnoughSpace) as caught:
            pipeline.check_space(tmp_path, target)
        assert caught.value.needed == 3000
        assert "--workdir" in str(caught.value)

    def test_allows_a_job_that_fits(self, tmp_path, monkeypatch) -> None:
        target = tmp_path / "big.mkv"
        target.write_bytes(b"x" * 1000)
        monkeypatch.setattr(pipeline, "free_space", lambda _: 10_000)
        pipeline.check_space(tmp_path, target)

    def test_free_space_walks_up_to_an_existing_parent(self, tmp_path) -> None:
        """The working directory may not exist yet when the check runs."""
        assert pipeline.free_space(tmp_path / "not" / "created" / "yet") > 0


class TestRunRefusals:
    def test_a_blocked_plan_never_starts(self, tmp_path) -> None:
        plan = build_plan(make_info("a.mkv"), make_info("b.mkv"))
        with pytest.raises(pipeline.PipelineError, match="cannot run"):
            pipeline.run(plan, tmp_path / "out.mkv", workdir=tmp_path)


class TestReport:
    def test_a_failure_sinks_the_report(self) -> None:
        report = Report(
            checks=(
                Check("a", True, "fine"),
                Check("b", False, "not fine"),
            )
        )
        assert not report.passed
        assert len(report.failures) == 1
        assert "Do not keep this output" in report.describe()

    def test_a_skipped_check_is_reported_as_unmeasured_not_passed(self) -> None:
        report = Report(checks=(Check("a", True, "fine"), Check("b", True, "-", skipped=True)))
        assert report.passed
        assert [c.name for c in report.unmeasured] == ["b"]
        assert "Not measured: b" in report.describe()

    def test_everything_measured_and_passing_says_so_plainly(self) -> None:
        assert Report(checks=(Check("a", True, "fine"),)).describe().endswith("All checks passed.")


def test_sha256_matches_hashlib(tmp_path) -> None:
    import hashlib

    path = tmp_path / "blob.bin"
    payload = bytes(range(256)) * 5000
    path.write_bytes(payload)
    assert sha256_file(path, chunk=1024) == hashlib.sha256(payload).hexdigest()
