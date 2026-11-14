"""Ledger-only waits: fake time, no worker or model processes."""

from __future__ import annotations

import copy
import json

import pytest
import typer
from typer.testing import CliRunner

from helmraiser.cli import app, monitor
from helmraiser.config import DEFAULT_CONFIG
from helmraiser.ledger.db import RUN_STATUSES, Ledger
from helmraiser.ledger.rundir import RunDir
from helmraiser.task.loader import parse_task

runner = CliRunner()
TASK = """---
id: {id}
title: Wait test
project: {project}
status: queued
priority: normal
policy: review
isolation: none
verify:
  - name: marker
    run: "true"
---
## Goal

Wait.
"""
KEYS = {
    "run_id",
    "task_id",
    "status",
    "pending",
    "verdict",
    "cost_usd",
    "failure_reason",
    "branch_name",
    "pr_url",
    "stage",
    "project",
    "started_at",
    "ended_at",
    "timed_out",
}


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def ledger(data_dir):
    with Ledger(data_dir / "helmraiser.db") as db:
        yield db


def seed_task(ledger, project, task_id="widget", status="queued"):
    text = TASK.format(id=task_id, project=project)
    spec = parse_task(text, project / "topics" / f"{task_id}.md", copy.deepcopy(DEFAULT_CONFIG))
    full = ledger.upsert_task(spec, text)
    ledger.set_task_status(full, status)
    return full


def seed_run(ledger, task_id, run_id="run-1", status="running", **fields):
    ledger.create_run(
        {
            "run_id": run_id,
            "task_id": task_id,
            "project": "/repo",
            "status": status,
            "policy": "review",
            "isolation": "none",
            "model_implement": "p/i",
            "model_review": "p/r",
            "pi_version": "0.87.1",
            "started_at": 1,
            **fields,
        }
    )
    return run_id


def invoke(ref, data_dir, *args, human=False):
    return runner.invoke(
        app, ["wait", ref, "--data-dir", str(data_dir), *([] if human else ["--json"]), *args]
    )


@pytest.mark.parametrize("form", ["exact", "full", "bare", "prefix"])
def test_resolve_forms(ledger, tmp_path, form):
    task = seed_task(ledger, tmp_path)
    seed_run(ledger, task)
    ref = {"exact": "run-1", "full": task, "bare": "widget", "prefix": "run"}[form]
    kind = "task" if form in ("full", "bare") else "run"
    assert monitor._resolve_wait_ref(ledger, ref, as_json=True) == (
        kind,
        task if kind == "task" else "run-1",
    )


def test_resolution_precedence(ledger, tmp_path):
    task = seed_task(ledger, tmp_path, "run")
    seed_run(ledger, task, "run-1")
    assert monitor._resolve_wait_ref(ledger, "run", as_json=True) == ("task", task)
    seed_run(ledger, task, "run")
    assert monitor._resolve_wait_ref(ledger, "run", as_json=True) == ("run", "run")


@pytest.mark.parametrize("ref", ["widget", "run", "absent", "missing:task"])
def test_resolution_errors(ledger, tmp_path, capsys, ref):
    task = seed_task(ledger, tmp_path / "one")
    other = seed_task(ledger, tmp_path / "two")
    seed_run(ledger, task, "run-1")
    seed_run(ledger, other, "run-2")
    with pytest.raises(typer.Exit) as exc:
        monitor._resolve_wait_ref(ledger, ref, as_json=True)
    assert exc.value.exit_code == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert ref in error
    if ref == "widget":
        assert task in error and other in error
    if ref == "run":
        assert "run-1" in error and "run-2" in error


