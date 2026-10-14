"""SUT CPU overhead of the out-of-band metrics collector (week2 A-4).

Analyses ONE finished experiment whose conditions are exactly `collector-off` and
`collector-on`. Pure analysis functions plus a loader for the result directory layout:
spec.yaml and raw/run_<cond>_<n>.json (RunRecord JSON with `sut_cpu_usec` / `sut_cpu_note`).

Numbers in the test fixtures (tests/metrics/test_overhead.py) are test inputs only,
written to exercise the logic. They are not measurements.

Runs with result.valid false or sut_cpu_usec null are listed as excluded, with the reason,
and never enter the statistics. No bootstrap and no smoothing here (Week 3 does statistics).
"""

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from serverlab.experiment.spec import ExperimentSpec

OFF_CONDITION = "collector-off"
ON_CONDITION = "collector-on"
THRESHOLD_PCT = 2.0
MIN_INCLUDED_RUNS = 3
OVERLAP_NOTE = "(on/off ranges overlap: difference not distinguishable from run-to-run noise)"

_RUN_FILE = re.compile(r"^run_(?P<cond>[A-Za-z0-9-]+)_(?P<rep>\d+)\.json$")


class OverheadInputError(ValueError):
    """Missing, malformed or incomplete result directory (CLI exit 2)."""


class _Latency(BaseModel):
    model_config = ConfigDict(extra="ignore")
    p50: float
    p99: float


class _Result(BaseModel):
    model_config = ConfigDict(extra="ignore")
    valid: bool
    invalid_reasons: list[str] = Field(default_factory=list)
    latency_ms: _Latency


class OverheadRun(BaseModel):
    """The subset of RunRecord this analysis needs."""

    model_config = ConfigDict(extra="ignore")
    condition: str
    repetition: int
    result: _Result
    sut_cpu_usec: int | None = None
    sut_cpu_note: str | None = None


@dataclass(frozen=True)
class ExperimentParams:
    name: str
    duration_s: int
    sut_cpus: int


@dataclass(frozen=True)
class IncludedRun:
    file: str
    condition: str
    repetition: int
    utilization_pct: float
    p99_ms: float


@dataclass(frozen=True)
class Excluded:
    file: str
    condition: str
    repetition: int
    reason: str


@dataclass(frozen=True)
class ConditionStats:
    condition: str
    runs: tuple[IncludedRun, ...]

    @property
    def n(self) -> int:
        return len(self.runs)

    @property
    def utilizations(self) -> list[float]:
        return [r.utilization_pct for r in self.runs]

    @property
    def util_min(self) -> float:
        return min(self.utilizations)

    @property
    def util_median(self) -> float:
        return statistics.median(self.utilizations)

    @property
    def util_max(self) -> float:
        return max(self.utilizations)

    @property
    def p99_median(self) -> float:
        return statistics.median([r.p99_ms for r in self.runs])


@dataclass(frozen=True)
class OverheadReport:
    params: ExperimentParams
    off: ConditionStats
    on: ConditionStats
    excluded: tuple[Excluded, ...]
    overhead_pct: float | None
    ranges_overlap: bool | None
    verdict: str

    @property
    def insufficient(self) -> bool:
        return self.off.n < MIN_INCLUDED_RUNS or self.on.n < MIN_INCLUDED_RUNS


def utilization_pct(sut_cpu_usec: int, duration_s: int, sut_cpus: int) -> float:
    """SUT CPU time as a percentage of the SUT cpuset capacity over the measured window."""
    if duration_s <= 0 or sut_cpus <= 0:
        raise OverheadInputError(
            f"duration_s and SUT cpu count must be > 0 ({duration_s}, {sut_cpus})"
        )
    return 100.0 * sut_cpu_usec / (duration_s * 1_000_000 * sut_cpus)


def _exclusion_reasons(run: OverheadRun) -> list[str]:
    reasons: list[str] = []
    if not run.result.valid:
        detail = "; ".join(run.result.invalid_reasons)
        reasons.append("result.valid=false" + (f": {detail}" if detail else ""))
    if run.sut_cpu_usec is None:
        note = f" ({run.sut_cpu_note})" if run.sut_cpu_note else ""
        reasons.append(f"sut_cpu_usec is null{note}")
    return reasons


