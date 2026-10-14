"""Knee finder: curve analysis over a rate sweep (plan.md s3.9). Pure, no I/O.

Definitions (fixed in docs/phases/week3.md s3 before any data):

* A step is **good** if it is `ok`, achieved/offered >= 0.95 and P99 <= 2.0 x the baseline P99
  (the P99 of the lowest-rate `ok` step).
* A step is **bad** if it is `saturated`, or `ok` but not good.
* `invalid-other` steps are excluded from the curve and listed in the result.
* The knee bracket is [highest good step below the first bad step, first bad step]. The point
  estimate is the geometric midpoint of the bracket; its width is reported, never hidden.

Saturation vs a starved load generator (docs/phases/week3.md s2): an invalid run counts as
`saturated` only if the invalidity is purely dropped iterations / maxVUs, k6 had CPU to spare
(< 80% of its cpuset) AND the SUT's P99 inflated beyond the same 2.0x baseline factor. Anything
else invalid is `invalid-other`: excluded and reported.
"""

import math
from typing import Literal

from pydantic import BaseModel, Field

ACHIEVED_RATIO_MIN = 0.95
P99_INFLATION_MAX = 2.0
K6_CPU_MAX_PCT = 80.0
SATURATION_MARKERS = ("dropped iterations", "max_vus")  # reason substrings from load/k6.py

Status = Literal["ok", "saturated", "invalid-other"]
KneeStatus = Literal["found", "above_range", "below_range", "no_baseline"]


class StepMeasurement(BaseModel):
    """What one sweep step measured (raw; classification happens against the baseline)."""

    rate: float = Field(gt=0, description="offered arrival rate, req/s")
    achieved: float | None = Field(description="completed req/s over the measured window")
    p99_ms: float | None
    valid: bool
    invalid_reasons: list[str] = Field(default_factory=list)
    k6_cpu_pct: float | None = Field(
        default=None, description="k6 container CPU as % of its cpuset capacity"
    )


class Step(BaseModel):
    measurement: StepMeasurement
    status: Status
    note: str = ""

    @property
    def rate(self) -> float:
        return self.measurement.rate


class KneeResult(BaseModel):
    status: KneeStatus
    lo: float | None = None  # highest good rate below the first bad one
    hi: float | None = None  # first bad rate
    estimate: float | None = None  # geometric midpoint of [lo, hi]
    width_pct: float | None = None  # (hi / lo - 1) * 100
    baseline_p99_ms: float | None = None
    excluded: list[Step] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def baseline_p99(steps: list[StepMeasurement]) -> float | None:
    """P99 of the lowest-rate valid step with a P99, or None if there is none."""
    for m in sorted(steps, key=lambda s: s.rate):
        if m.valid and m.p99_ms is not None:
            return m.p99_ms
    return None


def _excluded(m: StepMeasurement, note: str) -> Step:
    return Step(measurement=m, status="invalid-other", note=note)


def classify_step(m: StepMeasurement, baseline_p99_ms: float | None) -> Step:
    if m.valid:
        return Step(measurement=m, status="ok")
    reasons = m.invalid_reasons
    purely_saturation = bool(reasons) and all(
        any(marker in r for marker in SATURATION_MARKERS) for r in reasons
    )
    if not purely_saturation:
        return _excluded(m, "invalid for a non-saturation reason")
    if m.k6_cpu_pct is None:
        return _excluded(m, "k6 CPU unknown: cannot rule out a starved load generator")
    if m.k6_cpu_pct >= K6_CPU_MAX_PCT:
        return _excluded(
            m, f"k6 CPU {m.k6_cpu_pct:.0f}% >= {K6_CPU_MAX_PCT:.0f}%: load generator may limit"
        )
    if baseline_p99_ms is None or m.p99_ms is None:
        return _excluded(m, "no baseline or P99 to confirm latency inflation")
    if m.p99_ms <= P99_INFLATION_MAX * baseline_p99_ms:
        return _excluded(m, "drops/maxVUs without SUT latency inflation: VU starvation")
    return Step(
        measurement=m, status="saturated",
        note="dropped/maxVUs, k6 had spare CPU, SUT P99 inflated",
    )


def classify_all(measurements: list[StepMeasurement]) -> list[Step]:
    base = baseline_p99(measurements)
    return sorted((classify_step(m, base) for m in measurements), key=lambda s: s.rate)


def _is_good(step: Step, base: float) -> bool:
    m = step.measurement
    if step.status != "ok" or m.achieved is None or m.p99_ms is None:
        return False
    return m.achieved / m.rate >= ACHIEVED_RATIO_MIN and m.p99_ms <= P99_INFLATION_MAX * base


def find_knee(steps: list[Step], baseline_p99_ms: float | None) -> KneeResult:
    excluded = [s for s in steps if s.status == "invalid-other"]
    curve = sorted((s for s in steps if s.status != "invalid-other"), key=lambda s: s.rate)
    if baseline_p99_ms is None or not curve or curve[0].status != "ok":
        return KneeResult(
            status="no_baseline", baseline_p99_ms=baseline_p99_ms, excluded=excluded,
            warnings=["lowest non-excluded step is not a valid baseline"],
        )
    good = [_is_good(s, baseline_p99_ms) for s in curve]
    first_bad = next((i for i, g in enumerate(good) if not g), None)
    warnings: list[str] = []
    if first_bad is None:
        return KneeResult(
            status="above_range", lo=curve[-1].rate, baseline_p99_ms=baseline_p99_ms,
            excluded=excluded, warnings=["no bad step: knee is above the tested range"],
        )
    if any(good[first_bad + 1 :]):
        warnings.append("non-monotonic curve: a good step exists above the first bad step")
    if first_bad == 0:
        return KneeResult(
            status="below_range", hi=curve[0].rate, baseline_p99_ms=baseline_p99_ms,
            excluded=excluded, warnings=["lowest step is already bad: knee is below the range"],
        )
    lo, hi = curve[first_bad - 1].rate, curve[first_bad].rate
    return KneeResult(
        status="found", lo=lo, hi=hi, estimate=math.sqrt(lo * hi),
        width_pct=(hi / lo - 1.0) * 100.0, baseline_p99_ms=baseline_p99_ms,
        excluded=excluded, warnings=warnings,
    )


def analyse_sweep(measurements: list[StepMeasurement]) -> KneeResult:
    base = baseline_p99(measurements)
    return find_knee(classify_all(measurements), base)
