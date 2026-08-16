"""Getting HEVC in and out of Matroska.

Both metadata tools read a ``.mkv`` happily when *extracting* — and neither will
write to one. ``hdr10plus_tool remove`` on a Matroska file answers::

    Error: Remover: Matroska format unsupported

So anything that rewrites the bitstream has to unwrap it first. Matroska stores
HEVC length-prefixed; ``mkvextract tracks`` writes the Annex B start-code form
the tools parse.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from hibrit.probe import VideoInfo, probe
from hibrit.tools import Toolbox

#: Container suffixes that must be unwrapped before a tool can rewrite them.
WRAPPED_SUFFIXES = {".mkv", ".mka", ".mks", ".webm", ".mp4", ".m4v", ".mov"}


class ContainerError(RuntimeError):
    """Raised when a track could not be extracted or a file could not be built."""


def is_wrapped(path: Path) -> bool:
    """True when *path* is a container rather than a raw bitstream."""
    return Path(path).suffix.lower() in WRAPPED_SUFFIXES


def video_track_id(info: VideoInfo) -> int:
    """The track number mkvextract wants.

    MediaInfo's ``StreamOrder`` is the container's own 0-based numbering, which
    is what mkvmerge and mkvextract call the track ID. Its ``ID`` field is
    1-based for Matroska and would be off by one.
    """
    try:
        return int(info.track.get("StreamOrder"))
    except (TypeError, ValueError):
        return 0


def extract_video(
    source: Path | VideoInfo,
    out: Path,
    toolbox: Toolbox | None = None,
    *,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Write *source*'s video track to *out* as an Annex B HEVC stream.

    A raw bitstream is returned as-is rather than copied; there is nothing to
    unwrap and the files involved are the size of films.
    """
    box = toolbox or Toolbox()
    info = source if isinstance(source, VideoInfo) else probe(Path(source), box)
    if not is_wrapped(info.path):
        return info.path

    box.run(
        "mkvextract",
        [str(info.path), "tracks", f"{video_track_id(info)}:{out}"],
        on_output=progress,
    )
    if not out.exists() or out.stat().st_size == 0:
        raise ContainerError(f"mkvextract produced no video stream from {info.name}")
    return out


def video_track_properties(source: Path, toolbox: Toolbox | None = None) -> list[str]:
    """mkvmerge arguments that restore a video track's own labelling.

    The new video track is built from a raw Annex B stream, which carries no
    name, no language and no flags; ``--no-video`` on the donor deliberately
    drops its video track and takes that labelling with it. So a remux whose
    video track was called "Blu-ray Remux" comes out with an unnamed one —
    measured, not supposed.

    Read through ``mkvmerge -J`` rather than MediaInfo because these values are
    going straight back to mkvmerge, and it is the one that has to accept them:
    MediaInfo normalises a language to "tr" where mkvmerge wants "tur".

    Only what the target actually had is passed on. Supplying a default for
    something it did not set would be inventing metadata, which is the one
    thing this program is careful never to do.
    """
    box = toolbox or Toolbox()
    proc = box.run("mkvmerge", ["-J", str(source)], check=False)
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return []

    track = next((t for t in payload.get("tracks", []) if t.get("type") == "video"), None)
    if track is None:
        return []

    props = track.get("properties", {})
    args: list[str] = []
    if props.get("track_name"):
        args += ["--track-name", f"0:{props['track_name']}"]
    language = props.get("language_ietf") or props.get("language")
    if language and language != "und":
        args += ["--language", f"0:{language}"]
    if props.get("default_track") is False:
        args += ["--default-track-flag", "0:no"]
    if props.get("forced_track") is True:
        args += ["--forced-display-flag", "0:yes"]
    return args


def stale_label_warning(name: str | None, result: VideoInfo) -> str | None:
    """Say so when a preserved track name no longer describes the track.

    Release groups write the metadata into the video track's name. A real remux
    on this machine carries::

        MPEG-H HEVC Video / 59681 kbps / 2160p / ... / HDR10+ Profile B /
        Dolby Vision MEL @ 69 kbps

    Convert that to single-layer 8.1, or add HDR10+ to something that had none,
    and the label is now wrong. Both available answers are bad on their own:
    editing someone's label is not this program's business, and leaving a
    description that has quietly become false is worse than dropping it.

    So it is preserved and reported. The file is correct either way — this is
    not a failure, it is something to go and fix in the name if you care.
    """
    if not name:
        return None

    lowered = name.lower()
    problems: list[str] = []

    if ("mel" in lowered or "fel" in lowered) and result.dv_profile == 8:
        problems.append("it mentions an enhancement layer, and this is now single-layer 8.1")
    for profile in (5, 7):
        if f"profile {profile}" in lowered and result.dv_profile not in (None, profile):
            problems.append(
                f"it says profile {profile}, and this is now profile {result.dv_profile}"
            )
    if "hdr10+" in lowered and not result.has_hdr10plus:
        problems.append("it mentions HDR10+, which this no longer carries")
    if "hdr10+" not in lowered and result.has_hdr10plus:
        problems.append("this now carries HDR10+, which the name does not mention")

    if not problems:
        return None
    return (
        "the video track's name was kept as the target had it, but "
        + "; ".join(problems)
        + ". The file is right; the label is not."
    )


def remux(
    video: Path,
    donor: VideoInfo,
    out: Path,
    toolbox: Toolbox | None = None,
    *,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Combine a new video stream with every other track from *donor*.

    ``--no-video`` on the second input keeps the donor's audio, subtitles,
    chapters and attachments and drops only its old video — and, with it, the
    video track's own name, language and flags. Those are read back off the
    donor and reapplied, or a labelled track comes out unlabelled. See
    :func:`video_track_properties`.

    mkvmerge exits 1 for warnings that are usually harmless (a track it had to
    reorder, a timestamp it adjusted) and 2 for real errors, so the exit code is
    read rather than assumed to be zero.
    """
    box = toolbox or Toolbox()
    labelling = video_track_properties(donor.path, box)
    proc = box.run(
        "mkvmerge",
        ["-o", str(out), *labelling, str(video), "--no-video", str(donor.path)],
        check=False,
        on_output=progress,
    )
    if proc.returncode >= 2:
        out.unlink(missing_ok=True)
        raise ContainerError(f"mkvmerge failed:\n{proc.stdout}\n{proc.stderr}".strip())
    if not out.exists() or out.stat().st_size == 0:
        raise ContainerError(f"mkvmerge produced no output at {out}")
    return out
