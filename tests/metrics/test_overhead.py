"""Tests for the collector overhead analysis (A-4).

Every number in these fixtures is a TEST INPUT written to exercise the logic. None is a
measurement. Fixture capacity: duration_s=10 and 2 SUT cpus, so utilization_pct is
100 * usec / (10 * 1e6 * 2): 200_000 usec -> 1.0 %, 500_000 usec -> 2.5 %.
"""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from serverlab.cli import app
from serverlab.metrics.overhead import (
    ExperimentParams,
    OverheadInputError,
    OverheadRun,
    analyse,
    load_inputs,
    render,
    utilization_pct,
)

OFF = "collector-off"
ON = "collector-on"
DURATION_S = 10
SUT_CPUS = [0, 1]


def _run_json(
    cond: str,
    rep: int,
    usec: int | None,
    *,
    valid: bool = True,
    reasons: list[str] | None = None,
    note: str | None = None,
    p99: float = 2.0,
) -> dict[str, Any]:
    return {
        "experiment": "a4-fixture",
        "condition": cond,
        "repetition": rep,
        "result": {
            "run_id": f"run_{cond}_{rep}",
            "planned": 100,
            "completed": 100,
            "dropped": 0,
            "peak_vus": 5,
            "latency_ms": {"p50": 1.0, "p90": 1.5, "p99": p99, "max": 9.0},
            "error_rate": 0.0,
            "valid": valid,
            "invalid_reasons": reasons or [],
            "summary_path": "raw/x.k6summary.json",
        },
        "sut_cpu_usec": usec,
        "sut_cpu_note": note,
    }


def _make_results(
    root: Path,
    runs: list[dict[str, Any]],
    *,
    conditions: tuple[str, ...] = (OFF, ON),
) -> Path:
    """Write spec.yaml and raw/run_<cond>_<n>.json files; return the result directory."""
    result_dir = root / "a4-fixture" / "20261014T000000Z"
    (result_dir / "raw").mkdir(parents=True)
    spec = {
        "name": "a4-fixture",
        "seed": 1,
        "target": {"image": "example/sut:latest"},
        "k6_image": "example/k6:latest",
        "load": {
            "rate": 100,
            "duration_s": DURATION_S,
            "pre_allocated_vus": 5,
            "max_vus": 10,
            "url": "http://sut:8000/",
        },
        "cpus": {"sut": SUT_CPUS, "load": [2, 3]},
        "conditions": [{"name": c} for c in conditions],
    }
    (result_dir / "spec.yaml").write_text(yaml.safe_dump(spec))
    for run in runs:
        name = f"run_{run['condition']}_{run['repetition']}.json"
        (result_dir / "raw" / name).write_text(json.dumps(run))
    return result_dir


def _balanced(off_usec: list[int], on_usec: list[int]) -> list[dict[str, Any]]:
    runs = [_run_json(OFF, i + 1, u) for i, u in enumerate(off_usec)]
    runs += [_run_json(ON, i + 1, u) for i, u in enumerate(on_usec)]
    return runs


# Utilization of the fixtures: off = 1.00, 1.05, 0.95 ; on = 2.50, 2.60, 2.40 (percent).
OFF_BELOW = [200_000, 210_000, 190_000]
ON_BELOW = [500_000, 520_000, 480_000]  # median diff 1.50 -> BELOW
ON_ABOVE = [700_000, 720_000, 680_000]  # median diff 2.50 -> ABOVE


def _fixture_params() -> ExperimentParams:
    return ExperimentParams(name="a4-fixture", duration_s=DURATION_S, sut_cpus=len(SUT_CPUS))


def _as_sources(runs: list[dict[str, Any]]) -> list[tuple[str, OverheadRun]]:
    out: list[tuple[str, OverheadRun]] = []
    for run in runs:
        parsed = OverheadRun.model_validate(run)
        out.append((f"run_{run['condition']}_{run['repetition']}.json", parsed))
    return out


