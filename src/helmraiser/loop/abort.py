"""Exact-run abort coordination and CLI wiring through the project's lease."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from helmraiser.ledger.db import (
    LIVE_RUN_STATUSES,
    RESUMING_RUN_STATUS,
    TERMINAL_RUN_STATUSES,
    Ledger,
    default_db_path,
)
from helmraiser.ledger.rundir import RunDir, restamp_summary
from helmraiser.loop.pairs import auto_pick, task_pair
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
    *,
    settle_pair: bool = True,
) -> bool:
    """Abort ``run_id`` recorded in ``project_dir``'s state; False if unknown.

    ``settle_pair``: when this call closes the ledger run, let its pair's other arm win
    by failure (``auto_pick``). ``abort_runs`` turns it off while it aborts both arms.

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
                ledger.set_task_status(
                    str(ledger_run["task_id"]), "aborted", run_id=canonical_run_id
                )
                try:
                    restamp_summary(RunDir(canonical_run_id, ledger_path.parent), "aborted")
                except OSError:
                    pass
                if settle_pair:
                    auto_pick(ledger, canonical_run_id)
                write_status(ledger, ledger_path.parent)
        except (OSError, sqlite3.Error):
            pass
    return True


def _abort_unstarted(ledger: Ledger, run_id: str) -> bool:
    """Close a ``resuming`` run that the project state does not know (a pair's second
    run before its start) as aborted, taking it from the worker first. False when the
    run is not resuming or a worker has just claimed it."""
    row = ledger.get_run(run_id)
    if row is None or row["status"] != RESUMING_RUN_STATUS or not ledger.claim_resumable(run_id):
        return False
    ledger.finish_run(run_id, "aborted", "aborted: before it started")
    ledger.set_task_status(str(row["task_id"]), "aborted", run_id=run_id)
    return True


def abort_runs(
    project_dir: Path,
    ref: str,
    process_controller: ProcessController | None = None,
) -> list[str]:
    """Abort run ``ref`` as ``abort_run`` does. When it is one run of an unpicked pair,
    or ``ref`` is the id of a task whose pair is unpicked, abort both of the pair's
    runs: an abort acts on the task, and must not leave one arm to win by the other's
    abort. Returns the runs aborted; empty when ``ref`` is unknown.

    Raises ``ValueError`` when ``ref`` is neither a canonical UUID4 nor such a task.
    """
    controller = () if process_controller is None else (process_controller,)
    ledger_path = default_db_path()
    ledger: Ledger | None = None
    pair = None
    if ledger_path.is_file():
        try:
            ledger = Ledger(ledger_path)
            pair = ledger.pair_for_run(ref) or task_pair(ledger, ref)
        except (OSError, sqlite3.Error, ValueError):
            pair = None
    if (
        ledger is None
        or pair is None
        or pair["pick"] is not None
        or not (pair["run_x"] and pair["run_y"])
    ):
        return [ref] if abort_run(project_dir, ref, *controller) else []

    def live(run_id: str) -> bool:
        row = ledger.get_run(run_id)
        return row is not None and row["status"] in LIVE_RUN_STATUSES

    # Neither abort settles the pair by itself, and the arm without a process goes
    # first: a live arm's worker records its end later, and by then the other arm is
    # aborted and cannot win by failure.
    runs = sorted((str(pair["run_x"]), str(pair["run_y"])), key=live)
    aborted = [
        run_id
        for run_id in runs
        if abort_run(project_dir, run_id, *controller, settle_pair=False)
        or _abort_unstarted(ledger, run_id)
    ]
    write_status(ledger, ledger_path.parent)
    return aborted
