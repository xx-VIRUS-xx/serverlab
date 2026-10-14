"""Run a rate sweep per condition: coarse log-spaced stage, then bracketed bisection.

Every step is a full clean-state run (fresh compose stack, short warmup) through the same
`run_one` as ordinary experiments, so validity rules and artifacts are identical. A step that
errors becomes an invalid-other measurement; nothing is hidden or retried.
"""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from serverlab.analysis.knee import StepMeasurement
from serverlab.analysis.sweep import SweepResult, run_sweep
from serverlab.experiment.runner import (
    Cmd,
    HdrBuilder,
    RunnerError,
    RunRecord,
    build_hdr_file,
    prepare_experiment,
    run_one,
    shell,
)
from serverlab.experiment.schedule import ScheduledRun
from serverlab.experiment.spec import ExperimentSpec
from serverlab.load.k6 import ContainerExec


class ConditionSweep(BaseModel):
    condition: str
    seed: int
    sweep: SweepResult
    records: list[RunRecord]


class SweepOutcome(BaseModel):
    result_dir: Path
    comparability: str
    conditions: list[ConditionSweep]


def measurement_from_record(rec: RunRecord, duration_s: int, load_cpus: int) -> StepMeasurement:
    res = rec.result
    # CPU over the container's real lifetime: past saturation k6 keeps draining VUs long after
    # the nominal duration, so dividing by duration_s would overstate its load.
    k6_pct = (
        None if res.k6_cpu_usec is None or not res.k6_wall_s
        else 100.0 * res.k6_cpu_usec / (res.k6_wall_s * 1_000_000 * load_cpus)
    )
    return StepMeasurement(
        rate=rec.offered_rate,
        achieved=res.completed / duration_s,
        p99_ms=res.latency_ms.p99 if res.completed > 0 else None,
        valid=res.valid,
        invalid_reasons=list(res.invalid_reasons),
        k6_cpu_pct=k6_pct,
    )


def run_sweeps(
    spec: ExperimentSpec,
    results_root: Path,
    *,
    base_dir: Path | None = None,
    cmd: Cmd = shell,
    hdr_builder: HdrBuilder | None = build_hdr_file,
    sleep: Callable[[float], None] = time.sleep,
    adapter_factory: Callable[[ContainerExec], Any] | None = None,
    on_step: Callable[[str, StepMeasurement], None] | None = None,
) -> SweepOutcome:
    cfg = spec.sweep
    if cfg is None:
        raise RunnerError("spec has no `sweep:` section")
    base = Path.cwd() if base_dir is None else base_dir
    prep = prepare_experiment(spec, results_root, None, base=base, cmd=cmd)
    load_cpus = len(spec.cpus.load)
    out: list[ConditionSweep] = []

    for cond_idx, cond in enumerate(spec.conditions):
        seed = spec.seed + cond_idx
        records: list[RunRecord] = []

        def measure(
            rate: int, name: str = cond.name, records: list[RunRecord] = records
        ) -> StepMeasurement:
            step_no = len(records) + 1
            step_load = spec.load.model_copy(
                update={"rate": rate, "duration_s": cfg.step_duration_s}
            )
            step_spec = spec.model_copy(update={
                "load": step_load, "warmup_s": cfg.step_warmup_s, "cooldown_s": 0,
                "keep_raw": False,
            })
            run = ScheduledRun(index=step_no - 1, condition=name, repetition=step_no, block=0)
            try:
                rec = run_one(
                    step_spec, run, prep.sut_image, prep.assignment, prep.comparability,
                    prep.result_dir, cmd=cmd, hdr_builder=hdr_builder, collector_factory=None,
                    sleep=sleep, adapter_factory=adapter_factory,
                )
            except (RunnerError, OSError, ValueError) as exc:
                m = StepMeasurement(
                    rate=rate, achieved=None, p99_ms=None, valid=False,
                    invalid_reasons=[f"run error: {exc}"],
                )
            else:
                records.append(rec)
                m = measurement_from_record(rec, cfg.step_duration_s, load_cpus)
            if on_step is not None:
                on_step(name, m)
            return m

        sweep = run_sweep(
            measure, cfg.rate_lo, cfg.rate_hi, seed, cfg.coarse_points, cfg.max_bisect,
            cfg.width_target_pct,
        )
        out.append(ConditionSweep(condition=cond.name, seed=seed, sweep=sweep, records=records))
        (prep.result_dir / f"sweep_{cond.name}.json").write_text(out[-1].model_dump_json(indent=2))

    return SweepOutcome(
        result_dir=prep.result_dir, comparability=prep.comparability, conditions=out
    )
