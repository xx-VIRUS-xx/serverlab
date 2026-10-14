"""serverlab CLI."""

from pathlib import Path
from typing import Annotated

import typer

from serverlab.analysis.validate import coverage_checks
from serverlab.experiment.runner import RunnerError, plan_schedule, run_experiment
from serverlab.experiment.spec import load_experiment
from serverlab.metrics.overhead import OverheadInputError, analyse, load_inputs, render

app = typer.Typer(no_args_is_help=True, help="ServerLab experiment framework")

EXIT_ERROR = 2
EXIT_INVALID_RUN = 3
EXIT_INSUFFICIENT = 3


@app.command()
def run(
    spec: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="experiment YAML")],
    results: Annotated[Path, typer.Option(help="results root directory")] = Path("results"),
    dry_run: Annotated[bool, typer.Option(help="print the schedule and exit")] = False,
) -> None:
    """Run an experiment. Exit 3 if any run is invalid or errored (all artifacts still written)."""
    try:
        experiment = load_experiment(spec)
        if dry_run:
            sched = plan_schedule(experiment)
            typer.echo(f"seed={sched.seed} method={sched.method} repetitions={sched.repetitions}")
            typer.echo(f"order: {sched.order()}")
            return
        outcome = run_experiment(experiment, results, base_dir=spec.resolve().parent.parent)
    except (RunnerError, ValueError, KeyError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc
    typer.echo(f"results: {outcome.result_dir}")
    typer.echo(f"order: {outcome.schedule.order()}")
    for rec in sorted(outcome.records, key=lambda r: r.index):
        res = rec.result
        typer.echo(
            f"#{rec.index} {rec.condition}/{rec.repetition} "
            f"{'valid' if res.valid else 'INVALID'} planned={res.planned} dropped={res.dropped} "
            f"p50={res.latency_ms.p50:.2f}ms p99={res.latency_ms.p99:.2f}ms"
        )
        for reason in res.invalid_reasons:
            typer.echo(f"    - {reason}", err=True)
    for err in outcome.errors:
        typer.echo(f"ERROR {err.run_id}: {err.message}", err=True)
    typer.echo(f"comparability={outcome.records[0].comparability}" if outcome.records else "")
    if not outcome.all_valid:
        raise typer.Exit(EXIT_INVALID_RUN)
    typer.echo("all runs valid")


@app.command()
def overhead(
    results_dir: Annotated[
        Path, typer.Argument(exists=True, file_okay=False, help="finished experiment result dir")
    ],
) -> None:
    """Collector overhead: SUT CPU utilization with collector-off vs collector-on.

    Exit 0 on a verdict, 2 on bad input, 3 if fewer than 3 included runs per condition.
    """
    try:
        params, sources = load_inputs(results_dir)
        report = analyse(params, sources)
    except OverheadInputError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc
    for line in render(report):
        typer.echo(line)
    if report.insufficient:
        raise typer.Exit(EXIT_INSUFFICIENT)


@app.command()
def validate(
    offline: Annotated[bool, typer.Option(help="statistics checks only (no Docker/k6)")] = False,
) -> None:
    """Validate the tool itself. Exits non-zero if any check fails (docs/predictions.md)."""
    checks = coverage_checks()
    for c in checks:
        typer.echo(f"[{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
    if not offline:
        typer.echo("[SKIP] knee recovery on the synthetic target: not wired yet (L-10)", err=True)
        raise typer.Exit(EXIT_ERROR)
    failed = [c for c in checks if not c.passed]
    if failed:
        typer.echo(f"VALIDATION FAILED: {len(failed)} check(s)", err=True)
        raise typer.Exit(EXIT_INVALID_RUN)
    typer.echo("offline validation passed")


@app.command()
def version() -> None:
    """Print version."""
    typer.echo("0.1.0")
