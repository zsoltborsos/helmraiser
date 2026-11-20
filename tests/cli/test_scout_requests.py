"""Contract tests for owner-requested scouts."""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, monitor, scout_commands, supervisor
from helmraiser.config import SUBSCRIPTION_TEMPLATES
from helmraiser.ledger.db import SCHEMA_VERSION, Ledger, ProjectScope, ScoutRequestUnavailable
from helmraiser.loop import scout as scout_module
from helmraiser.loop.scout import ScoutError, scout_gate
from helmraiser.loop.status import build_scout_status
from helmraiser.pi.adapter import PiJsonAdapter
from tests.cli.test_supervisor import Harness, supervise
from tests.conftest import FakePi, configured_test_config
from tests.ledger.test_db import make_spec, run_row

runner = CliRunner()


@pytest.fixture(autouse=True)
def runtime_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))


def config_for(*projects: Path, enabled: bool = False) -> dict[str, Any]:
    config = configured_test_config()
    config["subscriptions"] = {
        "openai-codex": copy.deepcopy(SUBSCRIPTION_TEMPLATES["openai-codex"])
    }
    config["routing"]["scout"].update(
        enabled=enabled,
        focus_projects=[str(project) for project in projects],
        paused_projects=[],
        model="openai-codex/gpt-6.1-sol:high",
    )
    return config


def invoke_request(data_dir: Path, *args: str):
    return runner.invoke(
        app,
        ["drafts", "request", *args, "--data-dir", str(data_dir)],
        catch_exceptions=False,
    )


def test_ledger_request_consumption_is_atomic_with_scout_creation(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "helmraiser.db")
    requested, pending = ledger.request_scouts(["/a", "/a", "/b"], requested_at=10.0)
    assert requested == ["/a", "/b"] and pending == []
    assert ledger.request_scouts(["/b", "/a"], requested_at=20.0) == ([], ["/b", "/a"])
    first = ledger.list_scout_requests()[0]
    record = {
        "id": "scout-1",
        "project": "/a",
        "model": "p/m",
        "request_id": first["id"],
        "started_at": 31.0,
        "report_path": str(tmp_path / "report"),
    }
    # A failed INSERT must roll back consumption in the same transaction.
    with pytest.raises(sqlite3.IntegrityError):
        ledger.create_scout({**record, "model": None})
    assert ledger.list_scout_requests()[0] == first
    assert ledger.list_scouts() == []
    assert ledger.create_scout(record) == "scout-1"
    # A separate connection models another worker's child arriving late.
    competitor = Ledger(ledger.db_path)
    with pytest.raises(ScoutRequestUnavailable):
        competitor.create_scout({**record, "id": "scout-2"})
    assert competitor.get_scout("scout-2") is None
    assert ledger.get_scout_request(first["id"])["scout_id"] == "scout-1"
    assert ledger.get_scout("scout-1")["request_id"] == first["id"]
    assert ledger.request_scouts(["/a"], requested_at=40.0) == (["/a"], [])
    assert ledger.cancel_scout_requests(["/a", "/missing"]) == (
        ["/a"],
        ["/missing"],
    )
    assert ledger.get_scout_request(first["id"])["started_at"] == 31.0


@pytest.fixture
def available_headroom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scout_module.spend, "live_for", lambda *args, **kwargs: {})
    monkeypatch.setattr(scout_module.spend, "usage", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        scout_module,
        "calculate_headroom",
        lambda *args, **kwargs: {"openai-codex": {"has_headroom": True}},
    )


def introduce_claim_blocker(ledger: Ledger, project: Path, blocker: str) -> None:
    if blocker == "pause":
        ledger.pause_queue("production tunnel")
    elif blocker == "queued":
        ledger.upsert_task(make_spec(project), "task")
    else:
        # Keep the task non-queued to exercise the run check independently.
        task_id = ledger.upsert_task(make_spec(project, status="done"), "task")
        ledger.create_run(run_row("active", task_id, str(project), status=blocker))


