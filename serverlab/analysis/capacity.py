"""Resource-based capacity estimators (docs/predictions.md, Week 4 amendment 2).

1. Utilization law for the API ceiling: least-squares fit of SUT CPU time per second against
   completed requests per second, ceiling = (CPU capacity - fixed overhead) / marginal cost.
   Interval: percentile bootstrap over steps, seeded.
2. Pool capacity from saturated 1-second intervals of the app metrics (pool exhausted and
   requests waiting): the median of delta_ok / interval_s_actual.

Both average over many requests and do not depend on a p99, which swap stalls on a loaded
host can inflate at random steps. Pure functions plus loaders for a results directory.
"""

import json
import statistics
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel

CPU_US_PER_S_PER_CPU = 1_000_000.0
DEFAULT_RESAMPLES = 10_000
MIN_FIT_POINTS = 5


class UtilizationFit(BaseModel):
    n: int
    fixed_us_per_s: float
    marginal_us_per_req: float
    r_squared: float
    cpu_capacity_us_per_s: float
    ceiling: float
    lo: float
    hi: float
    resamples: int
    seed: int


def _fit(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    slope, intercept = np.polyfit(x, y, 1)
    return float(intercept), float(slope)


def fit_utilization_law(
    points: Sequence[tuple[float, float]],
    cpu_capacity_us_per_s: float,
    seed: int,
    resamples: int = DEFAULT_RESAMPLES,
) -> UtilizationFit:
    """`points` = (completed requests per second, SUT CPU us per second), one per valid step."""
    if len(points) < MIN_FIT_POINTS:
        raise ValueError(f"need at least {MIN_FIT_POINTS} points, got {len(points)}")
    x = np.array([p[0] for p in points], dtype=np.float64)
    y = np.array([p[1] for p in points], dtype=np.float64)
    if float(np.ptp(x)) == 0.0:
        raise ValueError("all points have the same request rate: slope undefined")
    fixed, marginal = _fit(x, y)
    if marginal <= 0:
        raise ValueError(f"non-positive marginal cost {marginal:.1f} us/request: no CPU trend")
    pred = fixed + marginal * x
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    rng = np.random.default_rng(seed)
    ceilings: list[float] = []
    for _ in range(resamples):
        idx = rng.integers(0, len(x), len(x))
        if float(np.ptp(x[idx])) == 0.0:
            continue
        b0, b1 = _fit(x[idx], y[idx])
        if b1 > 0:
            ceilings.append((cpu_capacity_us_per_s - b0) / b1)
    if len(ceilings) < resamples // 2:
        raise ValueError("bootstrap produced too few valid resamples")
    lo, hi = (float(v) for v in np.quantile(ceilings, [0.025, 0.975]))
    return UtilizationFit(
        n=len(x), fixed_us_per_s=fixed, marginal_us_per_req=marginal, r_squared=r2,
        cpu_capacity_us_per_s=cpu_capacity_us_per_s,
        ceiling=(cpu_capacity_us_per_s - fixed) / marginal, lo=lo, hi=hi,
        resamples=len(ceilings), seed=seed,
    )


def saturated_throughputs(app_lines: Iterable[Mapping[str, Any]]) -> list[float]:
    """Per-interval completed req/s over intervals where the pool was exhausted AND requests
    were waiting (no counter reset, delta present)."""
    out: list[float] = []
    for line in app_lines:
        if line.get("counter_reset") or line.get("delta_ok") is None:
            continue
        if line.get("pool_available") != 0 or not (line.get("requests_waiting") or 0) > 0:
            continue
        dt = line.get("interval_s_actual")
        if not dt or dt <= 0:
            continue
        out.append(float(line["delta_ok"]) / float(dt))
    return out


def pool_capacity(app_lines: Iterable[Mapping[str, Any]]) -> tuple[float | None, int]:
    """(median saturated throughput, number of saturated intervals); None if there were none."""
    vals = saturated_throughputs(app_lines)
    return (statistics.median(vals) if vals else None), len(vals)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
