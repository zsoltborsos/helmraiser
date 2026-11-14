"""Ledger tests: DDL, next_queued ordering, project exclusivity and queue state."""

from __future__ import annotations

import gc
import sqlite3
import threading
import time
import warnings
from dataclasses import replace
from pathlib import Path

import pytest

from helmraiser.ledger.db import (
    SCHEMA_VERSION,
    Ledger,
    ProjectScope,
    RunUnavailable,
    TaskActive,
    default_db_path,
)
from helmraiser.ledger.rundir import RunDir
from helmraiser.task.loader import render_task
from helmraiser.task.model import Budget, ModelSpec, TaskSpec, VerifyStep

EXPECTED_TABLES = {
    "tasks",
    "runs",
    "run_events",
    "iterations",
    "evidence",
    "approvals",
    "queue_state",
    "scouts",
    "scout_drafts",
}


def make_spec(project: Path, task_id: str = "t1", **overrides) -> TaskSpec:
    base = TaskSpec(
        id=task_id,
        title=f"Task {task_id}",
        project=project,
        priority="normal",
        status="queued",
        policy="review",
        isolation="none",
        models={
            "implement": ModelSpec("a/impl", "medium"),
            "fallback": ModelSpec("a/fallback", "high"),
            "review": ModelSpec("a/review", "high"),
        },
        budget=Budget(5.0, 90, 4),
        verify=(VerifyStep("tests", "pytest -q"),),
        evidence=(),
        tags=(),
        body="body",
        source_path=project / "topics" / f"{task_id}.md",
    )
    return replace(base, **overrides)


def run_row(run_id: str, task_id: str, project: str, status: str = "running") -> dict:
    return {
        "run_id": run_id,
        "task_id": task_id,
        "project": project,
        "status": status,
        "policy": "review",
        "isolation": "none",
        "model_implement": "a/impl:medium",
        "model_review": "a/review:high",
        "pi_version": "0.87.1",
    }


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "helmraiser.db")


class TestSchema:
    def test_read_only_observes_writes_and_filters_tasks(self, ledger, tmp_path):
        first = ledger.upsert_task(make_spec(tmp_path), "snapshot")
        second = ledger.upsert_task(make_spec(tmp_path, "t2"), "snapshot")
        with Ledger(ledger.db_path, read_only=True) as reader:
            assert reader.list_runs() == []
            for run_id, task_id, started in (("a", first, 1), ("b", second, 2), ("c", first, 3)):
                ledger.create_run(
                    {**run_row(run_id, task_id, str(tmp_path)), "started_at": started}
                )
            assert [r["run_id"] for r in reader.list_runs()] == ["c", "b", "a"]
            assert [r["run_id"] for r in reader.list_runs(task_id=first)] == ["c", "a"]
            with pytest.raises(ValueError, match="read-only"):
                reader.set_task_status(first, "draft")
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader._get_conn().execute("DELETE FROM runs")

    def test_read_only_missing_does_not_create_directory(self, tmp_path):
        parent = tmp_path / "missing"
        with pytest.raises(sqlite3.OperationalError):
            Ledger(parent / "helmraiser.db", read_only=True)
        assert not parent.exists()

    def test_read_only_refuses_migration(self, ledger):
        conn = ledger._get_conn()
        conn.execute("PRAGMA user_version = 8")
        before = list(conn.iterdump())
        with pytest.raises(ValueError, match="migration"):
            Ledger(ledger.db_path, read_only=True)
        assert list(conn.iterdump()) == before
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8

    @pytest.mark.parametrize("pending", [None, "plan", "merge"])
    def test_rework_migration_pending_approvals(self, tmp_path: Path, pending):
        path = tmp_path / "v8.db"
        with Ledger(path) as ledger:
            task_id = ledger.upsert_task(make_spec(tmp_path), "snapshot")
            ledger.create_run(run_row("parked", task_id, str(tmp_path), "awaiting_approval"))
            for status in ("running", "landed", "failed"):
                ledger.create_run(run_row(status, task_id, str(tmp_path), status))
            if pending:
                ledger.record_approval("parked", pending, "pending")
        with sqlite3.connect(path) as conn:
            conn.execute("PRAGMA user_version = 8")
        for _ in range(2):
            with Ledger(path) as ledger:
                approvals = ledger.approvals("parked")
                assert len(approvals) == 1
                assert approvals[0]["kind"] == (pending or "merge")
                assert approvals[0]["decision"] == "pending"
                for status in ("running", "landed", "failed"):
                    assert ledger.approvals(status) == []

    def test_ddl_and_user_version(self, ledger: Ledger, tmp_path: Path):
        conn = sqlite3.connect(tmp_path / "helmraiser.db")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert EXPECTED_TABLES <= tables
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        assert {"model_plan", "plan_cost_usd"} <= run_columns
        scout_columns = {row[1] for row in conn.execute("PRAGMA table_info(scouts)")}
        assert "head_commit" in scout_columns
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "run_events_run" in indexes
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    def test_reopen_is_idempotent(self, tmp_path: Path):
        Ledger(tmp_path / "helmraiser.db")
        Ledger(tmp_path / "helmraiser.db")

    def test_opening_a_current_ledger_does_not_wait_for_the_write_lock(self, tmp_path: Path):
        # Every open used to write ``PRAGMA user_version``, so it queued for the write
        # lock behind every writer: parallel ``intake --propose`` runs and the worker
        # starved each other until the busy timeout ("database is locked").
        path = tmp_path / "helmraiser.db"
        Ledger(path).close()
        writer = sqlite3.connect(path, isolation_level=None)
        writer.execute("BEGIN IMMEDIATE")
        read: list[list[dict]] = []

        def open_and_read() -> None:
            with Ledger(path) as ledger:
                read.append(ledger.list_tasks())

        thread = threading.Thread(target=open_and_read, daemon=True)
        try:
            thread.start()
            thread.join(timeout=5)
            assert read == [[]]
        finally:
            writer.execute("COMMIT")
            writer.close()
            thread.join()

    def test_concurrent_opens_of_version_3_all_migrate(self, tmp_path: Path):
        path = tmp_path / "helmraiser.db"
        Ledger(path).close()
        conn = sqlite3.connect(path)
        conn.execute("DROP TABLE scouts")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.close()
        barrier = threading.Barrier(8)
        errors: list[BaseException] = []

        def open_ledger() -> None:
            barrier.wait()
            try:
                with Ledger(path) as ledger:
                    ledger.is_paused()
            except sqlite3.Error as exc:
                errors.append(exc)

        threads = [threading.Thread(target=open_ledger) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert errors == []
        conn = sqlite3.connect(path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scouts'"
        ).fetchone()
        conn.close()

    def test_version_4_database_migrates_scout_drafts_and_preserves_rows(self, tmp_path: Path):
        path = tmp_path / "v4.db"
        ledger = Ledger(path)
        task_id = ledger.upsert_task(make_spec(tmp_path, status="draft"), "snapshot")
        ledger.create_scout(
            {"id": "scout", "project": str(tmp_path), "model": "p/m", "report_path": "/r"}
        )
        ledger.close()
        conn = sqlite3.connect(path)
        conn.execute("DROP TABLE scout_drafts")
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        conn.close()

        migrated = Ledger(path)

        assert migrated._get_conn().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert migrated.get_task(task_id)["snapshot"] == "snapshot"
        assert migrated.get_scout("scout")["project"] == str(tmp_path)
        assert (
            migrated._get_conn()
            .execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scout_drafts'")
            .fetchone()
        )

    def test_version_7_database_migrates_intake_notes_and_preserves_proposal(self, tmp_path: Path):
        path = tmp_path / "v7.db"
        with Ledger(path) as ledger:
            task_id = ledger.upsert_task(make_spec(tmp_path, status="draft"), "snapshot")
            ledger.create_scout(
                {"id": "scout", "project": str(tmp_path), "model": "p/m", "report_path": "/r"}
            )
            ledger.create_scout_draft(task_id, "scout", {"id": "candidate"})
            ledger.set_scout_draft_intake(
                task_id, status="proposed", proposal={"verify": ["true"]}, error=None
            )
        with sqlite3.connect(path) as conn:
            conn.execute("ALTER TABLE scout_drafts DROP COLUMN intake_notes")
            conn.execute("PRAGMA user_version = 7")

        with Ledger(path) as migrated:
            draft = migrated.get_scout_draft(task_id)
            assert draft["intake_notes"] == []
            assert draft["proposal"] == {"verify": ["true"]}
            assert draft["candidate"] == {"id": "candidate"}
            assert (
                migrated._get_conn().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            )
            migrated.set_scout_draft_intake(
                task_id,
                status="proposed",
                proposal=draft["proposal"],
                error=None,
                notes=["diagnostic"],
            )
        with Ledger(path) as reopened:
            assert reopened.get_scout_draft(task_id)["intake_notes"] == ["diagnostic"]

    def test_version_3_database_migrates_scouts_table(self, tmp_path: Path):
        path = tmp_path / "v3.db"
        Ledger(path).close()
        conn = sqlite3.connect(path)
        conn.execute("DROP TABLE scouts")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.close()

        with Ledger(path) as ledger:
            assert ledger._get_conn().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert (
                ledger._get_conn()
                .execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scouts'")
                .fetchone()
            )

    def test_version_2_database_migrates_plan_columns(self, tmp_path: Path):
        path = tmp_path / "v2.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, "
            "delivery TEXT NOT NULL DEFAULT 'merge', pr_url TEXT)"
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()

        ledger = Ledger(path)
        columns = {row[1]: row for row in ledger._get_conn().execute("PRAGMA table_info(runs)")}

        assert ledger._get_conn().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert columns["model_plan"][2] == "TEXT"
        assert columns["plan_cost_usd"][2] == "REAL"
        assert columns["plan_cost_usd"][4] == "0"

    def test_version_5_database_adds_scout_head_and_preserves_rows(self, tmp_path: Path):
        path = tmp_path / "v5.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE scouts (id TEXT PRIMARY KEY, project TEXT NOT NULL, "
            "model TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL, "
            "cost_usd REAL NOT NULL DEFAULT 0, tokens_in INTEGER NOT NULL DEFAULT 0, "
            "tokens_out INTEGER NOT NULL DEFAULT 0, outcome TEXT NOT NULL, "
            "drafts_written TEXT NOT NULL DEFAULT '[]', report_path TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO scouts (id, project, model, started_at, outcome, report_path) "
            "VALUES ('old', '/project', 'p/m', 1, 'succeeded', '/report')"
        )
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
        conn.close()

        ledger = Ledger(path)

        assert ledger._get_conn().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        old = ledger.get_scout("old")
        assert old is not None and old["head_commit"] is None
        ledger.create_scout(
            {
                "id": "new",
                "project": "/project",
                "model": "p/m",
                "head_commit": "abc123",
                "report_path": "/report",
            }
        )
        new = ledger.get_scout("new")
        assert new is not None and new["head_commit"] == "abc123"

    def test_default_path_honours_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HELMRAISER_DATA_DIR", str(tmp_path / "data"))
        assert default_db_path() == tmp_path / "data" / "helmraiser.db"
        ledger = Ledger()
        assert ledger.db_path == tmp_path / "data" / "helmraiser.db"
        assert ledger.db_path.exists()


