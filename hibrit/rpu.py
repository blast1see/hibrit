"""Thin, opinionated wrapper around ``dovi_tool``.

Opinionated in one specific way: :func:`inject_rpu` refuses a frame-count
mismatch that ``dovi_tool`` itself accepts.

``dovi_tool inject-rpu`` prints::

    Warning: mismatched lengths. video 1000, RPU 800
    Metadata will be duplicated at the end to match video length

...and then exits 0 with a finished file. That file plays, shows a Dolby
Vision badge, and carries metadata shifted by 200 frames for its entire
runtime. Since the failure is invisible downstream, the check has to happen
here.

Small mismatches are a real convenience (an encoder leaving a couple of spare
frames), so the guard is a tolerance, not an absolute equality test.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hibrit.tools import Toolbox, UnreadableMismatch

#: Frame-count differences at or below this are padded silently by dovi_tool
#: and are harmless; anything larger is a misalignment and must be refused.
DEFAULT_FRAME_TOLERANCE = 3

#: Split in two for the same reason hdr10plus.py is: finding the warning and
#: reading its numbers are different questions, and only the split lets "a
#: warning I could not parse" exist as an answer. A single strict pattern
#: answers None to both a clean run and a reworded warning, and one of those is
#: a misaligned file.
_MISMATCH_LINE = re.compile(r"^.*mismatched lengths\..*$", re.IGNORECASE | re.MULTILINE)
_MISMATCH_RE = re.compile(
    r"mismatched lengths\.\s*video\s+(?P<video>\d+),\s*RPU\s+(?P<rpu>\d+)",
    re.IGNORECASE,
)
_FRAMES_RE = re.compile(r"^\s*Frames:\s*(\d+)", re.MULTILINE)
_PROFILE_RE = re.compile(r"^\s*Profile:\s*(\d+)(?:\s*\((?P<layer>\w+)\))?", re.MULTILINE)
_DM_RE = re.compile(r"^\s*DM version:\s*(?P<dm>.+)$", re.MULTILINE)
_SCENES_RE = re.compile(r"^\s*Scene/shot count:\s*(\d+)", re.MULTILINE)
#: One field of the L5 line. dovi_tool prints three forms and they are three
#: different facts: `281` for an edge that holds still, `0..281` for one that
#: moves through the film, and `N/A` for an RPU carrying no level 5 at all.
#: Matching only the first collapsed the other two into "no offsets".
_L5_FIELD = r"(N/A|\d+(?:\.\.\d+)?)"
_L5_RE = re.compile(
    rf"^\s*L5 offsets:\s*top={_L5_FIELD},\s*bottom={_L5_FIELD},"
    rf"\s*left={_L5_FIELD},\s*right={_L5_FIELD}",
    re.MULTILINE,
)
#: The same line, read loosely, so that "dovi_tool printed offsets I cannot
#: read" can be told apart from "dovi_tool printed no offsets". Without this
#: pair the two collapse, which is the bug the strict pattern above was widened
#: to fix — and it would come back the next time the format moves.
_L5_LINE_RE = re.compile(r"^\s*L5 offsets:.*$", re.MULTILINE)


class FrameCountMismatch(RuntimeError):
    """Raised instead of writing a silently misaligned file."""

    def __init__(self, video_frames: int, rpu_frames: int) -> None:
        delta = rpu_frames - video_frames
        direction = "longer than" if delta > 0 else "shorter than"
        super().__init__(
            f"RPU has {rpu_frames} frames, video has {video_frames} "
            f"({abs(delta)} frames {direction} the video).\n"
            "dovi_tool would pad or truncate and still produce a file, but the "
            "metadata would be misaligned for the whole runtime. Align the RPU "
            "first (hibrit align), then inject."
        )
        self.video_frames = video_frames
        self.rpu_frames = rpu_frames
        self.delta = delta


@dataclass(frozen=True)
class L5Offsets:
    """Active-area offsets, and whether the film keeps one shape.

    Each edge is the ``(lowest, highest)`` dovi_tool reported for it, so an
    edge that never moves is ``(n, n)`` and one that does is a real range.
    Keeping both ends is the point: a film that opens 1.85:1 and settles into
    2.40:1 has no single set of offsets, and answering with either end of the
    range would be inventing one.

    Measured: `dovi_tool editor` given two active_area presets over a 3000-frame
    RPU produces `top=0..281, bottom=0..281, left=0, right=0` — plain numbers
    and ranges on the same line, so the two forms have to be read per field
    rather than per line.
    """

    top: tuple[int, int]
    bottom: tuple[int, int]
    left: tuple[int, int]
    right: tuple[int, int]

    @property
    def variable(self) -> bool:
        """True when any edge moves through the film."""
        return any(lo != hi for lo, hi in (self.top, self.bottom, self.left, self.right))

    @property
    def fixed(self) -> tuple[int, int, int, int] | None:
        """The one set of offsets, or None when there is no such thing.

        A caller placing bars needs a single answer, and a film that changes
        shape does not have one. Returning None rather than an end of the range
        is the same refusal the rest of this package makes elsewhere.
        """
        if self.variable:
            return None
        return (self.top[0], self.bottom[0], self.left[0], self.right[0])

    def __str__(self) -> str:
        def edge(value: tuple[int, int]) -> str:
            lo, hi = value
            return str(lo) if lo == hi else f"{lo}..{hi}"

        line = (
            f"top={edge(self.top)}, bottom={edge(self.bottom)}, "
            f"left={edge(self.left)}, right={edge(self.right)}"
        )
        # A reader who does not know `0..281` is a range reads it as one shape,
        # so the fact travels with the numbers rather than somewhere above them.
        return f"{line} (varies)" if self.variable else line


@dataclass(frozen=True)
class RpuInfo:
    """Parsed ``dovi_tool info -s`` summary."""

    frames: int
    profile: int
    layer_kind: str | None
    dm_version: str | None
    scene_count: int | None
    l5_offsets: L5Offsets | None
    raw: str

    @property
    def is_fel(self) -> bool:
        """Profile 7 Full Enhancement Layer: converting to 8.1 loses mapping."""
        return self.profile == 7 and (self.layer_kind or "").upper() == "FEL"

    @property
    def is_mel(self) -> bool:
        """Profile 7 Minimal Enhancement Layer: conversion to 8.1 keeps everything."""
        return self.profile == 7 and (self.layer_kind or "").upper() == "MEL"

    def describe(self) -> str:
        profile = f"P{self.profile}"
        if self.layer_kind:
            profile += f" ({self.layer_kind})"
        bits = [f"{self.frames} frames", profile]
        if self.dm_version:
            bits.append(f"DM {self.dm_version}")
        if self.scene_count is not None:
            bits.append(f"{self.scene_count} scenes")
        return " · ".join(bits)


def _read_l5(text: str) -> L5Offsets | None:
    """Read the L5 line, or refuse when there is one and it makes no sense.

    Three outcomes, and keeping them apart is the whole point:

    * no such line, or every field ``N/A`` — the RPU carries no level 5, and
      None says so. `dovi_tool export --levels level5=` on such a file writes an
      empty table, which is how N/A was confirmed to mean absent rather than
      unreported. It is what a release already cropped to its own picture looks
      like, since there are no bars left to mask.
    * four readable fields — offsets, fixed or variable.
    * a line that does not parse — raise. The tool said something about the
      active area and this code did not understand it; answering "no offsets"
      would be the same confident silence that hid the range form, and the next
      format change would land in exactly the same place.

    A partly-``N/A`` line has never been observed. If one appears, no edge can
    be trusted to describe the same frame as the others, so it is declined as a
    whole rather than half-read.
    """
    match = _L5_RE.search(text)
    if match is None:
        line = _L5_LINE_RE.search(text)
        if line is None:
            return None
        raise ValueError(
            f"could not read the L5 offsets line:\n  {line.group(0).strip()}\n"
            "dovi_tool may have changed its format."
        )

    fields = match.groups()
    if any(field == "N/A" for field in fields):
        return None
    edges = tuple((int(ends[0]), int(ends[-1])) for ends in (f.split("..") for f in fields))
    return L5Offsets(top=edges[0], bottom=edges[1], left=edges[2], right=edges[3])


def parse_info(text: str) -> RpuInfo:
    """Parse the summary ``dovi_tool info -s`` prints."""
    frames = _FRAMES_RE.search(text)
    profile = _PROFILE_RE.search(text)
    if frames is None or profile is None:
        raise ValueError(f"could not parse dovi_tool info output:\n{text}")
    scenes = _SCENES_RE.search(text)
    dm = _DM_RE.search(text)
    return RpuInfo(
        frames=int(frames.group(1)),
        profile=int(profile.group(1)),
        layer_kind=profile.group("layer"),
        dm_version=dm.group("dm").strip() if dm else None,
        scene_count=int(scenes.group(1)) if scenes else None,
        l5_offsets=_read_l5(text),
        raw=text,
    )


def find_mismatch(stderr: str) -> tuple[int, int] | None:
    """Return ``(video_frames, rpu_frames)`` if dovi_tool warned, else None.

    Raises :class:`UnreadableMismatch` when a warning is there but its counts
    are not where this expects them. None means dovi_tool said nothing, and
    nothing else may be allowed to mean that.
    """
    line = _MISMATCH_LINE.search(stderr or "")
    if line is None:
        return None
    match = _MISMATCH_RE.search(line.group(0))
    if match is None:
        raise UnreadableMismatch(line.group(0))
    return int(match.group("video")), int(match.group("rpu"))


class DoviTool:
    """Command wrappers. Every method returns the path it wrote."""

    def __init__(self, toolbox: Toolbox | None = None) -> None:
        self.box = toolbox or Toolbox()

    # -- reading ---------------------------------------------------------

    def extract_rpu(self, source: Path, out: Path, mode: int | None = None) -> Path:
        """Extract the RPU from an HEVC bitstream or Matroska file."""
        args: list[str] = []
        if mode is not None:
            args += ["-m", str(mode)]
        args += ["extract-rpu", str(source), "-o", str(out)]
        self.box.run("dovi_tool", args)
        return out

    def info(self, rpu: Path) -> RpuInfo:
        """Summary of an RPU file."""
        proc = self.box.run("dovi_tool", ["info", "-i", str(rpu), "-s"])
        return parse_info(proc.stdout)

    def export(self, rpu: Path, out: Path, what: str = "all") -> Path:
        """Export RPU metadata to JSON (``all``, ``scenes``, ``level5``...)."""
        self.box.run("dovi_tool", ["export", "-i", str(rpu), "-d", f"{what}={out}"])
        return out

    def export_levels(self, rpu: Path, out: Path, level: str = "level1") -> Path:
        """Export one metadata level as CSV, one row per frame.

        Separate from :meth:`export` because it is a different flag with a
        different output format: ``--data`` writes JSON, ``--levels`` writes
        CSV, and only the latter reaches L1 through L11.
        """
        self.box.run("dovi_tool", ["export", "-i", str(rpu), "--levels", f"{level}={out}"])
        return out

    # -- editing ---------------------------------------------------------

    def editor(self, rpu: Path, config: dict[str, Any], out: Path, *, workdir: Path) -> Path:
        """Apply an editor config (``mode``, ``remove``, ``duplicate``...)."""
        config_path = workdir / "editor.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        self.box.run(
            "dovi_tool",
            ["editor", "-i", str(rpu), "-j", str(config_path), "-o", str(out)],
        )
        return out

    def convert(self, source: Path, out: Path, *, mode: int, discard_el: bool = True) -> Path:
        """Convert the RPU inside a single-layer HEVC file.

        ``-m`` is a global flag and must precede the subcommand.
        """
        args = ["-m", str(mode), "convert"]
        if discard_el:
            args.append("--discard")
        args += ["-i", str(source), "-o", str(out)]
        self.box.run("dovi_tool", args)
        return out

    # -- writing ---------------------------------------------------------

    def inject_rpu(
        self,
        video: Path,
        rpu: Path,
        out: Path,
        *,
        video_frames: int | None = None,
        frame_tolerance: int = DEFAULT_FRAME_TOLERANCE,
        progress: Callable[[str], None] | None = None,
    ) -> Path:
        """Interleave RPU NAL units into *video*.

        Raises :class:`FrameCountMismatch` when the lengths disagree by more
        than *frame_tolerance*, and deletes the file it wrote — a misaligned
        output is worse than no output, because it looks correct.

        When *video_frames* is known the counts are compared before dovi_tool
        runs, which is both faster and sturdier. Faster because a doomed job
        fails in a second instead of after rewriting 68 GB. Sturdier because
        the check afterwards is a regex over the warning dovi_tool prints, and
        a reworded warning would leave nothing behind it: this was the only
        guard on the Dolby Vision side, while the HDR10+ side had checked up
        front all along.
        """
        if video_frames is not None:
            # Reading the count costs a `dovi_tool info` on a file of a few
            # megabytes: measured at 0.06s for a 15,000-frame RPU, against the
            # twenty minutes the injection it guards would otherwise spend.
            # The pipeline has this figure already, but taking it again keeps
            # the guard inside the method that writes the file.
            rpu_frames = self.info(rpu).frames
            if abs(rpu_frames - video_frames) > frame_tolerance:
                raise FrameCountMismatch(video_frames, rpu_frames)

        proc = self.box.run(
            "dovi_tool",
            ["inject-rpu", "-i", str(video), "--rpu-in", str(rpu), "-o", str(out)],
            on_output=progress,
        )
        combined = f"{proc.stdout}\n{proc.stderr}"
        try:
            mismatch = find_mismatch(combined)
        except UnreadableMismatch:
            # The refusal is only half of it: dovi_tool has already written the
            # file, and leaving 68 GB of misaligned output behind for someone to
            # find later is the thing this method exists to prevent.
            out.unlink(missing_ok=True)
            raise
        if mismatch is not None:
            video_frames, rpu_frames = mismatch
            if abs(rpu_frames - video_frames) > frame_tolerance:
                out.unlink(missing_ok=True)
                raise FrameCountMismatch(video_frames, rpu_frames)
        return out

    def remove(self, source: Path, out: Path) -> Path:
        """Strip the enhancement layer and RPU, leaving the base layer."""
        self.box.run("dovi_tool", ["remove", str(source), "-o", str(out)])
        return out