def test_utilization_formula_matches_spec() -> None:
    # 100 * usec / (duration_s * 1e6 * cpus): 1 s of CPU on 2 cpus over 10 s is 5 %.
    assert utilization_pct(1_000_000, 10, 2) == pytest.approx(5.0)
    assert utilization_pct(200_000, 10, 2) == pytest.approx(1.0)
    assert utilization_pct(0, 10, 2) == 0.0


def test_utilization_rejects_non_positive_capacity() -> None:
    with pytest.raises(OverheadInputError):
        utilization_pct(1, 0, 2)
    with pytest.raises(OverheadInputError):
        utilization_pct(1, 10, 0)


def test_verdict_below_threshold() -> None:
    report = analyse(_fixture_params(), _as_sources(_balanced(OFF_BELOW, ON_BELOW)))
    assert report.overhead_pct == pytest.approx(1.5)
    assert report.ranges_overlap is False
    assert report.verdict == "BELOW 2% threshold"
    assert not report.insufficient


def test_verdict_above_threshold() -> None:
    report = analyse(_fixture_params(), _as_sources(_balanced(OFF_BELOW, ON_ABOVE)))
    assert report.overhead_pct == pytest.approx(2.5)
    assert report.verdict == "ABOVE 2% threshold"


def test_difference_of_exactly_two_points_is_above() -> None:
    # Rule: BELOW only if the difference is < 2.0; a difference of exactly 2.0 is ABOVE.
    off = [200_000, 200_000, 200_000]  # 1.0 % each
    on = [600_000, 600_000, 600_000]  # 3.0 % each -> diff 2.0
    report = analyse(_fixture_params(), _as_sources(_balanced(off, on)))
    assert report.overhead_pct == pytest.approx(2.0)
    assert report.verdict == "ABOVE 2% threshold"


def test_overlapping_ranges_are_flagged() -> None:
    # off utilization 0.95..1.05 ; on utilization 1.00..1.15 -> ranges overlap.
    on = [200_000, 215_000, 230_000]
    report = analyse(_fixture_params(), _as_sources(_balanced(OFF_BELOW, on)))
    assert report.ranges_overlap is True
    assert report.verdict == (
        "BELOW 2% threshold (on/off ranges overlap: difference not distinguishable "
        "from run-to-run noise)"
    )


def test_invalid_and_null_cpu_runs_are_excluded_and_not_counted() -> None:
    runs = _balanced(OFF_BELOW, ON_BELOW)
    # Extreme values that must not enter the statistics.
    runs.append(_run_json(OFF, 4, 9_000_000, valid=False, reasons=["dropped too many"]))
    runs.append(_run_json(ON, 4, None, note="container exited before window end"))
    runs.append(_run_json(ON, 5, 9_000_000, valid=False, note=None))
    report = analyse(_fixture_params(), _as_sources(runs))
    assert report.off.n == 3
    assert report.on.n == 3
    assert report.overhead_pct == pytest.approx(1.5)
    assert report.verdict == "BELOW 2% threshold"
    reasons = {(e.condition, e.repetition): e.reason for e in report.excluded}
    assert reasons[(OFF, 4)] == "result.valid=false: dropped too many"
    assert reasons[(ON, 4)] == "sut_cpu_usec is null (container exited before window end)"
    assert reasons[(ON, 5)] == "result.valid=false"
    assert len(report.excluded) == 3


def test_null_cpu_without_note_is_excluded_with_reason() -> None:
    runs = _balanced(OFF_BELOW, ON_BELOW) + [_run_json(OFF, 4, None)]
    report = analyse(_fixture_params(), _as_sources(runs))
    assert report.off.n == 3
    assert [e.reason for e in report.excluded] == ["sut_cpu_usec is null"]


