"""Fake-only exact-run abort tests; this module emits no OS signals."""

from uuid import uuid4

from helmraiser.ledger.db import Ledger
from helmraiser.loop.abort import AbortCoordinator, AbortResult, abort_run
from helmraiser.runner.process_control import ProcessIdentity
from helmraiser.storage.state import ProcessLease, StateManager
from tests.ledger.test_db import make_spec, run_row


class FakeProcessController:
    def __init__(self, running: bool = True, terminate_result: bool = True):
        self.running = running
        self.terminate_result = terminate_result
        self.terminated: list[ProcessIdentity] = []
        self.killed: list[ProcessIdentity] = []

    def identify(self, pid: int) -> ProcessIdentity:
        raise AssertionError("abort uses persisted identity, not live discovery")

    def is_running(self, identity: ProcessIdentity) -> bool:
        return self.running

    def terminate(self, identity: ProcessIdentity) -> bool:
        self.terminated.append(identity)
        return self.terminate_result

    def kill(self, identity: ProcessIdentity) -> bool:
        self.killed.append(identity)
        self.running = False
        return True


def _coordinator(tmp_path, controller=None):
    state = StateManager(tmp_path / "state.db")
    controller = controller or FakeProcessController()
    coordinator = AbortCoordinator(
        state,
        controller,
        sleep=lambda _: None,
        grace_seconds=0,
    )
    return coordinator, state, controller


def test_abort_types_live_in_loop_abort():
    assert AbortCoordinator.__module__ == "helmraiser.loop.abort"
    assert AbortResult.__module__ == "helmraiser.loop.abort"


def test_unknown_run_is_rejected(tmp_path):
    coordinator, _, controller = _coordinator(tmp_path)
    assert coordinator.abort("missing").found is False
    assert controller.terminated == []


def test_abort_without_active_child_reaches_local_terminal_state(tmp_path):
    coordinator, state, _ = _coordinator(tmp_path)
    state.create_run("run", "topic")

    result = coordinator.abort("run")

    assert result.status == "aborted"
    assert not result.signaled
    assert state.get_run("run")["status"] == "aborted"


def test_abort_targets_only_exact_runs_validated_lease(tmp_path):
    coordinator, state, controller = _coordinator(tmp_path)
    state.create_run("one", "topic")
    state.create_run("two", "topic")
    state.register_process(ProcessLease("two", "attempt", 42, 42, "token"))

    result = coordinator.abort("two")

    identity = ProcessIdentity(42, 42, "token")
    assert result.signaled
    assert controller.terminated == [identity]
    assert controller.killed == [identity]
    assert state.get_run("one")["status"] == "starting"
    assert state.get_run("two")["status"] == "aborted"


def test_stale_lease_refusal_never_escalates_to_kill(tmp_path):
    controller = FakeProcessController(terminate_result=False)
    coordinator, state, _ = _coordinator(tmp_path, controller)
    state.create_run("run", "topic")
    state.register_process(ProcessLease("run", "attempt", 42, 42, "stale"))

    result = coordinator.abort("run")

    assert not result.signaled
    assert controller.killed == []
    assert state.get_run("run")["status"] == "aborted"


def test_abort_without_live_worker_closes_ledger_row(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    data_dir = tmp_path / "data"
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    monkeypatch.setenv("HELMRAISER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    ledger = Ledger(data_dir / "helmraiser.db")
    task_id = ledger.upsert_task(make_spec(project), "")
    ledger.set_task_status(task_id, "running")
    run_id = str(uuid4())
    ledger.create_run(run_row(run_id, task_id, str(project)))
    state = StateManager(project / ".helmraiser" / "state.db")
    state.create_run(run_id, task_id)

    assert abort_run(project, run_id, FakeProcessController()) is True

    assert ledger.get_run(run_id)["status"] == "aborted"
    assert ledger.get_run(run_id)["failure_reason"] == "aborted: no worker alive"
    assert ledger.get_task(task_id)["status"] == "aborted"


def test_terminal_abort_is_idempotent_and_does_not_signal_again(tmp_path):
    coordinator, state, controller = _coordinator(tmp_path)
    state.create_run("run", "topic")
    state.update_run_status("run", "aborted")

    result = coordinator.abort("run")

    assert result.status == "aborted"
    assert controller.terminated == []
