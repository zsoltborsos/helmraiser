"""``helmraiser abort`` wiring: ``loop.abort.abort_run`` over the project state."""

import json
from uuid import uuid4

import pytest

from helmraiser.ledger.db import Ledger
from helmraiser.ledger.rundir import RunDir
from helmraiser.loop.abort import abort_run
from helmraiser.runner.process_control import ProcessIdentity
from helmraiser.storage.state import ProcessLease, StateManager
from tests.ledger.test_db import make_spec, run_row
from tests.runner.test_abort import FakeProcessController


def _state(project_dir):
    return StateManager(project_dir / ".helmraiser" / "state.db")


def test_unknown_run_returns_false(tmp_path):
    assert abort_run(tmp_path, str(uuid4()), FakeProcessController()) is False


def test_non_canonical_run_id_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="UUID"):
        abort_run(tmp_path, "not-a-uuid", FakeProcessController())


def test_abort_signals_the_leased_process_and_marks_the_run_aborted(tmp_path):
    run_id = str(uuid4())
    (tmp_path / ".helmraiser").mkdir()
    state = _state(tmp_path)
    state.create_run(run_id, "task")
    state.register_process(ProcessLease(run_id, "attempt", 42, 42, "token"))
    controller = FakeProcessController(running=False)

    assert abort_run(tmp_path, run_id, controller) is True

    assert controller.terminated == [ProcessIdentity(42, 42, "token")]
    assert _state(tmp_path).get_run(run_id)["status"] == "aborted"


@pytest.fixture
def _ledger_abort_case(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / ".helmraiser").mkdir(parents=True)
    data = tmp_path / "data"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("HELMRAISER_DATA_DIR", str(data))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    ledger = Ledger(data / "helmraiser.db")
    task = make_spec(project)
    ledger.upsert_task(task, "snapshot")
    run_id = str(uuid4())
    ledger.create_run(run_row(run_id, task.task_id, str(project), "awaiting_approval"))
    ledger.set_task_status(task.task_id, "awaiting_approval")
    state = _state(project)
    state.create_run(run_id, task.id)
    summary = RunDir(run_id, data).create().summary_md
    summary.write_text("## Task — awaiting_approval\n\n- worktree: keep\n")
    return project, ledger, task, run_id, state, summary, runtime


def test_abort_restamps_summary(_ledger_abort_case):
    project, ledger, task, run_id, state, summary, _ = _ledger_abort_case
    assert abort_run(project, run_id, FakeProcessController()) is True
    assert ledger.get_run(run_id)["status"] == "aborted"
    assert ledger.get_task(task.task_id)["status"] == "aborted"
    assert state.get_run(run_id)["status"] == "aborted"
    assert summary.read_text() == "## Task — aborted\n\n- worktree: keep\n"


def test_abort_ignores_summary_oserror(_ledger_abort_case, monkeypatch):
    project, ledger, task, run_id, state, summary, runtime = _ledger_abort_case
    before = summary.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("summary unavailable")

    monkeypatch.setattr("helmraiser.loop.abort.restamp_summary", fail)
    assert abort_run(project, run_id, FakeProcessController()) is True
    assert ledger.get_run(run_id)["status"] == "aborted"
    assert ledger.get_task(task.task_id)["status"] == "aborted"
    assert state.get_run(run_id)["status"] == "aborted"
    assert summary.read_bytes() == before
    status = json.loads((runtime / "helmraiser" / "status.json").read_text())
    assert status["awaiting_count"] == 0


@pytest.mark.parametrize("landed", [False, True])
def test_abort_does_not_restamp_without_ledger_transition(_ledger_abort_case, monkeypatch, landed):
    project, ledger, task, run_id, _, summary, _ = _ledger_abort_case
    if landed:
        ledger.finish_run(run_id, "landed")
        ledger.set_task_status(task.task_id, "landed")
        summary.write_text("## Task — landed\n\n- worktree: keep\n")
    monkeypatch.setattr("helmraiser.loop.abort.is_process_alive", lambda pid: not landed)
    before_run = ledger.get_run(run_id)
    before = summary.read_bytes()
    assert abort_run(project, run_id, FakeProcessController()) is True
    assert ledger.get_run(run_id) == before_run
    assert summary.read_bytes() == before