def analyse(params: ExperimentParams, sources: Sequence[tuple[str, OverheadRun]]) -> OverheadReport:
    """Pure analysis. `sources` pairs each raw file name with its parsed run."""
    included: dict[str, list[IncludedRun]] = {OFF_CONDITION: [], ON_CONDITION: []}
    excluded: list[Excluded] = []
    for file, run in sorted(sources, key=lambda s: (s[1].condition, s[1].repetition)):
        if run.condition not in included:
            raise OverheadInputError(f"{file}: unexpected condition {run.condition!r}")
        reasons = _exclusion_reasons(run)
        usec = run.sut_cpu_usec
        if usec is None or reasons:
            excluded.append(Excluded(file, run.condition, run.repetition, "; ".join(reasons)))
            continue
        included[run.condition].append(
            IncludedRun(
                file=file,
                condition=run.condition,
                repetition=run.repetition,
                utilization_pct=utilization_pct(usec, params.duration_s, params.sut_cpus),
                p99_ms=run.result.latency_ms.p99,
            )
        )

    off = ConditionStats(OFF_CONDITION, tuple(included[OFF_CONDITION]))
    on = ConditionStats(ON_CONDITION, tuple(included[ON_CONDITION]))
    if off.n < MIN_INCLUDED_RUNS or on.n < MIN_INCLUDED_RUNS:
        verdict = (
            f"INSUFFICIENT DATA (need >= {MIN_INCLUDED_RUNS} included runs per condition; "
            f"{OFF_CONDITION} n={off.n}, {ON_CONDITION} n={on.n})"
        )
        return OverheadReport(params, off, on, tuple(excluded), None, None, verdict)

    overhead = on.util_median - off.util_median
    overlap = on.util_min <= off.util_max and off.util_min <= on.util_max
    side = "BELOW" if overhead < THRESHOLD_PCT else "ABOVE"
    verdict = f"{side} {THRESHOLD_PCT:g}% threshold"
    if overlap:
        verdict += f" {OVERLAP_NOTE}"
    return OverheadReport(params, off, on, tuple(excluded), overhead, overlap, verdict)


def load_inputs(results_dir: Path) -> tuple[ExperimentParams, list[tuple[str, OverheadRun]]]:
    """Read spec.yaml and raw/run_<cond>_<n>.json from one experiment result directory."""
    spec_path = results_dir / "spec.yaml"
    if not spec_path.is_file():
        raise OverheadInputError(f"no spec.yaml in {results_dir}")
    try:
        spec = ExperimentSpec.model_validate(yaml.safe_load(spec_path.read_text()))
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise OverheadInputError(f"spec.yaml is not a valid ExperimentSpec: {exc}") from exc

    names = set(spec.condition_names)
    if names != {OFF_CONDITION, ON_CONDITION}:
        raise OverheadInputError(
            f"spec.yaml must have exactly the conditions {OFF_CONDITION!r} and "
            f"{ON_CONDITION!r}; found {sorted(names)}"
        )
    if not spec.cpus.sut:
        raise OverheadInputError("spec.yaml cpus.sut is empty")
    params = ExperimentParams(
        name=spec.name, duration_s=spec.load.duration_s, sut_cpus=len(spec.cpus.sut)
    )

    raw = results_dir / "raw"
    if not raw.is_dir():
        raise OverheadInputError(f"no raw/ directory in {results_dir}")
    sources: list[tuple[str, OverheadRun]] = []
    for path in sorted(raw.glob("run_*.json")):
        match = _RUN_FILE.match(path.name)
        if match is None:  # warmup, error and k6 summary files are not runs
            continue
        try:
            run = OverheadRun.model_validate_json(path.read_text())
        except (OSError, ValueError) as exc:
            raise OverheadInputError(f"{path.name}: not a RunRecord: {exc}") from exc
        if run.condition != match["cond"] or run.repetition != int(match["rep"]):
            raise OverheadInputError(f"{path.name}: condition/repetition do not match file name")
        sources.append((path.name, run))
    return params, sources


def render(report: OverheadReport) -> list[str]:
    """Human-readable lines: the numbers first, the verdict last."""
    p = report.params
    lines = [
        f"experiment: {p.name}  duration_s={p.duration_s}  sut_cpus={p.sut_cpus}",
        "utilization_pct = 100 * sut_cpu_usec / (duration_s * 1e6 * sut_cpus)",
    ]
    for stats in (report.off, report.on):
        lines.append(f"{stats.condition}: n={stats.n} included")
        for r in stats.runs:
            lines.append(
                f"  rep {r.repetition} ({r.file}): utilization_pct={r.utilization_pct:.3f} "
                f"p99_ms={r.p99_ms:.2f}"
            )
        if stats.n:
            lines.append(
                f"  utilization_pct min={stats.util_min:.3f} median={stats.util_median:.3f} "
                f"max={stats.util_max:.3f}; median p99_ms={stats.p99_median:.2f}"
            )
    if report.excluded:
        lines.append(f"excluded: {len(report.excluded)}")
        for e in report.excluded:
            lines.append(f"  {e.file} ({e.condition} rep {e.repetition}): {e.reason}")
    if report.overhead_pct is not None:
        lines.append(
            "overhead_pct_of_sut_capacity = median(on) - median(off) = "
            f"{report.on.util_median:.3f} - {report.off.util_median:.3f} = "
            f"{report.overhead_pct:.3f} percentage points"
        )
    lines.append(f"Verdict: {report.verdict}")
    return lines
