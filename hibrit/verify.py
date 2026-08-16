"""Check the result independently of the tools that produced it.

This module exists because of the question the whole project started from:
these programs exist, but do they actually do the thing? A player showing a
Dolby Vision badge proves that a flag is set. It does not prove the metadata is
the metadata that was extracted, and it does not prove the picture survived.

Two tiers, because they cost very different amounts:

* **Metadata checks** read the result's RPU and HDR10+ back out and compare them
  against what went in. Cheap: a pass over the output, no rewriting.
* **The pixel check** strips both layers off the result and compares the hash of
  what is left against the untouched target stream. If the hashes match, not one
  bit of picture data changed. This rewrites the stream, so on a 70 GB remux it
  costs a full extra copy and the time to read it twice — opt in deliberately.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from hibrit.hdr10plus import Hdr10PlusTool
from hibrit.matroska import extract_video
from hibrit.probe import probe
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


def sha256_file(path: Path, *, chunk: int = HASH_CHUNK) -> str:
    """Hash a file without loading it into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


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


def _compare_hdr10plus(
    result: Path, expected: Path, workdir: Path, toolbox: Toolbox
) -> list[Check]:
    tool = Hdr10PlusTool(toolbox)
    extracted = workdir / "verify_hdr10plus.json"
    tool.extract(result, extracted)

    # The same program wrote both files, so byte equality is the normal outcome;
    # a semantic comparison is the fallback in case a version changes its
    # formatting, since what matters is the per-frame values, not the whitespace.
    same_bytes = sha256_file(extracted) == sha256_file(expected)
    if same_bytes:
        detail = "the HDR10+ metadata read back out is byte-for-byte identical"
        passed = True
    else:
        got = json.loads(extracted.read_text(encoding="utf-8"))
        want = json.loads(Path(expected).read_text(encoding="utf-8"))
        passed = got.get("SceneInfo") == want.get("SceneInfo")
        detail = (
            "the per-frame HDR10+ values match, though the files are not byte-identical"
            if passed
            else "the HDR10+ metadata read back out differs from what was injected"
        )
    return [Check("hdr10+ round-trip", passed, detail)]


def _strip_all(source: Path, workdir: Path, tag: str, toolbox: Toolbox) -> Path:
    """Remove every layer of dynamic metadata, leaving the base bitstream.

    The result is unwrapped from its container first: both tools read Matroska
    but neither will rewrite it (``Remover: Matroska format unsupported``).
    """
    unwrapped = workdir / f"verify_{tag}_unwrapped.hevc"
    interim = workdir / f"verify_{tag}_no_hdr10plus.hevc"
    stripped = workdir / f"verify_{tag}_stripped.hevc"

    stream = extract_video(source, unwrapped, toolbox)
    Hdr10PlusTool(toolbox).remove(stream, interim)
    DoviTool(toolbox).remove(interim, stripped)

    interim.unlink(missing_ok=True)
    if stream == unwrapped:
        unwrapped.unlink(missing_ok=True)
    return stripped


def _compare_pixels(
    result: Path, clean_target: Path, workdir: Path, toolbox: Toolbox
) -> list[Check]:
    """Strip both files down to bare bitstream and compare what is left.

    *clean_target* is the target's video stream as it was before injection.

    Both sides go through the same strip, and that is not redundancy — it is
    what makes the comparison valid. Measured on a real clip: ``mkvextract``
    writes an Annex B stream of 117,524,013 bytes, while ``dovi_tool remove``
    writes the same picture as 117,526,056. Neither is wrong; they simply do not
    agree byte for byte on how to emit the same NAL units. Comparing a raw
    extraction against a tool's output would report a difference on a file where
    nothing changed, which is a check that fails when it should pass — the worst
    kind, because the natural response is to stop trusting the check.
    """
    stripped_result = _strip_all(result, workdir, "result", toolbox)
    stripped_target = _strip_all(clean_target, workdir, "target", toolbox)

    same = sha256_file(stripped_result) == sha256_file(stripped_target)
    stripped_result.unlink(missing_ok=True)
    stripped_target.unlink(missing_ok=True)
    return [
        Check(
            "picture untouched",
            same,
            (
                "with the metadata stripped off, the result and the original target "
                "are byte-identical; not one bit of picture data changed"
                if same
                else "the stripped result does not match the stripped target — "
                "something other than metadata was modified"
            ),
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
    the pixel check — pass the target's extracted ``.hevc`` from before
    injection, or leave it out to skip that tier.
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

    if clean_target_stream is not None:
        checks += _compare_pixels(Path(result), Path(clean_target_stream), workdir, box)
    else:
        checks.append(
            Check(
                "picture untouched",
                True,
                "not run — pass the target's pre-injection video stream to check it. "
                "It costs a full rewrite of the output.",
                skipped=True,
            )
        )

    return Report(checks=tuple(checks))
