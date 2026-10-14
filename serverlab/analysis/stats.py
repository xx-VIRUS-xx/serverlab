"""Statistics (plan.md s3.6, s3.7). Pure functions, seeded, no I/O.

* Latency percentiles: merge per-run HDR histograms and read the percentile from the merged
  histogram. A P99 is never averaged. Run-to-run variation is reported as the distribution of
  per-run percentiles (`per_run_percentiles`).
* Inference: percentile bootstrap, 10 000 resamples by default, `numpy.random.default_rng(seed)`,
  95% interval. No normal-theory intervals. n < 3 is refused: a bootstrap interval from one or
  two values would be meaningless.
* Effect size: ratio and difference of medians, each with its own bootstrap interval from
  resampling the two groups independently. If the interval contains the null (1 for the ratio,
  0 for the difference) the verdict is "no significant difference detected".
"""

from collections.abc import Callable, Sequence
from typing import Literal

import numpy as np
from hdrh.histogram import HdrHistogram  # type: ignore[import-untyped]
from numpy.typing import NDArray
from pydantic import BaseModel

DEFAULT_RESAMPLES = 10_000
DEFAULT_CONFIDENCE = 0.95
MIN_N = 3
HDR_LOW_US, HDR_HIGH_US, HDR_SIG_FIGS = 1, 60_000_000, 3  # must match serverlab.load.hdr

Stat = Callable[[NDArray[np.float64]], float]
Verdict = Literal["significant", "no significant difference detected"]


class Interval(BaseModel):
    point: float
    lo: float
    hi: float
    n: int
    resamples: int
    confidence: float
    seed: int

    def contains(self, x: float) -> bool:
        return self.lo <= x <= self.hi


class EffectSize(BaseModel):
    ratio: Interval  # median(b) / median(a)
    difference: Interval  # median(b) - median(a)
    n_a: int
    n_b: int
    verdict: Verdict

    def describe(self, unit: str = "") -> str:
        r, d, pct = self.ratio, self.difference, int(round(self.ratio.confidence * 100))
        head = (
            f"B/A = {r.point:.3f}x, {pct}% bootstrap CI [{r.lo:.3f}, {r.hi:.3f}]; "
            f"B-A = {d.point:.4g}{unit}, CI [{d.lo:.4g}, {d.hi:.4g}]; "
            f"n_A={self.n_a}, n_B={self.n_b}"
        )
        return head if self.verdict == "significant" else f"{head}: {self.verdict}"


def _array(values: Sequence[float], what: str) -> NDArray[np.float64]:
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim != 1 or arr.size < MIN_N:
        raise ValueError(f"{what}: need at least {MIN_N} values, got {arr.size}")
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{what}: contains non-finite values")
    return arr


def _median_axis(sample: NDArray[np.float64]) -> NDArray[np.float64]:
    return np.median(sample, axis=1)


def _bounds(dist: NDArray[np.float64], confidence: float) -> tuple[float, float]:
    alpha = (1.0 - confidence) / 2.0
    lo, hi = np.quantile(dist, [alpha, 1.0 - alpha])
    return float(lo), float(hi)


def bootstrap_median_ci(
    values: Sequence[float],
    seed: int,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
) -> Interval:
    """Percentile-bootstrap interval for the median of `values`."""
    x = _array(values, "values")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, x.size, size=(resamples, x.size))
    lo, hi = _bounds(_median_axis(x[idx]), confidence)
    return Interval(
        point=float(np.median(x)), lo=lo, hi=hi, n=int(x.size), resamples=resamples,
        confidence=confidence, seed=seed,
    )


def effect_size(
    a: Sequence[float],
    b: Sequence[float],
    seed: int,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
) -> EffectSize:
    """Effect of B relative to A (medians). Groups are resampled independently."""
    xa, xb = _array(a, "group A"), _array(b, "group B")
    if np.median(xa) == 0:
        raise ValueError("median of group A is 0: ratio undefined")
    rng = np.random.default_rng(seed)
    ma = _median_axis(xa[rng.integers(0, xa.size, size=(resamples, xa.size))])
    mb = _median_axis(xb[rng.integers(0, xb.size, size=(resamples, xb.size))])
    valid = ma != 0
    ratio_dist = mb[valid] / ma[valid]
    diff_dist = mb - ma
    pa, pb = float(np.median(xa)), float(np.median(xb))
    r_lo, r_hi = _bounds(ratio_dist, confidence)
    d_lo, d_hi = _bounds(diff_dist, confidence)
    n_a, n_b = int(xa.size), int(xb.size)
    ratio = Interval(point=pb / pa, lo=r_lo, hi=r_hi, n=n_a + n_b, resamples=int(valid.sum()),
                     confidence=confidence, seed=seed)
    diff = Interval(point=pb - pa, lo=d_lo, hi=d_hi, n=n_a + n_b, resamples=resamples,
                    confidence=confidence, seed=seed)
    verdict: Verdict = (
        "no significant difference detected" if ratio.contains(1.0) or diff.contains(0.0)
        else "significant"
    )
    return EffectSize(ratio=ratio, difference=diff, n_a=n_a, n_b=n_b, verdict=verdict)


def new_histogram() -> HdrHistogram:
    return HdrHistogram(HDR_LOW_US, HDR_HIGH_US, HDR_SIG_FIGS)


def merge_histograms(hists: Sequence[HdrHistogram]) -> HdrHistogram:
    """Pool per-run HDR histograms into a new one (inputs are not modified)."""
    if not hists:
        raise ValueError("no histograms to merge")
    merged = new_histogram()
    for h in hists:
        merged.add(h)
    return merged


def pooled_percentile_ms(hists: Sequence[HdrHistogram], percentile: float) -> float:
    """Percentile of the merged histogram, in ms (values are stored in microseconds)."""
    return float(merge_histograms(hists).get_value_at_percentile(percentile)) / 1000.0


def per_run_percentiles_ms(hists: Sequence[HdrHistogram], percentile: float) -> list[float]:
    """Each run's own percentile in ms: the run-to-run variation, reported, never averaged."""
    return [float(h.get_value_at_percentile(percentile)) / 1000.0 for h in hists]
