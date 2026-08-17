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

import locale
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Binaries hibrit knows how to drive, mapped to the executable stem to look for.
#:
#: Everything listed here is something the program actually runs. ``ffprobe`` was
#: here and is not any more: it is never invoked — MediaInfo answers the one
#: question that matters and ffprobe does not report FrameCount reliably — so
#: listing it only told users to go and install something for no reason. A test
#: checks this list against the calls in the source.
KNOWN_TOOLS: dict[str, str] = {
    "dovi_tool": "dovi_tool",
    "hdr10plus_tool": "hdr10plus_tool",
    "mediainfo": "mediainfo",
    "ffmpeg": "ffmpeg",
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


#: A version-looking token: 2.3.3, v99.0, 26.05, or a dated git build.
_VERSION = re.compile(r"v?\d+(?:\.\d+)+|\d{4}-\d{2}-\d{2}")

#: Where a version line turns into boilerplate. ffmpeg prints its copyright on
#: the same line as its version and the copyright is four times as long.
_NOISE = re.compile(r"\s+(?:Copyright|copyright)\b.*$")


def console_encoding() -> str:
    """What the console this process was started from writes in."""
    return locale.getpreferredencoding(False)


def decode_output(raw: bytes, *, fallback: str | None = None) -> str:
    """Decode a line of tool output without losing characters.

    The toolchain is mixed. dovi_tool and hdr10plus_tool are Rust programs and
    emit UTF-8 whatever the console is set to; mkvtoolnix on Windows writes in
    the console code page. Forcing UTF-8 on the latter throws away every
    non-ASCII character: measured here, mkvextract's progress line arrived as
    ``b"\\xddlerleme: 0%"`` — 0xDD is not valid UTF-8, so ``errors="replace"``
    turned it into a replacement character and the log went to mojibake.

    Paths matter more than progress lines. Filenames carry non-ASCII letters all
    the time, and a tool echoing one back should not come out mangled in a log
    somebody is reading to work out what went wrong.

    UTF-8 first, because it is right for most of these tools and never guesses
    wrong — invalid UTF-8 is detectable. Then the console encoding. Then
    latin-1, which maps all 256 byte values and so cannot fail: the last
    resort is a wrong letter, never a lost one, and never an exception in the
    middle of a finished job.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode(fallback or console_encoding())
    except (UnicodeDecodeError, LookupError):
        return raw.decode("latin-1")


def parse_version(text: str) -> str | None:
    """Pull a readable version out of whatever a tool prints.

    Taking the first non-empty line is not enough. ``mediainfo --Version``
    opens with "MediaInfo Command line," and puts the number on the line after,
    so doctor reported no version at all for the one tool whose version matters
    most — an old copy silently under-reports the HDR fields everything here
    depends on.
    """
    for line in text.splitlines():
        line = _NOISE.sub("", line.strip())
        if line and _VERSION.search(line):
            return line if len(line) <= 70 else line[:67] + "..."
    # Nothing version-shaped; the first non-empty line is better than nothing.
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return None


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
        on_output: Callable[[str], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run *name* with *args*.

        stdout and stderr are captured as text so callers can inspect warnings.
        That matters more than usual here: dovi_tool reports a frame-count
        mismatch on stderr while still exiting 0, so the only way to catch it
        is to read the output. See :mod:`hibrit.rpu`.

        *on_output* forwards each line as it arrives instead of at the end.
        Rewriting a 68 GB stream takes minutes during which these tools print
        their progress and hibrit would otherwise swallow all of it, leaving a
        command that looks hung. The output is still collected and returned, so
        nothing that inspects it has to change.
        """
        exe = self.require(name)
        argv = [str(exe), *[str(a) for a in args]]

        if on_output is None:
            # Captured as bytes and decoded here rather than by subprocess, so
            # that a tool writing in the console code page survives. See
            # decode_output.
            raw = subprocess.run(
                argv,
                capture_output=capture,
                cwd=str(cwd) if cwd else None,
            )
            proc = subprocess.CompletedProcess(
                argv,
                raw.returncode,
                decode_output(raw.stdout or b""),
                decode_output(raw.stderr or b""),
            )
        else:
            proc = self._run_streaming(argv, cwd, on_output)

        if check and proc.returncode != 0:
            raise ToolFailed(argv, proc.returncode, proc.stderr or "")
        return proc

    @staticmethod
    def _run_streaming(
        argv: list[str], cwd: Path | None, on_output: Callable[[str], None]
    ) -> subprocess.CompletedProcess[str]:
        """Run *argv*, forwarding output as it appears.

        Read in small chunks and split on carriage returns as well as newlines:
        every one of these tools draws its progress by returning to the start of
        the line, so waiting for a newline would wait for the whole operation
        and defeat the point.
        """
        collected: list[str] = []
        partial = b""

        def emit(raw: bytes) -> None:
            text = decode_output(raw).strip()
            if text:
                collected.append(text)
                on_output(text)

        # Read bytes, split on line breaks, decode whole lines. Decoding chunks
        # would cut multi-byte characters in half at arbitrary boundaries.
        with subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            cwd=str(cwd) if cwd else None,
        ) as process:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(256)
                if not chunk:
                    break
                partial += chunk
                partial = partial.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                *lines, partial = partial.split(b"\n")
                for line in lines:
                    emit(line)
            emit(partial)
            returncode = process.wait()

        text = "\n".join(collected)
        return subprocess.CompletedProcess(argv, returncode, text, text)

    def version_of(self, name: str) -> str | None:
        """Best-effort version string, or ``None`` if the tool is absent."""
        if self.find(name) is None:
            return None
        flag = "--Version" if name == "mediainfo" else "--version"
        try:
            proc = self.run(name, [flag], check=False)
        except MissingTool:  # pragma: no cover - guarded above
            return None
        return parse_version(proc.stdout or proc.stderr or "")

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
