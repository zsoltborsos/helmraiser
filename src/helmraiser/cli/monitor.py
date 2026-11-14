"""Ledger-first monitoring commands (spec §5): ``status``, ``wait``, ``report``, ``log``,
``show``, ``attach``. Registered on the root app by ``commands.register_commands``."""

from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from helmraiser import logbook
from helmraiser.cli.commands import (
    EXIT_ERROR,
    EXIT_VALIDATION,
    _data_dir,
    _fail,
    _find_run,
    console,
)
from helmraiser.cli.commands import _ledger as open_ledger
from helmraiser.cli.worker_commands import exit_text
from helmraiser.config import load_config, missing_models
from helmraiser.isolation import for_level
from helmraiser.isolation.environment import stripped_names, stripped_path_entries
from helmraiser.isolation.exposure import ExposureReport, run_exposure
from helmraiser.isolation.process import (
    ProcessIsolation,
    SrtNotFoundError,
    tracked_denial_warnings,
    tracked_denials,
)
from helmraiser.ledger.db import PARKED_RUN_STATUSES, TERMINAL_RUN_STATUSES, Ledger
from helmraiser.ledger.rundir import RunDir
from helmraiser.loop.budget import completed_active_minutes, iteration_limit
from helmraiser.loop.model_report import REPORT_GROUPINGS, build_model_report, render_models_section
from helmraiser.loop.reconcile import find_phantom_runs, orphaned_services
from helmraiser.loop.report import build_report, parse_since, pending_scope_request, render_report
from helmraiser.loop.routing import format_dispatch_decision
from helmraiser.loop.services import docker_ready, labelled_containers
from helmraiser.loop.spend import subscription_warnings, usage_summary
from helmraiser.loop.status import (
    build_status,
    ended_since,
    held_task_text,
    run_origin,
    status_path,
    write_status_atomic,
)
from helmraiser.loop.verify import last_verify_results
from helmraiser.pi.adapter import PiVersionError, check_auth, pi_version, session_id_for
from helmraiser.project_notes import read_project_notes
from helmraiser.task.loader import parse_task
from helmraiser.task.model import Budget, ModelSpec
from helmraiser.task.project import load_project_defaults, pnpm_store_check, worktree_manager

ATTACH_STAGES: tuple[str, ...] = ("implement", "review", "plan")
SUMMARY_WIDTH = 100


# ── doctor / status ───────────────────────────────────────────────────


