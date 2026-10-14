"""Knee finder on hand-built curves. Numbers here are test inputs, not measurements."""

import math

from serverlab.analysis.knee import StepMeasurement, analyse_sweep, classify_step

SAT = ["dropped iterations ratio 0.5 (50/100) exceeded 0.005 (0.5%) threshold"]


def ok(rate: float, p99: float = 100.0, frac: float = 1.0) -> StepMeasurement:
    return StepMeasurement(rate=rate, achieved=rate * frac, p99_ms=p99, valid=True)


def sat(rate: float, p99: float = 900.0, cpu: float | None = 30.0,
        reasons: list[str] | None = None) -> StepMeasurement:
    return StepMeasurement(rate=rate, achieved=rate * 0.6, p99_ms=p99, valid=False,
                           invalid_reasons=reasons or SAT, k6_cpu_pct=cpu)


def test_clean_knee_bracket_and_geometric_midpoint() -> None:
    res = analyse_sweep([ok(10), ok(20), ok(40), ok(80, 110), ok(160, 900, 0.6), sat(320)])
    assert res.status == "found" and (res.lo, res.hi) == (80, 160)
    assert res.estimate is not None and math.isclose(res.estimate, math.sqrt(80 * 160))
    assert res.width_pct is not None and math.isclose(res.width_pct, 100.0)


def test_divergence_alone_marks_bad() -> None:
    res = analyse_sweep([ok(10), ok(20), ok(40, 105, 0.90), ok(80, 105, 0.6)])
    assert (res.lo, res.hi) == (20, 40)


def test_p99_inflation_alone_marks_bad() -> None:
    res = analyse_sweep([ok(10, 100), ok(20, 150), ok(40, 250), ok(80, 400)])
    assert (res.lo, res.hi) == (20, 40)  # 250 > 2 x 100


def test_no_knee_in_range_reports_above() -> None:
    res = analyse_sweep([ok(10), ok(20), ok(40)])
    assert res.status == "above_range" and res.lo == 40 and res.estimate is None


def test_knee_below_range_reports_below() -> None:
    res = analyse_sweep([ok(10, 100, 0.5), ok(20, 500, 0.4), ok(40, 900, 0.3)])
    assert res.status == "below_range" and res.hi == 10


def test_saturated_invalid_step_is_upper_bracket_not_hidden() -> None:
    res = analyse_sweep([ok(10), ok(20), ok(40), sat(80)])
    assert (res.lo, res.hi) == (40, 80) and res.excluded == []


def test_starved_load_generator_is_excluded_not_a_knee() -> None:
    # invalid for drops/maxVUs but P99 flat: VU starvation, not saturation
    res = analyse_sweep([ok(10), ok(20), sat(40, p99=105.0), ok(80)])
    assert [s.status for s in res.excluded] == ["invalid-other"]
    assert res.status == "above_range"  # the excluded step does not create a knee


def test_high_k6_cpu_or_unknown_k6_cpu_is_excluded() -> None:
    base = ok(10).p99_ms
    assert classify_step(sat(80, cpu=95.0), base).status == "invalid-other"
    assert classify_step(sat(80, cpu=None), base).status == "invalid-other"


def test_non_saturation_invalid_reason_is_excluded() -> None:
    odd = sat(80, reasons=["k6 process exited with non-zero return code 1"])
    assert classify_step(odd, 100.0).status == "invalid-other"
    mixed = sat(80, reasons=[*SAT, "k6 exited with code 99"])
    assert classify_step(mixed, 100.0).status == "invalid-other"


def test_non_monotonic_curve_warns() -> None:
    res2 = analyse_sweep([ok(10), ok(20), ok(40, 500, 0.5), ok(80)])
    assert res2.status == "found" and any("non-monotonic" in w for w in res2.warnings)


def test_no_valid_baseline_is_reported() -> None:
    res = analyse_sweep([sat(10), sat(20)])
    assert res.status == "no_baseline"


def test_input_order_does_not_matter() -> None:
    steps = [ok(10), ok(20), ok(40, 600, 0.5), ok(80, 900, 0.3)]
    a = analyse_sweep(steps)
    b = analyse_sweep(list(reversed(steps)))
    assert a == b
