"""Experiment lifecycle with Docker, the load adapter and the collector faked."""

import datetime as dt
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

import serverlab.experiment.runner as runner
from serverlab.cli import app
from serverlab.experiment.runner import ExperimentOutcome, run_experiment
from serverlab.experiment.spec import ExperimentSpec
from serverlab.load.base import LatencyMs, LoadSpec, RunResult

K6 = "grafana/k6@sha256:" + "1" * 64
SUT = "sha256:" + "2" * 64
BUILD = {"build_context": ".", "dockerfile": "Dockerfile"}


def _spec(reps: int = 2, **over: Any) -> ExperimentSpec:
    raw: dict[str, Any] = {
        "name": "unit", "seed": 7, "repetitions": reps, "warmup_s": 1, "cooldown_s": 0,
        "target": {"image": SUT}, "k6_image": K6,
        "load": {"rate": 10, "duration_s": 3, "pre_allocated_vus": 2, "max_vus": 5,
                 "url": "http://sut:8000/work"},
        "cpus": {"sut": [0], "load": [1]},
        "conditions": [{"name": "A"}, {"name": "B", "target_env": {"X": "2"}}],
    }
    raw.update(over)
    return ExperimentSpec.model_validate(raw)


class FakeAdapter:
    def __init__(self, invalid_runs: set[str] | None = None) -> None:
        self.invalid_runs = invalid_runs or set()
        self.calls: list[str] = []

    def run(self, spec: LoadSpec, out_dir: Path, run_id: str, **kw: Any) -> RunResult:
        self.calls.append(run_id)
        if kw.get("per_request_path"):
            kw["per_request_path"].write_text(
                json.dumps({"type": "Point", "metric": "http_req_duration",
                            "data": {"value": 5.0}}) + "\n"
            )
        bad = run_id in self.invalid_runs
        return RunResult(
            run_id=run_id, planned=30, completed=30, dropped=20 if bad else 0,
            peak_vus=1, latency_ms=LatencyMs(p50=1, p90=2, p99=3, max=4), error_rate=0,
            valid=not bad, invalid_reasons=["dropped"] if bad else [],
            summary_path=out_dir / f"{run_id}.k6summary.json",
        )


