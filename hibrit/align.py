"""Find the frame offset between two releases of the same title.

Two releases almost never share a frame count: studio logos differ, credits
are trimmed, cuts differ. Dynamic HDR metadata is a per-frame list, so
transferring it without knowing the offset produces a file that plays and is
wrong for its entire runtime.

The measurement is the video equivalent of what AudioSyncTool does with audio:
reduce both sources to a cheap one-dimensional signal, cross-correlate, and read
the lag.

Decoding 4K HEVC to 128x72 grayscale runs at roughly 200 frames per second.
Measured end to end on a 223,615-frame UHD remux at the default settings — two
windows of 11,180 frames plus a 2,236-frame search margin, decoded from both
files — the whole measurement took four and a half minutes. It is the slowest
thing hibrit does, and the only one a caller should expect to wait on, which is
why :func:`align` reports progress.

The signal is *not* mean luma. Mean luma barely moves inside a shot, so its
correlation surface is a broad plateau and the argmax wanders across it: on a
clip with a known offset of +137 it answered +135 with a confidence of 1.05,
which is to say no answer at all. The frame-accurate landmark in a film is the
**scene cut** — the one place the luma jumps. Correlating the clipped absolute
frame-to-frame difference instead moved the same measurement to +137 at
confidence 3.24. See :func:`cut_signal` for why the clip matters. This is the
same idea as GCC-PHAT whitening in the audio case: flatten the parts both
signals share, keep the parts that mark a position in time.

Two rules carried over from that work, both learned the expensive way:

* **A correlation always returns a peak.** A number alone is not evidence. The
  result carries a confidence ratio and a verdict, and the verdict can be
  ``no_match``.
* **One window can lie.** The offset is measured in two places in the film.
  If they disagree, the releases differ structurally and no single offset can
  align them, so the answer is a refusal rather than an average.

One limitation follows from the clipping, and it is better stated than
discovered: because clipping flattens how *large* each cut is, what remains is
mostly *when* the cuts happen. Footage whose shots all run the same length gives
a near-periodic signal that matches itself almost as well one shot over as it
does at the true offset — measured at confidence 1.1 on a test clip cut at a
metronomic 9 frames. Real films have irregular shot rhythm and that irregularity
is the fingerprint; material that does not (a slideshow, a rigidly cut montage)
will be refused rather than mismeasured, which is the right failure but is still
a failure.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction

import numpy as np

from hibrit.probe import VideoInfo
from hibrit.tools import Toolbox

#: Downscaled frame size used for the luma signal. Small enough to be cheap,
#: large enough that two encodes of the same frame still look alike.
SAMPLE_WIDTH = 128
SAMPLE_HEIGHT = 72

#: Peak-to-runner-up ratio below which the match is not trusted.
CONFIDENCE_SUSPECT = 1.6
CONFIDENCE_RELIABLE = 2.5

#: Percentile at which the cut signal is clipped. The tallest cuts are rare and
#: not shared between two encodes; letting them dominate is what produced a
#: 87-frame error in testing.
CLIP_PERCENTILE = 99.0

#: Two windows may disagree by this many frames and still count as agreeing;
#: seeking into the middle of a stream is not always frame-exact.
WINDOW_AGREEMENT_FRAMES = 2


class Verdict(str, Enum):
    """How much the measurement can be trusted."""

    RELIABLE = "reliable"
    SUSPECT = "suspect"
    NO_MATCH = "no_match"


class AlignError(RuntimeError):
    """Raised when the luma signal could not be produced."""


@dataclass(frozen=True)
class WindowResult:
    """One correlation measurement."""

    start_frame: int
    frames: int
    offset: int
    confidence: float
    peak_score: float


@dataclass(frozen=True)
class Alignment:
    """The decision, not just the number."""

    offset: int | None
    verdict: Verdict
    confidence: float
    windows: tuple[WindowResult, ...]
    reason: str

    @property
    def usable(self) -> bool:
        return self.verdict is Verdict.RELIABLE and self.offset is not None

    def describe(self) -> str:
        """One line, with the verdict where it will be read.

        A rejected measurement still has a number attached, and leading with
        that number is how it gets used anyway — the reader sees "offset -115"
        and the refusal after it. So an untrusted result leads with the verdict
        and demotes the figure to a candidate.
        """
        if self.offset is None:
            return f"{self.verdict.value}: {self.reason}"

        sign = "+" if self.offset >= 0 else ""
        windows = f"{len(self.windows)} window" + ("s" if len(self.windows) != 1 else "")
        if self.verdict is Verdict.RELIABLE:
            return (
                f"offset {sign}{self.offset} frames · confidence {self.confidence:.2f} "
                f"· {windows} · reliable"
            )
        return (
            f"{self.verdict.value} — best candidate was {sign}{self.offset} frames "
            f"at confidence {self.confidence:.2f} ({windows})"
        )


def window_plan(
    frames_a: int,
    frames_b: int,
    *,
    max_shift: int | None = None,
    window_frames: int | None = None,
    windows: int = 2,
) -> tuple[int, int, list[tuple[int, int]]]:
    """Choose search range, window size and window start frames.

    Every threshold scales with the material. A fixed window that suits a
    three-hour remux is longer than a test clip, and a fixed one that suits a
    clip is noise on a feature.
    """
    shortest = max(1, min(frames_a, frames_b))

    if max_shift is None:
        max_shift = int(min(2500, max(120, shortest * 0.01)))
    max_shift = max(1, min(max_shift, shortest // 3 or 1))

    if window_frames is None:
        window_frames = int(min(12000, max(1000, max_shift * 5)))
    window_frames = max(1, min(window_frames, shortest))

    # Window starts must leave room for the window itself and for the shift.
    usable = max(0, shortest - window_frames - max_shift)
    starts: list[tuple[int, int]] = [(0, window_frames)]
    if windows > 1 and usable > window_frames:
        step = usable // windows
        for index in range(1, windows):
            start = min(usable, step * index)
            if start > 0 and all(abs(start - s) > window_frames // 2 for s, _ in starts):
                starts.append((start, window_frames))
    return max_shift, window_frames, starts


def luma_curve(
    info: VideoInfo,
    toolbox: Toolbox,
    *,
    start_frame: int = 0,
    frames: int | None = None,
    width: int = SAMPLE_WIDTH,
    height: int = SAMPLE_HEIGHT,
) -> np.ndarray:
    """Mean luma per frame, as a 1-D float array.

    Seeking is done by timestamp because ffmpeg has no frame-index seek; the
    frame rate from :class:`~hibrit.probe.VideoInfo` converts between the two.
    """
    exe = toolbox.require("ffmpeg")
    args: list[str] = ["-hide_banner", "-loglevel", "error", "-nostdin"]

    rate: Fraction = info.frame_rate or Fraction(24000, 1001)
    if start_frame > 0:
        args += ["-ss", f"{float(start_frame / rate):.6f}"]
    args += ["-i", str(info.path), "-map", "0:v:0"]
    if frames is not None:
        args += ["-frames:v", str(frames)]
    args += [
        "-vf",
        f"scale={width}:{height}:flags=bilinear,format=gray",
        "-f",
        "rawvideo",
        "-",
    ]

    proc = subprocess.run([str(exe), *args], capture_output=True, check=False)
    frame_bytes = width * height
    if not proc.stdout or len(proc.stdout) < frame_bytes:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise AlignError(f"no frames decoded from {info.name}: {stderr or 'empty output'}")

    usable = len(proc.stdout) - (len(proc.stdout) % frame_bytes)
    pixels = np.frombuffer(proc.stdout[:usable], dtype=np.uint8)
    return pixels.reshape(-1, frame_bytes).mean(axis=1).astype(np.float64)


def _normalise(signal: np.ndarray) -> np.ndarray:
    signal = np.asarray(signal, dtype=np.float64)
    centred = signal - signal.mean()
    spread = centred.std()
    return centred / spread if spread > 0 else centred


def cut_signal(curve: np.ndarray, *, clip_percentile: float = CLIP_PERCENTILE) -> np.ndarray:
    """Turn a luma curve into a scene-cut curve, one sample shorter.

    Two steps, both measured on a clip with a known offset of +137:

    * **Difference.** Mean luma is nearly constant within a shot, so
      correlating it directly gave +135 at confidence 1.05. Differencing keeps
      only the moments the picture changes.
    * **Clip.** Raw ``|diff|`` was worse, not better: +50, off by 87 frames.
      A handful of hard cuts tower over everything else, and two encodes
      disagree on exactly how tall those spikes are, so the correlation ends up
      matching a few outliers rather than the pattern of cuts. Flattening
      everything above the 99th percentile turns the signal into "a cut
      happened here", which both encodes agree on: +137 at confidence 3.24.

    The lag is unaffected by the differencing. If ``b[i] == a[i + offset]`` then
    ``diff(b)[i] == diff(a)[i + offset]`` as well, so the offset read off the
    shortened signals is the offset between the originals.
    """
    curve = np.asarray(curve, dtype=np.float64)
    if curve.size < 2:
        return np.zeros(0, dtype=np.float64)
    diff = np.abs(np.diff(curve))
    ceiling = float(np.percentile(diff, clip_percentile))
    return np.clip(diff, 0.0, ceiling) if ceiling > 0 else diff


def correlate(
    a: np.ndarray, b: np.ndarray, max_shift: int, *, shape: bool = True
) -> tuple[int, float, float]:
    """Cross-correlate two luma curves.

    Returns ``(offset, confidence, peak_score)`` where *offset* is how far *b*
    sits behind *a*: frame ``i`` of *b* corresponds to frame ``i + offset`` of
    *a*. A positive offset means *a* has extra frames at the head.

    Both inputs are passed through :func:`cut_signal` first unless *shape* is
    false, which is only useful for comparing signal choices.

    *confidence* is the ratio of the winning peak to the best competing peak
    elsewhere in the search range. A flat correlation surface scores near 1
    however large its peak is, which is exactly the case a bare maximum cannot
    distinguish from a real match.
    """
    a_signal = cut_signal(a) if shape else np.asarray(a, dtype=np.float64)
    b_signal = cut_signal(b) if shape else np.asarray(b, dtype=np.float64)
    if a_signal.size == 0 or b_signal.size == 0:
        raise AlignError("empty luma curve")

    a_norm = _normalise(a_signal)
    b_norm = _normalise(b_signal)

    max_shift = max(1, min(max_shift, min(a_norm.size, b_norm.size) - 1))
    size = 1 << int(np.ceil(np.log2(a_norm.size + b_norm.size)))
    spectrum = np.fft.rfft(a_norm, size) * np.conj(np.fft.rfft(b_norm, size))
    raw = np.fft.irfft(spectrum, size)

    lags = np.arange(-max_shift, max_shift + 1)
    overlap = np.maximum(min(a_norm.size, b_norm.size) - np.abs(lags), 1)
    scores = raw[lags] / overlap

    best_index = int(np.argmax(scores))
    best_score = float(scores[best_index])
    offset = int(lags[best_index])

    # Ignore the shoulder of the winning peak when looking for a runner-up:
    # adjacent lags are correlated by construction, not competition. A cut is
    # only a few frames wide but its shoulder is broad, and a narrow exclusion
    # zone measures the peak against its own slope — on the +137 clip, ±6
    # frames scored 1.78 where ±25 scored 3.24 for the very same peak.
    exclusion = min(max(25, max_shift // 10), max(3, max_shift // 2))
    mask = np.abs(lags - offset) > exclusion
    runner_up = float(np.max(np.abs(scores[mask]))) if mask.any() else 0.0
    confidence = best_score / runner_up if runner_up > 1e-9 else float("inf")
    return offset, confidence, best_score


def align(
    source: VideoInfo,
    target: VideoInfo,
    toolbox: Toolbox,
    *,
    windows: int = 2,
    max_shift: int | None = None,
    window_frames: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> Alignment:
    """Measure how far *source* sits from *target*, and decide whether to trust it.

    A positive offset means *source* has that many extra frames at the head, so
    its metadata must have that many frames removed to line up with *target*.

    *progress* is called before each window. On a feature this runs for minutes,
    and a command that prints nothing for four of them looks like one that has
    hung.
    """

    def say(message: str) -> None:
        if progress is not None:
            progress(message)

    frames_a = source.frame_count or 0
    frames_b = target.frame_count or 0
    if not frames_a or not frames_b:
        return Alignment(
            offset=None,
            verdict=Verdict.NO_MATCH,
            confidence=0.0,
            windows=(),
            reason="frame count unknown for one of the files; cannot align",
        )

    shift, _window_size, starts = window_plan(
        frames_a, frames_b, max_shift=max_shift, window_frames=window_frames, windows=windows
    )

    results: list[WindowResult] = []
    for index, (start, length) in enumerate(starts, start=1):
        say(
            f"window {index} of {len(starts)}: decoding {length + shift} frames from "
            f"frame {start} of each file"
        )
        curve_a = luma_curve(source, toolbox, start_frame=start, frames=length + shift)
        curve_b = luma_curve(target, toolbox, start_frame=start, frames=length + shift)
        offset, confidence, peak = correlate(curve_a, curve_b, shift)
        say(f"window {index}: offset {offset:+d}, confidence {confidence:.2f}")
        results.append(
            WindowResult(
                start_frame=start,
                frames=min(curve_a.size, curve_b.size),
                offset=offset,
                confidence=confidence,
                peak_score=peak,
            )
        )

    best = min(results, key=lambda r: r.confidence)

    # A peak sitting on the wall of the search range is not a peak, it is the
    # edge of what was looked at. Reporting it as an answer is how a search that
    # was too narrow turns into a confident wrong number.
    at_edge = next((r for r in results if abs(r.offset) >= shift), None)
    if at_edge is not None:
        return Alignment(
            offset=None,
            verdict=Verdict.NO_MATCH,
            confidence=best.confidence,
            windows=tuple(results),
            reason=(
                f"the best offset ({at_edge.offset:+d}) is at the edge of the searched "
                f"range (±{shift} frames), so the real offset is probably larger. "
                "Re-run with a wider max_shift."
            ),
        )

    # A window that could not find a peak has not disagreed with anything — it
    # has failed to measure. Rolling its argmax into the agreement check turns
    # "one place in the film was too flat to read" into "these are different
    # cuts", which sends the user to fix something that is not broken.
    measured = [r for r in results if r.confidence >= CONFIDENCE_SUSPECT]
    unmeasured = [r for r in results if r.confidence < CONFIDENCE_SUSPECT]

    if not measured:
        detail = ", ".join(f"frame {r.start_frame}: {r.confidence:.2f}" for r in results)
        return Alignment(
            offset=None,
            verdict=Verdict.NO_MATCH,
            confidence=best.confidence,
            windows=tuple(results),
            reason=(
                f"no window found a peak distinguishable from the noise floor "
                f"(confidence by window — {detail}; need {CONFIDENCE_SUSPECT}). "
                "These are probably not the same content."
            ),
        )

    if unmeasured:
        found = ", ".join(f"frame {r.start_frame}: {r.offset:+d}" for r in measured)
        failed = ", ".join(f"frame {r.start_frame}: {r.confidence:.2f}" for r in unmeasured)
        return Alignment(
            offset=measured[0].offset,
            verdict=Verdict.NO_MATCH,
            confidence=best.confidence,
            windows=tuple(results),
            reason=(
                f"{len(unmeasured)} of {len(results)} windows could not measure an "
                f"offset ({failed}), so there is nothing to confirm the one that did "
                f"({found}) against. Try a longer window, or a different part of the "
                "film: a stretch with few scene cuts gives the correlation nothing to "
                "lock onto."
            ),
        )

    measured_offsets = {r.offset for r in measured}
    spread = max(measured_offsets) - min(measured_offsets)
    if len(measured) > 1 and spread > WINDOW_AGREEMENT_FRAMES:
        detail = ", ".join(f"frame {r.start_frame}: {r.offset:+d}" for r in measured)
        return Alignment(
            offset=None,
            verdict=Verdict.NO_MATCH,
            confidence=best.confidence,
            windows=tuple(results),
            reason=(
                f"windows disagree ({detail}). The releases differ structurally — "
                "different cut, different frame rate, or missing scenes — and no "
                "single offset can align them."
            ),
        )

    # Every window measured something and they agree, so the answer is the one
    # they agree on; how much to trust it is the weakest window's business.
    offset = measured[0].offset
    if best.confidence < CONFIDENCE_RELIABLE:
        return Alignment(
            offset=offset,
            verdict=Verdict.SUSPECT,
            confidence=best.confidence,
            windows=tuple(results),
            reason=(
                f"peak is weak (confidence {best.confidence:.2f}). Check the offset "
                "against a known scene before using it."
            ),
        )
    return Alignment(
        offset=offset,
        verdict=Verdict.RELIABLE,
        confidence=best.confidence,
        windows=tuple(results),
        reason=(
            f"{len(results)} windows agree on {offset:+d} frames"
            if len(results) != 1
            else f"one window, measuring {offset:+d} frames"
        ),
    )


def edit_config_for_offset(offset: int, source_frames: int, target_frames: int) -> dict:
    """Build a dovi_tool / hdr10plus_tool editor config that retimes metadata.

    *offset* is the alignment offset: positive means the source starts earlier
    (has extra head frames) and those metadata frames must be dropped;
    negative means frames must be prepended.

    The tail is trimmed or padded afterwards so the result matches
    *target_frames* exactly. Padding repeats the nearest real frame, which is
    what dovi_tool would have done implicitly — the difference is that here it
    is a deliberate, reported edit rather than a warning nobody reads.
    """
    config: dict = {}
    remove: list[str] = []
    duplicate: list[dict[str, int]] = []

    remaining = source_frames
    if offset > 0:
        remove.append(f"0-{offset - 1}")
        remaining -= offset
    elif offset < 0:
        duplicate.append({"source": 0, "offset": 0, "length": -offset})
        remaining += -offset

    if remaining > target_frames:
        surplus = remaining - target_frames
        remove.append(f"{remaining - surplus}-{remaining - 1}")
    elif remaining < target_frames:
        shortfall = target_frames - remaining
        duplicate.append({"source": remaining - 1, "offset": remaining, "length": shortfall})

    if remove:
        config["remove"] = remove
    if duplicate:
        config["duplicate"] = duplicate
    return config
