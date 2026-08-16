"""Command line interface.

Every subcommand that could write a large file also exists in a form that only
looks: ``probe`` and ``plan`` answer "what would happen" without touching a
byte, and ``align`` answers the one question that decides whether the rest is
worth starting.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hibrit import __version__
from hibrit.align import Alignment, align
from hibrit.planner import build_plan
from hibrit.probe import probe
from hibrit.tools import MissingTool, Toolbox, ToolFailed

DEFAULT_WORKDIR_HINT = (
    "Pick a drive with room for roughly three times the target file. "
    "The drive holding your sources usually does not have it."
)


def _toolbox(args: argparse.Namespace) -> Toolbox:
    extra = [Path(d) for d in (args.tools_dir or [])]
    return Toolbox(extra_dirs=tuple(extra))


class InputMissing(RuntimeError):
    """A path given on the command line does not exist."""


def _existing(value: str | None, label: str) -> Path | None:
    """Resolve a path argument, or explain which one is wrong.

    Checked here rather than left to whatever opens the file first: a stack
    trace ending in FileNotFoundError does not say which of five path arguments
    was the bad one, and this program takes a lot of path arguments.
    """
    if value is None:
        return None
    path = Path(value)
    if not path.exists():
        raise InputMissing(f"{label} does not exist: {path}")
    return path


def cmd_doctor(args: argparse.Namespace) -> int:
    box = _toolbox(args)
    print(f"hibrit {__version__}\n")
    worst = 0
    for status in box.doctor():
        if status.ok:
            mark = "ok  "
        elif status.required:
            mark = "MISSING"
            worst = 1
        else:
            mark = "-   "
        version = f"  {status.version}" if status.version else ""
        location = f"  {status.path}" if status.path else "  not found"
        print(f"[{mark}] {status.name}{version}{location}")
    if worst:
        print(
            "\nPut the missing binaries in hibrit/tools/ or anywhere on PATH.\n"
            "dovi_tool:      https://github.com/quietvoid/dovi_tool/releases\n"
            "hdr10plus_tool: https://github.com/quietvoid/hdr10plus_tool/releases"
        )
    return worst


def cmd_probe(args: argparse.Namespace) -> int:
    box = _toolbox(args)
    for path in args.files:
        info = probe(Path(path), box)
        print(f"{info.name}\n  {info.describe()}")
        if args.verbose:
            print(f"  frames: {info.frame_count}   rate: {info.frame_rate}")
            print(f"  hdr format: {info.hdr_format_raw or '-'}")
            if info.dv_compatibility:
                print(f"  compatibility: {', '.join(info.dv_compatibility)}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    box = _toolbox(args)
    plan = build_plan(
        probe(Path(args.source), box),
        probe(Path(args.target), box),
        replace_existing=args.replace,
    )
    print(plan.describe())
    return 0 if plan.ok else 1


def _print_alignment(result: Alignment) -> None:
    # The offset and its confidence are printed at the same weight on purpose.
    # A single large number reads as an answer; the two together read as a
    # measurement, which is what it is.
    print(result.describe())
    print(f"  {result.reason}")
    for window in result.windows:
        print(
            f"  window at frame {window.start_frame}: offset {window.offset:+d}, "
            f"confidence {window.confidence:.2f}"
        )


def cmd_align(args: argparse.Namespace) -> int:
    box = _toolbox(args)
    result = align(
        probe(Path(args.source), box),
        probe(Path(args.target), box),
        box,
        windows=args.windows,
        max_shift=args.max_shift,
    )
    _print_alignment(result)
    return 0 if result.usable else 1


def _ask(result: Alignment) -> bool:
    _print_alignment(result)
    if not result.usable:
        print("\nThis offset is not trustworthy. Injecting it would misalign the")
        print("metadata for the whole runtime.")
    answer = input("\nUse this offset? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def cmd_run(args: argparse.Namespace) -> int:
    from hibrit import pipeline
    from hibrit.verify import verify

    box = _toolbox(args)
    plan = build_plan(
        probe(Path(args.source), box),
        probe(Path(args.target), box),
        replace_existing=args.replace,
    )
    print(plan.describe())
    print()

    if not plan.ok:
        return 1
    if args.dry_run:
        print("--dry-run: stopping before the first command.")
        return 0

    if not args.yes and input("Proceed? [y/N] ").strip().lower() not in {"y", "yes"}:
        print("Nothing was written.")
        return 1

    result = pipeline.run(
        plan,
        Path(args.output),
        workdir=Path(args.workdir),
        toolbox=box,
        progress=lambda message: print(f"  {message}"),
        approve=None if args.yes else _ask,
        keep_intermediates=args.keep,
    )

    if args.no_verify:
        return 0

    print("\nverifying")
    report = verify(
        result.output,
        target=plan.target.path,
        rpu=result.rpu,
        hdr10plus=result.hdr10plus,
        clean_target_stream=result.clean_target_stream if args.verify_pixels else None,
        workdir=Path(args.workdir),
        toolbox=box,
    )
    print(report.describe())

    if not args.keep and result.clean_target_stream is not None:
        result.clean_target_stream.unlink(missing_ok=True)
    return 0 if report.passed else 1


def cmd_verify(args: argparse.Namespace) -> int:
    from hibrit.verify import verify

    box = _toolbox(args)
    report = verify(
        _existing(args.result, "result"),
        target=_existing(args.target, "--target"),
        rpu=_existing(args.rpu, "--rpu"),
        hdr10plus=_existing(args.hdr10plus, "--hdr10plus"),
        clean_target_stream=_existing(args.clean_stream, "--clean-stream"),
        workdir=Path(args.workdir),
        toolbox=box,
    )
    print(report.describe())
    return 0 if report.passed else 1


def cmd_gui(args: argparse.Namespace) -> int:
    from hibrit.gui import main as gui_main

    return gui_main()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hibrit",
        description=(
            "Move Dolby Vision and HDR10+ metadata from one release to another "
            "without re-encoding a single frame."
        ),
    )
    parser.add_argument("--version", action="version", version=f"hibrit {__version__}")
    parser.add_argument(
        "--tools-dir",
        action="append",
        metavar="DIR",
        help="extra directory to search for dovi_tool and friends (repeatable)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check that the external tools are available")
    doctor.set_defaults(func=cmd_doctor)

    probe_cmd = sub.add_parser("probe", help="show what HDR metadata a file carries")
    probe_cmd.add_argument("files", nargs="+")
    probe_cmd.add_argument("-v", "--verbose", action="store_true")
    probe_cmd.set_defaults(func=cmd_probe)

    plan_cmd = sub.add_parser("plan", help="show what would be done, without doing it")
    plan_cmd.add_argument("source", help="the file that has the metadata")
    plan_cmd.add_argument("target", help="the file whose picture you want to keep")
    plan_cmd.add_argument(
        "--replace", action="store_true", help="overwrite metadata the target has"
    )
    plan_cmd.set_defaults(func=cmd_plan)

    align_cmd = sub.add_parser("align", help="measure the frame offset between two releases")
    align_cmd.add_argument("source")
    align_cmd.add_argument("target")
    align_cmd.add_argument("--windows", type=int, default=2, help="how many places to measure")
    align_cmd.add_argument("--max-shift", type=int, default=None, help="search range in frames")
    align_cmd.set_defaults(func=cmd_align)

    run_cmd = sub.add_parser("run", help="transfer the metadata and remux")
    run_cmd.add_argument("source")
    run_cmd.add_argument("target")
    run_cmd.add_argument("-o", "--output", required=True)
    run_cmd.add_argument("-w", "--workdir", required=True, help=DEFAULT_WORKDIR_HINT)
    run_cmd.add_argument("--replace", action="store_true")
    run_cmd.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    run_cmd.add_argument("-n", "--dry-run", action="store_true", help="print the plan and stop")
    run_cmd.add_argument("--keep", action="store_true", help="keep intermediate files")
    run_cmd.add_argument("--no-verify", action="store_true")
    run_cmd.add_argument(
        "--no-picture-check",
        dest="verify_pixels",
        action="store_false",
        help="skip proving the picture is unchanged (it costs one extra read)",
    )
    run_cmd.set_defaults(func=cmd_run)

    verify_cmd = sub.add_parser("verify", help="check a finished file against its inputs")
    verify_cmd.add_argument("result")
    verify_cmd.add_argument("--target", help="the original target file")
    verify_cmd.add_argument("--rpu", help="the RPU that was injected")
    verify_cmd.add_argument("--hdr10plus", help="the HDR10+ JSON that was injected")
    verify_cmd.add_argument("--clean-stream", help="the target's pre-injection .hevc stream")
    verify_cmd.add_argument("-w", "--workdir", required=True)
    verify_cmd.set_defaults(func=cmd_verify)

    gui_cmd = sub.add_parser("gui", help="open the window")
    gui_cmd.set_defaults(func=cmd_gui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except MissingTool as error:
        print(f"error: {error}\n\nRun 'hibrit doctor' to see what is missing.", file=sys.stderr)
        return 2
    except (ToolFailed, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        # Anything the up-front checks did not catch: a path that vanished
        # mid-run, a full disk, a permission problem. Still a sentence, not a
        # traceback.
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted; nothing further was written.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
