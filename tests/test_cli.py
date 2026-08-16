"""Argument parsing and exit codes.

Exit codes are the part a script sees. ``hibrit plan`` returning 0 for a job
that cannot run would make a shell loop happily feed impossible pairs into
``hibrit run``.
"""

from __future__ import annotations

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

    def test_version_is_the_packaged_version(self, capsys) -> None:
        from hibrit import __version__

        with pytest.raises(SystemExit):
            cli.main(["--version"])
        assert __version__ in capsys.readouterr().out
