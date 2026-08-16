"""Discovery and invocation of the external binaries hibrit drives.

hibrit never installs anything system-wide. Binaries are looked up in this
order, most specific first:

1. an explicit path passed by the caller
2. any directory the caller named (``--tools-dir``)
3. the ``tools/`` directory next to the package (or next to the frozen exe)
4. ``PATH``

Someone who passes ``--tools-dir`` is pointing at a particular build for a
reason — a newer dovi_tool, a debug build — so that directory outranks the
bundled default rather than being a fallback for it.

Nothing here silently falls back to a different binary: if a tool is missing,
the caller gets a :class:`MissingTool` error naming it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Binaries hibrit knows how to drive, mapped to the executable stem to look for.
KNOWN_TOOLS: dict[str, str] = {
    "dovi_tool": "dovi_tool",
    "hdr10plus_tool": "hdr10plus_tool",
    "mediainfo": "mediainfo",
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffprobe",
    "mkvmerge": "mkvmerge",
    "mkvextract": "mkvextract",
}

#: Tools without which hibrit cannot do its core job.
REQUIRED_TOOLS: tuple[str, ...] = (
    "dovi_tool",
    "hdr10plus_tool",
    "mediainfo",
    "ffmpeg",
    "mkvmerge",
    "mkvextract",
)


class MissingTool(RuntimeError):
    """Raised when a required external binary cannot be located."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"{name} not found. Put it in the tools/ directory next to hibrit, "
            f"or on PATH. Run `hibrit doctor` to see what is missing."
        )
        self.name = name


class ToolFailed(RuntimeError):
    """Raised when an external binary exits non-zero."""

    def __init__(self, argv: Sequence[str], returncode: int, stderr: str) -> None:
        super().__init__(f"{Path(argv[0]).name} exited {returncode}\n{stderr.strip()}")
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class ToolStatus:
    """Where a tool was found and what version it reports."""

    name: str
    path: Path | None
    version: str | None
    required: bool

    @property
    def ok(self) -> bool:
        return self.path is not None


def bundled_tools_dir() -> Path:
    """Directory searched before PATH.

    When frozen by PyInstaller the exe lives next to ``tools/``; in a source
    checkout the package lives one level below the repo root.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "tools"
    return Path(__file__).resolve().parent.parent / "tools"


class Toolbox:
    """Resolves and runs the external binaries."""

    def __init__(
        self,
        overrides: dict[str, str | Path] | None = None,
        extra_dirs: Iterable[Path] = (),
    ) -> None:
        self._overrides = {k: Path(v) for k, v in (overrides or {}).items()}
        self._search_dirs = [*extra_dirs, bundled_tools_dir()]
        self._cache: dict[str, Path | None] = {}

    def find(self, name: str) -> Path | None:
        """Locate *name*, or return ``None``. Results are cached."""
        if name in self._cache:
            return self._cache[name]

        found: Path | None = None
        override = self._overrides.get(name)
        if override is not None and override.exists():
            found = override

        stem = KNOWN_TOOLS.get(name, name)
        if found is None:
            for directory in self._search_dirs:
                for suffix in (".exe", "") if os.name == "nt" else ("", ".exe"):
                    candidate = directory / f"{stem}{suffix}"
                    if candidate.is_file():
                        found = candidate
                        break
                if found is not None:
                    break

        if found is None:
            which = shutil.which(stem)
            if which:
                found = Path(which)

        self._cache[name] = found
        return found

    def require(self, name: str) -> Path:
        """Locate *name* or raise :class:`MissingTool`."""
        path = self.find(name)
        if path is None:
            raise MissingTool(name)
        return path

    def run(
        self,
        name: str,
        args: Sequence[str],
        *,
        check: bool = True,
        capture: bool = True,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run *name* with *args*.

        stdout and stderr are captured as text so callers can inspect warnings.
        That matters more than usual here: dovi_tool reports a frame-count
        mismatch on stderr while still exiting 0, so the only way to catch it
        is to read the output. See :mod:`hibrit.rpu`.
        """
        exe = self.require(name)
        argv = [str(exe), *[str(a) for a in args]]
        proc = subprocess.run(
            argv,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd) if cwd else None,
        )
        if check and proc.returncode != 0:
            raise ToolFailed(argv, proc.returncode, proc.stderr or "")
        return proc

    def version_of(self, name: str) -> str | None:
        """Best-effort version string, or ``None`` if the tool is absent."""
        if self.find(name) is None:
            return None
        flag = "--Version" if name == "mediainfo" else "--version"
        try:
            proc = self.run(name, [flag], check=False)
        except MissingTool:  # pragma: no cover - guarded above
            return None
        text = (proc.stdout or proc.stderr or "").strip()
        for line in text.splitlines():
            line = line.strip()
            if line:
                return line
        return None

    def doctor(self) -> list[ToolStatus]:
        """Status of every known tool, for ``hibrit doctor``."""
        return [
            ToolStatus(
                name=name,
                path=self.find(name),
                version=self.version_of(name),
                required=name in REQUIRED_TOOLS,
            )
            for name in KNOWN_TOOLS
        ]

    def missing_required(self) -> list[str]:
        """Names of required tools that could not be found."""
        return [name for name in REQUIRED_TOOLS if self.find(name) is None]
