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

from hibrit.tools import KNOWN_TOOLS, REQUIRED_TOOLS, MissingTool, Toolbox, ToolStatus


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
