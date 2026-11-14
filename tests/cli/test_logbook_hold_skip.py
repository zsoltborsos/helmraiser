"""Fresh-run admission against an isolated scripted Logbook."""

import json
import os
from dataclasses import replace

import pytest

from helmraiser.cli import commands, supervisor
from helmraiser.ledger.db import Ledger, ProjectScope
from helmraiser.logbook import HoldGate
from helmraiser.loop.status import status_path
from helmraiser.storage.state import StateManager
from tests.cli.test_supervisor import FakeChild, Harness, events, queue_real, supervise
from tests.cli.test_work import REVIEW_PASS, queue_task, work
from tests.conftest import configured_test_config
from tests.ledger.test_db import make_spec, run_row
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

HOLDS = [
    {"id": "hold-one", "title": "Tunnel open", "release": "close tunnel"},
    {"id": "hold-two", "title": "Wait", "until": "tomorrow"},
]


def block(fake_lb):
    fake_lb.write_script({"check": {"exit": 3, "stdout": json.dumps(HOLDS)}})


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    config = configured_test_config()
    monkeypatch.setattr(commands, "load_config", lambda: config)
    monkeypatch.setattr(commands, "get_intake_adapter", lambda config: None)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    return config


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


def test_logbook_task_skip_default_gate_and_no_caps(tmp_project, data_dir, fake_lb):
    block(fake_lb)
    ledger = Ledger(data_dir / "helmraiser.db")
    task = make_spec(tmp_project)
    for no_caps in (True, False):
        payload, text = commands._task_skip(task, ledger, configured_test_config(), no_caps)
        assert payload == {"logbook_hold": HOLDS}
        assert "close tunnel" in text
    assert not ledger.list_runs()


def test_logbook_earlier_skip_avoids_check(tmp_project, data_dir, fake_lb, monkeypatch):
    block(fake_lb)
    ledger = Ledger(data_dir / "helmraiser.db")
    task = replace(make_spec(tmp_project), verify=())
    assert commands._task_skip(task, ledger, {}, False) is not None
    assert fake_lb.calls() == []
    task = make_spec(tmp_project)
    monkeypatch.setattr(commands, "caps_enforcement", lambda *a: (True, "test"))
    monkeypatch.setattr(commands, "usage", lambda *a, **k: {})
    monkeypatch.setattr(commands, "live_for", lambda *a: None)
    monkeypatch.setattr(commands, "stop_block", lambda *a: {"reason": "cap"})
    assert commands._task_skip(task, ledger, configured_test_config(), False)[0] == {
        "reason": "cap"
    }
    assert fake_lb.calls() == []


@pytest.mark.parametrize("options", [[], ["--no-caps"], ["--dry-run"], ["--supervised"]])
def test_logbook_work_blocked(tmp_project, data_dir, fake_lb, fake_pi, options):
    task_id = queue_task(tmp_project, data_dir)
    block(fake_lb)
    result = work("--once", "--json", *options, data_dir=data_dir)
    assert result.exit_code == 0, result.output
    event = json.loads(result.output.splitlines()[0])
    assert event == {"skipped": task_id, "logbook_hold": HOLDS}
    ledger = Ledger(data_dir / "helmraiser.db")
    assert ledger.get_task(task_id)["status"] == "queued"
    assert ledger.list_runs() == []
    assert ledger._get_conn().execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 0
    assert fake_pi.calls() == []
    assert not (tmp_project / ".helmraiser/worktrees").exists()
    assert not (data_dir / "runs").exists()
    assert (
        StateManager(tmp_project / ".helmraiser/state.db").get_locked_run_id("fix-widget") is None
    )


def test_logbook_work_human_and_cached_skips(tmp_project, data_dir, fake_lb, fake_pi):
    ids = [queue_task(tmp_project, data_dir, task_id=f"task-{i}") for i in range(3)]
    block(fake_lb)
    result = work(data_dir=data_dir)
    assert result.exit_code == 0, result.output
    for task_id in ids:
        assert f"skipped {task_id}: logbook hold: [hold-one] Tunnel open" in result.output
    assert "Batch:" in result.output
    assert len(fake_lb.calls()) == 1