class TestTasks:
    def test_upsert_and_get(self, ledger: Ledger, tmp_path: Path):
        spec = make_spec(tmp_path)
        key = ledger.upsert_task(spec, "---\nid: t1\n---\n")
        assert key == f"{str(tmp_path).replace('/', '-')}:t1"
        row = ledger.get_task(key)
        assert row is not None
        assert row["status"] == "queued"
        assert row["priority"] == 2
        assert row["snapshot"].startswith("---")
        assert row["queued_at"] is not None
        assert row["source_path"] == str(spec.source_path)

    def test_upsert_keeps_created_at_and_queued_at(self, ledger: Ledger, tmp_path: Path):
        spec = make_spec(tmp_path)
        key = ledger.upsert_task(spec, "v1")
        first = ledger.get_task(key)
        assert first is not None
        time.sleep(0.01)
        ledger.upsert_task(replace(spec, title="renamed"), "v2")
        second = ledger.get_task(key)
        assert second is not None
        assert second["created_at"] == first["created_at"]
        assert second["queued_at"] == first["queued_at"]
        assert second["title"] == "renamed"
        assert second["snapshot"] == "v2"
        assert second["updated_at"] >= first["updated_at"]

    def test_draft_has_no_queued_at(self, ledger: Ledger, tmp_path: Path):
        key = ledger.upsert_task(make_spec(tmp_path, status="draft"), "")
        row = ledger.get_task(key)
        assert row is not None and row["queued_at"] is None

    def test_set_task_status_sets_queued_at(self, ledger: Ledger, tmp_path: Path):
        key = ledger.upsert_task(make_spec(tmp_path, status="draft"), "")
        assert ledger.set_task_status(key, "queued")
        row = ledger.get_task(key)
        assert row is not None and row["status"] == "queued" and row["queued_at"] is not None
        assert not ledger.set_task_status("missing", "queued")

    def test_list_tasks_filters_by_status_and_project(self, ledger: Ledger, tmp_path: Path):
        ledger.upsert_task(make_spec(tmp_path, "a", status="draft"), "")
        ledger.upsert_task(make_spec(tmp_path, "b"), "")
        ledger.upsert_task(make_spec(tmp_path / "other", "c"), "")
        assert [t["id"] for t in ledger.list_tasks(status="queued")] == ["b", "c"]
        assert [t["id"] for t in ledger.list_tasks(project=str(tmp_path))] == ["a", "b"]
        assert [t["id"] for t in ledger.list_tasks(status="draft", project=str(tmp_path))] == ["a"]
        assert len(ledger.list_tasks()) == 3


