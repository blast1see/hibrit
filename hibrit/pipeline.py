"""Run a :class:`~hibrit.planner.Plan` against real files.

Two things shape this module more than the commands do.

**Disk.** A 70 GB remux needs its video stream extracted, injected into, and
remuxed. That is three copies of a very large file before anything is deleted.
The machine this was written on has 42 GB free on the drive holding the sources,
so a run that starts optimistically ends with a half-written file and a full
disk. Space is therefore checked before the first command, and each intermediate
is deleted the moment the next step has consumed it.

**Consent.** When the frame counts differ, the metadata has to be retimed, and
the retiming is a guess until a human has looked at it. The alignment result is
handed to a callback that can say no. If no callback is supplied, an alignment
that is not ``reliable`` stops the run.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from hibrit.align import Alignment, align, edit_config_for_offset
from hibrit.hdr10plus import Hdr10PlusTool, read_json
from hibrit.matroska import extract_video, remux
from hibrit.planner import Kind, Plan
from hibrit.rpu import DoviTool
from hibrit.tools import Toolbox

#: Working space needed, as a multiple of the target file's size: the extracted
#: stream, the injected stream, and the remuxed result can coexist briefly.
SPACE_FACTOR = 3.0

#: Callbacks. ``Progress`` is told what is happening; ``Approve`` is asked
#: whether a measured alignment may be used.
Progress = Callable[[str], None]
Approve = Callable[[Alignment], bool]


class PipelineError(RuntimeError):
    """A run stopped before writing its output."""


class NotEnoughSpace(PipelineError):
    """Raised before starting, not halfway through."""

    def __init__(self, workdir: Path, needed: int, free: int) -> None:
        super().__init__(
            f"{workdir} has {free / 2**30:.1f} GB free, this job needs about "
            f"{needed / 2**30:.1f} GB of working space. Choose a different working "
            "directory with --workdir."
        )
        self.workdir = workdir
        self.needed = needed
        self.free = free


@dataclass
class Result:
    """What the run produced, and what it did along the way."""

    output: Path
    plan: Plan
    alignment: Alignment | None = None
    rpu: Path | None = None
    hdr10plus: Path | None = None
    clean_target_stream: Path | None = None
    log: list[str] = field(default_factory=list)


def free_space(path: Path) -> int:
    """Bytes free on the volume holding *path*, walking up to a parent that exists."""
    probe_path = Path(path)
    while not probe_path.exists() and probe_path.parent != probe_path:
        probe_path = probe_path.parent
    return shutil.disk_usage(probe_path).free


def check_space(workdir: Path, target: Path, *, factor: float = SPACE_FACTOR) -> None:
    """Refuse to start a job that cannot finish."""
    needed = int(Path(target).stat().st_size * factor)
    available = free_space(workdir)
    if available < needed:
        raise NotEnoughSpace(Path(workdir), needed, available)


def _retime(
    offset: int,
    source_frames: int,
    target_frames: int,
    *,
    rpu: Path | None,
    hdr10plus: Path | None,
    workdir: Path,
    box: Toolbox,
    say: Progress,
) -> tuple[Path | None, Path | None]:
    """Cut or pad both metadata streams so they line up with the target."""
    config = edit_config_for_offset(offset, source_frames, target_frames)
    if not config:
        say("metadata already matches the target length; no retiming needed")
        return rpu, hdr10plus

    say(f"retiming metadata: {config}")
    new_rpu = new_hdr10plus = None
    if rpu is not None:
        new_rpu = DoviTool(box).editor(rpu, config, workdir / "rpu_aligned.bin", workdir=workdir)
        rpu.unlink(missing_ok=True)
    if hdr10plus is not None:
        new_hdr10plus = Hdr10PlusTool(box).editor(
            hdr10plus, config, workdir / "hdr10plus_aligned.json", workdir=workdir
        )
        hdr10plus.unlink(missing_ok=True)
    return new_rpu, new_hdr10plus


def run(
    plan: Plan,
    output: Path,
    *,
    workdir: Path,
    toolbox: Toolbox | None = None,
    progress: Progress | None = None,
    approve: Approve | None = None,
    alignment: Alignment | None = None,
    keep_intermediates: bool = False,
    keep_clean_stream: bool = True,
    skip_space_check: bool = False,
) -> Result:
    """Execute *plan*, writing the finished Matroska file to *output*.

    *alignment* accepts a measurement made earlier — the window measures it so
    the user can look at it before agreeing, and re-deriving it here would
    decode the same minutes of video a second time. It is still put through
    *approve*, so a forced offset is forced once, in one place.

    *keep_clean_stream* leaves the target's extracted video stream on disk so
    :func:`hibrit.verify.verify` can prove the picture is unchanged. It is the
    largest intermediate there is, so the caller deletes it once satisfied; set
    it false to reclaim the space immediately and give up that check.
    """
    if not plan.ok:
        detail = "\n".join(n.describe() for n in plan.blockers) or "nothing to transfer"
        raise PipelineError(f"this plan cannot run:\n{detail}")

    box = toolbox or Toolbox()
    missing = box.missing_required()
    if missing:
        raise PipelineError(f"missing required tools: {', '.join(missing)}. Run 'hibrit doctor'.")

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    output = Path(output)

    log: list[str] = []

    def say(message: str) -> None:
        log.append(message)
        if progress is not None:
            progress(message)

    if not skip_space_check:
        check_space(workdir, plan.target.path)
        say(f"working directory {workdir}: {free_space(workdir) / 2**30:.1f} GB free")

    source, target = plan.source, plan.target
    dovi = DoviTool(box)
    hdr10plus_tool = Hdr10PlusTool(box)

    # --- read the target's picture out of its container -------------------------
    say(f"extracting video stream from {target.name}")
    target_stream = extract_video(target, workdir / "target.hevc", box)

    # --- read the source's metadata ---------------------------------------------
    rpu: Path | None = None
    hdr10plus: Path | None = None

    if Kind.DV in plan.transfer:
        mode = plan.convert_mode
        say(
            "extracting Dolby Vision RPU"
            + (f" and converting to profile 8.1 (-m {mode})" if mode else "")
        )
        rpu = dovi.extract_rpu(source.path, workdir / "rpu.bin", mode=mode)
        say(f"RPU: {dovi.info(rpu).describe()}")

    if Kind.HDR10PLUS in plan.transfer:
        say("extracting HDR10+ metadata")
        hdr10plus = hdr10plus_tool.extract(source.path, workdir / "hdr10plus.json")
        say(f"HDR10+: {read_json(hdr10plus).describe()}")

    # --- line the metadata up with the target ------------------------------------
    if plan.needs_alignment:
        if alignment is None:
            say("measuring frame offset between source and target")
            alignment = align(source, target, box, progress=say)
        else:
            say("using the offset measured earlier")
        say(alignment.describe())
        say(alignment.reason)

        accepted = approve(alignment) if approve is not None else alignment.usable
        if not accepted or alignment.offset is None:
            raise PipelineError(
                "alignment was not accepted, so nothing was written.\n"
                f"{alignment.describe()}\n{alignment.reason}"
            )

        source_frames = source.frame_count or 0
        target_frames = target.frame_count or 0
        rpu, hdr10plus = _retime(
            alignment.offset,
            source_frames,
            target_frames,
            rpu=rpu,
            hdr10plus=hdr10plus,
            workdir=workdir,
            box=box,
            say=say,
        )

    # --- write the metadata into the target's stream ------------------------------
    # Order was measured to be irrelevant: injecting HDR10+ first and DV first
    # produce byte-identical files. HDR10+ goes first only because its check is
    # the cheaper of the two to fail on.
    current = target_stream
    if hdr10plus is not None:
        say("injecting HDR10+ metadata")
        injected = hdr10plus_tool.inject(
            current,
            hdr10plus,
            workdir / "with_hdr10plus.hevc",
            video_frames=target.frame_count,
        )
        if current is not target_stream and not keep_intermediates:
            current.unlink(missing_ok=True)
        current = injected

    if rpu is not None:
        say("injecting Dolby Vision RPU")
        injected = dovi.inject_rpu(current, rpu, workdir / "with_dv.hevc")
        if current is not target_stream and not keep_intermediates:
            current.unlink(missing_ok=True)
        current = injected

    # --- put the container back together -------------------------------------------
    say(f"remuxing to {output}")
    remux(current, target, output, box)
    if not keep_intermediates:
        current.unlink(missing_ok=True)

    if not keep_clean_stream and not keep_intermediates:
        target_stream.unlink(missing_ok=True)
        say("removed the extracted target stream; the picture check cannot be run")
        clean_stream = None
    else:
        clean_stream = target_stream
        say(f"kept {target_stream.name} for the picture check — delete it when done")

    say(f"done: {output} ({output.stat().st_size / 2**30:.2f} GB)")

    return Result(
        output=output,
        plan=plan,
        alignment=alignment,
        rpu=rpu,
        hdr10plus=hdr10plus,
        clean_target_stream=clean_stream,
        log=log,
    )
