"""Owner-facing command to land an approved pull-request run through CI."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import NoReturn

import typer

from helmraiser.cli import commands
from helmraiser.loop import landing
from helmraiser.task.loader import TaskLoadError


def land(
    run: str = typer.Argument(..., help="Run id (or unique prefix)"),
    timeout: float = typer.Option(1800, "--timeout", help="CI deadline in seconds"),
    interval: float = typer.Option(
        10, "--interval", help="CI polling interval in seconds (0 allowed)"
    ),
    no_ci: bool = typer.Option(
        False, "--no-ci", help="Accept absent checks, never bypass existing checks"
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Approve a PR run, wait for CI, and non-force push its head into its origin base."""

    def fail(message: str, *, as_json: bool = as_json, code: int = 2) -> NoReturn:
        typer.echo(
            json.dumps({"landed": False, "error": message, "steps": []})
            if as_json
            else f"Error: {message}"
        )
        raise typer.Exit(code)

    for name, value in (("timeout", timeout), ("interval", interval)):
        if not math.isfinite(value) or value < 0:
            fail(f"--{name} must be finite and nonnegative")
    config = commands.load_config()
    ledger = commands._ledger(data_dir)
    row = commands._find_run(ledger, run, as_json=as_json, failure=fail)
    run_id = str(row["run_id"])
    if row["delivery"] != "pr":
        fail(f"run {run_id} has delivery {row['delivery']}, not pr")
    pending = ledger.pending_approval(run_id)
    if row["status"] == "awaiting_approval":
        if pending is None or pending["kind"] != "merge":
            kind = pending["kind"] if pending else "none"
            fail(f"run {run_id} is awaiting_approval with pending approval {kind}, not merge")
    elif row["status"] != "landed" or not row.get("pr_url"):
        fail(
            f"run {run_id} has status {row['status']}; requires awaiting_approval with merge approval or landed with pr_url"
        )
    try:
        row, task = commands._awaiting_run(
            ledger,
            run_id,
            config,
            as_json=as_json,
            statuses=("awaiting_approval", "landed"),
            failure=fail,
        )
    except TaskLoadError as exc:
        fail(str(exc))
    manager = commands.worktree_manager(task.project)
    manager.command_timeout = landing.COMMAND_TIMEOUT

    def approve() -> tuple[str | None, str]:
        return commands._approve_merge(
            ledger,
            row,
            task,
            pending,
            None,
            commands._data_dir(data_dir),
            manager=manager,
            push=landing.push_branch,
            deliver_pr=lambda base, branch, body: landing.open_pull_request(
                task.project, base=base, head=branch, title=task.title, body=body
            ),
        )

    def progress(step: dict[str, str]) -> None:
        typer.echo(f"{step['name']}: {step['status']}: {step['detail']}")

    result = landing.land_run(
        row,
        task.project,
        manager,
        approve,
        timeout=timeout,
        interval=interval,
        no_ci=no_ci,
        progress=None if as_json else progress,
    )
    if as_json:
        typer.echo(json.dumps(result.payload))
    elif result.exit_code == 0:
        typer.echo(f"{result.payload['pr_url']}\n{result.payload['head']}")
    raise typer.Exit(result.exit_code)
