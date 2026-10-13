"""serverlab CLI. `run` is implemented in L-3."""

import typer

app = typer.Typer(no_args_is_help=True, help="ServerLab experiment framework")


@app.command()
def run(spec: str) -> None:
    """Run a single-condition experiment spec."""
    raise typer.Exit(code=2)


@app.command()
def version() -> None:
    """Print version."""
    typer.echo("0.1.0")
