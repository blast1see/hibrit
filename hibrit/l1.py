"""Reading and plotting the L1 dynamic brightness an RPU carries.

L1 is what the Dolby Vision metadata claims about the picture: the minimum,
maximum and average light level, measured **per scene** rather than per frame.
``dovi_tool export --levels`` writes one row per frame, repeating the scene's
value across it, which is easy to misread -- comparing a single frame's
measured peak against "its" L1 value is meaningless; the scene's peak is the
thing L1 describes.

Two uses, and the second is the reason this module exists:

1. Draw the curve, the way the plots that circulate on forums do.
2. **Compare two releases.** If both come from the same master, their L1
   curves are identical frame for frame. Where they differ tells you where the
   releases differ -- which is the question alignment actually turns on, and
   answering it costs a few megabytes of RPU rather than a full decode.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np

#: Thresholds the forum plots quote, in nits. Two scales because the average
#: and the peak live in different ranges: an average above 400 nits is
#: essentially unheard of, a peak above 400 is ordinary.
AVERAGE_THRESHOLDS = (400.0, 200.0, 100.0, 92.0, 50.0, 25.0, 10.0, 2.5)
PEAK_THRESHOLDS = (4000.0, 2000.0, 1500.0, 1000.0, 800.0, 500.0, 200.0, 100.0)

_M1 = 2610 / 16384
_M2 = 2523 / 4096 * 128
_C1 = 3424 / 4096
_C2 = 2413 / 4096 * 32
_C3 = 2392 / 4096 * 32

#: RPU levels are 12-bit PQ codes.
PQ_MAX_CODE = 4095


def pq_to_nits(code):
    """12-bit PQ code -> cd/m². Vectorised."""
    signal = np.clip(np.asarray(code, dtype=float) / PQ_MAX_CODE, 0.0, 1.0)
    powered = np.power(signal, 1 / _M2)
    return 10000 * np.power(np.clip(powered - _C1, 0.0, None) / (_C2 - _C3 * powered), 1 / _M1)


def nits_to_pq(nits):
    """cd/m² -> 12-bit PQ code. The inverse of :func:`pq_to_nits`."""
    linear = np.clip(np.asarray(nits, dtype=float) / 10000.0, 0.0, 1.0)
    powered = np.power(linear, _M1)
    return np.power((_C1 + _C2 * powered) / (1 + _C3 * powered), _M2) * PQ_MAX_CODE


@dataclass(frozen=True)
class L1Curve:
    """Per-frame L1 levels, in nits."""

    minimum: np.ndarray
    maximum: np.ndarray
    average: np.ndarray

    def __len__(self) -> int:
        return len(self.maximum)

    @property
    def maxcll(self) -> float:
        """Highest peak the RPU measured anywhere in the file."""
        return float(self.maximum.max())

    @property
    def maxfall(self) -> float:
        """Highest scene average -- 'frame average light level' at its worst."""
        return float(self.average.max())

    def distribution(self, thresholds, *, of: str = "maximum") -> list[tuple[float, float]]:
        """Percentage of frames whose ``of`` level exceeds each threshold.

        This is the table the forum plots print down their right-hand side. It
        is a compact fingerprint of a grade: two releases cut from the same
        master produce the same numbers.
        """
        values = getattr(self, of)
        return [(t, 100.0 * float((values > t).mean())) for t in thresholds]


def load_l1(csv_path: Path) -> L1Curve:
    """Read a ``dovi_tool export --levels level1=...`` CSV into nits."""
    rows = {"min_pq": [], "max_pq": [], "avg_pq": []}
    with Path(csv_path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            for key in rows:
                rows[key].append(int(row[key]))
    if not rows["max_pq"]:
        raise ValueError(f"{csv_path} has no rows")
    return L1Curve(
        minimum=pq_to_nits(rows["min_pq"]),
        maximum=pq_to_nits(rows["max_pq"]),
        average=pq_to_nits(rows["avg_pq"]),
    )


@dataclass(frozen=True)
class Divergence:
    """Where two L1 curves stop agreeing."""

    compared: int
    differing: np.ndarray
    peak_delta: float
    average_delta: float

    @property
    def identical(self) -> bool:
        return len(self.differing) == 0

    def describe(self) -> str:
        if self.identical:
            return f"identical across all {self.compared} frames compared"
        first, last = int(self.differing.min()), int(self.differing.max())
        span = f"frame {first}" if first == last else f"frames {first}-{last}"
        return (
            f"{len(self.differing)} of {self.compared} frames differ ({span}); "
            f"peak by up to {self.peak_delta:.2f} nits, "
            f"average by up to {self.average_delta:.2f} nits"
        )


def compare(a: L1Curve, b: L1Curve, *, tolerance: float = 0.01) -> Divergence:
    """Frame-by-frame comparison of two L1 curves.

    Only the overlap is compared: releases of different lengths are the normal
    case, and the interesting question is whether the frames they share agree.

    ``tolerance`` is in nits, and guards against float noise from the PQ
    conversion rather than against real differences -- the codes themselves are
    integers, so a genuine difference is never this small.
    """
    n = min(len(a), len(b))
    peak = np.abs(a.maximum[:n] - b.maximum[:n])
    avg = np.abs(a.average[:n] - b.average[:n])
    differing = np.nonzero((peak > tolerance) | (avg > tolerance))[0]
    return Divergence(
        compared=n,
        differing=differing,
        peak_delta=float(peak.max()) if n else 0.0,
        average_delta=float(avg.max()) if n else 0.0,
    )


def _no_matplotlib() -> str:
    """What to tell someone whose build cannot draw.

    The packaged build has no Python to install into, so telling its user to
    pip install something is advice they cannot follow. The comparison is the
    half of this command that matters and it works either way, so say that
    rather than leaving them at a dead end.
    """
    import sys

    if getattr(sys, "frozen", False):
        return (
            "this packaged build cannot draw plots: matplotlib is not bundled, "
            "and doubling the download for it is not worth it. Comparing two "
            "files still works and needs no plot -- drop the -o. To draw, "
            "install from source: pip install 'hibrit[plot]'"
        )
    return (
        "plotting needs matplotlib, which hibrit does not install by default. "
        "Install it with:  pip install 'hibrit[plot]'"
    )


def render(
    curve: L1Curve,
    out: Path,
    *,
    title: str,
    subtitle: str = "",
    notes: tuple[str, ...] = (),
) -> Path:
    """Draw the curve as the plots that circulate on forums draw it.

    matplotlib is imported here rather than at module scope: it is an optional
    extra, and everything else in this module works without it.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.ticker import FixedFormatter, FixedLocator
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(_no_matplotlib()) from exc

    # A log axis cannot show zero, and the minimum curve sits at zero for most
    # of a film. Everything is clamped to the bottom of the axis instead.
    floor = 0.01
    ticks = [0.01, 0.1, 0.5, 1, 2.5, 5, 10, 25, 50, 100, 200, 400, 600, 1000, 2000, 4000, 10000]
    frames = np.arange(len(curve))

    fig = plt.figure(figsize=(20, 9.4), dpi=150)
    ax = fig.add_axes((0.045, 0.075, 0.90, 0.70))
    ax.set_yscale("log")
    ax.set_ylim(floor, 10000)
    ax.set_xlim(0, max(len(curve) - 1, 1))

    for values, fill, line, label in (
        (
            curve.maximum,
            "#c9d4f2",
            "#3f6fd1",
            f"Maximum (MaxCLL: {curve.maxcll:.2f} nits, avg: {curve.maximum.mean():.2f} nits)",
        ),
        (
            curve.average,
            "#7e5aa8",
            "#5b3f86",
            f"Average (MaxFALL: {curve.maxfall:.2f} nits, avg: {curve.average.mean():.2f} nits)",
        ),
    ):
        clamped = np.maximum(values, floor)
        ax.fill_between(frames, floor, clamped, step="post", color=fill)
        ax.step(frames, clamped, where="post", color=line, lw=0.55, label=label)
    ax.step(
        frames,
        np.maximum(curve.minimum, floor),
        where="post",
        color="#111",
        lw=0.8,
        label=f"Minimum (max: {curve.minimum.max():.6f} nits)",
    )

    ax.yaxis.set_major_locator(FixedLocator(ticks))
    ax.yaxis.set_major_formatter(FixedFormatter([f"{t:g}" for t in ticks]))
    ax.yaxis.set_minor_locator(FixedLocator([]))
    ax.set_ylabel("nits (cd/m²)", fontsize=9)
    ax.set_xlabel("frames", fontsize=9)
    ax.tick_params(labelsize=8)
    ax.grid(alpha=0.22, lw=0.5)
    ax.legend(loc="lower left", fontsize=8.5, framealpha=0.95, borderpad=0.6)

    # The same axis in the units the metadata is actually stored in, so a
    # reader comparing against dovi_tool's own output does not have to convert.
    codes = ax.twinx()
    codes.set_yscale("log")
    codes.set_ylim(floor, 10000)
    codes.yaxis.set_major_locator(FixedLocator(ticks))
    codes.yaxis.set_major_formatter(
        FixedFormatter([f"{nits_to_pq(t):.0f}" for t in ticks[:-1]] + ["12 bit"])
    )
    codes.yaxis.set_minor_locator(FixedLocator([]))
    codes.tick_params(labelsize=8)

    fig.text(0.045, 0.965, title, fontsize=9.5, va="top")
    if subtitle:
        fig.text(0.045, 0.941, subtitle, fontsize=9, va="top")
    for i, note in enumerate(notes):
        fig.text(0.545, 0.965 - i * 0.021, note, fontsize=9, va="top")
    fig.text(0.50, 0.965, "DoVi L1 PLOT", fontsize=10.5, ha="center", va="top")

    for column, (label, thresholds, which) in enumerate(
        (
            ("Average Above", AVERAGE_THRESHOLDS, "average"),
            ("Peak Above", PEAK_THRESHOLDS, "maximum"),
        )
    ):
        for i, (threshold, percent) in enumerate(curve.distribution(thresholds, of=which)):
            fig.text(
                0.775 + column * 0.13,
                0.965 - i * 0.0185,
                f"{label} {threshold:>6g}nits: {percent:.2f}%",
                fontsize=8.5,
                va="top",
            )

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor="white")
    plt.close(fig)
    return out
