"""serverlab CLI."""

from pathlib import Path
from typing import Annotated

import typer

from serverlab.experiment.runner import RunnerError, run_single
from serverlab.experiment.spec import load_spec

app = typer.Typer(no_args_is_help=True, help="ServerLab experiment framework")

EXIT_INVALID_RUN = 3
EXIT_ERROR = 2


@app.command()
def run(
    spec: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="experiment YAML")],
    results: Annotated[Path, typer.Option(help="results root directory")] = Path("results"),
) -> None:
    """Run a single-condition experiment. Exit 3 if the run is invalid (artifacts still written)."""
    try:
        outcome = run_single(load_spec(spec), results, base_dir=spec.resolve().parent.parent)
    except (RunnerError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc
    res = outcome.record.result
    typer.echo(f"results: {outcome.result_dir}")
    typer.echo(
        f"planned={res.planned} completed={res.completed} dropped={res.dropped} "
        f"peak_vus={res.peak_vus} p50={res.latency_ms.p50:.2f}ms p99={res.latency_ms.p99:.2f}ms "
        f"comparability={outcome.record.comparability}"
    )
    if not outcome.valid:
        typer.echo("RUN INVALID:", err=True)
        for reason in res.invalid_reasons:
            typer.echo(f"  - {reason}", err=True)
        raise typer.Exit(EXIT_INVALID_RUN)
    typer.echo("run valid")


@app.command()
def version() -> None:
    """Print version."""
    typer.echo("0.1.0")
