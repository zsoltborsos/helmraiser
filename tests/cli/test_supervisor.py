"""The long-running ``work`` loop: ``work --watch`` / ``--max-parallel`` / ``worker start``.

Most tests drive ``Supervisor`` with fake children and a fake clock: a fake child
claims its project's next run from the ledger as ``work --once --project`` would,
and finishes after a few fake seconds. One test spawns real children
(``python -m helmraiser work --once``) through the fake pi.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import helmraiser
from helmraiser.cli import app, commands, supervisor
from helmraiser.cli.supervisor import (
    NO_RUN_BACKOFF_S,
    PROJECT_ERROR_BACKOFF_S,
    CodeVersion,
    Supervisor,
    SupervisorResult,
)
from helmraiser.cli.worker_commands import update_record
from helmraiser.ledger.db import Ledger, ProjectScope
from helmraiser.storage.state import StateManager
from helmraiser.task.loader import render_task
from helmraiser.task.project import effective_run_limit
from tests.cli.test_held_base_sync import seed_pr_dependency_chain, stub_sync
from tests.conftest import FakePi, configured_test_config
from tests.ledger.test_db import make_spec, run_row
from tests.loop.test_reconcile import FakeController

runner = CliRunner()


@pytest.fixture(autouse=True)
def runtime_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("HELMRAISER_RESTART_CHILDREN", raising=False)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def ledger(data_dir: Path) -> Ledger:
    return Ledger(data_dir / "helmraiser.db")


class FakeChild:
    def __init__(self, harness: Harness, pid: int, finish: Callable[[], int], project: str = ""):
        self.harness = harness
        self.pid = pid
        self.project = project
        self.finish_at = harness.now + harness.duration
        self.finish = finish
        self.code: int | None = None

    def poll(self) -> int | None:
        if self.code is None and self.harness.now >= self.finish_at:
            self.code = self.finish()
        return self.code


class TestAdoptedProcess:
    def test_stat_parses_last_parenthesis(self, tmp_path: Path):
        path = tmp_path / "123"
        path.mkdir()
        stat = path / "stat"
        stat.write_text("123 (a strange ) name) S " + "0 " * 18 + "456 0")
        assert supervisor._read_child_stat(123, tmp_path) == ("S", "456")
        stat.write_text("malformed")
        assert supervisor._read_child_stat(123, tmp_path) is None
        assert supervisor._read_child_stat(124, tmp_path) is None

    @pytest.mark.parametrize("status, expected", [(0, 0), (256, 1), (9, -9)])
    def test_wait_status_and_cached_completion(self, monkeypatch, status, expected):
        monkeypatch.setattr(supervisor, "_read_child_stat", lambda pid: ("S", "token"))
        calls = []
        monkeypatch.setattr(
            os, "waitpid", lambda pid, flags: calls.append((pid, flags)) or (pid, status)
        )
        process = supervisor.AdoptedProcess(123, "token")
        assert process.poll() == expected
        assert process.poll() == expected
        assert calls == [(123, os.WNOHANG)]

    @pytest.mark.parametrize(
        "identity, token", [(None, "token"), (("S", "new"), "old"), (("S", "token"), None)]
    )
    def test_missing_or_reused_identity_never_waits(self, monkeypatch, identity, token):
        monkeypatch.setattr(supervisor, "_read_child_stat", lambda pid: identity)
        monkeypatch.setattr(os, "waitpid", lambda *args: pytest.fail("unsafe wait"))
        assert supervisor.AdoptedProcess(123, token).poll() == 1
        assert supervisor.AdoptedProcess(1, token).poll() == 1

    @pytest.mark.parametrize("state, expected", [("S", None), ("Z", 1), ("X", 1)])
    def test_non_child_liveness(self, monkeypatch, state, expected):
        monkeypatch.setattr(supervisor, "_read_child_stat", lambda pid: (state, "token"))

        def wait(*args):
            raise ChildProcessError

        monkeypatch.setattr(os, "waitpid", wait)
        assert supervisor.AdoptedProcess(123, "token").poll() == expected

    def test_matching_child_is_live(self, monkeypatch):
        monkeypatch.setattr(supervisor, "_read_child_stat", lambda pid: ("S", "token"))
        monkeypatch.setattr(os, "waitpid", lambda *args: (0, 0))
        assert supervisor.AdoptedProcess(123, "token").poll() is None


class Harness:
    """Fake clock, sleep and children for ``Supervisor``."""

    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.now = 0.0
        self.ticks = 0
        self.duration = 5.0
        self.hooks: dict[int, Callable[[], None]] = {}
        self.no_run: set[str] = set()
        self.children: list[FakeChild] = []
        self.spawned: list[dict[str, Any]] = []
        self.scout_spawned: list[dict[str, Any]] = []
        self.scout_drafts: dict[str, int] = {}
        self.scout_fail: set[str] = set()
        self.max_alive = 0
        self.run_config: dict[str, Any] = {}

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.ticks += 1
        assert self.ticks < 5000, "the supervisor never exited"
        self.now += seconds
        hook = self.hooks.pop(self.ticks, None)
        if hook is not None:
            hook()

    def adopt_waits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def waitpid(pid: int, flags: int) -> tuple[int, int]:
            assert flags == os.WNOHANG
            child = next(child for child in self.children if child.pid == pid)
            code = child.poll()
            return (0, 0) if code is None else (pid, code << 8)

        monkeypatch.setattr(os, "waitpid", waitpid)

    def alive(self) -> list[FakeChild]:
        return [child for child in self.children if child.code is None]

    def spawn(self, project: str, output: Path) -> FakeChild:
        """What ``work --once --json --project <project>`` does, in fake time."""
        running = [child.project for child in self.children if child.code is None]
        assert running.count(project) < effective_run_limit(Path(project), self.run_config)
        entry: dict[str, Any] = {"project": project, "at": self.now}
        run_id: str | None = None
        task_id: str | None = None
        resumable = self.ledger.next_resumable(project=project, config=self.run_config)
        if project in self.no_run:
            pass
        elif resumable is not None:
            run_id, task_id = str(resumable["run_id"]), str(resumable["task_id"])
            assert self.ledger.claim_resumable(run_id)
            entry["resumed"] = run_id
        else:
            row = self.ledger.next_queued(project=project, config=self.run_config)
            assert row is not None, f"nothing to run in {project}"
            task_id = str(row["task_id"])
            run_id = f"run-{len(self.spawned)}"
            self.ledger.create_run(run_row(run_id, task_id, project))
            self.ledger.set_task_status(task_id, "running")
            entry["task_id"] = task_id

        def finish() -> int:
            if run_id is None:
                output.write_text(json.dumps({"error": "nothing to run"}) + "\n")
                return 1
            StateManager(Path(project) / ".helmraiser/state.db").release_lock(run_id, task["id"])
            self.ledger.finish_run(run_id, "landed")
            self.ledger.set_task_status(str(task_id), "landed")
            outcome = {
                "run_id": run_id,
                "task_id": task_id,
                "status": "landed",
                "iterations": 1,
                "cost_usd": 0.25,
                "minutes": 1.0,
            }
            output.write_text(json.dumps(outcome) + "\n" + json.dumps({"runs": [outcome]}) + "\n")
            return 0

        child = FakeChild(self, 1000 + len(self.children), finish, project)
        if run_id:
            task = self.ledger.get_task(str(task_id))
            state = StateManager(Path(project) / ".helmraiser/state.db")
            assert state.acquire_lock(run_id, task["id"], child.pid)
        self.spawned.append(entry)
        self.children.append(child)
        self.max_alive = max(self.max_alive, len(self.alive()))
        return child

    def spawn_scout(
        self, project: str, output: Path, scout_id: str, request_id: int | None
    ) -> FakeChild:
        """A model-free ``helmraiser scout --json`` child."""
        assert project not in [child.project for child in self.alive()]
        entry = {
            "project": project,
            "at": self.now,
            "scout_id": scout_id,
            "request_id": request_id,
        }
        request = self.ledger.get_scout_request(request_id) if request_id is not None else None
        self.ledger.create_scout(
            {
                "lens": request["lens"] if request else "state",
                "id": scout_id,
                "project": project,
                "model": "p/m",
                "request_id": request_id,
                "report_path": "/report",
            }
        )

        def finish() -> int:
            failed = project in self.scout_fail
            count = self.scout_drafts.get(project, 0)
            outcome = "failed" if failed else "succeeded"
            drafts = [f"task-{number}" for number in range(count)]
            self.ledger.finish_scout(
                scout_id,
                outcome=outcome,
                cost_usd=0.0,
                tokens_in=0,
                tokens_out=0,
                drafts_written=drafts,
            )
            payload = {
                "scout_id": scout_id,
                "outcome": outcome,
                "written_task_ids": drafts,
                "written_paths": [],
                "rulings": [],
                "skipped_duplicates": [],
                "report_path": "/report",
                "usage": {},
                "error": "model failed" if failed else None,
            }
            output.write_text(json.dumps(payload) + "\n")
            return 1 if failed else 0

        child = FakeChild(self, 1000 + len(self.children), finish, project)
        self.scout_spawned.append(entry)
        self.children.append(child)
        self.max_alive = max(self.max_alive, len(self.alive()))
        return child


@pytest.fixture
def harness(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setattr(supervisor, "_read_child_stat", lambda pid: ("S", str(pid)))
    return Harness(ledger)


def supervise(
    ledger: Ledger, data_dir: Path, harness: Harness, **overrides: Any
) -> tuple[SupervisorResult, list[dict[str, Any]]]:
    notices: list[dict[str, Any]] = []
    options: dict[str, Any] = {
        "scope": ProjectScope.of(),
        "max_parallel": 2,
        "watch": False,
        "max_runs": None,
        "poll_s": 30.0,
        "stop_file": data_dir / "worker.stop",
        "skip": lambda row: None,
        "as_json": True,
        "spawn": harness.spawn,
        **overrides,
    }
    harness.run_config = options.get("run_config", {})
    result = Supervisor(
        ledger=ledger,
        data_dir=data_dir,
        notify=notices.append,
        controller=FakeController(),
        sleep=harness.sleep,
        clock=harness.clock,
        **options,
    ).run()
    return result, notices


def events(output: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


def queue(ledger: Ledger, project: Path, task: str, priority: str = "normal") -> str:
    return ledger.upsert_task(make_spec(project, task, priority=priority), "")


class TestSlots:
    def test_disjoint_siblings_fill_two_slots(self, ledger, data_dir, harness, tmp_path):
        for id in ("a", "b", "c"):
            spec = make_spec(tmp_path, id, paths=(f"{id}/**",))
            ledger.upsert_task(spec, render_task(spec))
        result, notices = supervise(
            ledger, data_dir, harness, max_parallel=3, run_config={"max_runs_per_project": 2}
        )
        assert [entry["at"] for entry in harness.spawned] == [0, 0, 5]
        assert harness.max_alive == 2
        assert len(result.outcomes) == len(notices) == 3

    def test_fills_slots_with_different_projects_never_two_of_one(
        self, ledger: Ledger, data_dir: Path, harness: Harness, tmp_path: Path
    ):
        a, b, c = (str(tmp_path / name) for name in "abc")
        queue(ledger, Path(a), "a1", "critical")
        queue(ledger, Path(a), "a2")
        queue(ledger, Path(b), "b1")
        queue(ledger, Path(c), "c1")

        result, notices = supervise(ledger, data_dir, harness)

        assert [entry["project"] for entry in harness.spawned] == [a, b, a, c]
        assert harness.max_alive == 2
        assert [o["status"] for o in result.outcomes] == ["landed"] * 4
        assert len(notices) == 4
        assert ledger.list_tasks(status="queued") == []
        assert not result.stopped and result.paused is None

    def test_resumable_runs_go_first(
        self, ledger: Ledger, data_dir: Path, harness: Harness, tmp_path: Path
    ):
        a, b = str(tmp_path / "a"), str(tmp_path / "b")
        back = queue(ledger, Path(a), "back")
        ledger.create_run(run_row("r-a", back, a, status="awaiting_approval"))
        ledger.hand_back("r-a", "implement")
        queue(ledger, Path(b), "b1", "critical")

        result, _ = supervise(ledger, data_dir, harness, max_parallel=1)

        assert harness.spawned[0] == {"project": a, "at": 0.0, "resumed": "r-a"}
        assert harness.spawned[1]["project"] == b
        assert [o["run_id"] for o in result.outcomes][0] == "r-a"

    def test_started_event_names_the_resumed_run(
        self,
        ledger: Ledger,
        data_dir: Path,
        harness: Harness,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        a = str(tmp_path / "a")
        back = queue(ledger, Path(a), "back")
        ledger.create_run(run_row("r-a", back, a, status="failed"))
        ledger.hand_back("r-a", "verify")

        supervise(ledger, data_dir, harness)

        started = [e["started"] for e in events(capsys.readouterr().out) if "started" in e]
        assert [(s["project"], s["run_id"], s["pid"]) for s in started] == [(a, "r-a", 1000)]

    @pytest.mark.parametrize(
        ("scope", "expected"),
        [
            (lambda a, b, c: ProjectScope.of([a, b]), "ab"),
            (lambda a, b, c: ProjectScope.of(None, [a]), "bc"),
        ],
    )
    def test_serves_only_its_scope(
        self,
        ledger: Ledger,
        data_dir: Path,
        harness: Harness,
        tmp_path: Path,
        scope: Callable[..., ProjectScope],
        expected: str,
    ):
        projects = {name: str(tmp_path / name) for name in "abc"}
        for name, project in projects.items():
            queue(ledger, Path(project), f"{name}1")

        supervise(ledger, data_dir, harness, scope=scope(*projects.values()), max_parallel=3)

        assert sorted(entry["project"] for entry in harness.spawned) == [
            projects[name] for name in expected
        ]
        left = {task["project"] for task in ledger.list_tasks(status="queued")}
        assert left == {projects[name] for name in "abc" if name not in expected}

    def test_max_runs_limits_the_children_even_when_watching(
        self, ledger: Ledger, data_dir: Path, harness: Harness, tmp_path: Path
    ):
        queue(ledger, tmp_path / "a", "a1")
        queue(ledger, tmp_path / "b", "b1")

        result, _ = supervise(ledger, data_dir, harness, watch=True, max_runs=1)

        assert len(harness.spawned) == 1 and len(result.outcomes) == 1


class TestIdle:
    @pytest.mark.parametrize("stage", ["implement", "verify"])
    def test_waits_while_held_and_picks_up_a_handed_back_run(
        self, ledger: Ledger, data_dir: Path, harness: Harness, tmp_path: Path, stage: str
    ):
        a = str(tmp_path / "a")
        parked = queue(ledger, Path(a), "parked")
        ledger.set_task_status(parked, "awaiting_approval")
        ledger.create_run(run_row("r1", parked, a, status="awaiting_approval"))
        held = queue(ledger, Path(a), "held")
        # approve (a plan) or retry hands the run back at tick 40; stop at tick 200.
        harness.hooks[40] = lambda: ledger.hand_back("r1", stage)
        harness.hooks[200] = lambda: (data_dir / "worker.stop").write_text("stop\n")

        result, _ = supervise(ledger, data_dir, harness, watch=True)

        assert harness.ticks == 200  # it waited instead of exiting
        first, second = harness.spawned
        assert first["resumed"] == "r1" and 40 <= first["at"] <= 71
        assert second["task_id"] == held
        assert result.stopped and not (data_dir / "worker.stop").exists()

    def test_drain_mode_exits_when_only_held_tasks_remain(
        self, ledger: Ledger, data_dir: Path, harness: Harness, tmp_path: Path
    ):
        a = str(tmp_path / "a")
        parked = queue(ledger, Path(a), "parked")
        ledger.create_run(run_row("r1", parked, a, status="awaiting_approval"))
        queue(ledger, Path(a), "held")

        result, _ = supervise(ledger, data_dir, harness)

        assert harness.spawned == [] and harness.ticks == 0
        assert result.outcomes == []

    def test_paused_queue_starts_nothing_until_resumed(
        self,
        ledger: Ledger,
        data_dir: Path,
        harness: Harness,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        queue(ledger, tmp_path / "a", "a1")
        ledger.pause_queue("quota")
        harness.hooks[100] = ledger.resume_queue
        harness.hooks[200] = lambda: (data_dir / "worker.stop").write_text("stop\n")

        supervise(ledger, data_dir, harness, watch=True)

        assert len(harness.spawned) == 1 and harness.spawned[0]["at"] >= 100
        paused = [e for e in events(capsys.readouterr().out) if "paused" in e]
        assert len(paused) == 1 and paused[0]["paused"]["reason"] == "quota"

    def test_skipped_task_is_reported_once_and_never_started(
        self,
        ledger: Ledger,
        data_dir: Path,
        harness: Harness,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        a1 = queue(ledger, tmp_path / "a", "a1", "critical")
        queue(ledger, tmp_path / "b", "b1")
        harness.hooks[120] = lambda: (data_dir / "worker.stop").write_text("stop\n")

        def skip(row: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
            if row["task_id"] == a1:
                return {"reason": "quota:openai-codex", "until": 1.0}, "openai-codex out of quota"
            return None

        result, _ = supervise(ledger, data_dir, harness, watch=True, skip=skip)

        assert [entry["project"] for entry in harness.spawned] == [str(tmp_path / "b")]
        skipped = [e for e in events(capsys.readouterr().out) if "skipped" in e]
        assert skipped == [{"skipped": a1, "reason": "quota:openai-codex", "until": 1.0}]
        assert result.skipped == {a1}

    def test_a_child_without_a_run_backs_its_project_off(
        self, ledger: Ledger, data_dir: Path, harness: Harness, tmp_path: Path
    ):
        a = str(tmp_path / "a")
        queue(ledger, Path(a), "a1")
        harness.no_run.add(a)
        harness.hooks[400] = lambda: (data_dir / "worker.stop").write_text("stop\n")

        result, _ = supervise(ledger, data_dir, harness, watch=True)

        starts = [entry["at"] for entry in harness.spawned]
        assert len(starts) == 2 and starts[1] - starts[0] >= NO_RUN_BACKOFF_S
        assert result.outcomes == []


class TestScoutSupervisor:
    def config(self, *projects: Path) -> dict[str, Any]:
        config = configured_test_config()
        config["routing"]["scout"].update(
            enabled=True,
            focus_projects=[str(project) for project in projects],
            paused_projects=[],
        )
        return config

    def test_scout_uses_spare_slot_and_emits_events_and_notice(
        self,
        ledger: Ledger,
        data_dir: Path,
        harness: Harness,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        run_project, scout_project = tmp_path / "run", tmp_path / "scout"
        queue(ledger, run_project, "task")
        harness.scout_drafts[str(scout_project)] = 2
        harness.hooks[2] = lambda: (data_dir / "worker.stop").write_text("stop\n")
        monkeypatch.setattr(supervisor, "scout_gate", lambda *args, **kwargs: None)
        alerts: list[str] = []

        result, _ = supervise(
            ledger,
            data_dir,
            harness,
            watch=True,
            scout_config=self.config(scout_project),
            spawn_scout=harness.spawn_scout,
            alert=alerts.append,
        )

        assert harness.spawned[0]["project"] == str(run_project)
        assert harness.scout_spawned[0]["project"] == str(scout_project)
        assert harness.max_alive == 2 and len(result.outcomes) == 1
        assert alerts == ["2 drafts for scout — helmraiser drafts"]
        lines = events(capsys.readouterr().out)
        assert len([line for line in lines if "scout_started" in line]) == 1
        finished = [line["scout_finished"] for line in lines if "scout_finished" in line]
        assert len(finished) == 1 and finished[0]["drafts"] == 2
        assert ledger.get_scout(harness.scout_spawned[0]["scout_id"])["outcome"] == "succeeded"

    def test_scout_is_watch_only(
        self,
        ledger: Ledger,
        data_dir: Path,
        harness: Harness,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(supervisor, "scout_gate", lambda *args, **kwargs: None)
        supervise(
            ledger,
            data_dir,
            harness,
            watch=False,
            scout_config=self.config(tmp_path / "scout"),
            spawn_scout=harness.spawn_scout,
        )
        assert harness.scout_spawned == []

    def test_queue_pause_stops_scouts_until_resumed(
        self,
        ledger: Ledger,
        data_dir: Path,
        harness: Harness,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        project = tmp_path / "scout"
        ledger.pause_queue("production tunnel")
        harness.hooks[2] = ledger.resume_queue
        harness.hooks[4] = lambda: (data_dir / "worker.stop").write_text("stop\n")
        monkeypatch.setattr(supervisor, "scout_gate", lambda *args, **kwargs: None)

        supervise(
            ledger,
            data_dir,
            harness,
            watch=True,
            poll_s=1.0,
            scout_config=self.config(project),
            spawn_scout=harness.spawn_scout,
        )

        assert len(harness.scout_spawned) == 1
        assert harness.scout_spawned[0]["at"] >= 2

    def test_failed_scout_backs_off_its_project_then_retries_while_others_continue(
        self,
        ledger: Ledger,
        data_dir: Path,
        harness: Harness,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        bad, good = tmp_path / "bad", tmp_path / "good"
        harness.scout_fail.add(str(bad))
        harness.hooks[290] = lambda: harness.scout_fail.remove(str(bad))
        harness.hooks[340] = lambda: (data_dir / "worker.stop").write_text("stop\n")

        refresh_flags: list[bool] = []

        def gate(
            repo: Path, ledger: Ledger, config: dict[str, Any], now: float, *, refresh: bool
        ) -> None:
            refresh_flags.append(refresh)
            if any(row["outcome"] == "succeeded" for row in ledger.list_scouts(str(repo))):
                raise supervisor.ScoutError("already succeeded")

        monkeypatch.setattr(supervisor, "scout_gate", gate)
        alerts: list[str] = []

        result, _ = supervise(
            ledger,
            data_dir,
            harness,
            watch=True,
            max_parallel=1,
            poll_s=10.0,
            scout_config=self.config(bad, good),
            spawn_scout=harness.spawn_scout,
            alert=alerts.append,
        )

        bad_starts = [
            entry["at"] for entry in harness.scout_spawned if entry["project"] == str(bad)
        ]
        assert len(bad_starts) == 2
        assert bad_starts[1] - bad_starts[0] >= PROJECT_ERROR_BACKOFF_S
        assert any(entry["project"] == str(good) for entry in harness.scout_spawned)
        assert result.stopped and result.outcomes == []
        assert ledger.get_scout(harness.scout_spawned[0]["scout_id"])["outcome"] == "failed"
        assert ledger.list_scouts(str(bad))[0]["outcome"] == "succeeded"
        assert any(message == "bad: model failed" for message in alerts)
        # The worker checks every tick from cached usage; only the scout refreshes.
        assert refresh_flags and not any(refresh_flags)


class TestStop:
    def test_stop_lets_running_children_finish_and_starts_no_more(
        self,
        ledger: Ledger,
        data_dir: Path,
        harness: Harness,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        harness.duration = 50.0
        queue(ledger, tmp_path / "a", "a1")
        queue(ledger, tmp_path / "b", "b1")
        c1 = queue(ledger, tmp_path / "c", "c1")
        harness.hooks[3] = lambda: (data_dir / "worker.stop").write_text("stop\n")

        result, _ = supervise(ledger, data_dir, harness, watch=True)

        assert [entry["project"] for entry in harness.spawned] == [
            str(tmp_path / "a"),
            str(tmp_path / "b"),
        ]
        assert harness.now >= 50.0 and len(result.outcomes) == 2
        assert [t["task_id"] for t in ledger.list_tasks(status="queued")] == [c1]
        assert result.stopped and not (data_dir / "worker.stop").exists()
        stopping = [e for e in events(capsys.readouterr().out) if "stopping" in e]
        assert stopping == [{"stopping": True, "running": 2}]

    def test_a_pending_stop_starts_nothing(
        self, ledger: Ledger, data_dir: Path, harness: Harness, tmp_path: Path
    ):
        queue(ledger, tmp_path / "a", "a1")
        (data_dir / "worker.stop").write_text("stop\n")

        result, _ = supervise(ledger, data_dir, harness, watch=True)

        assert harness.spawned == [] and result.stopped


class TestAdoptedCompletion:
    @pytest.mark.parametrize("identity", [None, ("S", "reused"), ("S", "token")])
    @pytest.mark.parametrize("status", ["running", "landed", "awaiting_approval"])
    def test_reaps_once_and_reconciles_even_during_backoff(
        self, ledger, data_dir, harness, tmp_path, monkeypatch, capsys, identity, status
    ):
        from helmraiser.storage.state import StateManager

        project = tmp_path / "project"
        task_id = queue(ledger, project, "task")
        ledger.create_run(run_row("r", task_id, str(project), status=status))
        state = StateManager(project / ".helmraiser" / "state.db")
        state.acquire_lock("r", "task", 123)
        monkeypatch.setattr("helmraiser.loop.reconcile.is_process_alive", lambda pid: True)
        monkeypatch.setattr(supervisor, "_read_child_stat", lambda pid: identity)
        monkeypatch.setattr(os, "waitpid", lambda pid, flags: (pid, 256))
        child = TestHandoff().child(data_dir, str(project))
        result, notices = supervise(ledger, data_dir, harness, children=[child])
        finished = [e["finished"] for e in events(capsys.readouterr().out) if "finished" in e]
        assert len(finished) == 1 and finished[0]["exit_code"] == 1
        assert finished[0]["started_at"] == 42.0
        assert result.outcomes == notices == []
        assert harness.spawned == []
        assert ledger.get_run("r")["status"] == ("failed" if status == "running" else status)
        assert not child.output.exists()

    def test_saved_output_and_notification_once(
        self, ledger, data_dir, harness, monkeypatch, capsys
    ):
        child = TestHandoff().child(data_dir)
        child.output.parent.mkdir()
        outcome = {"run_id": "r", "status": "landed", "iterations": 1, "cost_usd": 0.25}
        child.output.write_text(json.dumps(outcome) + "\n")
        monkeypatch.setattr(supervisor, "_read_child_stat", lambda pid: ("S", "token"))
        calls = []
        monkeypatch.setattr(os, "waitpid", lambda pid, flags: calls.append(pid) or (pid, 0))
        result, notices = supervise(ledger, data_dir, harness, children=[child])
        assert result.outcomes == notices == [outcome]
        assert calls == [123]
        assert len([e for e in events(capsys.readouterr().out) if "finished" in e]) == 1
        assert not child.output.exists()


class TestOutput:
    def test_human_lines(
        self,
        ledger: Ledger,
        data_dir: Path,
        harness: Harness,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        a, b = str(tmp_path / "a"), str(tmp_path / "b")
        a1 = queue(ledger, Path(a), "a1", "critical")
        queue(ledger, Path(b), "b1")
        harness.no_run.add(b)

        supervise(ledger, data_dir, harness, as_json=False)

        lines = capsys.readouterr().out.splitlines()
        assert lines[:2] == [f"started {a} (pid 1000)", f"started {b} (pid 1001)"]
        assert f"landed {a1} run run-0 — 1 iteration(s), $0.2500, 1.0 min" in lines
        assert f"{b}: error: nothing to run" in lines
        assert f"{b}: pid 1001 exited (1) without a run; not tried again for 5 min" in lines

    def test_reconciles_phantoms_only_in_its_scope(
        self,
        ledger: Ledger,
        data_dir: Path,
        harness: Harness,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        a, b = str(tmp_path / "a"), str(tmp_path / "b")
        ledger.create_run(run_row("ghost-a", queue(ledger, Path(a), "a1"), a))
        ledger.create_run(run_row("ghost-b", queue(ledger, Path(b), "b1"), b))

        supervise(ledger, data_dir, harness, scope=ProjectScope.of(None, [b]))

        assert ledger.get_run("ghost-a")["status"] == "failed"
        assert ledger.get_run("ghost-b")["status"] == "running"
        reconciled = [e for e in events(capsys.readouterr().out) if "reconciled" in e]
        assert [item["run_id"] for item in reconciled[0]["reconciled"]] == ["ghost-a"]


class TestHandoff:
    def test_sibling_handoff_preserves_ids_and_rejects_scout_mix(self, data_dir, monkeypatch):
        one = self.child(data_dir).restart_dict()
        two = {
            **one,
            "pid": 124,
            "run_id": "second",
            "output": str(data_dir / "worker-children/second.jsonl"),
        }

        def restore(entries):
            monkeypatch.setenv(
                "HELMRAISER_RESTART_CHILDREN", json.dumps({"pid": os.getpid(), "children": entries})
            )
            return supervisor.restore_children(data_dir)

        assert [c.run_id for c in restore([one, two])] == ["run-id", "second"]
        assert len(restore([one, {**two, "run_id": "run-id"}])) == 1
        assert len(restore([one, {**two, "run_id": []}])) == 1  # unhashable: skipped
        assert len(restore([one, {**two, "run_id": {}}])) == 1
        assert (
            len(restore([one, {**two, "kind": "scout", "scout_id": "scout", "run_id": None}])) == 1
        )

    def test_orphan_sibling_not_hidden_by_live_child(self, ledger, data_dir, harness, tmp_path):
        project = tmp_path / "project"
        for id in ("live", "orphan"):
            spec = make_spec(project, id, status="running", paths=(f"{id}/**",))
            ledger.upsert_task(spec, render_task(spec))
            ledger.create_run(run_row(id, spec.task_id, str(project)))
        child = self.child(data_dir, str(project))
        child.run_id = "live"
        child.process = FakeChild(harness, 123, lambda: 0, str(project))
        instance = Supervisor(
            ledger=ledger,
            data_dir=data_dir,
            scope=ProjectScope.of(),
            max_parallel=2,
            watch=False,
            max_runs=None,
            poll_s=1,
            stop_file=data_dir / "worker.stop",
            spawn=harness.spawn,
            skip=lambda row: None,
            notify=lambda row: None,
            as_json=True,
            children=[child],
            controller=FakeController(),
        )
        instance._reconcile(ProjectScope.of())
        assert ledger.get_run("live")["status"] == "running"
        assert ledger.get_run("orphan")["status"] == "failed"
        # A child without an outcome must be reconciled even during its project's backoff.
        child.process.finish = lambda: 1
        harness.now = 5
        instance._reap()
        assert ledger.get_run("live")["status"] == "failed"
        assert str(project) in instance.backoff

    def child(self, data_dir, project="/project", kind="run"):
        return supervisor.Child(
            project,
            supervisor.AdoptedProcess(123, "token"),
            data_dir / "worker-children" / "output.jsonl",
            42.0,
            run_id="run-id" if kind == "run" else None,
            kind=kind,
            scout_id="scout-id" if kind == "scout" else None,
            request_id=7 if kind == "scout" else None,
            start_token="token",
            code_commit=OLD.commit,
        )

    @pytest.mark.parametrize("recorded", [True, False])
    def test_round_trip_and_consumption(self, data_dir, monkeypatch, recorded):
        children = [self.child(data_dir), self.child(data_dir, "/scout", "scout")]
        children[1].process.pid = 124
        payload = [child.restart_dict() for child in children]
        if recorded:
            (data_dir / "worker.json").write_text(
                json.dumps({"pid": os.getpid(), "restart_children": payload})
            )
        monkeypatch.setenv(
            "HELMRAISER_RESTART_CHILDREN", json.dumps({"pid": os.getpid(), "children": payload})
        )
        assert len(supervisor.read_restart_children(data_dir)) == 2
        restored = supervisor.restore_children(data_dir)
        assert [c.restart_dict() for c in restored] == payload
        assert all(isinstance(c.process, supervisor.AdoptedProcess) for c in restored)
        assert supervisor.read_restart_children(data_dir) == []
        assert "HELMRAISER_RESTART_CHILDREN" not in os.environ
        assert set(children[0].as_dict()) == {"pid", "project", "run_id", "kind", "started_at"}

    def test_foreign_owner_and_malformed_entries(self, data_dir, monkeypatch):
        good = self.child(data_dir).restart_dict()
        record = {"pid": 1, "restart_children": [good]}
        (data_dir / "worker.json").write_text(json.dumps(record))
        monkeypatch.setenv(
            "HELMRAISER_RESTART_CHILDREN", json.dumps({"pid": 1, "children": [good]})
        )
        assert supervisor.restore_children(data_dir) == []
        assert json.loads((data_dir / "worker.json").read_text()) == record
        bad = [
            None,
            {},
            {**good, "pid": True},
            {**good, "kind": "other"},
            {**good, "output": "/tmp/unsafe"},
            {**good, "started_at": "42"},
        ]
        monkeypatch.setenv(
            "HELMRAISER_RESTART_CHILDREN",
            json.dumps({"pid": os.getpid(), "children": bad + [good, good, {**good, "pid": 456}]}),
        )
        assert len(supervisor.restore_children(data_dir)) == 1

    def test_run_id_requires_matching_lock(self, ledger, data_dir, harness, tmp_path):
        from helmraiser.storage.state import StateManager

        project = tmp_path / "project"
        task_id = queue(ledger, project, "task")
        ledger.create_run(run_row("r", task_id, str(project)))
        instance = Supervisor(
            ledger=ledger,
            data_dir=data_dir,
            scope=ProjectScope.of(),
            max_parallel=2,
            watch=False,
            max_runs=None,
            poll_s=1,
            stop_file=data_dir / "worker.stop",
            spawn=harness.spawn,
            skip=lambda row: None,
            notify=lambda row: None,
            as_json=True,
        )
        child = self.child(data_dir, str(project))
        child.run_id = None
        assert instance._child_run_id(child) is None
        path = project / ".helmraiser" / "state.db"
        assert not path.exists()
        state = StateManager(path)
        state.acquire_lock("r", "task", 456)
        assert instance._child_run_id(child) is None
        state.release_lock("r", "task")
        state.acquire_lock("r", "task", 123)
        assert instance._child_run_id(child) == "r"


class TestPublish:
    def test_the_recorded_worker_lists_its_children(
        self, ledger: Ledger, data_dir: Path, harness: Harness, tmp_path: Path
    ):
        a = str(tmp_path / "a")
        queue(ledger, Path(a), "a1")
        (data_dir / "worker.json").write_text(json.dumps({"pid": os.getpid()}))
        seen: list[dict[str, Any]] = []
        harness.hooks[1] = lambda: seen.append(
            json.loads((data_dir / "worker.active.json").read_text())
        )

        supervise(ledger, data_dir, harness, max_parallel=3)

        (during,) = seen
        assert during["pid"] == os.getpid() and during["max_parallel_runs"] == 3
        assert [(c["project"], c["pid"]) for c in during["children"]] == [(a, 1000)]
        after = json.loads((data_dir / "worker.active.json").read_text())
        assert after["children"] == []

    def test_a_foreground_supervisor_leaves_the_record_alone(
        self, ledger: Ledger, data_dir: Path, harness: Harness, tmp_path: Path
    ):
        queue(ledger, tmp_path / "a", "a1")
        (data_dir / "worker.json").write_text(json.dumps({"pid": 1}))

        supervise(ledger, data_dir, harness)

        assert not (data_dir / "worker.active.json").exists()
        assert json.loads((data_dir / "worker.json").read_text()) == {"pid": 1}

    @pytest.mark.parametrize(
        ("stop", "max_runs", "reason"),
        [(True, None, "stopped"), (False, 1, "max_runs"), (False, None, "drained")],
    )
    def test_the_recorded_worker_records_its_clean_end(
        self,
        ledger: Ledger,
        data_dir: Path,
        harness: Harness,
        tmp_path: Path,
        stop: bool,
        max_runs: int | None,
        reason: str,
    ):
        queue(ledger, tmp_path / "a", "a1")
        (data_dir / "worker.json").write_text(json.dumps({"pid": os.getpid()}))
        if stop:
            harness.hooks[2] = lambda: (data_dir / "worker.stop").write_text("stop\n")

        supervise(ledger, data_dir, harness, watch=stop, max_runs=max_runs)

        record = json.loads((data_dir / "worker.json").read_text())
        assert record["pid"] == os.getpid() and record["exit"]["reason"] == reason


def queue_real(ledger: Ledger, project: Path, task: str) -> str:
    """A queued task whose snapshot parses, so the real ``_task_skip`` can load it (and
    the project's ``project.toml``)."""
    project.mkdir(parents=True, exist_ok=True)
    return ledger.upsert_task(make_spec(project, task), TASK.format(task_id=task, project=project))


def break_project_toml(project: Path) -> Path:
    path = project / ".helmraiser" / "project.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[[services]]\nname = "db"\nimage = "postgres:16"\nport_mapping = 1\n')
    return path


class TestProjectErrors:
    def test_sibling_success_does_not_clear_error_or_allow_scout(
        self, ledger, data_dir, harness, tmp_path, monkeypatch
    ):
        for id in ("a", "b"):
            spec = make_spec(tmp_path, id, paths=(f"{id}/**",))
            ledger.upsert_task(spec, render_task(spec))
        harness.run_config = {"max_runs_per_project": 2}
        instance = Supervisor(
            ledger=ledger,
            data_dir=data_dir,
            scope=ProjectScope.of(),
            max_parallel=3,
            watch=True,
            max_runs=None,
            poll_s=1,
            stop_file=data_dir / "worker.stop",
            spawn=harness.spawn,
            skip=lambda row: None,
            notify=lambda row: None,
            as_json=True,
            controller=FakeController(),
            clock=harness.clock,
            wall_clock=harness.clock,
            run_config=harness.run_config,
            scout_config=TestScoutSupervisor().config(tmp_path),
            spawn_scout=harness.spawn_scout,
        )
        monkeypatch.setattr(supervisor, "scout_gate", lambda *args, **kwargs: None)
        instance._fill()
        assert len(instance.children) == 2
        assert not harness.scout_spawned
        one, two = instance.children
        harness.now = 1
        one.process.finish_at = 1
        one.process.finish = lambda: 1
        instance._reap()
        assert ledger.get_run(one.run_id)["status"] == "failed"
        assert instance.errors[str(tmp_path)]["stage"] == "run"
        assert instance._fill_scouts() == 0
        harness.now = 5
        instance._reap()
        assert ledger.get_run(two.run_id)["status"] == "landed"
        assert str(tmp_path) in instance.errors
        assert instance.backoff[str(tmp_path)] > harness.now
        harness.now = 302
        spec = make_spec(tmp_path, "later", paths=("later/**",))
        ledger.upsert_task(spec, render_task(spec))
        assert instance._fill() == 1
        harness.now = 307
        instance._reap()
        assert str(tmp_path) not in instance.errors

    def test_an_invalid_project_toml_holds_only_its_project_and_is_reported_once(
        self,
        ledger: Ledger,
        data_dir: Path,
        harness: Harness,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        """The 2026-09-30 incident: a project's project.toml gained a key the worker's
        code did not know, and the whole worker died."""
        a, b = tmp_path / "a", tmp_path / "b"
        queue_real(ledger, a, "a1")
        b1 = queue_real(ledger, b, "b1")
        broken = break_project_toml(a)
        (data_dir / "worker.json").write_text(json.dumps({"pid": os.getpid()}))
        config = configured_test_config()
        alerts: list[str] = []
        seen: list[dict[str, Any]] = []
        harness.hooks[10] = lambda: seen.append(
            json.loads((data_dir / "worker.active.json").read_text())
        )
        harness.hooks[200] = broken.unlink  # fixed; tried again after the backoff
        harness.hooks[500] = lambda: (data_dir / "worker.stop").write_text("stop\n")

        def skip(row: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
            task = commands._task_from_row(row, config)
            return commands._task_skip(task, ledger, config, True)

        result, _ = supervise(
            ledger, data_dir, harness, watch=True, poll_s=60.0, skip=skip, alert=alerts.append
        )

        spawned = [(entry["project"], entry["at"]) for entry in harness.spawned]
        assert spawned[0] == (str(b), 0.0)
        assert spawned[1][0] == str(a) and spawned[1][1] >= PROJECT_ERROR_BACKOFF_S
        assert ledger.get_task(b1)["status"] == "landed" and len(result.outcomes) == 2
        errors = [
            e["project_error"] for e in events(capsys.readouterr().out) if "project_error" in e
        ]
        assert {e["project"] for e in errors} == {str(a)}
        assert errors[0]["stage"] == "task"
        assert "unknown key(s) port_mapping" in errors[0]["error"]
        assert alerts == [f"a: {errors[0]['error']}"]  # once, however often it recurs
        (during,) = seen
        assert [(e["project"], e["stage"]) for e in during["errors"]] == [(str(a), "task")]
        after = json.loads((data_dir / "worker.active.json").read_text())
        assert after["errors"] == []  # the project works again

    def test_a_distinct_error_is_notified_again(
        self, ledger: Ledger, data_dir: Path, harness: Harness, tmp_path: Path
    ):
        a = tmp_path / "a"
        queue(ledger, a, "a1")
        messages = iter(["first problem", "second problem"])
        alerts: list[str] = []
        harness.hooks[700] = lambda: (data_dir / "worker.stop").write_text("stop\n")

        def skip(row: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
            raise ValueError(next(messages, "second problem"))

        supervise(ledger, data_dir, harness, watch=True, skip=skip, alert=alerts.append)

        assert alerts == ["a: ValueError: first problem", "a: ValueError: second problem"]
        assert harness.spawned == []

    def test_a_failing_reconcile_is_reported_per_project(
        self,
        ledger: Ledger,
        data_dir: Path,
        harness: Harness,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        a, b = str(tmp_path / "a"), str(tmp_path / "b")
        ledger.create_run(run_row("ghost-a", queue(ledger, Path(a), "a1"), a))
        ledger.create_run(run_row("ghost-b", queue(ledger, Path(b), "b1"), b))
        real = supervisor.find_phantom_runs

        def find(ledger: Ledger, scope: ProjectScope | None = None, **kwargs: Any) -> Any:
            if scope is not None and scope.matches(a):
                raise OSError("state.db is unreadable")
            return real(ledger, scope, **kwargs)

        monkeypatch.setattr(supervisor, "find_phantom_runs", find)

        supervise(ledger, data_dir, harness)

        assert ledger.get_run("ghost-a")["status"] == "running"
        assert ledger.get_run("ghost-b")["status"] == "failed"
        lines = events(capsys.readouterr().out)
        (error,) = [e["project_error"] for e in lines if "project_error" in e]
        assert error["project"] == a and error["stage"] == "reconcile"
        assert error["error"] == "OSError: state.db is unreadable"

    def test_a_child_that_fails_without_a_run_is_a_project_error(
        self, ledger: Ledger, data_dir: Path, harness: Harness, tmp_path: Path
    ):
        a = str(tmp_path / "a")
        queue(ledger, Path(a), "a1")
        harness.no_run.add(a)
        alerts: list[str] = []
        harness.hooks[400] = lambda: (data_dir / "worker.stop").write_text("stop\n")

        supervise(ledger, data_dir, harness, watch=True, alert=alerts.append)

        assert len(harness.spawned) == 2 and alerts == ["a: nothing to run"]

    def test_a_spawn_that_raises_holds_only_its_project(
        self, ledger: Ledger, data_dir: Path, harness: Harness, tmp_path: Path
    ):
        a, b = str(tmp_path / "a"), str(tmp_path / "b")
        queue(ledger, Path(a), "a1", "critical")
        queue(ledger, Path(b), "b1")
        alerts: list[str] = []

        def spawn(project: str, output: Path) -> FakeChild:
            if project == a:
                raise OSError("no such interpreter")
            return harness.spawn(project, output)

        result, _ = supervise(ledger, data_dir, harness, spawn=spawn, alert=alerts.append)

        assert [entry["project"] for entry in harness.spawned] == [b]
        assert alerts == ["a: OSError: no such interpreter"] and len(result.outcomes) == 1
        assert not any((data_dir / "worker-children").iterdir())


OLD = CodeVersion(commit="a" * 40, tree="1" * 40)
NEW = CodeVersion(commit="b" * 40, tree="2" * 40)


class Execed(Exception):
    """Stands in for ``os.execv``: the supervisor's loop never continues after it."""


class FakeCode:
    """The installed code's version, and a fake re-exec that records when it ran."""

    def __init__(self, harness: Harness, problem: str | None = None):
        self.harness = harness
        self.version = OLD
        self.problem = problem
        self.execs: list[dict[str, Any]] = []

    def __call__(self) -> CodeVersion:
        return self.version

    def reexec(self, new: CodeVersion, announce: Callable[[], None]) -> str:
        self.execs.append(
            {"at": self.harness.now, "alive": len(self.harness.alive()), "to": new.commit}
        )
        if self.problem is not None:
            return self.problem
        announce()
        raise Execed


class TestRestartOnNewCode:
    @pytest.mark.parametrize("parallel", [2, 3])
    def test_reexecs_immediately_with_running_children(
        self,
        ledger: Ledger,
        data_dir: Path,
        harness: Harness,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        parallel: int,
    ):
        harness.duration = 50.0
        harness.adopt_waits(monkeypatch)
        monkeypatch.setenv("HELMRAISER_CODE_SNAPSHOT", OLD.commit)
        for name in "ab":
            queue(ledger, tmp_path / name, f"{name}1")
        queue(ledger, tmp_path / "a", "a2")
        (data_dir / "worker.json").write_text(json.dumps({"pid": os.getpid()}))
        code = FakeCode(harness)
        alerts: list[str] = []
        harness.hooks[10] = lambda: (
            setattr(code, "version", NEW),
            queue(ledger, tmp_path / "c", "c1", "critical"),
        )

        with pytest.raises(Execed):
            supervise(
                ledger,
                data_dir,
                harness,
                watch=True,
                max_parallel=parallel,
                poll_s=5.0,
                code_version=code,
                reexec=code.reexec,
                alert=alerts.append,
            )

        # Re-exec happens in the change-detection tick, before any more dispatch.
        assert [entry["project"] for entry in harness.spawned] == [
            str(tmp_path / "a"),
            str(tmp_path / "b"),
        ]
        ((execed),) = code.execs
        assert execed == {"alive": 2, "at": 10.0, "to": NEW.commit}
        assert alerts == ["worker restarted on new code (aaaaaaaa → bbbbbbbb)"]
        lines = events(capsys.readouterr().out)
        changed = [e["code_changed"] for e in lines if "code_changed" in e]
        assert changed == [{"from": OLD.commit, "to": NEW.commit, "running": 2}]
        assert {"restarting": {"from": OLD.commit, "to": NEW.commit}} in lines
        record = json.loads((data_dir / "worker.json").read_text())
        assert record["pid"] == os.getpid() and "exit" not in record
        assert record["restarted"]["from"] == OLD.commit
        assert record["restarted"]["to"] == NEW.commit
        handoff = record["restart_children"]
        assert len(handoff) == 2
        assert all(c["code_commit"] == OLD.commit and c["kind"] == "run" for c in handoff)
        assert [c["start_token"] for c in handoff] == ["1000", "1001"]
        adopted = supervisor.restore_children(data_dir)
        harness.duration = 5.0
        harness.hooks[100] = lambda: (data_dir / "worker.stop").write_text("stop\n")
        result, notices = supervise(
            ledger,
            data_dir,
            harness,
            children=adopted,
            watch=True,
            max_parallel=parallel,
            poll_s=5.0,
            code_version=code,
            reexec=code.reexec,
            running_code=NEW,
        )
        c_start = next(e["at"] for e in harness.spawned if e["project"] == str(tmp_path / "c"))
        assert c_start == (10.0 if parallel == 3 else 50.0)
        assert (
            next(e["at"] for e in harness.spawned if e.get("task_id", "").endswith(":a2")) == 50.0
        )
        assert len(result.outcomes) == len(notices) == 4
        assert len(code.execs) == 1
        finished = [e["finished"] for e in events(capsys.readouterr().out) if "finished" in e]
        assert len(finished) == 4
        assert all(not Path(c["output"]).exists() for c in handoff)

    def test_starts_from_the_running_snapshot_not_head(
        self, ledger: Ledger, data_dir: Path, harness: Harness
    ):
        """A worker on the snapshot of OLD restarts when HEAD is already NEW: the
        commit it runs is the snapshot's, not whatever HEAD is when it starts."""
        code = FakeCode(harness)
        code.version = NEW

        with pytest.raises(Execed):
            supervise(
                ledger,
                data_dir,
                harness,
                watch=True,
                poll_s=5.0,
                code_version=code,
                reexec=code.reexec,
                running_code=OLD,
            )

        ((execed),) = code.execs
        assert execed["to"] == NEW.commit

    def test_a_docs_only_commit_does_not_restart(
        self, ledger: Ledger, data_dir: Path, harness: Harness, tmp_path: Path
    ):
        queue(ledger, tmp_path / "a", "a1")
        code = FakeCode(harness)
        harness.hooks[3] = lambda: setattr(code, "version", CodeVersion("c" * 40, OLD.tree))
        harness.hooks[60] = lambda: (data_dir / "worker.stop").write_text("stop\n")

        supervise(
            ledger, data_dir, harness, watch=True, poll_s=5.0, code_version=code, reexec=code.reexec
        )

        assert code.execs == []

    def test_new_code_that_does_not_import_keeps_the_old_worker_serving(
        self,
        ledger: Ledger,
        data_dir: Path,
        harness: Harness,
        tmp_path: Path,
    ):
        harness.duration = 20.0
        queue(ledger, tmp_path / "a", "a1")
        queue(ledger, tmp_path / "b", "b1")
        code = FakeCode(harness, problem="the new code does not import: SyntaxError: bad")
        alerts: list[str] = []
        harness.hooks[2] = lambda: setattr(code, "version", NEW)
        harness.hooks[200] = lambda: (data_dir / "worker.stop").write_text("stop\n")

        result, _ = supervise(
            ledger,
            data_dir,
            harness,
            watch=True,
            max_parallel=1,
            poll_s=5.0,
            code_version=code,
            reexec=code.reexec,
            alert=alerts.append,
        )

        assert code.execs == [{"at": 5.0, "alive": 1, "to": NEW.commit}]
        assert "HELMRAISER_RESTART_CHILDREN" not in os.environ
        assert not supervisor.read_restart_children(data_dir)  # failed handoff cleared
        assert [entry["project"] for entry in harness.spawned] == [
            str(tmp_path / "a"),
            str(tmp_path / "b"),
        ]
        assert harness.spawned[1]["at"] >= 20.0 and len(result.outcomes) == 2
        assert alerts == [
            "worker not restarted on new code (bbbbbbbb): "
            "the new code does not import: SyntaxError: bad"
        ]

    def test_stop_wins_over_code_change(self, ledger, data_dir, harness, tmp_path):
        queue(ledger, tmp_path / "a", "a1")
        harness.duration = 20
        code = FakeCode(harness)
        harness.hooks[5] = lambda: (
            setattr(code, "version", NEW),
            (data_dir / "worker.stop").write_text("stop"),
        )
        result, _ = supervise(
            ledger, data_dir, harness, watch=True, poll_s=5, code_version=code, reexec=code.reexec
        )
        assert result.stopped and harness.now == 20
        assert code.execs == []

    def test_announce_then_exec_failure_clears_handoff(
        self, ledger, data_dir, harness, tmp_path, capsys
    ):
        queue(ledger, tmp_path / "a", "a1")
        (data_dir / "worker.json").write_text(json.dumps({"pid": os.getpid()}))
        code = FakeCode(harness)
        harness.duration = 20
        harness.hooks[5] = lambda: setattr(code, "version", NEW)
        harness.hooks[30] = lambda: (data_dir / "worker.stop").write_text("stop")

        def fail(new, announce):
            assert len(supervisor.read_restart_children(data_dir)) == 1
            announce()
            return "exec failed"

        result, _ = supervise(
            ledger, data_dir, harness, watch=True, poll_s=5, code_version=code, reexec=fail
        )
        assert result.stopped and len(result.outcomes) == 1
        record = json.loads((data_dir / "worker.json").read_text())
        assert record["restart_children"] is None
        assert record["restarted"]["to"] == NEW.commit
        assert len([e for e in events(capsys.readouterr().out) if "restart_failed" in e]) == 1

    def test_code_version_follows_the_package_tree_not_head(
        self, tmp_path: Path, git_init: Any, monkeypatch: pytest.MonkeyPatch
    ):
        repo = tmp_path / "repo"
        package = repo / "src" / "helmraiser"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        git_init(repo)

        def commit(path: Path, text: str) -> None:
            path.write_text(text)
            for args in (["add", "-A"], ["commit", "-q", "-m", path.name]):
                subprocess.run(["git", "-C", str(repo), *args], check=True)

        first = supervisor.code_version(repo)
        commit(repo / "README.md", "docs\n")
        docs = supervisor.code_version(repo)
        commit(package / "__init__.py", "X = 1\n")
        code = supervisor.code_version(repo)
        (package / "__init__.py").write_text("X = 'uncommitted'\n")  # never counts

        assert first is not None and docs is not None and code is not None
        assert docs.commit != first.commit and docs.tree == first.tree
        assert code.tree != first.tree
        assert supervisor.code_version(repo) == code
        assert supervisor.code_version(repo, first.commit) == first
        assert supervisor.code_version(tmp_path) is None  # not a git checkout
        # By default: the checkout HELMRAISER_SOURCE_REPO names (a worker on a snapshot).
        monkeypatch.setenv("HELMRAISER_SOURCE_REPO", str(repo))
        assert supervisor.code_version() == code


class TestAdoptionAcrossRestarts:
    @pytest.mark.parametrize("scout_result", ["succeeded", "failed", "missing"])
    def test_adopted_run_and_requested_scout_keep_slots_and_stop_gracefully(
        self, ledger, data_dir, harness, tmp_path, monkeypatch, capsys, scout_result
    ):
        run_project, scout_project, other = (tmp_path / name for name in ["run", "scout", "other"])
        queue(ledger, run_project, "run1")
        ledger.request_scouts([scout_project])
        request_id = ledger.list_scout_requests()[0]["id"]
        config = TestScoutSupervisor().config(scout_project)
        monkeypatch.setattr(supervisor, "scout_gate", lambda *args, **kwargs: None)
        monkeypatch.setenv("HELMRAISER_CODE_SNAPSHOT", OLD.commit)
        harness.duration = 50
        harness.scout_drafts[str(scout_project)] = 2 if scout_result == "succeeded" else 0
        if scout_result == "failed":
            harness.scout_fail.add(str(scout_project))
        harness.adopt_waits(monkeypatch)
        code = FakeCode(harness)
        harness.hooks[5] = lambda: setattr(code, "version", NEW)
        (data_dir / "worker.json").write_text(json.dumps({"pid": os.getpid()}))
        with pytest.raises(Execed):
            supervise(
                ledger,
                data_dir,
                harness,
                watch=True,
                poll_s=5,
                code_version=code,
                reexec=code.reexec,
                scout_config=config,
                spawn_scout=harness.spawn_scout,
            )
        assert code.execs == [{"at": 5.0, "alive": 2, "to": NEW.commit}]
        adopted = supervisor.restore_children(data_dir)
        scout = next(c for c in adopted if c.kind == "scout")
        assert scout.request_id == request_id
        assert scout.code_commit == OLD.commit
        if scout_result == "missing":
            harness.children[1].finish = lambda: 1
        # Work queued after exec must not take either occupied project or a full slot.
        queue(ledger, run_project, "run2")
        queue(ledger, scout_project, "scout-task")
        queue(ledger, other, "other-task")
        observed = []
        harness.hooks[6] = lambda: observed.append(
            json.loads((data_dir / "worker.active.json").read_text())
        )
        harness.hooks[10] = lambda: (data_dir / "worker.stop").write_text("stop")
        alerts = []
        result, notices = supervise(
            ledger,
            data_dir,
            harness,
            children=adopted,
            watch=True,
            poll_s=5,
            # Even a reduced limit/excluded project must retain already running children.
            max_parallel=1,
            scope=ProjectScope.of(None, [scout_project]),
            code_version=code,
            reexec=code.reexec,
            running_code=NEW,
            scout_config=config,
            spawn_scout=harness.spawn_scout,
            alert=alerts.append,
        )
        assert result.stopped and harness.now == 50
        assert len(harness.spawned) == len(harness.scout_spawned) == 1
        assert len(notices) == 1
        assert len(observed[0]["children"]) == 2
        record = json.loads((data_dir / "worker.json").read_text())
        assert record["exit"]["reason"] == "stopped"
        lines = events(capsys.readouterr().out)
        finished = [e["scout_finished"] for e in lines if "scout_finished" in e]
        assert len(finished) == 1 and finished[0]["scout_id"] == scout.scout_id
        assert len([e for e in lines if "finished" in e]) == 1
        assert not scout.output.exists()
        assert ledger.get_scout(scout.scout_id)["outcome"] == (
            "succeeded" if scout_result == "succeeded" else "failed"
        )
        assert alerts == (
            ["2 drafts for scout — helmraiser drafts"]
            if scout_result == "succeeded"
            else (
                ["scout: model failed"] if scout_result == "failed" else ["scout: scout exited (1)"]
            )
        )

    @pytest.mark.parametrize("cancelled", [True, False])
    def test_adopted_requested_scout_waiting_or_cancelled_is_not_failed(
        self, ledger, data_dir, harness, monkeypatch, cancelled, capsys
    ):
        child = TestHandoff().child(data_dir, "/scout", "scout")
        ledger.request_scouts([child.project])
        child.request_id = ledger.list_scout_requests()[0]["id"]
        if cancelled:
            ledger.cancel_scout_requests([child.project])
        else:
            child.output.parent.mkdir()
            child.output.write_text(json.dumps({"scout_waiting": True}) + "\n")
        monkeypatch.setattr(supervisor, "_read_child_stat", lambda pid: None)
        alerts = []
        supervise(ledger, data_dir, harness, children=[child], alert=alerts.append)
        assert ledger.get_scout(child.scout_id) is None
        assert alerts == []
        assert len([e for e in events(capsys.readouterr().out) if "scout_finished" in e]) == 1

    def test_second_restart_preserves_original_child_metadata(
        self, ledger, data_dir, harness, tmp_path, monkeypatch, capsys
    ):
        queue(ledger, tmp_path / "run", "task")
        harness.duration = 50
        harness.adopt_waits(monkeypatch)
        monkeypatch.setenv("HELMRAISER_CODE_SNAPSHOT", OLD.commit)
        code = FakeCode(harness)
        harness.hooks[5] = lambda: setattr(code, "version", NEW)
        with pytest.raises(Execed):
            supervise(
                ledger,
                data_dir,
                harness,
                watch=True,
                poll_s=5,
                code_version=code,
                reexec=code.reexec,
            )
        first = supervisor.restore_children(data_dir)
        original = first[0].restart_dict()
        monkeypatch.setenv("HELMRAISER_CODE_SNAPSHOT", NEW.commit)
        newer = CodeVersion("c" * 40, "3" * 40)
        harness.hooks[10] = lambda: setattr(code, "version", newer)
        with pytest.raises(Execed):
            supervise(
                ledger,
                data_dir,
                harness,
                children=first,
                watch=True,
                poll_s=5,
                code_version=code,
                reexec=code.reexec,
                running_code=NEW,
            )
        second = supervisor.restore_children(data_dir)
        assert second[0].restart_dict() == original
        assert code.execs[-1] == {"at": 10.0, "alive": 1, "to": newer.commit}
        harness.hooks[20] = lambda: (data_dir / "worker.stop").write_text("stop")
        result, notices = supervise(ledger, data_dir, harness, children=second, watch=True)
        assert result.stopped and harness.now == 50
        assert len(notices) == 1 and len(harness.spawned) == 1
        assert len([e for e in events(capsys.readouterr().out) if "finished" in e]) == 1


# ── the CLI ───────────────────────────────────────────────────────────


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    config = configured_test_config()
    monkeypatch.setattr(commands, "load_config", lambda: config)
    return config


class FakeSupervisor:
    calls: list[dict[str, Any]] = []
    behaviour: Callable[[dict[str, Any]], None] | None = None

    def __init__(self, **kwargs: Any):
        FakeSupervisor.calls.append(kwargs)
        self.kwargs = kwargs

    def run(self) -> SupervisorResult:
        if FakeSupervisor.behaviour is not None:
            FakeSupervisor.behaviour(self.kwargs)
        return SupervisorResult()


@pytest.fixture
def fake_supervisor(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    FakeSupervisor.calls = []
    monkeypatch.setattr(commands, "Supervisor", FakeSupervisor)
    # ``work --watch`` would re-exec into a code snapshot (tests/cli/test_worker_snapshot.py)
    monkeypatch.setattr(commands, "_serve_from_snapshot", lambda base, *, as_json: None)
    monkeypatch.setattr(FakeSupervisor, "behaviour", None)
    return FakeSupervisor.calls


def work(*args: str, data_dir: Path) -> Any:
    return runner.invoke(app, ["work", *args, "--data-dir", str(data_dir)])


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(commands, "_notify_text", lambda text, enabled: sent.append(text))
    return sent


class TestWorkerExit:
    """``work --watch`` as the recorded background worker never ends silently."""

    @pytest.fixture(autouse=True)
    def recorded(self, data_dir: Path) -> None:
        (data_dir / "worker.json").write_text(json.dumps({"pid": os.getpid(), "argv": []}))

    def record(self, data_dir: Path) -> dict[str, Any]:
        return json.loads((data_dir / "worker.json").read_text())

    def test_an_error_is_recorded_and_notified(
        self, data_dir: Path, config: dict, fake_pi: FakePi, fake_supervisor: list, notices: list
    ):
        def fail(kwargs: dict[str, Any]) -> None:
            raise sqlite3.DatabaseError("database disk image is malformed")

        FakeSupervisor.behaviour = fail

        result = work("--watch", "--json", data_dir=data_dir)

        assert result.exit_code == 1 and isinstance(result.exception, sqlite3.DatabaseError)
        ended = self.record(data_dir)["exit"]
        assert ended["reason"] == "error"
        assert ended["error"] == "DatabaseError: database disk image is malformed"
        assert notices == ["worker exited (error: DatabaseError: database disk image is malformed)"]

    def test_a_startup_failure_is_recorded(
        self, data_dir: Path, config: dict, fake_pi: FakePi, fake_supervisor: list, notices: list
    ):
        fake_pi.write_script({"version": ""})  # pi --version fails before the supervisor starts

        result = work("--watch", "--json", data_dir=data_dir)

        assert result.exit_code == 1 and fake_supervisor == []
        assert self.record(data_dir)["exit"]["error"] == "exit code 1; see the log"
        assert notices == ["worker exited (error: exit code 1; see the log)"]

    def test_sigterm_is_recorded_and_the_handler_restored(
        self, data_dir: Path, config: dict, fake_pi: FakePi, fake_supervisor: list, notices: list
    ):
        before = signal.getsignal(signal.SIGTERM)

        def terminated(kwargs: dict[str, Any]) -> None:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)  # as the signal would, without sending one

        FakeSupervisor.behaviour = terminated

        result = work("--watch", "--json", data_dir=data_dir)

        assert result.exit_code == 143
        assert self.record(data_dir)["exit"]["error"] == "terminated by SIGTERM"
        assert notices == ["worker exited (error: terminated by SIGTERM)"]
        assert signal.getsignal(signal.SIGTERM) is before

    def test_a_stop_keeps_the_supervisors_record_and_sends_no_notice(
        self, data_dir: Path, config: dict, fake_pi: FakePi, fake_supervisor: list, notices: list
    ):
        def stopped(kwargs: dict[str, Any]) -> None:
            ended = {"at": "now", "reason": "stopped"}
            assert update_record(os.getpid(), {"exit": ended}, data_dir)

        FakeSupervisor.behaviour = stopped

        result = work("--watch", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        assert self.record(data_dir)["exit"] == {"at": "now", "reason": "stopped"}
        assert notices == []

    def test_another_workers_record_is_left_alone(
        self, data_dir: Path, config: dict, fake_pi: FakePi, fake_supervisor: list, notices: list
    ):
        (data_dir / "worker.json").write_text(json.dumps({"pid": 1}))

        def fail(kwargs: dict[str, Any]) -> None:
            raise ValueError("boom")

        FakeSupervisor.behaviour = fail

        result = work("--watch", "--json", data_dir=data_dir)

        assert isinstance(result.exception, ValueError)
        assert self.record(data_dir) == {"pid": 1} and notices == []


class TestRestartWiring:
    @pytest.mark.parametrize(
        ("args", "flag", "enabled"),
        [
            (["--watch"], True, True),
            (["--watch"], False, False),
            (["--watch", "--max", "3"], True, False),
            (["--max-parallel", "2"], True, False),
        ],
    )
    def test_only_a_long_running_worker_restarts(
        self,
        data_dir: Path,
        config: dict,
        fake_pi: FakePi,
        fake_supervisor: list,
        args: list[str],
        flag: bool,
        enabled: bool,
        monkeypatch: pytest.MonkeyPatch,
    ):
        config["worker_restart_on_code_change"] = flag

        result = work(*args, "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        (call,) = fake_supervisor
        assert (call["code_version"] is supervisor.code_version) is enabled
        assert call["running_code"] is None  # the startup step is stubbed here
        calls: list[Any] = []
        monkeypatch.setattr(commands, "_reexec", lambda *a: calls.append(a) or "no")
        announce = lambda: None  # noqa: E731
        assert call["reexec"](NEW, announce) == "no"
        assert calls == [(NEW, announce, data_dir)]

    def test_reexec_execs_into_a_snapshot_of_the_new_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        calls: list[Any] = []
        monkeypatch.setenv("HELMRAISER_SOURCE_REPO", str(tmp_path / "checkout"))
        monkeypatch.setattr(
            commands, "exec_into", lambda *a: calls.append(a) or "exec failed: refused"
        )
        announce = lambda: None  # noqa: E731

        problem = commands._reexec(NEW, announce, tmp_path / "data")

        assert problem == "exec failed: refused"
        assert calls == [(NEW.commit, tmp_path / "checkout", tmp_path / "data", announce)]

    def test_reexec_without_a_checkout_stays(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(commands, "source_repo", lambda: None)
        problem = commands._reexec(NEW, lambda: None, Path("/nowhere"))
        assert problem == "the Helmraiser checkout is unknown"


class TestWorkCommand:
    @pytest.mark.parametrize("other", ["--once", "--dry-run"])
    @pytest.mark.parametrize("parallel", [["--watch"], ["--max-parallel", "2"]])
    def test_parallel_options_refuse_once_and_dry_run(
        self, data_dir: Path, config: dict, other: str, parallel: list[str]
    ):
        result = work(*parallel, other, "--json", data_dir=data_dir)
        assert result.exit_code == 2, result.output
        assert "cannot be combined" in json.loads(result.output)["error"]

    @pytest.mark.parametrize("interval", [None, 42])
    def test_max_parallel_and_poll_come_from_config(
        self, data_dir: Path, config: dict, fake_pi: FakePi, fake_supervisor: list, interval
    ):
        config.update(
            max_parallel_runs=3, worker_poll_seconds=5, worker_exclude_projects=["~/other-project"]
        )

        if interval is not None:
            config["base_sync_interval_seconds"] = interval
        result = work("--watch", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        (call,) = fake_supervisor
        assert call["base_sync_interval_s"] == (300 if interval is None else interval)
        assert call["max_parallel"] == 3 and call["poll_s"] == 5.0 and call["watch"] is True
        assert call["scope"] == ProjectScope.of(None, [str(Path("~/other-project").expanduser())])
        assert call["stop_file"] == data_dir / "worker.stop" and call["max_runs"] is None
        final = json.loads(result.output.splitlines()[-1])
        assert final["runs"] == [] and final["summary"]["counts"]["landed"] == 0

    def test_cli_overrides_the_limit_and_the_scope(
        self, data_dir: Path, config: dict, fake_pi: FakePi, fake_supervisor: list
    ):
        config.update(max_parallel_runs=3, worker_exclude_projects=["/p1"])

        result = work(
            "--max-parallel", "5", "--project", "/p1", "--project", "/p2", data_dir=data_dir
        )

        assert result.exit_code == 0, result.output
        (call,) = fake_supervisor
        assert call["max_parallel"] == 5 and call["watch"] is False
        assert call["scope"] == ProjectScope.of(["/p1", "/p2"])
        assert result.output.startswith("draining the queue: up to 5 run(s) at once")

    def test_scout_supervisor_callback_uses_snapshot_process_environment(
        self,
        data_dir: Path,
        config: dict,
        fake_pi: FakePi,
        fake_supervisor: list,
        monkeypatch: pytest.MonkeyPatch,
    ):
        result = work("--watch", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        (call,) = fake_supervisor
        spawned: list[tuple[list[str], Path]] = []
        sentinel = object()
        monkeypatch.setattr(
            commands,
            "_spawn_child",
            lambda argv, output: spawned.append((argv, output)) or sentinel,
        )
        output = data_dir / "child.jsonl"

        assert call["spawn_scout"]("/repo", output, "scout-id", 17) is sentinel
        assert spawned == [
            (
                commands._scout_child_argv(
                    "/repo", data_dir=data_dir, scout_id="scout-id", request_id=17
                ),
                output,
            )
        ]
        assert call["scout_config"] is config

    def test_child_argv(self, tmp_path: Path):
        argv = commands._child_argv("/p1", data_dir=tmp_path, no_caps=True)
        assert argv[:3] == [sys.executable, "-m", "helmraiser"]
        assert argv[3:8] == ["work", "--once", "--json", "--supervised", "--no-notify"]
        assert argv[argv.index("--project") + 1] == "/p1"
        assert argv[argv.index("--data-dir") + 1] == str(tmp_path)
        assert "--no-caps" in argv

    def test_run_notice(self):
        outcome = {"task_id": "p:t", "status": "awaiting_approval", "cost_usd": 0.5, "minutes": 3}
        assert commands._run_notice(outcome) == "p:t: awaiting approval — $0.50, 3 min"

    def test_a_supervised_child_leaves_the_stop_request_to_its_supervisor(
        self, data_dir: Path, config: dict, fake_pi: FakePi, tmp_project: Path
    ):
        (data_dir / "worker.stop").write_text("stop\n")

        result = work("--once", "--json", "--supervised", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        assert (data_dir / "worker.stop").exists()


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


def test_real_children_run_two_projects_in_parallel(
    tmp_path: Path,
    data_dir: Path,
    config: dict,
    fake_pi: FakePi,
    git_init: Any,
    monkeypatch: pytest.MonkeyPatch,
):
    """``work --max-parallel 2`` spawns real ``work --once`` children through the fake pi."""
    monkeypatch.setattr(commands, "get_intake_adapter", lambda config: None)
    # The children read their own config: no live usage fetches, no notifications.
    child_config = tmp_path / "child-config"
    child_config.mkdir()
    (child_config / "config.json").write_text(
        json.dumps(
            {
                "profiles": configured_test_config()["profiles"],
                "notify": False,
                "subscriptions": {"opencode-go": {"live": None}, "openai-codex": {"live": None}},
            }
        )
    )
    monkeypatch.setenv("HELMRAISER_CONFIG_DIR", str(child_config))
    source = str(Path(helmraiser.__file__).resolve().parents[1])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([source, os.environ.get("PYTHONPATH", "")]))
    config["worker_poll_seconds"] = 1
    fake_pi.write_script({"default": {"cost": 0.01, "files": [["done.txt", "ok\n"]]}})
    task_ids = []
    for name in ("one", "two"):
        project = tmp_path / name
        project.mkdir()
        git_init(project)
        path = project / "topics" / f"{name}.md"
        path.parent.mkdir()
        path.write_text(TASK.format(task_id=name, project=project), encoding="utf-8")
        queued = runner.invoke(
            app, ["intake", str(path), "--yes", "--json", "--data-dir", str(data_dir)]
        )
        assert queued.exit_code == 0, queued.output
        task_ids.append(json.loads(queued.output)["task_id"])

    result = work("--max-parallel", "2", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    lines = events(result.output)
    started = [e["started"]["project"] for e in lines if "started" in e]
    assert sorted(started) == [str(tmp_path / "one"), str(tmp_path / "two")]
    kinds = [next(iter(e)) for e in lines if "started" in e or "finished" in e]
    assert kinds == ["started", "started", "finished", "finished"]  # both at once
    finished = [e["finished"] for e in lines if "finished" in e]
    assert [f["exit_code"] for f in finished] == [0, 0]
    final = lines[-1]
    assert sorted(run["task_id"] for run in final["runs"]) == sorted(task_ids)
    assert final["summary"]["counts"]["awaiting_approval"] == 2
    ledger = Ledger(data_dir / "helmraiser.db")
    assert {ledger.get_task(t)["status"] for t in task_ids} == {"awaiting_approval"}
    assert not list((data_dir / "worker-children").iterdir())


def test_real_disjoint_siblings_overlap_at_plan(
    tmp_project, data_dir, config, fake_pi, tmp_path, monkeypatch
):
    child_config = tmp_path / "sibling-config"
    child_config.mkdir()
    config["max_runs_per_project"] = 2
    config["worker_poll_seconds"] = 30
    settings = {
        "profiles": configured_test_config()["profiles"],
        "max_runs_per_project": 2,
        "notify": False,
        "subscriptions": {"opencode-go": {"live": None}, "openai-codex": {"live": None}},
    }
    (child_config / "config.json").write_text(json.dumps(settings))
    monkeypatch.setenv("HELMRAISER_CONFIG_DIR", str(child_config))
    monkeypatch.setenv("PYTHONPATH", str(Path(helmraiser.__file__).resolve().parents[1]))
    fake_pi.write_script(
        {
            "sessions": {
                "*-plan": {
                    "text": "A safe plan",
                    "cost": 0.01,
                    "wait_for_sessions": {"pattern": "*-plan", "count": 2, "timeout": 15},
                }
            }
        }
    )
    with Ledger(data_dir / "helmraiser.db") as ledger:
        for id in ("one", "two", "three"):
            spec = make_spec(tmp_project, id, policy="approve-plan-first", paths=(f"{id}/**",))
            ledger.upsert_task(spec, render_task(spec))
    result = work("--max-parallel", "3", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    lines = events(result.output)
    kinds = [next(iter(e)) for e in lines if "started" in e or "finished" in e]
    assert kinds == ["started", "started", "finished", "finished"]
    assert (
        len([call for call in fake_pi.calls() if call.get("session_id", "").endswith("-plan")]) == 2
    )
    with Ledger(data_dir / "helmraiser.db") as ledger:
        assert len(ledger.list_runs(status="awaiting_approval")) == 2
        assert ledger.list_tasks(status="queued")[0]["id"] == "three"


def test_depends_on_supervisor_skips_held(
    ledger, data_dir, harness, tmp_project, tmp_path, git_init
):
    from helmraiser.task.loader import render_task

    other = tmp_path / "other"
    other.mkdir()
    git_init(other)
    held = make_spec(tmp_project, "held", priority="critical", depends_on=("unknown",))
    ledger.upsert_task(held, render_task(held))
    free = queue(ledger, tmp_project, "free")
    other_free = queue(ledger, other, "other-free")
    result, _ = supervise(ledger, data_dir, harness)
    assert [entry["task_id"] for entry in harness.spawned] == [free, other_free]
    assert ledger.get_task(held.task_id)["status"] == "queued"
    assert len(result.outcomes) == 2


def test_depends_on_supervisor_all_held(ledger, data_dir, harness, tmp_project):
    from helmraiser.task.loader import render_task

    held = make_spec(tmp_project, "held", depends_on=("missing",))
    ledger.upsert_task(held, render_task(held))
    result, _ = supervise(ledger, data_dir, harness)
    assert harness.spawned == []
    assert result.outcomes == []
    assert ledger.get_task(held.task_id)["status"] == "queued"


def test_depends_on_supervisor_watch_release(ledger, data_dir, harness, tmp_project):
    from helmraiser.task.loader import render_task
    from tests.ledger.test_depends_on import feature, git

    _, head = feature(tmp_project)
    upstream = make_spec(tmp_project, "upstream", status="landed")
    dependent = make_spec(tmp_project, "dependent", depends_on=("upstream",))
    for spec in (upstream, dependent):
        ledger.upsert_task(spec, render_task(spec))
    ledger.create_run(
        {**run_row("delivered", upstream.task_id, str(tmp_project), "landed"), "head_commit": head}
    )

    def merge():
        assert harness.spawned == []
        git(tmp_project, "merge", "--no-ff", "feature", "-m", "merge")

    harness.hooks[40] = merge
    harness.hooks[200] = lambda: (data_dir / "worker.stop").write_text("stop\n")
    result, _ = supervise(ledger, data_dir, harness, watch=True)
    assert len(harness.spawned) == 1
    assert harness.spawned[0]["task_id"] == dependent.task_id
    assert harness.spawned[0]["at"] >= 40
    assert result.stopped
    assert len(result.outcomes) == 1


def test_held_pr_worker_poll_releases_stale_base(ledger, data_dir, harness, tmp_project, capsys):
    dependent, _ = seed_pr_dependency_chain(ledger, tmp_project)
    harness.hooks[10] = lambda: (data_dir / "worker.stop").write_text("stop")
    result, _ = supervise(ledger, data_dir, harness, watch=True, poll_s=1)
    assert harness.spawned[0]["task_id"] == dependent.task_id
    assert harness.spawned[0]["at"] == 0
    assert len(result.outcomes) == 1
    payload = next(e["base_sync"] for e in events(capsys.readouterr().out) if "base_sync" in e)
    assert payload["status"] == "synced" and payload["project"] == str(tmp_project)


@pytest.mark.parametrize("interval", [None, 12])
def test_held_pr_worker_failure_retries_on_interval(
    ledger, data_dir, harness, tmp_project, tmp_path, capsys, interval
):
    from tests.ledger.test_depends_on import git

    dependent, origin = seed_pr_dependency_chain(ledger, tmp_project)
    git(tmp_project, "remote", "set-url", "origin", str(origin / "missing"))
    queue(ledger, tmp_path / "other", "other")
    cooldown = 300 if interval is None else interval
    harness.hooks[1] = lambda: git(tmp_project, "remote", "set-url", "origin", str(origin))
    harness.hooks[cooldown + 10] = lambda: (data_dir / "worker.stop").write_text("stop")
    options = {} if interval is None else {"base_sync_interval_s": interval}
    result, _ = supervise(ledger, data_dir, harness, watch=True, poll_s=1, **options)
    assert harness.spawned[0]["at"] == 0 and harness.spawned[0]["project"] != str(tmp_project)
    assert (
        harness.spawned[1]["at"] == cooldown and harness.spawned[1]["task_id"] == dependent.task_id
    )
    outcomes = [e["base_sync"] for e in events(capsys.readouterr().out) if "base_sync" in e]
    assert [o["status"] for o in outcomes] == ["failed", "synced"]
    assert outcomes[0]["reason"] == "fetch_failed" and outcomes[0]["detail"]
    assert len(result.outcomes) == 2 and result.stopped


@pytest.mark.parametrize(
    "exclusion", ["paused", "running", "awaiting_approval", "merge", "unknown", "scope"]
)
def test_held_pr_worker_exclusions(ledger, data_dir, harness, tmp_project, monkeypatch, exclusion):
    dependent, _ = seed_pr_dependency_chain(
        ledger, tmp_project, delivery="merge" if exclusion == "merge" else "pr"
    )
    if exclusion == "paused":
        ledger.pause_queue("test")
    elif exclusion in {"running", "awaiting_approval"}:
        ledger.create_run(run_row("holder", dependent.task_id, str(tmp_project), exclusion))
        monkeypatch.setattr(Supervisor, "_reconcile", lambda *args: None)
    elif exclusion == "unknown":
        holds = {dependent.task_id: {"reason": "depends_on", "unmet": [{"reason": "unknown"}]}}
        monkeypatch.setattr(ledger, "held_tasks", lambda **kwargs: holds)
    calls = stub_sync(monkeypatch)
    harness.hooks[3] = lambda: (data_dir / "worker.stop").write_text("stop")
    scope = ProjectScope.of(exclude=[tmp_project]) if exclusion == "scope" else ProjectScope.of()
    result, _ = supervise(ledger, data_dir, harness, scope=scope, watch=True, poll_s=1)
    assert result.stopped and calls == [] and harness.spawned == []


def test_held_pr_sync_while_slots_full(ledger, data_dir, harness, tmp_project, tmp_path, capsys):
    from tests.ledger.test_depends_on import git

    dependent, origin = seed_pr_dependency_chain(ledger, tmp_project)
    git(tmp_project, "remote", "set-url", "origin", str(origin / "missing"))
    queue(ledger, tmp_path / "other", "other", "critical")
    harness.duration = 10
    harness.hooks[1] = lambda: git(tmp_project, "remote", "set-url", "origin", str(origin))

    def already_synced():
        assert ledger.held_tasks() == {}
        assert len(harness.spawned) == 1
        assert ledger.get_task(dependent.task_id)["status"] == "queued"

    harness.hooks[3] = already_synced
    harness.hooks[22] = lambda: (data_dir / "worker.stop").write_text("stop")
    result, _ = supervise(
        ledger, data_dir, harness, watch=True, max_parallel=1, poll_s=1, base_sync_interval_s=2
    )
    assert harness.spawned[1]["at"] == 10
    assert len(result.outcomes) == 2
    assert [
        e["base_sync"]["status"] for e in events(capsys.readouterr().out) if "base_sync" in e
    ] == ["failed", "synced"]


@pytest.mark.parametrize("exclusion", ["child", "backoff", "stop"])
def test_held_pr_parent_excludes_inflight_backoff_and_stop(
    ledger, data_dir, harness, tmp_project, monkeypatch, exclusion
):
    from helmraiser.cli.supervisor import Child

    seed_pr_dependency_chain(ledger, tmp_project)
    calls = stub_sync(monkeypatch)
    worker = Supervisor(
        ledger=ledger,
        data_dir=data_dir,
        scope=ProjectScope.of(),
        max_parallel=1,
        watch=True,
        max_runs=None,
        poll_s=1,
        stop_file=data_dir / "worker.stop",
        spawn=harness.spawn,
        skip=lambda row: None,
        notify=lambda outcome: None,
        as_json=True,
        clock=harness.clock,
        sleep=harness.sleep,
    )
    if exclusion == "stop":
        worker.stop_file.write_text("stop")
        worker.run()
    else:
        if exclusion == "child":
            process = FakeChild(harness, 1234, lambda: 0)
            worker.children.append(Child(str(tmp_project), process, data_dir / "output", 0))
        else:
            worker.backoff[str(tmp_project)] = 10
        worker._fill()
    assert calls == []
