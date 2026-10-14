"""Grading of knee recovery against the committed predictions. Numbers are test inputs."""

import pytest

from serverlab.analysis.knee import KneeResult
from serverlab.analysis.validate import grade_knee, knee_checks, predicted_capacity


def _knee(
    lo: float | None, hi: float | None, est: float | None, status: str = "found"
) -> KneeResult:
    return KneeResult.model_validate({"status": status, "lo": lo, "hi": hi, "estimate": est})


def test_predicted_capacity_from_env_only() -> None:
    assert predicted_capacity({"CONCURRENCY_LIMIT": "5"}, {"SERVICE_TIME_MS": "100"}) == 50.0
    assert predicted_capacity({}, {"CONCURRENCY_LIMIT": "20", "SERVICE_TIME_MS": "100"}) == 200.0
    with pytest.raises(KeyError):
        predicted_capacity({}, {"SERVICE_TIME_MS": "100"})


def test_within_tolerance_and_containment() -> None:
    c = grade_knee("a", 100.0, _knee(90, 108, 98.7), steps=12)
    assert c.within_tolerance and c.bracket_contains_c and round(c.error_pct or 0, 1) == -1.3


def test_outside_ten_percent_fails() -> None:
    c = grade_knee("a", 100.0, _knee(60, 80, 69.3), steps=12)
    assert not c.within_tolerance and not c.bracket_contains_c


def test_no_estimate_fails() -> None:
    c = grade_knee("a", 100.0, _knee(None, None, None, "above_range"), steps=8)
    assert not c.within_tolerance and c.error_pct is None


def test_two_of_three_brackets_rule() -> None:
    ok = grade_knee("a", 100.0, _knee(90, 110, 99.5), 10)
    miss = grade_knee("b", 100.0, _knee(60, 80, 69.3), 10)
    two = knee_checks([ok, ok, miss])
    assert [c.passed for c in two] == [True, True, False, True]
    one = knee_checks([ok, miss, miss])
    assert one[-1].passed is False and "1 of 3" in one[-1].detail
