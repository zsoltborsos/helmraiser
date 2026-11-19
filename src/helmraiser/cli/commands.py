"""Commands on the root app: ``add``, ``intake``, ``queue``, ``work``,
``approve``, ``reject`` (spec §5); ``status``, ``report``, ``log``, ``attach``
live in ``monitor``.

Every command accepts ``--json`` and ``--data-dir``. Exit codes: 0 ok, 1 error,
2 validation, 3 queue paused.
"""

from __future__ import annotations

import asyncio
import fcntl
import functools
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Collection
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import click
import typer
from rich.console import Console
from rich.table import Table

from helmraiser.cli.base_sync import HeldBaseSync, base_sync_text
from helmraiser.cli.model_setup import (
    WORK_MODEL_ROLES,
    models_not_configured_message,
    require_models,
    require_work_models,
)
from helmraiser.cli.resources import disable_core_dumps
from helmraiser.cli.supervisor import (
    Child,
    CodeVersion,
    Supervisor,
    code_version,
    read_restart_children,
    restore_children,
)
from helmraiser.cli.worker_commands import (
    exit_text,
    max_parallel,
    stop_path,
    update_record,
    work_scope,
    worker_app,
    worker_serves,
    worker_state,
)
from helmraiser.code_snapshot import (
    SNAPSHOT_ENV,
    exec_into,
    loaded_from,
    package_dir,
    prune,
    snapshot_path,
    source_repo,
)
from helmraiser.config import load_config, missing_models
from helmraiser.isolation import Isolation, ProcessIsolation, for_level
from helmraiser.ledger.db import (
    TERMINAL_RUN_STATUSES,
    Ledger,
    ProjectScope,
    RunUnavailable,
    TaskActive,
    default_data_dir,
    default_db_path,
)
from helmraiser.ledger.rundir import RunDir, restamp_summary
from helmraiser.logbook import HoldGate
from helmraiser.loop import landing
from helmraiser.loop.budget import completed_active_minutes
from helmraiser.loop.cleanup import discard_landed_worktree
from helmraiser.loop.delivery import open_pull_request, pr_body
from helmraiser.loop.intake import (
    IntakeOutcome,
    _intake_model_overrides,
    check_acceptance,
    get_intake_adapter,
    run_intake,
)
from helmraiser.loop.prompts import REBASE_NOTE, initial_prompt
from helmraiser.loop.reconcile import find_phantom_runs, reconcile, remove_orphaned_services
from helmraiser.loop.report import pending_scope_request
from helmraiser.loop.review import parse_verdict
from helmraiser.loop.routing import overflow_setting, route_dispatch
from helmraiser.loop.runner import STATE_STATUS, GovernedRun, RunOutcome, TaskLocked
from helmraiser.loop.spend import (
    caps_enforcement,
    live_for,
    never_startable,
    never_startable_text,
    provider,
    stop_block,
    subscription_warnings,
    usage,
)
from helmraiser.loop.status import held_task_text, write_status
from helmraiser.loop.verify import last_verify_results
from helmraiser.pi.adapter import PiJsonAdapter, PiVersionError, pi_version
from helmraiser.pi.directives import render_implement_directives
from helmraiser.pi.protocol import PiAdapter, PiRequest
from helmraiser.pipeline.worktree import WorktreeManager
from helmraiser.project_notes import project_notes_for_body
from helmraiser.runner.engine import ExecutionEngine
from helmraiser.runner.process_control import PosixProcessController
from helmraiser.storage.state import StateManager
from helmraiser.task.loader import (
    TaskLoadError,
    load_task,
    parse_task,
    render_task,
    resolve_models,
    spec_to_frontmatter,
    write_task,
)
from helmraiser.task.model import (
    PRIORITIES,
    Budget,
    ModelSpec,
    TaskOrigin,
    TaskSpec,
    parse_origin,
)
from helmraiser.task.paths import add_paths_to_frontmatter
from helmraiser.task.project import load_project_defaults, worktree_manager
from helmraiser.task.schema import validate_task
from helmraiser.task.structural import (
    RELATED_TESTS_AUTO_ADD_LIMIT,
    body_outside_paths_warnings,
    outside_paths_warnings,
    related_tests_outside_scope,
    related_tests_warnings,
)
from helmraiser.utils.markdown import serialize_frontmatter

console = Console()

EXIT_ERROR = 1
EXIT_VALIDATION = 2
EXIT_PAUSED = 3

# ── Helpers ───────────────────────────────────────────────────────────


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "task"


def _data_dir(data_dir: Path | None) -> Path:
    return data_dir.expanduser() if data_dir is not None else default_data_dir()


def _ledger(data_dir: Path | None, *, read_only: bool = False) -> Ledger:
    return Ledger(default_db_path(_data_dir(data_dir)), read_only=read_only)


def _fail(
    message: str,
    *,
    as_json: bool,
    code: int,
    errors: list[str] | None = None,
    check: dict[str, Any] | None = None,
) -> NoReturn:
    if as_json:
        payload: dict[str, Any] = {"error": message}
        if errors:
            payload["errors"] = errors
        if check is not None:
            payload["check"] = check
        typer.echo(json.dumps(payload))
    else:
        console.print(f"[red]Error:[/red] {message}")
        for err in errors or []:
            console.print(f"  - {err}")
    raise typer.Exit(code=code)


def _load(path: Path, config: dict[str, Any], *, as_json: bool) -> TaskSpec:
    if not path.is_file():
        _fail(f"task file not found: {path}", as_json=as_json, code=EXIT_ERROR)
    try:
        return load_task(path, config)
    except TaskLoadError as exc:
        _fail(str(exc), as_json=as_json, code=EXIT_VALIDATION)


def _is_tty() -> bool:
    """Whether intake may open an editor (tests monkeypatch this)."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat() if ts else None


def _task_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out.pop("snapshot", None)
    out["priority"] = PRIORITIES[int(row["priority"])]
    out["queued_at"] = _iso(row.get("queued_at"))
    out["created_at"] = _iso(row.get("created_at"))
    out["updated_at"] = _iso(row.get("updated_at"))
    return out


# ── Intake ────────────────────────────────────────────────────────────


def _parse_origin_option(value: str, *, as_json: bool) -> TaskOrigin:
    kind, _separator, session = value.partition(":")
    try:
        origin = parse_origin({"kind": kind, "session": session})
    except ValueError as exc:
        _fail(str(exc), as_json=as_json, code=EXIT_VALIDATION)
    assert origin is not None
    return origin


# ── Commands ──────────────────────────────────────────────────────────


def add(
    target: str | None = typer.Argument(
        None, help="Task file to register, or the task text when --title is given"
    ),
    project: Path | None = typer.Option(None, "--project", help="Repository root (with --title)"),
    title: str | None = typer.Option(None, "--title", help="Create a new draft with this title"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Register a task file (or create a draft from text) in the ledger."""
    config = load_config()
    if title is not None:
        if project is None:
            _fail("--project is required with --title", as_json=as_json, code=EXIT_VALIDATION)
        repo = project.expanduser().resolve()
        if not (repo / ".git").exists():
            _fail(f"not a git repository: {repo}", as_json=as_json, code=EXIT_VALIDATION)
        task_id = slugify(title)
        path = repo / ".helmraiser" / "tasks" / f"{task_id}.md"
        if path.exists():
            _fail(f"task file already exists: {path}", as_json=as_json, code=EXIT_ERROR)
        content = serialize_frontmatter(
            {
                "id": task_id,
                "title": title,
                "project": str(repo),
                "priority": "normal",
                "status": "draft",
            },
            (target or "").strip(),
        )
        spec = parse_task(content, path, config)
        errors = validate_task(spec)
        if errors:
            _fail("invalid task", as_json=as_json, code=EXIT_VALIDATION, errors=errors)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content + "\n", encoding="utf-8")
    else:
        if target is None:
            _fail(
                "give a task file, or --title with --project", as_json=as_json, code=EXIT_VALIDATION
            )
        path = Path(target).expanduser()
        spec = replace(_load(path, config, as_json=as_json), status="draft")
        errors = validate_task(spec)
        if errors:
            _fail("invalid task", as_json=as_json, code=EXIT_VALIDATION, errors=errors)

    ledger = _ledger(data_dir)
    try:
        key = ledger.upsert_task(spec, path.read_text(encoding="utf-8"))
    except TaskActive as exc:
        _fail(str(exc), as_json=as_json, code=EXIT_VALIDATION)
    if as_json:
        typer.echo(
            json.dumps({"task_id": key, "id": spec.id, "status": "draft", "path": str(path)})
        )
    else:
        typer.echo(key)


def _edit_proposal(
    outcome: IntakeOutcome,
    config: dict[str, Any],
    *,
    allow_auto: bool,
    original_models: dict[str, Any],
) -> TaskSpec:
    """Open the merged task in ``$EDITOR`` and return the edited task, validated as queued."""
    edited = click.edit(render_task(outcome.spec), extension=".md")
    if edited is None:
        _fail("intake cancelled", as_json=False, code=EXIT_ERROR)
    try:
        spec = parse_task(edited, outcome.spec.source_path, config)
        frontmatter = spec_to_frontmatter(spec)
        models = _intake_model_overrides(
            original_models, frontmatter.get("models", {}), config, spec.project
        )
        if models:
            frontmatter["models"] = models
        else:
            frontmatter.pop("models", None)
        spec = parse_task(serialize_frontmatter(frontmatter, spec.body), spec.source_path, config)
    except TaskLoadError as exc:
        _fail(str(exc), as_json=False, code=EXIT_VALIDATION)
    spec = replace(spec, status="queued")
    if spec.policy == "auto" and not allow_auto:
        spec = replace(spec, policy="review")
        outcome.notes.append("proposal asked for policy auto; set to review (pass --allow-auto)")
    errors = validate_task(spec)
    if errors:
        _fail(
            "acceptance block is not valid for queueing",
            as_json=False,
            code=EXIT_VALIDATION,
            errors=errors,
        )
    return spec


def _never_startable_warning(never: dict[str, Any]) -> str:
    return (
        f"{never['reason']} (caps are not enforced, so it may run now): "
        f"{never_startable_text(never)}"
    )


