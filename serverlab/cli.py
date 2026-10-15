"""serverlab CLI."""

import signal
from pathlib import Path
from typing import Annotated

import typer

from serverlab.analysis.pool_report import grade_size, sweep_capacities, utilization_fit_for
from serverlab.analysis.validate import (
    coverage_checks,
    grade_knee,
    knee_checks,
    predicted_capacity,
)
from serverlab.experiment.runner import (
    RunnerError,
    default_collectors,
    plan_schedule,
    run_experiment,
)
from serverlab.experiment.spec import load_experiment
from serverlab.experiment.sweeps import SweepOutcome, run_sweeps
from serverlab.metrics.overhead import OverheadInputError, analyse, load_inputs, render

app = typer.Typer(no_args_is_help=True, help="ServerLab experiment framework")


def _terminate_as_interrupt() -> None:
    """SIGTERM (UI cancel, `kill`) becomes KeyboardInterrupt so teardown runs."""

    def handler(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handler)

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
    _terminate_as_interrupt()
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
    for w in outcome.warnings:
        typer.echo(f"WARNING (environment): {w}", err=True)
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


def _print_sweep(outcome: SweepOutcome) -> None:
    for w in outcome.warnings:
        typer.echo(f"WARNING (environment): {w}", err=True)
    for cs in outcome.conditions:
        k = cs.sweep.knee
        typer.echo(
            f"[{cs.condition} rep {cs.repetition}] seed={cs.seed} "
            f"coarse order: {cs.sweep.coarse_order}"
        )
        typer.echo(f"  bisect rates: {cs.sweep.bisect_rates}")
        for s in cs.sweep.steps:
            m = s.measurement
            ach = "n/a" if m.achieved is None else f"{m.achieved:.1f}"
            p99 = "n/a" if m.p99_ms is None else f"{m.p99_ms:.1f}"
            cpu = "n/a" if m.k6_cpu_pct is None else f"{m.k6_cpu_pct:.0f}%"
            typer.echo(
                f"  rate={m.rate:<5g} {s.status:<13} achieved={ach:<7} p99={p99:<7}ms "
                f"k6cpu={cpu:<5} {s.note}"
            )
        est = "n/a" if k.estimate is None else f"{k.estimate:.1f}"
        typer.echo(f"  knee: {k.status} bracket=[{k.lo}, {k.hi}] estimate={est} {k.warnings}")


