"""Atomic ``status.json`` writer (spec §5.1)."""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from helmraiser.config import load_config
from helmraiser.ledger.db import (
    IDLE_RUN_STATUSES,
    RESUMING_RUN_STATUS,
    TERMINAL_RUN_STATUSES,
    Ledger,
    default_data_dir,
)
from helmraiser.ledger.rundir import RunDir
from helmraiser.loop.pairs import awaiting_pick_count, run_pair
from helmraiser.loop.routing import calculate_headroom
from helmraiser.loop.spend import (
    caps_enforcement,
    live_for,
    never_startable,
    never_startable_text,
    spend_by_provider,
    usage,
)
from helmraiser.task.loader import TaskLoadError, origin_from_text, parse_task
from helmraiser.task.model import PRIORITIES, TaskOrigin


def status_path(data_dir: Path | None = None) -> Path:
    """``$XDG_RUNTIME_DIR/helmraiser/status.json``, else ``$HELMRAISER_DATA_DIR/status.json``."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        return Path(runtime) / "helmraiser" / "status.json"
    return (data_dir or default_data_dir()) / "status.json"


def _snapshot_origin(snapshot: Any) -> TaskOrigin | None:
    try:
        origin = origin_from_text(str(snapshot))
    except (TaskLoadError, TypeError, ValueError):
        return None
    if origin is None:
        return None
    return {"kind": origin["kind"], "session": origin["session"]}


def _task_origin(task_row: dict[str, Any]) -> TaskOrigin | None:
    return _snapshot_origin(task_row.get("snapshot"))


def held_task_text(task_id: str, hold: dict[str, Any]) -> str:
    """Format project-run and/or dependency holds without inventing a holder."""
    text = task_id
    if "run_id" in hold:
        wait = f"awaiting {hold['pending']} approval" if hold["pending"] else str(hold["status"])
        text += f" by run {str(hold['run_id'])[:8]} ({wait})"
    if hold.get("reason") in ("project_busy", "paths_overlap"):
        text += f" {hold['reason']}"
        if hold.get("other_task_id"):
            text += f" with {hold['other_task_id']}"
        if hold.get("detail"):
            text += f": {hold['detail']}"
    if hold.get("reason") == "depends_on":
        text += " depends_on: " + "; ".join(item["text"] for item in hold["unmet"])
    return text


def run_origin(run_id: str, data_dir: Path | None) -> TaskOrigin | None:
    """Read origin from a run's immutable task snapshot, if available and valid."""
    try:
        return _snapshot_origin(RunDir(run_id, data_dir).task_md.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None


def pair_field(ledger: Ledger, run_id: str) -> dict[str, Any]:
    """``{"pair": {task_id, label, picked, awaiting_pick}}`` for a paired run's status
    row, with the blind label and never the arm. Empty for an unpaired run, whose row
    keeps its shape."""
    pair = run_pair(ledger, run_id)
    return {"pair": pair} if pair is not None else {}


def ended_since(ledger: Ledger, status: str, cutoff: float) -> list[dict[str, Any]]:
    """Runs with ``status`` whose run *ended* at or after ``cutoff`` (whenever they
    started). A landed run's end is the end of its review, not the merge."""
    return [
        r
        for r in ledger.list_runs(status=status)
        if r["ended_at"] and float(r["ended_at"]) >= cutoff
    ]


def failed_since(ledger: Ledger, cutoff: float) -> list[dict[str, Any]]:
    """Runs that *ended* as failed at or after ``cutoff`` (whenever they started)."""
    return ended_since(ledger, "failed", cutoff)


def never_startable_rows(
    queued: list[dict[str, Any]], config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Queued tasks whose projection alone reaches ``stop_at``: ``work`` never starts
    them. Each row is the task plus the ``never_startable`` block and its ``text``."""
    rows = []
    for task in queued:
        try:
            spec = parse_task(str(task["snapshot"]), Path(str(task["source_path"])), config)
        except (TaskLoadError, ValueError, OSError):
            # An invalid task or project.toml is reported where it is scheduled; a
            # status write must never fail on it.
            continue
        block = never_startable(config, spec)
        if block is not None:
            rows.append(
                {
                    "task_id": task["task_id"],
                    "id": task["id"],
                    "title": task["title"],
                    "project": task["project"],
                    "origin": _task_origin(task),
                    **block,
                    "text": never_startable_text(block),
                }
            )
    return rows


def _iso_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=UTC).isoformat()


def build_scout_status(
    ledger: Ledger,
    config: dict[str, Any],
    now: float,
    worker: dict[str, Any],
) -> dict[str, Any]:
    """Build the owner-facing state of requested and recent scouts."""
    # Local import avoids loop.runner → status → scout → preflight → runner.
    from helmraiser.loop.scout import (
        ScoutError,
        configured_focus_projects,
        configured_paused_projects,
        scout_gate,
    )
    from helmraiser.loop.scout_lenses import scout_lens_settings

    ledger_running = [row for row in ledger.list_scouts() if row["outcome"] == "running"]
    running_by_id = {str(row["id"]): row for row in ledger_running}
    scout_children = [
        child
        for child in worker.get("children", [])
        if child.get("kind") == "scout" and child.get("scout_id")
    ]
    running_projects = {
        str(Path(str(row["project"])).expanduser().resolve()) for row in ledger_running
    }
    children = list(worker.get("children") or [])
    running_projects.update(
        str(Path(str(child["project"])).expanduser().resolve())
        for child in children
        if child.get("project")
    )
    maximum = worker.get("max_parallel_runs")
    slots_busy = (
        worker.get("state") == "running" and isinstance(maximum, int) and len(children) >= maximum
    )

    pending = []
    for request in ledger.list_scout_requests():
        project = str(Path(str(request["project"])).expanduser().resolve())
        reason: str | None = None
        if ledger.is_paused() is not None:
            reason = "queue paused"
        elif project in running_projects:
            reason = "project busy"
        else:
            try:
                scout_gate(
                    Path(project),
                    ledger,
                    config,
                    now,
                    refresh=False,
                    ignore_enabled_and_cooldown=True,
                    lens=request["lens"],
                )
            except ScoutError as exc:
                reason = exc.wait_reason
            if reason is None and slots_busy:
                reason = "slots busy"
        pending.append(
            {
                "project": project,
                "lens": request["lens"],
                "requested_at": _iso_timestamp(float(request["requested_at"])),
                "wait_reason": reason,
            }
        )

    running = [
        {
            "project": str(Path(str(row["project"])).expanduser().resolve()),
            "scout_id": str(row["id"]),
            "lens": row["lens"],
            "started_at": _iso_timestamp(float(row["started_at"])),
        }
        for row in ledger_running
    ]
    for child in scout_children:
        scout_id = str(child["scout_id"])
        if scout_id in running_by_id:
            continue
        running.append(
            {
                "project": str(Path(str(child["project"])).expanduser().resolve()),
                "scout_id": scout_id,
                "lens": child["lens"],
                "started_at": _iso_timestamp(float(child["started_at"])),
            }
        )
    running.sort(key=lambda row: (row["started_at"], row["scout_id"]))

    recent = []
    for row in ledger.list_scouts():
        ended_at = row.get("ended_at")
        if ended_at is None or float(ended_at) < now - 24 * 3600:
            continue
        report = Path(str(row["report_path"])).expanduser().resolve()
        recent.append(
            {
                "project": str(Path(str(row["project"])).expanduser().resolve()),
                "scout_id": str(row["id"]),
                "lens": row["lens"],
                "ended_at": _iso_timestamp(float(ended_at)),
                "ok": row["outcome"] == "succeeded",
                "drafts": len(row["drafts_written"]),
                "report": str(report),
                "error": row.get("error"),
            }
        )
    recent.sort(key=lambda row: row["ended_at"], reverse=True)
    return {
        "lenses": scout_lens_settings(config, ledger),
        "focus_projects": [str(path) for path in configured_focus_projects(config)],
        "paused_projects": [str(path) for path in configured_paused_projects(config)],
        "pending": pending,
        "running": running,
        "recent": recent,
    }


def build_status(
    ledger: Ledger,
    now: float | None = None,
    config: dict[str, Any] | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    from helmraiser.cli.worker_commands import (
        worker_payload,  # local import: avoids an import cycle
    )

    now = now if now is not None else time.time()
    config = config if config is not None else load_config()
    resume_holds = ledger.resume_holds(config=config)
    worker = worker_payload(data_dir)
    running: list[dict[str, Any]] = []
    resuming: list[dict[str, Any]] = []
    reworked: list[dict[str, Any]] = []
    for run in ledger.list_runs():
        requests = ledger.reworks(str(run["run_id"]))
        history = {"rework_rounds": len(requests), "reworks": requests} if requests else {}
        if requests and (
            run["status"] not in TERMINAL_RUN_STATUSES
            or float(run["ended_at"] or run["started_at"]) >= now - 24 * 3600
        ):
            reworked.append(
                {
                    "run_id": run["run_id"],
                    "task": run["task_id"],
                    "status": run["status"],
                    **history,
                    **pair_field(ledger, str(run["run_id"])),
                }
            )
        if run["status"] == RESUMING_RUN_STATUS:
            task = ledger.get_task(run["task_id"])
            resuming.append(
                {
                    "run_id": run["run_id"],
                    "task": run["task_id"],
                    "project": run["project"],
                    "id": task["id"] if task else None,
                    "title": task["title"] if task else None,
                    "stage": run["stage"],
                    "held_by": resume_holds.get(run["run_id"]),
                    **history,
                    "origin": run_origin(str(run["run_id"]), data_dir),
                    **pair_field(ledger, str(run["run_id"])),
                }
            )
            continue
        if run["status"] in TERMINAL_RUN_STATUSES or run["status"] in IDLE_RUN_STATUSES:
            continue
        task = ledger.get_task(run["task_id"])
        running.append(
            {
                "run_id": run["run_id"],
                "task": run["task_id"],
                "project": run["project"],
                "id": task["id"] if task else None,
                "title": task["title"] if task else None,
                "stage": run["stage"],
                **history,
                "iteration": len(ledger.iterations(run["run_id"])),
                "cost_usd": round(float(run["cost_usd"] or 0.0), 4),
                "minutes": int((now - float(run["started_at"])) // 60),
                "origin": run_origin(str(run["run_id"]), data_dir),
                **pair_field(ledger, str(run["run_id"])),
            }
        )
    failed_24h = len(failed_since(ledger, now - 24 * 3600))
    draft_counts: dict[str, int] = {}
    for task in ledger.list_tasks(status="draft"):
        project = str(task["project"])
        draft_counts[project] = draft_counts.get(project, 0) + 1
    queued = ledger.list_tasks(status="queued")
    held_by = ledger.held_tasks(config=config)
    queue_next = [
        {
            "task_id": task["task_id"],
            "id": task["id"],
            "title": task["title"],
            "priority": PRIORITIES[int(task["priority"])],
            "project": task["project"],
            "held_by": held_by.get(task["task_id"]),
            "origin": _task_origin(task),
        }
        for task in queued[:5]
    ]
    held = [
        {
            "task_id": task["task_id"],
            "id": task["id"],
            "title": task["title"],
            "project": task["project"],
            "origin": _task_origin(task),
            **held_by[task["task_id"]],
        }
        for task in queued
        if task["task_id"] in held_by
    ]
    caps_enforced = caps_enforcement(ledger, config)[0]
    usage_payload = usage(ledger, config, now, live=live_for(ledger, config, now))
    return {
        "updated_at": datetime.fromtimestamp(now, tz=UTC).isoformat(),
        "queued_count": len(queued),
        "drafts": dict(sorted(draft_counts.items())),
        "drafts_counted_kinds": ["scout", "manual"],
        "queue_next": queue_next,
        "held": held,
        "never_startable": never_startable_rows(queued, config) if caps_enforced else [],
        "running": running,
        "resuming": resuming,
        "reworks": reworked,
        "awaiting_count": len(ledger.list_runs(status="awaiting_approval")),
        "awaiting_pick_count": awaiting_pick_count(ledger),
        "failed_24h_count": failed_24h,
        "spend": spend_by_provider(ledger, now),
        "usage": usage_payload,
        "routing": {"headroom": calculate_headroom(usage_payload, config, now)},
        "scouts": build_scout_status(ledger, config, now, worker),
        "caps_enforced": caps_enforced,
        "paused": ledger.is_paused(),
        "held_providers": ledger.provider_holds(now),
        "worker": worker,
    }


def write_status_atomic(payload: dict[str, Any], path: Path) -> Path:
    """Write via a temp file in the same directory and ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".status-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path


def write_status(ledger: Ledger, data_dir: Path | None = None) -> Path:
    return write_status_atomic(build_status(ledger, data_dir=data_dir), status_path(data_dir))
