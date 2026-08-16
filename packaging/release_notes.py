"""Extract release notes for a tag, and refuse the release if they are missing.

Run before the build, not after: a tag that disagrees with the packaged version,
or one the changelog says nothing about, should stop the workflow in a second
rather than twenty minutes later when the artefact is already built.

    python packaging/release_notes.py v0.1.0 notes.md
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"


def packaged_version() -> str:
    """Read __version__ without importing the package or its dependencies."""
    text = (ROOT / "hibrit" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise SystemExit("hibrit/__init__.py does not define __version__")
    return match.group(1)


def section_for(version: str, changelog: str) -> str:
    """The body under ``## <version>``, up to the next second-level heading."""
    pattern = rf"^##\s+{re.escape(version)}\s*$(.*?)(?=^##\s|\Z)"
    match = re.search(pattern, changelog, re.MULTILINE | re.DOTALL)
    if match is None:
        raise SystemExit(
            f"CHANGELOG.md has no '## {version}' section.\n"
            "A release with nothing written about it is a release nobody can read."
        )
    body = match.group(1).strip()
    if not body:
        raise SystemExit(f"the '## {version}' section in CHANGELOG.md is empty")
    return body


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit(f"usage: {Path(__file__).name} <tag> <output.md>")
    tag, out = argv
    version = tag.lstrip("v")

    packaged = packaged_version()
    if version != packaged:
        raise SystemExit(f"tag says {version}, hibrit/__init__.py says {packaged}")

    notes = section_for(version, CHANGELOG.read_text(encoding="utf-8"))
    Path(out).write_text(
        f"{notes}\n\n---\n\n"
        "`dovi_tool` and `hdr10plus_tool` are not bundled — they are separate "
        "programs under active development, and a frozen copy would go stale. "
        "Run `hibrit doctor` after unzipping to see what to download.\n",
        encoding="utf-8",
    )
    print(f"notes for {version} written to {out} ({len(notes)} characters)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