@pytest.fixture
def adapter(monkeypatch):
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "ok\n")], commit=True),
            "review": PiScript(final_text=REVIEW_PASS),
            "plan": PiScript(final_text="Implement the widget."),
        }
    )
    monkeypatch.setattr(commands, "make_adapter_factory", lambda config: lambda *args: adapter)
    return adapter


def test_logbook_other_project_runs_and_release_without_intake(
    tmp_project, tmp_path, git_init, data_dir, fake_lb, fake_pi, adapter
):
    a = queue_task(tmp_project, data_dir)
    other = tmp_path / "other"
    other.mkdir()
    git_init(other)
    b = queue_task(other, data_dir)
    fake_lb.write_script({f"check@{tmp_project}": {"exit": 3, "stdout": json.dumps(HOLDS)}})
    result = work("--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    ledger = Ledger(data_dir / "helmraiser.db")
    assert ledger.get_task(a)["status"] == "queued"
    assert ledger.get_task(b)["status"] == "awaiting_approval"
    assert [r["task_id"] for r in ledger.list_runs()] == [b]
    assert len(adapter.calls_for("implement")) == 1
    fake_lb.write_script({})
    result = work("--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert ledger.get_task(a)["status"] == "awaiting_approval"
    assert len(adapter.calls_for("implement")) == 2


@pytest.mark.parametrize("failure", ["spawn_error", "exit1", "exit6", "timeout", "garbage"])
@pytest.mark.parametrize("as_json", [False, True])
def test_logbook_work_unavailable_schedules(
    tmp_project, data_dir, fake_lb, fake_pi, monkeypatch, failure, as_json
):
    from helmraiser import logbook

    queue_task(tmp_project, data_dir)
    if failure == "spawn_error":
        monkeypatch.setattr(logbook, "find_lb", lambda: "/nonexistent/lb")
    elif failure == "timeout":
        monkeypatch.setattr(logbook, "CHECK_TIMEOUT_S", 0.2)
        fake_lb.write_script({"check": {"sleep": 1}})
    elif failure == "garbage":
        fake_lb.write_script({"check": {"stdout": "not json"}})
    else:
        fake_lb.write_script({"check": {"exit": int(failure[-1]), "stderr": "broken"}})
    # Dry-run exercises admission without building a model session.
    result = work("--dry-run", *(["--json"] if as_json else []), data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert result.output.count("logbook: hold check skipped") == 1
    if as_json:
        assert "warning" in json.loads(result.output.splitlines()[0])
        assert json.loads(result.output.splitlines()[1])["dry_run"]
    else:
        assert "[warn] logbook:" in result.output
        assert "argv: pi" in result.output


def test_logbook_resume_does_not_check(tmp_project, data_dir, fake_lb, fake_pi, adapter):
    from helmraiser.cli import app
    from tests.cli.test_work import runner

    first = queue_task(tmp_project, data_dir)
    ledger = Ledger(data_dir / "helmraiser.db")
    row = ledger.get_task(first)
    snapshot = row["snapshot"].replace("policy: review", "policy: approve-plan-first")
    ledger._get_conn().execute("UPDATE tasks SET snapshot = ? WHERE task_id = ?", (snapshot, first))
    ledger._get_conn().commit()
    result = work("--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    run = ledger.list_runs()[0]
    assert ledger.pending_approval(run["run_id"])["kind"] == "plan"
    approved = runner.invoke(app, ["approve", run["run_id"], "--data-dir", str(data_dir)])
    assert approved.exit_code == 0, approved.output
    queue_task(tmp_project, data_dir, task_id="later")
    block(fake_lb)
    before = fake_lb.calls()
    result = work("--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert [c for c in fake_lb.calls() if c["argv"][0] == "check"] == [
        c for c in before if c["argv"][0] == "check"
    ]
    (brief,) = fake_lb.calls()[len(before) :]
    assert brief["argv"][0] == "brief"
    assert brief["env"]["LOGBOOK_SESSION"] == run["run_id"]
    assert len(adapter.calls_for("implement")) == 1


def test_logbook_foreground_cache_refresh_after_run(
    tmp_project, data_dir, fake_lb, fake_pi, adapter, monkeypatch
):
    first = queue_task(tmp_project, data_dir, task_id="first")
    second = queue_task(tmp_project, data_dir, task_id="second")
    original = commands.GovernedRun.run

    async def run(self):
        result = await original(self)
        # Release the project admission slot so the next candidate is examined.
        self.ledger.finish_run(self.run_id, "landed")
        self.ledger.set_task_status(first, "landed")
        block(fake_lb)
        return result

    monkeypatch.setattr(commands.GovernedRun, "run", run)
    result = work("--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert f'"skipped": "{second}"' in result.output
    assert len([c for c in fake_lb.calls() if c["argv"][0] == "check"]) == 2
    assert len([c for c in fake_lb.calls() if c["argv"][0] == "brief"]) == 1
    ledger = Ledger(data_dir / "helmraiser.db")
    assert [r["task_id"] for r in ledger.list_runs()] == [first]


@pytest.fixture
def ledger(data_dir):
    return Ledger(data_dir / "helmraiser.db")


@pytest.fixture
def harness(ledger, monkeypatch):
    monkeypatch.setattr(supervisor, "_read_child_stat", lambda pid: ("S", str(pid)))
    return Harness(ledger)


def admission(ledger, gate):
    return lambda row: commands._task_skip(
        commands._task_from_row(row, configured_test_config()),
        ledger,
        configured_test_config(),
        True,
        holds=gate,
    )


def test_logbook_supervisor_pass_cache_change_and_release(
    ledger, data_dir, harness, tmp_path, fake_lb, capsys
):
    projects = [tmp_path / "a", tmp_path / "b"]
    ids = [queue_real(ledger, projects[0], f"a{i}") for i in range(3)]
    ids.append(queue_real(ledger, projects[1], "b"))
    block(fake_lb)
    gate = HoldGate(lambda text: pytest.fail(text))

    def second_poll():
        assert len(fake_lb.calls()) == 4  # two projects, two passes
        assert not ledger.list_runs()
        assert ledger._get_conn().execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 0
        fake_lb.write_script({"check": {"exit": 3, "stdout": json.dumps(HOLDS[:1])}})

    harness.hooks[2] = second_poll
    harness.hooks[4] = lambda: fake_lb.write_script({})
    harness.hooks[5] = lambda: (data_dir / "worker.stop").touch()
    supervise(
        ledger,
        data_dir,
        harness,
        watch=True,
        poll_s=1,
        skip=admission(ledger, gate),
        begin_pass=gate.clear,
    )
    skips = [e for e in events(capsys.readouterr().out) if "skipped" in e]
    assert len(skips) == 8  # once per task, then once for the changed hold set
    assert {e["skipped"] for e in skips} == set(ids)
    assert harness.spawned[0]["at"] == 4


def test_logbook_supervisor_project_scope(ledger, data_dir, harness, tmp_path, fake_lb):
    a, b = tmp_path / "a", tmp_path / "b"
    held = queue_real(ledger, a, "a")
    queue_real(ledger, b, "b")
    fake_lb.write_script({f"check@{a}": {"exit": 3, "stdout": json.dumps(HOLDS)}})
    gate = HoldGate(lambda text: pytest.fail(text))
    supervise(ledger, data_dir, harness, skip=admission(ledger, gate), begin_pass=gate.clear)
    assert [s["project"] for s in harness.spawned] == [str(b)]
    assert ledger.get_task(held)["status"] == "queued"
    assert all(r["task_id"] != held for r in ledger.list_runs())


def test_logbook_supervise_wires_shared_gate(ledger, data_dir, tmp_path, fake_lb, monkeypatch):
    from helmraiser.cli.supervisor import SupervisorResult

    row_id = queue_real(ledger, tmp_path / "a", "a")
    block(fake_lb)

    class Capture:
        def __init__(self, **options):
            row = ledger.get_task(row_id)
            assert options["skip"](row)
            assert options["skip"](row)
            assert len(fake_lb.calls()) == 1
            options["begin_pass"]()
            assert options["skip"](row)
            assert len(fake_lb.calls()) == 2

        def run(self):
            return SupervisorResult()

    monkeypatch.setattr(commands, "Supervisor", Capture)
    commands._supervise(
        ledger,
        configured_test_config(),
        data_dir,
        ProjectScope.of(),
        watch=False,
        max_parallel_runs=2,
        max_runs=None,
        no_caps=True,
        notify=False,
        as_json=True,
        data_dir=data_dir,
    )


def make_supervisor(ledger, data_dir, harness, gate):
    from tests.loop.test_reconcile import FakeController

    return supervisor.Supervisor(
        ledger=ledger,
        data_dir=data_dir,
        scope=ProjectScope.of(),
        max_parallel=2,
        watch=True,
        max_runs=None,
        poll_s=1,
        stop_file=data_dir / "worker.stop",
        spawn=harness.spawn,
        skip=admission(ledger, gate),
        begin_pass=gate.clear,
        notify=lambda row: None,
        as_json=True,
        controller=FakeController(),
        clock=harness.clock,
        wall_clock=harness.clock,
        run_config={},
    )


def test_logbook_live_skips_published_and_status(
    ledger, data_dir, harness, tmp_path, fake_lb, monkeypatch
):
    from helmraiser.cli import app, worker_commands
    from tests.cli.test_work import runner

    task_id = queue_real(ledger, tmp_path / "a", "a")
    block(fake_lb)
    worker_commands.write_record({"pid": os.getpid()}, data_dir)
    monkeypatch.setattr(worker_commands, "pid_alive", lambda pid: True)
    gate = HoldGate(lambda text: pytest.fail(text))
    instance = make_supervisor(ledger, data_dir, harness, gate)
    assert instance._fill() == 0
    active_path = data_dir / "worker.active.json"
    skips = json.loads(active_path.read_text())["skips"]
    assert skips == [
        {
            "task_id": task_id,
            "reason": "logbook hold: [hold-one] Tunnel open (release: close tunnel); [hold-two] Wait",
        }
    ]
    stamp = active_path.stat().st_mtime_ns
    instance._fill()
    assert active_path.stat().st_mtime_ns == stamp
    before = len(fake_lb.calls())
    result = runner.invoke(app, ["status", "--json", "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["worker"]["skips"] == skips
    assert json.loads(status_path(data_dir).read_text())["worker"]["skips"] == skips
    result = runner.invoke(app, ["status", "--data-dir", str(data_dir)])
    assert "Skipped: " + task_id + ": logbook hold: [hold-one]" in result.output.replace(
        "\n", ""
    ), result.output
    assert len(fake_lb.calls()) == before
    fake_lb.write_script({"check": {"exit": 3, "stdout": json.dumps(HOLDS[:1])}})
    instance._fill()
    assert json.loads(active_path.read_text())["skips"] != skips
    fake_lb.write_script({})
    assert instance._fill() == 1
    assert json.loads(active_path.read_text())["skips"] == []


@pytest.mark.parametrize(
    "active", [None, "broken", "[]", '{"pid": -1, "skips": [1]}', '{"pid": PID}']
)
def test_logbook_worker_skips_not_stale(data_dir, monkeypatch, active):
    from helmraiser.cli import worker_commands

    assert worker_commands.worker_payload(data_dir) == {"state": "none"}
    worker_commands.write_record({"pid": os.getpid()}, data_dir)
    monkeypatch.setattr(worker_commands, "pid_alive", lambda pid: True)
    if active is not None:
        (data_dir / "worker.active.json").write_text(active.replace("PID", str(os.getpid())))
    assert worker_commands.worker_payload(data_dir)["skips"] == []
    monkeypatch.setattr(worker_commands, "pid_alive", lambda pid: False)
    assert "skips" not in worker_commands.worker_payload(data_dir)


def test_logbook_other_skips_and_foreground_publication(ledger, data_dir, harness, monkeypatch):
    from helmraiser.cli import worker_commands

    gate = HoldGate(lambda text: None)
    instance = make_supervisor(ledger, data_dir, harness, gate)
    instance._report_skips({"task": ({"reason": "quota"}, "quota")})
    assert not (data_dir / "worker.active.json").exists()
    worker_commands.write_record({"pid": os.getpid()}, data_dir)
    instance._report_skips({"task": ({"reason": "caps"}, "caps")})
    assert json.loads((data_dir / "worker.active.json").read_text())["skips"] == [
        {"task_id": "task", "reason": "caps"}
    ]
    instance._report_skips({})
    assert json.loads((data_dir / "worker.active.json").read_text())["skips"] == []


@pytest.mark.parametrize("failure", ["spawn_error", "exit1", "timeout", "garbage"])
def test_logbook_supervisor_unavailable_warn_streak(
    ledger, data_dir, harness, tmp_path, fake_lb, monkeypatch, capsys, failure
):
    from helmraiser import logbook

    project = tmp_path / "a"
    queue_real(ledger, project, "a")
    if failure == "spawn_error":
        monkeypatch.setattr(logbook, "find_lb", lambda: "/nonexistent/lb")
    elif failure == "timeout":
        monkeypatch.setattr(logbook, "CHECK_TIMEOUT_S", 0.2)
        fake_lb.write_script({"check": {"sleep": 1}})
    elif failure == "garbage":
        fake_lb.write_script({"check": {"stdout": "bad json"}})
    else:
        fake_lb.write_script({"check": {"exit": 1, "stderr": "broken"}})
    gate = HoldGate(lambda text: print(f"[warn] {text}"))
    instance = make_supervisor(ledger, data_dir, harness, gate)
    # A child that exits successfully without a run leaves the task queued.
    instance.spawn = lambda project, output: FakeChild(harness, 999, lambda: 0, project)

    def pass_and_reap():
        assert instance._fill() == 1
        harness.now += 5
        instance._reap()
        assert instance.backoff[str(project)] == harness.now + 300
        harness.now += 301

    pass_and_reap()
    pass_and_reap()
    assert capsys.readouterr().out.count("[warn] logbook:") == 1
    monkeypatch.setattr(logbook, "find_lb", lambda: str(fake_lb.bin))
    fake_lb.write_script({"check": {"exit": 6, "stderr": "different"}})
    pass_and_reap()
    assert capsys.readouterr().out.count("[warn] logbook:") == 1
    fake_lb.write_script({})
    pass_and_reap()
    assert "[warn]" not in capsys.readouterr().out
    fake_lb.write_script({"check": {"exit": 6, "stderr": "different"}})
    pass_and_reap()
    assert capsys.readouterr().out.count("[warn] logbook:") == 1
    assert ledger.list_runs() == []


def test_logbook_parent_child_race_keeps_no_run_backoff(
    ledger, data_dir, harness, tmp_path, fake_lb, fake_pi
):
    project = tmp_path / "a"
    task_id = queue_real(ledger, project, "a")
    gate = HoldGate(lambda text: pytest.fail(text))
    instance = make_supervisor(ledger, data_dir, harness, gate)

    def spawn(project, output):
        block(fake_lb)  # owner adds a hold after the parent allowed selection
        result = work("--once", "--supervised", "--json", "--project", project, data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output.splitlines()[0])["skipped"] == task_id
        output.write_text(result.output)
        return FakeChild(harness, 999, lambda: 0, project)

    instance.spawn = spawn
    assert instance._fill() == 1
    assert len(fake_lb.calls()) == 2
    harness.now = 5
    instance._reap()
    assert instance.backoff[str(project)] == 305
    assert ledger.list_runs() == []
    assert ledger.get_task(task_id)["status"] == "queued"
    assert fake_pi.calls() == []


def test_logbook_supervisor_resume_and_running_project_not_checked(
    ledger, data_dir, harness, tmp_path, fake_lb
):
    project = tmp_path / "a"
    first = queue_real(ledger, project, "first")
    queue_real(ledger, project, "later")
    ledger.create_run(run_row("resume", first, str(project), status="awaiting_approval"))
    ledger.hand_back("resume", "implement")
    block(fake_lb)
    gate = HoldGate(lambda text: pytest.fail(text))
    instance = make_supervisor(ledger, data_dir, harness, gate)
    assert instance._fill() == 1
    assert harness.spawned[0]["resumed"] == "resume"
    instance._fill()
    assert fake_lb.calls() == []
    harness.now = 5
    instance._reap()
    assert ledger.get_run("resume")["status"] == "landed"
    instance._fill()
    assert len(fake_lb.calls()) == 1