@pytest.mark.parametrize("pending", ["plan", "scope", "merge", None])
def test_payload_pending(ledger, tmp_path, pending):
    task = seed_task(ledger, tmp_path)
    seed_run(
        ledger,
        task,
        status="awaiting_approval",
        started_at=0,
        ended_at=1,
        cost_usd=0.123456,
        verdict="pass",
    )
    scope = {"paths": ["src/extra.py"], "reason": "need it"}
    if pending:
        ledger.record_approval("run-1", pending, "pending")
    if pending == "scope":
        ledger.append_event("run-1", "helmraiser", "scope_requested", scope)
    row = monitor._wait_run_row(ledger, ledger.get_run("run-1"), task, "queued", timed_out=False)
    assert set(row) == KEYS | ({"scope_request"} if pending == "scope" else set())
    assert row["pending"] == (pending or "merge")
    assert row["started_at"] == "1970-01-01T00:00:00+00:00"
    assert row["ended_at"] == "1970-01-01T00:00:01+00:00"
    assert row["cost_usd"] == 0.1235
    if pending == "scope":
        assert row["scope_request"] == scope
    ledger.update_run("run-1", status="running")
    row = monitor._wait_run_row(ledger, ledger.get_run("run-1"), task, "queued", timed_out=True)
    assert row["pending"] is None
    assert "scope_request" not in row


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 0.0

        def __init__(self):
            self.sleeps = []
            self.on_sleep = lambda: None

        def sleep(self, seconds):
            assert len(self.sleeps) < 10, "unbounded wait"
            self.sleeps.append(seconds)
            self.now += seconds
            self.on_sleep()

    fake = Clock()
    monkeypatch.setattr(monitor.time, "monotonic", lambda: fake.now)
    monkeypatch.setattr(monitor.time, "sleep", fake.sleep)
    return fake


@pytest.mark.parametrize(
    "status,code", [("awaiting_approval", 0), ("landed", 0), ("failed", 1), ("aborted", 1)]
)
def test_immediate_outcomes(ledger, data_dir, tmp_path, clock, status, code):
    task = seed_task(ledger, tmp_path)
    seed_run(ledger, task, status=status, failure_reason="rejected" if status == "failed" else None)
    result = invoke("run-1", data_dir, "--timeout", "0")
    assert result.exit_code == code, result.output
    row = json.loads(result.stdout)
    assert set(row) == KEYS
    assert row["status"] == status
    assert row["timed_out"] is False
    assert row["pending"] == ("merge" if status == "awaiting_approval" else None)
    assert not result.stderr
    assert not clock.sleeps


def test_leading_dash_task_id(ledger, data_dir, tmp_path, clock):
    task = seed_task(ledger, tmp_path, status="awaiting_approval")
    assert task.startswith("-")
    seed_run(ledger, task, status="awaiting_approval")
    result = invoke(task, data_dir)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["run_id"] == "run-1"


def test_human_timeout(ledger, data_dir, tmp_path, clock):
    task = seed_task(ledger, tmp_path)
    seed_run(ledger, task, failure_reason="[bold]café[/bold]", branch_name="[blue]branch")
    result = invoke("run-1", data_dir, "--timeout", "0", human=True)
    assert result.exit_code == 124
    assert "running" in result.stdout and "run-1" in result.stdout
    assert "reason: [bold]café[/bold]" in result.stdout
    assert "branch: [blue]branch" in result.stdout
    assert "Timed out" in result.stderr


def test_interrupt_is_silent(ledger, data_dir, tmp_path, clock):
    task = seed_task(ledger, tmp_path)
    seed_run(ledger, task)

    def interrupt():
        raise KeyboardInterrupt

    clock.on_sleep = interrupt
    result = invoke("run-1", data_dir)
    assert result.exit_code == 130
    assert result.stdout == result.stderr == ""


def test_no_run_payload(ledger):
    row = monitor._wait_run_row(ledger, None, "project:widget", "queued", timed_out=True)
    assert set(row) == KEYS
    assert row["task_id"] == "project:widget"
    assert row["status"] == "queued"
    assert row["timed_out"] is True
    assert all(row[key] is None for key in KEYS - {"task_id", "status", "timed_out"})


# Deliberately explicit: a new status requires a human decision for both modes.
STATUS_DECISIONS = {
    "starting": (124, 124),
    "running": (124, 124),
    "verifying": (124, 124),
    "reviewing": (124, 124),
    "awaiting_approval": (0, 124),
    "resuming": (124, 124),
    "landed": (0, 0),
    "failed": (1, 1),
    "aborting": (124, 124),
    "aborted": (1, 1),
}