class TestScouts:
    def test_create_finish_get_and_list(self, ledger: Ledger, tmp_path: Path):
        common = {"model": "provider/model:high", "report_path": str(tmp_path / "scout.md")}
        ledger.create_scout(
            {
                "id": "older",
                "project": "/p1",
                "head_commit": "abc123",
                "started_at": 1.0,
                **common,
            }
        )
        ledger.create_scout({"id": "newer", "project": "/p2", "started_at": 2.0, **common})

        assert ledger.finish_scout(
            "older",
            outcome="succeeded",
            cost_usd=0.25,
            tokens_in=10,
            tokens_out=20,
            drafts_written=["-p1:one", "-p1:two"],
            ended_at=3.0,
        )
        row = ledger.get_scout("older")
        assert row is not None
        assert row["outcome"] == "succeeded"
        assert row["head_commit"] == "abc123"
        assert row["drafts_written"] == ["-p1:one", "-p1:two"]
        assert (row["cost_usd"], row["tokens_in"], row["tokens_out"]) == (0.25, 10, 20)
        assert [item["id"] for item in ledger.list_scouts()] == ["newer", "older"]
        assert [item["id"] for item in ledger.list_scouts(project="/p1")] == ["older"]

    def test_rejects_unknown_columns_and_outcomes(self, ledger: Ledger):
        record = {"id": "s", "project": "/p", "model": "m", "report_path": "/r"}
        with pytest.raises(ValueError, match="unknown scout columns: typo"):
            ledger.create_scout({**record, "typo": True})
        with pytest.raises(ValueError, match="invalid scout outcome"):
            ledger.create_scout({**record, "outcome": "done"})

    def test_scout_draft_round_trip_and_intake_state_validation(
        self, ledger: Ledger, tmp_path: Path
    ):
        task_id = ledger.upsert_task(make_spec(tmp_path, status="draft"), "draft")
        ledger.create_scout(
            {"id": "scout", "project": str(tmp_path), "model": "p/m", "report_path": "/r"}
        )
        candidate = {"files": ["tests/new.py"], "title": "New"}
        proposal = {"verify": [{"name": "tests", "run": "pytest"}]}
        preflight = {"summary": "expected red", "steps": []}

        ledger.create_scout_draft(task_id, "scout", candidate)
        assert ledger.set_scout_draft_intake(
            task_id, status="proposed", proposal=proposal, error=None
        )
        assert ledger.set_scout_draft_preflight(task_id, preflight)
        row = ledger.get_scout_draft(task_id)
        assert row is not None
        assert row["candidate"] == candidate
        assert row["proposal"] == proposal
        assert row["preflight"] == preflight
        assert row["intake_status"] == "proposed" and row["intake_error"] is None

        assert ledger.set_scout_draft_intake(
            task_id, status="intake_failed", proposal=None, error="invalid acceptance"
        )
        failed = ledger.get_scout_draft(task_id)
        assert failed is not None and failed["intake_error"] == "invalid acceptance"
        with pytest.raises(ValueError, match="invalid scout draft intake status"):
            ledger.set_scout_draft_intake(task_id, status="unknown", proposal=None, error=None)


class TestAtomicAdmission:
    @pytest.mark.parametrize("update", ["upsert", "snapshot"])
    def test_snapshot_change_serializes_with_claim(self, tmp_path, update):
        db = tmp_path / "race.db"
        original = make_spec(tmp_path, paths=("a/**",))
        changed = replace(original, paths=("b/**",))
        with Ledger(db) as ledger:
            ledger.upsert_task(original, render_task(original))
        barrier = threading.Barrier(2)
        results, errors = [], []

        def claim():
            with Ledger(db) as ledger:
                barrier.wait(timeout=5)
                try:
                    ledger.claim_queued_run(
                        run_row("holder", original.task_id, str(tmp_path)),
                        snapshot=render_task(original),
                        config={},
                    )
                    results.append("claimed")
                except RunUnavailable:
                    results.append("claim_refused")
                except Exception as exc:
                    errors.append(exc)

        def modify():
            with Ledger(db) as ledger:
                barrier.wait(timeout=5)
                try:
                    if update == "upsert":
                        ledger.upsert_task(changed, render_task(changed))
                    else:
                        ledger.set_task_snapshot(original.task_id, render_task(changed))
                    results.append("changed")
                except TaskActive:
                    results.append("change_refused")
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=fn) for fn in (claim, modify)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
        assert not errors
        assert set(results) in ({"claimed", "change_refused"}, {"changed", "claim_refused"})
        with Ledger(db) as ledger:
            if "claimed" in results:
                assert ledger.get_task(original.task_id)["snapshot"] == render_task(original)
                assert ledger.get_task(original.task_id)["status"] == "running"
            else:
                assert not ledger.list_runs()
                assert ledger.get_task(original.task_id)["snapshot"] == render_task(changed)

    @pytest.mark.parametrize("limit,overlap,expected", [(1, False, 1), (2, False, 2), (2, True, 1)])
    def test_competing_connections(self, tmp_path, limit, overlap, expected):
        db = tmp_path / "claims.db"
        ledger = Ledger(db)
        specs = [
            make_spec(tmp_path, id, paths=(("same/**" if overlap else f"{id}/**"),))
            for id in ("a", "b")
        ]
        for spec in specs:
            ledger.upsert_task(spec, render_task(spec))
        barrier = threading.Barrier(2)
        results, errors = [], []

        def claim(spec):
            with Ledger(db) as connection:
                barrier.wait(timeout=5)
                try:
                    connection.claim_queued_run(
                        run_row(spec.id, spec.task_id, str(tmp_path)),
                        snapshot=render_task(spec),
                        config={"max_runs_per_project": limit},
                    )
                    results.append(spec.id)
                except RunUnavailable:
                    pass
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=claim, args=(spec,)) for spec in specs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
        assert not errors
        assert len(results) == len(ledger.list_runs()) == expected
        assert len(ledger.list_tasks(status="running")) == expected

    @pytest.mark.parametrize("change", ["snapshot", "dequeued", "paused"])
    def test_changed_selection(self, ledger, tmp_path, change):
        spec = make_spec(tmp_path)
        snapshot = render_task(spec)
        ledger.upsert_task(spec, snapshot)
        if change == "snapshot":
            ledger.set_task_snapshot(spec.task_id, snapshot + "changed")
        elif change == "dequeued":
            ledger.dequeue_task(spec.task_id)
        else:
            ledger.pause_queue("test")
        with pytest.raises(RunUnavailable):
            ledger.claim_queued_run(
                run_row("r", spec.task_id, str(tmp_path)), snapshot=snapshot, config={}
            )
        assert not ledger.list_runs()
        assert ledger.get_task(spec.task_id)["status"] != "running"

    def test_checked_resume_refusal(self, ledger, tmp_path):
        for id, status in [("a", "resuming"), ("b", "running")]:
            spec = make_spec(tmp_path, id, paths=("same/**",), status=status)
            ledger.upsert_task(spec, render_task(spec))
            ledger.create_run(run_row(id, spec.task_id, str(tmp_path), status))
        assert not ledger.claim_resumable("a", checked=True, config={"max_runs_per_project": 2})
        assert ledger.get_run("a")["status"] == "resuming"


