"""Check the result independently of the tools that produced it.

This module exists because of the question the whole project started from:
these programs exist, but do they actually do the thing? A player showing a
Dolby Vision badge proves that a flag is set. It does not prove the metadata is
the metadata that was extracted, and it does not prove the picture survived.

Two kinds of check:

* **Metadata checks** read the result's RPU and HDR10+ back out and compare them
  against what went in.
* **The picture check** hashes the coded picture data of the result and of the
  original target and compares the two. If they match, not one bit of picture
  data changed. It needs the target's pre-injection video stream, which the
  pipeline keeps for exactly this purpose.

The metadata checks cost a pass over the output and write only the few megabytes
they read back. The picture check is not free: the result is a Matroska file and
:func:`picture_digest` needs Annex B, so its video track is extracted first —
another full copy of the stream, about 68 GB for a 70 GB output.

That could be avoided only by verifying something other than the finished file,
which would not be verification. It is budgeted for instead: the peak during
this check matches the peak during the run, so
:data:`hibrit.pipeline.SPACE_FACTOR` covers both.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hibrit.hdr10plus import Hdr10PlusTool
from hibrit.matroska import extract_video, is_wrapped
from hibrit.probe import VideoInfo, probe
from hibrit.rpu import DoviTool
from hibrit.tools import Toolbox

#: Read size for hashing. Large enough that a 70 GB file is not death by syscall.
HASH_CHUNK = 8 * 1024 * 1024


@dataclass(frozen=True)
class Check:
    """One verdict, with the evidence that produced it."""

    name: str
    passed: bool
    detail: str
    skipped: bool = False

    def describe(self) -> str:
        mark = "SKIP" if self.skipped else ("PASS" if self.passed else "FAIL")
        return f"[{mark}] {self.name}: {self.detail}"


@dataclass(frozen=True)
class Report:
    """Everything that was checked, including what was not."""

    checks: tuple[Check, ...]

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.passed and not c.skipped)

    @property
    def passed(self) -> bool:
        """False if anything failed. A skipped check is not a passed check, but
        it is not a failure either — it is reported as unmeasured."""
        return not self.failures

    @property
    def unmeasured(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.skipped)

    def describe(self) -> str:
        lines = [c.describe() for c in self.checks]
        lines.append("")
        if self.failures:
            lines.append(f"{len(self.failures)} check(s) failed. Do not keep this output.")
        elif self.unmeasured:
            names = ", ".join(c.name for c in self.unmeasured)
            lines.append(f"All measured checks passed. Not measured: {names}.")
        else:
            lines.append("All checks passed.")
        return "\n".join(lines)


#: HEVC NAL unit types 0 to 31 are VCL: they carry coded picture data. Everything
#: from 32 up is parameter sets, SEI messages, access unit delimiters and the
#: rest of the scaffolding — including where Dolby Vision and HDR10+ live.
VCL_NAL_MAX = 31


def sha256_file(path: Path, *, chunk: int = HASH_CHUNK) -> str:
    """Hash a file without loading it into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def _nal_units(handle, chunk: int) -> Iterator[bytes]:
    """Yield Annex B NAL unit payloads from an open binary stream.

    Streamed rather than loaded: these files are the size of the films they came
    from. Trailing zero bytes are dropped from each unit because they belong to
    the next four-byte start code, not to this unit.
    """
    buffer = b""
    start: int | None = None
    at_eof = False

    while not at_eof:
        block = handle.read(chunk)
        if block:
            buffer += block
        else:
            at_eof = True

        search = 0
        while True:
            found = buffer.find(b"\x00\x00\x01", search)
            if found < 0:
                break
            if start is not None:
                yield buffer[start:found].rstrip(b"\x00")
            start = found + 3
            search = start

        if start is None:
            # No start code yet; keep only enough to catch one straddling the
            # chunk boundary.
            buffer = buffer[-2:]
        else:
            buffer = buffer[start - 3 :]
            start = 3

    if start is not None and len(buffer) > start:
        yield buffer[start:].rstrip(b"\x00")


