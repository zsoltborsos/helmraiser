"""``helmraiser worker start|stop|status`` and the work-loop stop request.

The spawned command is stubbed (``worker_argv``) so no test ever runs a real
``helmraiser work``: a sleeping interpreter stands in for it, and teardown kills
it. The work-loop tests reuse the FakePi harness.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from helmraiser.cli import app, commands, monitor, worker_commands
from helmraiser.ledger.db import Ledger
from tests.conftest import FakePi

runner = CliRunner()

STUB_ARGV = [sys.executable, "-c", "import time; time.sleep(30)  # worker stub"]

REVIEW_PASS = (
    "Looks correct.\n\n```json\n" '{"verdict": "pass", "findings": [], "evidence_checks": []}\n```'
)

TASK = """---
id: {task_id}
title: Fix the widget
project: {project}
status: draft
policy: review
isolation: none
budget:
  max_cost_usd: 2.0
  max_minutes: 30
  max_iterations: 3
verify:
  - name: marker
    run: test -f done.txt
---
## Goal

Make the widget work.
"""


@pytest.fixture(autouse=True)
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configured_config: dict) -> dict:
    config = configured_config
    monkeypatch.setattr(commands, "load_config", lambda: config)
    monkeypatch.setattr(monitor, "load_config", lambda: config)
    monkeypatch.setattr(worker_commands, "load_config", lambda: config)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    return config


def test_missing_models_prevent_worker_files(cli_env, data_dir, monkeypatch):
    cli_env["profiles"] = {}
    monkeypatch.setattr(worker_commands.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    result = runner.invoke(app, ["worker", "start", "--json", "--data-dir", str(data_dir)])
    assert result.exit_code == 2, result.output
    assert json.loads(result.output) == {
        "error": "models_not_configured",
        "missing": ["plan", "implement", "fallback", "review"],
    }
    assert not data_dir.exists()


@pytest.mark.parametrize("maximum", [1, 2])
def test_worker_model_preflight_honors_max(cli_env, data_dir, tmp_project, monkeypatch, maximum):
    first = queue_task(tmp_project, data_dir, "configured")
    queue_task(tmp_project, data_dir, "unconfigured")
    models = cli_env["profiles"]
    cli_env["profiles"] = {}
    with Ledger(data_dir / "helmraiser.db") as ledger:
        row = ledger.get_task(first)
        overrides = "models:\n" + "".join(
            f"  {role}: {models[role]['model']}\n"
            for role in ("plan", "implement", "fallback", "review")
        )
        ledger.set_task_snapshot(first, row["snapshot"].replace("---\n", "---\n" + overrides, 1))
    spawned = []

    def spawn(*args, **kwargs):
        spawned.append(args)
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(worker_commands.subprocess, "Popen", spawn)
    result = worker("start", "--max", str(maximum), "--json", data_dir=data_dir)
    if maximum == 1:
        assert result.exit_code == 0, result.output
        assert len(spawned) == 1
    else:
        assert result.exit_code == 2, result.output
        assert json.loads(result.output)["error"] == "models_not_configured"
        assert not spawned
        assert not worker_commands.record_path(data_dir).exists()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def stub_argv(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Never spawn real ``helmraiser work``; a sleeping interpreter stands in.

    Its cmdline contains ``work`` so the /proc liveness check treats it as the
    worker. ``stub_argv.calls`` records the arguments ``worker_argv`` received.
    """
    calls: list[dict[str, Any]] = []
    real_worker_argv = worker_commands.worker_argv

    def fake(
        projects: list[str] | None,
        max_runs: int | None,
        max_parallel_runs: int | None,
        no_caps: bool,
        no_notify: bool,
        data_dir: Path | None,
    ) -> list[str]:
        calls.append(
            {
                "projects": projects,
                "max_runs": max_runs,
                "max_parallel_runs": max_parallel_runs,
                "no_caps": no_caps,
                "no_notify": no_notify,
                "data_dir": data_dir,
            }
        )
        return list(STUB_ARGV)

    monkeypatch.setattr(worker_commands, "worker_argv", fake)
    return SimpleNamespace(argv=STUB_ARGV, calls=calls, real=real_worker_argv)


