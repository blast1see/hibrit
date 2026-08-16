"""Argument parsing and exit codes.

Exit codes are the part a script sees. ``hibrit plan`` returning 0 for a job
that cannot run would make a shell loop happily feed impossible pairs into
``hibrit run``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_info

from hibrit import cli


class TestParser:
    def test_workdir_is_required_for_run(self) -> None:
        """No default: the drive holding the sources is usually the wrong one."""
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["run", "a.mkv", "b.mkv", "-o", "out.mkv"])

    def test_run_accepts_a_workdir(self) -> None:
        args = cli.build_parser().parse_args(
            ["run", "a.mkv", "b.mkv", "-o", "out.mkv", "-w", "E:/work"]
        )
        assert args.workdir == "E:/work"
        assert not args.yes and not args.dry_run

    def test_a_subcommand_is_required(self) -> None:
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([])

    def test_align_defaults_to_two_windows(self) -> None:
        """One window can agree with itself. Two can disagree."""
        args = cli.build_parser().parse_args(["align", "a.mkv", "b.mkv"])
        assert args.windows == 2
        assert args.max_shift is None

    def test_tools_dir_is_repeatable(self) -> None:
        args = cli.build_parser().parse_args(["--tools-dir", "one", "--tools-dir", "two", "doctor"])
        assert args.tools_dir == ["one", "two"]


class TestExitCodes:
    def test_plan_returns_non_zero_when_the_job_cannot_run(self, monkeypatch) -> None:
        monkeypatch.setattr(cli, "probe", lambda path, box: make_info(str(path)))
        args = cli.build_parser().parse_args(["plan", "a.mkv", "b.mkv"])
        assert cli.cmd_plan(args) == 1

    def test_plan_returns_zero_for_a_workable_job(self, monkeypatch) -> None:
        def fake_probe(path, box):
            if str(path) == "a.mkv":
                return make_info("a.mkv", dv=True, dv_profile=8, frames=1000)
            return make_info("b.mkv", frames=1000)

        monkeypatch.setattr(cli, "probe", fake_probe)
        args = cli.build_parser().parse_args(["plan", "a.mkv", "b.mkv"])
        assert cli.cmd_plan(args) == 0

    def test_a_missing_tool_exits_2_and_points_at_doctor(self, capsys, monkeypatch) -> None:
        from hibrit.tools import MissingTool

        def explode(_args):
            raise MissingTool("dovi_tool")

        monkeypatch.setattr(cli, "cmd_doctor", explode)
        code = cli.main(["doctor"])
        assert code == 2
        assert "hibrit doctor" in capsys.readouterr().err

    def test_interrupt_says_nothing_further_was_written(self, capsys, monkeypatch) -> None:
        def explode(_args):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "cmd_doctor", explode)
        assert cli.main(["doctor"]) == 130
        assert "nothing further was written" in capsys.readouterr().err

    def test_a_missing_path_names_which_argument_was_wrong(self, capsys, tmp_path) -> None:
        """This subcommand takes five paths. A bare FileNotFoundError traceback
        does not say which of them the user got wrong."""
        result = tmp_path / "result.mkv"
        result.write_bytes(b"not really a matroska file")

        code = cli.main(
            [
                "verify",
                str(result),
                "--clean-stream",
                str(tmp_path / "gone.hevc"),
                "-w",
                str(tmp_path),
            ]
        )
        assert code == 2
        message = capsys.readouterr().err
        assert "--clean-stream" in message
        assert "Traceback" not in message

    def test_the_first_bad_path_is_the_one_reported(self, capsys, tmp_path) -> None:
        code = cli.main(["verify", str(tmp_path / "gone.mkv"), "-w", str(tmp_path)])
        assert code == 2
        assert "result does not exist" in capsys.readouterr().err

    def test_version_is_the_packaged_version_and_stops(self, capsys) -> None:
        from hibrit import __version__

        with pytest.raises(SystemExit):
            cli.main(["--version"])
        assert __version__ in capsys.readouterr().out


class TestRunDecisions:
    """The decisions `hibrit run` makes before and after the pipeline.

    Every one of these was exercised by hand and none of them by a test, which
    is backwards: they are the branches that decide whether a file gets written
    at all.
    """

    @pytest.fixture
    def workable(self, monkeypatch, tmp_path):
        """Two files that exist and produce a runnable plan."""
        source = tmp_path / "source.mkv"
        target = tmp_path / "target.mkv"
        for path in (source, target):
            path.write_bytes(b"pretend this is a matroska file")

        def fake_probe(path, box):
            if Path(path).name == "source.mkv":
                return make_info(str(path), dv=True, dv_profile=8, frames=1000)
            return make_info(str(path), frames=1000)

        monkeypatch.setattr(cli, "probe", fake_probe)
        return source, target

    @staticmethod
    def _args(source, target, tmp_path, **overrides):
        argv = [
            "run",
            str(source),
            str(target),
            "-o",
            str(tmp_path / "out.mkv"),
            "-w",
            str(tmp_path / "work"),
        ]
        argv += [flag for flag, on in overrides.items() if on]
        return cli.build_parser().parse_args(argv)

    def test_dry_run_stops_before_touching_anything(self, workable, tmp_path, monkeypatch, capsys):
        from hibrit import pipeline

        def explode(*_a, **_k):
            raise AssertionError("--dry-run reached the pipeline")

        monkeypatch.setattr(pipeline, "run", explode)
        source, target = workable
        assert cli.cmd_run(self._args(source, target, tmp_path, **{"--dry-run": True})) == 0
        assert "stopping before the first command" in capsys.readouterr().out

    def test_declining_the_prompt_writes_nothing(self, workable, tmp_path, monkeypatch, capsys):
        from hibrit import pipeline

        def explode(*_a, **_k):
            raise AssertionError("a declined run reached the pipeline")

        monkeypatch.setattr(pipeline, "run", explode)
        monkeypatch.setattr("builtins.input", lambda _prompt: "n")
        source, target = workable
        assert cli.cmd_run(self._args(source, target, tmp_path)) == 1
        assert "Nothing was written" in capsys.readouterr().out

    def test_yes_does_not_hand_the_pipeline_a_blank_cheque(self, workable, tmp_path, monkeypatch):
        """--yes means "do not ask me", not "ignore your own checks".

        If it passed an approve callback that always agreed, an unattended run
        would accept an untrustworthy offset and write exactly the file this
        program exists to prevent.
        """
        from hibrit import pipeline

        seen = {}

        def fake_run(_plan, output, **kwargs):
            seen.update(kwargs)
            Path(output).write_bytes(b"out")
            return pipeline.Result(output=Path(output), plan=_plan)

        monkeypatch.setattr(pipeline, "run", fake_run)
        source, target = workable
        args = self._args(source, target, tmp_path, **{"--yes": True, "--no-verify": True})
        assert cli.cmd_run(args) == 0
        assert seen["approve"] is None, "--yes must not supply an always-yes approver"

    def test_a_failed_verification_is_a_non_zero_exit(self, workable, tmp_path, monkeypatch):
        """A script chaining commands has only the exit code to go on."""
        from hibrit import pipeline
        from hibrit.verify import Check, Report

        out = tmp_path / "out.mkv"

        def fake_run(_plan, output, **_kwargs):
            Path(output).write_bytes(b"out")
            return pipeline.Result(output=Path(output), plan=_plan)

        monkeypatch.setattr(pipeline, "run", fake_run)
        monkeypatch.setattr(
            "hibrit.verify.verify",
            lambda *_a, **_k: Report(checks=(Check("picture untouched", False, "changed"),)),
        )
        source, target = workable
        args = self._args(source, target, tmp_path, **{"--yes": True})
        assert cli.cmd_run(args) == 1
        assert out.exists(), "the output is left in place for inspection"

    def test_a_blocked_plan_exits_one_without_asking(self, monkeypatch, tmp_path):
        source = tmp_path / "source.mkv"
        target = tmp_path / "target.mkv"
        for path in (source, target):
            path.write_bytes(b"x")
        monkeypatch.setattr(cli, "probe", lambda path, box: make_info(str(path)))

        def explode(_prompt):
            raise AssertionError("asked about a plan that cannot run")

        monkeypatch.setattr("builtins.input", explode)
        assert cli.cmd_run(self._args(source, target, tmp_path)) == 1


class TestAsk:
    def test_it_says_plainly_that_the_offset_is_not_trustworthy(self, monkeypatch, capsys):
        from hibrit.align import Alignment, Verdict

        monkeypatch.setattr("builtins.input", lambda _prompt: "n")
        refused = Alignment(
            offset=-115, verdict=Verdict.NO_MATCH, confidence=1.04, windows=(), reason="noise"
        )
        assert cli._ask(refused) is False
        out = capsys.readouterr().out
        assert "not trustworthy" in out
        assert "misalign" in out

    @pytest.mark.parametrize(
        ("answer", "accepted"),
        [("y", True), ("yes", True), ("Y", True), ("n", False), ("", False), ("maybe", False)],
    )
    def test_only_a_clear_yes_counts(self, monkeypatch, answer, accepted):
        from hibrit.align import Alignment, Verdict

        monkeypatch.setattr("builtins.input", lambda _prompt: answer)
        result = Alignment(
            offset=10, verdict=Verdict.RELIABLE, confidence=5.0, windows=(), reason="ok"
        )
        assert cli._ask(result) is accepted
