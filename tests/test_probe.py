"""Reading MediaInfo's JSON.

The payloads below are the real fields MediaInfo 26.05 emits for four files on
the developer's disk, trimmed to what hibrit reads. Inventing them would have
hidden the case in ``test_dune_declares_no_st2086_but_still_has_hdr10``, which
is the only reason the mastering-display fallback exists.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from hibrit.probe import ProbeError, parse_mediainfo

COMMON = {
    "@type": "Video",
    "StreamOrder": "0",
    "FrameRate_Num": "24000",
    "FrameRate_Den": "1001",
    "FrameRate": "23.976",
    "BitDepth": "10",
}


def payload(**video) -> dict:
    """A MediaInfo payload with the fields every file has, plus *video*."""
    return {"media": {"track": [{"@type": "General"}, {**COMMON, **video}]}}


def payload_exactly(video: dict) -> dict:
    """A payload with only the fields given — for testing what happens without."""
    return {"media": {"track": [{"@type": "General"}, {"@type": "Video", **video}]}}


#: Barbarian (MA WEB-DL): single-layer profile 8.1 over an HDR10 base.
BARBARIAN = payload(
    Format="HEVC",
    Width="3840",
    Height="2160",
    FrameCount="147624",
    Duration="6157.151000000",
    HDR_Format="Dolby Vision / SMPTE ST 2086",
    HDR_Format_Profile="dvhe.08 / ",
    HDR_Format_Settings="BL+RPU / ",
    HDR_Format_Level="06 / ",
    HDR_Format_Compatibility="HDR10 / HDR10",
    MasteringDisplay_Luminance="min: 0.0001 cd/m2, max: 1000 cd/m2",
)

#: Dune Part One (hybrid remux): dual-layer profile 7 carrying HDR10+ as well.
DUNE = payload(
    Format="HEVC",
    Width="3840",
    Height="2160",
    FrameCount="223615",
    Duration="9326.609000000",
    HDR_Format="Dolby Vision / SMPTE ST 2094 App 4",
    HDR_Format_Profile="dvhe.07 / ",
    HDR_Format_Settings="BL+EL+RPU / ",
    HDR_Format_Level="06 / ",
    HDR_Format_Compatibility="Blu-ray / HDR10+ Profile B",
    MasteringDisplay_Luminance="min: 0.0050 cd/m2, max: 4000 cd/m2",
)

#: Casino: a plain HDR10 remux, the archetypal transfer target.
CASINO = payload(
    Format="HEVC",
    Width="3840",
    Height="2160",
    FrameCount="256391",
    Duration="10693.641000000",
    HDR_Format="SMPTE ST 2086",
    HDR_Format_Profile="",
    HDR_Format_Settings="",
    HDR_Format_Compatibility="HDR10",
    MasteringDisplay_Luminance="min: 0.0050 cd/m2, max: 1000 cd/m2",
)

#: 12 Angry Men: 1080p AVC, no HDR at all. Nothing can be injected into it.
TWELVE_ANGRY_MEN = payload(
    Format="AVC",
    Width="1920",
    Height="1080",
    FrameCount="138170",
    Duration="5762.841000000",
    BitDepth="8",
    HDR_Format="",
    HDR_Format_Profile="",
    HDR_Format_Compatibility="",
    MasteringDisplay_Luminance="",
)

HERE = Path("sample.mkv")


class TestDolbyVision:
    def test_profile_81_single_layer(self) -> None:
        info = parse_mediainfo(BARBARIAN, HERE)
        assert info.has_dv
        assert info.dv_profile == 8
        assert info.dv_layers == "BL+RPU"
        assert not info.is_dual_layer
        assert info.dv_level == 6
        assert "P8.1" in info.describe()

    def test_profile_7_dual_layer(self) -> None:
        info = parse_mediainfo(DUNE, HERE)
        assert info.dv_profile == 7
        assert info.dv_layers == "BL+EL+RPU"
        assert info.is_dual_layer

    def test_no_dv_leaves_the_profile_unset_rather_than_zero(self) -> None:
        info = parse_mediainfo(CASINO, HERE)
        assert not info.has_dv
        assert info.dv_profile is None
        assert info.dv_layers is None


class TestHdrDetection:
    def test_hdr10plus_is_read_from_the_format_string(self) -> None:
        info = parse_mediainfo(DUNE, HERE)
        assert info.has_hdr10plus
        assert set(info.carries()) == {"dolby_vision", "hdr10plus", "hdr10"}

    def test_dune_declares_no_st2086_but_still_has_hdr10(self) -> None:
        """The load-bearing fallback.

        Dune's HDR_Format lists only Dolby Vision and ST 2094 App 4, and its
        compatibility string says "Blu-ray / HDR10+ Profile B" — the substring
        "HDR10" appears, but ST 2086 never does. The static layer is only
        visible through MasteringDisplay_Luminance. Without that fallback the
        planner would warn that a profile 8.1 RPU has no HDR10 base to sit on,
        on a file that plainly has one.
        """
        video = DUNE["media"]["track"][1]
        assert "SMPTE ST 2086" not in video["HDR_Format"]
        assert parse_mediainfo(DUNE, HERE).has_hdr10

        without = payload_exactly({**video, "MasteringDisplay_Luminance": ""})
        assert not parse_mediainfo(without, HERE).has_hdr10

    def test_plain_hdr10(self) -> None:
        info = parse_mediainfo(CASINO, HERE)
        assert info.has_hdr10
        assert not info.has_dv
        assert not info.has_hdr10plus
        assert info.carries() == ("hdr10",)

    def test_sdr_says_so_rather_than_saying_nothing(self) -> None:
        info = parse_mediainfo(TWELVE_ANGRY_MEN, HERE)
        assert info.carries() == ()
        assert "no HDR metadata" in info.describe()


class TestBasics:
    def test_frame_rate_comes_from_the_exact_fraction(self) -> None:
        """23.976 is 24000/1001; rounding it would make offsets drift."""
        assert parse_mediainfo(DUNE, HERE).frame_rate == Fraction(24000, 1001)

    def test_frame_rate_falls_back_to_the_decimal(self) -> None:
        video = dict(DUNE["media"]["track"][1])
        video.pop("FrameRate_Num")
        video.pop("FrameRate_Den")
        rate = parse_mediainfo(payload_exactly(video), HERE).frame_rate
        assert rate is not None
        assert float(rate) == pytest.approx(23.976, abs=1e-3)

    def test_missing_frame_rate_is_none_not_a_guess(self) -> None:
        video = dict(CASINO["media"]["track"][1])
        for key in ("FrameRate_Num", "FrameRate_Den", "FrameRate"):
            video.pop(key, None)
        assert parse_mediainfo(payload_exactly(video), HERE).frame_rate is None

    def test_codec_and_resolution(self) -> None:
        info = parse_mediainfo(TWELVE_ANGRY_MEN, HERE)
        assert not info.is_hevc
        assert info.resolution == (1920, 1080)
        assert info.bit_depth == 8

    def test_frame_count_is_an_int(self) -> None:
        assert parse_mediainfo(CASINO, HERE).frame_count == 256391

    def test_missing_frame_count_is_none_rather_than_zero(self) -> None:
        """Zero would read as a real answer and sail through every comparison."""
        video = dict(CASINO["media"]["track"][1])
        video.pop("FrameCount")
        assert parse_mediainfo(payload_exactly(video), HERE).frame_count is None

    def test_a_file_with_no_video_track_raises(self) -> None:
        with pytest.raises(ProbeError, match="no video track"):
            parse_mediainfo({"media": {"track": [{"@type": "Audio"}]}}, HERE)

    def test_compatibility_is_split_on_slashes(self) -> None:
        assert parse_mediainfo(DUNE, HERE).dv_compatibility == ("Blu-ray", "HDR10+ Profile B")
