"""``helmraiser status`` / ``report`` / ``log`` / ``attach`` (spec §5, §5.1) over a
seeded ledger: no pi, no network."""

from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, monitor
from helmraiser.config import DEFAULT_CONFIG, SUBSCRIPTION_TEMPLATES
from helmraiser.ledger.db import Ledger
from helmraiser.ledger.rundir import RunDir
from helmraiser.loop.report import parse_since
from helmraiser.task.loader import parse_task

runner = CliRunner()

TASK = """---
id: {id}
title: {title}
project: {project}
status: queued
priority: {priority}
policy: review
isolation: none
verify:
  - name: marker
    run: test -f done.txt
---
## Goal

Make the widget work.
"""

REVIEW_MD = """The change is fine but one test is weak.

```json
{"verdict": "concerns",
 "findings": [{"severity": "minor", "file": "tests/test_x.py", "line": 12, "note": "weak assert"}],
 "evidence_checks": []}
```
"""

SUMMARY_MD = """## Fix the widget — awaiting_approval

- task: `x`
- run: `y`
- next: `helmraiser approve y`

### Last verify

- marker: pass in 0.1s

### Review: pass

- no findings
"""


@pytest.fixture(autouse=True)
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(monitor, "load_config", lambda: copy.deepcopy(DEFAULT_CONFIG))
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    return runtime


@pytest.fixture
def subscription_config(cli_env, monkeypatch):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["subscriptions"] = copy.deepcopy(SUBSCRIPTION_TEMPLATES)
    monkeypatch.setattr(monitor, "load_config", lambda: config)
    return config


def test_status_without_subscriptions(cli_env, monkeypatch, data_dir):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["subscriptions"] = {}
    monkeypatch.setattr(monitor, "load_config", lambda: config)
    human = invoke("status", data_dir=data_dir)
    result = invoke("status", "--json", data_dir=data_dir)
    assert human.exit_code == result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["usage"] == {}
    assert payload["routing"]["headroom"] == {}
    assert "opencode-go" not in human.output
    assert "openai-codex" not in human.output
    written = json.loads((cli_env / "helmraiser" / "status.json").read_text())
    assert written["usage"] == {}


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def ledger(data_dir: Path) -> Ledger:
    return Ledger(data_dir / "helmraiser.db")


def invoke(*args: str, data_dir: Path):
    return runner.invoke(app, [*args, "--data-dir", str(data_dir)])


def seed_task(
    ledger: Ledger,
    project: Path,
    task_id: str = "fix-widget",
    title: str = "Fix it",
    priority: str = "normal",
):
    text = TASK.format(id=task_id, title=title, project=project, priority=priority)
    spec = parse_task(text, project / "topics" / f"{task_id}.md", copy.deepcopy(DEFAULT_CONFIG))
    return ledger.upsert_task(spec, text)


def seed_run(
    ledger: Ledger,
    task_id: str,
    run_id: str,
    status: str,
    *,
    started_at: float,
    ended_at: float | None = None,
    cost: float = 0.5,
    model: str = "prov/impl",
    verdict: str | None = None,
    stage: str | None = None,
    worktree: str | None = None,
    failure_reason: str | None = None,
    model_plan: str | None = None,
    model_fallback: str | None = None,
) -> str:
    ledger.create_run(
        {
            "run_id": run_id,
            "task_id": task_id,
            "project": "/repo",
            "status": status,
            "stage": stage,
            "policy": "review",
            "isolation": "none",
            "model_implement": model,
            "model_review": "prov/rev",
            "model_plan": model_plan,
            "model_fallback": model_fallback,
            "pi_version": "0.87.1",
            "cost_usd": cost,
            "verdict": verdict,
            "worktree_path": worktree,
            "started_at": started_at,
            "ended_at": ended_at,
            "failure_reason": failure_reason,
        }
    )
    return run_id


@pytest.mark.parametrize("status", ["resuming", "running", "awaiting_approval", "failed", "landed"])
def test_rework_history_from_ledger_without_summary(ledger, data_dir, tmp_path, status):
    task_id = seed_task(ledger, tmp_path)
    run_id = seed_run(
        ledger, task_id, "reworked", status, started_at=time.time(), ended_at=time.time()
    )
    requests = []
    for n in (1, 2):
        request = {"round": n, "note": f"café {n}\n[bold]literal[/bold] 終わり", "findings": []}
        requests.append(request)
        ledger.append_event(run_id, "helmraiser", "rework_requested", request)
    if status == "awaiting_approval":
        ledger.record_approval(run_id, "merge", "pending")
    result = invoke("status", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["reworks"] == [
        {
            "run_id": run_id,
            "task": task_id,
            "status": status,
            "rework_rounds": 2,
            "reworks": requests,
        }
    ]
    if status in ("resuming", "running"):
        assert payload[status][0]["reworks"] == requests
    if status == "awaiting_approval":
        assert payload["awaiting"][0]["reworks"] == requests
    persisted = json.loads(Path(payload["status_path"]).read_text())
    assert persisted["reworks"] == payload["reworks"]
    human = invoke("status", data_dir=data_dir).output
    for request in requests:
        assert request["note"] in human
    label = "Reworked (24h): " if status in ("failed", "landed") else "Rework: "
    assert f"{label}{run_id} " in human
    if status in ("awaiting_approval", "failed", "landed"):
        # Stale summary headers cannot hide or duplicate ledger history.
        rundir = RunDir(run_id, data_dir)
        rundir.create()
        rundir.summary_md.write_text(SUMMARY_MD.replace("- next:", "- rework rounds: 99\n- next:"))
        result = invoke("report", "--json", data_dir=data_dir)
        report = json.loads(result.output)
        lane = {"awaiting_approval": "awaiting", "failed": "failed", "landed": "landed"}[status]
        assert report[lane][0]["reworks"] == requests
        assert report[lane][0]["rework_rounds"] == 2
        human_report = invoke("report", data_dir=data_dir).output
        assert human_report.count("rework rounds: 2") == 1
        assert "rework rounds: 99" not in human_report
        for request in requests:
            assert request["note"].replace("\n", "\n  ") in human_report


def test_rework_guidance_is_not_offered_for_plan(ledger, data_dir, tmp_path):
    task_id = seed_task(ledger, tmp_path)
    run_id = seed_run(ledger, task_id, "plan", "awaiting_approval", started_at=time.time())
    ledger.record_approval(run_id, "plan", "pending")
    assert "helmraiser rework" not in invoke("status", data_dir=data_dir).output
    assert "helmraiser rework" not in invoke("report", data_dir=data_dir).output


def test_status_preserves_same_project_siblings(ledger, data_dir, tmp_path, monkeypatch):
    from helmraiser.cli import worker_commands
    from helmraiser.task.loader import render_task
    from tests.ledger.test_db import make_spec, run_row

    monkeypatch.setattr(worker_commands, "pid_alive", lambda pid: True)
    monkeypatch.setattr(
        monitor, "load_config", lambda: {**DEFAULT_CONFIG, "max_runs_per_project": 3}
    )
    children = []
    for i, id in enumerate(("a", "b")):
        spec = make_spec(tmp_path, id, paths=(f"{id}/**",), status="running")
        ledger.upsert_task(spec, render_task(spec))
        ledger.create_run(run_row(f"{id * 8}-run", spec.task_id, str(tmp_path)))
        children.append({"pid": 123 + i, "project": str(tmp_path), "run_id": f"{id * 8}-run"})
    spec = make_spec(tmp_path, "held", paths=("b/**",))
    ledger.upsert_task(spec, render_task(spec))
    (data_dir / "worker.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "started_at": "now",
                "max_parallel_runs": 3,
                "argv": [],
                "log": str(data_dir / "worker.log"),
            }
        )
    )
    (data_dir / "worker.active.json").write_text(
        json.dumps({"pid": os.getpid(), "max_parallel_runs": 3, "children": children})
    )
    worker = invoke("worker", "status", "--json", data_dir=data_dir)
    assert worker.exit_code == 0, worker.output
    assert json.loads(worker.output)["children"] == children
    status = invoke("status", "--json", data_dir=data_dir)
    assert status.exit_code == 0, status.output
    payload = json.loads(status.output)
    assert {r["run_id"] for r in payload["running"]} == {c["run_id"] for c in children}
    assert payload["worker"]["children"] == children
    assert payload["queue_next"][0]["held_by"]["reason"] == "paths_overlap"
    text = invoke("worker", "status", data_dir=data_dir)
    assert "run aaaaaaaa" in text.output and "run bbbbbbbb" in text.output


