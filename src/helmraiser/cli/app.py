"""Main Typer app — registers all sub-command groups."""

from pathlib import Path

import typer

from helmraiser.cli.commands import register_commands
from helmraiser.cli.monitor import doctor

app = typer.Typer(help="Helmraiser - governed, unattended task runner for pi")

register_commands(app)
app.command("doctor")(doctor)


@app.command()
def abort(
    run_id: str = typer.Argument(..., help="Canonical UUID4 run ID"),
    project_dir: Path = typer.Option(..., "--project-dir", help="Canonical repository root"),
) -> None:
    """Promptly abort one exact governed run."""
    from helmraiser.loop.abort import abort_run

    if not project_dir.is_dir() or not (project_dir / ".git").exists():
        raise typer.BadParameter(f"not a Git repository: {project_dir}")
    try:
        stopped = abort_run(project_dir, run_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    if not stopped:
        raise typer.BadParameter(f"unknown run: {run_id}")
    typer.echo(f"run_id={run_id} status=aborted")


@app.command()
def version():
    """Show Helmraiser version."""
    from rich.console import Console

    from helmraiser import __version__

    Console().print(f"[bold blue]Helmraiser[/bold blue] v{__version__}")