class FakeDocker:
    def __init__(self, fail_up_for: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_up_for = fail_up_for
        self.cpu_reads = 0

    def __call__(self, argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(argv))
        failed = "up" in argv and self.fail_up_for is not None and any(
            self.fail_up_for in a for a in argv
        )
        out = SUT if "inspect" in argv else ""
        if "exec" in argv:
            self.cpu_reads += 1
            out = f"usage_usec {self.cpu_reads * 1000}\nuser_usec 1\n"
        code, err = (1, "boom") if failed else (0, "")
        return subprocess.CompletedProcess(list(argv), code, out, err)


class Stats(BaseModel):
    samples: int = 3


class FakeCollector:
    started: list[str] = []
    containers: list[list[str]] = []

    def __init__(self, containers: list[str]) -> None:
        FakeCollector.containers.append(containers)

    def start(self, run_id: str, out_path: Path) -> None:
        out_path.write_text('{"cpu_percent": 1.0}\n')
        FakeCollector.started.append(run_id)

    def stop(self) -> Stats:
        return Stats()


def _run(tmp_path: Path, spec: ExperimentSpec, docker: FakeDocker | None = None,
         adapter: FakeAdapter | None = None, **kw: Any) -> ExperimentOutcome:
    adapter = adapter or FakeAdapter()
    return run_experiment(spec, tmp_path, base_dir=tmp_path, cmd=docker or FakeDocker(),
                          adapter_factory=lambda _c: adapter, sleep=lambda _s: None,
                          **{"collector_factory": None, **kw})


def test_full_layout_and_fresh_state_per_run(tmp_path: Path) -> None:
    docker, adapter = FakeDocker(), FakeAdapter()
    FakeCollector.started, FakeCollector.containers = [], []
    out = _run(tmp_path, _spec(2), docker, adapter, collector_factory=FakeCollector)
    assert all(len(c) == 1 and c[0].endswith("-sut-1") for c in FakeCollector.containers)
    d = out.result_dir
    for name in ("environment.json", "spec.yaml", "schedule.json"):
        assert (d / name).is_file()
    assert len(out.records) == 4 and out.all_valid
    for rec in out.records:
        rid = f"run_{rec.condition}_{rec.repetition}"
        assert (d / "raw" / f"{rid}.json").is_file() and (d / "raw" / f"{rid}.hdr").is_file()
        assert (d / "metrics" / f"{rid}.jsonl").is_file()
        assert (d / "compose" / f"{rid}.yaml").is_file()
        assert not (d / "raw" / f"{rid}.requests.json").exists()  # keep_raw false
    ups = [c for c in docker.calls if "up" in c]
    downs = [c for c in docker.calls if "down" in c]
    assert len(ups) == len(downs) == 4  # a fresh stack per scheduled run
    assert len({c[3] for c in ups}) == 4  # distinct compose project per run
    assert [r.index for r in out.records] == [0, 1, 2, 3]
    assert sorted(FakeCollector.started) == sorted(
        f"run_{r.condition}_{r.repetition}" for r in out.records
    )


def test_execution_follows_stored_schedule(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    out = _run(tmp_path, _spec(3), adapter=adapter)
    stored = json.loads((out.result_dir / "schedule.json").read_text())
    expected = [f"run_{r['condition']}_{r['repetition']}" for r in stored["runs"]]
    assert [c for c in adapter.calls if not c.endswith(".warmup")] == expected


def test_conditions_get_their_own_env(tmp_path: Path) -> None:
    out = _run(tmp_path, _spec(1))
    import yaml
    for name in ("A", "B"):
        doc = yaml.safe_load((out.result_dir / "compose" / f"run_{name}_1.yaml").read_text())
        env = doc["services"]["sut"]["environment"]
        assert ("X" in env) == (name == "B")


def test_invalid_runs_kept_flagged_not_retried(tmp_path: Path) -> None:
    adapter = FakeAdapter(invalid_runs={"run_A_1"})
    out = _run(tmp_path, _spec(2), adapter=adapter)
    assert not out.all_valid and [r.condition for r in out.invalid] == ["A"]
    assert len(out.records) == 4 and adapter.calls.count("run_A_1") == 1
    saved = json.loads((out.result_dir / "raw" / "run_A_1.json").read_text())
    assert saved["result"]["valid"] is False and saved["result"]["invalid_reasons"] == ["dropped"]


def test_failed_run_recorded_and_experiment_continues(tmp_path: Path) -> None:
    docker = FakeDocker(fail_up_for="run-a-1")
    out = _run(tmp_path, _spec(2), docker)
    assert [e.run_id for e in out.errors] == ["run_A_1"] and len(out.records) == 3
    assert (out.result_dir / "raw" / "run_A_1.error.json").is_file()
    assert not out.all_valid
    assert sum("down" in c for c in docker.calls) == 4  # torn down even after the failure


def test_results_dir_never_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> "Frozen":
            return cls(2026, 1, 1, 0, 0, 0, tzinfo=tz)

    monkeypatch.setattr(runner, "datetime", Frozen)
    _run(tmp_path, _spec(1))
    with pytest.raises(FileExistsError):
        _run(tmp_path, _spec(1))


def test_cli_dry_run_prints_same_order_as_schedule_file(tmp_path: Path) -> None:
    import yaml
    spec_file = tmp_path / "e.yaml"
    spec_file.write_text(yaml.safe_dump(_spec(3).model_dump(mode="json")))
    res = CliRunner().invoke(app, ["run", str(spec_file), "--dry-run"])
    assert res.exit_code == 0
    again = CliRunner().invoke(app, ["run", str(spec_file), "--dry-run"])
    assert res.stdout == again.stdout and "order:" in res.stdout
    assert not (tmp_path / "results").exists()  # dry run writes nothing


def test_sut_cpu_usec_recorded_from_cgroup_delta(tmp_path: Path) -> None:
    out = _run(tmp_path, _spec(1))
    # per run: one read before and one after the measured window => delta of 1000 us
    assert [r.sut_cpu_usec for r in out.records] == [1000, 1000]
    assert all(r.sut_cpu_note is None for r in out.records)


def test_per_condition_metrics_override(tmp_path: Path) -> None:
    spec = _spec(1, conditions=[{"name": "off", "metrics_enabled": False}, {"name": "on"}])
    FakeCollector.started, FakeCollector.containers = [], []
    _run(tmp_path, spec, collector_factory=FakeCollector)
    assert FakeCollector.started == ["run_on_1"]
