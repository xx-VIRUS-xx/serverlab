"""Python SDK: a thin typed facade over the same code the CLI runs. No logic of its own.

    from serverlab.sdk import Lab
    lab = Lab()
    spec = lab.load_spec("experiments/week2_demo.yaml")
    print(lab.plan(spec).order())        # seeded schedule, no side effects
    outcome = lab.run(spec)              # same lifecycle as `serverlab run`

Running needs Docker (and the pinned k6 image, pulled by compose). Results written by the
SDK are identical to the CLI's and are never overwritten.
"""

from pathlib import Path
from typing import Any

import yaml

from serverlab.analysis.capacity import UtilizationFit
from serverlab.analysis.pool_report import (
    SizeVerdict,
    grade_size,
    sweep_capacities,
    utilization_fit_for,
)
from serverlab.analysis.validate import CheckResult, coverage_checks
from serverlab.experiment.runner import (
    ExperimentOutcome,
    plan_schedule,
    run_experiment,
)
from serverlab.experiment.schedule import Schedule as _Schedule
from serverlab.experiment.spec import ExperimentSpec, load_experiment
from serverlab.experiment.sweeps import SweepOutcome, run_sweeps
from serverlab.results import list_results, read_metrics, result_summary

__all__ = ["Lab", "ExperimentSpec"]


class Lab:
    def __init__(
        self, results_root: str | Path = "results", experiments_dir: str | Path = "experiments",
    ) -> None:
        self.results_root = Path(results_root)
        self.experiments_dir = Path(experiments_dir)

    # specs
    def load_spec(self, spec: str | Path | dict[str, Any]) -> ExperimentSpec:
        if isinstance(spec, dict):
            return ExperimentSpec.model_validate(spec)
        path = Path(spec)
        if not path.is_file() and (self.experiments_dir / path).is_file():
            path = self.experiments_dir / path
        return load_experiment(path)

    def plan(self, spec: ExperimentSpec) -> _Schedule:
        """The seeded, randomized-block schedule. Pure: nothing runs, nothing is written."""
        return plan_schedule(spec)

    # execution (needs Docker)
    def run(self, spec: ExperimentSpec, base_dir: str | Path | None = None) -> ExperimentOutcome:
        return run_experiment(
            spec, self.results_root, base_dir=None if base_dir is None else Path(base_dir)
        )

    def sweep(self, spec: ExperimentSpec, base_dir: str | Path | None = None) -> SweepOutcome:
        return run_sweeps(
            spec, self.results_root, base_dir=None if base_dir is None else Path(base_dir)
        )

    # results (read-only)
    def results(self) -> list[dict[str, Any]]:
        return list_results(self.results_root)

    def result(self, experiment: str, timestamp: str) -> dict[str, Any]:
        return result_summary(self.results_root, experiment, timestamp)

    def metrics(self, experiment: str, timestamp: str, run_id: str, kind: str) -> dict[str, Any]:
        return read_metrics(self.results_root, experiment, timestamp, run_id, kind)

    # checks and analysis
    def validate(self, offline: bool = True) -> list[CheckResult]:
        if not offline:
            raise NotImplementedError("online validation runs Docker: use `serverlab validate`")
        return coverage_checks()

    def analyze_pool(
        self, results_dir: str | Path, calibration_dir: str | Path, max_rate: int = 975
    ) -> tuple[UtilizationFit, list[SizeVerdict]]:
        fit = utilization_fit_for(Path(calibration_dir), max_rate=max_rate)
        verdicts = [
            grade_size(size, sweeps, fit.lo, fit.hi)
            for size, sweeps in sorted(sweep_capacities(Path(results_dir)).items())
        ]
        return fit, verdicts

    @staticmethod
    def dump_spec(spec: ExperimentSpec) -> str:
        return yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False)