def picture_digest(path: Path, *, chunk: int = HASH_CHUNK) -> tuple[str, int]:
    """Hash only the coded picture data of an **Annex B** stream.

    Returns ``(digest, vcl_nal_count)``.

    Hashing the whole file cannot answer "did the picture change", because
    injection legitimately rewrites the scaffolding around it. Measured on a
    synthetic clip: ``dovi_tool inject-rpu`` adds a seven-byte access unit
    delimiter to every frame that lacks one — 1680 bytes across 240 frames — and
    ``dovi_tool remove`` does not take them back out. Blu-ray remuxes already
    carry AUDs, so the same comparison passes there and fails on a clip from
    ffmpeg, which is the worst way for a check to be wrong.

    Restricting the hash to VCL units answers the question exactly and costs one
    read instead of a rewrite, which is why this check runs by default. Parsing
    costs about 1.7 times a bare SHA-256 of the same bytes — 800 MB/s against
    1330 on a warm cache — which is still several times faster than the disk
    those bytes arrive from, so the check is bound by reading, not by counting.

    A container is refused rather than parsed. Matroska stores HEVC
    length-prefixed, not with start codes, so scanning one for ``00 00 01``
    finds byte patterns that happen to occur inside compressed data — 303,436 of
    them in a 72 GB remux, measured — and returns a confident hash of nothing in
    particular. Unwrap it with :func:`hibrit.matroska.extract_video` first.
    """
    path = Path(path)
    if is_wrapped(path):
        raise ValueError(
            f"{path.name} is a container, not an Annex B stream. Matroska stores HEVC "
            "length-prefixed, so scanning it for start codes would hash coincidental "
            "byte patterns. Extract the video track first (hibrit.matroska.extract_video)."
        )

    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as handle:
        for unit in _nal_units(handle, chunk):
            if not unit:
                continue
            if ((unit[0] >> 1) & 0x3F) <= VCL_NAL_MAX:
                digest.update(unit)
                count += 1
    return digest.hexdigest(), count


def _compare_rpu(result: Path, expected: Path, workdir: Path, toolbox: Toolbox) -> list[Check]:
    dovi = DoviTool(toolbox)
    extracted = workdir / "verify_rpu.bin"
    dovi.extract_rpu(result, extracted)

    same = sha256_file(extracted) == sha256_file(expected)
    checks = [
        Check(
            "dolby vision round-trip",
            same,
            (
                "the RPU read back out of the result is byte-for-byte the RPU that was injected"
                if same
                else "the RPU read back out differs from the one that was injected"
            ),
        )
    ]

    info = dovi.info(extracted)
    result_info = probe(result, toolbox)
    frames = result_info.frame_count
    if frames is None:
        checks.append(
            Check(
                "dolby vision frame count",
                True,
                "video frame count unknown, so the RPU length could not be compared",
                skipped=True,
            )
        )
    else:
        matches = info.frames == frames
        checks.append(
            Check(
                "dolby vision frame count",
                matches,
                f"RPU has {info.frames} frames, video has {frames}"
                + ("" if matches else " — the metadata is padded or short"),
            )
        )
    return checks


#: Per-frame HDR10+ keys that hdr10plus_tool re-derives when it reads metadata
#: back out of a bitstream, rather than carrying through what was written.
#: Measured: inject 240 frames all belonging to scene 0, extract, and the tool
#: reports 240 scenes — it decides where a scene begins by noticing that the
#: luminance values changed. That bookkeeping is a description of the payload,
#: not part of it, so comparing it would report a difference on metadata that
#: transferred perfectly.
DERIVED_HDR10PLUS_KEYS = frozenset({"SceneId", "SceneFrameIndex"})