def intake(
    file: Path = typer.Argument(..., help="Task file"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Accept the proposal as-is if valid"),
    propose: bool = typer.Option(
        False,
        "--propose",
        help="Save the proposed acceptance block to the file without queueing",
    ),
    check: bool = typer.Option(
        False, "--check", help="Check acceptance on committed HEAD before queueing"
    ),
    model: str | None = typer.Option(None, "--model", help="Intake model provider/id[:thinking]"),
    allow_auto: bool = typer.Option(False, "--allow-auto", help="Let intake propose policy auto"),
    origin: str | None = typer.Option(
        None, "--origin", help="Queueing agent session as <claude-code|fo>:<session>"
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Propose and approve the acceptance block, then queue the task."""
    check_only = check and not yes
    if propose and yes:
        _fail("--propose and --yes are exclusive", as_json=as_json, code=EXIT_VALIDATION)
    requested_origin = _parse_origin_option(origin, as_json=as_json) if origin is not None else None
    config = load_config()
    spec = _load(file.expanduser(), config, as_json=as_json)
    original_text = spec.source_path.read_bytes().decode("utf-8")
    if requested_origin is not None:
        spec = replace(spec, origin=requested_origin)
    requires_approval = load_project_defaults(spec.project).require_approval
    if spec.policy == "auto" and requires_approval:
        _fail(
            "project requires approval: policy auto is not allowed",
            as_json=as_json,
            code=EXIT_VALIDATION,
        )
    intake_model = ModelSpec.parse(model) if model else spec.models.get("intake")
    if validate_task(replace(spec, status="queued")):
        effective = (
            replace(spec, models={**spec.models, "intake": intake_model}) if intake_model else spec
        )
        require_models(config, ("intake",), task=effective, as_json=as_json)
    try:
        outcome = run_intake(
            spec,
            config=config,
            adapter=get_intake_adapter(config),
            model=intake_model,
            data_dir=_data_dir(data_dir),
            allow_auto=allow_auto and not requires_approval,
            approve=not (propose or check_only),
            collect_scope_warnings=not (yes or check_only),
        )
    except ValueError as exc:
        _fail(str(exc), as_json=as_json, code=EXIT_ERROR)
    if requires_approval and allow_auto:
        outcome.notes.append("project requires approval; --allow-auto ignored")

    if outcome.errors:
        _fail(
            "acceptance block is not valid for queueing",
            as_json=as_json,
            code=EXIT_VALIDATION,
            errors=outcome.errors,
        )

    related_output: dict[str, Any] = {
        "related_tests_added": [],
        "related_tests_would_add": [],
        "related_tests_count": 0,
    }
    if check_only:
        candidates = related_tests_outside_scope(outcome.spec.paths, outcome.spec.project)
        related_output["related_tests_count"] = len(candidates)
        outcome.notes += outside_paths_warnings(outcome.spec)
        outcome.notes += body_outside_paths_warnings(outcome.spec)
        if (
            candidates
            and len(candidates) <= RELATED_TESTS_AUTO_ADD_LIMIT
            and spec.status != "queued"
        ):
            related_output["related_tests_would_add"] = candidates
            outcome.notes.append("Would add related tests to paths: " + ", ".join(candidates))
            outcome.notes.append(
                "Preview only: additions require a task that is neither queued nor active "
                "in the ledger; check-only does not inspect ledger state."
            )
        else:
            outcome.notes += related_tests_warnings(outcome.spec, candidates)

    checked: dict[str, Any] | None = None
    if check:
        if check_only and outcome.proposal is not None:
            write_task(outcome.spec, outcome.spec.source_path)
        checked = check_acceptance(outcome.spec, config=config, data_dir=_data_dir(data_dir))
        passed = not checked.get("error") and all(
            step["class"] in ("green", "expected red") for step in checked["steps"]
        )
        if not as_json:
            for step in checked["steps"]:
                typer.echo(f"{step['name']}: {step['class']}")
                if step["class"] in ("fail", "sandbox"):
                    if step["tail"]:
                        typer.echo(step["tail"])
                    if step["hint"]:
                        typer.echo(step["hint"])
            if checked.get("error"):
                typer.echo(checked["error"])
        if not passed or check_only:
            if not as_json:
                for note in outcome.notes:
                    console.print(note, markup=False, highlight=False)
            if as_json:
                typer.echo(
                    json.dumps(
                        {
                            "task_id": outcome.spec.task_id,
                            "id": outcome.spec.id,
                            "status": spec.status,
                            "path": str(outcome.spec.source_path),
                            "proposal": outcome.proposal,
                            "task": render_task(outcome.spec),
                            "notes": outcome.notes,
                            "check": checked,
                            **related_output,
                            **({"error": "acceptance check failed"} if not passed else {}),
                        }
                    )
                )
            if not passed:
                raise typer.Exit(code=EXIT_VALIDATION)
            return

    if propose:
        ledger = _ledger(data_dir)
        never = never_startable(config, outcome.spec)
        if never is not None:
            enforced, _source = caps_enforcement(ledger, config)
            outcome.notes.append(
                f"{never['reason']}: {never_startable_text(never)}; intake --yes will refuse it"
                if enforced
                else _never_startable_warning(never)
            )
        write_task(outcome.spec, outcome.spec.source_path)
        try:
            key = ledger.upsert_task(
                outcome.spec, outcome.spec.source_path.read_text(encoding="utf-8")
            )
        except TaskActive as exc:
            _fail(str(exc), as_json=as_json, code=EXIT_VALIDATION)
        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "task_id": key,
                        "id": outcome.spec.id,
                        "status": "draft",
                        "path": str(outcome.spec.source_path),
                        "proposal": outcome.proposal,
                        "task": render_task(outcome.spec),
                        "notes": outcome.notes,
                    }
                )
            )
        else:
            for note in outcome.notes:
                console.print(f"[dim]{note}[/dim]")
            console.print(render_task(outcome.spec), markup=False, highlight=False)
            typer.echo(
                f"{key} proposed; queue it with "
                f"helmraiser intake {outcome.spec.source_path} --yes"
            )
        return

    shown = 0
    if not yes and not as_json and _is_tty():
        # The notes (a structure check, for one) must be seen before the task is queued.
        for note in outcome.notes:
            console.print(f"[dim]{note}[/dim]")
        shown = len(outcome.notes)
        outcome.spec = _edit_proposal(
            outcome,
            config,
            allow_auto=allow_auto and not requires_approval,
            original_models=spec_to_frontmatter(spec).get("models", {}),
        )
        if requested_origin is not None:
            outcome.spec = replace(outcome.spec, origin=requested_origin)
    elif not yes:
        rendered = render_task(outcome.spec)
        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "error": "interactive intake is not available; re-run with --yes",
                        "proposal": outcome.proposal,
                        "task": rendered,
                        "notes": outcome.notes,
                    }
                )
            )
        else:
            for note in outcome.notes:
                console.print(f"[dim]{note}[/dim]")
            console.print(rendered, markup=False, highlight=False)
            console.print(
                "[yellow]Not a terminal, so no editor was opened; re-run with --yes to accept.[/yellow]"
            )
        raise typer.Exit(code=EXIT_ERROR)

    ledger = _ledger(data_dir)
    never = never_startable(config, outcome.spec)
    if never is not None:
        if caps_enforcement(ledger, config)[0]:
            _fail(
                f"refusing to queue {outcome.spec.id}: {never['reason']}: "
                f"{never_startable_text(never)}",
                as_json=as_json,
                code=EXIT_VALIDATION,
                check=checked,
            )
        outcome.notes.append(_never_startable_warning(never))
    additions: list[str] = []
    if yes:
        existing = ledger.get_task(outcome.spec.task_id)
        active = any(
            run["status"] not in TERMINAL_RUN_STATUSES
            for run in ledger.list_runs(task_id=outcome.spec.task_id)
        )
        candidates = related_tests_outside_scope(outcome.spec.paths, outcome.spec.project)
        related_output["related_tests_count"] = len(candidates)
        if (
            candidates
            and len(candidates) <= RELATED_TESTS_AUTO_ADD_LIMIT
            and spec.status != "queued"
            and (existing is None or existing["status"] != "queued")
            and not active
        ):
            additions = candidates
            stamp = dict(outcome.spec.extra["intake"])
            recorded = stamp.get("related_tests_added")
            previous = (
                [p for p in recorded if isinstance(p, str)] if isinstance(recorded, list) else []
            )
            stamp["related_tests_added"] = list(dict.fromkeys([*previous, *additions]))
            outcome.spec = replace(
                outcome.spec,
                paths=(*(outcome.spec.paths or ()), *additions),
                extra={**outcome.spec.extra, "intake": stamp},
            )
            errors = validate_task(outcome.spec)
            if errors:
                _fail(
                    "augmented task is not valid",
                    as_json=as_json,
                    code=EXIT_VALIDATION,
                    errors=errors,
                )
        else:
            outcome.notes += related_tests_warnings(outcome.spec, candidates)
        outcome.notes += outside_paths_warnings(outcome.spec)
        outcome.notes += body_outside_paths_warnings(outcome.spec)
    document = render_task(outcome.spec)
    outcome.spec.source_path.write_bytes(document.encode("utf-8"))
    try:
        key = ledger.upsert_task(outcome.spec, document)
    except TaskActive as exc:
        outcome.spec.source_path.write_bytes(original_text.encode("utf-8"))
        _fail(str(exc), as_json=as_json, code=EXIT_VALIDATION, check=checked)
    related_output["related_tests_added"] = additions
    if additions:
        outcome.notes.append("Added related tests to paths: " + ", ".join(additions))
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "task_id": key,
                    "id": outcome.spec.id,
                    "status": "queued",
                    "path": str(outcome.spec.source_path),
                    "proposal": outcome.proposal,
                    "notes": outcome.notes,
                    **related_output,
                    **({"check": checked} if checked is not None else {}),
                }
            )
        )
    else:
        for note in outcome.notes[shown:]:
            console.print(f"[dim]{note}[/dim]")
        typer.echo(f"{key} queued")


queue_app = typer.Typer(help="Inspect and control the cross-project task queue")


@queue_app.callback(invoke_without_command=True)
def queue_default(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """``helmraiser queue`` without a subcommand lists the queue."""
    if ctx.invoked_subcommand is None:
        queue_list(status=None, as_json=as_json, data_dir=data_dir)


@queue_app.command("list")
def queue_list(
    status: str | None = typer.Option(None, "--status", help="Only tasks in this status"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """List tasks by status, priority, project and queued time."""
    ledger = _ledger(data_dir)
    rows = [_task_row(r) for r in ledger.list_tasks(status=status)]
    paused = ledger.is_paused()
    if as_json:
        typer.echo(json.dumps({"tasks": rows, "paused": paused}))
        return
    table = Table(title="Task queue")
    for column in ("task_id", "status", "priority", "project", "queued_at"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            row["task_id"], row["status"], row["priority"], row["project"], row["queued_at"] or ""
        )
    console.print(table)
    if paused:
        console.print(f"[yellow]Queue paused:[/yellow] {paused['reason']}")


@queue_app.command("next")
def queue_next(
    project: str | None = typer.Option(None, "--project", help="Restrict to one repository"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Print the task ``work`` would pick next."""
    config = load_config()
    scope = work_scope([project] if project else None, config)
    ledger = _ledger(data_dir)
    paused = ledger.is_paused()
    if paused:
        if as_json:
            typer.echo(json.dumps({"task": None, "paused": paused}))
        else:
            console.print(f"[yellow]Queue paused:[/yellow] {paused['reason']}")
        raise typer.Exit(code=EXIT_PAUSED)
    row = ledger.next_queued(scope=scope, config=config)
    held = _held_rows(ledger, scope, config=config)
    if as_json:
        payload = {"task": _task_row(row) if row else None, "paused": None}
        typer.echo(json.dumps({**payload, **({"held": held} if held else {})}))
    elif row is None and held:
        typer.echo("no eligible task")
        for item in held:
            typer.echo(f"  held: {_held_text(item)}")
    elif row is None:
        typer.echo("no queued task")
    else:
        typer.echo(row["task_id"])


@queue_app.command("pause")
def queue_pause(
    reason: str = typer.Option("manual", "--reason", help="Why the queue is paused"),
    minutes: int | None = typer.Option(None, "--minutes", help="Auto-resume after N minutes"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Pause dispatch (indefinitely, or for --minutes)."""
    until = datetime.now(UTC).timestamp() + minutes * 60 if minutes else None
    _ledger(data_dir).pause_queue(reason, until)
    if as_json:
        typer.echo(json.dumps({"paused": {"reason": reason, "until": until}}))
    else:
        typer.echo(f"queue paused: {reason}")


@queue_app.command("resume")
def queue_resume(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Resume dispatch and clear provider holds."""
    _ledger(data_dir).resume_queue()
    if as_json:
        typer.echo(json.dumps({"paused": None, "held_providers": {}}))
    else:
        typer.echo("queue resumed; provider holds cleared")


# Full task ids start with "-" (the project slug of an absolute path), so they must not
# be parsed as options.
@queue_app.command("remove", context_settings={"ignore_unknown_options": True})
def queue_remove(
    task: str = typer.Argument(..., help="Full task id (<project-slug>:<id>) or a unique bare id"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Take one queued task out of the queue (back to draft; the file is untouched)."""
    ledger = _ledger(data_dir)
    tasks = ledger.list_tasks()
    if ":" in task:
        matches = [row for row in tasks if row["task_id"] == task]
    else:
        bare = [row for row in tasks if row["id"] == task]
        if len(bare) > 1:
            ids = ", ".join(sorted(str(row["task_id"]) for row in bare))
            _fail(f"ambiguous task id: {task}; matches: {ids}", as_json=as_json, code=EXIT_ERROR)
        matches = bare
    if not matches:
        _fail(f"unknown task: {task}", as_json=as_json, code=EXIT_ERROR)
    row = matches[0]
    if not ledger.dequeue_task(str(row["task_id"])):
        current = ledger.get_task(str(row["task_id"]))
        status = current["status"] if current else row["status"]
        _fail(
            f"cannot remove task in status {status}; only queued tasks can be removed",
            as_json=as_json,
            code=EXIT_VALIDATION,
        )
    if as_json:
        typer.echo(json.dumps({"task_id": row["task_id"], "status": "draft"}))
    else:
        typer.echo(f"{row['task_id']} removed from the queue (now draft)")


# ── work ──────────────────────────────────────────────────────────────


def _task_from_row(row: dict[str, Any], config: dict[str, Any]) -> TaskSpec:
    """The task as snapshotted at enqueue (the ledger row is the source of truth)."""
    return parse_task(str(row["snapshot"]), Path(str(row["source_path"])), config)


def _dispatch_task(
    task: TaskSpec, ledger: Ledger, config: dict[str, Any]
) -> tuple[TaskSpec, list[dict[str, Any]]]:
    """Resolve fresh dispatch before holds/caps inspect the selected models."""
    routing = config.get("routing") or {}
    if (
        not any((routing.get(lever) or {}).get("enabled") for lever in ("plan", "implement"))
        and overflow_setting(config) is None
    ):
        return task, []
    now = datetime.now(UTC).timestamp()
    held_providers = ledger.provider_holds(now)
    readings = usage(ledger, config, now, live=live_for(ledger, config, now))
    return route_dispatch(task, config, readings, now, held_providers=held_providers)


def make_adapter_factory(config: dict[str, Any]) -> Callable[[StateManager, Isolation], PiAdapter]:
    controller = PosixProcessController()

    def factory(state: StateManager, isolation: Isolation) -> PiAdapter:
        return PiJsonAdapter(
            pi_bin=str(config.get("pi_bin", "pi")),
            engine=ExecutionEngine(state, controller),
            argv_wrapper=isolation.wrap,
        )

    return factory


def dry_run_plan(
    task: TaskSpec, config: dict[str, Any], data_dir: Path, isolation: Isolation
) -> dict[str, Any]:
    """What the first implement spawn would look like; touches nothing."""
    run_id = str(uuid.uuid4())
    rundir = RunDir(run_id, data_dir)
    worktree = worktree_manager(task.project).path_for(task.id, run_id)
    scout = task.models.get("scout") or resolve_models(None, config).get("scout")
    services = load_project_defaults(task.project).services
    directives = render_implement_directives(
        run_id=run_id,
        title=task.title,
        worktree=worktree,
        task_path=rundir.task_md,
        task_id=task.id,
        scout_model=scout.render() if scout else None,
        max_iterations=task.budget.max_iterations,
        max_minutes=task.budget.max_minutes,
        max_cost_usd=task.budget.max_cost_usd,
        iteration=1,
        service_vars=tuple(key for spec in services for key in spec.provides),
        project_notes=project_notes_for_body(task.project, task.body),
    )
    prompt = initial_prompt(task)
    request = PiRequest(
        prompt=prompt,
        session_id=f"{run_id}-implement",
        model=task.models["implement"],
        cwd=worktree,
        session_dir=rundir.sessions,
        system_prompt_path=rundir.directives_md,
        timeout_s=task.budget.max_minutes * 60,
    )
    # Process isolation writes its settings file into the run dir when wrapping;
    # a dry run must not leave a run dir behind, so wrap against a throwaway one.
    with tempfile.TemporaryDirectory(prefix="helmraiser-dry-run-") as tmp:

        def wrapper(argv: list[str], cwd: Path, _run_dir: Path) -> list[str]:
            return isolation.wrap(argv, cwd, Path(tmp))

        adapter = PiJsonAdapter(pi_bin=str(config.get("pi_bin", "pi")), argv_wrapper=wrapper)
        argv = adapter.build_argv(request)
    return {
        "dry_run": True,
        "run_id": run_id,
        "task_id": task.task_id,
        "worktree": str(worktree),
        "run_dir": str(rundir.path),
        "argv": argv,
        "directives": directives,
        "prompt": prompt,
    }


def _print_outcome(outcome: RunOutcome, task: TaskSpec, data_dir: Path) -> None:
    colour = {"awaiting_approval": "green", "landed": "green", "failed": "red"}.get(
        outcome.status, "yellow"
    )
    console.print(
        f"[{colour}]{outcome.status}[/{colour}] {task.task_id} run {outcome.run_id} — "
        f"{outcome.iterations} iteration(s), ${outcome.cost_usd:.4f}, {outcome.minutes:.1f} min"
    )
    if outcome.review_rounds:
        console.print(f"  review rounds: {outcome.review_rounds}", markup=False)
    if outcome.failure_reason:
        console.print(f"  reason: {outcome.failure_reason}", markup=False)
    for warning in outcome.warnings:
        console.print(f"  [warn] {warning}", markup=False)
    if outcome.branch_name:
        base_line = f"\n  base: {task.base}" if task.base else ""
        console.print(
            f"  branch: {outcome.branch_name}{base_line}\n  worktree: {outcome.worktree_path}"
        )
    if outcome.pr_url:
        console.print(f"  pr: {outcome.pr_url}", markup=False)
    for result in outcome.verify_results:
        mark = "pass" if result["passed"] else f"FAIL (exit {result['exit_code']})"
        console.print(f"  verify {result['name']}: {mark}", markup=False)
        for hint in result.get("hints") or []:
            console.print(f"    hint: {hint}", markup=False)
    for result in outcome.evidence_results:
        mark = "ok" if result["ok"] else f"NOT OK (exit {result['exit_code']})"
        console.print(f"  evidence {result['name']}: {mark}", markup=False)
        for hint in result.get("hints") or []:
            console.print(f"    hint: {hint}", markup=False)
    if outcome.verdict:
        verdict = outcome.verdict
        console.print(f"  review: {verdict['verdict']}", markup=False)
        for finding in verdict.get("findings") or []:
            console.print(
                f"    [{finding.get('severity')}] {finding.get('file')}:{finding.get('line')} "
                f"{finding.get('note')}",
                markup=False,
            )
    if outcome.pending_approval:
        if outcome.pending_approval == "plan":
            typer.echo(f"  plan: {RunDir(outcome.run_id, data_dir).plan_md}")
        if outcome.scope_request:
            request = outcome.scope_request
            typer.echo(
                f"  scope request: {', '.join(request['paths'])} — "
                f"{request['reason'] or '(no reason given)'}"
            )
        console.print(f"  next: helmraiser approve {outcome.run_id} (or reject)", markup=False)


def _batch_summary(
    outcomes: list[dict[str, Any]],
    excluded_task_ids: set[str],
    paused: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    held_providers: dict[str, dict[str, Any]] | None = None,
    stopped: bool = False,
    held: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    statuses = ("landed", "awaiting_approval", "failed", "aborted")
    entries = []
    for outcome in outcomes:
        entry: dict[str, Any] = {
            "run_id": str(outcome["run_id"])[:8],
            "task_id": outcome.get("task_id"),
            "status": outcome["status"],
            "cost_usd": outcome["cost_usd"],
            "review_rounds": outcome.get("review_rounds", 0),
        }
        if outcome.get("pr_url"):
            entry["pr_url"] = outcome["pr_url"]
        if outcome["status"] == "failed" and outcome.get("failure_reason"):
            entry["failure_reason"] = outcome["failure_reason"]
        entries.append(entry)
    summary = {
        "counts": {
            status: sum(item["status"] == status for item in outcomes) for status in statuses
        },
        "total_cost_usd": round(sum(float(item["cost_usd"]) for item in outcomes), 4),
        "total_minutes": round(sum(float(item["minutes"]) for item in outcomes), 1),
        "runs": entries,
        "skipped_task_ids": sorted(excluded_task_ids),
        "held": held or [],
        "paused": paused,
        "held_providers": held_providers or {},
        "warnings": [
            *(warnings or []),
            *(
                f"{str(item['run_id'])[:8]}: {warning}"
                for item in outcomes
                for warning in item.get("warnings") or []
            ),
        ],
    }
    if stopped:
        summary["stopped"] = True
    return summary


def _batch_line(summary: dict[str, Any]) -> str:
    run_count = len(summary["runs"])
    labels = {
        "landed": "landed",
        "awaiting_approval": "awaiting approval",
        "failed": "failed",
        "aborted": "aborted",
    }
    counts = ", ".join(
        f"{count} {labels[status]}" for status, count in summary["counts"].items() if count
    )
    counts = counts or "no outcomes"
    minutes = f"{summary['total_minutes']:g}"
    line = (
        f"Batch: {run_count} {'run' if run_count == 1 else 'runs'} — {counts} — "
        f"${summary['total_cost_usd']:.2f}, {minutes} min"
    )
    if summary.get("held"):
        line += f" — {len(summary['held'])} held"
    return line


def _print_batch(summary: dict[str, Any]) -> None:
    typer.echo(_batch_line(summary))
    status_labels = {"awaiting_approval": "awaiting"}
    for run in summary["runs"]:
        status = status_labels.get(run["status"], run["status"])
        line = f"  {status} {run['run_id']} {run['task_id']} ${run['cost_usd']:.2f}"
        if run.get("review_rounds"):
            line += f" review rounds: {run['review_rounds']}"
        if run.get("pr_url"):
            line += f" {run['pr_url']}"
        typer.echo(line)
        if run.get("failure_reason"):
            typer.echo(f"    reason: {run['failure_reason']}")
    paused = summary.get("paused")
    if paused:
        line = f"  paused: {paused.get('reason')}"
        if paused.get("until") is not None:
            local = datetime.fromtimestamp(float(paused["until"])).strftime("%Y-%m-%d %H:%M")
            line += f" until {local}"
        typer.echo(line)
    if summary.get("stopped"):
        typer.echo("  stopped: on request")
    for name, hold in summary.get("held_providers", {}).items():
        local = datetime.fromtimestamp(float(hold["until"])).strftime("%Y-%m-%d %H:%M")
        typer.echo(f"  held provider: {name} until {local}")
    if summary["skipped_task_ids"]:
        typer.echo(f"  skipped: {', '.join(summary['skipped_task_ids'])}")
    for item in summary.get("held") or []:
        typer.echo(f"  held: {_held_text(item)}")
    for warning in summary.get("warnings") or []:
        typer.echo(f"  [warn] {warning}")
    if summary["counts"]["awaiting_approval"]:
        typer.echo("  next: helmraiser report --since 24h")


def _held_rows(
    ledger: Ledger,
    scope: ProjectScope | None = None,
    exclude: Collection[str] = (),
    *,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Queued tasks held by project runs or unmet dependencies, in queue order."""
    held = ledger.held_tasks(scope=scope, config=config)
    return [
        {"task_id": task["task_id"], **held[task["task_id"]]}
        for task in ledger.list_tasks(status="queued")
        if task["task_id"] in held and task["task_id"] not in exclude
    ]


def _held_text(item: dict[str, Any]) -> str:
    return held_task_text(str(item["task_id"]), item)


def _notify_batch(summary: dict[str, Any], enabled: bool) -> None:
    executable = shutil.which("notify-send") if enabled else None
    if executable is None:
        return
    try:
        subprocess.run(
            [executable, "Helmraiser", _batch_line(summary)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        pass


def _run_notice(outcome: dict[str, Any]) -> str:
    label = str(outcome["status"]).replace("_", " ")
    return (
        f"{outcome.get('task_id')}: {label} — ${float(outcome['cost_usd']):.2f}, "
        f"{float(outcome.get('minutes') or 0.0):g} min"
    )


def _notify_run(outcome: dict[str, Any], enabled: bool) -> None:
    """One notification per finished run (the long-running worker's)."""
    _notify_text(_run_notice(outcome), enabled)


def _notify_text(text: str, enabled: bool) -> None:
    """One desktop notification through ``notify-send``, when available."""
    executable = shutil.which("notify-send") if enabled else None
    if executable is None:
        return
    try:
        subprocess.run(
            [executable, "Helmraiser", text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        pass


def _task_skip(
    task: TaskSpec,
    ledger: Ledger,
    config: dict[str, Any],
    no_caps: bool,
    *,
    holds: HoldGate | None = None,
) -> tuple[dict[str, Any], str] | None:
    """Why ``work`` skips a task (invalid, quota, caps or Logbook hold).

    Return (JSON fields, human text), or None when it may run; write nothing.
    """
    errors = validate_task(task)
    if errors:
        return {"errors": errors}, f"invalid: {'; '.join(errors)}"
    missing = missing_models(config, WORK_MODEL_ROLES, task)
    if missing:
        return {
            "reason": "models_not_configured",
            "missing": missing,
        }, models_not_configured_message(missing)
    held_providers = ledger.provider_holds(datetime.now(UTC).timestamp())
    held = next(
        (
            (name, held_providers[name])
            for name in (
                provider(task.models["implement"].render()),
                provider(task.models["review"].render()),
            )
            if name in held_providers
        ),
        None,
    )
    if held is not None:
        name, hold = held
        until = float(hold["until"])
        local = datetime.fromtimestamp(until).strftime("%H:%M")
        return (
            {"reason": f"quota:{name}", "until": until},
            f"{name} out of quota until {local}",
        )
    enforced, _source = caps_enforcement(ledger, config)
    if enforced and not no_caps:
        now = datetime.now(UTC).timestamp()
        block = stop_block(
            usage(ledger, config, now, live=live_for(ledger, config, now)), config, task, now
        )
        if block is not None and str(block["reason"]).startswith("never_startable:"):
            return block, f"{block['reason']}: {never_startable_text(block)}"
        if block is not None:
            detail = str(block["reason"])
            if block.get("until"):
                local = datetime.fromtimestamp(float(block["until"])).strftime("%H:%M")
                detail += (
                    f" ({block['fraction']:.0%} used + "
                    f"${block.get('projection_usd', task.budget.max_cost_usd):g} "
                    f"projected; fits again {local})"
                )
            return block, detail
    gate = holds if holds is not None else HoldGate(lambda text: typer.echo(f"[warn] {text}"))
    check = gate.check(str(task.project))
    if check.status == "blocked":
        return check.skip_payload(), check.skip_text()
    return None


def _child_argv(project: str, *, data_dir: Path, no_caps: bool) -> list[str]:
    """One supervised run: ``python -m helmraiser work --once --json --supervised``."""
    argv = [sys.executable, "-m", "helmraiser", "work", "--once", "--json", "--supervised"]
    argv += ["--no-notify", "--project", project, "--data-dir", str(data_dir)]
    if no_caps:
        argv.append("--no-caps")
    return argv


def _scout_child_argv(
    project: str, *, data_dir: Path, scout_id: str, request_id: int | None
) -> list[str]:
    """One supervised scout using the worker's current code snapshot."""
    argv = [
        sys.executable,
        "-m",
        "helmraiser",
        "scout",
        project,
        "--json",
        "--data-dir",
        str(data_dir),
        "--scout-id",
        scout_id,
    ]
    if request_id is not None:
        argv += ["--request-id", str(request_id)]
    return argv


def _spawn_child(argv: list[str], output: Path) -> subprocess.Popen[bytes]:
    with output.open("wb") as handle:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=handle)


def _check_pi_version(config: dict[str, Any], *, as_json: bool) -> str:
    """The installed pi version, recorded on every run; fails when pi gives none."""
    try:
        return pi_version(str(config.get("pi_bin", "pi")))
    except PiVersionError as exc:
        _fail(str(exc), as_json=as_json, code=EXIT_ERROR)


def work(
    once: bool = typer.Option(False, "--once", help="Run at most one task"),
    max_runs: int | None = typer.Option(None, "--max", help="Run at most N tasks"),
    project: list[str] | None = typer.Option(
        None, "--project", help="Serve only this repository (repeatable)"
    ),
    watch: bool = typer.Option(
        False, "--watch", help="Keep running: wait for work instead of exiting when idle"
    ),
    max_parallel_runs: int | None = typer.Option(
        None,
        "--max-parallel",
        min=1,
        help="Run up to N runs at once, each in a child process (config max_parallel_runs)",
    ),
    supervised: bool = typer.Option(False, "--supervised", hidden=True),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print argv and prompts; spawn nothing"),
    no_caps: bool = typer.Option(False, "--no-caps", help="Disable spend caps for this invocation"),
    no_notify: bool = typer.Option(False, "--no-notify", help="Disable the batch notification"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Drain the queue: one governed run per queued task until paused or empty. Runs
    handed back to the worker (an approved plan, ``retry``) are resumed first.

    ``--watch`` or ``--max-parallel`` run the supervisor instead: each run in its own
    child process, up to N children at once; ``--watch`` keeps it running."""
    config = load_config()
    base = _data_dir(data_dir)
    parallel = watch or max_parallel_runs is not None
    if parallel and (once or dry_run):
        _fail(
            "--watch and --max-parallel cannot be combined with --once or --dry-run",
            as_json=as_json,
            code=EXIT_VALIDATION,
        )
    scope = work_scope(project, config)
    require_work_models(
        config, base, scope, max_runs=1 if once else max_runs, dry_run=dry_run, as_json=as_json
    )
    running_code = _serve_from_snapshot(base, as_json=as_json) if watch else None
    ledger = _ledger(data_dir)
    children = restore_children(base) if watch else []
    reconciled_runs = (
        []
        if supervised
        else reconcile(
            ledger,
            find_phantom_runs(
                ledger,
                scope.without({c.project for c in children if c.run_id is None}),
                exclude_run_ids={c.run_id for c in children if c.run_id is not None},
            ),
            PosixProcessController(),
        )
    )
    if reconciled_runs:
        if as_json:
            typer.echo(json.dumps({"reconciled": reconciled_runs}))
        else:
            for item in reconciled_runs:
                typer.echo(f"reconciled {item['run_id']}: {item['reason']}")
    removed_services = remove_orphaned_services(ledger)
    if removed_services:
        if as_json:
            typer.echo(json.dumps({"services_removed": removed_services}))
        else:
            for item in removed_services:
                what = "removed" if item["removed"] else f"could not remove ({item['error']})"
                typer.echo(
                    f"{what} service container {item['name']} of run {item['run_id'][:8]}"
                    f" ({item['run_status']})"
                )
    limit = 1 if once else max_runs
    disable_core_dumps()
    pi_version = _check_pi_version(config, as_json=as_json)
    notify = config.get("notify", True) is not False and not no_notify
    if parallel:
        _supervise(
            ledger,
            config,
            base,
            scope,
            watch=watch,
            max_parallel_runs=max_parallel(config, max_parallel_runs),
            max_runs=max_runs,
            no_caps=no_caps,
            notify=notify,
            as_json=as_json,
            data_dir=data_dir,
            running_code=running_code,
            children=children,
        )
        return

    holds = HoldGate(
        lambda text: typer.echo(json.dumps({"warning": text}) if as_json else f"[warn] {text}")
    )
    outcomes: list[dict[str, Any]] = []
    excluded_task_ids: set[str] = set()
    excluded_resume_ids: set[str] = set()
    planned = False
    stopped = False
    base_sync_checked = False

    def record(outcome: RunOutcome, task: TaskSpec) -> None:
        outcomes.append(outcome.as_dict())
        if as_json:
            typer.echo(json.dumps(outcome.as_dict()))
        else:
            _print_outcome(outcome, task, base)
        if outcome.paused:
            _paused_exit(
                outcome.paused,
                outcomes,
                excluded_task_ids,
                as_json=as_json,
                notify=notify,
                warnings=_usage_warnings(config, ledger),
                held_providers=ledger.provider_holds(datetime.now(UTC).timestamp()),
            )

    while limit is None or len(outcomes) < limit:
        if not dry_run and not supervised:
            stop_file = stop_path(data_dir)
            if stop_file.exists():
                stop_file.unlink(missing_ok=True)
                stopped = True
                break
        paused = ledger.is_paused()
        if paused:
            _paused_exit(
                paused,
                outcomes,
                excluded_task_ids,
                as_json=as_json,
                notify=notify,
                warnings=_usage_warnings(config, ledger),
                held_providers=ledger.provider_holds(datetime.now(UTC).timestamp()),
            )
        if not base_sync_checked and not dry_run and not supervised:
            base_sync_checked = True
            for payload in HeldBaseSync().check(ledger, scope, config=config):
                typer.echo(
                    json.dumps({"base_sync": payload}) if as_json else base_sync_text(payload)
                )
        resumable = (
            None
            if dry_run
            else ledger.next_resumable(scope=scope, config=config, exclude=excluded_resume_ids)
        )
        if resumable is not None:
            resumed_id = str(resumable["run_id"])
            resume_row = ledger.get_task(str(resumable["task_id"]))
            if resume_row is not None:
                require_models(
                    config,
                    WORK_MODEL_ROLES,
                    task=_task_from_row(resume_row, config),
                    as_json=as_json,
                )
            governed = GovernedRun.resume(
                resumed_id,
                config=config,
                ledger=ledger,
                adapter_factory=make_adapter_factory(config),
                pi_version=pi_version,
                data_dir=base,
            )
            if not governed.claim_resume():
                excluded_resume_ids.add(resumed_id)
                continue
            if not as_json:
                console.print(
                    f"[bold]resuming[/bold] {governed.task.task_id} run {resumed_id} "
                    f"at {resumable['stage']}"
                )
            try:
                outcome = asyncio.run(governed.run())
            finally:
                holds.clear()
            record(outcome, governed.task)
            continue
        row = ledger.next_queued(scope=scope, exclude=excluded_task_ids, config=config)
        if row is None:
            break
        selected_task = _task_from_row(row, config)
        require_models(config, WORK_MODEL_ROLES, task=selected_task, as_json=as_json)
        task, routing_decisions = _dispatch_task(selected_task, ledger, config)
        skip = _task_skip(task, ledger, config, no_caps, holds=holds)
        if skip is not None:
            excluded_task_ids.add(task.task_id)
            payload, text = skip
            if as_json:
                typer.echo(json.dumps({"skipped": task.task_id, **payload}))
            else:
                typer.echo(f"skipped {task.task_id}: {text}")
            continue
        isolation = for_level(task.isolation, config)
        if isinstance(isolation, ProcessIsolation):
            defaults = load_project_defaults(task.project)
            isolation.set_project_deny_read(defaults.deny_read, task.project)
        reason = isolation.check_available()
        if reason is not None:
            _fail(
                f"refusing {task.task_id}: isolation {task.isolation} unavailable: {reason}",
                as_json=as_json,
                code=EXIT_ERROR,
            )
        if dry_run:
            planned = True
            plan = dry_run_plan(task, config, base, isolation)
            if as_json:
                typer.echo(json.dumps(plan))
            else:
                typer.echo(f"# dry run for {plan['task_id']} (run {plan['run_id']})")
                typer.echo(f"cwd: {plan['worktree']}")
                typer.echo(f"argv: {shlex.join(plan['argv'])}")
                typer.echo("--- directives.md ---")
                typer.echo(plan["directives"])
                typer.echo("--- prompt ---")
                typer.echo(plan["prompt"])
            break
        governed = GovernedRun(
            task,
            config=config,
            ledger=ledger,
            adapter_factory=make_adapter_factory(config),
            pi_version=pi_version,
            data_dir=base,
            snapshot=str(row["snapshot"]),
            routing_decisions=routing_decisions,
            isolation=isolation,
        )
        if not as_json:
            console.print(f"[bold]running[/bold] {task.task_id} as run {governed.run_id}")
        try:
            outcome = asyncio.run(governed.run())
        except RunUnavailable as exc:
            excluded_task_ids.add(task.task_id)
            if as_json:
                typer.echo(json.dumps({"skipped": task.task_id, "hold": exc.hold}))
            else:
                typer.echo(f"skipped {task.task_id}: {exc}")
            continue
        except TaskLocked as exc:
            excluded_task_ids.add(task.task_id)
            if as_json:
                typer.echo(json.dumps({"skipped": task.task_id, "held_by": exc.holder_run_id}))
            else:
                typer.echo(f"skipped {task.task_id}: held by run {exc.holder_run_id}")
            continue
        finally:
            holds.clear()
        record(outcome, task)

    if not dry_run and not stopped and not supervised:
        # A stop can arrive during the last run allowed by --max; consume it here
        # so it cannot stop the next worker immediately on a stale request.
        stop_file = stop_path(data_dir)
        if stop_file.exists():
            stop_file.unlink(missing_ok=True)
            stopped = True
    held_rows = [] if planned else _held_rows(ledger, scope, excluded_task_ids, config=config)
    if outcomes or excluded_task_ids or stopped:
        held_providers = ledger.provider_holds(datetime.now(UTC).timestamp())
        summary = _batch_summary(
            outcomes,
            excluded_task_ids,
            warnings=_usage_warnings(config, ledger),
            held_providers=held_providers,
            stopped=stopped,
            held=held_rows,
        )
        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "runs": outcomes,
                        "paused": None,
                        "held_providers": held_providers,
                        "summary": summary,
                    }
                )
            )
        else:
            _print_batch(summary)
        _notify_batch(summary, notify)
    elif as_json and not dry_run:
        typer.echo(
            json.dumps({"runs": [], "paused": None, **({"held": held_rows} if held_rows else {})})
        )
    elif held_rows and not as_json:
        # Only tasks held by their project's run are left: as for an empty queue.
        typer.echo("no eligible task")
        for item in held_rows:
            typer.echo(f"  held: {_held_text(item)}")
    elif not planned and not excluded_task_ids:
        typer.echo("no queued task")
    if any(o["status"] in ("failed", "aborted") for o in outcomes):
        raise typer.Exit(code=EXIT_ERROR)


def _supervise(
    ledger: Ledger,
    config: dict[str, Any],
    base: Path,
    scope: ProjectScope,
    *,
    watch: bool,
    max_parallel_runs: int,
    max_runs: int | None,
    no_caps: bool,
    notify: bool,
    as_json: bool,
    data_dir: Path | None,
    running_code: CodeVersion | None = None,
    children: list[Child] | None = None,
) -> None:
    """``work --watch`` / ``--max-parallel``: run the supervisor, then print the summary
    of every run it saw finish."""

    holds = HoldGate(
        lambda text: typer.echo(json.dumps({"warning": text}) if as_json else f"[warn] {text}")
    )

    def skip(row: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
        task, _decisions = _dispatch_task(_task_from_row(row, config), ledger, config)
        return _task_skip(task, ledger, config, no_caps, holds=holds)

    def spawn(project: str, output: Path) -> subprocess.Popen[bytes]:
        argv = _child_argv(project, data_dir=base, no_caps=no_caps)
        return _spawn_child(argv, output)

    def spawn_scout(
        project: str, output: Path, scout_id: str, request_id: int | None
    ) -> subprocess.Popen[bytes]:
        return _spawn_child(
            _scout_child_argv(project, data_dir=base, scout_id=scout_id, request_id=request_id),
            output,
        )

    # Restart on new Helmraiser code: only a long-running worker, and not with --max,
    # whose count a restart would reset.
    restart = (
        watch
        and max_runs is None
        and config.get("worker_restart_on_code_change", True) is not False
    )
    if not as_json:
        mode = "watching" if watch else "draining"
        typer.echo(f"{mode} the queue: up to {max_parallel_runs} run(s) at once")
    result = Supervisor(
        ledger=ledger,
        data_dir=base,
        scope=scope,
        max_parallel=max_parallel_runs,
        watch=watch,
        max_runs=max_runs,
        poll_s=float(config.get("worker_poll_seconds", 30)),
        base_sync_interval_s=float(config.get("base_sync_interval_seconds", 300)),
        stop_file=stop_path(data_dir),
        spawn=spawn,
        spawn_scout=spawn_scout,
        run_config=config,
        scout_config=config,
        skip=skip,
        begin_pass=holds.clear,
        notify=lambda outcome: _notify_run(outcome, notify),
        as_json=as_json,
        alert=lambda text: _notify_text(text, notify),
        code_version=code_version if restart else None,
        reexec=lambda new, announce: _reexec(new, announce, base),
        running_code=running_code,
        children=children,
    ).run()
    outcomes = result.outcomes
    held_providers = ledger.provider_holds(datetime.now(UTC).timestamp())
    summary = _batch_summary(
        outcomes,
        result.skipped,
        result.paused,
        warnings=_usage_warnings(config, ledger),
        held_providers=held_providers,
        stopped=result.stopped,
        held=_held_rows(ledger, scope, result.skipped, config=config),
    )
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "runs": outcomes,
                    "paused": result.paused,
                    "held_providers": held_providers,
                    "summary": summary,
                }
            )
        )
    else:
        _print_batch(summary)
    if any(o["status"] in ("failed", "aborted") for o in outcomes):
        raise typer.Exit(code=EXIT_ERROR)


def _serve_from_snapshot(base: Path, *, as_json: bool) -> CodeVersion | None:
    """The background worker (``work --watch``) runs committed code only: a snapshot of
    the checkout's HEAD under ``<base>/code`` (see ``helmraiser.code_snapshot``), so its
    run children never import uncommitted edits.

    Off a snapshot (started by ``worker start``, or re-execed by an older worker with
    the same argv and environment), it exports HEAD and re-execs onto it; it fails
    rather than serve from the working tree. On its snapshot it records the commit in
    ``worker.json``, prunes old snapshots and returns the version it runs. ``None``
    for a package installed outside a git checkout, which has no uncommitted code."""
    commit = os.environ.get(SNAPSHOT_ENV)
    if commit:
        snapshot = snapshot_path(base, commit)
        if not loaded_from(snapshot):
            _fail(
                f"the worker should run the code snapshot {snapshot} "
                f"but imports helmraiser from {package_dir()}",
                as_json=as_json,
                code=EXIT_ERROR,
            )
        update_record(os.getpid(), {"code": {"commit": commit, "snapshot": str(snapshot)}}, base)
        prune(
            base,
            commit,
            protected_commits={
                child.code_commit
                for child in read_restart_children(base)
                if child.code_commit is not None
            },
        )
        if as_json:
            typer.echo(json.dumps({"code_snapshot": {"commit": commit, "path": str(snapshot)}}))
        else:
            typer.echo(f"serving from the code snapshot {commit[:8]} ({snapshot})")
        return code_version(source_repo(), commit)
    repo = source_repo()
    if repo is None:
        return None
    head = code_version(repo)
    if head is None:
        _fail(
            f"cannot read HEAD of the Helmraiser checkout {repo}", as_json=as_json, code=EXIT_ERROR
        )
    problem = exec_into(head.commit, repo, base, lambda: None)
    _fail(
        f"cannot serve from a code snapshot of {head.short}: {problem}",
        as_json=as_json,
        code=EXIT_ERROR,
    )


def _reexec(new: CodeVersion, announce: Callable[[], None], base: Path) -> str:
    """Replace this process with the same command on a snapshot of ``new``: the same
    argv (``sys.orig_argv``), and ``os.execve`` keeps the pid, so ``worker.json``
    stays right. It first exports the snapshot and checks that it imports, so a broken
    commit leaves the worker serving on its old snapshot instead of ending it. Returns
    only when the process was not replaced, with the reason."""
    repo = source_repo()
    if repo is None:
        return "the Helmraiser checkout is unknown"
    return exec_into(new.commit, repo, base, announce)


class WorkerTerminated(Exception):
    """A termination signal reached ``work --watch``."""


def _watch_exit_guard(command: Callable[..., None]) -> Callable[..., None]:
    """Wrap ``work`` so that the background worker (``work --watch`` whose pid
    ``worker.json`` records) never ends silently: whatever ends it, including SIGTERM,
    its record gets ``exit`` ({at, reason, error}) and, unless it stopped on request,
    a desktop notice says why. The supervisor records a clean end itself; this adds
    the error for every other end."""

    @functools.wraps(command)
    def guarded(**options: Any) -> None:
        if not options.get("watch"):
            command(**options)
            return

        def terminate(signum: int, _frame: object) -> None:
            raise WorkerTerminated(f"terminated by {signal.Signals(signum).name}")

        previous = signal.signal(signal.SIGTERM, terminate)
        error: str | None = None
        try:
            command(**options)
        except WorkerTerminated as exc:
            error = str(exc)
            raise typer.Exit(code=128 + signal.SIGTERM) from None
        except (typer.Exit, click.exceptions.Exit) as exc:
            if exc.exit_code:
                error = f"exit code {exc.exit_code}; see the log"
            raise
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            raise
        finally:
            signal.signal(signal.SIGTERM, previous)
            enabled = load_config().get("notify", True) is not False
            _record_worker_exit(
                _data_dir(options.get("data_dir")), error, enabled and not options.get("no_notify")
            )

    return guarded


def _record_worker_exit(base: Path, error: str | None, notify: bool) -> None:
    """When this process is the recorded background worker: add ``exit`` to its record
    unless the supervisor recorded a clean end, and notify unless it stopped on
    request."""
    record = update_record(os.getpid(), {}, base)
    if record is None:
        return
    ended = record.get("exit")
    if not isinstance(ended, dict):
        ended = {
            "at": datetime.now(UTC).isoformat(),
            "reason": "error",
            "error": error or "exited unexpectedly",
        }
        update_record(os.getpid(), {"exit": ended}, base)
    if ended.get("reason") != "stopped":
        _notify_text(f"worker exited ({exit_text(ended)})", notify)


def _usage_warnings(config: dict[str, Any], ledger: Ledger) -> list[str]:
    now = datetime.now(UTC).timestamp()
    return subscription_warnings(
        config, usage(ledger, config, now, live=live_for(ledger, config, now))
    )


def _paused_exit(
    paused: dict[str, Any],
    outcomes: list[dict[str, Any]],
    excluded_task_ids: set[str],
    *,
    as_json: bool,
    notify: bool,
    warnings: list[str] | None = None,
    held_providers: dict[str, dict[str, Any]] | None = None,
) -> NoReturn:
    if outcomes:
        summary = _batch_summary(outcomes, excluded_task_ids, paused, warnings, held_providers)
        if as_json:
            typer.echo(json.dumps({"runs": outcomes, "paused": paused, "summary": summary}))
        else:
            _print_batch(summary)
        _notify_batch(summary, notify)
    elif as_json:
        typer.echo(json.dumps({"runs": outcomes, "paused": paused}))
    else:
        console.print(f"[yellow]Queue paused:[/yellow] {paused.get('reason')}")
    raise typer.Exit(code=EXIT_PAUSED)


# ── approve / reject ─────────────────────────────────────────────────


def _find_run(
    ledger: Ledger,
    ref: str,
    *,
    as_json: bool,
    failure: Callable[..., NoReturn] | None = None,
) -> dict[str, Any]:
    """Exact run id, or a unique prefix of one."""
    fail = failure or _fail
    row = ledger.get_run(ref)
    if row is not None:
        return row
    matches = [r for r in ledger.list_runs() if str(r["run_id"]).startswith(ref)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        fail(f"unknown run: {ref}", as_json=as_json, code=EXIT_ERROR)
    fail(
        f"ambiguous run prefix {ref}: " + ", ".join(str(m["run_id"]) for m in matches),
        as_json=as_json,
        code=EXIT_ERROR,
    )


def _awaiting_run(
    ledger: Ledger,
    ref: str,
    config: dict[str, Any],
    *,
    as_json: bool,
    statuses: Collection[str] = ("awaiting_approval",),
    failure: Callable[..., NoReturn] | None = None,
) -> tuple[dict[str, Any], TaskSpec]:
    fail = failure or _fail
    row = _find_run(ledger, ref, as_json=as_json, failure=failure)
    if row["status"] not in statuses:
        wanted = " or ".join(s.replace("_", " ") for s in statuses)
        fail(
            f"run {row['run_id']} is {row['status']}, not {wanted}",
            as_json=as_json,
            code=EXIT_VALIDATION,
        )
    task_row = ledger.get_task(str(row["task_id"]))
    if task_row is None:
        fail(f"run {row['run_id']} has no task row", as_json=as_json, code=EXIT_ERROR)
    task = replace(_task_from_row(task_row, config), delivery=str(row["delivery"]))
    return row, task


def _settle_run(
    ledger: Ledger,
    task: TaskSpec,
    run_id: str,
    status: str,
    data_dir: Path | None,
    failure_reason: str | None = None,
    *,
    ended_at: float | None = None,
    summary_note: str | None = None,
) -> None:
    ledger.finish_run(run_id, status, failure_reason, ended_at=ended_at)
    ledger.set_task_status(task.task_id, status)
    state = StateManager(task.project / ".helmraiser" / "state.db")
    if state.get_run(run_id) is not None:
        state.update_run_status(run_id, STATE_STATUS.get(status, "failed"))
    current = ledger.get_run(run_id)
    pr_url = (
        str(current["pr_url"]) if status == "landed" and current and current.get("pr_url") else None
    )
    try:
        restamp_summary(RunDir(run_id, data_dir), status, pr_url=pr_url, note=summary_note)
    except OSError:
        pass
    write_status(ledger, data_dir)


def _eligible_hint(
    ledger: Ledger, project: Path, data_dir: Path | None, config: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Queued tasks of ``project`` that ``work`` may start now that its run settled,
    and the background worker's state; ``None`` when there are none."""
    held = ledger.held_tasks(project=str(project), config=config)
    task_ids = [
        str(task["task_id"])
        for task in ledger.list_tasks(status="queued")
        if task["project"] == str(project) and task["task_id"] not in held
    ]
    if not task_ids:
        return None
    state = worker_state(data_dir)
    return {
        "task_ids": task_ids,
        "worker": state,
        "worker_serves_project": state == "running" and worker_serves(data_dir, project),
        "project": str(project),
    }


def _print_eligible(hint: dict[str, Any] | None) -> None:
    if hint is None:
        return
    typer.echo(f"  now eligible: {', '.join(hint['task_ids'])}")
    if hint["worker_serves_project"]:
        typer.echo("  the running worker picks them up")
    elif hint["worker"] == "running":
        typer.echo(
            "  the running worker does not serve this project: "
            f"helmraiser work --project {hint['project']}"
        )
    else:
        typer.echo("  no worker is running: helmraiser worker start")


def _hand_back(
    ledger: Ledger,
    run_id: str,
    task: TaskSpec,
    stage: str,
    data_dir: Path | None,
    *,
    rework: dict[str, Any] | None = None,
    reopen_pr: dict[str, Any] | None = None,
    scope: dict[str, Any] | None = None,
    restarted_without_commits: bool = False,
    as_json: bool = False,
) -> dict[str, Any]:
    """Mark the run ``resuming`` at ``stage``; the next ``work`` loop resumes it.
    ``scope``: the scope decision and note, for the resumed run."""
    if (
        not ledger.hand_back(run_id, stage, rework=rework, reopen_pr=reopen_pr)
        and rework is not None
    ):
        _fail(
            (
                f"run {run_id} is no longer eligible for PR reopening; rework refused"
                if reopen_pr
                else f"run {run_id} no longer has the pending merge approval; rework refused"
            ),
            as_json=as_json,
            code=EXIT_VALIDATION,
        )
    restart = {"restarted_without_commits": True} if restarted_without_commits else {}
    ledger.append_event(
        run_id,
        "helmraiser",
        "handed_back",
        {"stage": stage, **({"scope": scope} if scope else {}), **restart},
    )
    write_status(ledger, _data_dir(data_dir))
    state = worker_state(data_dir)
    serves = state == "running" and worker_serves(data_dir, task.project)
    return {
        "run_id": run_id,
        "task_id": task.task_id,
        "project": str(task.project),
        "status": "resuming",
        "stage": stage,
        "worker": state,
        "worker_serves_project": serves,
        **restart,
    }


def _print_handed_back(payload: dict[str, Any]) -> None:
    typer.echo(f"  the worker resumes run {payload['run_id']} at {payload['stage']}")
    if payload["worker_serves_project"]:
        typer.echo("  the running worker picks it up")
    elif payload["worker"] == "running":
        typer.echo(
            "  the running worker does not serve this project: "
            f"helmraiser work --project {payload['project']}"
        )
    else:
        typer.echo("  no worker is running: helmraiser worker start (or helmraiser work)")
    if payload.get("restarted_without_commits"):
        typer.echo("  no commits beyond the base: the worker restarts the run from its start")


class MergeApprovalRefused(RuntimeError):
    """Finalization was refused; the run remains awaiting approval."""


def _approve_merge(
    ledger: Ledger,
    row: dict[str, Any],
    task: TaskSpec,
    pending: dict[str, Any] | None,
    note: str | None,
    base: Path,
    *,
    manager: WorktreeManager | None = None,
    push: Callable[[Path, str, str], None] | None = None,
    deliver_pr: Callable[[str, str, str], str] | None = None,
) -> tuple[str | None, str]:
    """Deliver and settle a merge approval without rendering command output."""
    run_id = str(row["run_id"])
    pr_url: str | None = None
    try:
        manager = manager or worktree_manager(task.project)
        if task.delivery == "pr":
            existing_pr = str(row["pr_url"] or "")
            if existing_pr:
                state = landing.pr_state(task.project, existing_pr)
                if state != "OPEN":
                    raise RuntimeError(f"pull request is {state}; only OPEN PRs can be updated")
            base_sync = manager.require_current_base(str(row["base_branch"]))
            if base_sync["status"] == "synced":
                ledger.append_event(run_id, "helmraiser", "base_sync", base_sync)
            if existing_pr:
                contains = manager._git(
                    "merge-base",
                    "--is-ancestor",
                    f"refs/heads/{row['base_branch']}",
                    f"refs/heads/{row['branch_name']}",
                    check=False,
                )
                if contains.returncode != 0:
                    raise RuntimeError(
                        "published branch does not contain the current base; "
                        f"helmraiser rework {run_id} --rebase"
                    )
            iterations = ledger.iterations(run_id)
            verify_results: list[Any] = next(
                (
                    iteration["verify_results"]
                    for iteration in reversed(iterations)
                    if iteration.get("verify_results")
                ),
                [],
            )
            review_path = RunDir(run_id, base).review_md
            verdict = parse_verdict(
                review_path.read_text(encoding="utf-8") if review_path.is_file() else ""
            )
            # The ledger holds the governed verdict (after any forcing), review.md the raw one.
            verdict.verdict = str(row["verdict"] or verdict.verdict)
            body = pr_body(
                task,
                run_id,
                verdict,
                verify_results,
                ledger.evidence(run_id),
                float(row["cost_usd"] or 0.0),
            )
            metadata = manager.finalize_remote(
                task.id,
                run_id,
                rebase=not existing_pr,
                push=push or (landing.push_branch if existing_pr else None),
                deliver=lambda base_branch, head: existing_pr
                or (
                    deliver_pr(base_branch, head, body)
                    if deliver_pr is not None
                    else open_pull_request(
                        task.project, base=base_branch, head=head, title=task.title, body=body
                    )
                ),
            )
            pr_url = metadata["pr_url"]
            ledger.update_run(run_id, pr_url=pr_url)
            ledger.append_event(run_id, "helmraiser", "delivered", {"pr_url": pr_url})
        else:
            manager.finalize_local(task.id, run_id, rebase=True)
        delivered_branch = row["branch_name"] if task.delivery == "pr" else row["base_branch"]
        ref = f"refs/heads/{delivered_branch}"
        try:
            resolved = subprocess.run(
                ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
                cwd=task.project,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"cannot resolve delivered head {ref}: {exc}") from exc
        if resolved.returncode or not resolved.stdout.strip():
            raise RuntimeError(f"cannot resolve delivered head {ref}: {resolved.stderr.strip()}")
        head = resolved.stdout.strip()
        ledger.update_run(run_id, head_commit=head)
    except RuntimeError as exc:
        ledger.update_run(run_id, failure_reason=f"finalize_refused: {exc}")
        message = f"finalize_refused: {exc}"
        if str(exc).startswith("rebase onto moved HEAD failed"):
            message += (
                "; hand the run back to rebase and resolve: " f"helmraiser rework {run_id} --rebase"
            )
        raise MergeApprovalRefused(message) from exc
    if pending is not None and pending["kind"] == "merge":
        ledger.decide_approval(int(pending["id"]), "approved", note)
    else:
        ledger.record_approval(run_id, "merge", "approved", note)
    _settle_run(ledger, task, run_id, "landed", base)
    return pr_url, head


def approve(
    run: str = typer.Argument(..., help="Run id (or unique prefix)"),
    note: str | None = typer.Option(
        None,
        "--note",
        help="Recorded with the decision; a plan or scope approval or decline's note reaches "
        "the implementer",
    ),
    paths: str | None = typer.Option(
        None,
        "--paths",
        help="Scope request: approve only these of the requested paths (comma-separated)",
    ),
    decline: bool = typer.Option(
        False,
        "--decline",
        help="Decline a pending scope request (requires --note)",
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Merge an awaiting run (ff-merge, worktree removed) or, for a pending plan or
    scope request, hand the run back to the worker to resume at the implement stage.
    A scope approval first adds the approved paths to the task file's ``paths:``.
    With --decline --note, decline a scope request and resume without adding paths."""
    config = load_config()
    base = _data_dir(data_dir)
    ledger = _ledger(data_dir)
    row, task = _awaiting_run(ledger, run, config, as_json=as_json)
    run_id = str(row["run_id"])
    pending = ledger.pending_approval(run_id)

    if decline:
        if paths is not None:
            _fail(
                "--decline cannot be used with --paths",
                as_json=as_json,
                code=EXIT_VALIDATION,
            )
        if not note or not note.strip():
            _fail("--decline requires a non-empty --note", as_json=as_json, code=EXIT_VALIDATION)
        if pending is None or pending["kind"] != "scope":
            _fail(
                "--decline applies only to a pending scope request",
                as_json=as_json,
                code=EXIT_VALIDATION,
            )
        _decline_scope(ledger, row, task, pending, note, config, data_dir, as_json=as_json)
        return

    if pending is not None and pending["kind"] == "scope":
        _approve_scope(ledger, row, task, pending, paths, note, config, data_dir, as_json=as_json)
        return
    if paths is not None:
        _fail(
            "--paths applies only to a pending scope request",
            as_json=as_json,
            code=EXIT_VALIDATION,
        )

    if pending is not None and pending["kind"] == "plan":
        ledger.decide_approval(int(pending["id"]), "approved", note)
        if note:
            # The implementer reads plan.md, so the owner's note travels with the plan.
            plan_md = RunDir(run_id, base).plan_md
            with plan_md.open("a", encoding="utf-8") as fh:
                fh.write(f"\n## Owner's note on approval\n\n{note}\n")
        payload = _hand_back(ledger, run_id, task, "implement", data_dir)
        if as_json:
            typer.echo(json.dumps({"approved": "plan", **payload}))
        else:
            console.print(f"[bold]plan approved[/bold] for {task.task_id} run {run_id}")
            _print_handed_back(payload)
        return

    try:
        pr_url, head = _approve_merge(ledger, row, task, pending, note, base)
    except MergeApprovalRefused as exc:
        _fail(str(exc), as_json=as_json, code=EXIT_ERROR)
    hint = _eligible_hint(ledger, task.project, data_dir, config)
    if as_json:
        payload = {
            "approved": "merge",
            "run_id": run_id,
            "task_id": task.task_id,
            "status": "landed",
            "branch_name": row["branch_name"],
        }
        if pr_url:
            payload["pr_url"] = pr_url
        else:
            payload["head"] = head
        if hint:
            payload["eligible"] = hint
        typer.echo(json.dumps(payload))
        return
    if pr_url:
        action = "updated" if row["pr_url"] else "opened"
        console.print(f"[green]{action}[/green] {pr_url}")
    else:
        console.print(f"[green]landed[/green] {task.task_id} run {run_id} → {head[:12]}")
    _print_eligible(hint)
    if pr_url:
        typer.echo(pr_url)


def _decline_scope(
    ledger: Ledger,
    row: dict[str, Any],
    task: TaskSpec,
    pending: dict[str, Any],
    note: str,
    config: dict[str, Any],
    data_dir: Path | None,
    *,
    as_json: bool,
) -> None:
    """Re-read the owner's task and resume without granting any requested paths."""
    run_id = str(row["run_id"])
    request = pending_scope_request(ledger, run_id)
    requested = request["paths"] if request else []
    task_row = ledger.get_task(str(row["task_id"]))
    assert task_row is not None  # _awaiting_run refuses a run without one
    spec, _ = _reread_task(ledger, run_id, task_row, config, data_dir, as_json=as_json, parked=True)
    ledger.decide_approval(int(pending["id"]), "declined", note)
    payload = _hand_back(
        ledger,
        run_id,
        replace(spec, delivery=task.delivery),
        "implement",
        data_dir,
        scope={"paths": [], "declined": requested, "note": note},
    )
    if as_json:
        typer.echo(json.dumps({"declined": "scope", "paths": requested, **payload}))
    else:
        console.print(
            f"scope request declined for {task.task_id} run {run_id}: {', '.join(requested)}",
            markup=False,
        )
        typer.echo("  paths: is unchanged; the run resumes without new paths")
        _print_handed_back(payload)


def _approve_scope(
    ledger: Ledger,
    row: dict[str, Any],
    task: TaskSpec,
    pending: dict[str, Any],
    paths: str | None,
    note: str | None,
    config: dict[str, Any],
    data_dir: Path | None,
    *,
    as_json: bool,
) -> None:
    """Add the approved paths to the task file, re-read it and hand the run back."""
    run_id = str(row["run_id"])
    request = pending_scope_request(ledger, run_id)
    requested = request["paths"] if request else []
    approved = requested if paths is None else [p.strip() for p in paths.split(",") if p.strip()]
    unknown = [p for p in approved if p not in requested]
    if not approved or unknown:
        _fail(
            f"--paths must name requested paths ({', '.join(requested) or 'none'}); "
            f"not requested: {', '.join(unknown) or '(empty)'}",
            as_json=as_json,
            code=EXIT_VALIDATION,
        )
    task_row = ledger.get_task(str(row["task_id"]))
    assert task_row is not None  # _awaiting_run refuses a run without one
    source = Path(str(task_row["source_path"]))
    declared = _load(source, config, as_json=as_json).paths
    if declared is None:
        _fail(f"{source} declares no paths: to add to", as_json=as_json, code=EXIT_VALIDATION)
    original = source.read_text(encoding="utf-8")
    missing = [p for p in approved if p not in declared]
    if missing:
        try:
            edited = add_paths_to_frontmatter(original, missing)
            if set(approved) - set(parse_task(edited, source, config).paths or ()):
                raise ValueError("the edited paths: list does not parse as intended")
        except ValueError as exc:  # a TaskLoadError too
            _fail(f"cannot add the paths to {source}: {exc}", as_json=as_json, code=EXIT_VALIDATION)
        source.write_text(edited, encoding="utf-8")
    try:
        spec, _ = _reread_task(
            ledger, run_id, task_row, config, data_dir, as_json=as_json, parked=True
        )
    except typer.Exit:
        source.write_text(original, encoding="utf-8")  # the approval did not happen
        raise
    ledger.decide_approval(int(pending["id"]), "approved", note)
    payload = _hand_back(
        ledger,
        run_id,
        replace(spec, delivery=task.delivery),
        "implement",
        data_dir,
        scope={"paths": approved, "note": note},
    )
    if as_json:
        typer.echo(json.dumps({"approved": "scope", "paths": approved, **payload}))
    else:
        console.print(
            f"scope approved for {task.task_id} run {run_id}: {', '.join(approved)}",
            markup=False,
        )
        typer.echo(f"  added to paths: in {source}")
        _print_handed_back(payload)


def reject(
    run: str = typer.Argument(..., help="Run id (or unique prefix)"),
    note: str | None = typer.Option(None, "--note", help="Recorded with the rejection"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Reject a run that awaits approval or waits for the worker to resume it: worktree
    and metadata removed, branch kept, run marked failed."""
    config = load_config()
    base = _data_dir(data_dir)
    ledger = _ledger(data_dir)
    row, task = _awaiting_run(
        ledger, run, config, as_json=as_json, statuses=("awaiting_approval", "resuming")
    )
    run_id = str(row["run_id"])
    if row["status"] == "resuming" and not ledger.claim_resumable(run_id):
        _fail(f"run {run_id} was just resumed by the worker", as_json=as_json, code=EXIT_ERROR)
    pending = ledger.pending_approval(run_id)
    kind = str(pending["kind"]) if pending else "merge"

    removed = True
    remove_error: str | None = None
    try:
        worktree_manager(task.project).discard(task.id, run_id)
    except RuntimeError as exc:
        removed, remove_error = False, str(exc)
    if pending is not None:
        ledger.decide_approval(int(pending["id"]), "rejected", note)
    else:
        ledger.record_approval(run_id, kind, "rejected", note)
    _settle_run(ledger, task, run_id, "failed", base, "rejected")
    payload = {
        "rejected": kind,
        "run_id": run_id,
        "task_id": task.task_id,
        "status": "failed",
        "branch_name": row["branch_name"],
        "worktree_removed": removed,
        "worktree_error": remove_error,
    }
    hint = _eligible_hint(ledger, task.project, data_dir, config)
    if hint:
        payload["eligible"] = hint
    if as_json:
        typer.echo(json.dumps(payload))
    else:
        console.print(f"[red]rejected[/red] {task.task_id} run {run_id} ({kind})")
        console.print(f"  branch kept: {row['branch_name']}")
        if not removed:
            console.print(f"  worktree not removed: {remove_error}", markup=False)
        _print_eligible(hint)


# A failed run whose work may be complete: the implementer gave up (``blocked: …``), verify
# never passed, a budget ran out (``budget:*``) or the services would not start
# (``services: …``). ``retry`` verifies it again against the corrected task file;
# ``retry --implement`` gives it a new implement loop from its last verify failure.
RETRYABLE_FAILURES = ("blocked:", "verify_exhausted", "verify_failed", "budget:", "services:")


def rework(
    run: str = typer.Argument(..., help="Run id (or unique prefix)"),
    note: str | None = typer.Option(None, "--note", help="Approver's feedback to the implementer"),
    rebase: bool = typer.Option(
        False,
        "--rebase",
        help="Also rebase the run branch onto its current base branch "
        "(the implementer resolves conflicts)",
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Hand a merge approval or an open PR back for feedback, a base update, or both.

    The same run branch is kept and the task file is re-read, as ``retry`` does.
    """
    if note is None and not rebase:
        _fail("--note or --rebase is required", as_json=as_json, code=EXIT_VALIDATION)
    if note is not None and not note.strip():
        _fail("--note must be non-empty", as_json=as_json, code=EXIT_VALIDATION)
    config = load_config()
    ledger = _ledger(data_dir)
    run_id = str(_find_run(ledger, run, as_json=as_json)["run_id"])
    # Keep this separate from the worktree manager's finalization lock: restoring
    # takes that lock internally. Never unlink this file (waiters use its inode).
    run_dir = RunDir(run_id, _data_dir(data_dir)).path
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "rework.lock").open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            with ExitStack() as cleanup:
                _rework_run(ledger, run_id, config, note, rebase, as_json, data_dir, cleanup)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _rework_run(
    ledger: Ledger,
    run: str,
    config: dict[str, Any],
    note: str | None,
    rebase: bool,
    as_json: bool,
    data_dir: Path | None,
    cleanup: ExitStack,
) -> None:
    """Recheck admission and hand back while holding this run's rework lock."""
    row, task = _awaiting_run(
        ledger, run, config, as_json=as_json, statuses=("awaiting_approval", "landed")
    )
    run_id = str(row["run_id"])
    pending = ledger.pending_approval(run_id)
    reopening = row["status"] == "landed"
    if reopening:
        if row["delivery"] != "pr" or not row["pr_url"]:
            _fail(
                f"run {run_id} is landed but has no delivered PR to reopen",
                as_json=as_json,
                code=EXIT_VALIDATION,
            )
        try:
            state = landing.pr_state(task.project, str(row["pr_url"]))
        except RuntimeError as exc:
            _fail(str(exc), as_json=as_json, code=EXIT_ERROR)
        if state != "OPEN":
            _fail(
                f"run {run_id} pull request is {state}; only OPEN PRs can be reworked",
                as_json=as_json,
                code=EXIT_VALIDATION,
            )
    elif pending is None or pending["kind"] != "merge":
        _fail(
            f"run {run_id} requires a pending merge approval for rework (not a plan approval)",
            as_json=as_json,
            code=EXIT_VALIDATION,
        )
    task_row = ledger.get_task(str(row["task_id"]))
    assert task_row is not None
    if reopening:
        spec, _ = _reread_task(
            ledger, run_id, task_row, config, data_dir, as_json=as_json, parked=True
        )
        task = replace(spec, delivery=task.delivery)
    manager = worktree_manager(task.project)
    restored: dict[str, Any] | None = None
    worktree = Path(str(row["worktree_path"])) if row["worktree_path"] else None
    if reopening:
        try:
            restored = manager.restore_pr(
                task.id,
                run_id,
                branch_name=str(row["branch_name"]),
                base_branch=str(row["base_branch"]),
                worktree_path=str(row["worktree_path"]),
            )
            if restored["worktree_recreated"]:

                def discard_unaccepted_restoration() -> None:
                    current = ledger.get_run(run_id)
                    # A failure in rendering/events after a committed hand-back
                    # must not delete a tree the worker may already be using.
                    if current is not None and current["status"] == "landed":
                        try:
                            manager.discard(task.id, run_id)
                        except RuntimeError as exc:
                            _fail(
                                f"cannot remove restored worktree: {exc}",
                                as_json=as_json,
                                code=EXIT_ERROR,
                            )

                cleanup.callback(discard_unaccepted_restoration)
            restored["pr_url"] = row["pr_url"]
        except (RuntimeError, OSError, ValueError) as exc:
            _fail(f"cannot restore PR worktree: {exc}", as_json=as_json, code=EXIT_ERROR)
        present = True
    else:
        try:
            manager.metadata(run_id)
            present = worktree is not None and worktree.is_dir() and manager.exists(task.id, run_id)
        except RuntimeError:
            present = False
    if not present:
        _fail(
            f"run {run_id} cannot be reworked: its worktree {row['worktree_path']} "
            f"or branch {row['branch_name']} is gone",
            as_json=as_json,
            code=EXIT_ERROR,
        )
    base = row["base_branch"]
    if rebase:
        if not base:
            _fail(f"run {run_id} has no base branch", as_json=as_json, code=EXIT_ERROR)
        branch = row["branch_name"]
        compared = subprocess.run(
            ["git", "merge-base", "--is-ancestor", f"refs/heads/{base}", f"refs/heads/{branch}"],
            cwd=task.project,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if compared.returncode == 0:
            _fail(
                f"run {run_id} already contains {base}; nothing to rebase",
                as_json=as_json,
                code=EXIT_VALIDATION,
            )
        if compared.returncode != 1:
            _fail(
                f"cannot compare {branch} with {base}: {compared.stderr.strip()}",
                as_json=as_json,
                code=EXIT_ERROR,
            )
    review = next(
        (e["payload"] for e in reversed(ledger.events(run_id, types=("review_verdict",)))),
        None,
    )
    if note is None:
        note = REBASE_NOTE
        findings: list[dict[str, Any]] = []
    elif review is not None:
        findings = review.get("findings") or []
    else:
        review_path = RunDir(run_id, _data_dir(data_dir)).review_md
        findings = parse_verdict(
            review_path.read_text(encoding="utf-8") if review_path.is_file() else ""
        ).findings
    if not reopening:
        spec, _ = _reread_task(
            ledger, run_id, task_row, config, data_dir, as_json=as_json, parked=True
        )
        task = replace(spec, delivery=task.delivery)
    allowance = min(2, task.budget.max_iterations)
    share = allowance / task.budget.max_iterations
    payload = _hand_back(
        ledger,
        run_id,
        task,
        "implement",
        data_dir,
        as_json=as_json,
        rework={
            **({"rebase": True} if rebase else {}),
            **({"worktree_recreated": restored["worktree_recreated"]} if restored else {}),
            "approval_id": pending["id"] if pending else None,
            "note": note,
            "findings": findings,
            "iteration_allowance": allowance,
            "extra_cost_usd": task.budget.max_cost_usd * share,
            "extra_minutes": task.budget.max_minutes * share,
        },
        reopen_pr=restored,
    )
    if as_json:
        typer.echo(
            json.dumps({"reworked": "merge", **({"rebase": True} if rebase else {}), **payload})
        )
    else:
        suffix = f" (rebase onto {base})" if rebase else ""
        typer.echo(f"rework requested for {task.task_id} run {run_id}{suffix}")
        _print_handed_back(payload)


def _reread_task(
    ledger: Ledger,
    run_id: str,
    task_row: dict[str, Any],
    config: dict[str, Any],
    data_dir: Path | None,
    *,
    as_json: bool,
    parked: bool = False,
    check: Callable[[TaskSpec, Path], None] | None = None,
) -> tuple[TaskSpec, Path]:
    """Validate the task file and make it the run's snapshot (``retry`` and ``rework``).
    ``check`` may refuse the validated file before anything is written."""
    source = Path(str(task_row["source_path"]))
    spec = _load(source, config, as_json=as_json)
    if spec.task_id != task_row["task_id"]:
        _fail(
            f"{source} is now task {spec.task_id}, not {task_row['task_id']}",
            as_json=as_json,
            code=EXIT_VALIDATION,
        )
    errors = validate_task(replace(spec, status="queued"))
    if errors:
        _fail("invalid task", as_json=as_json, code=EXIT_VALIDATION, errors=errors)
    if spec.policy == "auto" and load_project_defaults(spec.project).require_approval:
        _fail(
            "project requires approval: policy auto is not allowed",
            as_json=as_json,
            code=EXIT_VALIDATION,
        )
    if check is not None:
        check(spec, source)

    snapshot = source.read_text(encoding="utf-8")
    try:
        ledger.set_task_snapshot(spec.task_id, snapshot, parked_run=run_id if parked else None)
    except TaskActive as exc:
        _fail(str(exc), as_json=as_json, code=EXIT_VALIDATION)
    RunDir(run_id, _data_dir(data_dir)).task_md.write_text(snapshot, encoding="utf-8")
    return spec, source


def _exhausted_budgets(ledger: Ledger, row: dict[str, Any], budget: Budget) -> list[str]:
    """Each cap a run out of budget has already reached. A budget stays cumulative across
    a budget retry, so a run needs room on all three or it fails again straight away."""
    run_id = str(row["run_id"])
    reworks = ledger.reworks(run_id)
    extras = " plus rework extras" if reworks else ""
    exhausted = []
    iterations = len(ledger.iterations(run_id))
    if budget.max_iterations <= iterations:
        exhausted.append(
            f"iterations: {iterations} used, limit {budget.max_iterations} (max_iterations)"
        )
    cost = float(row["cost_usd"] or 0.0)
    max_cost = budget.max_cost_usd + sum(r["extra_cost_usd"] for r in reworks)
    if max_cost <= cost:
        exhausted.append(f"cost: ${cost:.2f} used, limit ${max_cost:.2f} (max_cost_usd{extras})")
    minutes = completed_active_minutes(
        ledger.events(run_id, types=("run_start", "run_resume", "run_end")),
        started_at=float(row["started_at"]),
        ended_at=row["ended_at"],
    )
    max_minutes = budget.max_minutes + sum(r["extra_minutes"] for r in reworks)
    if max_minutes <= minutes:
        exhausted.append(
            f"time: {minutes:.1f} min used, limit {max_minutes:.1f} min (max_minutes{extras})"
        )
    return exhausted


def retry(
    run: str = typer.Argument(..., help="Run id (or unique prefix)"),
    implement: bool = typer.Option(
        False,
        "--implement",
        help="Resume at implement with the last verify failure, or after a budget failure "
        "to continue the work; without commits, restart from start as plain retry does",
    ),
    adopt_base: bool = typer.Option(
        False,
        "--adopt-base",
        help="Record an already-completed host rebase onto the run's current base branch",
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Re-read a failed run's task file and hand it back to the worker.
    ``--adopt-base`` records an already-completed host rebase. Existing work
    resumes at verify → evidence → review, or with ``--implement`` at implement with
    the last verify failure. With no commits and a clean worktree (or a setup or services
    failure), both forms restart at start: setup if needed, plan if needed, then
    implementation. Setup failures are retryable only without commits; the worktree must
    still exist. A ``budget:*`` failure needs a task file with room on every cap: budgets
    stay cumulative, so ``max_iterations: 12`` means 12 in all, not 12 more; with it,
    ``--implement`` continues the work even without a failed verify result."""
    config = load_config()
    ledger = _ledger(data_dir)
    row = _find_run(ledger, run, as_json=as_json)
    run_id = str(row["run_id"])
    reason = str(row["failure_reason"] or "")
    if row["status"] != "failed":
        _fail(f"run {run_id} is {row['status']}, not failed", as_json=as_json, code=EXIT_VALIDATION)
    cleaned = ledger.events(run_id, types=("worktree_cleaned",))
    if cleaned:
        worktree = Path(str(row["worktree_path"])) if row["worktree_path"] else None
        gone = worktree is None or not worktree.is_dir()
        if not gone:
            manager = worktree_manager(Path(str(row["project"])))
            topic_id = str(row["task_id"]).rsplit(":", 1)[-1]
            gone = manager.get_info(topic_id, run_id) is None
        if gone:
            event = cleaned[-1]
            payload = event["payload"]
            branch = payload.get("branch_name") or row["branch_name"]
            _fail(
                f"run {run_id} cannot be retried: its worktree was cleaned "
                f"({payload['reason']}) on {_iso(event['ts'])}; "
                f"branch {branch} was kept; queue the task again instead",
                as_json=as_json,
                code=EXIT_ERROR,
            )
    setup_failed = reason.startswith("setup:")
    budget_failed = reason.startswith("budget:")
    # Both fail before any implement session, so what they leave behind is setup debris.
    restarts_with_debris = setup_failed or reason.startswith("services:")
    reason_error = (
        f"run {run_id} failed with {reason or 'no reason'}; retry is only for a run that was "
        "blocked, failed verify, ran out of budget or could not start its services, "
        "or failed setup without commits"
    )
    if not reason.startswith(RETRYABLE_FAILURES) and not setup_failed:
        _fail(reason_error, as_json=as_json, code=EXIT_VALIDATION)
    task_row = ledger.get_task(str(row["task_id"]))
    if task_row is None:
        _fail(f"run {run_id} has no task row", as_json=as_json, code=EXIT_ERROR)
    latest = max(
        (r for r in ledger.list_runs() if r["task_id"] == row["task_id"]),
        key=lambda r: float(r["started_at"]),
    )
    if task_row["status"] != "failed" or latest["run_id"] != run_id:
        _fail(
            f"task {task_row['task_id']} has moved on since run {run_id} "
            f"(now {task_row['status']}); only its latest failed run can be retried",
            as_json=as_json,
            code=EXIT_VALIDATION,
        )

    manager = worktree_manager(Path(str(row["project"])))
    worktree = Path(str(row["worktree_path"])) if row["worktree_path"] else None
    try:
        manager.metadata(run_id)
        present = (
            worktree is not None
            and worktree.is_dir()
            and manager.exists(str(task_row["id"]), run_id)
        )
    except RuntimeError:
        present = False
    adopted_base = None
    if adopt_base:

        def git(*args: str) -> subprocess.CompletedProcess[str]:
            try:
                result = subprocess.run(
                    ["git", *args],
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                _fail(f"cannot adopt base: {exc}", as_json=as_json, code=EXIT_ERROR)
            if result.returncode != 0 and not (args[0] == "merge-base" and result.returncode == 1):
                _fail(
                    f"cannot adopt base: git {' '.join(args)} failed: {result.stderr.strip()}",
                    as_json=as_json,
                    code=EXIT_ERROR,
                )
            return result

        # An unfinished rebase detaches HEAD, so manager.exists() reports it gone.
        # Classify that refusal before the generic missing-worktree error below.
        if worktree is not None and worktree.is_dir():
            for name in ("rebase-merge", "rebase-apply"):
                path = Path(git("rev-parse", "--git-path", name).stdout.strip())
                if not path.is_absolute():
                    path = worktree / path
                if path.is_dir():
                    _fail(
                        "cannot adopt base: a rebase is in progress and unfinished in the worktree",
                        as_json=as_json,
                        code=EXIT_VALIDATION,
                    )
    if not present or worktree is None:
        _fail(
            f"run {run_id} cannot be retried: its worktree {row['worktree_path']} "
            f"or branch {row['branch_name']} is gone",
            as_json=as_json,
            code=EXIT_ERROR,
        )
    if adopt_base:
        base = row["base_branch"]
        if not base:
            _fail("cannot adopt base: run has no base branch", as_json=as_json, code=EXIT_ERROR)
        tip = git("rev-parse", "--verify", f"refs/heads/{base}").stdout.strip()
        if not tip:
            _fail(f"cannot resolve base branch {base}", as_json=as_json, code=EXIT_ERROR)
        if git("merge-base", "--is-ancestor", tip, "HEAD").returncode == 1:
            _fail(
                f"base branch {base} at {tip} is not an ancestor of HEAD; "
                "rebase the worktree onto it first",
                as_json=as_json,
                code=EXIT_VALIDATION,
            )
        if tip == row["base_commit"]:
            _fail(
                f"base branch {base} at {tip} is already recorded; nothing to adopt",
                as_json=as_json,
                code=EXIT_VALIDATION,
            )
        adopted_base = {"base_branch": base, "base_commit": tip, "previous": row["base_commit"]}

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree, capture_output=True, text=True, check=False
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=worktree, capture_output=True, text=True, check=False
    ).stdout.strip()
    prospective_base = adopted_base["base_commit"] if adopted_base else row["base_commit"]
    would_restart = head == prospective_base and (not dirty or restarts_with_debris)
    if setup_failed and not would_restart:
        _fail(reason_error, as_json=as_json, code=EXIT_VALIDATION)

    if (
        implement
        and not would_restart
        and not budget_failed
        and all(result.passed for result in last_verify_results(ledger.iterations(run_id)))
    ):
        _fail(
            f"run {run_id} has no failed verify result to hand back to the implementer; "
            "retry it without --implement",
            as_json=as_json,
            code=EXIT_VALIDATION,
        )

    def check_budget_room(spec: TaskSpec, source: Path) -> None:
        exhausted = _exhausted_budgets(ledger, row, spec.budget)
        if exhausted:
            _fail(
                f"run {run_id} has no budget left to retry ({reason}): "
                f"{'; '.join(exhausted)}. Budgets stay cumulative: raise them in {source}",
                as_json=as_json,
                code=EXIT_VALIDATION,
            )

    spec, source = _reread_task(
        ledger,
        run_id,
        task_row,
        config,
        data_dir,
        as_json=as_json,
        check=check_budget_room if budget_failed else None,
    )
    if adopted_base:
        ledger.update_run(run_id, base_commit=adopted_base["base_commit"])
        manager.adopt_base(run_id, adopted_base["base_commit"])
        ledger.append_event(
            run_id,
            "helmraiser",
            "rebased",
            {**adopted_base, "by": "retry --adopt-base"},
            stage="implement",
        )
        row["base_commit"] = adopted_base["base_commit"]
    restartable = head == row["base_commit"] and (not dirty or restarts_with_debris)
    payload = _hand_back(
        ledger,
        run_id,
        spec,
        "start" if restartable else "implement" if implement else "verify",
        data_dir,
        restarted_without_commits=restartable,
    )
    if adopted_base:
        payload["adopted_base"] = adopted_base
    if as_json:
        typer.echo(json.dumps({"retried": reason, **payload}))
    else:
        typer.echo(f"retrying {spec.task_id} run {run_id} (failed: {reason})")
        typer.echo(f"  task re-read from {source}")
        if adopted_base:
            typer.echo(
                f"adopted base {adopted_base['base_branch']} at "
                f"{adopted_base['base_commit'][:12]} (was {adopted_base['previous'][:12]})"
            )
        _print_handed_back(payload)


def record_landed(
    run: str = typer.Argument(..., help="Run id (or unique prefix)"),
    commit: str | None = typer.Option(
        None,
        "--commit",
        help="Required: the commit that delivered the work; it must be in the base",
    ),
    note: str | None = typer.Option(
        None, "--note", help="Required: why the run is recorded as landed"
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Record a failed or aborted run as landed because its work was merged into the base
    branch outside Helmraiser, which releases the tasks that depend on it.
    ``--commit`` must already be in the run's base branch; no ref moves. Helmraiser does
    not verify or review it. The branch is kept, and a clean worktree is removed."""
    if commit is None or not commit.strip():
        _fail(
            "--commit is required: the commit that delivered the run's work",
            as_json=as_json,
            code=EXIT_VALIDATION,
        )
    if note is None or not note.strip():
        _fail(
            "--note is required and must not be blank: say why the run is recorded as landed",
            as_json=as_json,
            code=EXIT_VALIDATION,
        )
    config = load_config()
    ledger = _ledger(data_dir)
    row = _find_run(ledger, run, as_json=as_json)
    run_id = str(row["run_id"])
    project = str(row["project"])
    if row["status"] not in ("failed", "aborted"):
        _fail(
            f"run {run_id} is {row['status']}, not failed or aborted; "
            "only a run that ended without landing can be recorded as landed",
            as_json=as_json,
            code=EXIT_VALIDATION,
        )
    task_row = ledger.get_task(str(row["task_id"]))
    if task_row is None:
        _fail(f"run {run_id} has no task row", as_json=as_json, code=EXIT_ERROR)
    latest = max(
        ledger.list_runs(task_id=str(row["task_id"])),
        key=lambda r: (float(r["started_at"]), str(r["run_id"])),
    )
    if task_row["status"] != row["status"] or latest["run_id"] != run_id:
        _fail(
            f"task {task_row['task_id']} has moved on since run {run_id} "
            f"(now {task_row['status']}); only its latest run can be recorded as landed",
            as_json=as_json,
            code=EXIT_VALIDATION,
        )
    try:
        task = _task_from_row(task_row, config)
    except (TaskLoadError, ValueError, OSError) as exc:
        _fail(
            f"cannot read the task snapshot of {task_row['task_id']}: {exc}",
            as_json=as_json,
            code=EXIT_ERROR,
        )

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=project,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _fail(f"git {' '.join(args)} failed: {exc}", as_json=as_json, code=EXIT_ERROR)

    rev = commit.strip()
    resolved = None if rev.startswith("-") else git("rev-parse", "--verify", f"{rev}^{{commit}}")
    if resolved is None or resolved.returncode != 0:
        _fail(f"{rev} is not a commit in {project}", as_json=as_json, code=EXIT_VALIDATION)
    sha = resolved.stdout.strip()
    base = str(row["base_branch"] or "")
    if not base:
        _fail(f"run {run_id} has no base branch recorded", as_json=as_json, code=EXIT_ERROR)
    tip = git("rev-parse", "--verify", f"refs/heads/{base}^{{commit}}")
    if tip.returncode != 0:
        _fail(f"base branch {base} not found in {project}", as_json=as_json, code=EXIT_ERROR)
    # The dependency hold compares a landed run's head with its dependents' base, so a
    # commit that is not in the base would release nothing and be a false record.
    ancestry = git("merge-base", "--is-ancestor", sha, tip.stdout.strip())
    if ancestry.returncode == 1:
        _fail(
            f"commit {sha} is not in base branch {base}; merge it there first",
            as_json=as_json,
            code=EXIT_VALIDATION,
        )
    if ancestry.returncode != 0:
        _fail(
            f"git merge-base --is-ancestor failed: {ancestry.stderr.strip()}",
            as_json=as_json,
            code=EXIT_ERROR,
        )
    ledger.update_run(run_id, head_commit=sha)
    # The run's work ended when it failed; recording the landing later must not move that
    # (spend windows and durations read ended_at).
    _settle_run(
        ledger,
        task,
        run_id,
        "landed",
        _data_dir(data_dir),
        ended_at=row["ended_at"],
        summary_note=(
            f"landed outside Helmraiser at `{sha[:12]}`: not verified or reviewed by Helmraiser"
            f" — {' '.join(note.split())}"
        ),
    )
    ledger.append_event(
        run_id,
        "helmraiser",
        "landed_outside",
        {
            "commit": sha,
            "note": note,
            "previous_status": row["status"],
            "failure_reason": row["failure_reason"],
            "branch": row["branch_name"],
            "previous_head_commit": row["head_commit"],
        },
    )
    worktree = discard_landed_worktree(ledger, row)
    hint = _eligible_hint(ledger, task.project, data_dir, config)
    if as_json:
        payload: dict[str, Any] = {
            "recorded": "landed_outside",
            "run_id": run_id,
            "task_id": task.task_id,
            "status": "landed",
            "previous_status": row["status"],
            "failure_reason": row["failure_reason"],
            "commit": sha,
            "base_branch": base,
            "branch_name": row["branch_name"],
            "note": note,
            "worktree": worktree,
        }
        if hint:
            payload["eligible"] = hint
        typer.echo(json.dumps(payload))
        return
    was = f"{row['status']}: {row['failure_reason']}" if row["failure_reason"] else row["status"]
    typer.echo(f"recorded {task.task_id} run {run_id} as landed at {sha[:12]} (was {was})")
    typer.echo(f"  {base} contains the commit; Helmraiser did not verify or review it")
    typer.echo(f"  note: {note}")
    typer.echo(f"  branch {row['branch_name']} kept")
    if worktree["action"] == "removed":
        typer.echo(f"  worktree removed: {worktree['path']}")
    elif worktree["action"] == "kept":
        typer.echo(f"  worktree kept ({worktree['detail']}): {worktree['path']}")
    else:
        typer.echo("  worktree already gone")
    _print_eligible(hint)


def register_commands(app: typer.Typer) -> None:
    from helmraiser.cli.caps_commands import caps_app
    from helmraiser.cli.clean_commands import clean
    from helmraiser.cli.compare_commands import compare
    from helmraiser.cli.land_commands import land
    from helmraiser.cli.monitor import register_monitor_commands  # avoids an import cycle
    from helmraiser.cli.preflight_commands import preflight
    from helmraiser.cli.scout_commands import drafts_app, scout

    register_monitor_commands(app)
    app.command("preflight")(preflight)
    app.command("compare")(compare)
    app.command("scout")(scout)
    app.add_typer(drafts_app, name="drafts")
    app.command("add")(add)
    app.command("intake")(intake)
    app.command("work")(_watch_exit_guard(work))
    app.command("approve")(approve)
    app.command("land")(land)
    app.command("clean")(clean)
    app.command("reject")(reject)
    app.command("retry")(retry)
    app.command("record-landed")(record_landed)
    app.command("rework")(rework)
    app.add_typer(queue_app, name="queue")
    app.add_typer(caps_app, name="caps")
    app.add_typer(worker_app, name="worker")