@pytest.fixture
def reaper() -> Any:
    pids: list[int] = []
    yield pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass


def worker(*args: str, data_dir: Path) -> Any:
    return runner.invoke(app, ["worker", *args, "--data-dir", str(data_dir)])


def start_worker(data_dir: Path, reaper: list[int], *args: str) -> dict[str, Any]:
    result = worker("start", *args, data_dir=data_dir)
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    reaper.append(int(record["pid"]))
    return record


def queue_task(tmp_project: Path, data_dir: Path, task_id: str = "fix-widget") -> str:
    path = tmp_project / "topics" / f"{task_id}.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(TASK.format(project=tmp_project, task_id=task_id), encoding="utf-8")
    result = runner.invoke(
        app, ["intake", str(path), "--yes", "--json", "--data-dir", str(data_dir)]
    )
    assert result.exit_code == 0, result.output
    return str(json.loads(result.output)["task_id"])


def run_work(*args: str, data_dir: Path) -> Any:
    return runner.invoke(app, ["work", *args, "--data-dir", str(data_dir)])


class TestScoutWorker:
    def test_scout_child_argv_uses_current_python_without_force(self, tmp_path: Path):
        argv = commands._scout_child_argv(
            "/repo", data_dir=tmp_path, scout_id="assigned", request_id=None
        )
        assert argv == [
            sys.executable,
            "-m",
            "helmraiser",
            "scout",
            "/repo",
            "--json",
            "--data-dir",
            str(tmp_path),
            "--scout-id",
            "assigned",
        ]
        assert "--force" not in argv and "--request-id" not in argv


