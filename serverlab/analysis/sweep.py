"""Coarse-then-bracket sweep driver (docs/phases/week3.md s3). Pure: the system under test is
injected as `measure(rate) -> StepMeasurement`, so this module never touches k6 or Docker.

Algorithm:

1. Coarse: `coarse_points` log-spaced integer rates from `rate_lo` to `rate_hi` (de-duplicated,
   ascending), measured once each in a seeded random order.
2. Classify everything with `knee.py`. If the knee is not `found` (above/below range, no
   baseline) the search stops there.
3. Bisect: while fewer than `max_bisect` extra steps were measured and the bracket is wider than
   `width_target_pct`, measure the geometric midpoint, re-analyse ALL measurements, and narrow
   the bracket from the new knee. Stops at integer resolution or when the knee stops being found.

Nothing is dropped or re-measured: every measurement stays in `SweepResult.steps` with its status.
"""

import math
import random
from collections.abc import Callable

from pydantic import BaseModel

from serverlab.analysis.knee import (
    KneeResult,
    Step,
    StepMeasurement,
    baseline_p99,
    classify_all,
    find_knee,
)


class SweepResult(BaseModel):
    steps: list[Step]  # every measured step, sorted by rate, with its status
    knee: KneeResult
    coarse_order: list[int]  # order in which the coarse rates were measured
    bisect_rates: list[int]  # extra rates measured by bisection, in order
    seed: int


def coarse_rates(rate_lo: int, rate_hi: int, coarse_points: int) -> list[int]:
    """Log-spaced integer rates from rate_lo to rate_hi inclusive, de-duplicated, ascending."""
    if rate_lo < 1:
        raise ValueError(f"rate_lo must be >= 1, got {rate_lo}")
    if rate_hi <= rate_lo:
        raise ValueError(f"rate_hi must be > rate_lo, got {rate_hi} <= {rate_lo}")
    if coarse_points < 3:
        raise ValueError(f"coarse_points must be >= 3, got {coarse_points}")
    ratio = rate_hi / rate_lo
    rates = {
        round(rate_lo * ratio ** (i / (coarse_points - 1))) for i in range(coarse_points)
    }
    return sorted(rates)


def _analyse(measurements: list[StepMeasurement]) -> tuple[list[Step], KneeResult]:
    steps = classify_all(measurements)
    return steps, find_knee(steps, baseline_p99(measurements))


def run_sweep(
    measure: Callable[[int], StepMeasurement],
    rate_lo: int,
    rate_hi: int,
    seed: int,
    coarse_points: int = 8,
    max_bisect: int = 6,
    width_target_pct: float = 5.0,
) -> SweepResult:
    coarse = coarse_rates(rate_lo, rate_hi, coarse_points)
    rng = random.Random(seed)
    keyed = sorted((rng.random(), rate) for rate in coarse)
    coarse_order = [rate for _, rate in keyed]

    measurements: list[StepMeasurement] = [measure(rate) for rate in coarse_order]
    steps, knee = _analyse(measurements)

    bisect_rates: list[int] = []
    while len(bisect_rates) < max_bisect:
        if knee.status != "found" or knee.lo is None or knee.hi is None or knee.width_pct is None:
            break
        if knee.width_pct <= width_target_pct:
            break
        mid = round(math.sqrt(knee.lo * knee.hi))
        if mid <= knee.lo or mid >= knee.hi:  # integer resolution reached
            break
        if any(m.rate == mid for m in measurements):  # never re-measure a rate
            break
        measurements.append(measure(mid))
        bisect_rates.append(mid)
        steps, knee = _analyse(measurements)

    return SweepResult(
        steps=steps,
        knee=knee,
        coarse_order=coarse_order,
        bisect_rates=bisect_rates,
        seed=seed,
    )