class TestSiblingScheduling:
    @pytest.mark.parametrize(
        "status", ["starting", "running", "resuming", "awaiting_approval", "parked"]
    )
    @pytest.mark.parametrize("update", ["upsert", "snapshot"])
    @pytest.mark.parametrize(
        "declaration,reason",
        [
            ("overlap", "paths_overlap"),
            ("missing", "project_busy"),
            ("dependency", "depends_on"),
            ("transitive", "depends_on"),
        ],
    )
    def test_active_snapshot_cannot_change_admission(
        self, ledger, tmp_path, status, update, declaration, reason
    ):
        original = make_spec(
            tmp_path,
            "a",
            paths=() if declaration == "missing" else ("a/**",),
            depends_on=(
                ("c",)
                if declaration == "dependency"
                else ("bridge",) if declaration == "transitive" else ()
            ),
        )
        candidate = make_spec(
            tmp_path, "c", paths=("a/**",) if declaration == "overlap" else ("c/**",)
        )
        bridge = make_spec(tmp_path, "bridge", depends_on=("c",), status="draft")
        for spec in (original, candidate, bridge):
            ledger.upsert_task(spec, render_task(spec))
        ledger.create_run(run_row("holder", original.task_id, str(tmp_path), status))
        ledger.set_task_status(original.task_id, status)
        before = ledger.get_task(original.task_id)
        changed = replace(original, paths=("b/**",), depends_on=())
        config = {"max_runs_per_project": 2}
        with pytest.raises(TaskActive, match="nonterminal run holder"):
            if update == "upsert":
                ledger.upsert_task(changed, render_task(changed))
            else:
                ledger.set_task_snapshot(original.task_id, render_task(changed))
        assert ledger.get_task(original.task_id) == before
        assert ledger.held_tasks(config=config)[candidate.task_id]["reason"] == reason
        assert ledger.next_queued(config=config) is None
        with pytest.raises(RunUnavailable):
            ledger.claim_queued_run(
                run_row("candidate", candidate.task_id, str(tmp_path)),
                snapshot=render_task(candidate),
                config=config,
            )
        assert ledger.get_run("candidate") is None
        # Idempotent writes cannot change the holder's lifecycle state either.
        assert ledger.upsert_task(original, render_task(original)) == original.task_id
        assert ledger.set_task_snapshot(original.task_id, render_task(original))
        assert ledger.get_task(original.task_id) == before

    @pytest.mark.parametrize("terminal", ["landed", "failed", "aborted"])
    def test_settled_snapshot_can_change(self, ledger, tmp_path, terminal):
        original = make_spec(tmp_path, paths=("a/**",))
        ledger.upsert_task(original, render_task(original))
        ledger.create_run(run_row("settled", original.task_id, str(tmp_path), terminal))
        changed = replace(original, paths=("b/**",))
        assert ledger.set_task_snapshot(original.task_id, render_task(changed))
        changed = replace(changed, paths=("c/**",))
        assert ledger.upsert_task(changed, render_task(changed)) == original.task_id
        assert ledger.get_task(original.task_id)["snapshot"] == render_task(changed)

    def test_more_resuming_runs_than_slots_resume_oldest_first(self, ledger, tmp_path):
        # 2026-10-26: three resuming runs of one project at limit 2 each counted
        # the other two as holders, so none resumed.
        for n, id in enumerate("abc"):
            spec = make_spec(tmp_path, id, paths=(f"{id}/**",), status="resuming")
            ledger.upsert_task(spec, render_task(spec))
            ledger.create_run(
                {**run_row(id, spec.task_id, str(tmp_path), "resuming"), "started_at": 100.0 + n}
            )
        config = {"max_runs_per_project": 2}
        assert ledger.next_resumable(config=config)["run_id"] == "a"
        assert ledger.next_resumable(config=config, exclude={"a"})["run_id"] == "b"
        assert ledger.next_resumable(config=config, exclude={"a", "b"}) is None
        assert ledger.claim_resumable("a", checked=True, config=config)
        assert ledger.next_resumable(config=config)["run_id"] == "b"
        assert ledger.claim_resumable("b", checked=True, config=config)
        assert ledger.next_resumable(config=config) is None
        ledger.finish_run("a", "landed")
        assert ledger.next_resumable(config=config)["run_id"] == "c"

    @pytest.mark.parametrize("other_status", ["running", "awaiting_approval", "resuming"])
    def test_resume_owned_slot(self, ledger, tmp_path, other_status):
        for id, status in [("a", "resuming"), ("b", other_status)]:
            spec = make_spec(tmp_path, id, paths=(f"{id}/**",), status=status)
            ledger.upsert_task(spec, render_task(spec))
            ledger.create_run(run_row(id, spec.task_id, str(tmp_path), status))
        config = {"max_runs_per_project": 2}
        assert ledger.next_resumable(config=config)["run_id"] == "a"
        selected = ledger.next_resumable(config=config, exclude={"a"})
        assert (selected["run_id"] if selected else None) == (
            "b" if other_status == "resuming" else None
        )
        spec = make_spec(tmp_path, "c", paths=("c/**",), status="running")
        ledger.upsert_task(spec, render_task(spec))
        ledger.create_run(run_row("c", spec.task_id, str(tmp_path)))
        # c fills b's slot; a later resuming run never holds a, which takes the other.
        selected = ledger.next_resumable(config=config)
        assert (selected["run_id"] if selected else None) == (
            "a" if other_status == "resuming" else None
        )
        assert ledger.next_resumable(config=config, exclude={"a"}) is None
        ledger.finish_run("c", "landed")
        ledger.finish_run("b", "aborted")
        ledger.set_task_snapshot(
            make_spec(tmp_path, "b").task_id, render_task(make_spec(tmp_path, "b", paths=("a/**",)))
        )
        ledger.create_run(
            run_row("b-overlap", make_spec(tmp_path, "b").task_id, str(tmp_path), other_status)
        )
        # Two resumable runs never hold each other: the one resumed first goes, the later
        # one waits for it. A running or parked overlapping run still holds.
        selected = ledger.next_resumable(config=config)
        assert (selected["run_id"] if selected else None) == (
            "a" if other_status == "resuming" else None
        )
        assert ledger.next_resumable(config=config, exclude={"a"}) is None

    @pytest.mark.parametrize(
        "status", ["running", "starting", "resuming", "awaiting_approval", "parked"]
    )
    @pytest.mark.parametrize(
        "paths,other_paths,reason",
        [
            (("a/**",), ("b/**",), None),
            (("src/file*.py",), ("src/file*.txt",), "paths_overlap"),
            ((), ("b/**",), "project_busy"),
            (("a/**",), (), "project_busy"),
        ],
    )
    def test_admission_and_holds(self, ledger, tmp_path, status, paths, other_paths, reason):
        a = make_spec(tmp_path, "a", paths=paths)
        b = make_spec(tmp_path, "b", paths=other_paths, status="running")
        for spec in (a, b):
            ledger.upsert_task(spec, render_task(spec))
        ledger.create_run(run_row("b", b.task_id, str(tmp_path), status))
        config = {"max_runs_per_project": 2}
        holds = ledger.held_tasks(config=config)
        selected = ledger.next_queued(config=config)
        if reason:
            assert selected is None
            assert holds[a.task_id]["reason"] == reason
            assert holds[a.task_id]["other_task_id"] == "b"
        else:
            assert selected["task_id"] == a.task_id
            assert holds == {}
        assert ledger.next_queued(config={"max_runs_per_project": 1}) is None

    def test_capacity_every_holder_and_override(self, ledger, tmp_path):
        for id, paths in [("a", ("a/**",)), ("b", ("b/**",)), ("c", ("a/**",))]:
            spec = make_spec(tmp_path, id, paths=paths, status="queued" if id == "a" else "running")
            ledger.upsert_task(spec, render_task(spec))
            if id != "a":
                ledger.create_run(run_row(id, spec.task_id, str(tmp_path)))
        assert (
            next(iter(ledger.held_tasks(config={"max_runs_per_project": 2}).values()))["reason"]
            == "project_busy"
        )
        holds = ledger.held_tasks(config={"max_runs_per_project": 3})
        assert next(iter(holds.values()))["other_task_id"] == "c"
        assert next(iter(holds.values()))["reason"] == "paths_overlap"
        ledger.finish_run("c", "landed")
        (tmp_path / ".helmraiser").mkdir()
        (tmp_path / ".helmraiser/project.toml").write_text("max_runs_per_project = 2\n")
        assert ledger.next_queued(config={"max_runs_per_project": 1})["id"] == "a"
        (tmp_path / ".helmraiser/project.toml").write_text("max_runs_per_project = false\n")
        assert "max_runs_per_project" in next(iter(ledger.held_tasks(config={}).values()))["detail"]

    def test_transitive_reverse_dependency_blocks(self, ledger, tmp_path):
        a = make_spec(tmp_path, "a", paths=("a/**",))
        b = make_spec(tmp_path, "b", paths=("b/**",), depends_on=("c",), status="running")
        c = make_spec(tmp_path, "c", depends_on=("a",), status="draft")
        for spec in (a, b, c):
            ledger.upsert_task(spec, render_task(spec))
        ledger.create_run(run_row("b", b.task_id, str(tmp_path)))
        hold = ledger.held_tasks(config={"max_runs_per_project": 2})[a.task_id]
        assert hold["reason"] == "depends_on"
        assert hold["other_task_id"] == "b"
        assert ledger.next_queued(config={"max_runs_per_project": 2}) is None


