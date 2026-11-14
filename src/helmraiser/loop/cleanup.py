"""Conservative cleanup of superseded and old failed run worktrees."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from helmraiser.ledger.db import Ledger
from helmraiser.task.project import worktree_manager


def clean_worktrees(
    ledger: Ledger,
    *,
    config: dict[str, Any],
    project: Path | None = None,
    dry_run: bool = False,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Remove only clean, registered failed/aborted trees; always keep branches."""
    now = time.time() if now is None else now
    days = config.get("cleanup", {}).get("failed_after_days", 7)
    runs = ledger.list_runs()
    landed: dict[str, float] = {}
    for row in runs:
        if row["status"] == "landed":
            task_id = str(row["task_id"])
            landed[task_id] = max(landed.get(task_id, float("-inf")), row["started_at"])

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in runs:
        if row["status"] not in ("failed", "aborted") or not row["worktree_path"]:
            continue
        path = Path(row["project"])
        if project is not None and path.expanduser().resolve() != project.expanduser().resolve():
            continue
        if not path.is_dir():
            continue
        if landed.get(row["task_id"], float("-inf")) > row["started_at"]:
            reason = "superseded"
        elif (
            days is not None
            and row["ended_at"] is not None
            and row["ended_at"] < now - days * 86400
        ):
            reason = "age"
        else:
            continue
        entry = {
            key: row[key]
            for key in ("run_id", "task_id", "project", "worktree_path", "branch_name")
        }
        entry.update(reason=reason, action="kept", detail=None)
        groups.setdefault(str(path), []).append(entry)

    results: list[dict[str, Any]] = []
    for project_name, entries in groups.items():
        try:
            manager = worktree_manager(Path(project_name))
        except Exception as exc:
            for entry in entries:
                entry["detail"] = str(exc)
            results.extend(entries)
            continue
        for entry in entries:
            run_id = entry["run_id"]
            topic_id = entry["task_id"].rsplit(":", 1)[-1]
            try:
                info = manager.get_info(topic_id, run_id)
                if info is None:
                    continue
                status = manager._git(
                    "status", "--porcelain", "--untracked-files=all", cwd=Path(info["path"])
                ).stdout
            except Exception as exc:
                entry["detail"] = str(exc)
                results.append(entry)
                continue
            results.append(entry)
            if status.strip():
                count = len([line for line in status.splitlines() if line.strip()])
                entry["detail"] = f"dirty: {count} paths"
                continue
            if dry_run:
                entry["action"] = "would_remove"
                continue
            try:
                with manager._finalization_lock():
                    current = ledger.get_run(run_id)
                    if current is None or current["status"] not in ("failed", "aborted"):
                        status_name = current["status"] if current else "missing"
                        entry["detail"] = f"status changed: {status_name}"
                        continue
                    manager.discard(topic_id, run_id)
            except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
                entry["detail"] = str(exc)
                continue
            entry["action"] = "removed"
            # Do not disguise an event-write failure as a failed removal.
            ledger.append_event(
                run_id,
                "helmraiser",
                "worktree_cleaned",
                {key: entry[key] for key in ("reason", "worktree_path", "branch_name")},
            )
    return sorted(results, key=lambda entry: (entry["project"], entry["run_id"]))


def discard_landed_worktree(ledger: Ledger, row: dict[str, Any]) -> dict[str, Any]:
    """Remove the clean worktree of a run just recorded as landed outside Helmraiser.

    The sweep above only looks at failed and aborted runs, so nothing else would ever
    remove it. As there, a dirty tree is kept, removal never forces, and the branch stays.
    Returns ``{action: removed|kept|absent, path, detail}``; never raises.
    """
    run_id = str(row["run_id"])
    result: dict[str, Any] = {"action": "absent", "path": row["worktree_path"], "detail": None}
    topic_id = str(row["task_id"]).rsplit(":", 1)[-1]
    try:
        manager = worktree_manager(Path(str(row["project"])))
        info = manager.get_info(topic_id, run_id)
        if info is None:
            return result
        result["action"] = "kept"
        dirty = manager._git(
            "status", "--porcelain", "--untracked-files=all", cwd=Path(info["path"])
        ).stdout
        if dirty.strip():
            count = len([line for line in dirty.splitlines() if line.strip()])
            result["detail"] = f"dirty: {count} paths"
            return result
        with manager._finalization_lock():
            manager.discard(topic_id, run_id)
    except (RuntimeError, subprocess.SubprocessError, OSError, ValueError) as exc:
        result["detail"] = str(exc)
        return result
    result["action"] = "removed"
    ledger.append_event(
        run_id,
        "helmraiser",
        "worktree_cleaned",
        {
            "reason": "landed_outside",
            "worktree_path": str(row["worktree_path"]),
            "branch_name": row["branch_name"],
        },
    )
    return result
