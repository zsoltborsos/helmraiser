"""Manual read-only scouting and owner management of scout drafts."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import NoReturn

import typer

from helmraiser.cli.model_setup import models_not_configured_message, require_models
from helmraiser.config import load_config, missing_models
from helmraiser.isolation.process import ProcessIsolation
from helmraiser.ledger.db import Ledger, ScoutRequestUnavailable, default_data_dir
from helmraiser.loop.intake import IntakeOutcome, get_intake_adapter, run_intake
from helmraiser.loop.scout import (
    ScoutError,
    configured_focus_projects,
    configured_paused_projects,
    draft_rows,
    drop_draft,
    effective_scout_limit,
    execute_scout,
    resolve_draft,
    scout_gate,
)
from helmraiser.pi.adapter import PiJsonAdapter
from helmraiser.task.model import TaskSpec
from helmraiser.task.project import load_project_defaults
from helmraiser.task.schema import validate_task

EXIT_ERROR = 1
EXIT_VALIDATION = 2

drafts_app = typer.Typer(help="List, request, and drop scout-generated draft tasks")


def _fail(message: str, *, as_json: bool, code: int = EXIT_ERROR) -> NoReturn:
    typer.echo(json.dumps({"error": message}) if as_json else f"Error: {message}")
    raise typer.Exit(code=code)


def _git_root(repo: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo.expanduser(),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ScoutError(f"not a git repository: {repo}") from exc
    if result.returncode != 0:
        raise ScoutError(f"not a git repository: {repo}")
    return Path(result.stdout.strip()).resolve()


def get_scout_adapter(config: dict, repo: Path) -> PiJsonAdapter:
    defaults = load_project_defaults(repo)
    isolation = ProcessIsolation(config, readonly=True)
    isolation.set_project_deny_read(defaults.deny_read, repo)
    reason = isolation.check_available()
    if reason is not None:
        raise ScoutError(f"process isolation is unavailable: {reason}")
    return PiJsonAdapter(pi_bin=str(config.get("pi_bin") or "pi"), argv_wrapper=isolation.wrap)


def _intake_callback(config: dict, data_dir: Path) -> Callable[[TaskSpec], IntakeOutcome]:
    adapter = get_intake_adapter(config)

    def propose(spec: TaskSpec) -> IntakeOutcome:
        missing = missing_models(config, ("intake",), spec)
        if missing and validate_task(replace(spec, status="queued")):
            raise ValueError(models_not_configured_message(missing))
        return run_intake(
            spec,
            config=config,
            adapter=adapter,
            model=spec.models.get("intake"),
            data_dir=data_dir,
            allow_auto=False,
            approve=False,
        )

    return propose


def _format_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _list_drafts(project: str | None, as_json: bool, data_dir: Path | None) -> None:
    base = data_dir.expanduser() if data_dir is not None else default_data_dir()
    project_key = str(Path(project).expanduser().resolve()) if project else None
    rows = draft_rows(Ledger(base / "helmraiser.db"), project=project_key)
    if as_json:
        typer.echo(json.dumps(rows))
        return
    for row in rows:
        preflight = row["preflight"]
        preflight_text = preflight["summary"] if preflight else "not run"
        intake_text = str(row["intake_status"])
        if row["intake_error"]:
            intake_text += f" ({row['intake_error']})"
        typer.echo(
            f"{row['project']} {row['id']} {row['title']} size={row['size']} "
            f"source={row['source']} lens={row['lens']} age={_format_age(row['age'])} "
            f"intake: {intake_text} preflight: {preflight_text}"
        )
        for note in row["intake_notes"]:
            typer.echo(f"  Intake note: {note}")


@drafts_app.callback(invoke_without_command=True)
def drafts_default(
    ctx: typer.Context,
    project: str | None = typer.Option(None, "--project", help="Restrict to one repository"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """List pending drafts when invoked without a subcommand."""
    if ctx.invoked_subcommand is None:
        _list_drafts(project, as_json, data_dir)


@drafts_app.command("list")
def drafts_list(
    project: str | None = typer.Option(None, "--project", help="Restrict to one repository"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """List pending scout drafts."""
    _list_drafts(project, as_json, data_dir)


@drafts_app.command("request")
def drafts_request(
    repo: list[Path] = typer.Argument(..., help="Repositories to scout"),
    lens: str | None = typer.Option(None, "--lens", help="Scout method (default: state)"),
    cancel: bool = typer.Option(False, "--cancel", help="Cancel pending scout requests"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Request safe worker-driven scouts, or cancel pending requests."""
    from helmraiser.loop.scout_lenses import lens_text

    try:
        lens_text("state" if lens is None else lens)
        roots = list(dict.fromkeys(_git_root(path) for path in repo))
        base = data_dir.expanduser() if data_dir is not None else default_data_dir()
        if cancel:
            ledger = Ledger(base / "helmraiser.db")
            changed, unchanged = ledger.cancel_scout_requests(
                (str(root) for root in roots), lens=lens
            )
            payload = {"cancelled": changed, "not_pending": unchanged}
            phrases = (("cancelled", changed), ("not pending", unchanged))
        else:
            config = load_config()
            focus = set(configured_focus_projects(config))
            paused = set(configured_paused_projects(config))
            for root in roots:
                if root not in focus:
                    raise ScoutError(f"project is not in routing.scout.focus_projects: {root}")
                if root in paused:
                    raise ScoutError(f"project is paused for scouting: {root}")
            ledger = Ledger(base / "helmraiser.db")
            changed, unchanged = ledger.request_scouts(
                (str(root) for root in roots), lens=lens or "state"
            )
            payload = {"requested": changed, "already_pending": unchanged}
            phrases = (("requested", changed), ("already pending", unchanged))
    except (ScoutError, ValueError) as exc:
        _fail(str(exc), as_json=as_json, code=EXIT_VALIDATION)
    if as_json:
        typer.echo(json.dumps(payload))
        return
    for phrase, projects in phrases:
        for project in projects:
            typer.echo(f"{phrase}: {project}")


