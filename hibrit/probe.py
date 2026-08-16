"""Read what HDR metadata a file actually carries.

MediaInfo is the primary source: it reads container and bitstream headers only,
so probing a 70 GB remux costs a second rather than a full stream parse. The
fields hibrit depends on are all present in its JSON output::

    HDR_Format               "Dolby Vision / SMPTE ST 2094 App 4"
    HDR_Format_Profile       "dvhe.08 / "
    HDR_Format_Settings      "BL+RPU / "        (BL+EL+RPU for dual layer)
    HDR_Format_Compatibility "HDR10 / HDR10+ Profile B"
    FrameCount               "1000"

``FrameCount`` matters more than the rest combined: it is the only number that
decides whether metadata can be transferred without realignment, and ffprobe
does not reliably provide it (it reports ``N/A`` on the remuxes tested).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from hibrit.tools import Toolbox

#: Substrings MediaInfo uses inside ``HDR_Format`` for each metadata kind.
_HDR10PLUS_MARKERS = ("SMPTE ST 2094 App 4",)
_HDR10_MARKERS = ("SMPTE ST 2086",)
_DV_MARKERS = ("Dolby Vision",)


class ProbeError(RuntimeError):
    """Raised when a file cannot be probed or carries no video track."""


@dataclass(frozen=True)
class VideoInfo:
    """Everything hibrit needs to know about one file's video track."""

    path: Path
    codec: str
    width: int
    height: int
    frame_count: int | None
    frame_rate: Fraction | None
    duration_s: float | None
    bit_depth: int | None

    has_hdr10: bool
    has_hdr10plus: bool
    has_dv: bool

    dv_profile: int | None
    dv_level: int | None
    dv_layers: str | None
    dv_compatibility: tuple[str, ...]

    hdr_format_raw: str
    track: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def is_hevc(self) -> bool:
        return self.codec.upper() == "HEVC"

    @property
    def is_dual_layer(self) -> bool:
        """True for profile 7 streams that carry a separate enhancement layer."""
        return bool(self.dv_layers and "EL" in self.dv_layers.upper())

    @property
    def resolution(self) -> tuple[int, int]:
        return (self.width, self.height)

    def carries(self) -> tuple[str, ...]:
        """Metadata kinds present, as stable identifiers."""
        kinds = []
        if self.has_dv:
            kinds.append("dolby_vision")
        if self.has_hdr10plus:
            kinds.append("hdr10plus")
        if self.has_hdr10:
            kinds.append("hdr10")
        return tuple(kinds)

    def describe(self) -> str:
        """One-line human summary, e.g. ``HEVC 3840x2160 · DV P8.1 · HDR10+``."""
        parts = [f"{self.codec} {self.width}x{self.height}"]
        if self.has_dv:
            profile = f"P{self.dv_profile}" if self.dv_profile else "P?"
            if self.dv_profile == 8 and self.dv_compatibility:
                if any(c == "HDR10" for c in self.dv_compatibility):
                    profile = "P8.1"
                elif any("HLG" in c for c in self.dv_compatibility):
                    profile = "P8.4"
            layers = f" {self.dv_layers}" if self.dv_layers else ""
            parts.append(f"DV {profile}{layers}")
        if self.has_hdr10plus:
            parts.append("HDR10+")
        if self.has_hdr10:
            parts.append("HDR10")
        if not (self.has_dv or self.has_hdr10plus or self.has_hdr10):
            parts.append("SDR / no HDR metadata")
        if self.frame_count is not None:
            parts.append(f"{self.frame_count} frames")
        return " · ".join(parts)


def _split_multi(value: str | None) -> list[str]:
    """MediaInfo joins per-format values with ' / '; split and drop blanks."""
    if not value:
        return []
    return [part.strip() for part in value.split("/") if part.strip()]


def _first_int(values: list[str]) -> int | None:
    for value in values:
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits:
            return int(digits)
    return None


def _parse_dv_profile(profile_field: str | None) -> int | None:
    """``dvhe.08`` -> 8, ``dvhe.05`` -> 5. Returns None when absent."""
    for part in _split_multi(profile_field):
        # Accept dvhe.NN, dvav.NN, hev1.NN and bare numbers.
        tail = part.split(".")[-1] if "." in part else part
        digits = "".join(ch for ch in tail if ch.isdigit())
        if digits:
            return int(digits)
    return None


#: The frame rates video actually ships at. A closed set, and every one of the
#: fractional ones is a whole rate divided by 1001.
STANDARD_RATES: tuple[Fraction, ...] = (
    Fraction(24000, 1001),  # 23.976
    Fraction(24),
    Fraction(25),
    Fraction(30000, 1001),  # 29.97
    Fraction(30),
    Fraction(48000, 1001),
    Fraction(48),
    Fraction(50),
    Fraction(60000, 1001),  # 59.94
    Fraction(60),
)