def test_all_statuses_have_a_wait_decision():
    assert set(STATUS_DECISIONS) == RUN_STATUSES


@pytest.mark.parametrize("status,decisions", STATUS_DECISIONS.items())
@pytest.mark.parametrize("until,index", [("attention", 0), ("end", 1)])
def test_every_status(ledger, data_dir, tmp_path, clock, status, decisions, until, index):
    task = seed_task(ledger, tmp_path)
    seed_run(ledger, task, status=status)
    result = invoke("run-1", data_dir, "--until", until, "--timeout", "0")
    assert result.exit_code == decisions[index], result.output
    row = json.loads(result.stdout)
    assert row["status"] == status
    assert row["timed_out"] == (decisions[index] == 124)
    assert row["pending"] == ("merge" if status == "awaiting_approval" else None)
    assert result.stderr == ""
    assert not clock.sleeps


@pytest.mark.parametrize("ref", ["run-1", "run", "widget", "full"])
def test_command_ref_forms(ledger, data_dir, tmp_path, clock, ref):
    task = seed_task(ledger, tmp_path, status="landed")
    seed_run(ledger, task, status="landed")
    result = invoke(task if ref == "full" else ref, data_dir)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["task_id"] == task


@pytest.mark.parametrize("ref", ["widget", "run", "unknown"])
def test_command_resolution_errors(ledger, data_dir, tmp_path, clock, ref):
    first = seed_task(ledger, tmp_path / "one")
    second = seed_task(ledger, tmp_path / "two")
    seed_run(ledger, first, "run-1")
    seed_run(ledger, second, "run-2")
    result = invoke(ref, data_dir)
    assert result.exit_code == 2
    assert set(json.loads(result.stdout)) == {"error"}
    assert ref in json.loads(result.stdout)["error"]
    assert result.stderr == ""
    assert not clock.sleeps


@pytest.mark.parametrize("status", ["draft", "dropped", "intake"])
def test_nonqueued_task_without_run(ledger, data_dir, tmp_path, clock, status):
    seed_task(ledger, tmp_path, status=status)
    result = invoke("widget", data_dir)
    assert result.exit_code == 2
    assert status in json.loads(result.stdout)["error"]
    assert "widget" in result.stdout
    assert not clock.sleeps


@pytest.mark.parametrize("old_run", [False, True])
def test_queued_task_timeout_ignores_history(ledger, data_dir, tmp_path, clock, old_run):
    task = seed_task(ledger, tmp_path)
    if old_run:
        seed_run(ledger, task, status="failed")
        # Even changing an old run cannot turn it into a newly scheduled run.
        clock.on_sleep = lambda: ledger.update_run("run-1", status="landed")
    result = invoke("widget", data_dir, "--timeout", "2", "--interval", "1")
    assert result.exit_code == 124, result.output
    row = json.loads(result.stdout)
    assert row["run_id"] is None and row["status"] == "queued"
    assert row["task_id"] == task
    assert all(row[key] is None for key in KEYS - {"task_id", "status", "timed_out"})
    assert clock.sleeps == [1, 1]


@pytest.mark.parametrize("old_run", [False, True])
def test_dispatch_between_initial_task_and_history_reads(
    ledger, data_dir, tmp_path, clock, monkeypatch, old_run
):
    task_id = seed_task(ledger, tmp_path)
    if old_run:
        seed_run(ledger, task_id, status="failed")
    original_get_task = Ledger.get_task
    dispatched = False

    def get_task_then_dispatch(self, key):
        nonlocal dispatched
        task = original_get_task(self, key)
        if self.read_only and key == task_id and not dispatched:
            assert task["status"] == "queued"
            dispatched = True
            # Commit on the worker's separate connection between the waiter's
            # initial task-status read and its run-history read.
            ledger.claim_queued_run(
                {
                    "run_id": "new-run",
                    "task_id": task_id,
                    "project": str(tmp_path),
                    "policy": "review",
                    "isolation": "none",
                    "model_implement": "p/i",
                    "model_review": "p/r",
                    "pi_version": "0.87.1",
                },
                snapshot=task["snapshot"],
                config=copy.deepcopy(DEFAULT_CONFIG),
            )
        return task

    monkeypatch.setattr(Ledger, "get_task", get_task_then_dispatch)
    clock.on_sleep = lambda: ledger.finish_run("new-run", "landed")
    result = invoke("widget", data_dir, "--timeout", "20")
    assert dispatched
    assert result.exit_code == 0, result.output
    row = json.loads(result.stdout)
    assert row["run_id"] == "new-run"
    assert row["status"] == "landed"
    assert not row["timed_out"]
    assert clock.sleeps == [10]
    assert not result.stderr


