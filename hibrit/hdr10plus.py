"""Thin wrapper around ``hdr10plus_tool``.

Same posture as :mod:`hibrit.rpu`: the metadata is a per-frame list, so a
length mismatch between the JSON and the target video is a misalignment, not a
detail. The count is checked here rather than trusted to the tool.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hibrit.tools import Toolbox

DEFAULT_FRAME_TOLERANCE = 3

#: The line hdr10plus_tool 1.7.2 prints, measured rather than assumed::
#:
#:     Warning: mismatched lengths. video 240, HDR10+ JSON 150
#:     Metadata will be duplicated at the end to match video length
#:
#: Two attempts at a pattern went wrong here, which is why this is now split
#: into "find the line" and "read the numbers off it". The first expected a
#: count straight after "HDR10+", as dovi_tool's message has after "RPU", and
#: matched nothing — the guard was dead code for as long as nobody fed it a
#: real mismatch. The second skipped ahead to the next number and found the
#: "10" inside "HDR10+".
_MISMATCH_LINE = re.compile(r"^.*mismatched lengths\..*$", re.IGNORECASE | re.MULTILINE)

#: The video's count is the number right after "video"; the metadata's is the
#: last number on the line, whatever words the tool puts in between.
_VIDEO_COUNT = re.compile(r"video\s+(\d+)", re.IGNORECASE)
_TRAILING_COUNT = re.compile(r"(\d+)\s*$")


class Hdr10PlusMismatch(RuntimeError):
    """Raised instead of writing a silently misaligned file."""

    def __init__(self, video_frames: int, meta_frames: int) -> None:
        delta = meta_frames - video_frames
        super().__init__(
            f"HDR10+ metadata has {meta_frames} frames, video has {video_frames} "
            f"(difference {abs(delta)}). Align the metadata first (hibrit align)."
        )
        self.video_frames = video_frames
        self.meta_frames = meta_frames
        self.delta = delta


@dataclass(frozen=True)
class Hdr10PlusInfo:
    """What a HDR10+ JSON file contains."""

    frames: int
    profile: str | None
    path: Path

    def describe(self) -> str:
        profile = f" · profile {self.profile}" if self.profile else ""
        return f"{self.frames} frames{profile}"


def read_json(path: Path) -> Hdr10PlusInfo:
    """Count frames in an hdr10plus_tool JSON export."""
    payload: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    scenes = payload.get("SceneInfo") or []
    summary = payload.get("SceneInfoSummary") or {}
    profile = payload.get("Profile") or summary.get("Profile")
    return Hdr10PlusInfo(frames=len(scenes), profile=profile, path=Path(path))


def find_mismatch(stderr: str) -> tuple[int, int] | None:
    """Return ``(video_frames, metadata_frames)`` if the tool warned, else None."""
    line = _MISMATCH_LINE.search(stderr or "")
    if line is None:
        return None
    text = line.group(0)
    video = _VIDEO_COUNT.search(text)
    meta = _TRAILING_COUNT.search(text.rstrip())
    if video is None or meta is None:
        return None
    return int(video.group(1)), int(meta.group(1))


class Hdr10PlusTool:
    """Command wrappers. Every method returns the path it wrote."""

    def __init__(self, toolbox: Toolbox | None = None) -> None:
        self.box = toolbox or Toolbox()

    def extract(self, source: Path, out: Path, *, skip_validation: bool = False) -> Path:
        """Extract HDR10+ metadata to JSON from an HEVC or Matroska file."""
        args: list[str] = []
        if skip_validation:
            args.append("--skip-validation")
        args += ["extract", str(source), "-o", str(out)]
        self.box.run("hdr10plus_tool", args)
        return out

    def editor(self, metadata: Path, config: dict[str, Any], out: Path, *, workdir: Path) -> Path:
        """Apply ``remove`` / ``duplicate`` edits to a metadata JSON."""
        config_path = workdir / "hdr10plus_editor.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        self.box.run(
            "hdr10plus_tool",
            ["editor", str(metadata), "-j", str(config_path), "-o", str(out)],
        )
        return out

    def inject(
        self,
        video: Path,
        metadata: Path,
        out: Path,
        *,
        video_frames: int | None = None,
        frame_tolerance: int = DEFAULT_FRAME_TOLERANCE,
        progress: Callable[[str], None] | None = None,
    ) -> Path:
        """Interleave HDR10+ SEI NAL units into *video*.

        When *video_frames* is known the count is checked before running, so a
        misaligned job fails in a second instead of after rewriting 68 GB.
        """
        meta = read_json(metadata)
        if video_frames is not None and abs(meta.frames - video_frames) > frame_tolerance:
            raise Hdr10PlusMismatch(video_frames, meta.frames)

        proc = self.box.run(
            "hdr10plus_tool",
            ["inject", "-i", str(video), "-j", str(metadata), "-o", str(out)],
            on_output=progress,
        )
        mismatch = find_mismatch(f"{proc.stdout}\n{proc.stderr}")
        if mismatch is not None:
            found_video, found_meta = mismatch
            if abs(found_meta - found_video) > frame_tolerance:
                out.unlink(missing_ok=True)
                raise Hdr10PlusMismatch(found_video, found_meta)
        return out

    def remove(self, source: Path, out: Path) -> Path:
        """Strip HDR10+ SEI NAL units."""
        self.box.run("hdr10plus_tool", ["remove", str(source), "-o", str(out)])
        return out
