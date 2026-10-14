"""Experiment lifecycle: build once, then execute the seeded schedule one clean run at a time.

Docker, the load adapter and the metrics collector are injected so the lifecycle is
unit-testable without any of them. Results go under results/<experiment>/<timestamp>/
(plan.md s5) and are never overwritten: a fresh directory is created per experiment.

Every scheduled run is kept. Invalid runs are recorded and flagged, never retried or hidden;
a run that errors before producing a result is recorded as `raw/run_<cond>_<n>.error.json`.
"""

import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel

from serverlab.env.detect import capture_environment
from serverlab.experiment.schedule import Schedule, ScheduledRun, build_schedule
from serverlab.experiment.spec import ExperimentSpec, RunSpec
from serverlab.load.base import RunResult
from serverlab.load.hdr import build_hdr_from_json, write_hdr
from serverlab.load.k6 import ContainerExec, K6Adapter
from serverlab.metrics.collector import DockerStatsCollector
from serverlab.topology.compose import render_compose
from serverlab.topology.cpuset import CpuAssignment, assign_cpusets

Cmd = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
HdrBuilder = Callable[[Path, Path], dict[str, int]]  # (ndjson, hdr out) -> value accounting


class Collector(Protocol):
    def start(self, run_id: str, out_path: Path) -> None: ...
    def stop(self) -> Any: ...


def default_collector_factory(containers: list[str]) -> Collector:
    return DockerStatsCollector(interval_s=1.0, containers=containers)


def build_hdr_file(ndjson: Path, out: Path) -> dict[str, int]:
    """Default HDR step: per-request NDJSON -> compressed HDR file, with value accounting."""
    built = build_hdr_from_json(ndjson)
    write_hdr(built.histogram, out)
    return {
        "recorded": built.recorded,
        "below_range": built.below_range,
        "above_range": built.above_range,
        "other_lines": built.other_lines,
    }