@pytest.mark.parametrize(
    ("blocker", "wait_reason"),
    [
        ("pause", "queue paused"),
        ("queued", "project busy"),
        ("running", "project busy"),
        ("needs_merge", "project busy"),
    ],
)
def test_ledger_claim_rechecks_safety_after_gate(
    tmp_project: Path,
    tmp_path: Path,
    available_headroom,
    blocker: str,
    wait_reason: str,
) -> None:
    ledger = Ledger(tmp_path / "helmraiser.db")
    ledger.request_scouts([tmp_project], requested_at=10.0)
    request = ledger.list_scout_requests()[0]
    scout_gate(tmp_project, ledger, config_for(tmp_project), 20.0, ignore_enabled_and_cooldown=True)
    with Ledger(ledger.db_path) as competitor:
        introduce_claim_blocker(competitor, tmp_project, blocker)
    with pytest.raises(ScoutRequestUnavailable) as caught:
        ledger.create_scout(
            {
                "id": "blocked",
                "project": str(tmp_project),
                "model": "p/m",
                "request_id": request["id"],
                "report_path": str(tmp_path / "report"),
            }
        )
    assert caught.value.wait_reason == wait_reason
    assert ledger.list_scouts() == []
    assert ledger.get_scout_request(request["id"]) == request
    assert ledger.list_scout_requests() == [request]


@pytest.mark.parametrize("status", ["landed", "failed", "aborted"])
def test_ledger_claim_ignores_terminal_runs_other_projects_and_expired_pause(
    tmp_project: Path,
    tmp_path: Path,
    available_headroom,
    status: str,
) -> None:
    ledger = Ledger(tmp_path / "helmraiser.db")
    ledger.request_scouts([tmp_project], requested_at=10.0)
    request = ledger.list_scout_requests()[0]
    scout_gate(tmp_project, ledger, config_for(tmp_project), 20.0, ignore_enabled_and_cooldown=True)
    with Ledger(ledger.db_path) as competitor:
        introduce_claim_blocker(competitor, tmp_project, status)
        introduce_claim_blocker(competitor, tmp_path / "other", "queued")
        other_id = competitor.upsert_task(
            make_spec(tmp_path / "other", "active-other", status="done"), "task"
        )
        competitor.create_run(run_row("other-run", other_id, str(tmp_path / "other")))
        competitor.pause_queue("expired", until=1.0)
    assert (
        ledger.create_scout(
            {
                "id": "claimed",
                "project": str(tmp_project),
                "model": "p/m",
                "request_id": request["id"],
                "started_at": 31.0,
                "report_path": str(tmp_path / "report"),
            }
        )
        == "claimed"
    )
    assert ledger.list_scout_requests() == []
    assert ledger.get_scout_request(request["id"]) == {
        **request,
        "started_at": 31.0,
        "scout_id": "claimed",
    }
    assert len(ledger.list_scouts()) == 1
    assert ledger.get_scout("claimed")["request_id"] == request["id"]


@pytest.mark.parametrize("blocker", ["pause", "queued", "running", "needs_merge"])
@pytest.mark.parametrize("mode", ["requested", "unrequested", "force"])
def test_cli_claim_safety_race_before_model(
    tmp_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pi: FakePi,
    available_headroom,
    blocker: str,
    mode: str,
) -> None:
    data_dir = tmp_path / "data"
    ledger = Ledger(data_dir / "helmraiser.db")
    ledger.request_scouts([tmp_project], requested_at=7.0)
    request = ledger.list_scout_requests()[0]
    config = config_for(tmp_project, enabled=True)
    monkeypatch.setattr(scout_commands, "load_config", lambda: config)
    fake_pi.write_script({"default": {"text": "[]"}})
    monkeypatch.setattr(scout_commands, "_intake_callback", lambda *args: None)
    monkeypatch.setattr(scout_module, "_logbook_brief", lambda *args: None)

    def adapter_after_gate(*args):
        # Adapter construction is after the real gate, before the atomic claim.
        with Ledger(ledger.db_path) as competitor:
            introduce_claim_blocker(competitor, tmp_project, blocker)
        return PiJsonAdapter()

    monkeypatch.setattr(scout_commands, "get_scout_adapter", adapter_after_gate)
    args = ["scout", str(tmp_project), "--json", "--data-dir", str(data_dir), "--scout-id", "child"]
    if mode != "unrequested":
        args += ["--request-id", str(request["id"])]
    if mode == "force":
        args += ["--force"]
    result = runner.invoke(app, args, catch_exceptions=False)
    payload = json.loads(result.output)
    if mode == "requested":
        assert result.exit_code == 1, result.output
        assert payload == {
            "error": (
                "queue is paused"
                if blocker == "pause"
                else (
                    "project has queued work"
                    if blocker == "queued"
                    else f"project has a non-terminal run: {blocker}"
                )
            ),
            "scout_waiting": True,
            "lens": "state",
        }
        assert ledger.list_scouts() == []
        assert ledger.list_scout_requests() == [request]
        assert ledger.get_scout_request(request["id"]) == request
        assert fake_pi.calls() == []
    else:
        assert result.exit_code == 0, result.output
        assert payload["outcome"] == "succeeded"
        assert len(ledger.list_scouts()) == 1
        assert len(fake_pi.calls()) == 1
        if mode == "force":
            assert ledger.list_scout_requests() == []
            assert ledger.get_scout_request(request["id"])["scout_id"] == "child"
        else:
            assert ledger.list_scout_requests() == [request]


