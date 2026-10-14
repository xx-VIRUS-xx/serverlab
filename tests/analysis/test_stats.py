"""Statistics tests. Sample values are test inputs, not measurements."""

import numpy as np
import pytest
from hdrh.histogram import HdrHistogram  # type: ignore[import-untyped]
from scipy import stats as sps

from serverlab.analysis.stats import (
    bootstrap_median_ci,
    effect_size,
    merge_histograms,
    new_histogram,
    per_run_percentiles_ms,
    pooled_percentile_ms,
)

SAMPLE = [10.1, 9.8, 10.4, 10.0, 9.9, 10.2, 10.3, 9.7]


def test_bootstrap_is_seeded_and_deterministic() -> None:
    assert bootstrap_median_ci(SAMPLE, seed=1) == bootstrap_median_ci(SAMPLE, seed=1)
    wide = list(np.random.default_rng(0).normal(10, 1, 40))
    bounds = {(c.lo, c.hi) for c in (bootstrap_median_ci(wide, seed=s) for s in range(10))}
    assert len(bounds) > 1  # the seed really drives the resampling


def test_interval_brackets_the_point_and_reports_settings() -> None:
    ci = bootstrap_median_ci(SAMPLE, seed=3)
    assert ci.lo <= ci.point <= ci.hi
    assert (ci.n, ci.resamples, ci.confidence) == (8, 10_000, 0.95)


def test_matches_scipy_percentile_bootstrap_closely() -> None:
    ours = bootstrap_median_ci(SAMPLE, seed=5)
    ref = sps.bootstrap((np.array(SAMPLE),), np.median, n_resamples=10_000, method="percentile",
                        random_state=np.random.default_rng(5), confidence_level=0.95)
    assert ours.lo == pytest.approx(ref.confidence_interval.low, abs=0.15)
    assert ours.hi == pytest.approx(ref.confidence_interval.high, abs=0.15)


def test_small_or_bad_inputs_refused() -> None:
    with pytest.raises(ValueError, match="at least 3"):
        bootstrap_median_ci([1.0, 2.0], seed=1)
    with pytest.raises(ValueError, match="non-finite"):
        bootstrap_median_ci([1.0, float("nan"), 3.0], seed=1)


def test_clear_effect_is_significant_and_described() -> None:
    a = [100.0, 102.0, 98.0, 101.0, 99.0]
    b = [240.0, 245.0, 238.0, 250.0, 242.0]
    e = effect_size(a, b, seed=1)
    assert e.verdict == "significant" and 2.3 < e.ratio.point < 2.5
    assert e.ratio.lo > 1.0 and e.difference.lo > 0.0
    assert "no significant" not in e.describe()


def test_overlapping_groups_say_no_significant_difference() -> None:
    rng = np.random.default_rng(0)
    a = list(rng.normal(100, 5, 6))
    b = list(rng.normal(100, 5, 6))
    e = effect_size(a, b, seed=2)
    assert e.ratio.contains(1.0) and e.verdict == "no significant difference detected"
    assert e.describe().endswith("no significant difference detected")


def test_effect_size_deterministic_and_zero_median_rejected() -> None:
    a, b = [1.0, 2.0, 3.0], [2.0, 3.0, 4.0]
    assert effect_size(a, b, seed=4) == effect_size(a, b, seed=4)
    with pytest.raises(ValueError, match="ratio undefined"):
        effect_size([0.0, 0.0, 0.0], b, seed=1)


def _hist(values_us: list[int]) -> HdrHistogram:
    h = new_histogram()
    for v in values_us:
        h.record_value(v)
    return h


def test_merge_equals_pooling_the_samples_not_averaging_p99s() -> None:
    fast = _hist([100_000] * 990 + [101_000] * 10)  # p99 ~ 101 ms
    slow = _hist([100_000] * 900 + [900_000] * 100)  # p99 = 900 ms
    pooled = pooled_percentile_ms([fast, slow], 99)
    averaged = sum(per_run_percentiles_ms([fast, slow], 99)) / 2
    direct = _hist([100_000] * 990 + [101_000] * 10 + [100_000] * 900 + [900_000] * 100)
    assert pooled == pytest.approx(direct.get_value_at_percentile(99) / 1000.0, rel=1e-3)
    assert abs(pooled - averaged) > 100  # the two methods genuinely differ
    assert per_run_percentiles_ms([fast, slow], 99)[1] == pytest.approx(900.0, rel=1e-3)


def test_merge_does_not_modify_inputs_and_rejects_empty() -> None:
    h = _hist([1000, 2000])
    merge_histograms([h, h])
    assert h.get_total_count() == 2
    with pytest.raises(ValueError):
        merge_histograms([])
