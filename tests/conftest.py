"""Shared fixtures.

Three tiers of test live here, separated by marker:

* unmarked — pure logic, no binaries, no media. These run everywhere.
* ``tools`` — needs dovi_tool / hdr10plus_tool / ffmpeg on this machine.
* ``real`` — needs the large media files the project was developed against.

The split exists because the lesson this project inherited is that unit tests
alone are not evidence. The unmarked tier proves the code does what it says;
only the ``real`` tier proves the result is right.
"""

from __future__ import annotations

import os
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from hibrit.probe import VideoInfo
from hibrit.tools import Toolbox

#: Set HIBRIT_MEDIA to a directory holding the sample files to enable the
#: ``real`` tier. Nothing here downloads anything.
MEDIA_DIR = Path(os.environ.get("HIBRIT_MEDIA", r"E:\hibrit-calisma"))


def pytest_collection_modifyitems(config, items) -> None:
    """Mark the tiers that cannot run here as skipped, at collection time.

    This has to happen during collection rather than in an autouse fixture:
    session-scoped fixtures are set up before function-scoped ones, so a
    fixture that builds a clip with ffmpeg would run — and fail — before any
    per-test skip had a chance to fire.
    """
    missing = Toolbox().missing_required()
    if not missing:
        return
    skip = pytest.mark.skip(reason=f"external tools not available: {', '.join(missing)}")
    for item in items:
        if item.get_closest_marker("tools") or item.get_closest_marker("real"):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def toolbox() -> Toolbox:
    return Toolbox()


@pytest.fixture(scope="session")
def have_tools(toolbox: Toolbox) -> bool:
    return not toolbox.missing_required()


@pytest.fixture(scope="session")
def media() -> Path:
    if not MEDIA_DIR.is_dir():
        pytest.skip(f"media directory {MEDIA_DIR} not present")
    return MEDIA_DIR


def make_info(
    name: str = "sample.mkv",
    *,
    codec: str = "HEVC",
    width: int = 3840,
    height: int = 2160,
    frames: int | None = 100_000,
    rate: Fraction | None = Fraction(24000, 1001),
    hdr10: bool = True,
    hdr10plus: bool = False,
    dv: bool = False,
    dv_profile: int | None = None,
    dv_layers: str | None = None,
    compatibility: tuple[str, ...] = (),
) -> VideoInfo:
    """Build a VideoInfo without touching a file. For planner tests."""
    return VideoInfo(
        path=Path(name),
        codec=codec,
        width=width,
        height=height,
        frame_count=frames,
        frame_rate=rate,
        duration_s=None if frames is None or rate is None else float(frames / rate),
        bit_depth=10,
        has_hdr10=hdr10,
        has_hdr10plus=hdr10plus,
        has_dv=dv,
        dv_profile=dv_profile,
        dv_level=6 if dv else None,
        dv_layers=dv_layers,
        dv_compatibility=compatibility,
        hdr_format_raw="",
        track={},
    )


def shot_curve(
    frames: int, *, seed: int = 0, mean_shot: int = 40, noise: float = 0.0
) -> np.ndarray:
    """A synthetic mean-luma curve: constant inside a shot, jumping at cuts.

    This is what real footage looks like to :func:`hibrit.align.luma_curve`, and
    reproducing that shape is the point — a smooth random walk would be far
    easier to align than the material actually is.
    """
    rng = np.random.default_rng(seed)
    curve = np.empty(frames, dtype=np.float64)
    position = 0
    while position < frames:
        length = max(4, int(rng.exponential(mean_shot)))
        end = min(frames, position + length)
        curve[position:end] = rng.uniform(10, 220)
        position = end
    if noise:
        curve = curve + rng.normal(0, noise, size=frames)
    return curve
