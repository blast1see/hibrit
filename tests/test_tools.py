"""Binary discovery.

The order matters: a project-local ``tools/`` copy must win over whatever is on
PATH. The machine this was written on has MediaInfo 21.03 from 2021 sitting in
``C:\\dee`` as part of an unrelated install, and it does not report the fields
hibrit needs. Silently picking that one up would produce a wrong diagnosis
rather than an error.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hibrit.tools import (
    KNOWN_TOOLS,
    REQUIRED_TOOLS,
    MissingTool,
    Toolbox,
    ToolStatus,
    parse_version,
)


def _fake_binary(directory: Path, stem: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (f"{stem}.exe" if os.name == "nt" else stem)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class TestDiscovery:
    def test_finds_a_binary_in_an_extra_directory(self, tmp_path: Path) -> None:
        expected = _fake_binary(tmp_path / "bin", "dovi_tool")
        box = Toolbox(extra_dirs=(tmp_path / "bin",))
        assert box.find("dovi_tool") == expected

    def test_an_override_wins_over_every_directory(self, tmp_path: Path) -> None:
        _fake_binary(tmp_path / "bin", "dovi_tool")
        special = _fake_binary(tmp_path / "elsewhere", "dovi_tool")
        box = Toolbox(overrides={"dovi_tool": special}, extra_dirs=(tmp_path / "bin",))
        assert box.find("dovi_tool") == special

    def test_an_override_pointing_nowhere_is_ignored_not_obeyed(self, tmp_path: Path) -> None:
        real = _fake_binary(tmp_path / "bin", "dovi_tool")
        box = Toolbox(
            overrides={"dovi_tool": tmp_path / "gone.exe"},
            extra_dirs=(tmp_path / "bin",),
        )
        assert box.find("dovi_tool") == real

    def test_a_missing_tool_is_none_rather_than_a_substitute(self, tmp_path: Path) -> None:
        box = Toolbox(extra_dirs=(tmp_path,))
        assert box.find("no_such_tool_at_all") is None

    def test_the_result_is_cached(self, tmp_path: Path) -> None:
        path = _fake_binary(tmp_path, "hdr10plus_tool")
        box = Toolbox(extra_dirs=(tmp_path,))
        assert box.find("hdr10plus_tool") == path
        path.unlink()
        assert box.find("hdr10plus_tool") == path


class TestRequire:
    def test_names_the_tool_and_says_where_to_put_it(self, tmp_path: Path) -> None:
        box = Toolbox(extra_dirs=(tmp_path,))
        with pytest.raises(MissingTool) as caught:
            box.require("definitely_not_installed")
        assert caught.value.name == "definitely_not_installed"
        assert "hibrit doctor" in str(caught.value)


class TestParseVersion:
    """What each tool actually prints, and what doctor should show for it."""

    def test_mediainfo_puts_the_number_on_the_second_line(self) -> None:
        """The one that mattered.

        Taking the first non-empty line gave "MediaInfo Command line," — no
        version at all, for the tool whose version matters most. An old copy
        under-reports the HDR fields everything here depends on, and this
        machine has a 2021 one installed by something else.
        """
        assert (
            parse_version("MediaInfo Command line,\nMediaInfoLib - v26.05\n")
            == "MediaInfoLib - v26.05"
        )

    def test_ffmpeg_copyright_is_trimmed(self) -> None:
        text = (
            "ffmpeg version 2026-08-03-git-01a25f74cc-full_build-www.gyan.dev "
            "Copyright (c) 2000-2026 the FFmpeg developers\nbuilt with gcc 15.2.0"
        )
        version = parse_version(text)
        assert version is not None
        assert version.startswith("ffmpeg version 2026-08-03")
        assert "Copyright" not in version

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("dovi_tool 2.3.3", "dovi_tool 2.3.3"),
            ("hdr10plus_tool 1.7.2", "hdr10plus_tool 1.7.2"),
            ("mkvmerge v99.0 ('Buka') 64-bit", "mkvmerge v99.0 ('Buka') 64-bit"),
        ],
    )
    def test_the_ones_that_were_already_fine_stay_fine(self, text: str, expected: str) -> None:
        assert parse_version(text) == expected

    def test_a_very_long_line_is_cut(self) -> None:
        version = parse_version("tool 1.0 " + "x" * 200)
        assert version is not None
        assert len(version) == 70

    def test_output_with_no_version_falls_back_to_the_first_line(self) -> None:
        assert parse_version("some tool\nwith no numbers") == "some tool"

    def test_nothing_at_all_is_none(self) -> None:
        assert parse_version("") is None
        assert parse_version("   \n\n  ") is None


class TestTheListIsHonest:
    """`hibrit doctor` should only report on tools the program runs.

    ffprobe sat in this list for a while without ever being invoked, so doctor
    told people to go and install something the program has no use for. The
    check is cheap and the failure is silent, which is the combination worth
    a test.
    """

    @staticmethod
    def _invoked_names() -> set[str]:
        import re

        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (Path(__file__).resolve().parent.parent / "hibrit").glob("*.py")
        )
        calls = re.findall(r'\b(?:run|require|find|version_of)\(\s*"([a-z0-9_]+)"', source)
        return set(calls)

    def test_every_known_tool_is_actually_invoked(self) -> None:
        unused = set(KNOWN_TOOLS) - self._invoked_names()
        assert not unused, f"declared but never run: {sorted(unused)}"

    def test_every_invoked_tool_is_declared(self) -> None:
        """The other direction: a tool run without being declared would never
        appear in doctor, so its absence would surface as a crash mid-job."""
        undeclared = self._invoked_names() - set(KNOWN_TOOLS)
        assert not undeclared, f"run but not declared: {sorted(undeclared)}"

    def test_required_is_a_subset_of_known(self) -> None:
        assert set(REQUIRED_TOOLS) <= set(KNOWN_TOOLS)


class TestDoctor:
    def test_reports_every_known_tool(self, tmp_path: Path) -> None:
        box = Toolbox(extra_dirs=(tmp_path,))
        statuses = box.doctor()
        assert [s.name for s in statuses] == list(KNOWN_TOOLS)
        assert all(isinstance(s, ToolStatus) for s in statuses)

    def test_required_flags_match_the_declared_set(self, tmp_path: Path) -> None:
        box = Toolbox(extra_dirs=(tmp_path,))
        required = {s.name for s in box.doctor() if s.required}
        assert required == set(REQUIRED_TOOLS)

    def test_missing_required_lists_names_not_a_bare_boolean(self, tmp_path: Path) -> None:
        """The caller has to be able to say which one is missing."""
        box = Toolbox(extra_dirs=(tmp_path,))
        box._cache = dict.fromkeys(KNOWN_TOOLS)  # nothing found anywhere
        assert box.missing_required() == list(REQUIRED_TOOLS)

    def test_status_ok_follows_the_path(self) -> None:
        assert ToolStatus("x", Path("x"), None, True).ok
        assert not ToolStatus("x", None, None, True).ok