def test_schema_six_migrates_existing_scout_and_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    connection = sqlite3.connect(db)
    connection.executescript("""
        CREATE TABLE scouts (
          id TEXT PRIMARY KEY, project TEXT NOT NULL, model TEXT NOT NULL,
          head_commit TEXT, started_at REAL NOT NULL, ended_at REAL,
          cost_usd REAL NOT NULL DEFAULT 0, tokens_in INTEGER NOT NULL DEFAULT 0,
          tokens_out INTEGER NOT NULL DEFAULT 0, outcome TEXT NOT NULL,
          drafts_written TEXT NOT NULL DEFAULT '[]', report_path TEXT NOT NULL
        );
        INSERT INTO scouts
          (id, project, model, started_at, outcome, report_path)
          VALUES ('old', '/repo', 'p/m', 1, 'succeeded', '/report');
        PRAGMA user_version = 6;
        """)
    connection.commit()
    connection.close()

    ledger = Ledger(db)
    assert ledger.get_scout("old")["request_id"] is None
    assert ledger.get_scout("old")["error"] is None
    assert ledger.request_scouts(["/repo"], requested_at=2.0) == (["/repo"], [])
    ledger.close()
    reopened = Ledger(db)
    assert reopened.list_scout_requests()[0]["project"] == "/repo"
    assert reopened._get_conn().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    indexes = {
        row[1]
        for row in reopened._get_conn().execute("PRAGMA index_list(scout_requests)").fetchall()
    }
    assert "scout_requests_pending_project" in indexes


def test_request_cli_json_idempotency_duplicates_and_cancel(
    tmp_path: Path, git_init, monkeypatch: pytest.MonkeyPatch
) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    git_init(one)
    git_init(two)
    nested = one / "nested"
    nested.mkdir()
    data_dir = tmp_path / "data"
    config = config_for(one, two)
    monkeypatch.setattr(scout_commands, "load_config", lambda: config)

    result = invoke_request(data_dir, str(nested), str(one), str(two), "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "requested": [str(one.resolve()), str(two.resolve())],
        "already_pending": [],
    }
    again = invoke_request(data_dir, str(two), str(one), "--json")
    assert json.loads(again.output) == {
        "requested": [],
        "already_pending": [str(two.resolve()), str(one.resolve())],
    }

    config["routing"]["scout"]["focus_projects"] = []
    config["routing"]["scout"]["paused_projects"] = [str(one)]
    cancelled = invoke_request(data_dir, "--cancel", str(one), str(two), str(two), "--json")
    assert json.loads(cancelled.output) == {
        "cancelled": [str(one.resolve()), str(two.resolve())],
        "not_pending": [],
    }
    not_pending = invoke_request(data_dir, "--cancel", str(one), "--json")
    assert json.loads(not_pending.output) == {
        "cancelled": [],
        "not_pending": [str(one.resolve())],
    }


def test_request_cli_refuses_mixed_nonfocus_and_paused_without_partial_writes(
    tmp_path: Path, git_init, monkeypatch: pytest.MonkeyPatch
) -> None:
    good, bad = tmp_path / "good", tmp_path / "bad"
    good.mkdir()
    bad.mkdir()
    git_init(good)
    git_init(bad)
    data_dir = tmp_path / "data"
    config = config_for(good)
    monkeypatch.setattr(scout_commands, "load_config", lambda: config)

    result = invoke_request(data_dir, str(good), str(bad), "--json")
    assert result.exit_code == 2
    assert not (data_dir / "helmraiser.db").exists()

    config["routing"]["scout"]["focus_projects"] = [str(good), str(bad)]
    config["routing"]["scout"]["paused_projects"] = [str(bad)]
    result = invoke_request(data_dir, str(good), str(bad), "--json")
    assert result.exit_code == 2
    assert not (data_dir / "helmraiser.db").exists()


