"""Single-condition run lifecycle: build, capture env, up, warmup, run, cooldown, teardown.

Docker and the load adapter are injected so the lifecycle is unit-testable without either.
Results are written under results/<experiment>/<timestamp>/ (plan.md s5) and are never
overwritten: a fresh directory is created per run.
"""

import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from serverlab.env.detect import capture_environment
from serverlab.experiment.spec import RunSpec
from serverlab.load.base import RunResult
from serverlab.load.hdr import build_hdr_from_json, write_hdr
from serverlab.load.k6 import ContainerExec, K6Adapter
from serverlab.topology.compose import render_compose
from serverlab.topology.cpuset import assign_cpusets

CONDITION = "main"
Cmd = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
HdrBuilder = Callable[[Path, Path], dict[str, int]]  # (ndjson, hdr out) -> value accounting


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
    seed: int
    started_at: str
    finished_at: str
    sut_image: str
    k6_image: str
    cpus: dict[str, list[int]]
    hdr_stats: dict[str, int] | None = None
    comparability: str
    result: RunResult


@dataclass(frozen=True)
class Outcome:
    result_dir: Path
    record: RunRecord

    @property
    def valid(self) -> bool:
        return self.record.result.valid


def _must(cmd: Cmd, argv: Sequence[str], what: str) -> str:
    proc = cmd(argv)
    if proc.returncode != 0:
        raise RunnerError(f"{what} failed (exit {proc.returncode}): {proc.stderr.strip()[:400]}")
    return proc.stdout.strip()


def _resolve_sut_image(spec: RunSpec, base: Path, cmd: Cmd) -> str:
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


def run_single(
    spec: RunSpec,
    results_root: Path,
    *,
    base_dir: Path | None = None,
    cmd: Cmd = shell,
    hdr_builder: HdrBuilder | None = build_hdr_file,
    sleep: Callable[[float], None] = time.sleep,
    adapter_factory: Callable[[ContainerExec], Any] | None = None,
) -> Outcome:
    base = Path.cwd() if base_dir is None else base_dir
    result_dir = _fresh_dir(results_root, spec.name)
    raw = result_dir / "raw"
    raw.mkdir()
    (result_dir / "spec.yaml").write_text(yaml.safe_dump(spec.model_dump(mode="json")))
    started = datetime.now(UTC)

    sut_image = _resolve_sut_image(spec, base, cmd)
    pinned_target = spec.target.model_copy(update={"image": sut_image})
    run_spec = spec.model_copy(update={"target": pinned_target})

    # Total CPUs for validation = what the containers can actually see (Docker VM on macOS).
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

    compose_file = result_dir / "compose.yaml"
    doc = render_compose(
        run_spec, assignment, {"sut": sut_image, "k6": spec.k6_image}, work_dir=raw
    )
    compose_file.write_text(yaml.safe_dump(doc, sort_keys=False))
    project = f"serverlab-{spec.name}-{result_dir.name}".lower()
    dc = ["docker", "compose", "-p", project, "-f", str(compose_file)]
    container = ContainerExec(compose_file, project, "k6", raw)
    adapter = adapter_factory(container) if adapter_factory else K6Adapter(container=container)

    try:
        _must(cmd, [*dc, "up", "-d", "--wait", "sut"], "compose up")
        if spec.warmup_s > 0:
            warm = spec.load.model_copy(update={"duration_s": spec.warmup_s})
            wr = adapter.run(warm, raw, "warmup", summary_path=raw / "warmup.k6summary.json")
            (raw / "warmup.json").write_text(wr.model_dump_json(indent=2))
        run_id = f"run_{CONDITION}_1"
        hdr_stats: dict[str, int] | None = None
        ndjson = raw / f"{run_id}.requests.json"
        result: RunResult = adapter.run(
            spec.load, raw, run_id, per_request_path=ndjson,
            summary_path=raw / f"{run_id}.k6summary.json",
        )
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
        experiment=spec.name, condition=CONDITION, repetition=1, seed=spec.seed,
        started_at=started.isoformat(), finished_at=datetime.now(UTC).isoformat(),
        sut_image=sut_image, k6_image=spec.k6_image,
        cpus={"sut": list(assignment.sut), "load": list(assignment.load)},
        hdr_stats=hdr_stats, comparability=env.comparability, result=result,
    )
    (raw / f"{run_id}.json").write_text(record.model_dump_json(indent=2))
    return Outcome(result_dir, record)