def shell(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


class RunnerError(RuntimeError):
    pass


class RunRecord(BaseModel):
    experiment: str
    condition: str
    repetition: int
    index: int
    seed: int
    started_at: str
    finished_at: str
    sut_image: str
    k6_image: str
    cpus: dict[str, list[int]]
    hdr_stats: dict[str, int] | None = None
    metrics_stats: dict[str, Any] | None = None
    comparability: str
    result: RunResult


@dataclass(frozen=True)
class RunError:
    run_id: str
    message: str


@dataclass
class ExperimentOutcome:
    result_dir: Path
    schedule: Schedule
    records: list[RunRecord] = field(default_factory=list)
    errors: list[RunError] = field(default_factory=list)

    @property
    def invalid(self) -> list[RunRecord]:
        return [r for r in self.records if not r.result.valid]

    @property
    def all_valid(self) -> bool:
        return not self.errors and not self.invalid and len(self.records) == len(self.schedule.runs)


def _must(cmd: Cmd, argv: Sequence[str], what: str) -> str:
    proc = cmd(argv)
    if proc.returncode != 0:
        raise RunnerError(f"{what} failed (exit {proc.returncode}): {proc.stderr.strip()[:400]}")
    return proc.stdout.strip()


def _resolve_sut_image(spec: ExperimentSpec, base: Path, cmd: Cmd) -> str:
    if spec.target.build_context is None:
        return spec.target.image
    tag = f"serverlab-{spec.name}:build"
    argv = ["docker", "build", "-q", "-t", tag]
    if spec.target.dockerfile:
        argv += ["-f", str(base / spec.target.dockerfile)]
    _must(cmd, [*argv, str(base / spec.target.build_context)], "docker build")
    return _must(cmd, ["docker", "image", "inspect", tag, "--format", "{{.Id}}"], "image inspect")


def _fresh_dir(root: Path, name: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = root / name / stamp
    path.mkdir(parents=True, exist_ok=False)
    return path


def run_id_for(run: ScheduledRun) -> str:
    return f"run_{run.condition}_{run.repetition}"


def plan_schedule(spec: ExperimentSpec) -> Schedule:
    return build_schedule(spec.condition_names, spec.repetitions, spec.seed)


def run_experiment(
    spec: ExperimentSpec,
    results_root: Path,
    *,
    base_dir: Path | None = None,
    cmd: Cmd = shell,
    hdr_builder: HdrBuilder | None = build_hdr_file,
    collector_factory: Callable[[list[str]], Collector] | None = default_collector_factory,
    sleep: Callable[[float], None] = time.sleep,
    adapter_factory: Callable[[ContainerExec], Any] | None = None,
) -> ExperimentOutcome:
    base = Path.cwd() if base_dir is None else base_dir
    schedule = plan_schedule(spec)
    result_dir = _fresh_dir(results_root, spec.name)
    (result_dir / "raw").mkdir()
    (result_dir / "metrics").mkdir()
    (result_dir / "compose").mkdir()
    (result_dir / "spec.yaml").write_text(yaml.safe_dump(spec.model_dump(mode="json")))
    # The realized order is fixed and stored before anything runs.
    (result_dir / "schedule.json").write_text(schedule.model_dump_json(indent=2))

    sut_image = _resolve_sut_image(spec, base, cmd)

    # Total CPUs for validation = what the containers can see (the Docker VM on macOS).
    probe_env = capture_environment(experiment_dir=base)
    total = probe_env.docker_vm_cpus or probe_env.cpu_cores
    if total is None:
        raise RunnerError("cannot determine CPU count for cpuset validation")
    assignment = assign_cpusets(total, spec.cpus.sut, spec.cpus.load)
    env = capture_environment(
        image_digests={"sut": sut_image, "k6": spec.k6_image},
        cpu_allocation={"sut": list(assignment.sut), "load": list(assignment.load)},
        experiment_dir=base,
    )
    (result_dir / "environment.json").write_text(env.model_dump_json(indent=2))

    outcome = ExperimentOutcome(result_dir=result_dir, schedule=schedule)
    for run in schedule.runs:
        run_id = run_id_for(run)
        try:
            record = _run_one(
                spec, run, sut_image, assignment, env.comparability, result_dir,
                cmd=cmd, hdr_builder=hdr_builder, collector_factory=collector_factory,
                sleep=sleep, adapter_factory=adapter_factory,
            )
            outcome.records.append(record)
        except (RunnerError, OSError, ValueError) as exc:
            outcome.errors.append(RunError(run_id, str(exc)))
            (result_dir / "raw" / f"{run_id}.error.json").write_text(
                RunErrorFile(run_id=run_id, index=run.index, message=str(exc)).model_dump_json(
                    indent=2
                )
            )
    return outcome


class RunErrorFile(BaseModel):
    run_id: str
    index: int
    message: str


def _run_one(
    spec: ExperimentSpec,
    run: ScheduledRun,
    sut_image: str,
    assignment: CpuAssignment,
    comparability: str,
    result_dir: Path,
    *,
    cmd: Cmd,
    hdr_builder: HdrBuilder | None,
    collector_factory: Callable[[list[str]], Collector] | None,
    sleep: Callable[[float], None],
    adapter_factory: Callable[[ContainerExec], Any] | None,
) -> RunRecord:
    raw = result_dir / "raw"
    run_id = run_id_for(run)
    base_spec = spec.resolve(run.condition)
    run_spec: RunSpec = base_spec.model_copy(
        update={"target": base_spec.target.model_copy(update={"image": sut_image})}
    )
    compose_file = result_dir / "compose" / f"{run_id}.yaml"
    doc = render_compose(
        run_spec, assignment, {"sut": sut_image, "k6": spec.k6_image}, work_dir=raw
    )
    compose_file.write_text(yaml.safe_dump(doc, sort_keys=False))
    project = f"serverlab-{spec.name}-{result_dir.name}-{run_id}".lower().replace("_", "-")
    dc = ["docker", "compose", "-p", project, "-f", str(compose_file)]
    container = ContainerExec(compose_file, project, "k6", raw)
    adapter = adapter_factory(container) if adapter_factory else K6Adapter(container=container)
    # Only the SUT container: with no names, `docker stats` would watch the whole host.
    collector = (
        collector_factory([f"{project}-sut-1"]) if collector_factory and spec.metrics.enabled
        else None
    )
    started = datetime.now(UTC)
    metrics_stats: dict[str, Any] | None = None
    hdr_stats: dict[str, int] | None = None

    try:
        _must(cmd, [*dc, "up", "-d", "--wait", "sut"], "compose up")
        if spec.warmup_s > 0:
            warm = run_spec.load.model_copy(update={"duration_s": spec.warmup_s})
            wr = adapter.run(
                warm, raw, f"{run_id}.warmup", summary_path=raw / f"{run_id}.warmup.k6summary.json"
            )
            (raw / f"{run_id}.warmup.json").write_text(wr.model_dump_json(indent=2))
        ndjson = raw / f"{run_id}.requests.json"
        if collector is not None:
            collector.start(run_id, result_dir / "metrics" / f"{run_id}.jsonl")
        try:
            result: RunResult = adapter.run(
                run_spec.load, raw, run_id, per_request_path=ndjson,
                summary_path=raw / f"{run_id}.k6summary.json",
            )
        finally:
            if collector is not None:
                stats = collector.stop()
                metrics_stats = stats.model_dump() if hasattr(stats, "model_dump") else dict(stats)
        if hdr_builder is not None and ndjson.exists():
            hdr = raw / f"{run_id}.hdr"
            hdr_stats = hdr_builder(ndjson, hdr)
            result = result.model_copy(update={"hdr_path": hdr})
        if not spec.keep_raw:
            ndjson.unlink(missing_ok=True)
        sleep(spec.cooldown_s)
    finally:
        cmd([*dc, "down", "-v", "--remove-orphans"])

    record = RunRecord(
        experiment=spec.name, condition=run.condition, repetition=run.repetition,
        index=run.index, seed=spec.seed,
        started_at=started.isoformat(), finished_at=datetime.now(UTC).isoformat(),
        sut_image=sut_image, k6_image=spec.k6_image,
        cpus={"sut": list(assignment.sut), "load": list(assignment.load)},
        hdr_stats=hdr_stats, metrics_stats=metrics_stats,
        comparability=comparability, result=result,
    )
    (raw / f"{run_id}.json").write_text(record.model_dump_json(indent=2))
    return record
