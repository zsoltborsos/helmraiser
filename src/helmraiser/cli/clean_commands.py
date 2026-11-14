"""Manual cleanup of failed run worktrees, with machine-readable results."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from helmraiser.cli import commands
from helmraiser.loop.cleanup import clean_worktrees


def clean(
    project: Path | None = typer.Option(None, "--project", help="Only clean this project"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report without removing worktrees"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Remove clean, superseded or old failed/aborted worktrees; keep branches."""
    try:
        config = commands.load_config()
    except ValueError as exc:
        commands._fail(str(exc), as_json=as_json, code=commands.EXIT_VALIDATION)
    runs = clean_worktrees(
        commands._ledger(data_dir),
        config=config,
        project=project.expanduser() if project is not None else None,
        dry_run=dry_run,
    )
    if as_json:
        typer.echo(json.dumps({"dry_run": dry_run, "runs": runs}))
        return
    for row in runs:
        detail = f"; {row['detail']}" if row["detail"] else ""
        typer.echo(
            f"{row['action']} {row['project']} {row['run_id']} "
            f"({row['reason']}) {row['worktree_path']}{detail}"
        )
    counts = {
        action: sum(r["action"] == action for r in runs)
        for action in ("removed", "would_remove", "kept")
    }
    typer.echo(
        f"{counts['removed']} removed, {counts['would_remove']} would remove, {counts['kept']} kept"
    )
