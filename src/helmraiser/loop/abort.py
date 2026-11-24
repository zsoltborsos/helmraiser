"""Exact-run abort coordination and CLI wiring through the project's lease."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from helmraiser.ledger.db import TERMINAL_RUN_STATUSES, Ledger, default_db_path
from helmraiser.ledger.rundir import RunDir, restamp_summary
from helmraiser.loop.status import write_status
from helmraiser.pipeline.run_identity import resolve_run_id
from helmraiser.runner.process_control import (
    PosixProcessController,
    ProcessController,
    ProcessIdentity,
)
from helmraiser.storage.state import StateManager
from helmraiser.utils.process import is_process_alive


@dataclass(frozen=True)
class AbortResult:
    found: bool
    status: str
    signaled: bool = False


class AbortCoordinator:
    """Converge one run's local state and its worker process to ``aborted``."""

    def __init__(
        self,
        state: StateManager,
        process_controller: ProcessController,
        *,
        sleep: Callable[[float], None] = time.sleep,
        grace_seconds: float = 5.0,
        poll_interval: float = 0.1,
    ):
        self.state = state
        self.process_controller = process_controller
        self.sleep = sleep
        self.grace_seconds = grace_seconds
        self.poll_interval = poll_interval

    def abort(self, run_id: str) -> AbortResult:
        request = self.state.request_abort(run_id)
        if request is None:
            return AbortResult(False, "unknown")
        if request.status in {"succeeded", "failed", "aborted"}:
            return AbortResult(True, request.status)

        signaled = False
        if request.lease is not None:
            identity = ProcessIdentity(
                request.lease.leader_pid,
                request.lease.pgid,
                request.lease.process_start_token,
            )
            signaled = self.process_controller.terminate(identity)
            if signaled:
                deadline = time.monotonic() + self.grace_seconds
                while self.process_controller.is_running(identity) and time.monotonic() < deadline:
                    self.sleep(self.poll_interval)
                if self.process_controller.is_running(identity):
                    self.process_controller.kill(identity)

        self.state.update_run_status(run_id, "aborted")
        return AbortResult(True, "aborted", signaled)


def abort_run(
    project_dir: Path,
    run_id: str,
    process_controller: ProcessController | None = None,
) -> bool:
    """Abort ``run_id`` recorded in ``project_dir``'s state; False if unknown.

    Raises ``ValueError`` when ``run_id`` is not a canonical UUID4.
    """
    canonical_run_id = resolve_run_id(run_id)
    state_dir = project_dir.resolve() / ".helmraiser"
    state_dir.mkdir(parents=True, exist_ok=True)
    state = StateManager(state_dir / "state.db")
    run = state.get_run(canonical_run_id)
    coordinator = AbortCoordinator(state, process_controller or PosixProcessController())
    result = coordinator.abort(canonical_run_id)
    if not result.found:
        return False

    ledger_path = default_db_path()
    if ledger_path.is_file() and run is not None:
        try:
            ledger = Ledger(ledger_path)
            ledger_run = ledger.get_run(canonical_run_id)
            lock = state.get_lock(str(run["topic_id"]))
            worker_pid = int(lock["pid"]) if lock is not None else None
            if (
                ledger_run is not None
                and ledger_run["status"] not in TERMINAL_RUN_STATUSES
                and not is_process_alive(worker_pid)
            ):
                ledger.finish_run(canonical_run_id, "aborted", "aborted: no worker alive")
                ledger.set_task_status(str(ledger_run["task_id"]), "aborted")
                try:
                    restamp_summary(RunDir(canonical_run_id, ledger_path.parent), "aborted")
                except OSError:
                    pass
                write_status(ledger, ledger_path.parent)
        except (OSError, sqlite3.Error):
            pass
    return True