class TestNextQueued:
    def test_orders_by_priority_then_queued_at(self, ledger: Ledger, tmp_path: Path):
        ledger.upsert_task(make_spec(tmp_path, "low-first", priority="low"), "")
        time.sleep(0.01)
        ledger.upsert_task(make_spec(tmp_path, "normal-a"), "")
        time.sleep(0.01)
        ledger.upsert_task(make_spec(tmp_path, "normal-b"), "")
        ledger.upsert_task(make_spec(tmp_path, "crit", priority="critical"), "")

        picked = []
        while (row := ledger.next_queued()) is not None:
            picked.append(row["id"])
            ledger.set_task_status(row["task_id"], "landed")
        assert picked == ["crit", "normal-a", "normal-b", "low-first"]

    def test_skips_non_queued_and_returns_none_when_empty(self, ledger: Ledger, tmp_path: Path):
        ledger.upsert_task(make_spec(tmp_path, "d", status="draft"), "")
        assert ledger.next_queued() is None

    def test_project_filter(self, ledger: Ledger, tmp_path: Path):
        ledger.upsert_task(make_spec(tmp_path / "p1", "a", priority="high"), "")
        ledger.upsert_task(make_spec(tmp_path / "p2", "b"), "")
        row = ledger.next_queued(project=str(tmp_path / "p2"))
        assert row is not None and row["id"] == "b"

    def test_excludes_task_ids(self, ledger: Ledger, tmp_path: Path):
        first = ledger.upsert_task(make_spec(tmp_path, "first", priority="high"), "")
        second = ledger.upsert_task(make_spec(tmp_path, "second"), "")

        row = ledger.next_queued(exclude={first})

        assert row is not None and row["task_id"] == second
        assert ledger.next_queued(exclude={first, second}) is None

    def test_per_project_exclusivity(self, ledger: Ledger, tmp_path: Path):
        p1, p2 = tmp_path / "p1", tmp_path / "p2"
        busy = ledger.upsert_task(make_spec(p1, "busy", priority="critical"), "")
        ledger.upsert_task(make_spec(p1, "also-p1", priority="critical"), "")
        ledger.upsert_task(make_spec(p2, "other"), "")
        ledger.create_run(run_row("r1", busy, str(p1), status="running"))

        row = ledger.next_queued()
        assert row is not None and row["id"] == "other"
        assert ledger.next_queued(project=str(p1)) is None

        ledger.finish_run("r1", "landed")
        row = ledger.next_queued()
        assert row is not None and row["project"] == str(p1)

    @pytest.mark.parametrize("status", ["running", "aborting", "verifying", "awaiting_approval"])
    def test_active_run_statuses_block(self, ledger: Ledger, tmp_path: Path, status: str):
        key = ledger.upsert_task(make_spec(tmp_path, "t"), "")
        ledger.create_run(run_row("r1", key, str(tmp_path), status=status))
        assert ledger.next_queued() is None

    @pytest.mark.parametrize("pending", ["merge", "plan"])
    def test_awaiting_approval_run_holds_its_project(
        self, ledger: Ledger, tmp_path: Path, pending: str
    ):
        p1, p2 = tmp_path / "p1", tmp_path / "p2"
        parked = ledger.upsert_task(make_spec(p1, "parked"), "")
        ledger.create_run(run_row("r1", parked, str(p1), status="awaiting_approval"))
        if pending == "plan":
            ledger.record_approval("r1", "plan", "pending")
        ledger.set_task_status(parked, "awaiting_approval")
        waiting = ledger.upsert_task(make_spec(p1, "waiting", priority="critical"), "")
        other = ledger.upsert_task(make_spec(p2, "other", priority="low"), "")

        row = ledger.next_queued()

        assert row is not None and row["task_id"] == other
        assert ledger.next_queued(project=str(p1)) is None
        assert ledger.held_tasks() == {
            waiting: {"run_id": "r1", "status": "awaiting_approval", "pending": pending}
        }

        ledger.finish_run("r1", "landed")
        row = ledger.next_queued()
        assert row is not None and row["task_id"] == waiting
        assert ledger.held_tasks() == {}

    @pytest.mark.parametrize(
        "parked_variant,status,held",
        [
            ("widget", "awaiting_approval", False),
            ("widget", "running", True),
            ("gadget", "awaiting_approval", True),
            (None, "awaiting_approval", True),
        ],
    )
    def test_parked_variant_does_not_hold_its_sibling(
        self, ledger: Ledger, tmp_path: Path, parked_variant, status, held
    ):
        def variant(task_id: str, of: str | None) -> TaskSpec:
            return make_spec(
                tmp_path, task_id, paths=("src/**",), extra={"variant_of": of} if of else {}
            )

        first = variant("widget--a", parked_variant)
        parked = ledger.upsert_task(first, render_task(first))
        ledger.create_run(run_row("r1", parked, str(tmp_path), status=status))
        ledger.set_task_status(parked, status)
        second = variant("widget--b", "widget")
        waiting = ledger.upsert_task(second, render_task(second))

        row = ledger.next_queued()

        assert (row is None) is held
        if not held:
            assert row is not None and row["task_id"] == waiting

    def test_held_tasks_names_an_active_run_and_filters_by_project(
        self, ledger: Ledger, tmp_path: Path
    ):
        p1, p2 = tmp_path / "p1", tmp_path / "p2"
        busy = ledger.upsert_task(make_spec(p1, "busy"), "")
        ledger.set_task_status(busy, "running")
        ledger.create_run(run_row("r1", busy, str(p1), status="running"))
        held = ledger.upsert_task(make_spec(p1, "held"), "")
        ledger.upsert_task(make_spec(p2, "free"), "")
        ledger.upsert_task(make_spec(p1, "draft", status="draft"), "")

        assert ledger.held_tasks() == {held: {"run_id": "r1", "status": "running", "pending": None}}
        assert set(ledger.held_tasks(project=str(p1))) == {held}
        assert ledger.held_tasks(project=str(p2)) == {}

    @pytest.mark.parametrize("status", ["landed", "failed", "aborted"])
    def test_terminal_run_statuses_do_not_block(self, ledger: Ledger, tmp_path: Path, status: str):
        key = ledger.upsert_task(make_spec(tmp_path, "t"), "")
        ledger.create_run(run_row("r1", key, str(tmp_path), status=status))
        assert ledger.next_queued() is not None