@pytest.mark.parametrize("old_run", [False, True])
def test_queued_task_follows_new_run(ledger, data_dir, tmp_path, clock, old_run):
    task = seed_task(ledger, tmp_path)
    if old_run:
        seed_run(ledger, task, status="failed")

    def worker():
        if len(clock.sleeps) == 1:
            seed_run(ledger, task, "new-run", "starting", started_at=2)
            ledger.set_task_status(task, "starting")
        else:
            ledger.update_run("new-run", status="awaiting_approval")
            ledger.set_task_status(task, "awaiting_approval")

    clock.on_sleep = worker
    result = invoke("widget", data_dir)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["run_id"] == "new-run"
    assert clock.sleeps == [10, 10]
    assert len(result.stdout.splitlines()) == 1
    assert not result.stderr


@pytest.mark.parametrize("initial,final", [("running", "failed"), ("aborting", "aborted")])
def test_run_status_changes(ledger, data_dir, tmp_path, clock, initial, final):
    task = seed_task(ledger, tmp_path)
    seed_run(ledger, task, status=initial)
    clock.on_sleep = lambda: ledger.finish_run("run-1", final, "verify_failed")
    result = invoke("run-1", data_dir, "--until", "end")
    assert result.exit_code == 1, result.output
    row = json.loads(result.stdout)
    assert row["status"] == final
    assert row["failure_reason"] == "verify_failed"
    assert clock.sleeps == [10]


@pytest.mark.parametrize("ref", ["widget", "run-1", "run"])
def test_task_tracks_latest_but_run_stays_pinned(ledger, data_dir, tmp_path, clock, ref):
    task = seed_task(ledger, tmp_path, status="running")
    seed_run(ledger, task)

    def worker():
        seed_run(ledger, task, "new-run", "landed", started_at=2)
        ledger.set_task_status(task, "landed")

    clock.on_sleep = worker
    result = invoke(ref, data_dir, "--timeout", "1", "--interval", "1")
    assert result.exit_code == (0 if ref == "widget" else 124), result.output
    row = json.loads(result.stdout)
    assert row["run_id"] == ("new-run" if ref == "widget" else "run-1")
    assert clock.sleeps == [1]


@pytest.mark.parametrize(
    "timeout,interval,sleeps", [("2.5", "2", [2, 0.5]), ("0.25", "10", [0.25]), ("0", "1", [])]
)
def test_deadline_caps_sleep(ledger, data_dir, tmp_path, clock, timeout, interval, sleeps):
    task = seed_task(ledger, tmp_path)
    seed_run(ledger, task)
    result = invoke("run-1", data_dir, "--timeout", timeout, "--interval", interval)
    assert result.exit_code == 124, result.output
    assert clock.sleeps == sleeps
    assert clock.now == float(timeout)


def test_condition_at_deadline_wins(ledger, data_dir, tmp_path, clock):
    task = seed_task(ledger, tmp_path)
    seed_run(ledger, task)
    clock.on_sleep = lambda: ledger.update_run("run-1", status="landed")
    result = invoke("run-1", data_dir, "--timeout", "1", "--interval", "1")
    assert result.exit_code == 0
    assert not json.loads(result.stdout)["timed_out"]
    assert clock.sleeps == [1]


