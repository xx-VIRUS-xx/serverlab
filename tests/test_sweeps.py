"""Sweep wiring with Docker and the load adapter faked: clean stack per step, errors kept."""

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from serverlab.experiment.spec import ExperimentSpec
from serverlab.experiment.sweeps import measurement_from_record, run_sweeps
from serverlab.load.base import LatencyMs, LoadSpec, RunResult

SUT = "sha256:" + "2" * 64
K6 = "grafana/k6@sha256:" + "1" * 64
CAP = 100


def _spec() -> ExperimentSpec:
    return ExperimentSpec.model_validate({
        "name": "sw", "seed": 5, "repetitions": 1, "target": {"image": SUT}, "k6_image": K6,
        "load": {"rate": 10, "duration_s": 5, "pre_allocated_vus": 2, "max_vus": 5,
                 "url": "http://sut:8000/w"},
        "cpus": {"sut": [0], "load": [1]}, "metrics": {"enabled": False},
        "conditions": [{"name": "c1"}, {"name": "c2"}],
        "sweep": {"rate_lo": 20, "rate_hi": 320, "step_duration_s": 5, "step_warmup_s": 0,
                  "coarse_points": 5, "max_bisect": 3},
    })


class Docker:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(argv))
        out = SUT if "inspect" in argv else ("usage_usec 1000\n" if "exec" in argv else "")
        return subprocess.CompletedProcess(list(argv), 0, out, "")


class Adapter:
    """Known capacity: rates above CAP drop iterations with spare k6 CPU and inflated P99."""

    def __init__(self) -> None:
        self.rates: list[int] = []

    def run(self, spec: LoadSpec, out_dir: Path, run_id: str, **kw: Any) -> RunResult:
        over = spec.rate > CAP
        if not run_id.endswith("warmup"):
            self.rates.append(spec.rate)
        planned = spec.rate * spec.duration_s
        return RunResult(
            run_id=run_id, planned=planned, completed=planned // 2 if over else planned,
            dropped=planned // 2 if over else 0, peak_vus=1,
            latency_ms=LatencyMs(p50=100, p90=100, p99=1000 if over else 100, max=1000),
            error_rate=0, valid=not over,
            invalid_reasons=["dropped iterations ratio 0.5 (x/y) exceeded 0.005"] if over else [],
            k6_cpu_usec=1_000_000, k6_wall_s=10.0, summary_path=out_dir / f"{run_id}.json",
        )


def test_sweep_finds_known_capacity_per_condition_with_fresh_stacks(tmp_path: Path) -> None:
    docker, adapter = Docker(), Adapter()
    out = run_sweeps(_spec(), tmp_path, base_dir=tmp_path, cmd=docker, hdr_builder=None,
                     sleep=lambda _s: None, adapter_factory=lambda _c: adapter)
    assert [c.condition for c in out.conditions] == ["c1", "c2"]
    assert [c.seed for c in out.conditions] == [5, 6]
    for cs in out.conditions:
        knee = cs.sweep.knee
        assert knee.status == "found" and knee.lo is not None and knee.hi is not None
        assert knee.lo <= CAP <= knee.hi
        assert len(cs.records) == len(cs.sweep.steps)
        assert (out.result_dir / f"sweep_{cs.condition}.json").is_file()
    ups = [c for c in docker.calls if "up" in c]
    assert len(ups) == sum(len(c.records) for c in out.conditions)  # one fresh stack per step
    assert len([c for c in docker.calls if "down" in c]) == len(ups)


def test_measurement_uses_wall_time_not_nominal_duration(tmp_path: Path) -> None:
    out = run_sweeps(_spec(), tmp_path, base_dir=tmp_path, cmd=Docker(), hdr_builder=None,
                     sleep=lambda _s: None, adapter_factory=lambda _c: Adapter())
    rec = out.conditions[0].records[0]
    m = measurement_from_record(rec, duration_s=5, load_cpus=1)
    assert m.k6_cpu_pct == 10.0  # 1.0 s CPU over 10.0 s wall on 1 cpu, not over the nominal 5 s
    rec2 = rec.model_copy(update={"result": rec.result.model_copy(update={"k6_wall_s": None})})
    assert measurement_from_record(rec2, 5, 1).k6_cpu_pct is None
