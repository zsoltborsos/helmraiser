"""Detect and close ledger runs whose owning worker has disappeared, and remove the
service containers (``helmraiser.run`` label) of runs that are over or unknown."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from helmraiser.ledger.db import IDLE_RUN_STATUSES, TERMINAL_RUN_STATUSES, Ledger, ProjectScope
from helmraiser.ledger.rundir import RunDir, restamp_summary
from helmraiser.loop.abort import AbortCoordinator
from helmraiser.loop.services import labelled_containers, remove_containers
from helmraiser.loop.status import write_status
from helmraiser.runner.process_control import ProcessController, ProcessIdentity
from helmraiser.storage.state import ProcessLease, StateManager
from helmraiser.utils.process import is_process_alive


@dataclass(frozen=True)
class PhantomRun:
    run_id: str
    task_id: str
    topic_id: str
    project: Path
    status: str
    worker_pid: int | None
    lease: ProcessLease | None = None
    worktree_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        lease = self.lease
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "topic_id": self.topic_id,
            "project": str(self.project),
            "status": self.status,
            "worker_pid": self.worker_pid,
            "worktree_path": self.worktree_path,
            "lease": (
                {
                    "leader_pid": lease.leader_pid,
                    "pgid": lease.pgid,
                    "process_start_token": lease.process_start_token,
                }
                if lease is not None
                else None
            ),
        }


def _state_path(project: Path) -> Path:
    return project / ".helmraiser" / "state.db"


def find_phantom_runs(
    ledger: Ledger,
    scope: ProjectScope | None = None,
    *,
    dead_worker_pids: set[int] | None = None,
    exclude_run_ids: set[str] | None = None,
) -> list[PhantomRun]:
    """Return active ledger runs (of projects in ``scope``) whose task-lock worker is
    absent or dead."""
    phantoms: list[PhantomRun] = []
    for run in ledger.list_runs():
        if run["run_id"] in (exclude_run_ids or set()):
            continue
        status = str(run["status"])
        if status in TERMINAL_RUN_STATUSES or status in IDLE_RUN_STATUSES:
            continue
        if scope is not None and not scope.matches(str(run["project"])):
            continue

        project = Path(str(run["project"])).resolve()
        task_id = str(run["task_id"])
        task = ledger.get_task(task_id)
        topic_id = str(task["id"]) if task is not None else task_id
        state_path = _state_path(project)
        worker_pid: int | None = None
        lease: ProcessLease | None = None
        if state_path.is_file():
            state = StateManager(state_path)
            lock = state.get_lock(topic_id)
            # A lock held by another run of the same task is not this run's worker.
            if lock is not None and str(lock["run_id"]) == str(run["run_id"]):
                worker_pid = int(lock["pid"])
            lease = state.get_process_lease(str(run["run_id"]))
        if (
            worker_pid is not None
            and worker_pid not in (dead_worker_pids or set())
            and is_process_alive(worker_pid)
        ):
            continue
        phantoms.append(
            PhantomRun(
                run_id=str(run["run_id"]),
                task_id=task_id,
                topic_id=topic_id,
                project=project,
                status=status,
                worker_pid=worker_pid,
                lease=lease,
                worktree_path=(
                    str(run["worktree_path"]) if run.get("worktree_path") is not None else None
                ),
            )
        )
    return phantoms


def reconcile(
    ledger: Ledger,
    phantoms: list[PhantomRun],
    controller: ProcessController,
) -> list[dict[str, Any]]:
    """Stop leased children and close each phantom as a failed run."""
    reconciled: list[dict[str, Any]] = []
    for phantom in phantoms:
        state = StateManager(_state_path(phantom.project))
        lease = phantom.lease
        if lease is not None:
            identity = ProcessIdentity(
                lease.leader_pid,
                lease.pgid,
                lease.process_start_token,
            )
            if controller.is_running(identity):
                # The coordinator only signals while project state is still active;
                # a terminal state with a live child is stopped directly.
                AbortCoordinator(state, controller).abort(phantom.run_id)
                if controller.is_running(identity):
                    controller.kill(identity)

        # The worker may have closed the run between detection and now.
        current = ledger.get_run(phantom.run_id)
        if current is None or str(current["status"]) in TERMINAL_RUN_STATUSES:
            continue
        if str(current["status"]) in IDLE_RUN_STATUSES:
            continue

        worker = str(phantom.worker_pid) if phantom.worker_pid is not None else "unknown"
        reason = f"orphaned: worker {worker} is gone"
        ledger.finish_run(phantom.run_id, "failed", reason)
        ledger.set_task_status(phantom.task_id, "failed")
        if state.get_run(phantom.run_id) is not None:
            state.update_run_status(phantom.run_id, "failed")
        try:
            restamp_summary(RunDir(phantom.run_id, ledger.db_path.parent), "failed")
        except OSError:
            pass
        state.release_lock(phantom.run_id, phantom.topic_id)
        ledger.append_event(
            phantom.run_id,
            "helmraiser",
            "reconciled",
            {"status": "failed", "reason": reason},
        )
        item = phantom.as_dict()
        item.update({"status": "failed", "reason": reason})
        reconciled.append(item)

    if reconciled:
        write_status(ledger, ledger.db_path.parent)
    return reconciled


def orphaned_services(
    ledger: Ledger | None, containers: list[dict[str, str]]
) -> list[dict[str, str]]:
    """The labelled containers whose run is terminal, awaiting approval, resuming or
    not in ``ledger`` (every one when there is no ledger). A running run keeps its
    services."""
    orphans: list[dict[str, str]] = []
    for container in containers:
        run = ledger.get_run(container["run_id"]) if ledger is not None else None
        status = str(run["status"]) if run is not None else "unknown"
        if run is None or status in TERMINAL_RUN_STATUSES or status in IDLE_RUN_STATUSES:
            orphans.append({**container, "run_status": status})
    return orphans


def remove_orphaned_services(ledger: Ledger, docker: str = "docker") -> list[dict[str, Any]]:
    """``docker rm -f -v`` each orphaned service container whose run ``ledger`` knows.
    A container of a run this ledger has never seen belongs to another data directory
    (a test, a lab, another ledger) and is left alone; ``doctor`` still lists it.
    Without docker on PATH, or when it fails, there is nothing to do."""
    if shutil.which(docker) is None:
        return []
    containers = labelled_containers(docker)
    if not containers:
        return []
    removed: list[dict[str, Any]] = []
    for orphan in orphaned_services(ledger, containers):
        if orphan["run_status"] == "unknown":
            continue
        error = remove_containers([orphan["id"]], docker)
        removed.append({**orphan, "removed": error is None, "error": error})
    return removed