def test_request_cli_human_phrases(tmp_project: Path, tmp_path: Path, monkeypatch) -> None:
    config = config_for(tmp_project)
    monkeypatch.setattr(scout_commands, "load_config", lambda: config)
    data_dir = tmp_path / "data"
    assert "requested:" in invoke_request(data_dir, str(tmp_project)).output
    assert "already pending:" in invoke_request(data_dir, str(tmp_project)).output
    assert "cancelled:" in invoke_request(data_dir, "--cancel", str(tmp_project)).output
    assert "not pending:" in invoke_request(data_dir, "--cancel", str(tmp_project)).output


def test_requested_gate_skips_disabled_and_head_but_not_safety_gates(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Ledger(tmp_path / "db")
    config = config_for(tmp_project, enabled=False)
    monkeypatch.setattr(scout_module, "head_commit", lambda repo: pytest.fail("HEAD was read"))
    monkeypatch.setattr(scout_module.spend, "live_for", lambda *args, **kwargs: {})
    monkeypatch.setattr(scout_module.spend, "usage", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        scout_module,
        "calculate_headroom",
        lambda *args, **kwargs: {"openai-codex": {"has_headroom": True}},
    )

    scout_gate(
        tmp_project,
        ledger,
        config,
        100.0,
        ignore_enabled_and_cooldown=True,
    )
    ledger.pause_queue("tunnel")
    with pytest.raises(ScoutError) as caught:
        scout_gate(
            tmp_project,
            ledger,
            config,
            100.0,
            ignore_enabled_and_cooldown=True,
        )
    assert caught.value.wait_reason == "queue paused"


@pytest.mark.parametrize(
    ("gate_reason", "paused", "worker", "expected"),
    [
        (None, True, {}, "queue paused"),
        ("project busy", False, {}, "project busy"),
        ("draft cap", False, {}, "draft cap"),
        ("no headroom", False, {}, "no headroom"),
        (None, False, {"state": "running", "max_parallel_runs": 1, "children": [{}]}, "slots busy"),
        (None, False, {}, None),
    ],
)
def test_pending_status_wait_reasons_retain_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_reason: str | None,
    paused: bool,
    worker: dict[str, Any],
    expected: str | None,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    ledger = Ledger(tmp_path / "db")
    ledger.request_scouts([str(project)], requested_at=10.0)
    if paused:
        ledger.pause_queue("reason")

    def gate(*args, **kwargs):
        if gate_reason:
            raise ScoutError("blocked", wait_reason=gate_reason)

    monkeypatch.setattr(scout_module, "scout_gate", gate)
    config = config_for(project)
    payload = build_scout_status(ledger, config, 20.0, worker)
    assert payload["pending"] == [
        {
            "project": str(project.resolve()),
            "requested_at": "1970-01-01T00:00:10+00:00",
            "lens": "state",
            "wait_reason": expected,
        }
    ]
    assert len(ledger.list_scout_requests()) == 1


def test_requested_supervisor_runs_oldest_when_disabled_and_links_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ledger = Ledger(data_dir / "helmraiser.db")
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    ledger.request_scouts([str(first)], requested_at=1.0, lens="docs")
    ledger.request_scouts([str(second)], requested_at=2.0)
    harness = Harness(ledger)
    harness.scout_fail.add(str(first))
    harness.hooks[20] = lambda: (data_dir / "worker.stop").write_text("stop\n")
    seen: list[bool] = []

    def gate(*args, **kwargs):
        requested = bool(kwargs.get("ignore_enabled_and_cooldown"))
        seen.append(requested)
        if not requested:
            raise ScoutError("disabled")

    monkeypatch.setattr(supervisor, "scout_gate", gate)
    supervise(
        ledger,
        data_dir,
        harness,
        watch=True,
        poll_s=1.0,
        max_parallel=1,
        scout_config=config_for(first, second, enabled=False),
        spawn_scout=harness.spawn_scout,
        scope=ProjectScope.of(),
    )

    assert [row["project"] for row in harness.scout_spawned[:2]] == [str(first), str(second)]
    assert ledger.list_scout_requests() == []
    history = ledger.list_scout_requests(pending_only=False)
    for request in history:
        scout = ledger.get_scout(request["scout_id"])
        assert scout["request_id"] == request["id"]
        assert scout["lens"] == request["lens"]
    assert ledger.get_scout(history[0]["scout_id"])["outcome"] == "failed"
    assert any(seen)


@pytest.mark.parametrize(
    ("message", "wait_reason"),
    [("queue is paused", "queue paused"), ("scout subscription has no headroom", "no headroom")],
)
def test_child_safety_gate_race_keeps_request_pending_without_failed_scout(
    tmp_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    wait_reason: str,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ledger = Ledger(data_dir / "helmraiser.db")
    ledger.request_scouts([str(tmp_project)], requested_at=7.0)
    request = ledger.list_scout_requests()[0]
    config = config_for(tmp_project)
    monkeypatch.setattr(scout_commands, "load_config", lambda: config)
    monkeypatch.setattr(
        scout_commands,
        "scout_gate",
        lambda *args, **kwargs: (_ for _ in ()).throw(ScoutError(message, wait_reason=wait_reason)),
    )
    monkeypatch.setattr(
        scout_commands,
        "get_scout_adapter",
        lambda *args, **kwargs: pytest.fail("adapter created before the safety gate passed"),
    )

    result = runner.invoke(
        app,
        [
            "scout",
            str(tmp_project),
            "--json",
            "--data-dir",
            str(data_dir),
            "--scout-id",
            "assigned",
            "--request-id",
            str(request["id"]),
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.output) == {"error": message, "scout_waiting": True, "lens": "state"}
    pending = ledger.list_scout_requests()[0]
    assert pending["id"] == request["id"]
    assert pending["requested_at"] == 7.0
    assert pending["started_at"] is None and pending["scout_id"] is None

    output = data_dir / "child.jsonl"
    output.write_text(result.output, encoding="utf-8")

    class ExitedChild:
        pid = 1234

        def poll(self) -> int:
            return 1

    harness = Harness(ledger)
    instance = supervisor.Supervisor(
        ledger=ledger,
        data_dir=data_dir,
        scope=ProjectScope.of(),
        max_parallel=1,
        watch=True,
        max_runs=None,
        poll_s=1,
        stop_file=data_dir / "stop",
        spawn=harness.spawn,
        spawn_scout=harness.spawn_scout,
        scout_config=config,
        skip=lambda row: None,
        notify=lambda row: None,
        as_json=True,
        sleep=harness.sleep,
        clock=harness.clock,
    )
    child = supervisor.Child(
        str(tmp_project),
        ExitedChild(),
        output,
        8.0,
        kind="scout",
        scout_id="assigned",
        request_id=request["id"],
    )
    instance._finish_scout(child, 1)
    assert ledger.list_scout_requests()[0]["id"] == request["id"]
    assert ledger.list_scouts() == []
    assert instance.errors == {}


@pytest.mark.parametrize("worker_dies", [False, True])
def test_spawn_failure_or_worker_death_preserves_request(tmp_path: Path, worker_dies: bool) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ledger = Ledger(data_dir / "helmraiser.db")
    project = str(tmp_path / "repo")
    ledger.request_scouts([project], requested_at=7.0)
    request = ledger.list_scout_requests()[0]
    harness = Harness(ledger)

    def interrupted_spawn(*args):
        # SystemExit bypasses the supervisor's exception cleanup, like worker
        # death in the old claim-to-spawn window (before a child exists).
        if worker_dies:
            raise SystemExit("worker died before spawning")
        raise OSError("spawn failed")

    instance = supervisor.Supervisor(
        ledger=ledger,
        data_dir=data_dir,
        scope=ProjectScope.of(),
        max_parallel=1,
        watch=True,
        max_runs=None,
        poll_s=1,
        stop_file=data_dir / "stop",
        spawn=harness.spawn,
        spawn_scout=interrupted_spawn,
        scout_config=config_for(Path(project)),
        skip=lambda row: None,
        notify=lambda row: None,
        as_json=True,
        sleep=harness.sleep,
        clock=harness.clock,
        wall_clock=lambda: 50.0,
    )
    if worker_dies:
        with pytest.raises(SystemExit, match="worker died"):
            instance._start_scout(project, request["id"])
    else:
        assert not instance._start_scout(project, request["id"])
    ledger.close()
    recovered = Ledger(data_dir / "helmraiser.db")
    assert recovered.list_scout_requests() == [request]
    assert recovered.list_scouts() == []
    # Restart can dispatch the original request, with its original FIFO timestamp.
    instance.ledger = recovered
    instance.spawn_scout = Harness(recovered).spawn_scout
    assert instance._start_scout(project, request["id"])
    assert recovered.list_scout_requests() == []
    started = recovered.get_scout_request(request["id"])
    assert recovered.get_scout(started["scout_id"])["request_id"] == request["id"]


def test_child_output_directory_failure_keeps_request_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ledger = Ledger(data_dir / "helmraiser.db")
    project = str(tmp_path / "repo")
    ledger.request_scouts([project], requested_at=9.0)
    request = ledger.list_scout_requests()[0]
    harness = Harness(ledger)
    spawned = False

    def spawn(*args):
        nonlocal spawned
        spawned = True
        return harness.spawn_scout(*args)

    instance = supervisor.Supervisor(
        ledger=ledger,
        data_dir=data_dir,
        scope=ProjectScope.of(),
        max_parallel=1,
        watch=True,
        max_runs=None,
        poll_s=1,
        stop_file=data_dir / "stop",
        spawn=harness.spawn,
        spawn_scout=spawn,
        scout_config=config_for(Path(project)),
        skip=lambda row: None,
        notify=lambda row: None,
        as_json=True,
        sleep=harness.sleep,
        clock=harness.clock,
        wall_clock=lambda: 50.0,
    )
    original_mkdir = Path.mkdir

    def fail_worker_children(path: Path, *args, **kwargs):
        if path.name == "worker-children":
            raise OSError("read-only child directory")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_worker_children)
    assert not instance._start_scout(project, request["id"])
    pending = ledger.list_scout_requests()[0]
    assert pending["requested_at"] == 9.0
    assert pending["started_at"] is None and pending["scout_id"] is None
    assert not spawned


@pytest.mark.parametrize("cancelled", [False, True])
def test_late_child_cannot_run_consumed_or_cancelled_request(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    data_dir = tmp_path / "data"
    ledger = Ledger(data_dir / "helmraiser.db")
    ledger.request_scouts([tmp_project], requested_at=7.0)
    request = ledger.list_scout_requests()[0]
    if cancelled:
        ledger.cancel_scout_requests([tmp_project])
    else:
        ledger.create_scout(
            {
                "id": "winner",
                "project": str(tmp_project),
                "model": "p/m",
                "request_id": request["id"],
                "report_path": str(data_dir / "report"),
            }
        )
    config = config_for(tmp_project)
    monkeypatch.setattr(scout_commands, "load_config", lambda: config)
    monkeypatch.setattr(scout_commands, "scout_gate", lambda *args, **kwargs: None)

    class NoModel:
        def run(self, *args, **kwargs):
            pytest.fail("a late child must not call the model")

    monkeypatch.setattr(scout_commands, "get_scout_adapter", lambda *args: NoModel())
    monkeypatch.setattr(scout_commands, "_intake_callback", lambda *args: None)
    result = runner.invoke(
        app,
        [
            "scout",
            str(tmp_project),
            "--json",
            "--data-dir",
            str(data_dir),
            "--scout-id",
            "late-child",
            "--request-id",
            str(request["id"]),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 1, result.output
    assert json.loads(result.output) == {
        "error": "scout request is no longer pending",
        "scout_waiting": True,
        "lens": "state",
    }
    assert ledger.list_scout_requests() == []
    assert ledger.get_scout("late-child") is None
    assert len(ledger.list_scouts()) == (0 if cancelled else 1)


def test_child_failure_before_scout_record_consumes_request_with_recent_failure(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    ledger = Ledger(data_dir / "helmraiser.db")
    project = str(tmp_path / "repo")
    ledger.request_scouts([project], requested_at=7.0, lens="docs")
    request = ledger.list_scout_requests()[0]
    harness = Harness(ledger)
    config = config_for(Path(project))
    instance = supervisor.Supervisor(
        ledger=ledger,
        data_dir=data_dir,
        scope=ProjectScope.of(),
        max_parallel=1,
        watch=True,
        max_runs=None,
        poll_s=1,
        stop_file=data_dir / "stop",
        spawn=harness.spawn,
        scout_config=config,
        skip=lambda row: None,
        notify=lambda row: None,
        as_json=True,
        wall_clock=lambda: 50.0,
    )

    class FailedChild:
        pid = 1234

        def poll(self) -> int:
            return 1

    output = data_dir / "child.jsonl"
    output.write_text(json.dumps({"error": "adapter unavailable"}) + "\n")
    child = supervisor.Child(
        project,
        FailedChild(),
        output,
        40.0,
        kind="scout",
        scout_id="failed-start",
        lens="docs",
        request_id=request["id"],
    )
    instance._finish_scout(child, 1)
    assert ledger.list_scout_requests() == []
    assert ledger.get_scout_request(request["id"])["scout_id"] == "failed-start"
    scout = ledger.get_scout("failed-start")
    assert scout["request_id"] == request["id"]
    assert scout["outcome"] == "failed"
    assert scout["lens"] == "docs"
    recent = build_scout_status(ledger, config, 50.0, {})["recent"]
    assert len(recent) == 1
    assert recent[0]["ok"] is False
    assert recent[0]["lens"] == "docs"
    assert "Lens: docs" in Path(recent[0]["report"]).read_text()
    assert recent[0]["error"] == "adapter unavailable"
    assert "adapter unavailable" in Path(recent[0]["report"]).read_text()


def test_scout_status_running_recent_shape_and_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    ledger = Ledger(tmp_path / "db")
    now = 200_000.0
    report = tmp_path / "runs" / "new" / "scout.md"
    for scout_id, started, ended, outcome, error, drafts in (
        ("running", now - 5, None, "running", None, []),
        ("new", now - 20, now - 10, "failed", "model failed", ["a", "b"]),
        ("success", now - 40, now - 30, "succeeded", None, ["a"]),
        ("old", now - 90_000, now - 86_401, "succeeded", None, []),
    ):
        ledger.create_scout(
            {
                "id": scout_id,
                "project": str(project),
                "model": "p/m",
                "started_at": started,
                "report_path": str(report),
            }
        )
        if ended is not None:
            ledger.finish_scout(
                scout_id,
                outcome=outcome,
                cost_usd=0,
                tokens_in=0,
                tokens_out=0,
                drafts_written=drafts,
                ended_at=ended,
                error=error,
            )
    monkeypatch.setattr(scout_module, "scout_gate", lambda *args, **kwargs: None)
    config = config_for(project)
    config["routing"]["scout"]["paused_projects"] = [str(project)]
    payload = build_scout_status(ledger, config, now, {})
    assert payload["focus_projects"] == [str(project.resolve())]
    assert payload["paused_projects"] == [str(project.resolve())]
    assert payload["running"][0]["scout_id"] == "running"
    assert [row["scout_id"] for row in payload["recent"]] == ["new", "success"]
    assert payload["recent"][0] == {
        "project": str(project.resolve()),
        "scout_id": "new",
        "lens": "state",
        "ended_at": "1970-01-03T07:33:10+00:00",
        "ok": False,
        "drafts": 2,
        "report": str(report.resolve()),
        "error": "model failed",
    }


def test_status_human_lines_and_help(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    project = tmp_path / "repo"
    project.mkdir()
    ledger = Ledger(data_dir / "helmraiser.db")
    ledger.request_scouts([str(project)], requested_at=10.0)
    ledger.create_scout(
        {
            "id": "12345678-rest",
            "project": str(tmp_path / "other"),
            "model": "p/m",
            "started_at": 11.0,
            "report_path": str(tmp_path / "report"),
        }
    )
    config = config_for(project)
    monkeypatch.setattr(monitor, "load_config", lambda: config)
    monkeypatch.setattr(scout_module, "scout_gate", lambda *args, **kwargs: None)
    monkeypatch.setenv("COLUMNS", "300")

    result = runner.invoke(app, ["status", "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.output
    assert f"Scout pending: {project.resolve()} lens=state (ready)" in result.output
    assert f"Scout running: {(tmp_path / 'other').resolve()} 12345678" in result.output
    assert runner.invoke(app, ["drafts", "request", "--help"]).exit_code == 0
    help_text = runner.invoke(app, ["scout", "--help"]).output
    assert "queue pause" in help_text


@pytest.mark.parametrize("command", [["scout"], ["drafts", "request"]])
@pytest.mark.parametrize("json_mode", [False, True])
def test_unknown_lens(command, json_mode, tmp_project):
    result = runner.invoke(
        app, [*command, str(tmp_project), "--lens", "bogus", *(["--json"] if json_mode else [])]
    )
    assert result.exit_code == 2
    assert "state, docs, tests" in result.output


def test_request_lenses_and_cancellation(tmp_project, tmp_path, monkeypatch):
    monkeypatch.setattr(scout_commands, "load_config", lambda: config_for(tmp_project))
    for lens in ["state", "docs", "tests"]:
        result = invoke_request(tmp_path, str(tmp_project), "--lens", lens, "--json")
        assert result.exit_code == 0
        assert json.loads(result.output)["requested"] == [str(tmp_project)]
    result = invoke_request(tmp_path, str(tmp_project), "--lens", "docs")
    assert result.exit_code == 0
    assert "already pending" in result.output
    assert invoke_request(tmp_path, str(tmp_project), "--cancel", "--lens", "state").exit_code == 0
    ledger = Ledger(tmp_path / "helmraiser.db")
    assert [r["lens"] for r in ledger.list_scout_requests()] == ["docs", "tests"]
    assert invoke_request(tmp_path, str(tmp_project), "--cancel").exit_code == 0
    assert ledger.list_scout_requests() == []


@pytest.mark.parametrize("lens", ["docs", "tests"])
def test_child_resolves_requested_lens(tmp_project, tmp_path, monkeypatch, lens):
    from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

    config = config_for(tmp_project)
    monkeypatch.setattr(scout_commands, "load_config", lambda: config)
    monkeypatch.setattr(scout_module, "_logbook_brief", lambda repo: None)
    adapter = ScriptedPiAdapter({"scout": PiScript(final_text="[]")})
    monkeypatch.setattr(scout_commands, "get_scout_adapter", lambda *args: adapter)
    ledger = Ledger(tmp_path / "helmraiser.db")
    ledger.request_scouts([tmp_project], lens=lens)
    request = ledger.list_scout_requests()[0]
    result = runner.invoke(
        app,
        [
            "scout",
            str(tmp_project),
            "--request-id",
            str(request["id"]),
            "--data-dir",
            str(tmp_path),
            "--force",
            "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert json.loads(result.output)["lens"] == lens
    assert f"## Lens: {lens}" in adapter.calls[0].system_prompt_path.read_text()
    assert ledger.get_scout_request(request["id"])["started_at"] is not None


@pytest.mark.parametrize("lens", [None, "docs"])
def test_child_only_running_lens_and_legacy_fallback(tmp_path, lens):
    ledger = Ledger(tmp_path / "ledger.db")
    child = {"kind": "scout", "scout_id": "child", "project": "/one", "started_at": 10}
    if lens is not None:
        child["lens"] = lens
    payload = build_scout_status(ledger, config_for(), 20, {"children": [child]})
    assert payload["running"] == [
        {
            "scout_id": "child",
            "project": "/one",
            "started_at": "1970-01-01T00:00:10+00:00",
            "lens": lens or "state",
        },
    ]


def test_same_project_lens_requests_never_overlap(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    ledger = Ledger(data_dir / "helmraiser.db")
    ledger.request_scouts([project], lens="docs", requested_at=1)
    ledger.request_scouts([project], lens="tests", requested_at=2)
    harness = Harness(ledger)
    harness.hooks[20] = lambda: (data_dir / "worker.stop").write_text("stop\n")
    monkeypatch.setattr(supervisor, "scout_gate", lambda *args, **kwargs: None)
    supervise(
        ledger,
        data_dir,
        harness,
        watch=True,
        poll_s=1,
        max_parallel=2,
        scout_config=config_for(project),
        spawn_scout=harness.spawn_scout,
        scope=ProjectScope.of(),
    )
    # Harness.spawn_scout itself asserts there is no live child for the project.
    assert {r["lens"] for r in ledger.list_scouts()} == {"docs", "tests", "state"}
    for request in ledger.list_scout_requests(pending_only=False):
        assert ledger.get_scout(request["scout_id"])["lens"] == request["lens"]
    assert all(r["lens"] == "state" for r in ledger.list_scouts() if r["request_id"] is None)
    assert ledger.list_scout_requests() == []


@pytest.mark.parametrize("lens", [None, "docs", "tests"])
def test_ordinary_scout_gate_error_includes_lens(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lens: str | None
) -> None:
    monkeypatch.setattr(scout_commands, "load_config", lambda: config_for(tmp_project))

    def reject_gate(*args, **kwargs):
        raise ScoutError("pending draft cap reached")

    def unexpected_adapter(*args):
        pytest.fail("a rejected scout must not construct a model adapter")

    monkeypatch.setattr(scout_commands, "scout_gate", reject_gate)
    monkeypatch.setattr(scout_commands, "get_scout_adapter", unexpected_adapter)
    args = ["scout", str(tmp_project), "--json", "--data-dir", str(tmp_path)]
    if lens is not None:
        args += ["--lens", lens]
    result = runner.invoke(app, args, catch_exceptions=False)
    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "error": "pending draft cap reached",
        "lens": lens or "state",
    }
    with Ledger(tmp_path / "helmraiser.db") as ledger:
        assert ledger.list_scouts() == []
