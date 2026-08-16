"""The checks that run before a release build, not after it.

A tag that disagrees with the packaged version, or one the changelog says
nothing about, should stop the workflow in a second. The sibling project in this
house cut a package four merges before it meant to; the cheap guard against that
class of mistake is checking the paperwork before spending twenty minutes on the
artefact.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# packaging/ is a script directory, not an importable package: the release
# workflow runs it with `python packaging/release_notes.py`, so there is nothing
# to install and the path has to be added by hand.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packaging"))

import release_notes

CHANGELOG = """\
# Changelog

## 0.2.0

Second release.

### A heading inside the section

Body text that belongs to 0.2.0.

## 0.1.0

First release.

## 0.0.1

Nothing here yet.
"""


class TestSectionFor:
    def test_takes_only_the_requested_version(self) -> None:
        body = release_notes.section_for("0.2.0", CHANGELOG)
        assert body.startswith("Second release.")
        assert "belongs to 0.2.0" in body
        assert "First release" not in body

    def test_keeps_subheadings_inside_the_section(self) -> None:
        """Third-level headings are part of the notes; second-level ones end them."""
        assert "### A heading inside the section" in release_notes.section_for("0.2.0", CHANGELOG)

    def test_the_last_section_runs_to_the_end(self) -> None:
        assert release_notes.section_for("0.0.1", CHANGELOG) == "Nothing here yet."

    def test_a_version_nobody_wrote_about_is_refused(self) -> None:
        with pytest.raises(SystemExit, match=re.escape("no '## 9.9.9' section")):
            release_notes.section_for("9.9.9", CHANGELOG)

    def test_an_empty_section_is_refused_too(self) -> None:
        """A heading with nothing under it is not release notes."""
        with pytest.raises(SystemExit, match="empty"):
            release_notes.section_for("3.0.0", "## 3.0.0\n\n## 2.0.0\n\nreal notes\n")

    def test_a_version_is_not_matched_by_its_prefix(self) -> None:
        """0.1 must not silently pick up the 0.1.0 section."""
        with pytest.raises(SystemExit):
            release_notes.section_for("0.1", CHANGELOG)


class TestPackagedVersion:
    def test_reads_the_real_version_without_importing_the_package(self) -> None:
        from hibrit import __version__

        assert release_notes.packaged_version() == __version__

    def test_this_project_s_changelog_covers_this_project_s_version(self) -> None:
        """The check the workflow runs, run here so it fails at commit time."""
        version = release_notes.packaged_version()
        changelog = release_notes.CHANGELOG.read_text(encoding="utf-8")
        assert release_notes.section_for(version, changelog)
