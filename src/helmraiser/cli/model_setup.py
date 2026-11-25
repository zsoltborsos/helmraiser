"""Model readiness at model-calling CLI boundaries."""

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import typer

from helmraiser.config import missing_models
from helmraiser.ledger.db import Ledger, ProjectScope
from helmraiser.task.loader import parse_task
from helmraiser.task.model import TaskSpec

WORK_MODEL_ROLES = ("plan", "implement", "fallback", "review")


def require_work_models(
    config: dict[str, Any],
    base: Path,
    scope: ProjectScope,
    *,
    max_runs: int | None = None,
    dry_run: bool = False,
    as_json: bool = False,
) -> None:
    """Inspect existing queue snapshots without creating or writing to a ledger."""
    if not missing_models(config, WORK_MODEL_ROLES):
        return
    db = base / "helmraiser.db"
    limit = 1 if dry_run else max_runs
    selected = 0
    missing: set[str] = set()
    if db.exists():
        with Ledger(db, read_only=True) as ledger:
            excluded: set[str] = set()
            while not dry_run and (limit is None or selected < limit):
                run = ledger.next_resumable(scope=scope, config=config, exclude=excluded)
                if run is None:
                    break
                excluded.add(str(run["run_id"]))
                row = ledger.get_task(str(run["task_id"]))
                if row is not None:
                    task = parse_task(str(row["snapshot"]), Path(str(row["source_path"])), config)
                    missing.update(missing_models(config, WORK_MODEL_ROLES, task))
                    selected += 1
            excluded = set()
            while limit is None or selected < limit:
                row = ledger.next_queued(scope=scope, config=config, exclude=excluded)
                if row is None:
                    break
                excluded.add(str(row["task_id"]))
                task = parse_task(str(row["snapshot"]), Path(str(row["source_path"])), config)
                missing.update(missing_models(config, WORK_MODEL_ROLES, task))
                selected += 1
    require_models(
        config,
        [role for role in WORK_MODEL_ROLES if role in missing] if selected else WORK_MODEL_ROLES,
        as_json=as_json,
    )


def models_not_configured_message(missing: Iterable[str]) -> str:
    return (
        f"models not configured: {', '.join(missing)}. Set them in ~/.helmraiser/config.json "
        "(see examples/config.json for a tried-and-working setup, or run the /helmraiser-setup skill)."
    )


def require_models(
    config: dict[str, Any],
    roles: Iterable[str],
    *,
    task: TaskSpec | None = None,
    as_json: bool = False,
) -> None:
    missing = missing_models(config, roles, task)
    if missing:
        typer.echo(
            json.dumps({"error": "models_not_configured", "missing": missing})
            if as_json
            else models_not_configured_message(missing)
        )
        raise typer.Exit(2)
