"""Sweep driver against a fake system of known capacity.

Numbers in the fake (capacity C, p99 = 100 ms / 1000 ms, k6 CPU = 20%, the dropped-iterations
reason text) are test inputs chosen to exercise the driver. They are not measurements.
"""

import pytest

from serverlab.analysis.knee import StepMeasurement
from serverlab.analysis.sweep import coarse_rates, run_sweep

SAT_REASON = "dropped iterations ratio 0.5 (50/100) exceeded 0.005 (0.5%) threshold"
ODD_REASON = "k6 process exited with non-zero return code 1"


class FakeSystem:
    """Known capacity C: valid at rate <= C, saturated (dropped iterations) above it.

    `overload_valid=True` makes rates above C valid-but-overloaded (achieved = C, P99 inflated)
    instead, so that the lowest step can be a valid baseline that is already bad.
    `odd_rates` return a non-saturation invalid run regardless of C.
    """

    def __init__(
        self,
        capacity: float,
        *,
        overload_valid: bool = False,
        odd_rates: frozenset[int] = frozenset(),
    ) -> None:
        self.capacity = capacity
        self.overload_valid = overload_valid
        self.odd_rates = odd_rates
        self.calls: list[int] = []

    def __call__(self, rate: int) -> StepMeasurement:
        self.calls.append(rate)
        if rate in self.odd_rates:
            return StepMeasurement(
                rate=rate, achieved=None, p99_ms=100.0, valid=False,
                invalid_reasons=[ODD_REASON], k6_cpu_pct=20.0,
            )
        if rate <= self.capacity:
            return StepMeasurement(rate=rate, achieved=rate, p99_ms=100.0, valid=True)
        if self.overload_valid:
            return StepMeasurement(rate=rate, achieved=self.capacity, p99_ms=1000.0, valid=True)
        return StepMeasurement(
            rate=rate, achieved=self.capacity, p99_ms=1000.0, valid=False,
            invalid_reasons=[SAT_REASON], k6_cpu_pct=20.0,
        )


@pytest.mark.parametrize("capacity", [50, 100, 200])
def test_bracket_contains_capacity_and_is_tight(capacity: int) -> None:
    fake = FakeSystem(capacity)
    res = run_sweep(fake, rate_lo=10, rate_hi=1000, seed=1)
    knee = res.knee
    assert knee.status == "found"
    assert knee.lo is not None and knee.hi is not None and knee.estimate is not None
    assert knee.lo <= capacity <= knee.hi
    assert knee.width_pct is not None
    assert knee.width_pct <= 5.0 or len(res.bisect_rates) == 6
    assert abs(knee.estimate - capacity) / capacity <= 0.10
    assert len(fake.calls) <= 8 + 6


def test_capacity_above_range_reports_above_and_does_not_bisect() -> None:
    fake = FakeSystem(5000)
    res = run_sweep(fake, rate_lo=10, rate_hi=1000, seed=1)
    assert res.knee.status == "above_range"
    assert res.bisect_rates == []
    assert len(fake.calls) == len(res.coarse_order) == 8


def test_capacity_below_range_reports_below_and_does_not_bisect() -> None:
    # Lowest step must be a valid baseline that is already bad, so rates above C stay valid.
    fake = FakeSystem(5, overload_valid=True)
    res = run_sweep(fake, rate_lo=10, rate_hi=1000, seed=1)
    assert res.knee.status == "below_range"
    assert res.bisect_rates == []


def test_all_invalid_steps_report_no_baseline() -> None:
    # With the plain fake and C below the lowest rate there is no valid baseline at all.
    res = run_sweep(FakeSystem(5), rate_lo=10, rate_hi=1000, seed=1)
    assert res.knee.status == "no_baseline"
    assert res.bisect_rates == []


def test_same_seed_gives_identical_order_and_results() -> None:
    a = run_sweep(FakeSystem(100), rate_lo=10, rate_hi=1000, seed=42)
    b = run_sweep(FakeSystem(100), rate_lo=10, rate_hi=1000, seed=42)
    assert a.coarse_order == b.coarse_order
    assert a == b


def test_different_seeds_change_coarse_order() -> None:
    orders = {
        tuple(run_sweep(FakeSystem(100), rate_lo=10, rate_hi=1000, seed=s).coarse_order)
        for s in range(10)
    }
    assert len(orders) >= 2
    assert all(sorted(order) == coarse_rates(10, 1000, 8) for order in orders)


def test_measure_called_once_per_rate() -> None:
    fake = FakeSystem(100)
    res = run_sweep(fake, rate_lo=10, rate_hi=1000, seed=7)
    assert len(fake.calls) == len(set(fake.calls))
    assert set(fake.calls) == {*res.coarse_order, *res.bisect_rates}
    assert [s.rate for s in res.steps] == sorted(fake.calls)


def test_coarse_rates_are_log_spaced_and_inclusive() -> None:
    rates = coarse_rates(10, 1000, 8)
    assert rates[0] == 10 and rates[-1] == 1000
    assert rates == sorted(set(rates))


@pytest.mark.parametrize(
    ("rate_lo", "rate_hi", "coarse_points"),
    [(0, 1000, 8), (100, 100, 8), (100, 50, 8), (10, 1000, 2)],
)
def test_invalid_arguments_raise(rate_lo: int, rate_hi: int, coarse_points: int) -> None:
    with pytest.raises(ValueError):
        run_sweep(FakeSystem(100), rate_lo=rate_lo, rate_hi=rate_hi, seed=1,
                  coarse_points=coarse_points)


def test_non_saturation_invalid_step_is_kept_excluded_and_creates_no_knee() -> None:
    # 72 is a coarse rate below C=100: without the odd invalid run it would be the knee's lo.
    fake = FakeSystem(100, odd_rates=frozenset({72}))
    res = run_sweep(fake, rate_lo=10, rate_hi=1000, seed=3)
    odd = [s for s in res.steps if s.rate == 72]
    assert len(odd) == 1 and odd[0].status == "invalid-other"
    assert [s.rate for s in res.knee.excluded] == [72]
    assert res.knee.status == "found"
    assert res.knee.lo == 37 and res.knee.hi == 139  # 72 excluded, so the bracket skips it
    assert res.knee.lo <= 100 <= res.knee.hi
    # midpoint of [37, 139] is 72, already measured: bisection stops, it is not re-measured
    assert res.bisect_rates == [] and fake.calls.count(72) == 1