def test_insufficient_data_with_two_included_runs() -> None:
    runs = _balanced(OFF_BELOW[:2], ON_BELOW)
    report = analyse(_fixture_params(), _as_sources(runs))
    assert report.insufficient
    assert report.overhead_pct is None
    assert report.verdict.startswith("INSUFFICIENT DATA")
    assert "collector-off n=2" in report.verdict


def test_render_prints_numbers_before_verdict() -> None:
    report = analyse(_fixture_params(), _as_sources(_balanced(OFF_BELOW, ON_BELOW)))
    lines = render(report)
    assert lines[-1] == "Verdict: BELOW 2% threshold"
    assert any(line.startswith("collector-off: n=3 included") for line in lines)
    assert any(line.startswith("collector-on: n=3 included") for line in lines)
    assert any("overhead_pct_of_sut_capacity" in line for line in lines)


def test_load_inputs_ignores_warmup_and_error_files(tmp_path: Path) -> None:
    runs = _balanced(OFF_BELOW, ON_BELOW)
    result_dir = _make_results(tmp_path, runs)
    raw = result_dir / "raw"
    (raw / "run_collector-off_1.warmup.json").write_text("{}")
    (raw / "run_collector-on_2.error.json").write_text("{}")
    params, sources = load_inputs(result_dir)
    assert params.duration_s == DURATION_S
    assert params.sut_cpus == len(SUT_CPUS)
    assert len(sources) == 6


def test_cli_prints_verdict_and_exits_zero(tmp_path: Path) -> None:
    result_dir = _make_results(tmp_path, _balanced(OFF_BELOW, ON_BELOW))
    res = CliRunner().invoke(app, ["overhead", str(result_dir)])
    assert res.exit_code == 0, res.output
    assert "overhead_pct_of_sut_capacity" in res.output
    assert res.output.rstrip().splitlines()[-1] == "Verdict: BELOW 2% threshold"


def test_cli_above_threshold_exits_zero(tmp_path: Path) -> None:
    result_dir = _make_results(tmp_path, _balanced(OFF_BELOW, ON_ABOVE))
    res = CliRunner().invoke(app, ["overhead", str(result_dir)])
    assert res.exit_code == 0, res.output
    assert res.output.rstrip().splitlines()[-1] == "Verdict: ABOVE 2% threshold"


def test_cli_insufficient_exits_three(tmp_path: Path) -> None:
    result_dir = _make_results(tmp_path, _balanced(OFF_BELOW[:2], ON_BELOW))
    res = CliRunner().invoke(app, ["overhead", str(result_dir)])
    assert res.exit_code == 3, res.output
    assert "INSUFFICIENT DATA" in res.output


def test_cli_no_raw_runs_for_a_condition_is_insufficient(tmp_path: Path) -> None:
    result_dir = _make_results(tmp_path, _balanced(OFF_BELOW, []))
    res = CliRunner().invoke(app, ["overhead", str(result_dir)])
    assert res.exit_code == 3, res.output
    assert "INSUFFICIENT DATA" in res.output


def test_cli_missing_condition_exits_two(tmp_path: Path) -> None:
    result_dir = _make_results(
        tmp_path, [_run_json(OFF, i + 1, 200_000) for i in range(3)], conditions=(OFF,)
    )
    res = CliRunner().invoke(app, ["overhead", str(result_dir)])
    assert res.exit_code == 2
    assert "collector-on" in res.output


def test_cli_missing_spec_exits_two(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    res = CliRunner().invoke(app, ["overhead", str(empty)])
    assert res.exit_code == 2
    assert "no spec.yaml" in res.output


def test_cli_malformed_run_file_exits_two(tmp_path: Path) -> None:
    result_dir = _make_results(tmp_path, _balanced(OFF_BELOW, ON_BELOW))
    (result_dir / "raw" / "run_collector-on_9.json").write_text('{"not": "a run"}')
    res = CliRunner().invoke(app, ["overhead", str(result_dir)])
    assert res.exit_code == 2
    assert "not a RunRecord" in res.output
