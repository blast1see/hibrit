"""Run a :class:`~hibrit.planner.Plan` against real files.

Two things shape this module more than the commands do.

**Disk.** A 70 GB remux needs its video stream extracted, injected into, and
remuxed. That is three copies of a very large file before anything is deleted,
and the drive holding the sources is routinely the one without room for them —
a run that starts optimistically ends with a half-written file and a full disk.

Space is therefore checked before the first command, and every intermediate is
deleted as soon as the next step has consumed it, with one deliberate exception:
the extracted target stream stays, because the picture check needs something to
compare the output against. It is the largest thing here, so the caller is told
it is there and decides when it goes.

**Consent.** When the frame counts differ, the metadata has to be retimed, and
the retiming is a guess until a human has looked at it. The alignment result is
handed to a callback that can say no. If no callback is supplied, an alignment
that is not ``reliable`` stops the run.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from hibrit.align import Alignment, align, edit_config_for_offset
from hibrit.hdr10plus import Hdr10PlusTool, read_json
from hibrit.matroska import extract_video, remux, stale_label_warning
from hibrit.planner import Kind, Plan
from hibrit.probe import probe
from hibrit.rpu import DoviTool
from hibrit.tools import Toolbox

#: Working space needed, as a multiple of the target file's size.
#:
#: Traced on a job that moves both kinds of metadata, where the peak is highest.
#: The extracted target stream stays for the picture check; the HDR10+ pass
#: writes a second copy beside it; the Dolby Vision pass writes a third before
#: the second is deleted. That is three streams at once, and then the remuxed
#: result alongside what remains. For a 72 GB target the peak measures about
#: 208 GB, so three times over is the requirement with a little room, not a
#: guess.
#:
#: Verification reaches the same peak by a different route, which is worth
#: knowing before anyone trims this: the picture check must extract the finished
#: file's video track before it can hash it, so the kept target stream, the
#: output and that extraction coexist. Freeing the target stream earlier would
#: lower the run's peak and leave verification unable to fit.
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


#: A percentage anywhere in a line of tool output. Matched on the digits and the
#: sign rather than on a word: mkvmerge translates its messages, and on the
#: machine this was written for it reports "İlerleme: 42%". A filter keyed to
#: "Progress" would have passed every test written in English and dropped
#: nothing here.
_PERCENT = re.compile(r"(\d{1,3})\s*%")


def throttle_progress(say: Progress, *, step: int = 10) -> Progress:
    """Forward tool output, but thin out the percentage counters.

    mkvextract prints a line per percent. Four such steps over a 70 GB remux is
    four hundred lines of counting, which buries the handful of messages that
    say what is actually happening. Only crossings of *step* percent get
    through; everything else passes untouched.
    """
    last: int | None = None

    def forward(line: str) -> None:
        nonlocal last
        match = _PERCENT.search(line)
        if match is None:
            say(line)
            return
        percent = int(match.group(1))
        # The first reading always goes through, and a counter that has gone
        # backwards means the next tool in the chain started at zero.
        if last is None or percent < last or percent >= last + step:
            last = percent
            say(line)

    return forward


def describe_leftovers(workdir: Path) -> str:
    """What a stopped job left in the working directory, and how much of it.

    At this scale the number is the point. A run that dies after extracting a
    70 GB stream and injecting into it has 140 GB sitting there, and a user who
    is not told will meet it as a full disk some other day.
    """
    files = [p for p in Path(workdir).glob("*") if p.is_file()]
    if not files:
        return ""
    total = sum(p.stat().st_size for p in files)
    names = ", ".join(sorted(p.name for p in files))
    return (
        f"{len(files)} file(s) left in {workdir} using {total / 2**30:.1f} GB "
        f"({names}). Nothing was deleted; remove them when you are done looking."
    )


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
    skip_reorder: bool = False,
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

    tool_output = throttle_progress(say)

    if not skip_space_check:
        check_space(workdir, plan.target.path)
        say(f"working directory {workdir}: {free_space(workdir) / 2**30:.1f} GB free")

    try:
        return _execute(
            plan,
            output,
            workdir=workdir,
            box=box,
            say=say,
            tool_output=tool_output,
            approve=approve,
            alignment=alignment,
            keep_intermediates=keep_intermediates,
            keep_clean_stream=keep_clean_stream,
            skip_reorder=skip_reorder,
            log=log,
        )
    except BaseException:
        # KeyboardInterrupt included on purpose: a job stopped by hand at 60 GB
        # in leaves exactly as much behind as one that failed. Nothing is
        # deleted — a half-written stream is evidence, and this program's whole
        # posture is to explain rather than to tidy up quietly — but the user is
        # told what is there, because otherwise they find out when the disk
        # fills up a week later.
        remains = describe_leftovers(workdir)
        if remains:
            say(f"stopped. {remains}")
        raise


def _execute(
    plan: Plan,
    output: Path,
    *,
    workdir: Path,
    box: Toolbox,
    say: Progress,
    tool_output: Progress,
    approve: Approve | None,
    alignment: Alignment | None,
    keep_intermediates: bool,
    keep_clean_stream: bool,
    skip_reorder: bool,
    log: list[str],
) -> Result:
    """The steps themselves. Split out so :func:`run` can report what a failure
    left behind without the whole body sitting inside a ``try``."""
    source, target = plan.source, plan.target
    dovi = DoviTool(box)
    hdr10plus_tool = Hdr10PlusTool(box)

    # --- settle the offset before writing anything -------------------------------
    # Alignment reads the two original files and never looks at anything this
    # function produces, so it can go first — and it must. It is the step most
    # likely to end the job, and measuring it after extracting the target's
    # stream means a refusal costs ten minutes and 68 GB of writing before
    # anyone hears about it.
    if plan.needs_alignment:
        if alignment is None:
            say("measuring frame offset between source and target")
            # align speaks for itself rather than through a tool, so its
            # messages are not thinned.
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

    # --- read the target's picture out of its container -------------------------
    say(f"extracting video stream from {target.name}")
    # These steps each rewrite the whole stream. On a 70 GB remux that is
    # minutes apiece, and the tools do report their progress — swallowing it is
    # what makes a working job look like a hung one.
    target_stream = extract_video(target, workdir / "target.hevc", box, progress=tool_output)

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
        rpu_info = dovi.info(rpu)
        say(f"RPU: {rpu_info.describe()}")
        if mode == 2:
            # The planner could not know which kind of profile 7 this was —
            # the container reports MEL and FEL identically. The RPU does not.
            source_info = dovi.info(
                dovi.extract_rpu(source.path, workdir / "rpu_asis.bin", mode=None)
            )
            if source_info.is_fel:
                say(
                    "note: the source is profile 7 FEL, so converting to 8.1 has "
                    "discarded the enhancement layer's luma and chroma mapping. "
                    "The result is valid Dolby Vision; it is not the full grade."
                )
            elif source_info.is_mel:
                say("the source is profile 7 MEL, so nothing was lost converting it")
            (workdir / "rpu_asis.bin").unlink(missing_ok=True)

    if Kind.HDR10PLUS in plan.transfer:
        say("extracting HDR10+ metadata")
        hdr10plus = hdr10plus_tool.extract(
            source.path, workdir / "hdr10plus.json", skip_reorder=skip_reorder
        )
        say(f"HDR10+: {read_json(hdr10plus).describe()}")

    # --- retime the metadata to the offset settled above --------------------------
    if plan.needs_alignment:
        assert alignment is not None and alignment.offset is not None  # settled above
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
            progress=tool_output,
        )
        if current is not target_stream and not keep_intermediates:
            current.unlink(missing_ok=True)
        current = injected

    if rpu is not None:
        say("injecting Dolby Vision RPU")
        injected = dovi.inject_rpu(current, rpu, workdir / "with_dv.hevc", progress=tool_output)
        if current is not target_stream and not keep_intermediates:
            current.unlink(missing_ok=True)
        current = injected

    # --- put the container back together -------------------------------------------
    say(f"remuxing to {output}")
    remux(current, target, output, box, progress=tool_output)

    # The target's own labelling came across with it, and it may describe
    # metadata this job just changed.
    produced = probe(output, box)
    stale = stale_label_warning(produced.track.get("Title"), produced)
    if stale:
        say(f"note: {stale}")
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
