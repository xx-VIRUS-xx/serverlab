"""Runner lifecycle with Docker and the load adapter faked (no k6, no containers)."""

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from serverlab.experiment.runner import Outcome, RunnerError, run_single
from serverlab.experiment.spec import RunSpec
from serverlab.load.base import LatencyMs, LoadSpec, RunResult

K6 = "grafana/k6@sha256:" + "1" * 64
SUT = "sha256:" + "2" * 64
BUILD = {"build_context": ".", "dockerfile": "Dockerfile"}


def _spec(build: bool = False, cpus: dict[str, list[int]] | None = None) -> RunSpec:
    return RunSpec.model_validate(
        {
            "name": "unit", "seed": 7, "warmup_s": 1, "cooldown_s": 0,
            "target": {"image": SUT, **(BUILD if build else {})},
            "k6_image": K6,
            "load": {"rate": 10, "duration_s": 3, "pre_allocated_vus": 2, "max_vus": 5,
                     "url": "http://sut:8000/work"},
            "cpus": cpus or {"sut": [0], "load": [1]},
        }
    )


class FakeAdapter:
    def __init__(self, valid: bool = True) -> None:
        self.valid = valid
        self.calls: list[str] = []

    def run(self, spec: LoadSpec, out_dir: Path, run_id: str, **kw: Any) -> RunResult:
        self.calls.append(run_id)
        if kw.get("per_request_path"):
            kw["per_request_path"].write_text(
                json.dumps({"type": "Point", "metric": "http_req_duration",
                            "data": {"value": 5.0}}) + "\n"
            )
        return RunResult(
            run_id=run_id, planned=30, completed=30, dropped=0 if self.valid else 20,
            peak_vus=1, latency_ms=LatencyMs(p50=1, p90=2, p99=3, max=4), error_rate=0,
            valid=self.valid, invalid_reasons=[] if self.valid else ["dropped"],
            summary_path=out_dir / f"{run_id}.k6summary.json",
        )


class FakeDocker:
    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(argv))
        failed = self.fail_on is not None and self.fail_on in argv
        out = SUT if "inspect" in argv else ""
        code, err = (1, "boom") if failed else (0, "")
        return subprocess.CompletedProcess(list(argv), code, out, err)


def _run(tmp_path: Path, spec: RunSpec, docker: FakeDocker, adapter: FakeAdapter) -> Outcome:
    return run_single(spec, tmp_path, base_dir=tmp_path, cmd=docker,
                      adapter_factory=lambda _c: adapter, sleep=lambda _s: None)


def test_artifacts_and_teardown(tmp_path: Path) -> None:
    docker, adapter = FakeDocker(), FakeAdapter()
    out = _run(tmp_path, _spec(), docker, adapter)
    d = out.result_dir
    assert (d / "environment.json").is_file() and (d / "spec.yaml").is_file()
    assert (d / "compose.yaml").is_file() and (d / "raw" / "run_main_1.json").is_file()
    assert (d / "raw" / "run_main_1.hdr").is_file()
    assert not (d / "raw" / "run_main_1.requests.json").exists()  # keep_raw is false
    assert adapter.calls == ["warmup", "run_main_1"]
    assert any("down" in c for c in docker.calls[-1:])
    assert out.valid and out.record.hdr_stats == {
        "recorded": 1, "below_range": 0, "above_range": 0, "other_lines": 0}


def test_invalid_run_is_reported_not_hidden(tmp_path: Path) -> None:
    out = _run(tmp_path, _spec(), FakeDocker(), FakeAdapter(valid=False))
    assert not out.valid
    saved = json.loads((out.result_dir / "raw" / "run_main_1.json").read_text())
    assert saved["result"]["valid"] is False and saved["result"]["invalid_reasons"] == ["dropped"]


def test_teardown_runs_when_compose_up_fails(tmp_path: Path) -> None:
    docker = FakeDocker(fail_on="up")
    with pytest.raises(RunnerError, match="compose up"):
        _run(tmp_path, _spec(), docker, FakeAdapter())
    assert "down" in docker.calls[-1]


def test_results_dir_never_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import datetime as dt

    import serverlab.experiment.runner as runner

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> "Frozen":
            return cls(2026, 1, 1, 0, 0, 0, tzinfo=tz)

    monkeypatch.setattr(runner, "datetime", Frozen)
    _run(tmp_path, _spec(), FakeDocker(), FakeAdapter())
    with pytest.raises(FileExistsError):
        _run(tmp_path, _spec(), FakeDocker(), FakeAdapter())