def _repository(path: Path | None = None) -> Path | None:
    target = path.expanduser().resolve() if path is not None else None
    if target is not None and not target.is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=target,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def doctor(
    repo: Path | None = typer.Argument(None, help="Repository to inspect or probe"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
    exposure: bool = typer.Option(False, "--exposure", help="Probe run isolation from the host"),
) -> None:
    """Check readiness, or prove run-component isolation with ``--exposure``."""
    config = load_config()
    selected_repo = _repository(repo)
    if repo is not None and selected_repo is None:
        _fail(f"not a git repository: {repo}", as_json=as_json, code=EXIT_VALIDATION)
    if exposure:
        target = selected_repo
        if target is None:
            _fail("not a git repository", as_json=as_json, code=EXIT_VALIDATION)
        try:
            report: ExposureReport = run_exposure(target, config, data_dir=_data_dir(data_dir))
        except ValueError as exc:
            _fail(str(exc), as_json=as_json, code=EXIT_VALIDATION)
        if as_json:
            typer.echo(json.dumps(report.as_dict()))
        else:
            for exposure_result in report.results:
                typer.echo(
                    f"{exposure_result.component} {exposure_result.probe} "
                    f"{exposure_result.status} [control {exposure_result.control}]: "
                    f"{exposure_result.reason}"
                )
        if not report.safe:
            raise typer.Exit(code=EXIT_ERROR)
        if not report.complete:
            raise typer.Exit(code=EXIT_VALIDATION)
        return
    pi_bin = str(config.get("pi_bin", "pi"))
    try:
        installed_version: str | None = pi_version(pi_bin)
    except PiVersionError:
        installed_version = None

    auth: dict[str, str | None] = {}
    missing = missing_models(config, config.get("profiles", {}))
    for name, profile in config.get("profiles", {}).items():
        auth[str(name)] = (
            "not_configured" if name in missing else check_auth(ModelSpec.parse(profile), pi_bin)
        )

    isolation: dict[str, dict[str, Any]] = {}
    for level in ("none", "process", "vm"):
        reason = for_level(level, config).check_available()
        isolation[level] = {"available": reason is None, "reason": reason}

    srt: dict[str, str | None]
    try:
        srt = {"path": ProcessIsolation(config).srt_path(), "error": None}
    except SrtNotFoundError as exc:
        srt = {"path": None, "error": str(exc)}

    base = _data_dir(data_dir)
    db_path = base / "helmraiser.db"
    ledger_exists = db_path.is_file()
    ledger_opens = False
    ledger: Ledger | None = None
    paused: dict[str, Any] | None = None
    phantom_runs: list[dict[str, Any]] = []
    if ledger_exists:
        try:
            ledger = Ledger(db_path)
            paused = ledger.is_paused()
            phantom_runs = [phantom.as_dict() for phantom in find_phantom_runs(ledger)]
            ledger_opens = True
        except (OSError, sqlite3.Error):
            pass

    repo = selected_repo
    project_notes = read_project_notes(repo) if repo is not None else None
    project_defaults = load_project_defaults(repo) if repo is not None else None
    orphaned = worktree_manager(repo).orphaned_metadata() if repo is not None else []
    tracked: dict[str, list[str]] = {}
    pnpm_store: dict[str, Any] | None = None
    git_maintenance: dict[str, Any] | None = None
    if repo is not None:
        auto = None
        try:
            result = subprocess.run(
                ["git", "config", "--local", "--get", "maintenance.auto"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            if result.returncode == 0:
                auto = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        git_maintenance = {"auto": auto, "managed": auto == "false"}
    if repo is not None and project_defaults is not None:
        pnpm_store = pnpm_store_check(
            repo, project_defaults, worktree_manager(repo).worktree_dir, os.environ
        )
        process_isolation = ProcessIsolation(config, deny_read=project_defaults.deny_read)
        tracked = tracked_denials(repo, process_isolation.configured_deny_read())
    gh_check: dict[str, Any] | None = None
    if project_defaults is not None and project_defaults.delivery == "pr":
        gh_path = shutil.which("gh")
        detail: str | None = None
        gh_ready = False
        if gh_path is not None:
            result = subprocess.run(
                [gh_path, "auth", "status"], capture_output=True, text=True, check=False
            )
            output = result.stdout.strip() or result.stderr.strip()
            detail = output.splitlines()[0] if output else None
            gh_ready = result.returncode == 0
        gh_check = {"ready": gh_ready, "detail": detail or "gh not found on PATH"}
    services_check: dict[str, Any] | None = None
    if project_defaults is not None and project_defaults.services:
        docker_reason = (
            docker_ready() if shutil.which("docker") is not None else "docker not found on PATH"
        )
        containers = labelled_containers() if docker_reason is None else None
        services_check = {
            "declared": [service.name for service in project_defaults.services],
            "docker": {"ready": docker_reason is None, "detail": docker_reason},
            # helmraiser.run-labelled containers of runs that are over or unknown here;
            # the next `helmraiser work` removes them.
            "orphaned": orphaned_services(ledger if ledger_opens else None, containers or []),
        }

    stripped = stripped_names()
    stripped_paths = stripped_path_entries()
    lb = logbook.find_lb()
    logbook_home = logbook.logbook_home()
    logbook_exists = logbook_home.exists()
    payload = {
        "logbook": {"lb": lb, "home": str(logbook_home), "exists": logbook_exists},
        "pi": {
            "version": installed_version,
            "auth": auth,
        },
        "isolation": isolation,
        "srt": srt,
        "data_dir": str(base),
        "ledger": {
            "exists": ledger_exists,
            "opens": ledger_opens,
            "paused": paused,
        },
        "phantom_runs": phantom_runs,
        "worktrees": {
            "repo": str(repo) if repo is not None else None,
            "orphaned": orphaned,
        },
        "git_maintenance": git_maintenance,
        "pnpm_store": pnpm_store,
        "project_notes": {
            "repo": str(repo) if repo is not None else None,
            "exists": project_notes is not None,
            "size_bytes": project_notes.size_bytes if project_notes is not None else None,
        },
        # Variable values stay private; only removed PATH entries are reported.
        "environment": {
            "stripped_from_runs": stripped,
            "stripped_path_entries": stripped_paths,
        },
        # Relative deny_read entries that hide tracked files of the current repository.
        "sandbox": {"tracked_deny_read": {entry: files[:20] for entry, files in tracked.items()}},
    }
    if gh_check is not None:
        payload["gh"] = gh_check
    if services_check is not None:
        payload["services"] = services_check
    ready = (
        installed_version is not None
        and all(reason is None for reason in auth.values())
        # vm is reserved and always unavailable; only the levels a task can use gate readiness.
        and all(isolation[level]["available"] for level in ("none", "process"))
        and (not ledger_exists or ledger_opens)
        and paused is None
        and (gh_check is None or bool(gh_check["ready"]))
        and (services_check is None or bool(services_check["docker"]["ready"]))
    )

    if as_json:
        typer.echo(json.dumps(payload))
    else:
        table = Table(title="Helmraiser doctor")
        table.add_column("Check")
        table.add_column("Status")
        table.add_column("Detail")

        def add_row(name: str, ok: bool, detail: str | None = None) -> None:
            table.add_row(
                name,
                "[green]ready[/green]" if ok else "[red]not ready[/red]",
                detail or "-",
            )

        table.add_row(
            "logbook",
            "[cyan]info[/cyan]",
            escape(
                f"{lb or 'not installed (optional)'}; store {logbook_home} "
                f"({'exists' if logbook_exists else 'missing'})"
            ),
        )
        add_row("pi version", installed_version is not None, installed_version)
        for name, reason in auth.items():
            add_row(
                f"pi auth: {name}",
                reason is None,
                "not configured" if reason == "not_configured" else reason,
            )
        for level, check in isolation.items():
            if level == "vm":
                table.add_row("isolation: vm", "[cyan]info[/cyan]", "reserved; not implemented")
            else:
                add_row(f"isolation: {level}", bool(check["available"]), check["reason"])
        table.add_row("srt path", "[cyan]info[/cyan]", escape(srt["path"] or srt["error"] or "-"))
        add_row("data dir", base.is_dir(), str(base))
        if ledger_exists:
            add_row("ledger exists", ledger_exists, str(db_path))
            add_row("ledger opens", ledger_opens)
        else:
            table.add_row(
                "ledger exists",
                "[cyan]info[/cyan]",
                escape(f"{db_path}; not created yet; created on first use"),
            )
        add_row("queue paused", paused is None, paused["reason"] if paused else None)
        if phantom_runs:
            detail = ", ".join(f"{item['run_id'][:8]} ({item['task_id']})" for item in phantom_runs)
            detail += "; run `helmraiser work` to reconcile"
            table.add_row("phantom runs", "[cyan]info[/cyan]", detail)
        if repo is not None:
            table.add_row(
                "project notes",
                "[cyan]info[/cyan]",
                f"{project_notes.size_bytes} bytes" if project_notes is not None else "not found",
            )
        if pnpm_store is not None:
            warnings = pnpm_store["warnings"]
            table.add_row(
                "pnpm store",
                "[yellow]warn[/yellow]" if warnings else "[green]ready[/green]",
                escape("; ".join(warnings) if warnings else pnpm_store["store"]),
            )
        if project_defaults is not None and project_defaults.deny_read:
            table.add_row(
                "sandbox deny_read",
                "[cyan]info[/cyan]",
                ", ".join(project_defaults.deny_read),
            )
        for warning in tracked_denial_warnings(tracked):
            table.add_row("sandbox tracked files", "[yellow]warn[/yellow]", warning)
        if stripped or stripped_paths:
            details = []
            if stripped:
                details.append("stripped from runs: " + ", ".join(stripped))
            if stripped_paths:
                details.append("stripped from PATH: " + ", ".join(stripped_paths))
            table.add_row("environment", "[cyan]info[/cyan]", "; ".join(details))
        if gh_check is not None:
            add_row("gh", bool(gh_check["ready"]), str(gh_check["detail"]))
        if services_check is not None:
            add_row(
                "docker (services)",
                bool(services_check["docker"]["ready"]),
                services_check["docker"]["detail"] or ", ".join(services_check["declared"]),
            )
            for orphan in services_check["orphaned"]:
                table.add_row(
                    "orphaned service",
                    "[yellow]warn[/yellow]",
                    f"{orphan['name']} (run {orphan['run_id'][:8]}, {orphan['run_status']});"
                    " `helmraiser work` removes it",
                )
        if "night_max_cost_usd" in (config.get("budget") or {}):
            table.add_row(
                "budget.night_max_cost_usd",
                "[cyan]info[/cyan]",
                "no longer used; see subscriptions",
            )
        if "caps" in (config.get("budget") or {}):
            table.add_row(
                "budget.caps",
                "[cyan]info[/cyan]",
                "no longer used; see subscriptions",
            )
        if orphaned:
            summary = ", ".join(f"{item['run_id'][:8]} ({item['topic_id']})" for item in orphaned)
            cleanup = "; ".join(
                f"git branch -D {item['branch_name']} and delete " f"{item['metadata_path']}"
                for item in orphaned
            )
            detail = f"{len(orphaned)}: {summary}. To clear: {cleanup}"
        else:
            detail = "none"
        table.add_row("orphaned run state", "[cyan]info[/cyan]", detail)
        console.print(table)
        if git_maintenance is not None and not git_maintenance["managed"]:
            console.print(
                "Automatic git maintenance can delete live sessions' ref directories; "
                "the next run or `git config maintenance.auto false` fixes it."
            )

    if not ready:
        raise typer.Exit(code=EXIT_ERROR)


def _pending_kind(ledger: Ledger, run_id: str) -> str:
    pending = ledger.pending_approval(run_id)
    return str(pending["kind"]) if pending else "merge"


def _wait_run_row(
    ledger: Ledger,
    run: dict[str, Any] | None,
    task_id: str,
    task_status: str,
    *,
    timed_out: bool,
) -> dict[str, Any]:
    """Serialize only ledger data, without consulting or writing run-directory files."""
    fields = ("run_id", "verdict", "failure_reason", "branch_name", "pr_url", "stage", "project")
    row = {field: run[field] if run else None for field in fields}
    row.update(
        task_id=run["task_id"] if run else task_id,
        status=run["status"] if run else task_status,
        cost_usd=round(float(run["cost_usd"] or 0.0), 4) if run else None,
        pending=None,
        timed_out=timed_out,
    )
    for field in ("started_at", "ended_at"):
        ts = run[field] if run else None
        row[field] = datetime.fromtimestamp(ts, tz=UTC).isoformat() if ts is not None else None
    if run and run["status"] == "awaiting_approval":
        row["pending"] = _pending_kind(ledger, str(run["run_id"]))
        if row["pending"] == "scope":
            scope = pending_scope_request(ledger, str(run["run_id"]))
            if scope:
                row["scope_request"] = scope
    return row


def _resolve_wait_ref(
    ledger: Ledger, ref: str, *, as_json: bool, error_code: int = EXIT_VALIDATION
) -> tuple[str, str]:
    guidance = "expected run id, task id (short id or <project-key>:<id>), or unique run prefix"
    if ledger.get_run(ref):
        return "run", ref
    tasks = [
        task
        for task in ledger.list_tasks()
        if (task["task_id"] == ref if ":" in ref else task["id"] == ref)
    ]
    if len(tasks) > 1:
        matches = ", ".join(sorted(str(task["task_id"]) for task in tasks))
        _fail(
            f"ambiguous task id: {ref}; matches: {matches}; {guidance}",
            as_json=as_json,
            code=error_code,
        )
    if tasks:
        return "task", str(tasks[0]["task_id"])
    runs = sorted(
        str(run["run_id"]) for run in ledger.list_runs() if str(run["run_id"]).startswith(ref)
    )
    if len(runs) > 1:
        _fail(
            f"ambiguous run prefix: {ref}; matches: {', '.join(runs)}; {guidance}",
            as_json=as_json,
            code=error_code,
        )
    if runs:
        return "run", runs[0]
    _fail(f"unknown ref: {ref}; {guidance}", as_json=as_json, code=error_code)


def _awaiting_rows(ledger: Ledger, data_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for run in ledger.list_runs(status="awaiting_approval"):
        pending = _pending_kind(ledger, str(run["run_id"]))
        task = ledger.get_task(run["task_id"])
        reworks = ledger.reworks(str(run["run_id"]))
        scope = pending_scope_request(ledger, str(run["run_id"]))
        rows.append(
            {
                "run_id": run["run_id"],
                "task": run["task_id"],
                "project": run["project"],
                "id": task["id"] if task else None,
                "title": task["title"] if task else None,
                "pending": pending,
                **({"scope_request": scope} if scope else {}),
                **({"rework_rounds": len(reworks), "reworks": reworks} if reworks else {}),
                "verdict": run["verdict"],
                "cost_usd": round(float(run["cost_usd"] or 0.0), 4),
                "worktree": run["worktree_path"],
                "branch": run["branch_name"],
                "base_commit": run["base_commit"],
                "origin": run_origin(str(run["run_id"]), data_dir),
            }
        )
    return rows


def _origin_suffix(origin: Any) -> str:
    if not isinstance(origin, dict):
        return ""
    return f" (origin: {origin['kind']}:{origin['session'][:8]})"


def _verify_hints(ledger: Ledger, run_id: str) -> list[str]:
    """The environment hints of the run's last failed verify steps, once each."""
    results = last_verify_results(ledger.iterations(run_id))
    return list(dict.fromkeys(hint for result in results for hint in result.hints))


def status(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Queue depth, running runs, approvals, failures and provider spend."""
    base = _data_dir(data_dir)
    ledger = open_ledger(data_dir)
    now = time.time()
    payload = build_status(ledger, now, load_config(), base)
    path = write_status_atomic(payload, status_path(base))
    queue: dict[str, int] = {}
    for task in ledger.list_tasks():
        task_status = str(task["status"])
        if task_status in ("draft", "dropped"):
            continue
        queue[task_status] = queue.get(task_status, 0) + 1
    ended: dict[str, list[dict[str, Any]]] = {"failed": [], "landed": []}
    for lane, rows in ended.items():
        for r in ended_since(ledger, lane, now - 24 * 3600):
            task_row = ledger.get_task(r["task_id"])
            rows.append(
                {
                    "run_id": r["run_id"],
                    "task": r["task_id"],
                    "project": r["project"],
                    "id": task_row["id"] if task_row else None,
                    "title": task_row["title"] if task_row else None,
                    "origin": run_origin(str(r["run_id"]), base),
                    **(
                        {
                            "reason": r["failure_reason"],
                            "hints": _verify_hints(ledger, str(r["run_id"])),
                        }
                        if lane == "failed"
                        else {}
                    ),
                    "ended_at": _iso(r["ended_at"]),
                }
            )
    full = {
        **payload,
        "queue": queue,
        "awaiting": _awaiting_rows(ledger, base),
        "failed": ended["failed"],
        "landed": ended["landed"],
        "status_path": str(path),
    }
    if as_json:
        typer.echo(json.dumps(full))
        return

    paused = payload["paused"]
    pause_text = f"[yellow]paused ({paused['reason']})[/yellow]" if paused else "not paused"
    depth = ", ".join(f"{k} {v}" for k, v in sorted(queue.items())) or "empty"
    spend = (
        ", ".join(f"{name} ${windows['5h']:.4f}/5h" for name, windows in payload["spend"].items())
        or "$0/5h"
    )
    usage_line = usage_summary(payload["usage"], payload["routing"]["headroom"])
    console.print(
        f"[bold]Queue:[/bold] {depth} — {pause_text} — spend {spend} — "
        f"failed (24h) {payload['failed_24h_count']}",
        soft_wrap=True,
    )
    if payload["drafts"]:
        drafts = ", ".join(f"{project} {count}" for project, count in payload["drafts"].items())
        console.print(f"[bold]Drafts:[/bold] {drafts}", soft_wrap=True)
    if usage_line:
        console.print(f"[bold]Usage:[/bold] {usage_line}", soft_wrap=True)
    for item in payload["never_startable"]:
        console.print(
            f"[bold red]Never starts:[/bold red] {item['task_id']}: {item['text']}", soft_wrap=True
        )
    for name, hold in payload["held_providers"].items():
        local = datetime.fromtimestamp(float(hold["until"])).strftime("%Y-%m-%d %H:%M")
        console.print(f"[yellow]Provider held:[/yellow] {name} until {local}", soft_wrap=True)
    for line in subscription_warnings(load_config(), payload["usage"]):
        console.print(f"[yellow]{line}[/yellow]", soft_wrap=True)
    worker = payload["worker"]
    if worker["state"] != "none":
        console.print(f"[bold]Worker:[/bold] {escape(_worker_text(worker))}", soft_wrap=True)
        for error in worker.get("errors") or []:
            text = f"{error['project']} ({error['stage']}): {error['error']}"
            console.print(f"[red]Project error:[/red] {escape(text)}", soft_wrap=True)
    for scout_row in payload["scouts"]["pending"]:
        reason = scout_row["wait_reason"] or "ready"
        console.print(
            f"Scout pending: {scout_row['project']} ({reason})", markup=False, soft_wrap=True
        )
    for scout_row in payload["scouts"]["running"]:
        console.print(
            f"Scout running: {scout_row['project']} {str(scout_row['scout_id'])[:8]}",
            markup=False,
            soft_wrap=True,
        )
    if payload["running"]:
        table = Table(title="Running")
        for column in ("run_id", "task", "stage", "iter", "cost", "min"):
            table.add_column(column)
        for run in payload["running"]:
            table.add_row(
                str(run["run_id"])[:8],
                str(run["task"]),
                str(run["stage"] or "-"),
                str(run["iteration"]),
                f"{run['cost_usd']:.4f}",
                str(run["minutes"]),
            )
        console.print(table)
    else:
        console.print("[bold]Running:[/bold] none")
    for run in payload["resuming"]:
        suffix = ""
        if run["held_by"] is not None:
            suffix = ", waiting: " + escape(held_task_text(run["task"], run["held_by"]))
        console.print(
            f"[bold]Resuming:[/bold] {str(run['run_id'])[:8]} {run['task']} at {run['stage']}{suffix}",
            soft_wrap=True,
        )
    for item in payload["held"]:
        console.print(
            f"[yellow]Held:[/yellow] {escape(held_task_text(item['task_id'], item))}",
            soft_wrap=True,
        )
    for item in worker.get("skips") or []:
        text = f"{item['task_id']}: {item['reason']}"
        console.print(f"Skipped: {escape(text)}", soft_wrap=True, emoji=False)
    if full["awaiting"]:
        table = Table(title="Awaiting your approval")
        for column in ("run_id", "task", "pending", "verdict", "cost"):
            table.add_column(column)
        for row in full["awaiting"]:
            table.add_row(
                str(row["run_id"]),
                str(row["task"]) + _origin_suffix(row["origin"]),
                str(row["pending"]),
                str(row["verdict"] or "-"),
                f"{row['cost_usd']:.4f}",
            )
        console.print(table)
        for row in full["awaiting"]:
            if "scope_request" in row:
                request = row["scope_request"]
                console.print(
                    f"  scope request {str(row['run_id'])[:8]}: {', '.join(request['paths'])} — "
                    f"{request['reason'] or '(no reason given)'}",
                    markup=False,
                    soft_wrap=True,
                )
        console.print("  next: helmraiser approve <run> (or reject)", markup=False)
        if any(row["pending"] == "merge" for row in full["awaiting"]):
            console.print(
                '  merge rework: helmraiser rework <run> --note "<feedback>"', markup=False
            )
    for item in payload["reworks"]:
        label = "Reworked (24h)" if item["status"] in TERMINAL_RUN_STATUSES else "Rework"
        console.print(
            f"{label}: {item['run_id']} {item['task']} ({item['status']}) — "
            f"{item['rework_rounds']} round(s)",
            markup=False,
            soft_wrap=True,
        )
        for request in item["reworks"]:
            console.print(
                f"  round {request['round']} note:\n{request['note']}", markup=False, soft_wrap=True
            )
    for row in ended["failed"]:
        console.print(
            f"[red]failed[/red] {str(row['run_id'])[:8]} {row['task']}: {row['reason']}",
            soft_wrap=True,
        )
    console.print(f"[dim]status.json: {path}[/dim]", soft_wrap=True)


def _worker_text(worker: dict[str, Any]) -> str:
    """``running (pid 12, 1 of 2 runs, code 30f784a)``, ``exited (error: …)``."""
    if worker["state"] == "running":
        runs = (
            f", {len(worker.get('children') or [])} of {worker['max_parallel_runs']} runs"
            if "max_parallel_runs" in worker
            else ""
        )
        code = worker.get("code")
        if isinstance(code, dict) and code.get("commit"):
            runs += f", code {str(code['commit'])[:7]}"
        return f"running (pid {worker['pid']}{runs})"
    ended = worker.get("exit")
    return f"exited ({exit_text(ended)})" if isinstance(ended, dict) else "exited"


# ── report ────────────────────────────────────────────────────────────


def report(
    since: str = typer.Option("24h", "--since", help="Window: 24h, 7d or 30d"),
    models: bool = typer.Option(False, "--models", help="Add the model efficiency table"),
    by: str = typer.Option(
        "model",
        "--by",
        help=(
            "With --models, group by implement model (model) or by the whole "
            "model setup: plan, implement, fallback and review (setup)"
        ),
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Morning report: Landed, Awaiting your approval, Failed, Cost (markdown)."""
    try:
        seconds = parse_since(since)
    except ValueError as exc:
        _fail(str(exc), as_json=as_json, code=EXIT_VALIDATION)
    if by not in REPORT_GROUPINGS:
        _fail(
            f"--by must be one of {', '.join(REPORT_GROUPINGS)}",
            as_json=as_json,
            code=EXIT_VALIDATION,
        )
    if by != "model" and not models:
        _fail("--by needs --models", as_json=as_json, code=EXIT_VALIDATION)
    config = load_config()
    ledger = open_ledger(data_dir)
    data = build_report(ledger, _data_dir(data_dir), seconds, config=config)
    if models:
        data["models"] = build_model_report(
            ledger, _data_dir(data_dir), seconds, config=config, by=by
        )
    if as_json:
        typer.echo(json.dumps({"since": since, **data}))
    else:
        text = render_report(data, since)
        if models:
            text += render_models_section(data["models"], by=by)
        typer.echo(text)


# ── wait ──────────────────────────────────────────────────────────────


def _print_wait_result(row: dict[str, Any]) -> None:
    console.print(
        f"{row['status']} {row['task_id']} run {row['run_id'] or '-'} — "
        f"${row['cost_usd'] or 0.0:.4f}",
        markup=False,
    )
    for key, label in (
        ("failure_reason", "reason"),
        ("pending", "pending"),
        ("branch_name", "branch"),
        ("pr_url", "pr"),
        ("verdict", "review"),
    ):
        if row[key]:
            console.print(f"  {label}: {row[key]}", markup=False)
    if "scope_request" in row:
        request = row["scope_request"]
        console.print(
            f"  scope request {str(row['run_id'])[:8]}: {', '.join(request['paths'])} — "
            f"{request['reason'] or '(no reason given)'}",
            markup=False,
        )
    if row["status"] == "awaiting_approval":
        console.print(f"  next: helmraiser approve {row['run_id']} (or reject)", markup=False)
    if row["timed_out"]:
        typer.echo("Timed out waiting for the requested condition.", err=True)


def wait(
    ref: str = typer.Argument(..., help="Run id, task id, or unique run prefix"),
    until: str = typer.Option("attention", "--until", help="Wait for attention or end"),
    timeout: str | None = typer.Option(None, "--timeout", help="Maximum seconds to wait"),
    interval: str = typer.Option("10", "--interval", help="Poll interval in seconds (at least 1)"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Wait silently until a run needs its owner or has ended (ledger reads only)."""
    try:
        if until not in ("attention", "end"):
            _fail(f"{ref}: --until must be attention or end", as_json=as_json, code=2)
        durations: dict[str, float] = {}
        for option, value, minimum in (("timeout", timeout, 0), ("interval", interval, 1)):
            if value is None:
                continue
            try:
                seconds = float(value)
            except ValueError:
                seconds = float("nan")
            if not math.isfinite(seconds) or seconds < minimum:
                _fail(
                    f"{ref}: --{option} must be finite and at least {minimum} seconds",
                    as_json=as_json,
                    code=2,
                )
            durations[option] = seconds
        deadline = time.monotonic() + durations["timeout"] if timeout is not None else None
        try:
            ledger = open_ledger(data_dir, read_only=True)
        except (OSError, sqlite3.Error, ValueError) as exc:
            _fail(f"{ref}: cannot read ledger: {exc}", as_json=as_json, code=2)
        with ledger:
            kind, canonical_id = _resolve_wait_ref(ledger, ref, as_json=as_json)
            history: set[str] = set()
            if kind == "task":
                # A worker may claim the task between these reads. Its new run
                # must not be excluded as history after observing queued.
                with ledger.read_snapshot():
                    task = ledger.get_task(canonical_id)
                    assert task is not None
                    if task["status"] == "queued":
                        history = {
                            str(run["run_id"]) for run in ledger.list_runs(task_id=canonical_id)
                        }
            stop_statuses = TERMINAL_RUN_STATUSES
            if until == "attention":
                stop_statuses = stop_statuses | PARKED_RUN_STATUSES
            while True:
                task_id, task_status = canonical_id, "queued"
                if kind == "run":
                    run = ledger.get_run(canonical_id)
                    if run is None:
                        _fail(f"{ref}: run no longer exists", as_json=as_json, code=2)
                else:
                    task = ledger.get_task(canonical_id)
                    if task is None:
                        _fail(f"{ref}: task no longer exists", as_json=as_json, code=2)
                    task_status = str(task["status"])
                    runs = [
                        r
                        for r in ledger.list_runs(task_id=canonical_id)
                        if r["run_id"] not in history
                    ]
                    run = max(runs, key=lambda r: (r["started_at"], r["run_id"]), default=None)
                    if run is None and task_status != "queued":
                        _fail(
                            f"{ref}: task in status {task_status} has no run",
                            as_json=as_json,
                            code=2,
                        )
                met = run is not None and run["status"] in stop_statuses
                remaining = deadline - time.monotonic() if deadline is not None else None
                timed_out = not met and remaining is not None and remaining <= 0
                if met or timed_out:
                    row = _wait_run_row(ledger, run, task_id, task_status, timed_out=timed_out)
                    if as_json:
                        typer.echo(json.dumps(row))
                    else:
                        _print_wait_result(row)
                    code = (
                        124 if timed_out else (1 if row["status"] in ("failed", "aborted") else 0)
                    )
                    raise typer.Exit(code)
                delay = durations["interval"]
                time.sleep(min(delay, remaining) if remaining is not None else delay)
    except KeyboardInterrupt:
        raise typer.Exit(130) from None


# ── log ───────────────────────────────────────────────────────────────


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat() if ts else None


def summarize_payload(event_type: str, payload: dict[str, Any]) -> str:
    """One line per event for the human timeline."""
    text: str
    if event_type == "message_end" and isinstance(payload.get("message"), dict):
        message = payload["message"]
        parts = [
            c.get("text", "")
            for c in message.get("content") or []
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        text = f"{message.get('role', '?')}: " + " ".join(parts).strip()
    elif event_type == "tool_execution_start":
        args = payload.get("args")
        text = f"{payload.get('toolName', '?')} {json.dumps(args, default=str) if args else ''}"
    elif event_type == "tool_execution_end":
        text = f"{payload.get('toolName', '?')} {'error' if payload.get('isError') else 'ok'}"
    elif (
        event_type == "routing"
        and payload.get("lever") in ("plan", "implement")
        and "from" in payload
        and "to" in payload
    ):
        text = format_dispatch_decision(payload)
    elif (
        event_type == "handed_back"
        and isinstance(payload.get("scope"), dict)
        and "declined" in payload["scope"]
    ):
        scope = payload["scope"]
        text = f"scope request declined ({', '.join(scope['declined'])}): {scope['note']}"
    elif event_type == "scope_line_ignored":
        text = str(payload.get("line", ""))
    elif event_type == "landed_outside":
        reason = f" ({payload['failure_reason']})" if payload.get("failure_reason") else ""
        text = (
            f"{str(payload.get('commit'))[:12]} was {payload.get('previous_status')}{reason}: "
            f"{payload.get('note')}"
        )
    elif event_type == "iteration_start":
        text = f"n={payload.get('n')} attempt={payload.get('attempt')} model={payload.get('model')}"
    elif event_type == "verify_result":
        results = payload.get("results") or []
        marks = ", ".join(
            f"{r.get('name')}={'pass' if r.get('passed') else 'FAIL'}" for r in results
        )
        text = f"passed={payload.get('passed')} {marks}"
    elif event_type == "budget_exceeded" and "limit_source" in payload:
        text = (
            f"{payload.get('kind')}: {payload['iterations']} used, limit {payload['limit']} "
            f"({payload['limit_source']}), cost_usd={round(payload['cost_usd'], 4)}, "
            f"minutes={round(payload['minutes'], 2)}"
        )
    else:
        text = ", ".join(
            f"{k}={json.dumps(v, default=str)}" for k, v in payload.items() if k != "type"
        )
    text = " ".join(text.split())
    return text if len(text) <= SUMMARY_WIDTH else text[: SUMMARY_WIDTH - 1] + "…"


def format_event(event: dict[str, Any]) -> str:
    stamp = datetime.fromtimestamp(float(event["ts"]), tz=UTC).strftime("%H:%M:%S")
    return (
        f"{stamp} {event['id']:>6} {event['source']:<8} {(event['stage'] or '-'):<9} "
        f"{event['type']:<22} {summarize_payload(str(event['type']), event['payload'])}"
    )


def log(
    run: str = typer.Argument(..., help="Run id (or unique prefix)"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Poll until the run is terminal"),
    since_id: int = typer.Option(0, "--since-id", help="Only events with id > N"),
    interval: float = typer.Option(2.0, "--interval", help="Poll interval for --follow (s)"),
    as_json: bool = typer.Option(False, "--json", help="JSONL, one event per line"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Print a run's ledger events as a timeline (or JSONL with --json)."""
    ledger = open_ledger(data_dir)
    row = _find_run(ledger, run, as_json=as_json)
    run_id = str(row["run_id"])
    last = since_id
    while True:
        for event in ledger.events(run_id, since_id=last):
            last = int(event["id"])
            typer.echo(json.dumps(event, default=str) if as_json else format_event(event))
        if not follow:
            return
        current = ledger.get_run(run_id) or row
        if current["status"] in TERMINAL_RUN_STATUSES or current["status"] == "awaiting_approval":
            return
        time.sleep(interval)


# ── show ──────────────────────────────────────────────────────────────


def _snapshot_budget(
    ledger: Ledger, row: dict[str, Any], data_dir: Path
) -> tuple[Budget | None, str | None]:
    """The budget of the run's own task snapshot: its run directory's ``task.md``, else the
    ledger's copy. Never the source file, which may have changed since."""
    task_row = ledger.get_task(str(row["task_id"]))
    source = Path(str(task_row["source_path"])) if task_row else Path(str(row["task_id"]))
    try:
        text = RunDir(str(row["run_id"]), data_dir).task_md.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        if task_row is None:
            return None, "task snapshot unreadable: the run's task is not in the ledger"
        text = str(task_row["snapshot"])
    try:
        return parse_task(text, source, load_config()).budget, None
    except (ValueError, OSError) as exc:
        return None, f"task snapshot unreadable: {exc}"


def build_show(ledger: Ledger, row: dict[str, Any], data_dir: Path, now: float) -> dict[str, Any]:
    """One run's state and limits, read only. The limits are what the runner enforces:
    ``loop/budget.py`` decides the iteration limit, and the cost and time limits are the
    snapshot's caps plus every rework's extras."""
    run_id = str(row["run_id"])
    reworks = ledger.reworks(run_id)
    budget, warning = _snapshot_budget(ledger, row, data_dir)

    iterations_used = len(ledger.iterations(run_id))
    limit: int | None = None
    limit_source: str | None = None
    if budget is not None:
        resume_events = ledger.events(run_id, types=("run_resume", "handed_back"))
        limit, limit_source = iteration_limit(budget.max_iterations, reworks, resume_events)

    segments = ledger.events(run_id, types=("run_start", "run_resume", "run_end"))
    minutes_used = completed_active_minutes(
        segments, started_at=float(row["started_at"]), ended_at=row["ended_at"]
    )
    if row["status"] == "running" and segments and segments[-1]["type"] != "run_end":
        minutes_used += max(0.0, now - float(segments[-1]["ts"])) / 60

    pending = ledger.pending_approval(run_id)
    outside = ledger.events(run_id, types=("landed_outside",))
    worktree = row["worktree_path"]
    return {
        "run_id": run_id,
        "task_id": row["task_id"],
        "project": row["project"],
        "status": row["status"],
        "stage": row["stage"],
        "failure_reason": row["failure_reason"],
        "verdict": row["verdict"],
        "policy": row["policy"],
        "delivery": row["delivery"],
        "base_branch": row["base_branch"],
        "base_commit": row["base_commit"],
        "head_commit": row["head_commit"],
        "branch_name": row["branch_name"],
        "pr_url": row["pr_url"],
        "worktree_path": worktree,
        "worktree_exists": bool(worktree) and Path(str(worktree)).is_dir(),
        "started_at": _iso(row["started_at"]),
        "ended_at": _iso(row["ended_at"]),
        "pending_approval": pending["kind"] if pending else None,
        "rework_rounds": len(reworks),
        "landed_outside": (
            {"commit": outside[-1]["payload"]["commit"], "note": outside[-1]["payload"]["note"]}
            if outside
            else None
        ),
        "warnings": [warning] if warning else [],
        "limits": {
            "iterations": {"used": iterations_used, "limit": limit, "limit_source": limit_source},
            "cost_usd": {
                "used": round(float(row["cost_usd"] or 0.0), 4),
                "limit": (
                    round(budget.max_cost_usd + sum(r["extra_cost_usd"] for r in reworks), 4)
                    if budget
                    else None
                ),
            },
            "minutes": {
                "used": round(minutes_used, 2),
                "limit": (
                    round(budget.max_minutes + sum(r["extra_minutes"] for r in reworks), 2)
                    if budget
                    else None
                ),
            },
        },
    }


SHOW_FIELDS: tuple[str, ...] = (
    "run_id",
    "task_id",
    "project",
    "status",
    "stage",
    "failure_reason",
    "verdict",
    "policy",
    "delivery",
    "base_branch",
    "base_commit",
    "head_commit",
    "branch_name",
    "started_at",
    "ended_at",
    "pending_approval",
)


def _print_show(data: dict[str, Any]) -> None:
    """Plain ``typer.echo`` lines, so a pipe never wraps them."""
    if data["pr_url"]:
        typer.echo(f"pr: {data['pr_url']}")
    for key in SHOW_FIELDS:
        if data[key] is not None:
            typer.echo(f"{key}: {data[key]}")
    if data["worktree_path"]:
        state = "exists" if data["worktree_exists"] else "missing"
        typer.echo(f"worktree_path: {data['worktree_path']} ({state})")
    if data["rework_rounds"]:
        typer.echo(f"rework_rounds: {data['rework_rounds']}")
    if data["landed_outside"]:
        landed = data["landed_outside"]
        typer.echo(f"landed_outside: {landed['commit']} {' '.join(str(landed['note']).split())}")
    limits = data["limits"]
    iterations = limits["iterations"]
    source = f" ({iterations['limit_source']})" if iterations["limit_source"] else ""
    typer.echo(
        f"iterations: {iterations['used']} of {_limit_text(iterations['limit'], 'd')}{source}"
    )
    cost = limits["cost_usd"]
    typer.echo(f"cost_usd: {cost['used']:.4f} of {_limit_text(cost['limit'], '.4f')}")
    minutes = limits["minutes"]
    typer.echo(f"minutes: {minutes['used']:.1f} of {_limit_text(minutes['limit'], '.1f')}")
    for warning in data["warnings"]:
        typer.echo(f"warning: {warning}")


def _limit_text(limit: float | None, spec: str) -> str:
    return "unknown" if limit is None else format(limit, spec)


def show(
    run: str = typer.Argument(
        ..., help="Run id, task id (short id or <project-key>:<id>), or unique run prefix"
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """One run's state and limits: status, branch, worktree, and iterations, cost and minutes
    used against the limits its task snapshot and reworks set. A task reference selects
    its most recent run. Reads the ledger only."""
    try:
        ledger = open_ledger(data_dir, read_only=True)
    except (OSError, sqlite3.Error, ValueError) as exc:
        _fail(f"cannot read ledger: {exc}", as_json=as_json, code=EXIT_ERROR)
    with ledger:
        kind, canonical_id = _resolve_wait_ref(ledger, run, as_json=as_json, error_code=EXIT_ERROR)
        if kind == "task":
            row = max(
                ledger.list_runs(task_id=canonical_id),
                key=lambda row: (row["started_at"], row["run_id"]),
                default=None,
            )
        else:
            row = ledger.get_run(canonical_id)
        if row is None:
            message = f"task {run} has no runs" if kind == "task" else f"unknown run: {run}"
            _fail(message, as_json=as_json, code=EXIT_ERROR)
        data = build_show(ledger, row, _data_dir(data_dir), time.time())
    if as_json:
        typer.echo(json.dumps(data))
    else:
        _print_show(data)


# ── attach ────────────────────────────────────────────────────────────


def attach_plan(ledger: Ledger, row: dict[str, Any], stage: str, data_dir: Path) -> dict[str, Any]:
    run_id = str(row["run_id"])
    rundir = RunDir(run_id, data_dir)
    if stage == "implement":
        iterations = ledger.iterations(run_id)
        session_id = (
            str(iterations[-1]["session_id"]) if iterations else session_id_for(run_id, "implement")
        )
    else:
        session_id = session_id_for(run_id, stage)
    session_file = next(iter(sorted(rundir.sessions.glob(f"*_{session_id}.jsonl"))), None)
    worktree = Path(str(row["worktree_path"])) if row["worktree_path"] else None
    worktree_exists = worktree is not None and worktree.is_dir()
    pi_bin = str(load_config().get("pi_bin", "pi"))
    return {
        "run_id": run_id,
        "stage": stage,
        "session_id": session_id,
        "session_file": str(session_file) if session_file else None,
        "argv": [pi_bin, "--session-dir", str(rundir.sessions), "--session", session_id],
        "cwd": str(worktree if worktree_exists else rundir.path),
        "worktree": str(worktree) if worktree else None,
        "worktree_exists": worktree_exists,
    }


def attach(
    run: str = typer.Argument(..., help="Run id (or unique prefix)"),
    stage: str = typer.Option("implement", "--stage", help="implement, review or plan"),
    exec_: bool = typer.Option(False, "--exec", help="Replace this process with pi"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Print (or with --exec run) the pi command that opens a run's session."""
    if stage not in ATTACH_STAGES:
        _fail(
            f"--stage must be one of {', '.join(ATTACH_STAGES)}",
            as_json=as_json,
            code=EXIT_VALIDATION,
        )
    base = _data_dir(data_dir)
    ledger = open_ledger(data_dir)
    row = _find_run(ledger, run, as_json=as_json)
    plan = attach_plan(ledger, row, stage, base)
    if plan["session_file"] is None:
        _fail(
            f"run {plan['run_id']} has no {stage} session under {RunDir(plan['run_id'], base).sessions}",
            as_json=as_json,
            code=EXIT_ERROR,
        )
    if as_json:
        typer.echo(json.dumps(plan))
    else:
        if plan["worktree"] and not plan["worktree_exists"]:
            console.print(
                f"[yellow]worktree {plan['worktree']} is gone;[/yellow] cwd is the run dir"
            )
        typer.echo(f"cd {shlex.quote(plan['cwd'])} && {shlex.join(plan['argv'])}")
    if exec_:
        os.chdir(plan["cwd"])
        os.execvp(plan["argv"][0], plan["argv"])


def register_monitor_commands(app: typer.Typer) -> None:
    app.command("status")(status)
    app.command("wait", context_settings={"ignore_unknown_options": True})(wait)
    app.command("report")(report)
    app.command("log")(log)
    app.command("show", context_settings={"ignore_unknown_options": True})(show)
    app.command("attach")(attach)