class TestStart:
    def test_starts_detached_and_writes_record(
        self, data_dir: Path, stub_argv: Any, reaper: list[int]
    ):
        result = worker("start", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        record = json.loads(result.output)
        reaper.append(int(record["pid"]))
        assert set(record) == {
            "pid",
            "started_at",
            "argv",
            "log",
            "projects",
            "exclude_projects",
            "max_parallel_runs",
        }
        assert record["projects"] is None and record["exclude_projects"] == []
        assert record["max_parallel_runs"] == 2
        assert record["argv"] == stub_argv.argv
        assert record["log"] == str(data_dir / "worker.log")
        assert (data_dir / "worker.log").exists()
        saved = json.loads((data_dir / "worker.json").read_text())
        assert saved["pid"] == record["pid"]

    def test_human_output(self, data_dir: Path, stub_argv: Any, reaper: list[int]):
        result = worker("start", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        record = json.loads((data_dir / "worker.json").read_text())
        reaper.append(int(record["pid"]))
        expected = f"worker started (pid {record['pid']}), log {data_dir / 'worker.log'}"
        assert expected in result.output

    def test_passes_options_and_data_dir_to_work(
        self, data_dir: Path, stub_argv: Any, reaper: list[int]
    ):
        project, other = "/tmp/some-project", "/tmp/other-project"
        record = start_worker(
            data_dir,
            reaper,
            "--json",
            "--project",
            project,
            "--project",
            other,
            "--max",
            "3",
            "--max-parallel",
            "4",
            "--no-caps",
            "--no-notify",
        )
        assert stub_argv.calls == [
            {
                "projects": [project, other],
                "max_runs": 3,
                "max_parallel_runs": 4,
                "no_caps": True,
                "no_notify": True,
                "data_dir": data_dir,
            }
        ]
        assert record["projects"] == [other, project]
        assert record["exclude_projects"] == []
        assert record["max_parallel_runs"] == 4
        real_argv = stub_argv.real(  # type: ignore[operator]
            [project, other], 3, 4, True, True, data_dir
        )
        assert real_argv[3:6] == ["work", "--watch", "--json"]
        assert [real_argv[i + 1] for i, a in enumerate(real_argv) if a == "--project"] == [
            project,
            other,
        ]
        assert real_argv[real_argv.index("--max") + 1] == "3"
        assert real_argv[real_argv.index("--max-parallel") + 1] == "4"
        assert "--no-caps" in real_argv and "--no-notify" in real_argv
        assert real_argv[real_argv.index("--data-dir") + 1] == str(data_dir)

    def test_second_start_is_refused_while_running(
        self, data_dir: Path, stub_argv: Any, reaper: list[int]
    ):
        record = start_worker(data_dir, reaper, "--json")
        again = worker("start", data_dir=data_dir)
        assert again.exit_code == 1, again.output
        assert f"a worker is already running (pid {record['pid']})" in again.output

    def test_start_allowed_again_after_the_worker_died(
        self, data_dir: Path, stub_argv: Any, reaper: list[int]
    ):
        record = start_worker(data_dir, reaper, "--json")
        os.kill(int(record["pid"]), signal.SIGKILL)
        os.waitpid(int(record["pid"]), 0)
        assert worker("start", data_dir=data_dir).exit_code == 0


class TestStatus:
    @pytest.mark.parametrize("json_output", [False, True], ids=["human", "json"])
    def test_status_uses_live_parallel_limit(self, data_dir, monkeypatch, json_output):
        monkeypatch.setattr(worker_commands, "pid_alive", lambda pid: True)
        record = {
            "pid": 123,
            "started_at": "now",
            "argv": [],
            "log": "log",
            "max_parallel_runs": 3,
        }
        worker_commands.write_record(record, data_dir)
        worker_commands.write_active(
            {"pid": 123, "max_parallel_runs": 4, "children": [], "errors": [], "skips": []},
            data_dir,
        )

        result = worker("status", *(["--json"] if json_output else []), data_dir=data_dir)
        assert result.exit_code == 0, result.output
        if json_output:
            payload = json.loads(result.output)
            assert payload["state"] == "running"
            assert payload["max_parallel_runs"] == 4
        else:
            assert "runs: 0 of 4" in result.output
            assert "runs: 0 of 3" not in result.output
        assert worker_commands.read_record(data_dir) == record

    @pytest.mark.parametrize(
        ("alive", "active_pid", "active_limit"),
        [
            pytest.param(True, 123, None, id="legacy-active"),
            pytest.param(True, 124, 4, id="another-worker"),
            pytest.param(True, None, None, id="missing-active"),
            pytest.param(False, 123, 4, id="exited-worker"),
        ],
    )
    def test_parallel_limit_falls_back_to_record(
        self, data_dir, monkeypatch, alive, active_pid, active_limit
    ):
        monkeypatch.setattr(worker_commands, "pid_alive", lambda pid: alive)
        record = {
            "pid": 123,
            "started_at": "now",
            "argv": [],
            "log": "log",
            "max_parallel_runs": 3,
        }
        worker_commands.write_record(record, data_dir)
        if active_pid is not None:
            active = {"pid": active_pid, "children": [], "errors": [], "skips": []}
            if active_limit is not None:
                active["max_parallel_runs"] = active_limit
            worker_commands.write_active(active, data_dir)

        human = worker("status", data_dir=data_dir)
        result = worker("status", "--json", data_dir=data_dir)
        assert human.exit_code == 0, human.output
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["state"] == ("running" if alive else "exited")
        assert payload["max_parallel_runs"] == 3
        if alive:
            assert "runs: 0 of 3" in human.output
        else:
            assert "worker: exited" in human.output
            assert "runs:" not in human.output
        assert worker_commands.read_record(data_dir) == record

    def test_handoff_does_not_replace_public_run_and_scout_status(self, data_dir, monkeypatch):
        monkeypatch.setattr(worker_commands, "pid_alive", lambda pid: True)
        record = {
            "pid": 123,
            "started_at": "now",
            "argv": [],
            "log": "log",
            "max_parallel_runs": 2,
            "restart_children": [{"pid": 999, "project": "/private"}],
        }
        worker_commands.write_record(record, data_dir)
        children = [
            {"pid": 124, "project": "/run", "kind": "run", "run_id": "r", "started_at": 1.0},
            {
                "pid": 125,
                "project": "/scout",
                "kind": "scout",
                "scout_id": "s",
                "run_id": None,
                "started_at": 2.0,
            },
        ]
        worker_commands.write_active({"pid": 123, "children": children}, data_dir)
        payload = json.loads(worker("status", "--json", data_dir=data_dir).output)
        assert payload["children"] == children
        assert payload["restart_children"] == record["restart_children"]
        human = worker("status", data_dir=data_dir).output
        assert "runs: 2 of 2" in human and "/run (pid 124)" in human and "/scout (pid 125)" in human
        assert "/private" not in human

    def test_none_without_record(self, data_dir: Path):
        result = worker("status", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"state": "none"}

    def test_running_shows_record_and_tail(self, data_dir: Path, stub_argv: Any, reaper: list[int]):
        record = start_worker(data_dir, reaper, "--json")
        result = worker("status", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["state"] == "running"
        assert payload["pid"] == record["pid"]
        assert payload["argv"] == stub_argv.argv
        assert payload["log"] == str(data_dir / "worker.log")
        assert payload["tail"] == []
        human = worker("status", data_dir=data_dir)
        assert f"worker: running (pid {record['pid']}" in human.output

    def test_tail_shows_last_five_log_lines(
        self, data_dir: Path, stub_argv: Any, reaper: list[int]
    ):
        start_worker(data_dir, reaper, "--json")
        log = data_dir / "worker.log"
        log.write_text("\n".join(f"line-{i}" for i in range(8)) + "\n", encoding="utf-8")
        result = worker("status", "--json", data_dir=data_dir)
        assert json.loads(result.output)["tail"] == [f"line-{i}" for i in range(3, 8)]

    def test_human_tail_is_one_short_line_per_event(
        self, data_dir: Path, stub_argv: Any, reaper: list[int]
    ):
        start_worker(data_dir, reaper, "--json")
        outcome = {
            "run_id": "r1",
            "task_id": "p:fix",
            "status": "landed",
            "iterations": 2,
            "cost_usd": 0.5,
            "review": "x" * 5000,
        }
        lines = [
            {"started": {"pid": 11, "project": "/p1", "run_id": None, "started_at": 1.0}},
            {"finished": {"pid": 11, "project": "/p1", "exit_code": 0, "runs": [outcome]}},
            {"project_error": {"project": "/p2", "stage": "task", "error": "ValueError: bad"}},
            {"skipped": "p:other", "reason": "quota:openai-codex", "until": 1.0, "x": "y" * 500},
        ]
        log = data_dir / "worker.log"
        log.write_text("\n".join([*map(json.dumps, lines), "E" * 500]) + "\n", encoding="utf-8")

        result = worker("status", data_dir=data_dir)
        raw = json.loads(worker("status", "--json", data_dir=data_dir).output)["tail"]

        tail = result.output.splitlines()[-5:]
        assert tail[:3] == [
            "  started /p1 (pid 11)",
            "  finished /p1 (pid 11, exit 0): landed p:fix",
            "  error: /p2 (task): ValueError: bad",
        ]
        assert tail[3].startswith('  {"skipped": "p:other"') and tail[3].endswith("…")
        assert all(len(line) <= 2 + worker_commands.TAIL_WIDTH for line in tail)
        assert raw == log.read_text().splitlines()  # --json keeps the raw lines

    @pytest.mark.parametrize(
        ("line", "text"),
        [
            (
                '{"runs": [], "summary": {"counts": {"landed": 2, "failed": 0}}}',
                "summary: landed 2",
            ),
            ('{"stopping": true, "running": 1}', '{"stopping": true, "running": 1}'),
            ('{"reconciled": [{"run_id": "r1"}]}', 'reconciled: [{"run_id": "r1"}]'),
            (
                '{"scout_started": {"project": "/p3", "pid": 12}}',
                "scout started /p3 (pid 12)",
            ),
            (
                '{"scout_finished": {"project": "/p3", "pid": 12, "exit_code": 0, '
                '"outcome": "succeeded", "drafts": 2}}',
                "scout finished /p3 (pid 12, exit 0, succeeded): 2 draft(s)",
            ),
            ("Traceback (most recent call last):", "Traceback (most recent call last):"),
        ],
    )
    def test_compact_line(self, line: str, text: str):
        assert worker_commands.compact_line(line) == text

    def test_status_shows_the_code_snapshot_the_worker_runs(
        self, data_dir: Path, stub_argv: Any, reaper: list[int]
    ):
        record = start_worker(data_dir, reaper, "--json")
        code = {"commit": "30f784a" + "0" * 33, "snapshot": str(data_dir / "code" / "30f784a")}
        worker_commands.update_record(int(record["pid"]), {"code": code}, data_dir)

        human = worker("status", data_dir=data_dir).output
        payload = json.loads(worker("status", "--json", data_dir=data_dir).output)
        overview = runner.invoke(app, ["status", "--data-dir", str(data_dir)]).output
        status_json = json.loads(
            runner.invoke(app, ["status", "--json", "--data-dir", str(data_dir)]).output
        )

        assert "code: 30f784a (snapshot)" in human.splitlines()
        assert payload["code"] == code and status_json["worker"]["code"] == code
        assert f"Worker: running (pid {record['pid']}, 0 of 2 runs, code 30f784a)" in overview

    def test_exited_after_the_worker_dies(self, data_dir: Path, stub_argv: Any, reaper: list[int]):
        record = start_worker(data_dir, reaper, "--json")
        os.kill(int(record["pid"]), signal.SIGKILL)
        os.waitpid(int(record["pid"]), 0)
        result = worker("status", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["state"] == "exited"
        assert payload["pid"] == record["pid"]

    def test_exited_shows_why(self, data_dir: Path, stub_argv: Any, reaper: list[int]):
        record = start_worker(data_dir, reaper, "--json")
        os.kill(int(record["pid"]), signal.SIGKILL)
        os.waitpid(int(record["pid"]), 0)
        ended = {"at": "2026-09-30T10:00:00+00:00", "reason": "error", "error": "OSError: boom"}
        (data_dir / "worker.json").write_text(json.dumps({**record, "exit": ended}))

        payload = json.loads(worker("status", "--json", data_dir=data_dir).output)
        human = worker("status", data_dir=data_dir).output
        overview = runner.invoke(app, ["status", "--data-dir", str(data_dir)]).output

        assert payload["state"] == "exited" and payload["exit"] == ended
        assert "exit: error: OSError: boom (at 2026-09-30T10:00:00+00:00)" in human
        assert "Worker: exited (error: OSError: boom)" in overview

    def test_status_shows_a_running_worker_and_its_project_errors(
        self, data_dir: Path, stub_argv: Any, reaper: list[int]
    ):
        record = start_worker(data_dir, reaper, "--json")
        error = {"project": "/p1", "stage": "task", "error": "ValueError: [x]", "since": 1.0}
        (data_dir / "worker.active.json").write_text(
            json.dumps({"pid": record["pid"], "children": [], "errors": [error]})
        )

        overview = runner.invoke(app, ["status", "--data-dir", str(data_dir)]).output

        assert f"Worker: running (pid {record['pid']}, 0 of 2 runs)" in overview
        assert "Project error: /p1 (task): ValueError: [x]" in overview


class TestStop:
    def test_stop_preserves_restart_handoff_and_never_signals(self, data_dir, monkeypatch):
        monkeypatch.setattr(worker_commands, "pid_alive", lambda pid: True)
        monkeypatch.setattr(os, "kill", lambda *args: pytest.fail("stop must not signal"))
        record = {
            "pid": 123,
            "restart_children": [{"pid": 124, "kind": "scout"}, {"pid": 125, "kind": "run"}],
        }
        worker_commands.write_record(record, data_dir)
        result = worker("stop", "--json", data_dir=data_dir)
        assert result.exit_code == 0 and json.loads(result.output)["stopped"] is True
        assert (data_dir / "worker.stop").read_text() == "stop\n"
        assert worker_commands.read_record(data_dir) == record

    def test_running_worker_gets_a_stop_request(
        self, data_dir: Path, stub_argv: Any, reaper: list[int]
    ):
        start_worker(data_dir, reaper, "--json")
        result = worker("stop", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert "stop requested; the worker finishes its current runs first" in result.output
        assert (data_dir / "worker.stop").exists()

    def test_stop_without_a_worker(self, data_dir: Path):
        result = worker("stop", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert "no worker is running" in result.output
        assert not (data_dir / "worker.stop").exists()


@pytest.fixture
def notify_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A notify-send stub that logs its arguments (for batch notification checks)."""
    log = tmp_path / "notify.log"
    bin_dir = tmp_path / "notify-bin"
    bin_dir.mkdir()
    executable = bin_dir / "notify-send"
    executable.write_text('#!/bin/sh\nprintf "%s|%s\\n" "$1" "$2" >> "$NOTIFY_LOG"\nexit 0\n')
    executable.chmod(0o755)
    monkeypatch.setenv("NOTIFY_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def patch_run_to_write_stop(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, *, once: bool = False
) -> None:
    """Make every (or the first) governed run write a pending stop request."""
    original = commands.GovernedRun.run
    state = {"written": False}

    async def write_stop(self: Any) -> Any:
        if not (once and state["written"]):
            state["written"] = True
            (data_dir / "worker.stop").write_text("stop\n", encoding="utf-8")
        return await original(self)

    monkeypatch.setattr(commands.GovernedRun, "run", write_stop)


class TestWorkHonoursStopRequest:
    @pytest.fixture
    def fake_run_script(self, fake_pi: FakePi) -> None:
        fake_pi.write_script(
            {
                "sessions": {
                    "*-implement": {
                        "text": "Implemented.",
                        "cost": 0.05,
                        "tokens_in": 500,
                        "tokens_out": 40,
                        "tools": ["edit"],
                        "files": [["done.txt", "ok\n"]],
                    },
                    "*-review": {
                        "text": REVIEW_PASS,
                        "cost": 0.02,
                        "tokens_in": 300,
                        "tokens_out": 20,
                    },
                }
            }
        )

    def test_no_run_starts_while_a_stop_request_is_pending(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi, notify_stub: Path
    ):
        first = queue_task(tmp_project, data_dir, task_id="first")
        second = queue_task(tmp_project, data_dir, task_id="second")
        (data_dir / "worker.stop").write_text("stop\n", encoding="utf-8")

        result = run_work("--max", "2", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.splitlines()[0])
        assert payload["runs"] == [] and payload["paused"] is None
        summary = payload["summary"]
        assert summary["stopped"] is True and summary["runs"] == []
        assert not (data_dir / "worker.stop").exists()
        assert "Batch: 0 runs" in notify_stub.read_text()
        ledger = Ledger(data_dir / "helmraiser.db")
        assert ledger.list_runs() == []
        assert ledger.get_task(first)["status"] == "queued"
        assert ledger.get_task(second)["status"] == "queued"
        assert fake_pi.calls() == []

    def test_stops_after_the_current_run(
        self,
        tmp_project: Path,
        data_dir: Path,
        fake_pi: FakePi,
        fake_run_script: None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        task_id = queue_task(tmp_project, data_dir, task_id="first")
        second = queue_task(tmp_project, data_dir, task_id="second")
        real = commands.GovernedRun
        original = real.run

        class StopWritingRun(real):  # type: ignore[misc]
            async def run(self) -> Any:  # type: ignore[override]
                (data_dir / "worker.stop").write_text("stop\n", encoding="utf-8")
                return await original(self)

        monkeypatch.setattr(commands, "GovernedRun", StopWritingRun)

        result = run_work("--max", "2", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        outcome, summary = (json.loads(line) for line in result.output.strip().splitlines())
        assert outcome["status"] == "awaiting_approval"
        assert summary["summary"]["stopped"] is True
        ledger = Ledger(data_dir / "helmraiser.db")
        assert len(ledger.list_runs()) == 1
        assert ledger.get_task(task_id)["status"] == "awaiting_approval"
        assert ledger.get_task(second)["status"] == "queued"
        assert not (data_dir / "worker.stop").exists()

    def test_stop_arriving_during_the_last_allowed_run_is_consumed(
        self,
        tmp_project: Path,
        data_dir: Path,
        fake_pi: FakePi,
        fake_run_script: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        git_init: Any,
    ):
        other = tmp_path / "other"
        other.mkdir()
        git_init(other)
        queue_task(tmp_project, data_dir, task_id="first")
        # Another project: a parked run holds its own project's later tasks.
        second = queue_task(other, data_dir, task_id="second")
        patch_run_to_write_stop(monkeypatch, data_dir, once=True)

        result = run_work("--max", "1", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        outcome, summary = (json.loads(line) for line in result.output.strip().splitlines())
        assert outcome["status"] == "awaiting_approval"
        assert summary["summary"]["stopped"] is True
        assert not (data_dir / "worker.stop").exists()

        # The next worker must not stop immediately on the stale request.
        again = run_work("--once", "--json", data_dir=data_dir)
        assert again.exit_code == 0, again.output
        next_outcome, next_summary = (
            json.loads(line) for line in again.output.strip().splitlines()
        )
        assert next_outcome["status"] == "awaiting_approval"
        assert "stopped" not in next_summary["summary"]
        assert Ledger(data_dir / "helmraiser.db").get_task(second)["status"] == "awaiting_approval"

    def test_human_summary_mentions_stop(
        self,
        tmp_project: Path,
        data_dir: Path,
        fake_pi: FakePi,
        fake_run_script: None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        queue_task(tmp_project, data_dir, task_id="first")
        queue_task(tmp_project, data_dir, task_id="second")
        real = commands.GovernedRun
        original = real.run

        class StopWritingRun(real):  # type: ignore[misc]
            async def run(self) -> Any:  # type: ignore[no-untyped-def]
                (data_dir / "worker.stop").write_text("stop\n", encoding="utf-8")
                return await original(self)

        monkeypatch.setattr(commands, "GovernedRun", StopWritingRun)

        result = run_work("--max", "2", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        assert "  stopped: on request" in result.output


class TestConcurrentStart:
    def test_concurrent_starts_cannot_both_launch_a_worker(
        self,
        data_dir: Path,
        stub_argv: Any,
        reaper: list[int],
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A start that is still spawning holds the record lock, so a concurrent
        start waits for it and is then refused instead of launching a second,
        untracked worker."""
        real_popen = worker_commands.subprocess.Popen
        spawns: list[int] = []
        nested_results: list[Any] = []
        nested_thread: threading.Thread | None = None

        def direct_start() -> dict[str, Any]:
            """Run the command function directly: CliRunner's stream capture is
            not safe from a background thread."""
            try:
                worker_commands.start(
                    project=None,
                    max_runs=None,
                    max_parallel_runs=None,
                    no_caps=False,
                    no_notify=False,
                    as_json=True,
                    data_dir=data_dir,
                )
                return {"exit_code": 0}
            except typer.Exit as exc:
                return {"exit_code": exc.exit_code}

        def probing_popen(argv: list[str], **kwargs: Any) -> Any:
            nonlocal nested_thread
            process = real_popen(argv, **kwargs)
            spawns.append(process.pid)
            if len(spawns) == 1:
                # A concurrent start, still inside the first start's critical
                # section: it must wait for the lock, not launch its own child.
                nested_thread = threading.Thread(
                    target=lambda: nested_results.append(direct_start())
                )
                nested_thread.start()
                nested_thread.join(timeout=2)
                assert nested_thread.is_alive(), "concurrent start did not wait for the lock"
                assert spawns == [process.pid]
            return process

        monkeypatch.setattr(worker_commands.subprocess, "Popen", probing_popen)

        result = worker("start", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        assert nested_thread is not None
        nested_thread.join(timeout=10)
        assert len(spawns) == 1
        (nested,) = nested_results
        assert nested == {"exit_code": 1}
        record = json.loads((data_dir / "worker.json").read_text())
        reaper.append(int(record["pid"]))
        assert int(record["pid"]) == spawns[0]

    def test_start_waits_for_a_held_record_lock(
        self,
        data_dir: Path,
        stub_argv: Any,
        reaper: list[int],
        monkeypatch: pytest.MonkeyPatch,
    ):
        """While another start holds the record lock, no child is spawned."""
        real_popen = worker_commands.subprocess.Popen
        spawns: list[int] = []

        def counting_popen(argv: list[str], **kwargs: Any) -> Any:
            process = real_popen(argv, **kwargs)
            spawns.append(process.pid)
            return process

        monkeypatch.setattr(worker_commands.subprocess, "Popen", counting_popen)
        data_dir.mkdir(parents=True, exist_ok=True)
        record_file = data_dir / "worker.json"
        lock_handle = record_file.open("a+", encoding="utf-8")
        fcntl.flock(lock_handle, fcntl.LOCK_EX)

        done = threading.Event()

        def background_start() -> None:
            try:
                worker_commands.start(
                    project=None,
                    max_runs=None,
                    max_parallel_runs=None,
                    no_caps=False,
                    no_notify=False,
                    as_json=True,
                    data_dir=data_dir,
                )
            except typer.Exit:
                pass
            done.set()

        thread = threading.Thread(target=background_start)
        thread.start()
        try:
            done.wait(timeout=2)
            assert not done.is_set(), "start did not wait for the record lock"
            assert spawns == []  # no child while the lock is held
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
            lock_handle.close()
            thread.join(timeout=10)
        assert done.is_set()
        assert len(spawns) == 1
        record = json.loads(record_file.read_text())
        reaper.append(int(record["pid"]))
        assert int(record["pid"]) == spawns[0]


class TestStatusJson:
    def test_status_json_contains_worker(self, data_dir: Path, stub_argv: Any, reaper):
        start_worker(data_dir, reaper, "--json")
        result = runner.invoke(app, ["status", "--json", "--data-dir", str(data_dir)])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["worker"]["state"] == "running"
        assert payload["worker"]["pid"] == json.loads((data_dir / "worker.json").read_text())["pid"]

    def test_status_json_worker_none_without_a_record(self, data_dir: Path):
        result = runner.invoke(app, ["status", "--json", "--data-dir", str(data_dir)])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["worker"] == {"state": "none"}

    def test_status_json_worker_shows_children_and_limit(
        self, data_dir: Path, stub_argv: Any, reaper, cli_env: dict
    ):
        cli_env["max_parallel_runs"] = 3
        record = start_worker(data_dir, reaper, "--json")
        children = [{"pid": 11, "project": "/p1", "run_id": None, "started_at": 1.0}]
        (data_dir / "worker.active.json").write_text(
            json.dumps({"pid": record["pid"], "max_parallel_runs": 3, "children": children})
        )

        result = runner.invoke(app, ["status", "--json", "--data-dir", str(data_dir)])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)["worker"]
        assert payload["children"] == children and payload["max_parallel_runs"] == 3
        human = worker("status", data_dir=data_dir)
        assert "runs: 1 of 3" in human.output and "  /p1 (pid 11)" in human.output

    def test_status_json_worker_shows_project_errors(self, data_dir: Path, stub_argv: Any, reaper):
        record = start_worker(data_dir, reaper, "--json")
        error = {
            "project": "/p1",
            "stage": "task",
            "error": "ValueError: /p1/.helmraiser/project.toml: services[1]: unknown key(s) x",
            "since": 1.0,
            "retry_at": 301.0,
        }
        (data_dir / "worker.active.json").write_text(
            json.dumps({"pid": record["pid"], "children": [], "errors": [error]})
        )

        result = runner.invoke(app, ["status", "--json", "--data-dir", str(data_dir)])

        assert json.loads(result.output)["worker"]["errors"] == [error]
        human = worker("status", data_dir=data_dir)
        assert f"error: /p1 (task): {error['error']}" in human.output

    def test_children_of_another_worker_pid_are_ignored(
        self, data_dir: Path, stub_argv: Any, reaper
    ):
        start_worker(data_dir, reaper, "--json")
        (data_dir / "worker.active.json").write_text(
            json.dumps({"pid": 1, "children": [{"pid": 11, "project": "/p1"}]})
        )

        result = runner.invoke(app, ["status", "--json", "--data-dir", str(data_dir)])

        assert json.loads(result.output)["worker"]["children"] == []

    def test_start_records_the_config_scope(self, data_dir: Path, stub_argv: Any, reaper, cli_env):
        cli_env["worker_exclude_projects"] = ["/other-project"]

        record = start_worker(data_dir, reaper, "--json")

        assert record["projects"] is None and record["exclude_projects"] == ["/other-project"]
        assert stub_argv.calls[0]["projects"] is None