def test_status_resuming_held_by_running_sibling_limit_one(ledger, data_dir, tmp_path, monkeypatch):
    from helmraiser.loop.status import held_task_text
    from helmraiser.task.loader import render_task
    from tests.ledger.test_db import make_spec, run_row

    monkeypatch.setattr(
        monitor, "load_config", lambda: {**DEFAULT_CONFIG, "max_runs_per_project": 1}
    )
    for n, (run_id, status) in enumerate((("back", "resuming"), ("sibling-run", "running"))):
        spec = make_spec(tmp_path, run_id, status=status)
        ledger.upsert_task(spec, render_task(spec))
        ledger.create_run(
            {
                **run_row(run_id, spec.task_id, str(tmp_path), status),
                "started_at": 100.0 + n,
                "stage": "verify",
            }
        )
    result = invoke("status", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    (entry,) = payload["resuming"]
    assert entry == {
        "run_id": "back",
        "task": make_spec(tmp_path, "back").task_id,
        "project": str(tmp_path),
        "id": "back",
        "title": "Task back",
        "stage": "verify",
        "origin": None,
        "held_by": {"run_id": "sibling-run", "status": "running", "pending": None},
    }
    persisted = json.loads(Path(payload["status_path"]).read_text())
    assert persisted["resuming"] == payload["resuming"]
    monkeypatch.setenv("COLUMNS", "1000")
    human = invoke("status", data_dir=data_dir)
    assert human.exit_code == 0, human.output
    line = next(line for line in human.output.splitlines() if line.startswith("Resuming:"))
    assert ", waiting: " + held_task_text(entry["task"], entry["held_by"]) in line
    assert "by run sibling- (running)" in line


def test_status_resuming_held_three_runs_limit_two(ledger, data_dir, tmp_path, monkeypatch):
    from helmraiser.task.loader import render_task
    from tests.ledger.test_db import make_spec, run_row

    config = {**DEFAULT_CONFIG, "max_runs_per_project": 2}
    monkeypatch.setattr(monitor, "load_config", lambda: config)
    for n, run_id in enumerate(("a", "b", "c")):
        spec = make_spec(tmp_path, run_id, paths=(f"{run_id}/**",), status="resuming")
        ledger.upsert_task(spec, render_task(spec))
        ledger.create_run(
            {
                **run_row(run_id, spec.task_id, str(tmp_path), "resuming"),
                "started_at": 100.0 + n,
                "stage": "implement",
            }
        )
    result = invoke("status", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    entries = {entry["run_id"]: entry for entry in payload["resuming"]}
    assert entries["a"]["held_by"] is None
    assert entries["b"]["held_by"] is None
    assert entries["c"]["held_by"] == {
        "run_id": "a",
        "status": "resuming",
        "pending": None,
        "reason": "project_busy",
        "other_task_id": "a",
    }
    assert ledger.next_resumable(config=config)["run_id"] == "a"
    assert ledger.next_resumable(config=config, exclude={"a"})["run_id"] == "b"
    assert ledger.next_resumable(config=config, exclude={"a", "b"}) is None
    persisted = json.loads(Path(payload["status_path"]).read_text())
    assert persisted["resuming"] == payload["resuming"]


@pytest.mark.parametrize("kind", ["merge", "plan", "scope"])
def test_status_resuming_held_pending_approval(ledger, data_dir, tmp_path, monkeypatch, kind):
    from helmraiser.task.loader import render_task
    from tests.ledger.test_db import make_spec, run_row

    monkeypatch.setattr(
        monitor, "load_config", lambda: {**DEFAULT_CONFIG, "max_runs_per_project": 1}
    )
    for n, (run_id, status) in enumerate(
        (("back", "resuming"), ("sibling-run", "awaiting_approval"))
    ):
        spec = make_spec(tmp_path, run_id, status=status)
        ledger.upsert_task(spec, render_task(spec))
        ledger.create_run(
            {
                **run_row(run_id, spec.task_id, str(tmp_path), status),
                "started_at": 100.0 + n,
                "stage": "implement",
            }
        )
    ledger.record_approval("sibling-run", kind, "pending")
    result = invoke("status", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    (entry,) = json.loads(result.output)["resuming"]
    assert entry["held_by"] == {
        "run_id": "sibling-run",
        "status": "awaiting_approval",
        "pending": kind,
    }
    monkeypatch.setenv("COLUMNS", "1000")
    human = invoke("status", data_dir=data_dir)
    assert human.exit_code == 0, human.output
    line = next(line for line in human.output.splitlines() if line.startswith("Resuming:"))
    assert ", waiting: " in line
    assert f"awaiting {kind} approval" in line


def test_status_resuming_held_free_line_unchanged(ledger, data_dir, tmp_path, monkeypatch):
    from helmraiser.task.loader import render_task
    from tests.ledger.test_db import make_spec, run_row

    monkeypatch.setenv("COLUMNS", "1000")
    monkeypatch.setattr(
        monitor, "load_config", lambda: {**DEFAULT_CONFIG, "max_runs_per_project": 1}
    )
    spec = make_spec(tmp_path, "back", status="resuming")
    ledger.upsert_task(spec, render_task(spec))
    ledger.create_run(
        {
            **run_row("back", spec.task_id, str(tmp_path), "resuming"),
            "started_at": 100.0,
            "stage": "verify",
        }
    )
    result = invoke("status", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    (entry,) = json.loads(result.output)["resuming"]
    assert entry["held_by"] is None
    human = invoke("status", data_dir=data_dir)
    assert human.exit_code == 0, human.output
    line = next(line for line in human.output.splitlines() if line.startswith("Resuming:"))
    assert line == f"Resuming: back {spec.task_id} at verify"


def _seed_provider_run(
    ledger: Ledger,
    project: Path,
    task_id: str,
    run_id: str,
    *,
    model: str = "opencode-go/mystery",
    cost: float = 2.4,
) -> str:
    started = time.time() - 60
    ledger.create_run(
        {
            "run_id": run_id,
            "task_id": task_id,
            "project": str(project),
            "status": "landed",
            "policy": "review",
            "isolation": "none",
            "model_implement": model,
            "model_review": model,
            "pi_version": "0.87.1",
            "cost_usd": cost,
            "started_at": started,
            "ended_at": started,
        }
    )
    return run_id


def test_log_scope_decline_and_approval(ledger, data_dir, tmp_path):
    task_id = seed_task(ledger, tmp_path)
    run_id = seed_run(ledger, task_id, "scope-log", "resuming", started_at=time.time())
    granted = {"stage": "implement", "scope": {"paths": ["a.txt"], "note": "ok"}}
    declined = {
        "stage": "implement",
        "scope": {"paths": [], "declined": ["a.txt", "b.md"], "note": "Try another way"},
    }
    for payload in (granted, declined):
        ledger.append_event(run_id, "helmraiser", "handed_back", payload)
    result = invoke("log", run_id, data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert "scope request declined (a.txt, b.md): Try another way" in result.output
    old_summary = 'stage="implement", scope={"paths": ["a.txt"], "note": "ok"}'
    assert monitor.summarize_payload("handed_back", granted) == old_summary
    assert old_summary in result.output
    raw = invoke("log", run_id, "--json", data_dir=data_dir)
    assert [json.loads(line)["payload"] for line in raw.output.splitlines()] == [granted, declined]
    long = {"scope": {**declined["scope"], "note": "x" * monitor.SUMMARY_WIDTH}}
    summary = monitor.summarize_payload("handed_back", long)
    assert summary.startswith("scope request declined (a.txt, b.md): ")
    assert len(summary) == monitor.SUMMARY_WIDTH and summary.endswith("…")


class TestStatus:
    def test_writes_status_json_atomically_with_spec_shape(
        self, ledger: Ledger, data_dir: Path, tmp_project: Path, cli_env: Path
    ):
        task_id = seed_task(ledger, tmp_project)
        ledger.set_task_status(task_id, "queued")
        now = time.time()
        seed_run(
            ledger, task_id, "r-run", "running", started_at=now - 420, cost=0.41, stage="implement"
        )
        ledger.start_iteration("r-run", 1, "r-run-implement", "prov/impl")
        ledger.start_iteration("r-run", 2, "r-run-implement", "prov/impl")
        seed_run(
            ledger, task_id, "r-wait", "awaiting_approval", started_at=now - 900, verdict="pass"
        )
        seed_run(
            ledger, task_id, "r-back", "resuming", started_at=now - 1200, cost=0, stage="verify"
        )
        seed_run(
            ledger,
            task_id,
            "r-fail",
            "failed",
            started_at=now - 3600,
            ended_at=now - 3000,
            failure_reason="verify_exhausted",
        )
        ledger.pause_queue("maintenance", now + 600)
        ledger.hold_provider("opencode-go", now + 300, "429 quota")
        for task, project, status in (
            ("draft-a", tmp_project, "draft"),
            ("draft-b", tmp_project, "draft"),
            ("draft-c", Path("/other"), "draft"),
            ("dropped", tmp_project, "dropped"),
        ):
            key = seed_task(ledger, project, task_id=task, title=task)
            ledger.set_task_status(key, status)

        result = invoke("status", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        path = cli_env / "helmraiser" / "status.json"
        assert out["status_path"] == str(path)
        written = json.loads(path.read_text())
        assert set(written) == {
            "updated_at",
            "queued_count",
            "drafts",
            "queue_next",
            "held",
            "never_startable",
            "running",
            "resuming",
            "reworks",
            "awaiting_count",
            "failed_24h_count",
            "spend",
            "usage",
            "routing",
            "scouts",
            "caps_enforced",
            "paused",
            "held_providers",
            "worker",
        }
        assert written["queued_count"] == 1 and written["awaiting_count"] == 1
        assert written["drafts"] == {"/other": 1, str(tmp_project): 2}
        assert out["queue"] == {"queued": 1}
        assert written["queue_next"] == [
            {
                "task_id": task_id,
                "id": "fix-widget",
                "title": "Fix it",
                "priority": "normal",
                "project": str(tmp_project),
                "held_by": None,
                "origin": None,
            }
        ]
        assert written["held"] == []
        assert written["failed_24h_count"] == 1 and written["spend"]["prov"]["5h"] == 1.41
        assert written["scouts"] == {
            "focus_projects": [],
            "paused_projects": [],
            "pending": [],
            "running": [],
            "recent": [],
        }
        assert written["caps_enforced"] is True
        assert written["paused"]["reason"] == "maintenance"
        assert written["held_providers"] == {
            "opencode-go": {"until": now + 300, "detail": "429 quota"}
        }
        (running,) = written["running"]
        assert running == {
            "run_id": "r-run",
            "task": task_id,
            "project": "/repo",
            "id": "fix-widget",
            "title": "Fix it",
            "stage": "implement",
            "iteration": 2,
            "cost_usd": 0.41,
            "minutes": 7,
            "origin": None,
        }
        assert written["resuming"] == [
            {
                "run_id": "r-back",
                "task": task_id,
                "project": "/repo",
                "id": "fix-widget",
                "title": "Fix it",
                "stage": "verify",
                "origin": None,
                "held_by": {
                    "run_id": "r-wait",
                    "status": "awaiting_approval",
                    "pending": "merge",
                },
            }
        ]
        # No temp file left behind by the atomic write.
        assert sorted(p.name for p in path.parent.iterdir()) == ["status.json"]
        # The command's own JSON is a superset of the file.
        assert {k: out[k] for k in written} == written
        assert out["queue"] == {"queued": 1}
        assert out["awaiting"] == [
            {
                "run_id": "r-wait",
                "task": task_id,
                "project": "/repo",
                "id": "fix-widget",
                "title": "Fix it",
                "pending": "merge",
                "verdict": "pass",
                "cost_usd": 0.5,
                "worktree": None,
                "branch": None,
                "base_commit": None,
                "origin": None,
            }
        ]
        (failed_row,) = out["failed"]
        assert failed_row["run_id"] == "r-fail"
        assert failed_row["project"] == "/repo"
        assert failed_row["id"] == "fix-widget"
        assert failed_row["title"] == "Fix it"
        assert failed_row["reason"] == "verify_exhausted"
        assert failed_row["task"] == task_id
        assert failed_row["ended_at"] is not None
        assert isinstance(out["awaiting"], list)
        assert isinstance(out["failed"], list)
        assert isinstance(out["landed"], list)
        assert out["awaiting_count"] == len(out["awaiting"])
        assert out["failed_24h_count"] == len(out["failed"])

    def test_running_row_identity(
        self, ledger: Ledger, data_dir: Path, tmp_project: Path, cli_env: Path
    ):
        """Running rows name the project, short id and title from the ledger."""
        task_id = seed_task(ledger, tmp_project, title="Fix the widget")
        seed_run(ledger, task_id, "r-run", "running", started_at=time.time(), stage="implement")

        out = json.loads(invoke("status", "--json", data_dir=data_dir).output)

        (running,) = out["running"]
        assert running["project"] == "/repo"
        assert running["id"] == "fix-widget"
        assert running["title"] == "Fix the widget"

    def test_running_row_without_task_row(
        self, ledger: Ledger, data_dir: Path, tmp_project: Path, cli_env: Path
    ):
        """A run whose task row is gone still reports, with ``None`` id/title."""
        task_id = seed_task(ledger, tmp_project)
        seed_run(ledger, task_id, "r-run", "running", started_at=time.time(), stage="implement")
        conn = ledger._get_conn()
        conn.execute("PRAGMA foreign_keys=OFF")  # simulate a run orphaned from its task
        conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()

        out = json.loads(invoke("status", "--json", data_dir=data_dir).output)

        (running,) = out["running"]
        assert running["id"] is None
        assert running["title"] is None
        assert running["project"] == "/repo"

    def test_queue_next_is_limited_and_in_dispatch_order(
        self, ledger: Ledger, data_dir: Path, tmp_project: Path
    ) -> None:
        for task_id, priority in (
            ("low", "low"),
            ("normal-first", "normal"),
            ("normal-second", "normal"),
            ("high-first", "high"),
            ("high-second", "high"),
            ("high-third", "high"),
            ("low-last", "low"),
        ):
            seed_task(ledger, tmp_project, task_id=task_id, priority=priority)
            time.sleep(0.001)

        out = json.loads(invoke("status", "--json", data_dir=data_dir).output)

        assert [(row["id"], row["priority"]) for row in out["queue_next"]] == [
            ("high-first", "high"),
            ("high-second", "high"),
            ("high-third", "high"),
            ("normal-first", "normal"),
            ("normal-second", "normal"),
        ]

    def test_tasks_held_by_a_parked_run_are_marked(
        self, ledger: Ledger, data_dir: Path, tmp_project: Path, cli_env: Path
    ):
        parked = seed_task(ledger, tmp_project, task_id="parked")
        ledger.set_task_status(parked, "awaiting_approval")
        now = time.time()
        seed_run(ledger, parked, "r-wait", "awaiting_approval", started_at=now - 60)
        ledger.update_run("r-wait", project=str(tmp_project))
        held = seed_task(ledger, tmp_project, task_id="later", title="Later")
        ledger.set_task_status(held, "queued")
        other = tmp_project.parent / "other"
        free = seed_task(ledger, other, task_id="free", priority="low")
        ledger.set_task_status(free, "queued")

        result = invoke("status", "--json", data_dir=data_dir)

        out = json.loads(result.output)
        wait = {"run_id": "r-wait", "status": "awaiting_approval", "pending": "merge"}
        assert [(row["id"], row["held_by"]) for row in out["queue_next"]] == [
            ("later", wait),
            ("free", None),
        ]
        assert out["held"] == [
            {
                "task_id": held,
                "id": "later",
                "title": "Later",
                "project": str(tmp_project),
                "origin": None,
                **wait,
            }
        ]
        written = json.loads((cli_env / "helmraiser" / "status.json").read_text())
        assert written["held"] == out["held"]
        human = "".join(invoke("status", data_dir=data_dir).output.split())
        assert f"Held:{held}byrunr-wait(awaitingmergeapproval)" in human

    def test_status_json_includes_subscription_usage(
        self, ledger, data_dir, tmp_project, cli_env, subscription_config
    ):
        task_id = seed_task(ledger, tmp_project)
        _seed_provider_run(ledger, tmp_project, task_id, "r-go", cost=2.4)

        result = invoke("status", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        windows = out["usage"]["opencode-go"]
        assert set(windows) == {"5h", "week", "month"}
        # Unknown model: default $15 monthly, so $2.40 is 80% of the $3 window.
        assert windows["5h"]["fraction"] == pytest.approx(0.8)
        assert windows["week"]["fraction"] == pytest.approx(0.32)

        text = invoke("status", data_dir=data_dir).output
        assert "opencode-go 5h usage 80% (warn at 80%), resets " in text

    def test_status_shows_live_routing_headroom(
        self, monkeypatch, ledger: Ledger, data_dir: Path, cli_env: Path, subscription_config
    ):
        from helmraiser.loop import status as status_module

        week = 7 * 86400
        start = 1_791_158_400.0
        now = start + 0.46 * week
        live = {"openai-codex": {"week": {"fraction": 0.28, "resets_at": start + week}}}
        monkeypatch.setenv("COLUMNS", "160")
        monkeypatch.setattr(monitor.time, "time", lambda: now)
        monkeypatch.setattr(status_module, "live_for", lambda *args, **kwargs: live)

        json_result = invoke("status", "--json", data_dir=data_dir)

        assert json_result.exit_code == 0, json_result.output
        payload = json.loads(json_result.output)
        assert payload["usage"]["openai-codex"]["week"]["fraction"] == 0.28
        routing = payload["routing"]["headroom"]["openai-codex"]
        assert routing["has_headroom"] is True
        assert routing["week"] == {
            "used": 0.28,
            "elapsed": 0.46,
            "target": 0.95,
            "headroom": 0.157,
        }
        written = json.loads((cli_env / "helmraiser" / "status.json").read_text())
        assert written["routing"] == payload["routing"]

        human = invoke("status", data_dir=data_dir)
        assert human.exit_code == 0, human.output
        assert "codex wk 28% · 46% elapsed · headroom +16 pts" in human.output

    def test_failed_24h_counts_by_end_time(self, ledger: Ledger, data_dir: Path, tmp_project: Path):
        """Regression (dogfood review finding): a run that started 30 h ago but
        failed 1 h ago is a failure of the last 24 h; one that failed 25 h ago is not."""
        task_id = seed_task(ledger, tmp_project)
        now = time.time()
        seed_run(
            ledger,
            task_id,
            "r-recent",
            "failed",
            started_at=now - 30 * 3600,
            ended_at=now - 3600,
            failure_reason="timeout",
        )
        seed_run(
            ledger,
            task_id,
            "r-old",
            "failed",
            started_at=now - 26 * 3600,
            ended_at=now - 25 * 3600,
            failure_reason="timeout",
        )
        out = json.loads(invoke("status", "--json", data_dir=data_dir).output)
        assert out["failed_24h_count"] == 1
        assert [r["run_id"] for r in out["failed"]] == ["r-recent"]

    def test_landed_lists_runs_that_ended_in_the_last_24h(
        self, ledger: Ledger, data_dir: Path, tmp_project: Path
    ):
        """A notifier uses landings, so status names them like failures."""
        task_id = seed_task(ledger, tmp_project)
        now = time.time()
        seed_run(ledger, task_id, "l-recent", "landed", started_at=now - 7200, ended_at=now - 3600)
        seed_run(
            ledger, task_id, "l-old", "landed", started_at=now - 26 * 3600, ended_at=now - 25 * 3600
        )
        seed_run(ledger, task_id, "f-recent", "failed", started_at=now - 7200, ended_at=now - 600)
        out = json.loads(invoke("status", "--json", data_dir=data_dir).output)
        (landed,) = out["landed"]
        assert landed["run_id"] == "l-recent"
        assert landed["task"] == task_id and landed["ended_at"]
        assert "reason" not in landed
        assert [r["run_id"] for r in out["failed"]] == ["f-recent"]

    def test_failed_rows_carry_the_last_verify_hints(
        self, ledger: Ledger, data_dir: Path, tmp_project: Path
    ):
        task_id = seed_task(ledger, tmp_project)
        now = time.time()
        seed_run(
            ledger,
            task_id,
            "r-fail",
            "failed",
            started_at=now - 60,
            ended_at=now,
            failure_reason="blocked: no database",
        )
        hint = "nothing listens on 127.0.0.1:55440: no [[services]] entry …"
        for n, hints in ((1, ["an older hint"]), (2, [hint])):
            ledger.start_iteration("r-fail", n, "s", "prov/impl")
            ledger.finish_iteration(
                "r-fail",
                n,
                agent_ok=True,
                commit_sha="c",
                verify_passed=False,
                verify_results=[
                    {"name": "lint", "passed": True, "exit_code": 0, "seconds": 1, "tail": ""},
                    {"name": "test", "passed": False, "exit_code": 1, "seconds": 1, "tail": "x"},
                    {
                        "name": "e2e",
                        "passed": False,
                        "exit_code": 1,
                        "seconds": 1,
                        "tail": "y",
                        "hints": hints,
                    },
                ],
            )
        seed_run(ledger, task_id, "r-old", "failed", started_at=now - 90, ended_at=now - 30)

        result = invoke("status", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        rows = {row["run_id"]: row for row in json.loads(result.output)["failed"]}
        assert rows["r-fail"]["hints"] == [hint]
        assert rows["r-old"]["hints"] == []  # no iterations, or none with hints

    def test_human_output(
        self, ledger: Ledger, data_dir: Path, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Wide enough that a long tmp_path never wraps the "Drafts:" line.
        monkeypatch.setenv("COLUMNS", "1000")
        task_id = seed_task(ledger, tmp_project)
        seed_run(ledger, task_id, "r-wait", "awaiting_approval", started_at=time.time())
        draft = seed_task(ledger, tmp_project, task_id="draft", title="Draft")
        ledger.set_task_status(draft, "draft")
        dropped = seed_task(ledger, tmp_project, task_id="dropped", title="Dropped")
        ledger.set_task_status(dropped, "dropped")
        result = invoke("status", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert "Running: none" in result.output
        assert f"Drafts: {tmp_project} 1" in result.output
        assert "Awaiting your approval" in result.output and "r-wait" in result.output
        assert "helmraiser approve" in result.output


class TestReport:
    def test_sections_from_seeded_ledger(self, ledger: Ledger, data_dir: Path, tmp_project: Path):
        task_id = seed_task(ledger, tmp_project, title="Fix the widget")
        now = time.time()
        seed_run(
            ledger,
            task_id,
            "r-landed",
            "landed",
            started_at=now - 7200,
            ended_at=now - 3600,
            cost=0.30,
            verdict="pass",
        )
        ledger.start_iteration("r-landed", 1, "r-landed-implement", "prov/impl")
        ledger.finish_iteration(
            "r-landed",
            1,
            agent_ok=True,
            commit_sha="abc",
            verify_passed=True,
            verify_results=[],
            cost_usd=0.25,
        )
        RunDir("r-landed", data_dir).create()
        RunDir("r-landed", data_dir).summary_md.write_text(SUMMARY_MD)

        seed_run(
            ledger,
            task_id,
            "r-wait",
            "awaiting_approval",
            started_at=now - 600,
            cost=0.2,
            verdict="concerns",
        )
        RunDir("r-wait", data_dir).create()
        RunDir("r-wait", data_dir).review_md.write_text(REVIEW_MD)
        ledger.record_approval("r-wait", "plan", "pending")

        seed_run(
            ledger,
            task_id,
            "r-fail",
            "failed",
            started_at=now - 1800,
            ended_at=now - 1200,
            cost=0.1,
            failure_reason="verify_exhausted",
        )
        ledger.start_iteration("r-fail", 1, "r-fail-implement", "prov/impl")
        ledger.finish_iteration(
            "r-fail",
            1,
            agent_ok=True,
            commit_sha="def",
            verify_passed=False,
            verify_results=[
                {
                    "name": "marker",
                    "passed": False,
                    "exit_code": 1,
                    "seconds": 0.1,
                    "tail": "\n".join(f"line{i}" for i in range(20)),
                },
                {"name": "ok", "passed": True, "exit_code": 0, "seconds": 0.1, "tail": ""},
            ],
            cost_usd=0.1,
        )
        # Outside the window: ignored entirely.
        seed_run(
            ledger,
            task_id,
            "r-old",
            "landed",
            started_at=now - 10 * 86400,
            ended_at=now - 9 * 86400,
            cost=9.0,
        )

        result = invoke("report", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["since"] == "24h"
        assert [e["run_id"] for e in data["landed"]] == ["r-landed"]
        landed = data["landed"][0]
        assert landed["iterations"] == 1 and landed["verdict"] == "pass"
        assert landed["fragment"].startswith("### Last verify")
        (waiting,) = data["awaiting"]
        assert waiting["pending"] == "plan" and waiting["fragment"] is None
        assert waiting["plan_path"] == str(data_dir / "runs" / "r-wait" / "plan.md")
        assert waiting["findings"] == [
            {"severity": "minor", "file": "tests/test_x.py", "line": 12, "note": "weak assert"}
        ]
        (failed,) = data["failed"]
        assert failed["failure_reason"] == "verify_exhausted"
        (tail,) = failed["verify_tails"]
        assert tail["name"] == "marker" and tail["tail"].splitlines() == [
            f"line{i}" for i in range(10, 20)
        ]
        assert data["cost"]["by_model"] == {"prov/impl": 0.35, "prov/rev": 0.25}
        assert data["cost"]["total_usd"] == 0.6

        text = invoke("report", data_dir=data_dir).output
        assert text.index("# Landed (1)") < text.index("# Awaiting your approval (1)")
        assert text.index("# Awaiting your approval (1)") < text.index("# Failed (1)")
        assert text.index("# Failed (1)") < text.index("# Cost")
        assert "## Fix the widget — landed" in text and "- marker: pass in 0.1s" in text
        assert f"- plan: `{data_dir / 'runs' / 'r-wait' / 'plan.md'}`" in text
        assert "`helmraiser approve r-wait` or `helmraiser reject r-wait` — the plan" in text
        assert "- [minor] tests/test_x.py:12 — weak assert" in text
        assert "- reason: verify_exhausted" in text and "line19" in text and "line5" not in text
        assert "| prov/impl | 0.3500 |" in text
        assert "r-old" not in text

    def test_report_models_markdown_and_json(self, ledger, data_dir, tmp_project):
        task_id = seed_task(ledger, tmp_project)
        now = time.time()
        seed_run(
            ledger,
            task_id,
            "r-go",
            "landed",
            started_at=now - 600,
            ended_at=now - 300,
            cost=3.0,
            model="opencode-go/glm-5.3-flash:medium",
        )
        ledger.start_iteration("r-go", 1, "r-go-implement", "opencode-go/glm-5.3-flash:medium")
        ledger.finish_iteration(
            "r-go",
            1,
            agent_ok=True,
            commit_sha="abc",
            verify_passed=True,
            verify_results=[],
            cost_usd=3.0,
        )

        text = invoke("report", "--since", "7d", "--models", data_dir=data_dir).output
        assert "# Models" in text
        assert (
            "| model | runs | planned | landed | awaiting | failed | rejected | first_pass | "
            "escalated | "
            "escalation_reasons | review_rounds | severe_first_review | avg_iterations | "
            "avg_minutes | cost_usd | plan_cost_usd | cost_per_run | cost_per_landed | "
            "tokens_per_run | "
            "opencode_go_share_per_landed |" in text
        )
        assert "| opencode-go/glm-5.3-flash:medium | 1 |" in text
        assert "| setup |" not in text

        data = json.loads(
            invoke("report", "--since", "7d", "--models", "--json", data_dir=data_dir).output
        )
        (model,) = data["models"]
        assert model["model"] == "opencode-go/glm-5.3-flash:medium"
        assert model["landed"] == 1 and model["first_pass"] == 1
        assert "setup" not in model

        # Without --models the report is unchanged.
        plain = invoke("report", "--since", "7d", data_dir=data_dir).output
        assert "# Models" not in plain

    def _seed_setup_pair(self, ledger, tmp_project, now: float) -> None:
        task_id = seed_task(ledger, tmp_project)
        seed_run(
            ledger,
            task_id,
            "r-plan",
            "landed",
            started_at=now - 600,
            ended_at=now - 300,
            model="prov/impl",
            model_fallback="prov/fb",
            model_plan="prov/planner",
        )
        seed_run(
            ledger,
            task_id,
            "r-noplan",
            "landed",
            started_at=now - 500,
            ended_at=now - 200,
            model="prov/impl",
            model_fallback="prov/fb",
        )

    def test_report_models_by_setup_markdown(self, ledger, data_dir, tmp_project):
        self._seed_setup_pair(ledger, tmp_project, time.time())

        result = invoke("report", "--since", "7d", "--models", "--by", "setup", data_dir=data_dir)

        assert result.exit_code == 0
        text = result.output
        assert "# Models" in text
        assert "| setup | runs |" in text
        assert "plan=none impl=prov/impl fb=prov/fb rev=prov/rev" in text
        assert "plan=prov/planner impl=prov/impl fb=prov/fb rev=prov/rev" in text

    def test_report_models_by_setup_json(self, ledger, data_dir, tmp_project):
        self._seed_setup_pair(ledger, tmp_project, time.time())

        setup_result = invoke(
            "report", "--since", "7d", "--models", "--by", "setup", "--json", data_dir=data_dir
        )
        default_result = invoke("report", "--since", "7d", "--models", "--json", data_dir=data_dir)

        assert setup_result.exit_code == 0 and default_result.exit_code == 0
        setup_data = json.loads(setup_result.output)
        default_data = json.loads(default_result.output)
        assert set(setup_data) == set(default_data)
        assert len(default_data["models"]) == 1
        assert [row["setup"]["plan"] for row in setup_data["models"]] == [None, "prov/planner"]
        for row in setup_data["models"]:
            assert list(row["setup"]) == ["plan", "implement", "fallback", "review"]
            assert row["setup"]["implement"] == "prov/impl"
            assert row["setup"]["fallback"] == "prov/fb"
            assert row["setup"]["review"] == "prov/rev"

    @pytest.mark.parametrize("extra", [[], ["--json"]])
    def test_report_models_by_validation(self, ledger, data_dir, extra):
        result = invoke("report", "--by", "setup", *extra, data_dir=data_dir)
        assert result.exit_code == 2
        assert "--by needs --models" in result.output

        result = invoke("report", "--models", "--by", "nope", *extra, data_dir=data_dir)
        assert result.exit_code == 2
        assert "--by must be one of model, setup" in result.output

    def test_report_shows_subscription_usage_warning(
        self, ledger, data_dir, tmp_project, subscription_config
    ):
        task_id = seed_task(ledger, tmp_project)
        _seed_provider_run(ledger, tmp_project, task_id, "r-go", cost=2.4)

        data = json.loads(invoke("report", "--json", data_dir=data_dir).output)
        warnings = data["cost"]["warnings"]
        assert len(warnings) == 1 and warnings[0].startswith("opencode-go 5h usage 80%")

        text = invoke("report", data_dir=data_dir).output
        assert "| opencode-go | $2.4000 | $2.4000 | $2.4000 |" in text
        assert "opencode-go 5h usage 80% (warn at 80%), resets " in text
        assert "cap" not in text

    def test_human_report_shows_task_base(
        self, ledger: Ledger, data_dir: Path, tmp_project: Path
    ) -> None:
        text = TASK.format(
            id="based", title="Based", project=tmp_project, priority="normal"
        ).replace("policy: review", "policy: review\nbase: programme/x")
        spec = parse_task(text, tmp_project / "tasks" / "based.md", copy.deepcopy(DEFAULT_CONFIG))
        task_id = ledger.upsert_task(spec, text)
        seed_run(ledger, task_id, "r-based", "landed", started_at=time.time())
        ledger.update_run("r-based", branch_name="feature/based/r-based")

        result = invoke("report", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        assert "- branch: `feature/based/r-based`\n- base: `programme/x`" in result.output

    def test_human_report_shows_project_default_base(
        self, ledger: Ledger, data_dir: Path, tmp_project: Path
    ) -> None:
        (tmp_project / ".helmraiser").mkdir(exist_ok=True)
        (tmp_project / ".helmraiser" / "project.toml").write_text(
            '[defaults]\nbase = "programme/y"\n', encoding="utf-8"
        )
        text = TASK.format(
            id="pbased", title="Project based", project=tmp_project, priority="normal"
        )
        spec = parse_task(text, tmp_project / "tasks" / "pbased.md", copy.deepcopy(DEFAULT_CONFIG))
        task_id = ledger.upsert_task(spec, text)
        seed_run(ledger, task_id, "r-pbased", "landed", started_at=time.time())
        ledger.update_run("r-pbased", branch_name="feature/pbased/r-pbased")

        result = invoke("report", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        assert "- branch: `feature/pbased/r-pbased`\n- base: `programme/y`" in result.output

    def test_since_window_and_validation(self, ledger: Ledger, data_dir: Path, tmp_project: Path):
        task_id = seed_task(ledger, tmp_project)
        now = time.time()
        seed_run(
            ledger, task_id, "r-old", "landed", started_at=now - 3 * 86400, ended_at=now - 2 * 86400
        )
        assert json.loads(invoke("report", "--json", data_dir=data_dir).output)["landed"] == []
        week = json.loads(invoke("report", "--since", "7d", "--json", data_dir=data_dir).output)
        assert [e["run_id"] for e in week["landed"]] == ["r-old"]
        bad = invoke("report", "--since", "soon", "--json", data_dir=data_dir)
        assert bad.exit_code == 2 and "--since" in json.loads(bad.output)["error"]
        assert parse_since("30d") == 30 * 86400 and parse_since("36h") == 36 * 3600


class TestLog:
    def test_dispatch_routing_timeline_and_jsonl(self, ledger, data_dir, tmp_project):
        task_id = seed_task(ledger, tmp_project)
        seed_run(ledger, task_id, "r-1", "landed", started_at=time.time(), ended_at=time.time())
        figures = {
            "openai-codex": {
                "has_headroom": True,
                "week": {
                    "used": 0.2,
                    "elapsed": 0.5,
                    "headroom": 0.275,
                    "target": 0.95,
                },
            }
        }
        payloads = [
            {
                "lever": "plan",
                "from": False,
                "to": True,
                "reason": "plan_headroom",
                "headroom": figures,
            },
            {
                "lever": "implement",
                "from": "opencode-go/glm-5.3-flash:high",
                "to": "openai-codex/gpt-6-astra:high",
                "reason": "large_task_headroom",
                "headroom": figures,
            },
            {
                "lever": "scout",
                "subscription": "openai-codex",
                "used": 0.2,
                "elapsed": 0.5,
                "headroom": 0.275,
                "choice": "run",
            },
        ]
        for payload in payloads:
            ledger.append_event("r-1", "helmraiser", "routing", payload)
        human = invoke("log", "r-1", data_dir=data_dir)
        assert human.exit_code == 0, human.output
        lines = human.output.splitlines()
        assert "plan: plan_headroom; False -> True" in lines[0]
        assert "implement: large_task_headroom" in lines[1]
        assert 'lever="scout"' in lines[2] and 'choice="run"' in lines[2]
        jsonl = invoke("log", "r-1", "--json", data_dir=data_dir)
        assert jsonl.exit_code == 0, jsonl.output
        assert [json.loads(line)["payload"] for line in jsonl.output.splitlines()] == payloads

    def test_since_id_timeline_and_jsonl(self, ledger: Ledger, data_dir: Path, tmp_project: Path):
        task_id = seed_task(ledger, tmp_project)
        seed_run(ledger, task_id, "r-1", "landed", started_at=time.time(), ended_at=time.time())
        # A short literal: a task id built from tmp_path can outrun SUMMARY_WIDTH.
        first = ledger.append_event(
            "r-1", "helmraiser", "run_start", {"task_id": "proj:fix-widget"}
        )
        ledger.append_event(
            "r-1",
            "pi",
            "message_end",
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello  there"}],
                },
            },
            stage="implement",
            session_id="r-1-implement",
        )
        last = ledger.append_event("r-1", "helmraiser", "run_end", {"status": "landed"})

        result = invoke("log", "r-1", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert len(lines) == 3
        assert (
            "helmraiser" in lines[0]
            and "run_start" in lines[0]
            and 'task_id="proj:fix-widget"' in lines[0]
        )
        assert "pi" in lines[1] and "implement" in lines[1] and "assistant: hello there" in lines[1]

        result = invoke("log", "r-1", "--since-id", str(first), "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        events = [json.loads(line) for line in result.output.splitlines()]
        assert [e["id"] for e in events] == [first + 1, last]
        assert events[0]["payload"]["message"]["role"] == "assistant"
        assert events[1]["type"] == "run_end" and events[1]["stage"] is None

    def test_follow_stops_on_terminal_run(
        self, ledger: Ledger, data_dir: Path, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        task_id = seed_task(ledger, tmp_project)
        seed_run(ledger, task_id, "r-1", "running", started_at=time.time(), stage="implement")
        ledger.append_event("r-1", "helmraiser", "run_start", {})
        polls: list[float] = []

        def fake_sleep(seconds: float) -> None:
            polls.append(seconds)
            ledger.append_event("r-1", "helmraiser", "run_end", {"status": "landed"})
            ledger.finish_run("r-1", "landed")

        monkeypatch.setattr(monitor.time, "sleep", fake_sleep)
        result = invoke("log", "r-1", "--follow", "--interval", "0.5", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert [json.loads(line)["type"] for line in result.output.splitlines()] == [
            "run_start",
            "run_end",
        ]
        assert polls == [0.5]

    def test_unknown_run(self, data_dir: Path, ledger: Ledger):
        result = invoke("log", "nope", "--json", data_dir=data_dir)
        assert result.exit_code == 1 and "unknown run" in json.loads(result.output)["error"]


class TestAttach:
    def _seed(self, ledger: Ledger, data_dir: Path, tmp_project: Path, worktree: Path) -> str:
        task_id = seed_task(ledger, tmp_project)
        seed_run(
            ledger,
            task_id,
            "r-1",
            "awaiting_approval",
            started_at=time.time(),
            worktree=str(worktree),
        )
        ledger.start_iteration("r-1", 1, "r-1-implement", "prov/impl")
        ledger.start_iteration("r-1", 2, "r-1-implement-2", "prov/fallback")
        sessions = RunDir("r-1", data_dir).create().sessions
        for sid in ("r-1-implement", "r-1-implement-2", "r-1-review"):
            (sessions / f"2026-09-23T10-00-00-000Z_{sid}.jsonl").write_text("{}\n")
        return str(sessions)

    def test_argv_uses_worktree_and_latest_implement_session(
        self, ledger: Ledger, data_dir: Path, tmp_project: Path
    ):
        sessions = self._seed(ledger, data_dir, tmp_project, tmp_project)
        result = invoke("attach", "r-1", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["argv"] == ["pi", "--session-dir", sessions, "--session", "r-1-implement-2"]
        assert out["cwd"] == str(tmp_project) and out["worktree_exists"] is True
        review = json.loads(
            invoke("attach", "r-1", "--stage", "review", "--json", data_dir=data_dir).output
        )
        assert review["session_id"] == "r-1-review"
        human = invoke("attach", "r-1", data_dir=data_dir).output
        assert (
            human.strip()
            == f"cd {tmp_project} && pi --session-dir {sessions} --session r-1-implement-2"
        )

    def test_missing_worktree_falls_back_to_run_dir(
        self, ledger: Ledger, data_dir: Path, tmp_project: Path
    ):
        gone = tmp_project / "gone-worktree"
        self._seed(ledger, data_dir, tmp_project, gone)
        result = invoke("attach", "r-1", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert "is gone" in result.output and str(RunDir("r-1", data_dir).path) in result.output
        out = json.loads(invoke("attach", "r-1", "--json", data_dir=data_dir).output)
        assert out["cwd"] == str(RunDir("r-1", data_dir).path) and out["worktree_exists"] is False

    def test_exec_and_errors(
        self, ledger: Ledger, data_dir: Path, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        self._seed(ledger, data_dir, tmp_project, tmp_project)
        calls: list[tuple[str, list[str], str]] = []
        monkeypatch.setattr(os, "chdir", lambda path: calls.append(("chdir", [], str(path))))
        monkeypatch.setattr(os, "execvp", lambda file, argv: calls.append(("exec", argv, file)))
        result = invoke("attach", "r-1", "--exec", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert calls[0] == ("chdir", [], str(tmp_project))
        assert (
            calls[1][0] == "exec" and calls[1][2] == "pi" and calls[1][1][-1] == "r-1-implement-2"
        )

        missing = invoke("attach", "r-1", "--stage", "plan", "--json", data_dir=data_dir)
        assert missing.exit_code == 1 and "no plan session" in json.loads(missing.output)["error"]
        bad = invoke("attach", "r-1", "--stage", "merge", "--json", data_dir=data_dir)
        assert bad.exit_code == 2


@pytest.mark.parametrize("combined", [False, True])
def test_depends_on_hold_rendering(monkeypatch, ledger, tmp_project, data_dir, combined):
    key = seed_task(ledger, tmp_project)
    hold = {
        "reason": "depends_on",
        "unmet": [
            {"id": "missing", "reason": "unknown", "text": "missing: unknown dependency id"},
            {
                "id": "cycle",
                "reason": "cycle",
                "text": "cycle: dependency cycle: cycle -> next -> cycle",
            },
        ],
    }
    if combined:
        hold.update(run_id="r-wait", status="awaiting_approval", pending="merge")
    monkeypatch.setattr(Ledger, "held_tasks", lambda *args, **kwargs: {key: hold})
    result = invoke("status", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["queue_next"][0]["held_by"] == hold
    assert payload["held"][0]["unmet"] == hold["unmet"]
    human = invoke("status", data_dir=data_dir)
    assert human.exit_code == 0, human.output
    compact = " ".join(human.output.split())
    assert "depends_on:" in compact
    assert "missing: unknown dependency id" in compact
    assert "cycle -> next -> cycle" in compact
    if combined:
        assert "by run r-wait (awaiting merge approval)" in compact


def test_depends_on_status_real_ledger(ledger, tmp_project, data_dir, cli_env):
    from helmraiser.task.loader import render_task
    from tests.ledger.test_db import make_spec, run_row

    specs = [
        make_spec(tmp_project, "dependent", depends_on=("queued", "missing", "failed", "cycle")),
        make_spec(tmp_project, "queued"),
        make_spec(tmp_project, "failed", status="failed"),
        make_spec(tmp_project, "cycle", depends_on=("cycle-next",)),
        make_spec(tmp_project, "cycle-next", depends_on=("cycle",)),
    ]
    for spec in specs:
        ledger.upsert_task(spec, render_task(spec))
    ledger.create_run(run_row("r-failed", specs[2].task_id, str(tmp_project), "failed"))
    result = invoke("status", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    held = next(item for item in payload["held"] if item["id"] == "dependent")
    assert held["reason"] == "depends_on"
    assert [(item["id"], item["reason"]) for item in held["unmet"]] == [
        ("queued", "not_landed"),
        ("missing", "unknown"),
        ("failed", "failed"),
        ("cycle", "cycle"),
    ]
    queue = next(item for item in payload["queue_next"] if item["id"] == "dependent")
    assert queue["held_by"]["unmet"] == held["unmet"]
    written = json.loads((cli_env / "helmraiser/status.json").read_text())
    assert written["held"] == payload["held"]
    assert payload["queued_count"] == 4
    human = invoke("status", data_dir=data_dir)
    assert human.exit_code == 0, human.output
    compact = " ".join(human.output.split())
    for fragment in (
        "Held:",
        "depends_on:",
        "queued: not landed",
        "missing: unknown",
        "failed: latest run",
        "dependency cycle",
    ):
        assert fragment in compact


SCOPE_REASON = ("needs " * 30).strip()


def test_piped_status_keeps_free_text_lines_whole(
    monkeypatch, ledger, data_dir, tmp_project, cli_env
):
    """A piped console wraps at 80 columns; ``grep Held:`` must still find the reason."""
    monkeypatch.delenv("COLUMNS", raising=False)
    parked = seed_task(ledger, tmp_project, task_id="parked")
    ledger.set_task_status(parked, "awaiting_approval")
    seed_run(ledger, parked, "r-wait", "awaiting_approval", started_at=time.time() - 60)
    ledger.update_run("r-wait", project=str(tmp_project))
    ledger.record_approval("r-wait", "scope", "pending")
    held = seed_task(ledger, tmp_project, task_id="a-task-with-a-rather-long-identifier-" * 2)
    ledger.set_task_status(held, "queued")
    reworked = seed_task(ledger, tmp_project, task_id="reworked")
    ledger.set_task_status(reworked, "failed")
    seed_run(ledger, reworked, "r-fail", "failed", started_at=time.time() - 30)
    ledger.update_run("r-fail", ended_at=time.time(), failure_reason="verify: " + "boom " * 30)
    ledger.append_event("r-fail", "helmraiser", "rework_requested", {"round": 1, "note": "n"})
    monkeypatch.setattr(
        monitor,
        "pending_scope_request",
        lambda _ledger, _run_id: {"paths": ["src/a.py", "src/b.py"], "reason": SCOPE_REASON},
    )
    real_build = monitor.build_status

    def build(*args, **kwargs):
        return {
            **real_build(*args, **kwargs),
            "paused": {"reason": "usage limit " * 10},
            "drafts": {"/projects/" + "d" * 80: 2},
            "never_startable": [{"task_id": "t:never", "text": "projects to the cap " * 5}],
            "held_providers": {"provider-" + "p" * 70: {"until": 1_900_000_000.0}},
            "worker": {
                "state": "exited",
                "exit": {"reason": "error", "error": "OSError " * 12},
                "errors": [{"project": "/p" * 20, "stage": "task", "error": "ValueError " * 10}],
            },
            "scouts": {
                "pending": [{"project": "/projects/" + "s" * 80, "wait_reason": "cooldown"}],
                "running": [{"project": "/projects/" + "r" * 80, "scout_id": "abcdef0123456789"}],
            },
            "resuming": [
                {"run_id": "r-res", "task": "resume-" * 12, "stage": "verify", "held_by": None}
            ],
        }

    monkeypatch.setattr(monitor, "build_status", build)

    result = invoke("status", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    status_path = json.loads(invoke("status", "--json", data_dir=data_dir).output)["status_path"]
    prefixes = (
        "Queue: ",
        "Drafts: ",
        "Never starts: ",
        "Provider held: ",
        "Worker: ",
        "Project error: ",
        "Scout pending: ",
        "Scout running: ",
        "Resuming: ",
        "Held: ",
        "scope request ",
        "Reworked (24h): ",
        "failed ",
    )
    for prefix in prefixes:
        found = [line for line in lines if line.lstrip().startswith(prefix)]
        assert len(found) == 1, (prefix, result.output)
        (line,) = found
        assert len(line) > 80, line
    assert f"Held: {held} by run r-wait (awaiting scope approval)" in lines
    assert "  scope request r-wait: src/a.py, src/b.py — " + SCOPE_REASON in lines
    assert f"status.json: {status_path}" in lines


def test_depends_on_status_release(ledger, tmp_project, data_dir):
    from helmraiser.task.loader import render_task
    from tests.ledger.test_db import make_spec, run_row
    from tests.ledger.test_depends_on import feature, git

    base, head = feature(tmp_project)
    upstream = make_spec(tmp_project, "upstream", status="landed")
    dependent = make_spec(tmp_project, "dependent", depends_on=("upstream",), base=base)
    for spec in (upstream, dependent):
        ledger.upsert_task(spec, render_task(spec))
    ledger.create_run(
        {**run_row("r", upstream.task_id, str(tmp_project), "landed"), "head_commit": head}
    )
    before = json.loads(invoke("status", "--json", data_dir=data_dir).output)
    assert before["held"][0]["unmet"][0]["reason"] == "not_in_base"
    git(tmp_project, "merge", "--no-ff", "feature", "-m", "merge")
    after = json.loads(invoke("status", "--json", data_dir=data_dir).output)
    assert after["held"] == []
    assert after["queue_next"][0]["held_by"] is None
    assert before["queued_count"] == after["queued_count"] == 1
