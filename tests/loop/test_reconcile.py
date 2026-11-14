"""Recovery of ledger runs left active after their worker exits."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from helmraiser.ledger.db import ProjectScope
from helmraiser.ledger.rundir import RunDir
from helmraiser.loop.reconcile import find_phantom_runs, reconcile
from helmraiser.runner.process_control import ProcessIdentity
from helmraiser.storage.state import ProcessLease, StateManager


def _dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def _record_run(ledger, task, run_id: str, status: str = "running", worktree=None) -> None:
    ledger.create_run(
        {
            "run_id": run_id,
            "task_id": task.task_id,
            "project": str(task.project),
            "worktree_path": str(worktree) if worktree else None,
            "status": status,
            "policy": "review",
            "isolation": "none",
            "model_implement": "a/impl:medium",
            "model_review": "a/review:high",
            "pi_version": "0.87.1",
        }
    )


class FakeController:
    def __init__(self, running: bool = False):
        self.running = running
        self.terminated: list[ProcessIdentity] = []

    def identify(self, pid: int) -> ProcessIdentity:
        raise AssertionError("persisted identities must be used")

    def is_running(self, identity: ProcessIdentity) -> bool:
        return self.running

    def terminate(self, identity: ProcessIdentity) -> bool:
        self.terminated.append(identity)
        self.running = False
        return True

    def kill(self, identity: ProcessIdentity) -> bool:
        raise AssertionError("terminate should stop the fake process")


def test_find_phantom_run_with_dead_worker_includes_lease(ledger, make_task):
    task = make_task()
    run_id = "11111111-1111-4111-8111-111111111111"
    _record_run(ledger, task, run_id)
    state = StateManager(task.project / ".helmraiser" / "state.db")
    state.create_run(run_id, task.id)
    state.acquire_lock(run_id, task.id, _dead_pid())
    state.register_process(ProcessLease(run_id, "attempt", 42, 42, "token"))

    phantoms = find_phantom_runs(ledger)

    assert len(phantoms) == 1
    phantom = phantoms[0]
    assert phantom.run_id == run_id
    assert phantom.lease == ProcessLease(run_id, "attempt", 42, 42, "token")


def test_find_phantom_run_when_lock_row_is_missing(ledger, make_task):
    task = make_task()
    _record_run(ledger, task, "11111111-1111-4111-8111-111111111111")

    (phantom,) = find_phantom_runs(ledger)

    assert phantom.worker_pid is None


def test_phantoms_outside_the_scope_are_left_alone(ledger, make_task):
    """A worker reconciles only the projects it serves: another worker's run in its
    claim window (running, lock not taken yet) must not be closed."""
    task = make_task()
    _record_run(ledger, task, "11111111-1111-4111-8111-111111111111")

    assert find_phantom_runs(ledger, ProjectScope.of(None, [task.project])) == []
    assert find_phantom_runs(ledger, ProjectScope.of(["/elsewhere"])) == []
    (phantom,) = find_phantom_runs(ledger, ProjectScope.of([task.project]))
    assert phantom.run_id == "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize("status", ["awaiting_approval", "resuming"])
def test_idle_run_is_not_a_phantom(ledger, make_task, status):
    task = make_task()
    _record_run(ledger, task, "11111111-1111-4111-8111-111111111111", status=status)

    assert find_phantom_runs(ledger) == []


def test_live_worker_is_not_a_phantom(ledger, make_task):
    task = make_task()
    run_id = "11111111-1111-4111-8111-111111111111"
    _record_run(ledger, task, run_id)
    state = StateManager(task.project / ".helmraiser" / "state.db")
    state.create_run(run_id, task.id)
    state.acquire_lock(run_id, task.id, os.getpid())

    assert find_phantom_runs(ledger) == []


@pytest.mark.parametrize("status", ["running", "awaiting_approval", "resuming", "landed", "failed"])
def test_identity_proven_dead_worker_override(ledger, make_task, monkeypatch, status):
    task = make_task()
    run_id = "11111111-1111-4111-8111-111111111111"
    _record_run(ledger, task, run_id, status=status)
    state = StateManager(task.project / ".helmraiser" / "state.db")
    state.acquire_lock(run_id, task.id, 123)
    monkeypatch.setattr("helmraiser.loop.reconcile.is_process_alive", lambda pid: True)
    assert find_phantom_runs(ledger) == []
    assert find_phantom_runs(ledger, dead_worker_pids={456}) == []
    assert find_phantom_runs(ledger, ProjectScope.of(["/elsewhere"]), dead_worker_pids={123}) == []
    result = find_phantom_runs(ledger, dead_worker_pids={123})
    assert [p.run_id for p in result] == ([run_id] if status == "running" else [])
    assert state.get_lock(task.id)["pid"] == 123


def test_reconcile_stops_a_live_leased_child(ledger, make_task):
    task = make_task()
    run_id = "11111111-1111-4111-8111-111111111111"
    _record_run(ledger, task, run_id)
    state = StateManager(task.project / ".helmraiser" / "state.db")
    state.create_run(run_id, task.id)
    state.acquire_lock(run_id, task.id, _dead_pid())
    state.register_process(ProcessLease(run_id, "attempt", 42, 42, "token"))
    controller = FakeController(running=True)

    reconcile(ledger, find_phantom_runs(ledger), controller)

    assert controller.terminated == [ProcessIdentity(42, 42, "token")]
    assert ledger.get_run(run_id)["status"] == "failed"


@pytest.mark.parametrize("restamp_error", [False, True])
def test_reconcile_closes_run_releases_lock_keeps_worktree_and_rewrites_status(
    ledger, make_task, runtime_dir: Path, monkeypatch, restamp_error
):
    task = make_task()
    run_id = "11111111-1111-4111-8111-111111111111"
    worktree = task.project / ".helmraiser" / "worktrees" / "kept"
    worktree.mkdir(parents=True)
    (worktree / "marker").write_text("keep", encoding="utf-8")
    _record_run(ledger, task, run_id, worktree=worktree)
    ledger.set_task_status(task.task_id, "running")
    state = StateManager(task.project / ".helmraiser" / "state.db")
    state.create_run(run_id, task.id)
    dead_pid = _dead_pid()
    state.acquire_lock(run_id, task.id, dead_pid)
    summary = RunDir(run_id, ledger.db_path.parent).create().summary_md
    original = "## Task — awaiting_approval\n\n- worktree: keep this text\n"
    summary.write_text(original)
    if restamp_error:

        def fail(*args, **kwargs):
            raise OSError("summary unavailable")

        monkeypatch.setattr("helmraiser.loop.reconcile.restamp_summary", fail)

    result = reconcile(ledger, find_phantom_runs(ledger), FakeController())

    expected = original if restamp_error else original.replace("awaiting_approval", "failed")
    assert summary.read_text() == expected
    assert result[0]["reason"] == f"orphaned: worker {dead_pid} is gone"
    assert ledger.get_run(run_id)["status"] == "failed"
    assert ledger.get_run(run_id)["failure_reason"] == result[0]["reason"]
    assert ledger.get_task(task.task_id)["status"] == "failed"
    assert state.get_run(run_id)["status"] == "failed"
    assert state.get_lock(task.id) is None
    assert (worktree / "marker").read_text(encoding="utf-8") == "keep"
    assert ledger.events(run_id)[-1]["type"] == "reconciled"
    status = json.loads((runtime_dir / "helmraiser" / "status.json").read_text())
    assert status["running"] == []


class KillingController(FakeController):
    """A child that ignores SIGTERM and needs SIGKILL."""

    def __init__(self) -> None:
        super().__init__(running=True)
        self.killed: list[ProcessIdentity] = []

    def terminate(self, identity: ProcessIdentity) -> bool:
        self.terminated.append(identity)
        return True

    def kill(self, identity: ProcessIdentity) -> bool:
        self.killed.append(identity)
        self.running = False
        return True


def test_reconcile_kills_a_live_child_when_project_state_is_already_terminal(ledger, make_task):
    task = make_task()
    run_id = "11111111-1111-4111-8111-111111111111"
    _record_run(ledger, task, run_id)
    state = StateManager(task.project / ".helmraiser" / "state.db")
    state.create_run(run_id, task.id)
    state.acquire_lock(run_id, task.id, _dead_pid())
    state.register_process(ProcessLease(run_id, "attempt", 42, 42, "token"))
    state.update_run_status(run_id, "aborted")  # an earlier abort closed project state
    controller = KillingController()

    reconcile(ledger, find_phantom_runs(ledger), controller)

    assert controller.killed == [ProcessIdentity(42, 42, "token")]
    assert ledger.get_run(run_id)["status"] == "failed"


def test_reconcile_does_not_overwrite_a_run_closed_after_detection(ledger, make_task):
    task = make_task()
    run_id = "11111111-1111-4111-8111-111111111111"
    _record_run(ledger, task, run_id)
    state = StateManager(task.project / ".helmraiser" / "state.db")
    state.create_run(run_id, task.id)
    state.acquire_lock(run_id, task.id, _dead_pid())
    phantoms = find_phantom_runs(ledger)
    ledger.finish_run(run_id, "landed")  # the worker got there first
    summary = RunDir(run_id, ledger.db_path.parent).create().summary_md
    original = "## Task — landed\n\n- worktree: keep this text\n"
    summary.write_text(original)

    assert reconcile(ledger, phantoms, FakeController()) == []
    assert ledger.get_run(run_id)["status"] == "landed"
    assert summary.read_bytes() == original.encode()


def test_lock_held_by_another_run_does_not_hide_a_phantom(ledger, make_task):
    task = make_task()
    orphan = "11111111-1111-4111-8111-111111111111"
    other = "22222222-2222-4222-8222-222222222222"
    _record_run(ledger, task, orphan)
    state = StateManager(task.project / ".helmraiser" / "state.db")
    state.create_run(other, task.id)
    state.acquire_lock(other, task.id, os.getpid())  # a live lock, but not this run's

    phantoms = find_phantom_runs(ledger)

    assert [p.run_id for p in phantoms] == [orphan]
    reconcile(ledger, phantoms, FakeController())
    assert ledger.get_run(orphan)["status"] == "failed"
    assert state.get_locked_run_id(task.id) == other