class TestResumable:
    def test_resuming_held_by_running_sibling_limit_one(self, ledger, tmp_path):
        for n, (run_id, status) in enumerate((("back", "resuming"), ("sibling", "running"))):
            spec = make_spec(tmp_path, run_id, status=status)
            key = ledger.upsert_task(spec, render_task(spec))
            ledger.create_run(
                {**run_row(run_id, key, str(tmp_path), status), "started_at": 100.0 + n}
            )
        config = {"max_runs_per_project": 1}

        assert ledger.resume_holds(config=config) == {
            "back": {"run_id": "sibling", "status": "running", "pending": None}
        }
        assert ledger.next_resumable(config=config) is None

        ledger.finish_run("sibling", "landed")
        assert ledger.resume_holds(config=config) == {}
        assert ledger.next_resumable(config=config)["run_id"] == "back"

    def test_resuming_held_three_runs_limit_two_matches_scheduler(self, ledger, tmp_path):
        for n, run_id in enumerate(("a", "b", "c")):
            spec = make_spec(tmp_path, run_id, paths=(f"{run_id}/**",), status="resuming")
            key = ledger.upsert_task(spec, render_task(spec))
            ledger.create_run(
                {**run_row(run_id, key, str(tmp_path), "resuming"), "started_at": 100.0 + n}
            )
        config = {"max_runs_per_project": 2}
        before = ledger.list_runs()

        holds = ledger.resume_holds(config=config)

        assert holds.get("a") is None
        assert holds.get("b") is None
        assert holds == {
            "c": {
                "run_id": "a",
                "status": "resuming",
                "pending": None,
                "reason": "project_busy",
                "other_task_id": "a",
            }
        }
        assert ledger.next_resumable(config=config)["run_id"] == "a"
        assert ledger.next_resumable(config=config, exclude={"a"})["run_id"] == "b"
        assert ledger.next_resumable(config=config, exclude={"a", "b"}) is None
        assert ledger.list_runs() == before

    @pytest.mark.parametrize("kind", ["merge", "plan", "scope"])
    def test_resuming_held_pending_approval_kind(self, ledger, tmp_path, kind):
        for n, (run_id, status) in enumerate(
            (("back", "resuming"), ("sibling", "awaiting_approval"))
        ):
            spec = make_spec(tmp_path, run_id, status=status)
            key = ledger.upsert_task(spec, render_task(spec))
            ledger.create_run(
                {**run_row(run_id, key, str(tmp_path), status), "started_at": 100.0 + n}
            )
        ledger.record_approval("sibling", kind, "pending")
        config = {"max_runs_per_project": 1}

        assert ledger.resume_holds(config=config) == {
            "back": {"run_id": "sibling", "status": "awaiting_approval", "pending": kind}
        }
        assert ledger.next_resumable(config=config) is None

    def test_hand_back_marks_run_and_task_resuming_at_a_stage(self, ledger: Ledger, tmp_path: Path):
        key = ledger.upsert_task(make_spec(tmp_path, "t"), "")
        ledger.create_run(run_row("r1", key, str(tmp_path), status="awaiting_approval"))

        assert ledger.hand_back("r1", "implement")

        row = ledger.get_run("r1")
        assert (row["status"], row["stage"]) == ("resuming", "implement")
        assert ledger.get_task(key)["status"] == "resuming"
        assert ledger.next_resumable()["run_id"] == "r1"
        assert not ledger.hand_back("missing", "verify")

    def test_resuming_run_holds_its_projects_queued_tasks(self, ledger: Ledger, tmp_path: Path):
        p1, p2 = tmp_path / "p1", tmp_path / "p2"
        back = ledger.upsert_task(make_spec(p1, "back"), "")
        ledger.create_run(run_row("r1", back, str(p1), status="failed"))
        ledger.hand_back("r1", "verify")
        waiting = ledger.upsert_task(make_spec(p1, "waiting", priority="critical"), "")
        other = ledger.upsert_task(make_spec(p2, "other", priority="low"), "")

        assert ledger.next_queued()["task_id"] == other
        assert ledger.held_tasks() == {
            waiting: {"run_id": "r1", "status": "resuming", "pending": None}
        }

    @pytest.mark.parametrize("status", ["running", "awaiting_approval"])
    def test_another_active_run_of_the_project_holds_it(
        self, ledger: Ledger, tmp_path: Path, status: str
    ):
        p1, p2 = tmp_path / "p1", tmp_path / "p2"
        key = ledger.upsert_task(make_spec(p1, "t"), "")
        ledger.create_run({**run_row("r-old", key, str(p1), "failed"), "started_at": 1.0})
        ledger.hand_back("r-old", "verify")
        ledger.create_run({**run_row("r-now", key, str(p1), status), "started_at": 2.0})
        other = ledger.upsert_task(make_spec(p2, "o"), "")
        ledger.create_run({**run_row("r-p2", other, str(p2), "failed"), "started_at": 3.0})
        ledger.hand_back("r-p2", "verify")

        assert ledger.next_resumable()["run_id"] == "r-p2"
        assert ledger.next_resumable(project=str(p1)) is None
        ledger.finish_run("r-now", "landed")
        assert ledger.next_resumable(project=str(p1))["run_id"] == "r-old"

    def test_two_resuming_runs_of_a_project_go_oldest_first(self, ledger: Ledger, tmp_path: Path):
        key = ledger.upsert_task(make_spec(tmp_path, "t"), "")
        for run_id, started in (("r-new", 2.0), ("r-old", 1.0)):
            ledger.create_run(
                {**run_row(run_id, key, str(tmp_path), "failed"), "started_at": started}
            )
            ledger.hand_back(run_id, "verify")

        assert ledger.next_resumable()["run_id"] == "r-old"
        assert ledger.claim_resumable("r-old")
        # The claimed run is now running and holds the project for the other one.
        assert ledger.get_run("r-old")["status"] == "running"
        assert not ledger.claim_resumable("r-old")
        assert ledger.next_resumable() is None


