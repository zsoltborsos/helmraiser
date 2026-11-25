"""Definition-only experiment lifecycle; no scheduling or arm application."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import typer

from helmraiser.config import load_config
from helmraiser.ledger.db import Ledger, default_data_dir, default_db_path
from helmraiser.loop.experiments import validate_experiment

experiment_app = typer.Typer(help="Manage live experiment definitions")


def _ledger(data_dir: Path | None) -> Ledger:
    base = data_dir.expanduser() if data_dir is not None else default_data_dir()
    return Ledger(default_db_path(base))


def _error(message: str, as_json: bool) -> NoReturn:
    typer.echo(json.dumps({"error": message}) if as_json else message)
    raise typer.Exit(1)


def _available(ledger: Ledger) -> None:
    if not ledger.experiments_available:
        raise ValueError(ledger.upgrade_pending)


def _date(value: float | None) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else "unset"


def _show(ledger: Ledger, record: dict[str, Any] | None, as_json: bool) -> None:
    counts = (
        ledger.experiment_pair_counts(record["id"])
        if record
        else {
            "total": 0,
            "picked": 0,
            "judged_wins": {"A": 0, "B": 0},
            "failure_wins": {"A": 0, "B": 0},
            "ties": 0,
            "unpicked": 0,
        }
    )
    if as_json:
        typer.echo(json.dumps({"experiment": record, "pairs": counts}))
    elif record is None:
        typer.echo("none")
    else:
        typer.echo(f"experiment: {record['name']} ({record['status']})")
        for arm in ("A", "B"):
            typer.echo(f"  {arm}: {json.dumps(record['arms'][arm], sort_keys=True)}")
        typer.echo(f"started_at (UTC): {_date(record['started_at'])}")
        typer.echo(f"end_pairs: {record['end_pairs']}; end_at (UTC): {_date(record['end_at'])}")
        typer.echo(f"quota_brake: {record['quota_brake']}")
        typer.echo(f"exclude_projects: {json.dumps(record['exclude_projects'])}")
        if record["end_reason"] is not None:
            typer.echo(f"end_reason: {record['end_reason']}")
        for key, value in counts.items():
            typer.echo(f"{key}: {json.dumps(value, sort_keys=True)}")
        typer.echo("Definition lifecycle only; end conditions and quota brake are not enforced.")


@experiment_app.command("start")
def experiment_start(
    as_json: bool = typer.Option(False, "--json"),
    data_dir: Path | None = typer.Option(None, "--data-dir"),
) -> None:
    """Validate config and freeze a new active experiment in the ledger."""
    try:
        with _ledger(data_dir) as ledger:
            _available(ledger)
            config = load_config()
            if config.get("experiment") is None:
                raise ValueError("experiment: no definition configured")
            definition = validate_experiment(config["experiment"], config)
            ledger.start_experiment(
                definition.name,
                definition.arms,
                end_pairs=definition.end_pairs,
                end_at=definition.end_at,
                quota_brake=definition.quota_brake,
                exclude_projects=definition.exclude_projects,
            )
            _show(ledger, ledger.get_experiment(definition.name), as_json)
    except ValueError as exc:
        _error(str(exc), as_json)


@experiment_app.command("stop")
def experiment_stop(
    reason: str = typer.Option("stopped", "--reason"),
    as_json: bool = typer.Option(False, "--json"),
    data_dir: Path | None = typer.Option(None, "--data-dir"),
) -> None:
    """End the active experiment without changing config or pairs."""
    try:
        with _ledger(data_dir) as ledger:
            _available(ledger)
            record = ledger.active_experiment()
            if record is None or not ledger.end_experiment(record["id"], reason):
                raise ValueError("no active experiment remains")
            _show(ledger, ledger.get_experiment(record["name"]), as_json)
    except ValueError as exc:
        _error(str(exc), as_json)


@experiment_app.command("status")
def experiment_status(
    as_json: bool = typer.Option(False, "--json"),
    data_dir: Path | None = typer.Option(None, "--data-dir"),
) -> None:
    """Show the active frozen definition and its pair counts."""
    try:
        with _ledger(data_dir) as ledger:
            _available(ledger)
            with ledger.read_snapshot():
                _show(ledger, ledger.active_experiment(), as_json)
    except ValueError as exc:
        _error(str(exc), as_json)
