"""Main Typer app — registers all sub-command groups."""

from pathlib import Path

import typer

from helmraiser.cli.commands import register_commands
from helmraiser.cli.monitor import doctor

app = typer.Typer(help="Helmraiser - governed, unattended task runner for pi")

register_commands(app)
app.command("doctor")(doctor)


# A task id starts with the project key, which starts with a dash.
@app.command(context_settings={"ignore_unknown_options": True})
def abort(
    run_id: str = typer.Argument(
        ..., help="Canonical UUID4 run ID, or the id of a task with an unpicked pair"
    ),
    project_dir: Path = typer.Option(..., "--project-dir", help="Canonical repository root"),
) -> None:
    """Promptly abort one exact governed run. For one run of an unpicked pair, or the
    pair's task, abort both of the pair's runs."""
    from helmraiser.loop.abort import abort_runs

    if not project_dir.is_dir() or not (project_dir / ".git").exists():
        raise typer.BadParameter(f"not a Git repository: {project_dir}")
    try:
        stopped = abort_runs(project_dir, run_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    if not stopped:
        raise typer.BadParameter(f"unknown run: {run_id}")
    for stopped_id in stopped:
        typer.echo(f"run_id={stopped_id} status=aborted")


@app.command()
def version():
    """Show Helmraiser version."""
    from rich.console import Console

    from helmraiser import __version__

    Console().print(f"[bold blue]Helmraiser[/bold blue] v{__version__}")