class TestProjectScope:
    def test_matches_include_and_exclude_with_home_expanded(self):
        home = str(Path("~/p1").expanduser())
        assert ProjectScope.of().matches("/any")
        assert ProjectScope.of(["~/p1", "/p2/"]).matches(home)
        assert ProjectScope.of(["~/p1", "/p2/"]).matches("/p2")
        assert not ProjectScope.of(["/p2"]).matches("/p3")
        assert not ProjectScope.of(None, ["~/p1"]).matches(home)
        assert not ProjectScope.of(["/p2"]).without(["/p2"]).matches("/p2")

    def test_queue_and_resume_respect_the_scope(self, ledger: Ledger, tmp_path: Path):
        p1, p2, p3 = tmp_path / "p1", tmp_path / "p2", tmp_path / "p3"
        first = ledger.upsert_task(make_spec(p1, "a", priority="critical"), "")
        second = ledger.upsert_task(make_spec(p2, "b", priority="high"), "")
        third = ledger.upsert_task(make_spec(p3, "c"), "")
        back = ledger.upsert_task(make_spec(p3, "back"), "")
        ledger.create_run(run_row("r3", back, str(p3), status="failed"))
        ledger.hand_back("r3", "verify")

        assert ledger.next_queued(scope=ProjectScope.of([p2, p3]))["task_id"] == second
        assert ledger.next_queued(scope=ProjectScope.of(None, [p1]))["task_id"] == second
        assert ledger.next_queued(scope=ProjectScope.of([p1]).without([str(p1)])) is None
        assert ledger.next_queued(scope=ProjectScope.of([]), exclude=()) is None
        assert ledger.next_queued(scope=ProjectScope.of())["task_id"] == first
        assert ledger.next_resumable(scope=ProjectScope.of([p1, p2])) is None
        assert ledger.next_resumable(scope=ProjectScope.of([p3]))["run_id"] == "r3"
        assert ledger.next_resumable(scope=ProjectScope.of(None, [p3])) is None
        held = ledger.held_tasks()
        assert set(held) == {third}
        assert ledger.held_tasks(scope=ProjectScope.of([p3])) == held
        assert ledger.held_tasks(scope=ProjectScope.of([p1, p2])) == {}


class TestRunsAndEvents:
    def test_run_lifecycle(self, ledger: Ledger, tmp_path: Path):
        key = ledger.upsert_task(make_spec(tmp_path), "")
        ledger.create_run(run_row("r1", key, str(tmp_path), status="starting"))
        run = ledger.get_run("r1")
        assert run is not None
        assert run["status"] == "starting" and run["escalated"] == 0 and run["ended_at"] is None

        assert ledger.update_run("r1", stage="implement", status="running", cost_usd=0.5)
        run = ledger.get_run("r1")
        assert run is not None and run["stage"] == "implement" and run["cost_usd"] == 0.5

        assert ledger.finish_run("r1", "failed", failure_reason="verify_exhausted")
        run = ledger.get_run("r1")
        assert run is not None
        assert run["status"] == "failed"
        assert run["failure_reason"] == "verify_exhausted"
        assert run["ended_at"] is not None

    def test_create_run_rejects_unknown_columns(self, ledger: Ledger, tmp_path: Path):
        key = ledger.upsert_task(make_spec(tmp_path), "")
        with pytest.raises(ValueError, match="unknown run columns: bogus"):
            ledger.create_run({**run_row("r1", key, str(tmp_path)), "bogus": 1})
        with pytest.raises(ValueError, match="unknown run columns"):
            ledger.update_run("r1", nope=1)

    def test_create_run_requires_existing_task(self, ledger: Ledger, tmp_path: Path):
        with pytest.raises(sqlite3.IntegrityError):
            ledger.create_run(run_row("r1", "ghost", str(tmp_path)))

    def test_list_runs_filters(self, ledger: Ledger, tmp_path: Path):
        key = ledger.upsert_task(make_spec(tmp_path), "")
        ledger.create_run({**run_row("r-old", key, str(tmp_path)), "started_at": 100.0})
        ledger.create_run(run_row("r-new", key, str(tmp_path), status="landed"))
        assert [r["run_id"] for r in ledger.list_runs()] == ["r-new", "r-old"]
        assert [r["run_id"] for r in ledger.list_runs(since=1000.0)] == ["r-new"]
        assert [r["run_id"] for r in ledger.list_runs(status="running")] == ["r-old"]
        assert ledger.list_runs(project="/elsewhere") == []

    def test_events_append_and_read_since(self, ledger: Ledger, tmp_path: Path):
        key = ledger.upsert_task(make_spec(tmp_path), "")
        ledger.create_run(run_row("r1", key, str(tmp_path)))
        first = ledger.append_event("r1", "helmraiser", "stage_start", {"stage": "implement"})
        ledger.append_event(
            "r1",
            "pi",
            "message_end",
            {"usage": {"cost": 1}},
            stage="implement",
            session_id="r1-implement",
        )
        events = ledger.events("r1")
        assert [e["type"] for e in events] == ["stage_start", "message_end"]
        assert events[0]["payload"] == {"stage": "implement"}
        assert events[1]["session_id"] == "r1-implement"
        assert [e["type"] for e in ledger.events("r1", since_id=first)] == ["message_end"]

    def test_iterations_evidence_approvals(self, ledger: Ledger, tmp_path: Path):
        key = ledger.upsert_task(make_spec(tmp_path), "")
        ledger.create_run(run_row("r1", key, str(tmp_path)))
        ledger.start_iteration("r1", 1, "r1-implement", "a/impl:medium")
        results = [{"name": "tests", "passed": False, "exit_code": 1, "seconds": 2.0, "tail": "x"}]
        assert ledger.finish_iteration(
            "r1",
            1,
            agent_ok=True,
            commit_sha="abc",
            verify_passed=False,
            verify_results=results,
            cost_usd=0.2,
        )
        (it,) = ledger.iterations("r1")
        assert it["verify_results"] == results
        assert it["verify_passed"] == 0 and it["agent_ok"] == 1 and it["cost_usd"] == 0.2

        ledger.record_evidence("r1", "shots", ok=True, exit_code=0, artifacts=["a.png"])
        approval_id = ledger.record_approval("r1", "merge", "approved", note="lgtm")
        assert approval_id >= 1
        conn = sqlite3.connect(tmp_path / "helmraiser.db")
        assert conn.execute("SELECT artifacts FROM evidence").fetchone()[0] == '["a.png"]'
        assert conn.execute("SELECT decision FROM approvals").fetchone()[0] == "approved"


