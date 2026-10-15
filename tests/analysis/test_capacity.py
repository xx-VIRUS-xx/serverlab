"""Capacity estimators. Synthetic inputs are test data, not measurements."""

import numpy as np
import pytest

from serverlab.analysis.capacity import (
    fit_utilization_law,
    pool_capacity,
    saturated_throughputs,
)
from serverlab.analysis.pool_report import SweepCapacity, c_pool, grade_size, regime_for


def _line(
    avail: int, waiting: int, ok: int, dt: float = 1.0, reset: bool = False
) -> dict[str, object]:
    return {"pool_available": avail, "requests_waiting": waiting, "delta_ok": ok,
            "interval_s_actual": dt, "counter_reset": reset}


def test_fit_recovers_exact_line_and_ceiling() -> None:
    pts = [(r, 200_000 + 500 * r) for r in (100, 200, 400, 600, 800)]
    fit = fit_utilization_law(pts, 1_000_000, seed=1, resamples=500)
    assert fit.fixed_us_per_s == pytest.approx(200_000)
    assert fit.marginal_us_per_req == pytest.approx(500)
    assert fit.ceiling == pytest.approx(1600) and fit.r_squared == pytest.approx(1.0)
    assert fit.lo <= fit.ceiling <= fit.hi


def test_noisy_fit_interval_contains_truth_and_is_seeded() -> None:
    rng = np.random.default_rng(0)
    xs = np.linspace(100, 900, 30)
    pts = [(float(x), float(150_000 + 600 * x + rng.normal(0, 20_000))) for x in xs]
    a = fit_utilization_law(pts, 1_000_000, seed=3, resamples=2000)
    assert a == fit_utilization_law(pts, 1_000_000, seed=3, resamples=2000)
    assert a.lo < (1_000_000 - 150_000) / 600 < a.hi


def test_fit_refuses_degenerate_input() -> None:
    with pytest.raises(ValueError, match="at least"):
        fit_utilization_law([(1, 1)] * 3, 1e6, seed=1)
    with pytest.raises(ValueError, match="same request rate"):
        fit_utilization_law([(100.0, float(v)) for v in range(5)], 1e6, seed=1)
    with pytest.raises(ValueError, match="non-positive"):
        fit_utilization_law([(float(r), 1000.0 - r) for r in range(100, 600, 100)], 1e6, seed=1)


def test_saturated_intervals_only() -> None:
    lines = [
        _line(5, 0, 90),                 # pool not exhausted
        _line(0, 0, 100),                # exhausted but nobody waiting
        _line(0, 12, 200),               # saturated
        _line(0, 40, 400, dt=2.0),       # saturated, longer interval -> 200/s
        _line(0, 40, 999, reset=True),   # counter reset: ignored
    ]
    assert saturated_throughputs(lines) == [200.0, 200.0]
    assert pool_capacity(lines) == (200.0, 2)
    assert pool_capacity([_line(5, 0, 90)]) == (None, 0)


def _sweep(c: float | None, sat: bool = True, cpu: float | None = None) -> SweepCapacity:
    return SweepCapacity(
        repetition=1, c_meas=c, saturated_intervals=5 if c else 0, p99_knee_estimate=None,
        pool_ever_saturated=sat, saturated_steps_sut_cpu_pct=cpu,
    )


def test_regimes_follow_the_committed_interval() -> None:
    assert [regime_for(n, 1413, 1741) for n in (5, 10, 20, 40, 80)] == [
        "pool-limited", "pool-limited", "pool-limited", "pool-limited", "ambiguous"]
    assert regime_for(120, 1413, 1741) == "api-limited" and c_pool(5) == 100.0


def test_pool_limited_pass_and_fail_at_fifteen_percent() -> None:
    ok = grade_size(10, [_sweep(190), _sweep(205), _sweep(210)], 1413, 1741)
    assert ok.passed is True and ok.c_meas_median == 205
    bad = grade_size(10, [_sweep(150), _sweep(155), _sweep(160)], 1413, 1741)
    assert bad.passed is False and bad.error_pct is not None and bad.error_pct < -15
    none = grade_size(10, [_sweep(None, sat=False)], 1413, 1741)
    assert none.passed is False and "never saturated" in none.note


def test_ambiguous_is_not_graded_and_api_limited_needs_cpu_evidence() -> None:
    assert grade_size(80, [_sweep(1500)], 1413, 1741).passed is None
    good = grade_size(120, [_sweep(None, sat=False, cpu=96.0)], 1413, 1741)
    assert good.regime == "api-limited" and good.passed is True
    assert grade_size(120, [_sweep(1400, sat=True, cpu=96.0)], 1413, 1741).passed is False
    assert grade_size(120, [_sweep(None, sat=False, cpu=50.0)], 1413, 1741).passed is False
