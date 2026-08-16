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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hibrit.tools import Toolbox

#: Frame-count differences at or below this are padded silently by dovi_tool
#: and are harmless; anything larger is a misalignment and must be refused.
DEFAULT_FRAME_TOLERANCE = 3

_MISMATCH_RE = re.compile(
    r"mismatched lengths\.\s*video\s+(?P<video>\d+),\s*RPU\s+(?P<rpu>\d+)",
    re.IGNORECASE,
)
_FRAMES_RE = re.compile(r"^\s*Frames:\s*(\d+)", re.MULTILINE)
_PROFILE_RE = re.compile(r"^\s*Profile:\s*(\d+)(?:\s*\((?P<layer>\w+)\))?", re.MULTILINE)
_DM_RE = re.compile(r"^\s*DM version:\s*(?P<dm>.+)$", re.MULTILINE)
_SCENES_RE = re.compile(r"^\s*Scene/shot count:\s*(\d+)", re.MULTILINE)
_L5_RE = re.compile(
    r"^\s*L5 offsets:\s*top=(\d+),\s*bottom=(\d+),\s*left=(\d+),\s*right=(\d+)",
    re.MULTILINE,
)


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
class RpuInfo:
    """Parsed ``dovi_tool info -s`` summary."""

    frames: int
    profile: int
    layer_kind: str | None
    dm_version: str | None
    scene_count: int | None
    l5_offsets: tuple[int, int, int, int] | None
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


def parse_info(text: str) -> RpuInfo:
    """Parse the summary ``dovi_tool info -s`` prints."""
    frames = _FRAMES_RE.search(text)
    profile = _PROFILE_RE.search(text)
    if frames is None or profile is None:
        raise ValueError(f"could not parse dovi_tool info output:\n{text}")
    scenes = _SCENES_RE.search(text)
    dm = _DM_RE.search(text)
    l5 = _L5_RE.search(text)
    return RpuInfo(
        frames=int(frames.group(1)),
        profile=int(profile.group(1)),
        layer_kind=profile.group("layer"),
        dm_version=dm.group("dm").strip() if dm else None,
        scene_count=int(scenes.group(1)) if scenes else None,
        l5_offsets=(
            (int(l5.group(1)), int(l5.group(2)), int(l5.group(3)), int(l5.group(4))) if l5 else None
        ),
        raw=text,
    )


def find_mismatch(stderr: str) -> tuple[int, int] | None:
    """Return ``(video_frames, rpu_frames)`` if dovi_tool warned, else None."""
    match = _MISMATCH_RE.search(stderr or "")
    if match is None:
        return None
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
        frame_tolerance: int = DEFAULT_FRAME_TOLERANCE,
    ) -> Path:
        """Interleave RPU NAL units into *video*.

        Raises :class:`FrameCountMismatch` when dovi_tool reports a length
        mismatch larger than *frame_tolerance*, and deletes the file it wrote —
        a misaligned output is worse than no output, because it looks correct.
        """
        proc = self.box.run(
            "dovi_tool",
            ["inject-rpu", "-i", str(video), "--rpu-in", str(rpu), "-o", str(out)],
        )
        combined = f"{proc.stdout}\n{proc.stderr}"
        mismatch = find_mismatch(combined)
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
