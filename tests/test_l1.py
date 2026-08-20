"""L1 curve reading, statistics and comparison.

The plotting itself is not tested: it draws a picture, and a test that asserts
a PNG was written proves nothing about whether the picture is right. What is
tested is everything the picture is drawn from.
"""

from __future__ import annotations

import numpy as np
import pytest

from hibrit.l1 import (
    AVERAGE_THRESHOLDS,
    L1Curve,
    compare,
    load_l1,
    nits_to_pq,
    pq_to_nits,
)


def _curve(maximum, average=None, minimum=None) -> L1Curve:
    maximum = np.asarray(maximum, dtype=float)
    return L1Curve(
        minimum=np.zeros_like(maximum) if minimum is None else np.asarray(minimum, float),
        maximum=maximum,
        average=maximum / 10 if average is None else np.asarray(average, float),
    )


def _write_csv(tmp_path, rows):
    path = tmp_path / "l1.csv"
    lines = ["frame,min_pq,max_pq,avg_pq"]
    lines += [f"{i},{mn},{mx},{av}" for i, (mn, mx, av) in enumerate(rows)]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# PQ
# --------------------------------------------------------------------------


def test_pq_round_trip():
    """The conversion is the basis of every number here; it has to be exact."""
    for nits in (0.0001, 1.0, 100.0, 523.7, 4000.0, 10000.0):
        assert pq_to_nits(nits_to_pq(nits)) == pytest.approx(nits, rel=1e-9)


def test_pq_is_monotonic():
    codes = np.arange(0, 4096, 37)
    values = pq_to_nits(codes)
    assert np.all(np.diff(values) > 0)


def test_pq_endpoints():
    assert pq_to_nits(0) == pytest.approx(0.0, abs=1e-9)
    assert pq_to_nits(4095) == pytest.approx(10000.0, rel=1e-6)


def test_pq_clamps_out_of_range_codes():
    """A code past the 12-bit range is a bug upstream, not a reason to crash."""
    assert pq_to_nits(9999) == pytest.approx(10000.0, rel=1e-6)
    assert pq_to_nits(-5) == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def test_load_l1_converts_to_nits(tmp_path):
    path = _write_csv(tmp_path, [(0, 2081, 1229), (0, 2170, 1229)])
    curve = load_l1(path)

    assert len(curve) == 2
    # 2081 is the nearest whole code to 100 nits; the rounding is worth about
    # a tenth of a nit and is a property of the format, not of this code.
    assert curve.maximum[0] == pytest.approx(100.0, abs=0.2)
    assert curve.maxcll == pytest.approx(124.11, abs=0.01)


def test_load_l1_rejects_an_empty_file(tmp_path):
    """An empty export means the RPU had nothing, and silently plotting
    nothing would look like a working run."""
    path = _write_csv(tmp_path, [])
    with pytest.raises(ValueError, match="no rows"):
        load_l1(path)


# --------------------------------------------------------------------------
# Distribution
# --------------------------------------------------------------------------


def test_distribution_counts_frames_over_each_threshold():
    curve = _curve([50, 150, 250, 450])
    table = dict(curve.distribution((100.0, 200.0, 400.0)))

    assert table[100.0] == pytest.approx(75.0)
    assert table[200.0] == pytest.approx(50.0)
    assert table[400.0] == pytest.approx(25.0)


def test_distribution_can_read_the_average_instead():
    curve = _curve([1000, 1000], average=[5, 500])
    peak = dict(curve.distribution((100.0,), of="maximum"))
    average = dict(curve.distribution((100.0,), of="average"))

    assert peak[100.0] == pytest.approx(100.0)
    assert average[100.0] == pytest.approx(50.0)


def test_distribution_is_strictly_above_the_threshold():
    """A frame sitting exactly on the threshold is not above it."""
    curve = _curve([100.0, 100.0])
    assert dict(curve.distribution((100.0,)))[100.0] == pytest.approx(0.0)


def test_average_thresholds_descend():
    """The table reads top to bottom; unsorted thresholds would render wrong."""
    assert list(AVERAGE_THRESHOLDS) == sorted(AVERAGE_THRESHOLDS, reverse=True)


# --------------------------------------------------------------------------
# Comparison -- the reason this module exists
# --------------------------------------------------------------------------


def test_identical_curves_compare_clean():
    curve = _curve([100, 200, 300])
    result = compare(curve, _curve([100, 200, 300]))

    assert result.identical
    assert result.compared == 3
    assert "identical" in result.describe()


def test_comparison_finds_which_frames_differ():
    a = _curve([100, 200, 300, 400])
    b = _curve([100, 999, 300, 400])
    result = compare(a, b)

    assert not result.identical
    assert result.differing.tolist() == [1]
    assert result.peak_delta == pytest.approx(799.0)


def test_comparison_reports_a_span_of_differing_frames():
    """The real case: two releases that share a master but differ at the
    opening, which is exactly where an aligner is most tempted to measure."""
    a = _curve([10, 10, 10, 500, 500, 500])
    b = _curve([99, 99, 99, 500, 500, 500])
    result = compare(a, b)

    assert result.differing.tolist() == [0, 1, 2]
    assert "frames 0-2" in result.describe()


def test_comparison_only_looks_at_the_overlap():
    """Different lengths are the normal case, not an error: the question is
    whether the frames the two releases share agree."""
    short = _curve([100, 200])
    long = _curve([100, 200, 300, 400, 500])
    result = compare(short, long)

    assert result.compared == 2
    assert result.identical


def test_comparison_ignores_float_noise_but_not_real_differences():
    a = _curve([100.0, 100.0])
    b = _curve([100.0 + 1e-9, 100.5])
    result = compare(a, b, tolerance=0.01)

    assert result.differing.tolist() == [1], "a 0.5 nit difference is real"


def test_comparison_notices_an_average_only_difference():
    """Peak can match frame for frame while the average does not -- measured on
    a real pair, where only the opening scene differed."""
    a = _curve([500, 500], average=[10, 90])
    b = _curve([500, 500], average=[10, 20])
    result = compare(a, b)

    assert result.peak_delta == pytest.approx(0.0)
    assert result.average_delta == pytest.approx(70.0)
    assert result.differing.tolist() == [1]


# --------------------------------------------------------------------------
# The message someone sees when they cannot draw
# --------------------------------------------------------------------------


def test_packaged_build_is_not_told_to_pip_install(monkeypatch):
    """The zip has no Python in it, so pip advice is a dead end.

    Comparing is the half of this command that matters, and it works without
    matplotlib, so the packaged build should say that instead.
    """
    import sys

    from hibrit.l1 import _no_matplotlib

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    message = _no_matplotlib()

    assert "drop the -o" in message
    assert "Comparing two files still works" in message


def test_source_install_is_told_how_to_get_matplotlib(monkeypatch):
    import sys

    from hibrit.l1 import _no_matplotlib

    monkeypatch.delattr(sys, "frozen", raising=False)
    assert "pip install" in _no_matplotlib()