#: How close a decimal has to be to a standard rate to be treated as one.
#: 23.976 differs from 24000/1001 in the fifth decimal place; nothing else in
#: the list is anywhere near that gap.
RATE_TOLERANCE = Fraction(1, 1000)


def snap_to_standard_rate(rate: Fraction) -> Fraction:
    """Round a decimal frame rate to the exact fraction it is a rounding of.

    Older MediaInfo builds report only ``FrameRate`` as a decimal string, so
    "23.976" parses to 2997/125 — a number no video was ever shot at. The real
    rate is 24000/1001, which differs in the fifth decimal place.

    Measured: MediaInfo 21.03 omits FrameRate_Num/Den where 26.05 provides
    them, so the same file probes as 2997/125 on one machine and 24000/1001 on
    another. The error is small — under half a frame across a three-hour film,
    which the window-agreement tolerance absorbs — but seeking by timestamp is
    the one place hibrit converts frames to seconds, and being exactly right
    costs a lookup in a list of ten.
    """
    for standard in STANDARD_RATES:
        if abs(rate - standard) < RATE_TOLERANCE:
            return standard
    return rate


def _parse_frame_rate(track: dict[str, Any]) -> Fraction | None:
    """The video's frame rate, snapped to a standard one either way it arrives.

    An exact-looking pair is not the same as an exact rate. ``Alien.1979
    .Directors.Cut`` in this library reports ``FrameRate_Num 23976 /
    FrameRate_Den 1000`` — a rational, and a rounding: 23.976000 where the real
    rate is 24000/1001 = 23.976024. Trusting the pair because it is a pair was
    the mistake; it gets the same treatment the decimal does.
    """
    num = track.get("FrameRate_Num")
    den = track.get("FrameRate_Den")
    if num and den:
        try:
            return snap_to_standard_rate(Fraction(int(num), int(den)))
        except (ValueError, ZeroDivisionError):
            pass
    rate = track.get("FrameRate")
    if rate:
        try:
            return snap_to_standard_rate(Fraction(str(rate)).limit_denominator(100000))
        except (ValueError, ZeroDivisionError):
            return None
    return None


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_mediainfo(payload: dict[str, Any], path: Path) -> VideoInfo:
    """Build a :class:`VideoInfo` from MediaInfo's parsed JSON."""
    tracks = payload.get("media", {}).get("track", [])
    video = next((t for t in tracks if t.get("@type") == "Video"), None)
    if video is None:
        raise ProbeError(f"{path.name}: no video track")

    hdr_format = video.get("HDR_Format", "") or ""
    formats = _split_multi(hdr_format)

    has_dv = any(m in hdr_format for m in _DV_MARKERS)
    has_hdr10plus = any(m in hdr_format for m in _HDR10PLUS_MARKERS)
    # Static HDR10 is announced either as ST 2086 in HDR_Format or, when another
    # format is listed first, only by the presence of mastering display data.
    has_hdr10 = any(m in hdr_format for m in _HDR10_MARKERS) or bool(
        video.get("MasteringDisplay_Luminance")
    )

    layers = None
    settings = _split_multi(video.get("HDR_Format_Settings"))
    if settings:
        layers = settings[0]

    return VideoInfo(
        path=path,
        codec=str(video.get("Format", "") or ""),
        width=_as_int(video.get("Width")) or 0,
        height=_as_int(video.get("Height")) or 0,
        frame_count=_as_int(video.get("FrameCount")),
        frame_rate=_parse_frame_rate(video),
        duration_s=_as_float(video.get("Duration")),
        bit_depth=_as_int(video.get("BitDepth")),
        has_hdr10=has_hdr10,
        has_hdr10plus=has_hdr10plus,
        has_dv=has_dv,
        dv_profile=_parse_dv_profile(video.get("HDR_Format_Profile")) if has_dv else None,
        dv_level=_first_int(_split_multi(video.get("HDR_Format_Level"))) if has_dv else None,
        dv_layers=layers if has_dv else None,
        dv_compatibility=tuple(_split_multi(video.get("HDR_Format_Compatibility"))),
        hdr_format_raw=hdr_format or ", ".join(formats),
        track=video,
    )


def probe(path: str | Path, toolbox: Toolbox | None = None) -> VideoInfo:
    """Probe *path* with MediaInfo."""
    path = Path(path)
    if not path.exists():
        raise ProbeError(f"{path} does not exist")
    box = toolbox or Toolbox()
    proc = box.run("mediainfo", ["--Output=JSON", str(path)])
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - malformed tool output
        raise ProbeError(f"{path.name}: could not parse MediaInfo output: {exc}") from exc
    return parse_mediainfo(payload, path)
