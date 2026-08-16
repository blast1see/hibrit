"""Decide what can be transferred between two files, and refuse when it cannot.

The commands themselves are short. Knowing *which* commands to run, in which
order, and when to stop is the whole job, and it depends entirely on what the
two files turn out to contain.

Nothing here touches a file. A :class:`Plan` is a description that can be shown
to a human before a single byte is written — which is the point, because the
step this module is most useful for is the one it declines to take.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from hibrit.probe import VideoInfo

#: dovi_tool ``-m`` conversion modes, keyed by the source DV profile.
#: Mode 2 converts profile 7 to single-layer 8.1 and discards the enhancement
#: layer; mode 3 converts profile 5 to 8.1. Both are lossy in different ways,
#: which is what the accompanying warnings are for.
CONVERT_MODE_FOR_PROFILE = {5: 3, 7: 2}

#: Frame-count difference, as a fraction of the runtime, beyond which the two
#: files are not two releases of the same cut. Head trims and credit changes are
#: measured in seconds; this is measured in minutes.
GROSS_RUNTIME_GAP = 0.05


class Kind(str, Enum):
    """A kind of dynamic metadata that can be moved."""

    DV = "dolby-vision"
    HDR10PLUS = "hdr10plus"

    @property
    def label(self) -> str:
        return "Dolby Vision" if self is Kind.DV else "HDR10+"


class Level(str, Enum):
    """How much a note should stop the user."""

    BLOCKER = "blocker"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class Note:
    """One thing the user needs to know before agreeing to the plan."""

    level: Level
    text: str

    def describe(self) -> str:
        marker = {Level.BLOCKER: "STOP", Level.WARNING: "WARN", Level.INFO: "note"}
        return f"[{marker[self.level]}] {self.text}"


@dataclass(frozen=True)
class Step:
    """One command in the pipeline, with the reason it is there."""

    action: str
    summary: str
    reason: str

    def describe(self) -> str:
        return f"{self.summary}\n    why: {self.reason}"


@dataclass(frozen=True)
class Plan:
    """What will happen, what will be lost, and whether it can happen at all."""

    source: VideoInfo
    target: VideoInfo
    transfer: tuple[Kind, ...]
    steps: tuple[Step, ...]
    notes: tuple[Note, ...] = field(default_factory=tuple)
    convert_mode: int | None = None
    needs_alignment: bool = False

    @property
    def blockers(self) -> tuple[Note, ...]:
        return tuple(n for n in self.notes if n.level is Level.BLOCKER)

    @property
    def warnings(self) -> tuple[Note, ...]:
        return tuple(n for n in self.notes if n.level is Level.WARNING)

    @property
    def ok(self) -> bool:
        """True when the job can run. An empty transfer list is not a job."""
        return not self.blockers and bool(self.transfer)

    def describe(self) -> str:
        lines = [
            f"source: {self.source.name}",
            f"        {self.source.describe()}",
            f"target: {self.target.name}",
            f"        {self.target.describe()}",
            "",
        ]
        if self.transfer:
            moved = ", ".join(k.label for k in self.transfer)
            lines.append(f"transfer: {moved}")
        else:
            lines.append("transfer: nothing")
        lines.append("")

        for note in self.notes:
            lines.append(note.describe())
        if self.notes:
            lines.append("")

        if self.ok:
            lines.append("steps:")
            for index, step in enumerate(self.steps, start=1):
                lines.append(f"  {index}. {step.describe()}")
        else:
            lines.append("This job cannot run. See the entries marked STOP above.")
        return "\n".join(lines)


def _describe_frame_gap(source: VideoInfo, target: VideoInfo) -> str:
    a, b = source.frame_count, target.frame_count
    if a is None or b is None:
        return "frame count unknown"
    return f"source {a}, target {b}, difference {a - b:+d}"


def build_plan(
    source: VideoInfo,
    target: VideoInfo,
    *,
    kinds: tuple[Kind, ...] | None = None,
    replace_existing: bool = False,
) -> Plan:
    """Work out what to do with *source* (metadata donor) and *target* (video to keep).

    *kinds* limits what is considered; by default everything the source has and
    the target lacks is moved. *replace_existing* also moves metadata the target
    already carries, overwriting it.
    """
    notes: list[Note] = []
    wanted = kinds or (Kind.DV, Kind.HDR10PLUS)

    # --- what is even available -------------------------------------------------
    available: list[Kind] = []
    for kind in wanted:
        source_has = source.has_dv if kind is Kind.DV else source.has_hdr10plus
        target_has = target.has_dv if kind is Kind.DV else target.has_hdr10plus
        if not source_has:
            continue
        if target_has and not replace_existing:
            notes.append(
                Note(
                    Level.INFO,
                    f"target already carries {kind.label}; leaving it alone. "
                    "Pass --replace to overwrite it with the source's copy.",
                )
            )
            continue
        if target_has:
            notes.append(
                Note(
                    Level.WARNING,
                    f"target's existing {kind.label} metadata will be replaced by the "
                    "source's. The original is only recoverable from the untouched "
                    "target file, so keep it until you have checked the result.",
                )
            )
        available.append(kind)

    transfer = tuple(available)

    # --- reasons this cannot work ----------------------------------------------
    if not source.is_hevc:
        notes.append(
            Note(
                Level.BLOCKER,
                f"source video is {source.codec or 'not HEVC'}. Dolby Vision RPUs and "
                "HDR10+ SEI messages live inside the HEVC bitstream; there is nothing "
                "to extract from another codec.",
            )
        )
    if not target.is_hevc:
        notes.append(
            Note(
                Level.BLOCKER,
                f"target video is {target.codec or 'not HEVC'}. The metadata can only be "
                "injected into an HEVC bitstream, so an AVC remux cannot receive it.",
            )
        )
    if not transfer and not any(n.level is Level.BLOCKER for n in notes):
        notes.append(
            Note(
                Level.BLOCKER,
                "the source carries nothing the target is missing. Check that you have "
                "the two files the right way round: source donates metadata, target "
                "keeps its picture.",
            )
        )

    if source.resolution and target.resolution and source.resolution != target.resolution:
        notes.append(
            Note(
                Level.BLOCKER,
                f"resolution differs ({source.resolution} vs {target.resolution}). RPU "
                "level 5 active-area offsets are counted in pixels of the source frame, "
                "so the letterbox bars would be placed in the wrong rows.",
            )
        )

    if source.frame_rate and target.frame_rate and source.frame_rate != target.frame_rate:
        notes.append(
            Note(
                Level.BLOCKER,
                f"frame rate differs ({float(source.frame_rate):.3f} vs "
                f"{float(target.frame_rate):.3f} fps). Both kinds of metadata are one "
                "entry per frame; at different rates the entries do not correspond to "
                "the same moments and no offset can fix that.",
            )
        )

    # --- Dolby Vision profile handling ------------------------------------------
    convert_mode: int | None = None
    if Kind.DV in transfer:
        profile = source.dv_profile
        convert_mode = CONVERT_MODE_FOR_PROFILE.get(profile or 0)

        if profile == 5:
            notes.append(
                Note(
                    Level.WARNING,
                    "source is Dolby Vision profile 5, whose base layer is IPT-PQ-C2, "
                    "not the BT.2020 PQ picture in the target. Converting with -m 3 "
                    "keeps the trims but drops the colour mapping, and some players "
                    "reject a profile 5 RPU sitting on an HDR10 base outright. Check "
                    "the result on the device you actually watch on.",
                )
            )
        elif profile == 7:
            detail = (
                "the enhancement layer's luma and chroma mapping is discarded"
                if source.is_dual_layer
                else "there is no enhancement layer to lose here (MEL)"
            )
            notes.append(
                Note(
                    Level.INFO,
                    f"source is Dolby Vision profile 7; converting to single-layer 8.1 "
                    f"with -m 2, where {detail}.",
                )
            )
        elif profile == 8:
            notes.append(
                Note(Level.INFO, "source is Dolby Vision profile 8.1; no conversion needed.")
            )
        elif profile is not None:
            notes.append(
                Note(
                    Level.BLOCKER,
                    f"Dolby Vision profile {profile} is not handled. hibrit converts "
                    "profiles 5 and 7 to 8.1 and passes 8.1 through unchanged.",
                )
            )

        if not target.has_hdr10:
            notes.append(
                Note(
                    Level.WARNING,
                    "target has no HDR10 static metadata (SMPTE ST 2086). A profile 8.1 "
                    "RPU describes how to transform an HDR10 base, so a player falling "
                    "back to HDR10 has nothing to fall back to.",
                )
            )

    # --- alignment ---------------------------------------------------------------
    frames_differ = (
        source.frame_count is not None
        and target.frame_count is not None
        and source.frame_count != target.frame_count
    )
    if frames_differ:
        gap = abs((source.frame_count or 0) - (target.frame_count or 0))
        span = max(source.frame_count or 1, target.frame_count or 1)
        if gap > span * GROSS_RUNTIME_GAP:
            rate = float(target.frame_rate or 24)
            notes.append(
                Note(
                    Level.BLOCKER,
                    f"the two files differ by {gap} frames, about {gap / rate / 60:.0f} "
                    f"minutes ({gap / span:.0%} of the runtime). Trimmed logos and "
                    "credits account for seconds, not this. These are a different cut "
                    "or a different film, and no single offset can align them.",
                )
            )
        notes.append(
            Note(
                Level.WARNING,
                f"frame counts differ ({_describe_frame_gap(source, target)}). The "
                "metadata must be retimed before injection. dovi_tool will not stop "
                "you here: it pads the metadata to length and exits successfully, "
                "producing a file that plays and is wrong from end to end.",
            )
        )
    elif source.frame_count is None or target.frame_count is None:
        notes.append(
            Note(
                Level.WARNING,
                "frame count could not be read for one of the files, so the metadata "
                "cannot be checked for length. Measure the alignment before trusting "
                "the result.",
            )
        )

    notes.append(
        Note(
            Level.INFO,
            "this assumes both releases come from the same master. If they were graded "
            "separately, the RPU's measured brightness values describe the wrong "
            "picture, and every check below will still pass.",
        )
    )

    # --- the actual command sequence ---------------------------------------------
    steps: list[Step] = [
        Step(
            "extract-video",
            "mkvextract the target's video track to an Annex B .hevc stream",
            "Matroska stores HEVC with length prefixes; the metadata tools need the "
            "start-code form.",
        )
    ]

    if Kind.DV in transfer:
        if convert_mode:
            steps.append(
                Step(
                    "extract-rpu",
                    f"dovi_tool -m {convert_mode} extract-rpu from the source",
                    f"pulls the RPU out and converts it to profile 8.1 in one pass "
                    f"(-m {convert_mode}), because the target is a single-layer HDR10 "
                    "stream.",
                )
            )
        else:
            steps.append(
                Step(
                    "extract-rpu",
                    "dovi_tool extract-rpu from the source",
                    "pulls the per-frame RPU out of the source bitstream, untouched.",
                )
            )
    if Kind.HDR10PLUS in transfer:
        steps.append(
            Step(
                "extract-hdr10plus",
                "hdr10plus_tool extract from the source",
                "pulls the per-frame HDR10+ metadata out as JSON.",
            )
        )

    needs_alignment = frames_differ
    if needs_alignment:
        steps.append(
            Step(
                "align",
                "measure the frame offset between source and target, then retime "
                "the metadata with the editor",
                "the metadata is a per-frame list; without the offset every entry "
                "after the first is applied to the wrong picture.",
            )
        )

    if Kind.HDR10PLUS in transfer:
        steps.append(
            Step(
                "inject-hdr10plus",
                "hdr10plus_tool inject into the target stream",
                "writes the HDR10+ SEI messages back in, frame by frame.",
            )
        )
    if Kind.DV in transfer:
        steps.append(
            Step(
                "inject-rpu",
                "dovi_tool inject-rpu into the target stream",
                "writes the RPU NAL units back in. Order relative to HDR10+ was "
                "measured to make no difference: both orders produce byte-identical "
                "output.",
            )
        )

    steps.append(
        Step(
            "remux",
            "mkvmerge the new video track together with every other track, chapter "
            "and attachment from the target",
            "only the video track changed; audio, subtitles and chapters are copied "
            "across untouched.",
        )
    )
    steps.append(
        Step(
            "verify",
            "read the metadata back out of the result and compare it byte for byte, "
            "then strip both layers and compare the picture's hash",
            "proves the metadata landed intact and that not a single pixel moved. "
            "A player's Dolby Vision badge only proves a flag was set.",
        )
    )

    return Plan(
        source=source,
        target=target,
        transfer=transfer,
        steps=tuple(steps),
        notes=tuple(notes),
        convert_mode=convert_mode,
        needs_alignment=needs_alignment,
    )