def _hdr10plus_payload(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every per-frame entry with the derived bookkeeping removed."""
    return [_payload_of(scene) for scene in scenes]


def _payload_of(scene: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in scene.items() if k not in DERIVED_HDR10PLUS_KEYS}


def _payloads_match(got: list[dict[str, Any]], want: list[dict[str, Any]]) -> int | None:
    """Index of the first entry that differs, or ``None`` if they all match.

    Compared one entry at a time rather than by building two filtered lists.
    A feature has an entry per frame — 223,615 of them on the remux this was
    developed against — and materialising two more copies of that to compare
    them once is a gigabyte spent to answer a question that can be answered as
    it goes, and abandoned at the first difference.
    """
    # strict: the caller compares lengths first, and this catches one that did
    # not — silently stopping at the shorter list would call a truncated
    # metadata stream a match.
    for index, (left, right) in enumerate(zip(got, want, strict=True)):
        if _payload_of(left) != _payload_of(right):
            return index
    return None


def _compare_hdr10plus(
    result: Path, expected: Path, workdir: Path, toolbox: Toolbox
) -> list[Check]:
    tool = Hdr10PlusTool(toolbox)
    extracted = workdir / "verify_hdr10plus.json"
    tool.extract(result, extracted)

    # The byte comparison is the path that actually fires. Measured on a
    # 209,389-frame remux, retiming included: the JSON read back out was
    # byte-for-byte the JSON that went in, all 209,430,721 of them.
    #
    # The fallback below is not dead code, but it is insurance rather than the
    # normal case, and it is worth knowing which is which. hdr10plus_tool
    # re-derives SceneId and SceneFrameIndex from the payload when it reads a
    # stream: hand-author a JSON that says all 240 frames are scene 0, inject
    # it, read it back, and the tool reports 240 scenes. Anything that came out
    # of the tool in the first place -- including anything its own editor
    # retimed -- already carries the numbering it will derive again, so it
    # round-trips exactly. Only a JSON written by something else needs the
    # comparison further down.
    if sha256_file(extracted) == sha256_file(expected):
        return [
            Check(
                "hdr10+ round-trip",
                True,
                "the HDR10+ metadata read back out is byte-for-byte identical",
            )
        ]

    got = json.loads(extracted.read_text(encoding="utf-8"))
    want = json.loads(Path(expected).read_text(encoding="utf-8"))
    got_scenes = got.get("SceneInfo") or []
    want_scenes = want.get("SceneInfo") or []

    if len(got_scenes) != len(want_scenes):
        return [
            Check(
                "hdr10+ round-trip",
                False,
                f"the result carries {len(got_scenes)} frames of HDR10+ metadata, "
                f"{len(want_scenes)} were injected",
            )
        ]

    differs_at = _payloads_match(got_scenes, want_scenes)
    passed = differs_at is None
    return [
        Check(
            "hdr10+ round-trip",
            passed,
            (
                "every per-frame HDR10+ value matches; only the scene numbering "
                "differs, which hdr10plus_tool recomputes on extraction"
                if passed
                else f"the HDR10+ values read back out differ from what was injected, "
                f"first at frame {differs_at}"
            ),
        )
    ]


def _compare_pixels(
    result: Path, clean_target: Path, workdir: Path, toolbox: Toolbox
) -> list[Check]:
    """Compare the coded picture data of the result against the original target.

    *clean_target* is the target's video stream as it was before injection.
    Both sides are reduced to a hash over their VCL NAL units only, so every
    metadata layer — and every piece of scaffolding either tool decided to add
    or reorder — is excluded by construction. See :func:`picture_digest`.
    """
    unwrapped = workdir / "verify_result.hevc"
    stream = extract_video(result, unwrapped, toolbox)

    result_digest, result_frames = picture_digest(stream)
    target_digest, target_frames = picture_digest(Path(clean_target))

    if stream == unwrapped:
        unwrapped.unlink(missing_ok=True)

    checks = [
        Check(
            "picture untouched",
            result_digest == target_digest,
            (
                f"every coded picture unit is identical to the original target "
                f"({result_frames} units); not one bit of picture data changed"
                if result_digest == target_digest
                else "the coded picture data differs from the original target — "
                "something other than metadata was modified"
            ),
        )
    ]
    if result_frames != target_frames:
        checks.append(
            Check(
                "picture unit count",
                False,
                f"the result has {result_frames} coded picture units, the target had "
                f"{target_frames}",
            )
        )
    return checks


#: Fields MediaInfo reads out of the Dolby Vision configuration record that
#: mkvmerge writes into the Matroska track header. A player looks at that
#: record, not at the RPU units, to decide whether to engage Dolby Vision at
#: all — so a stream can carry perfect metadata and still play as plain HDR10
#: if the record is missing.
DV_CONFIGURATION_FIELDS = ("HDR_Format_Profile", "HDR_Format_Level", "HDR_Format_Settings")


def _check_container_signalling(after: VideoInfo) -> list[Check]:
    """Did the container advertise the Dolby Vision, not merely contain it?

    Everything else here inspects the bitstream. This is the one thing a player
    reads first: mkvmerge derives a configuration record from the injected
    stream and writes it into the track header, and its absence is invisible to
    every other check in this module.
    """
    if not after.has_dv:
        return []
    missing = [field for field in DV_CONFIGURATION_FIELDS if not after.track.get(field)]
    if missing:
        return [
            Check(
                "container signalling",
                False,
                "the Dolby Vision configuration record is incomplete — "
                f"{', '.join(missing)} absent. Players read that record to decide "
                "whether to engage Dolby Vision, so this would play as plain HDR10.",
            )
        ]
    profile = (after.track.get("HDR_Format_Profile") or "").split("/")[0].strip()
    level = (after.track.get("HDR_Format_Level") or "").split("/")[0].strip()
    layers = (after.track.get("HDR_Format_Settings") or "").split("/")[0].strip()
    return [
        Check(
            "container signalling",
            True,
            f"the track header advertises {profile}, level {level}, {layers} — "
            "which is what a player reads before it looks at the stream",
        )
    ]


def verify(
    result: Path,
    *,
    target: Path | None = None,
    rpu: Path | None = None,
    hdr10plus: Path | None = None,
    clean_target_stream: Path | None = None,
    workdir: Path,
    toolbox: Toolbox | None = None,
) -> Report:
    """Check *result* against the inputs it was built from.

    *rpu* and *hdr10plus* are the metadata files that were injected; each is
    compared against what can be read back out. *clean_target_stream* enables
    the picture check — pass the target's extracted ``.hevc`` from before
    injection, which :func:`hibrit.pipeline.run` keeps by default.
    """
    box = toolbox or Toolbox()
    workdir = Path(workdir)
    checks: list[Check] = []

    if rpu is not None:
        checks += _compare_rpu(Path(result), Path(rpu), workdir, box)
    if hdr10plus is not None:
        checks += _compare_hdr10plus(Path(result), Path(hdr10plus), workdir, box)

    if target is not None:
        before = probe(Path(target), box)
        after = probe(Path(result), box)
        if before.frame_count is None or after.frame_count is None:
            checks.append(
                Check(
                    "frame count preserved",
                    True,
                    "frame count unknown for one of the files",
                    skipped=True,
                )
            )
        else:
            same = before.frame_count == after.frame_count
            checks.append(
                Check(
                    "frame count preserved",
                    same,
                    f"target {before.frame_count} frames, result {after.frame_count}",
                )
            )
        checks.append(
            Check(
                "declared hdr format",
                bool(after.has_dv or after.has_hdr10plus),
                f"mediainfo reports: {after.describe()}",
            )
        )
        checks += _check_container_signalling(after)

    if clean_target_stream is not None:
        checks += _compare_pixels(Path(result), Path(clean_target_stream), workdir, box)
    else:
        checks.append(
            Check(
                "picture untouched",
                True,
                "not run — pass the target's pre-injection video stream to check it",
                skipped=True,
            )
        )

    return Report(checks=tuple(checks))