@app.command()
def sweep(
    spec: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="spec with `sweep:`")],
    results: Annotated[Path, typer.Option(help="results root directory")] = Path("results"),
) -> None:
    """Coarse log-spaced sweep, then bracketed search for the knee, per condition."""
    _terminate_as_interrupt()
    try:
        outcome = run_sweeps(
            load_experiment(spec), results, base_dir=spec.resolve().parent.parent,
            collectors_factory=default_collectors,
        )
    except (RunnerError, ValueError, KeyError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc
    typer.echo(f"results: {outcome.result_dir}  comparability={outcome.comparability}")
    _print_sweep(outcome)


@app.command()
def validate(
    offline: Annotated[bool, typer.Option(help="statistics checks only (no Docker/k6)")] = False,
    spec: Annotated[Path, typer.Option(help="synthetic validation spec")] = Path(
        "experiments/validate_synthetic.yaml"
    ),
    results: Annotated[Path, typer.Option(help="results root directory")] = Path("results"),
) -> None:
    """Validate the tool itself. Exits non-zero if any check fails (docs/predictions.md)."""
    checks = coverage_checks()
    if not offline:
        try:
            experiment = load_experiment(spec)
            outcome = run_sweeps(
                experiment, results, base_dir=spec.resolve().parent.parent,
                collectors_factory=default_collectors,
            )
        except (RunnerError, ValueError, KeyError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(EXIT_ERROR) from exc
        typer.echo(f"results: {outcome.result_dir}  comparability={outcome.comparability}")
        _print_sweep(outcome)
        graded = [
            grade_knee(
                cs.condition,
                predicted_capacity(
                    next(c.target_env for c in experiment.conditions if c.name == cs.condition),
                    experiment.target.env,
                ),
                cs.sweep.knee,
                len(cs.sweep.steps),
            )
            for cs in outcome.conditions
        ]
        checks = [*knee_checks(graded), *checks]
    for c in checks:
        typer.echo(f"[{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
    failed = [c for c in checks if not c.passed]
    if failed:
        typer.echo(f"VALIDATION FAILED: {len(failed)} check(s)", err=True)
        raise typer.Exit(EXIT_INVALID_RUN)
    typer.echo("offline validation passed" if offline else "validation passed")


@app.command("analyze-pool")
def analyze_pool(
    results_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    calibration: Annotated[
        Path, typer.Option(exists=True, file_okay=False, help="calibration sweep results dir")
    ],
    max_rate: Annotated[int, typer.Option(help="highest offered rate used for the fit")] = 975,
) -> None:
    """Grade a pool sweep against the committed predictions (docs/predictions.md)."""
    fit = utilization_fit_for(calibration, max_rate=max_rate)
    typer.echo(
        f"API ceiling A (utilization law, {fit.n} steps, R^2 {fit.r_squared:.3f}): "
        f"{fit.ceiling:.0f} req/s, 95% interval [{fit.lo:.0f}, {fit.hi:.0f}]; "
        f"fixed {fit.fixed_us_per_s / 1e4:.1f}% CPU, marginal {fit.marginal_us_per_req:.0f} us/req"
    )
    verdicts = [
        grade_size(size, sweeps, fit.lo, fit.hi)
        for size, sweeps in sorted(sweep_capacities(results_dir).items())
    ]
    for v in verdicts:
        label = {True: "PASS", False: "FAIL", None: "not graded"}[v.passed]
        err = "n/a" if v.error_pct is None else f"{v.error_pct:+.1f}%"
        med = "n/a" if v.c_meas_median is None else f"{v.c_meas_median:.0f}"
        rng = (
            "n/a" if v.c_meas_min is None or v.c_meas_max is None
            else f"{v.c_meas_min:.0f}..{v.c_meas_max:.0f}"
        )
        typer.echo(
            f"[{label}] pool {v.pool_size:<3} C_pool={v.c_pool:<6.0f} regime={v.regime:<12} "
            f"measured median={med} range={rng} error={err} | {v.note}"
        )
        for s in v.sweeps:
            knee = "n/a" if s.p99_knee_estimate is None else f"{s.p99_knee_estimate:.0f}"
            typer.echo(
                f"        rep {s.repetition}: plateau="
                f"{'n/a' if s.c_meas is None else round(s.c_meas)} "
                f"({s.saturated_intervals} saturated intervals) p99-knee (secondary)={knee}"
            )
    if any(v.passed is False for v in verdicts):
        raise typer.Exit(EXIT_INVALID_RUN)


@app.command()
def ui(
    port: Annotated[int, typer.Option(help="port on 127.0.0.1")] = 8765,
    root: Annotated[Path, typer.Option(help="project root with experiments/ results/ docs/")] = (
        Path(".")
    ),
    open_browser: Annotated[bool, typer.Option("--open/--no-open")] = True,
) -> None:
    """Start the local web UI (loopback only)."""
    try:
        import uvicorn

        from serverlab.ui.server import create_app
    except ImportError as exc:  # pragma: no cover
        typer.echo("the UI needs extras: pip install 'serverlab[ui]'", err=True)
        raise typer.Exit(EXIT_ERROR) from exc
    application = create_app(root, port=port)
    url = f"http://127.0.0.1:{port}/"
    typer.echo(f"serverlab UI on {url}  (local only; Ctrl+C to stop)")
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(application, host="127.0.0.1", port=port, log_level="warning")


@app.command()
def version() -> None:
    """Print version."""
    typer.echo("0.1.0")