@pytest.mark.parametrize(
    "option,value",
    [
        ("--until", "invalid"),
        ("--timeout", "-1"),
        ("--interval", "0"),
        ("--interval", "0.5"),
        ("--timeout", "bad"),
        ("--interval", "bad"),
        ("--timeout", "nan"),
        ("--interval", "inf"),
    ],
)
def test_invalid_options(ledger, data_dir, clock, option, value):
    result = invoke("my-ref", data_dir, option, value)
    assert result.exit_code == 2
    error = json.loads(result.stdout)["error"]
    assert "my-ref" in error and option in error
    assert not result.stderr and not clock.sleeps


@pytest.mark.parametrize("pending", ["plan", "scope", "merge", None])
def test_pending_command(ledger, data_dir, tmp_path, clock, pending):
    task = seed_task(ledger, tmp_path)
    seed_run(
        ledger,
        task,
        status="awaiting_approval",
        verdict="pass",
        branch_name="[b]branch",
        pr_url="https://example.test/pr",
    )
    if pending:
        ledger.record_approval("run-1", pending, "pending")
    scope = {"paths": ["extra.py"], "reason": "[bold]café[/bold]"}
    if pending == "scope":
        ledger.append_event("run-1", "helmraiser", "scope_requested", scope)
    result = invoke("run-1", data_dir)
    assert result.exit_code == 0, result.output
    row = json.loads(result.stdout)
    assert row["pending"] == (pending or "merge")
    assert set(row) == KEYS | ({"scope_request"} if pending == "scope" else set())
    if pending == "scope":
        assert row["scope_request"] == scope
    human = invoke("run-1", data_dir, human=True)
    assert human.exit_code == 0
    assert "pending: " + (pending or "merge") in human.stdout
    assert "branch: [b]branch" in human.stdout
    assert "pr: https://example.test/pr" in human.stdout
    assert "review: pass" in human.stdout
    assert "next: helmraiser approve run-1 (or reject)" in human.stdout
    if pending == "scope":
        assert "scope request run-1: extra.py — [bold]café[/bold]" in human.stdout


@pytest.mark.parametrize(
    "ref,timeout,code", [("run-1", "0", 0), ("run-2", "1", 124), ("missing", "0", 2)]
)
def test_wait_changes_no_rows_or_application_files(
    ledger, data_dir, tmp_path, clock, monkeypatch, ref, timeout, code
):
    task = seed_task(ledger, tmp_path)
    seed_run(ledger, task, status="awaiting_approval")
    seed_run(ledger, task, "run-2")
    ledger.record_approval("run-1", "merge", "pending")
    ledger.append_event("run-1", "helmraiser", "example", {})
    ledger.hold_provider("expired", 0, "do not clean up")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    conn = ledger._get_conn()
    before = list(conn.iterdump())
    version = conn.execute("PRAGMA user_version").fetchone()[0]

    def files():
        return {
            str(path): path.read_bytes()
            for parent in (data_dir, cwd)
            for path in parent.rglob("*")
            if path.is_file() and not path.name.startswith("helmraiser.db")
        }

    before_files = files()

    def forbidden(*args, **kwargs):
        pytest.fail("wait attempted a write")

    monkeypatch.setattr(Ledger, "_write", forbidden)
    monkeypatch.setattr(monitor, "write_status_atomic", forbidden)
    monkeypatch.setattr(RunDir, "create", forbidden)
    result = invoke(ref, data_dir, "--timeout", timeout, "--interval", "1")
    assert result.exit_code == code, result.output
    assert list(conn.iterdump()) == before
    assert conn.execute("PRAGMA user_version").fetchone()[0] == version
    assert files() == before_files


def test_missing_ledger_does_not_create_files(tmp_path, clock):
    data = tmp_path / "missing"
    result = invoke("some-ref", data)
    assert result.exit_code == 2
    assert "some-ref" in json.loads(result.stdout)["error"]
    assert not data.exists()


def test_outdated_ledger_is_not_migrated(ledger, data_dir, clock):
    conn = ledger._get_conn()
    conn.execute("PRAGMA user_version = 8")
    before = list(conn.iterdump())
    result = invoke("some-ref", data_dir)
    assert result.exit_code == 2
    assert "some-ref" in json.loads(result.stdout)["error"]
    assert "migration" in result.stdout
    assert list(conn.iterdump()) == before
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