class TestQueueState:
    def test_pause_resume(self, ledger: Ledger):
        assert ledger.is_paused() is None
        ledger.pause_queue("quota", until=time.time() + 60)
        paused = ledger.is_paused()
        assert paused is not None and paused["reason"] == "quota"
        assert paused["until"] is not None
        ledger.resume_queue()
        assert ledger.is_paused() is None

    def test_pause_expires(self, ledger: Ledger):
        ledger.pause_queue("quota", until=time.time() - 1)
        assert ledger.is_paused() is None

    def test_indefinite_pause(self, ledger: Ledger):
        ledger.pause_queue("manual")
        paused = ledger.is_paused()
        assert paused is not None and paused["until"] is None and paused["reason"] == "manual"

    def test_provider_holds_expire_and_can_be_cleared(self, ledger: Ledger):
        now = time.time()
        ledger.hold_provider("opencode-go", now + 60, "429 quota")
        ledger.hold_provider("expired", now - 1, None)
        assert ledger.provider_holds(now) == {
            "opencode-go": {"until": now + 60, "detail": "429 quota"}
        }
        ledger.clear_provider_hold("opencode-go")
        assert ledger.provider_holds(now) == {}

    def test_resume_clears_provider_holds(self, ledger: Ledger):
        now = time.time()
        ledger.pause_queue("manual")
        ledger.hold_provider("opencode-go", now + 60, "quota")
        ledger.resume_queue()
        assert ledger.is_paused() is None
        assert ledger.provider_holds(now) == {}

    def test_caps_switch(self, ledger: Ledger):
        assert ledger.caps_switch() is None
        ledger.set_caps_switch("off")
        assert ledger.caps_switch() == "off"
        ledger.set_caps_switch("on")
        assert ledger.caps_switch() == "on"
        ledger.set_caps_switch(None)
        assert ledger.caps_switch() is None


class TestRunDir:
    def test_layout(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HELMRAISER_DATA_DIR", str(tmp_path))
        rd = RunDir("abc").create()
        assert rd.path == tmp_path / "runs" / "abc"
        assert rd.sessions.is_dir() and rd.evidence.is_dir()
        assert rd.task_md == rd.path / "task.md"
        assert rd.events_jsonl == rd.path / "events.jsonl"
        assert rd.directives_md.name == "directives.md"
        assert (rd.review_md.name, rd.diff_patch.name, rd.summary_md.name) == (
            "review.md",
            "diff.patch",
            "summary.md",
        )
        rd.event_logger().log("stage_start", stage="implement")
        assert '"stage_start"' in rd.events_jsonl.read_text()

    def test_explicit_data_dir(self, tmp_path: Path):
        assert RunDir("x", data_dir=tmp_path / "d").path == tmp_path / "d" / "runs" / "x"


def test_opening_previous_runs_schema_adds_delivery_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE runs (
          run_id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL,
          project TEXT NOT NULL, worktree_path TEXT, branch_name TEXT,
          base_branch TEXT, base_commit TEXT, head_commit TEXT,
          status TEXT NOT NULL, stage TEXT,
          policy TEXT NOT NULL, isolation TEXT NOT NULL,
          model_implement TEXT NOT NULL, model_fallback TEXT, model_review TEXT NOT NULL,
          escalated INTEGER NOT NULL DEFAULT 0, pi_version TEXT NOT NULL,
          cost_usd REAL NOT NULL DEFAULT 0, tokens_in INTEGER NOT NULL DEFAULT 0,
          tokens_out INTEGER NOT NULL DEFAULT 0, verdict TEXT, failure_reason TEXT,
          started_at REAL NOT NULL, ended_at REAL
        )""")
    conn.commit()
    conn.close()

    ledger = Ledger(db_path)

    columns = {
        row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(runs)").fetchall()
    }
    assert {"delivery", "pr_url"} <= columns
    assert ledger.list_runs() == []


def test_ledger_close_reopen_context_and_gc(tmp_path: Path) -> None:
    """close() reopens on next use; context manager closes; GC emits no ResourceWarning."""
    ledger = Ledger(tmp_path / "close.db")
    assert ledger.list_tasks() == []
    ledger.close()
    assert ledger.list_tasks() == []  # a read after close reopens

    with Ledger(tmp_path / "ctx.db") as ctx:
        assert ctx.list_tasks() == []
    assert ctx._local.conn is None

    obj = Ledger(tmp_path / "gc.db")
    obj.list_tasks()
    gc.collect()  # sweep garbage from unrelated tests before recording
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        del obj
        gc.collect()
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]


def test_depends_on_queue_skips_and_scope(ledger, tmp_project, tmp_path):
    from helmraiser.task.loader import render_task

    held = make_spec(tmp_project, "held", priority="high", depends_on=("upstream",))
    free = make_spec(tmp_project, "free")
    other = make_spec(tmp_path / "other", "other", priority="low")
    for spec in (held, free, other):
        ledger.upsert_task(spec, render_task(spec))
    before = ledger.get_task(held.task_id)
    assert ledger.next_queued()["task_id"] == free.task_id
    assert ledger.next_queued(exclude=(free.task_id,))["task_id"] == other.task_id
    assert ledger.next_queued(project=str(tmp_project), exclude=(free.task_id,)) is None
    assert (
        ledger.next_queued(scope=ProjectScope.of(exclude=[tmp_project]))["task_id"] == other.task_id
    )
    assert (
        ledger.next_queued(scope=ProjectScope.of(include=[tmp_project]))["task_id"] == free.task_id
    )
    upstream = make_spec(tmp_project, "upstream")
    ledger.upsert_task(upstream, render_task(upstream))
    assert ledger.next_queued(exclude=(free.task_id,))["task_id"] == upstream.task_id
    assert ledger.get_task(held.task_id) == before


def test_depends_on_queue_release(ledger, tmp_project):
    from helmraiser.task.loader import render_task
    from tests.ledger.test_depends_on import feature, git

    base, head = feature(tmp_project)
    upstream = make_spec(tmp_project, "upstream", status="landed")
    dependent = make_spec(tmp_project, "dependent", depends_on=("upstream",), base=base)
    for spec in (upstream, dependent):
        ledger.upsert_task(spec, render_task(spec))
    ledger.create_run(
        {**run_row("r", upstream.task_id, str(tmp_project), "landed"), "head_commit": head}
    )
    assert ledger.next_queued() is None
    git(tmp_project, "merge", "--no-ff", "feature", "-m", "merge")
    assert ledger.next_queued()["task_id"] == dependent.task_id
    assert ledger.held_tasks() == {}


def test_depends_on_combined_hold(ledger, tmp_project):
    from helmraiser.task.loader import render_task

    held = make_spec(tmp_project, depends_on=("missing",))
    free = make_spec(tmp_project, "free")
    for spec in (held, free):
        ledger.upsert_task(spec, render_task(spec))
    ledger.create_run(run_row("r", free.task_id, str(tmp_project), "awaiting_approval"))
    holds = ledger.held_tasks()
    legacy = {"run_id": "r", "status": "awaiting_approval", "pending": "merge"}
    assert holds[free.task_id] == legacy
    assert holds[held.task_id] == {
        **legacy,
        "reason": "depends_on",
        "unmet": [{"id": "missing", "reason": "unknown", "text": "missing: unknown dependency id"}],
    }
    assert ledger.next_queued() is None
