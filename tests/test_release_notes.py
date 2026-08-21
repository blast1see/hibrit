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

    def test_the_two_places_that_state_a_version_agree(self) -> None:
        """`pyproject.toml` and `hibrit/__init__.py` both name it; nothing checked.

        Every release means editing two files, and the failure mode of editing
        one is a build whose `--version` disagrees with the wheel it came from —
        the same class of quiet inconsistency the changelog check above exists
        to catch, on the one axis that had nothing watching it.
        """
        import re
        from pathlib import Path

        pyproject = (Path(release_notes.ROOT) / "pyproject.toml").read_text(encoding="utf-8")
        declared = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
        assert declared is not None, "pyproject.toml does not declare a version"
        assert declared.group(1) == release_notes.packaged_version()


class TestThePackagedLayout:
    """What the reader finds after unzipping is part of the product.

    `doctor` tells them to put the missing binaries in hibrit/tools/. Until
    0.1.0 shipped, that folder was not in the build, so the advice ended with
    the reader guessing whether to create it and where. It is created next to
    the executable rather than through `datas`, because PyInstaller puts datas
    under _internal/ and bundled_tools_dir() looks beside the exe.
    """

    ROOT = Path(__file__).resolve().parent.parent

    def test_the_note_that_ships_in_the_tools_folder_exists(self) -> None:
        note = self.ROOT / "packaging" / "tools-placeholder.txt"
        assert note.exists(), "the spec copies this into the build"
        text = note.read_text(encoding="utf-8")
        assert "dovi_tool" in text and "hdr10plus_tool" in text
        assert "releases" in text, "it has to say where to get them"

    def test_the_spec_puts_it_beside_the_executable(self) -> None:
        spec = (self.ROOT / "hibrit.spec").read_text(encoding="utf-8")
        assert "tools-placeholder.txt" in spec
        # Beside the exe, not under _internal: DISTPATH rather than datas.
        assert 'Path(DISTPATH) / "hibrit" / "tools"' in spec

    def test_doctor_points_at_the_folder_the_build_ships(self) -> None:
        """The message and the layout have to agree, or one of them is a lie."""
        from hibrit import cli

        source = (self.ROOT / "hibrit" / "cli.py").read_text(encoding="utf-8")
        assert "hibrit/tools/" in source
        assert cli is not None