@drafts_app.command("drop", context_settings={"ignore_unknown_options": True})
def drafts_drop(
    task: str = typer.Argument(..., help="Full ledger task id or a unique bare id"),
    reason: str = typer.Option(..., "--reason", help="Why the draft is being dropped"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Keep a draft but mark it dropped with an owner reason."""
    base = data_dir.expanduser() if data_dir is not None else default_data_dir()
    ledger = Ledger(base / "helmraiser.db")
    try:
        row = resolve_draft(ledger, task)
        dropped = drop_draft(ledger, row, reason)
    except ScoutError as exc:
        _fail(str(exc), as_json=as_json, code=EXIT_VALIDATION)
    payload = {
        "task_id": dropped["task_id"],
        "status": "dropped",
        "reason": dropped["drop_reason"],
        "dropped_at": dropped["dropped_at"],
    }
    if as_json:
        typer.echo(json.dumps(payload))
    else:
        typer.echo(f"dropped {payload['task_id']}: {payload['reason']}")


def scout(
    repo: Path = typer.Argument(..., help="Repository to inspect for useful tasks"),
    lens: str | None = typer.Option(None, "--lens", help="Scout method (default: state)"),
    maximum: int | None = typer.Option(None, "--max", help="Maximum drafts to write"),
    force: bool = typer.Option(
        False, "--force", help="Bypass all start gates, including the queue pause"
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
    scout_id: str | None = typer.Option(None, "--scout-id", hidden=True),
    request_id: int | None = typer.Option(None, "--request-id", hidden=True),
) -> None:
    """Run one read-only scout session and write proposed tasks as drafts."""
    from helmraiser.loop.scout_lenses import lens_text

    try:
        lens_text("state" if lens is None else lens)
    except ValueError as exc:
        _fail(str(exc), as_json=as_json, code=EXIT_VALIDATION)
    try:
        root = _git_root(repo)
        config = load_config()
        require_models(config, ("routing.scout.model",), as_json=as_json)
        requested = effective_scout_limit(maximum, config)
        base = data_dir.expanduser() if data_dir is not None else default_data_dir()
        ledger = Ledger(base / "helmraiser.db")
        if request_id is not None:
            request = ledger.get_scout_request(request_id)
            if (
                request is None
                or request["started_at"] is not None
                or request["project"] != str(root)
            ):
                raise ScoutRequestUnavailable("scout request is no longer pending")
            if lens is not None and lens != request["lens"]:
                raise ScoutRequestUnavailable("scout request lens does not match")
            lens = request["lens"]
        lens = lens or "state"
        if not force:
            import time

            try:
                scout_gate(
                    root,
                    ledger,
                    config,
                    time.time(),
                    ignore_enabled_and_cooldown=request_id is not None,
                )
            except (ScoutError, ValueError) as exc:
                # Requests stay pending until execute_scout atomically creates
                # their scout record. A changed safety gate must leave them there.
                if request_id is not None:
                    if as_json:
                        typer.echo(
                            json.dumps(
                                {"error": str(exc), "scout_waiting": True, "lens": lens or "state"}
                            )
                        )
                    else:
                        typer.echo(f"Error: {exc}")
                    raise typer.Exit(code=EXIT_ERROR) from None
                raise
        adapter = get_scout_adapter(config, root)
        outcome = execute_scout(
            root,
            ledger,
            config,
            base,
            adapter,
            _intake_callback(config, base),
            requested=requested,
            force=force,
            gate_checked=True,
            scout_id=scout_id,
            request_id=request_id,
            lens=lens,
        )
    except ScoutRequestUnavailable as exc:
        # A cancellation, competing child, or safety gate won the race; no model was called.
        if as_json:
            typer.echo(
                json.dumps({"error": str(exc), "scout_waiting": True, "lens": lens or "state"})
            )
        else:
            typer.echo(f"Error: {exc}")
        raise typer.Exit(code=EXIT_ERROR) from None
    except (ScoutError, ValueError) as exc:
        _fail(str(exc), as_json=as_json)
    if as_json:
        typer.echo(json.dumps(outcome.as_dict()))
    elif outcome.outcome == "succeeded":
        typer.echo(
            f"scout {outcome.scout_id} lens={outcome.lens}: {len(outcome.written_task_ids)} draft(s)"
        )
        for task_id in outcome.written_task_ids:
            typer.echo(f"  drafted {task_id}")
        for ruling in outcome.rulings:
            typer.echo(f"  owner ruling: {ruling['title']} — {ruling['question']}")
        typer.echo(f"report: {outcome.report_path}")
    else:
        typer.echo(f"Error: scout {outcome.scout_id} lens={outcome.lens} failed: {outcome.error}")
        typer.echo(f"report: {outcome.report_path}")
    if outcome.outcome != "succeeded":
        raise typer.Exit(code=EXIT_ERROR)
