"""Refresh local bases for PR tasks held solely by delivered-head ancestry.

Dependency evaluation stays read-only. These host-side attempts have no run and
report through CLI output (the background worker's log), not ledger run events.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from helmraiser.ledger.db import TERMINAL_RUN_STATUSES, Ledger, ProjectScope
from helmraiser.task.loader import parse_task
from helmraiser.task.project import worktree_manager


def _failure(base: str | None, exc: Exception) -> dict[str, Any]:
    return {
        "status": "failed",
        "reason": "base_sync_error",
        "base_branch": base,
        "remote": None,
        "upstream": None,
        "from_commit": None,
        "to_commit": None,
        "upstream_commit": None,
        "detail": str(exc),
    }


class HeldBaseSync:
    def __init__(
        self, interval_s: float = 300.0, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.interval_s = interval_s
        self.clock = clock
        self.last_attempt: dict[tuple[str, str | None], float] = {}

    def check(
        self, ledger: Ledger, scope: ProjectScope, *, config: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if ledger.is_paused():
            return []
        holds = ledger.held_tasks(scope=scope, config=config)
        outcomes: list[dict[str, Any]] = []
        seen: set[tuple[str, str | None]] = set()
        for row in ledger.list_tasks(status="queued"):
            project = str(row["project"])
            if not scope.matches(project):
                continue
            hold = holds.get(str(row["task_id"]), {})
            unmet = hold.get("unmet", [])
            if (
                hold.get("reason") != "depends_on"
                or "run_id" in hold
                or not unmet
                or any(item.get("reason") != "not_in_base" for item in unmet)
            ):
                continue
            base = None
            failure = None
            try:
                spec = parse_task(str(row["snapshot"]), Path(row["source_path"]), {})
                if spec.delivery != "pr":
                    continue
                base = spec.base
                if base is None:
                    result = subprocess.run(
                        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
                        cwd=project,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    if result.returncode or not result.stdout.strip():
                        raise ValueError(result.stderr.strip() or "no local base branch")
                    base = result.stdout.strip()
            except Exception as exc:
                failure = _failure(base, exc)
            key = (project, base)
            if key in seen:
                continue
            seen.add(key)
            now = self.clock()
            previous = self.last_attempt.get(key)
            if previous is not None and now - previous < self.interval_s:
                continue
            # Ledger failures intentionally propagate; only candidate/git failures
            # are nonfatal. Recheck safety before consuming the cooldown.
            if ledger.is_paused():
                break
            if any(
                run["status"] not in TERMINAL_RUN_STATUSES
                for run in ledger.list_runs(project=project)
            ):
                continue
            self.last_attempt[key] = now
            if failure is None:
                try:
                    failure = worktree_manager(Path(project)).sync_base(base=base)
                except Exception as exc:
                    failure = _failure(base, exc)
            outcomes.append({"project": project, **failure})
        return outcomes


def base_sync_text(payload: dict[str, Any]) -> str:
    text = (
        f"base sync {payload['project']} ({payload.get('base_branch')}): "
        f"{payload['status']} ({payload['reason']})"
    )
    if payload.get("detail"):
        text += f": {payload['detail']}"
    return text
