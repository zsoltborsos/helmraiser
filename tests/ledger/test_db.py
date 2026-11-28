"""Ledger tests: DDL, next_queued ordering, project exclusivity and queue state."""

from __future__ import annotations

import gc
import json
import sqlite3
import threading
import time
import warnings
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any, cast

import pytest

import helmraiser.ledger.db as ledger_db
from helmraiser.ledger.db import (
    LIVE_RUN_STATUSES,
    PREVIOUS_SCHEMA_VERSION,
    SCHEMA_VERSION,
    Ledger,
    ProjectScope,
    RunUnavailable,
    TaskActive,
    default_db_path,
    is_busy,
)
from helmraiser.ledger.rundir import RunDir
from helmraiser.task.loader import render_task
from helmraiser.task.model import Budget, ModelSpec, TaskSpec, VerifyStep


def schema_shape(conn: sqlite3.Connection) -> dict[str, Any]:
    """Tables' columns and foreign keys, indexes and ``user_version``, ignoring column order
    and DDL text, so a created and an upgraded ledger compare equal."""
    shape: dict[str, Any] = {}
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    for (table,) in tables:
        shape[f"table:{table}"] = sorted(
            list(row)[1:] for row in conn.execute(f"PRAGMA table_xinfo({table})")
        )
        shape[f"fk:{table}"] = sorted(
            list(row)[2:5] for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        )
        for index in conn.execute(f"PRAGMA index_list({table})").fetchall():
            name = index[1]
            columns = [row[2] for row in conn.execute(f"PRAGMA index_info({name})")]
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()
            shape[f"index:{table}:{name}"] = [
                index[2],
                index[3],
                index[4],
                columns,
                sql[0] if sql else None,
            ]
    shape["user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
    return shape


def frozen_shape(conn: sqlite3.Connection) -> dict[str, Any]:
    """``schema_shape`` as read back from its JSON file (lists, not tuples)."""
    return cast("dict[str, Any]", json.loads(json.dumps(schema_shape(conn))))


def expected_shape(version: int) -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        json.loads((Path(__file__).parent / f"schema_v{version}.json").read_text()),
    )


def make_v11_ledger(path: Path, run_status: str = "awaiting_approval") -> list[str]:
    """A version 11 ledger as the previous Helmraiser created it, holding a task, a run
    in ``run_status``, an event and an approval; returns its dump."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        for statement in ledger_db._SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.execute("PRAGMA user_version = 11")
        conn.execute(
            "INSERT INTO tasks (task_id, id, project, title, priority, status, source_path, "
            "snapshot, created_at, updated_at) VALUES "
            "('p:t1', 't1', '/repo', 'Task t1', 2, 'running', '/repo/t1.md', 'snap', 1, 2)"
        )
        conn.execute(
            "INSERT INTO runs (run_id, task_id, project, status, policy, isolation, "
            "model_implement, model_review, pi_version, started_at) VALUES "
            "('run-1', 'p:t1', '/repo', ?, 'review', 'none', 'a/impl', 'a/review', '0.87', 3)",
            (run_status,),
        )
        conn.execute(
            "INSERT INTO run_events (run_id, ts, source, type, payload) "
            "VALUES ('run-1', 4, 'helmraiser', 'example', '{}')"
        )
        conn.execute(
            "INSERT INTO approvals (run_id, kind, decision, decided_at) "
            "VALUES ('run-1', 'merge', 'pending', 5)"
        )
        conn.commit()
        assert frozen_shape(conn) == expected_shape(11)
        return list(conn.iterdump())
    finally:
        conn.close()


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.v11-*.bak"))


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


def busy_error(tmp_path: Path) -> sqlite3.OperationalError:
    """The error SQLite raises when another connection holds the write lock."""
    path = tmp_path / "busy.db"
    holder = sqlite3.connect(path, isolation_level=None)
    other = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as caught:
            other.execute("BEGIN IMMEDIATE")
        return caught.value
    finally:
        other.close()
        holder.close()


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "helmraiser.db")


class TestConnection:
    def test_writes_sync_only_at_checkpoints(self, ledger: Ledger):
        # NORMAL: a commit holding the write lock never waits on fsync.
        assert ledger._get_conn().execute("PRAGMA synchronous").fetchone()[0] == 1

    def test_is_busy_only_for_a_held_lock(self, tmp_path: Path):
        assert is_busy(busy_error(tmp_path))
        assert not is_busy(sqlite3.OperationalError("no such table: runs"))
        with pytest.raises(sqlite3.OperationalError) as caught:
            sqlite3.connect(":memory:").execute("SELECT * FROM missing")
        assert not is_busy(caught.value)


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

    def test_read_only_refuses_an_older_version(self, ledger):
        conn = ledger._get_conn()
        conn.execute("PRAGMA user_version = 8")
        before = list(conn.iterdump())
        with pytest.raises(ValueError, match="schema version 8"):
            Ledger(ledger.db_path, read_only=True)
        assert list(conn.iterdump()) == before
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8

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

    def test_new_ledger_schema_matches_version_12(self, ledger: Ledger, tmp_path: Path):
        assert SCHEMA_VERSION == 12
        assert ledger.experiments_available and ledger.upgrade_pending is None
        conn = sqlite3.connect(tmp_path / "helmraiser.db")
        try:
            assert frozen_shape(conn) == expected_shape(12)
        finally:
            conn.close()

    @pytest.mark.parametrize(
        "version",
        [
            v
            for v in range(1, SCHEMA_VERSION + 2)
            if v not in (PREVIOUS_SCHEMA_VERSION, SCHEMA_VERSION)
        ],
    )
    def test_other_version_is_refused_unchanged(self, tmp_path: Path, version: int):
        path = tmp_path / "helmraiser.db"
        Ledger(path).close()
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        before = list(conn.iterdump())
        message = f"has schema version {version}; this Helmraiser expects version {SCHEMA_VERSION}"
        try:
            for read_only in (False, True):
                with pytest.raises(ValueError, match=message):
                    Ledger(path, read_only=read_only)
            assert list(conn.iterdump()) == before
            assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        finally:
            conn.close()

    def test_read_only_refuses_an_empty_file(self, tmp_path: Path):
        path = tmp_path / "helmraiser.db"
        sqlite3.connect(path).close()
        with pytest.raises(ValueError, match="no schema"):
            Ledger(path, read_only=True)

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

    def test_concurrent_opens_of_a_new_ledger_all_succeed(self, tmp_path: Path):
        path = tmp_path / "helmraiser.db"
        # An empty file already in WAL mode: concurrent opens race only on the schema.
        empty = sqlite3.connect(path)
        empty.execute("PRAGMA journal_mode=WAL").fetchone()
        empty.close()
        barrier = threading.Barrier(8)
        errors: list[BaseException] = []

        def open_ledger() -> None:
            barrier.wait()
            try:
                with Ledger(path) as ledger:
                    ledger.is_paused()
            except (sqlite3.Error, ValueError) as exc:
                errors.append(exc)

        threads = [threading.Thread(target=open_ledger) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert errors == []
        conn = sqlite3.connect(path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        conn.close()

    def test_default_path_honours_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HELMRAISER_DATA_DIR", str(tmp_path / "data"))
        assert default_db_path() == tmp_path / "data" / "helmraiser.db"
        ledger = Ledger()
        assert ledger.db_path == tmp_path / "data" / "helmraiser.db"
        assert ledger.db_path.exists()


@pytest.fixture
def distinct_stamps(monkeypatch):
    """Give every backup its own timestamp, so a second backup cannot overwrite the first."""
    ticks = count()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 25, 12, tzinfo=UTC) + timedelta(seconds=next(ticks))

    monkeypatch.setattr(ledger_db, "datetime", Clock)


class TestUpgradeFromVersion11:
    def test_live_statuses_are_those_with_a_process(self):
        assert LIVE_RUN_STATUSES == {"starting", "running", "verifying", "reviewing", "aborting"}

    def test_upgrade_keeps_rows_adds_tables_and_backs_up(self, tmp_path: Path, distinct_stamps):
        path = tmp_path / "helmraiser.db"
        before = make_v11_ledger(path)

        with Ledger(path) as ledger:
            assert ledger.experiments_available and ledger.upgrade_pending is None
            assert ledger.get_task("p:t1")["title"] == "Task t1"
            assert ledger.get_run("run-1")["status"] == "awaiting_approval"
            assert [e["type"] for e in ledger.events("run-1")] == ["example"]
            assert ledger.active_experiment() is None

        (backup,) = backups(path)
        assert backup.name == "helmraiser.db.v11-20261125T120000Z.bak"
        conn = sqlite3.connect(path)
        saved = sqlite3.connect(backup)
        try:
            assert frozen_shape(conn) == expected_shape(12)
            # Every version 11 row is still there, and the backup is the ledger as it was.
            dump = list(conn.iterdump())
            assert set(before) <= set(dump)
            assert list(saved.iterdump()) == before
            assert saved.execute("PRAGMA user_version").fetchone()[0] == 11
        finally:
            conn.close()
            saved.close()

        Ledger(path).close()
        assert len(backups(path)) == 1

    @pytest.mark.parametrize("status", sorted(LIVE_RUN_STATUSES))
    def test_live_run_defers_the_upgrade_to_a_later_open(
        self, tmp_path: Path, status: str, distinct_stamps
    ):
        path = tmp_path / "helmraiser.db"
        make_v11_ledger(path, run_status=status)
        message = "ledger upgrade to version 12 pending: 1 run still running"

        with Ledger(path) as ledger:
            assert not ledger.experiments_available
            assert ledger.upgrade_pending == message
            with pytest.raises(ValueError, match=message):
                ledger.active_experiment()
            with pytest.raises(ValueError, match=message):
                ledger.start_experiment("e1", {"A": {}, "B": {}}, quota_brake=0.85)
            # Everything that exists at version 11 still works.
            task_id = ledger.upsert_task(make_spec(tmp_path, "t2"), "snapshot")
            ledger.create_run(run_row("run-2", task_id, str(tmp_path)))
            ledger.append_event("run-2", "helmraiser", "example", {})
            assert {r["run_id"] for r in ledger.list_runs()} == {"run-1", "run-2"}
            assert ledger._get_conn().execute("PRAGMA user_version").fetchone()[0] == 11
            with Ledger(path) as again:
                assert again.upgrade_pending == (
                    "ledger upgrade to version 12 pending: 2 runs still running"
                )
            ledger.finish_run("run-1", "failed")
            ledger.finish_run("run-2", "landed")
        assert backups(path) == []

        with Ledger(path) as ledger:
            assert ledger.experiments_available
            assert ledger.active_experiment() is None
        assert len(backups(path)) == 1
        conn = sqlite3.connect(path)
        try:
            assert frozen_shape(conn) == expected_shape(12)
        finally:
            conn.close()

    @pytest.mark.parametrize("status", ["awaiting_approval", "resuming", "landed", "failed"])
    def test_runs_without_a_process_do_not_defer_the_upgrade(self, tmp_path: Path, status: str):
        path = tmp_path / "helmraiser.db"
        make_v11_ledger(path, run_status=status)
        with Ledger(path) as ledger:
            assert ledger.experiments_available
            assert ledger.get_run("run-1")["status"] == status

    def test_read_only_open_does_not_upgrade(self, tmp_path: Path):
        path = tmp_path / "helmraiser.db"
        before = make_v11_ledger(path)
        with Ledger(path, read_only=True) as ledger:
            assert ledger.upgrade_pending == (
                "ledger upgrade to version 12 pending: opened read-only"
            )
            assert ledger.get_run("run-1") is not None
        conn = sqlite3.connect(path)
        try:
            assert list(conn.iterdump()) == before
        finally:
            conn.close()
        assert backups(path) == []

    def test_concurrent_openers_upgrade_once(self, tmp_path: Path, distinct_stamps):
        path = tmp_path / "helmraiser.db"
        make_v11_ledger(path)
        barrier = threading.Barrier(8)
        errors: list[BaseException] = []
        available: list[bool] = []

        def open_ledger() -> None:
            barrier.wait()
            try:
                with Ledger(path) as ledger:
                    available.append(ledger.experiments_available)
                    ledger.is_paused()
            except (sqlite3.Error, ValueError) as exc:
                errors.append(exc)

        threads = [threading.Thread(target=open_ledger) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert errors == []
        assert available == [True] * 8
        assert len(backups(path)) == 1
        conn = sqlite3.connect(path)
        try:
            assert frozen_shape(conn) == expected_shape(12)
        finally:
            conn.close()

    def test_backup_runs_without_the_write_lock(self, tmp_path: Path, monkeypatch):
        """A multi-gigabyte backup must not block other writers: they would time out."""
        import helmraiser.ledger.db as db_module

        path = tmp_path / "helmraiser.db"
        make_v11_ledger(path)
        real_connect = sqlite3.connect
        probes: list[bool] = []

        def connect(target, *args, **kwargs):  # type: ignore[no-untyped-def]
            if str(target).endswith(".bak"):
                probe = real_connect(path, timeout=0)
                try:
                    probe.execute("BEGIN IMMEDIATE")
                    probe.execute("ROLLBACK")
                    probes.append(True)
                except sqlite3.OperationalError:
                    probes.append(False)
                finally:
                    probe.close()
            return real_connect(target, *args, **kwargs)

        monkeypatch.setattr(db_module.sqlite3, "connect", connect)
        with Ledger(path) as ledger:
            assert ledger.experiments_available
        assert probes == [True]

    def test_failed_backup_leaves_version_11(self, tmp_path: Path, distinct_stamps):
        path = tmp_path / "helmraiser.db"
        before = make_v11_ledger(path)
        # A directory where the backup would go: SQLite cannot open it.
        (tmp_path / "helmraiser.db.v11-20261125T120000Z.bak").mkdir()
        with Ledger(path) as ledger:
            assert not ledger.experiments_available
            assert ledger.upgrade_pending is not None
            assert "backup to" in ledger.upgrade_pending
            assert ledger.get_run("run-1") is not None
        conn = sqlite3.connect(path)
        try:
            assert list(conn.iterdump()) == before
        finally:
            conn.close()

        with Ledger(path) as ledger:
            assert ledger.experiments_available
        assert [b.name for b in backups(path) if b.is_file()] == [
            "helmraiser.db.v11-20261125T120001Z.bak"
        ]


class TestExperiments:
    ARMS = {"A": {"skills": "default"}, "B": {"skills": "none"}}

    def test_start_freezes_the_definition(self, ledger: Ledger):
        experiment_id = ledger.start_experiment(
            "skills",
            self.ARMS,
            quota_brake=0.95,
            end_pairs=30,
            end_at=1_800_000_000.0,
            exclude_projects=["/repo/x"],
        )
        active = ledger.active_experiment()
        assert active is not None
        assert active == ledger.get_experiment("skills")
        assert active["id"] == experiment_id
        assert active["arms"] == self.ARMS
        assert active["exclude_projects"] == ["/repo/x"]
        assert (active["status"], active["end_pairs"], active["quota_brake"]) == (
            "active",
            30,
            0.95,
        )
        assert active["ended_at"] is None and active["end_reason"] is None
        assert ledger.get_experiment("missing") is None

    def test_only_one_experiment_is_active(self, ledger: Ledger):
        first = ledger.start_experiment("one", self.ARMS, quota_brake=0.85)
        with pytest.raises(ValueError, match="experiment one is already active"):
            ledger.start_experiment("two", self.ARMS, quota_brake=0.85)
        # The schema enforces it too, not only the method.
        conn = ledger._get_conn()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO experiments (name, arms, quota_brake, started_at) "
                "VALUES ('raw', '{}', 0.85, 0)"
            )
        conn.rollback()
        assert ledger.end_experiment(first, "stopped")
        assert not ledger.end_experiment(first, "stopped")
        ended = ledger.get_experiment("one")
        assert ended is not None
        assert (ended["status"], ended["end_reason"]) == ("ended", "stopped")
        assert ended["ended_at"] is not None
        assert ledger.active_experiment() is None
        with pytest.raises(ValueError, match="experiment one already exists"):
            ledger.start_experiment("one", self.ARMS, quota_brake=0.85)
        second = ledger.start_experiment("two", self.ARMS, quota_brake=0.85)
        assert ledger.active_experiment()["id"] == second

    def test_set_pick_succeeds_once(self, ledger: Ledger, tmp_path: Path):
        experiment_id = ledger.start_experiment("e", self.ARMS, quota_brake=0.85)
        task_id = ledger.upsert_task(make_spec(tmp_path), "snapshot")
        pair_id = ledger.create_pair(experiment_id, task_id, "B", "A")
        with pytest.raises(sqlite3.IntegrityError):
            ledger.create_pair(experiment_id, task_id, "A", "B")
        pair = ledger.get_pair(experiment_id, task_id)
        assert pair is not None
        assert (pair["id"], pair["arm_x"], pair["arm_y"]) == (pair_id, "B", "A")
        assert (pair["run_x"], pair["run_y"], pair["pick"], pair["unblinded"]) == (
            None,
            None,
            None,
            False,
        )

        assert ledger.set_pair_pick(
            pair_id, "X", decided_by="judged", reason="cleaner diff", judge="claude-code/oracle"
        )
        assert not ledger.set_pair_pick(pair_id, "Y", decided_by="failure")
        assert not ledger.set_pair_pick(pair_id + 1, "Y", decided_by="failure")
        pair = ledger.get_pair(experiment_id, task_id)
        assert pair is not None
        assert (pair["pick"], pair["decided_by"], pair["reason"], pair["judge"]) == (
            "X",
            "judged",
            "cleaner diff",
            "claude-code/oracle",
        )
        assert pair["picked_at"] is not None

    @pytest.mark.parametrize(
        ("pick", "decided_by"),
        [("Z", "judged"), ("X", "coin"), ("tie", "judged"), ("X", "tie")],
    )
    def test_set_pick_refuses_invalid_values(self, ledger: Ledger, pick: str, decided_by: str):
        with pytest.raises(ValueError):
            ledger.set_pair_pick(1, pick, decided_by=decided_by)


ARMS = {"A": {"skills": "default"}, "B": {"skills": "none"}}
ARM_EVENT = ("experiment_arm", {"name": "A", "overrides": ARMS["A"], "replaced": {}})
ROUTING_EVENT = ("routing", {"lever": "plan", "from": False, "to": True})


def claim_pair(
    ledger: Ledger,
    experiment_id: int,
    spec: TaskSpec,
    run_x: str = "x",
    run_y: str = "y",
    config: dict[str, Any] | None = None,
) -> str:
    """Claim queued ``spec`` as a pair: X on arm B, Y on arm A."""
    project = str(spec.project)
    return ledger.claim_queued_run(
        run_row(run_x, spec.task_id, project, "starting"),
        snapshot=render_task(spec),
        config=config or {},
        pair={
            "experiment_id": experiment_id,
            "arm_x": "B",
            "arm_y": "A",
            "run": {**run_row(run_y, spec.task_id, project), "model_implement": "y/impl"},
            "events": [ARM_EVENT, ROUTING_EVENT],
        },
    )


@pytest.fixture
def experiment(ledger: Ledger) -> int:
    return ledger.start_experiment("e", ARMS, quota_brake=0.85)


class TestPairClaim:
    def test_claim_records_both_runs_the_pair_and_the_arm_event(
        self, ledger: Ledger, tmp_path: Path, experiment: int
    ):
        spec = make_spec(tmp_path)
        ledger.upsert_task(spec, render_task(spec))

        assert claim_pair(ledger, experiment, spec) == "x"

        x, y = ledger.get_run("x"), ledger.get_run("y")
        assert (x["status"], x["stage"]) == ("starting", None)
        assert (y["status"], y["stage"]) == ("resuming", "new")
        assert y["model_implement"] == "y/impl" and y["started_at"] == x["started_at"]
        pair = ledger.get_pair(experiment, spec.task_id)
        assert (pair["run_x"], pair["run_y"], pair["arm_x"], pair["arm_y"]) == ("x", "y", "B", "A")
        assert pair["pick"] is None
        assert ledger.pair_for_run("x") == ledger.pair_for_run("y") == pair
        assert ledger.pair_for_run("other") is None
        assert [(e["type"], e["payload"]) for e in ledger.events("y")] == [
            ARM_EVENT,
            ROUTING_EVENT,
        ]
        assert ledger.events("x") == []
        assert ledger.get_task(spec.task_id)["status"] == "running"

    def test_two_claims_race_and_the_loser_finds_the_task_changed(self, tmp_path: Path):
        db = tmp_path / "race.db"
        spec = make_spec(tmp_path)
        with Ledger(db) as ledger:
            experiment_id = ledger.start_experiment("e", ARMS, quota_brake=0.85)
            ledger.upsert_task(spec, render_task(spec))
        barrier = threading.Barrier(2)
        won, lost, errors = [], [], []

        def claim(n: int) -> None:
            with Ledger(db) as connection:
                barrier.wait(timeout=5)
                try:
                    won.append(claim_pair(connection, experiment_id, spec, f"x{n}", f"y{n}"))
                except RunUnavailable as exc:
                    lost.append(exc.hold)
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=claim, args=(n,)) for n in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
        assert not errors
        assert len(won) == 1 and lost == [{"reason": "task_changed"}]
        with Ledger(db) as ledger:
            run_x, run_y = won[0], won[0].replace("x", "y")
            assert {r["run_id"] for r in ledger.list_runs()} == {run_x, run_y}
            pair = ledger.get_pair(experiment_id, spec.task_id)
            assert (pair["run_x"], pair["run_y"]) == (run_x, run_y)
            assert [e["type"] for e in ledger.events(run_y)] == ["experiment_arm", "routing"]
            assert ledger.get_run(run_y)["status"] == "resuming"

    @pytest.mark.parametrize("change", ["ended", "paired"])
    def test_claim_rechecks_the_experiment(
        self, ledger: Ledger, tmp_path: Path, experiment: int, change: str
    ):
        spec = make_spec(tmp_path)
        ledger.upsert_task(spec, render_task(spec))
        if change == "ended":
            assert ledger.end_experiment(experiment, "stopped")
        else:
            ledger.create_pair(experiment, spec.task_id, "A", "B")

        with pytest.raises(RunUnavailable) as refused:
            claim_pair(ledger, experiment, spec)

        assert refused.value.hold == {"reason": "experiment_changed"}
        assert not ledger.list_runs()
        assert ledger.get_task(spec.task_id)["status"] == "queued"
        assert ledger.events("y") == []
        # Without the pair, the task is claimed as before.
        ledger.claim_queued_run(
            run_row("solo", spec.task_id, str(tmp_path)), snapshot=render_task(spec), config={}
        )
        assert ledger.pair_for_run("solo") is None

    def test_pair_for_run_without_experiment_tables(self, tmp_path: Path):
        path = tmp_path / "v11.db"
        make_v11_ledger(path, run_status="running")
        with Ledger(path) as ledger:
            assert not ledger.experiments_available
            assert ledger.pair_for_run("run-1") is None
            # Admission reads no pairs table either.
            queued = ledger.upsert_task(make_spec(Path("/repo"), "t2"), "")
            assert ledger.held_tasks(config={})[queued]["run_id"] == "run-1"


class TestPairTaskStatus:
    """A pair's task shows the strongest status of its two runs."""

    def pair(self, ledger: Ledger, tmp_path: Path, experiment: int) -> str:
        spec = make_spec(tmp_path)
        ledger.upsert_task(spec, render_task(spec))
        claim_pair(ledger, experiment, spec)
        return spec.task_id

    def end(self, ledger: Ledger, task_id: str, run_id: str, status: str) -> str:
        """As ``GovernedRun._close``: the run first, then its task."""
        ledger.finish_run(run_id, status)
        ledger.set_task_status(task_id, status, run_id=run_id)
        return str(ledger.get_task(task_id)["status"])

    @pytest.mark.parametrize(
        ("x_end", "y_end", "final"),
        [
            ("awaiting_approval", "awaiting_approval", "awaiting_approval"),
            ("awaiting_approval", "failed", "awaiting_approval"),
            ("failed", "awaiting_approval", "awaiting_approval"),
            ("failed", "failed", "failed"),
            ("failed", "aborted", "aborted"),
        ],
    )
    def test_first_end_leaves_the_task_running_then_the_strongest_remains(
        self, ledger: Ledger, tmp_path: Path, experiment: int, x_end: str, y_end: str, final: str
    ):
        task_id = self.pair(ledger, tmp_path, experiment)
        assert ledger.claim_resumable("y")
        ledger.set_task_status(task_id, "running", run_id="y")

        assert self.end(ledger, task_id, "x", x_end) == "running"
        assert self.end(ledger, task_id, "y", y_end) == final

    @pytest.mark.parametrize("x_end", ["awaiting_approval", "failed"])
    def test_task_resumes_while_the_second_run_waits_to_start(
        self, ledger: Ledger, tmp_path: Path, experiment: int, x_end: str
    ):
        task_id = self.pair(ledger, tmp_path, experiment)

        assert self.end(ledger, task_id, "x", x_end) == "resuming"
        assert ledger.claim_resumable("y")
        ledger.set_task_status(task_id, "running", run_id="y")
        assert ledger.get_task(task_id)["status"] == "running"

    @pytest.mark.parametrize(("y_status", "shown"), [("running", "running"), (None, "resuming")])
    def test_hand_back_uses_the_precedence(
        self, ledger: Ledger, tmp_path: Path, experiment: int, y_status: str | None, shown: str
    ):
        task_id = self.pair(ledger, tmp_path, experiment)
        assert ledger.claim_resumable("y")
        self.end(ledger, task_id, "x", "awaiting_approval")
        if y_status is None:
            self.end(ledger, task_id, "y", "awaiting_approval")

        assert ledger.hand_back("x", "implement")

        assert ledger.get_run("x")["status"] == "resuming"
        assert ledger.get_task(task_id)["status"] == shown

    def test_unpaired_task_takes_the_status_given(self, ledger: Ledger, tmp_path: Path):
        task_id = ledger.upsert_task(make_spec(tmp_path), "")
        ledger.create_run(run_row("r", task_id, str(tmp_path), "running"))
        for status in ("running", "awaiting_approval", "failed"):
            ledger.finish_run("r", status)
            assert ledger.set_task_status(task_id, status, run_id="r")
            assert ledger.get_task(task_id)["status"] == status
        # Without a run, the status is written as given whatever the task's runs are.
        ledger.create_run(run_row("live", task_id, str(tmp_path), "running"))
        assert ledger.set_task_status(task_id, "failed")
        assert ledger.get_task(task_id)["status"] == "failed"


class TestPairScheduling:
    def specs(self, ledger: Ledger, tmp_path: Path, **paths: tuple[str, ...]) -> dict:
        specs = {id: make_spec(tmp_path, id, paths=value) for id, value in paths.items()}
        for spec in specs.values():
            ledger.upsert_task(spec, render_task(spec))
        return specs

    def test_limit_one_admits_the_second_run_and_holds_other_tasks(
        self, ledger: Ledger, tmp_path: Path, experiment: int
    ):
        specs = self.specs(ledger, tmp_path, t=("t/**",), other=("other/**",))
        claim_pair(ledger, experiment, specs["t"])

        assert ledger.resume_holds(config={}) == {}
        assert ledger.next_resumable(config={})["run_id"] == "y"
        other = specs["other"].task_id
        assert ledger.held_tasks(config={})[other]["run_id"] in ("x", "y")
        assert ledger.next_queued(config={}) is None
        assert ledger.claim_resumable("y", checked=True, config={})
        assert ledger.next_queued(config={}) is None
        # The pair holds the project until both runs are over.
        ledger.finish_run("x", "failed")
        assert ledger.held_tasks(config={})[other]["run_id"] == "y"
        ledger.finish_run("y", "failed")
        assert ledger.next_queued(config={})["task_id"] == other

    def test_second_run_starts_while_the_first_awaits_approval(
        self, ledger: Ledger, tmp_path: Path, experiment: int
    ):
        specs = self.specs(ledger, tmp_path, t=("t/**",))
        claim_pair(ledger, experiment, specs["t"])
        ledger.finish_run("x", "awaiting_approval")

        assert ledger.claim_resumable("y", checked=True, config={})

    def test_arms_share_paths_and_one_slot(self, ledger: Ledger, tmp_path: Path, experiment: int):
        config = {"max_runs_per_project": 2}
        specs = self.specs(
            ledger, tmp_path, t=("same/**",), overlap=("same/**",), a=("a/**",), b=("b/**",)
        )
        claim_pair(ledger, experiment, specs["t"], config=config)

        # The two runs declare the same paths and do not hold each other.
        assert ledger.resume_holds(config=config) == {}
        assert ledger.claim_resumable("y", checked=True, config=config)
        held = ledger.held_tasks(config=config)
        assert held[specs["overlap"].task_id]["reason"] == "paths_overlap"
        # Both runs take one of the project's two slots: one disjoint task still fits.
        assert specs["a"].task_id not in held and specs["b"].task_id not in held
        ledger.claim_queued_run(
            run_row("a", specs["a"].task_id, str(tmp_path)),
            snapshot=render_task(specs["a"]),
            config=config,
        )
        assert ledger.held_tasks(config=config)[specs["b"].task_id]["reason"] == "project_busy"

    def test_unpaired_runs_of_one_task_still_hold_each_other(self, ledger: Ledger, tmp_path: Path):
        key = ledger.upsert_task(make_spec(tmp_path, "t"), "")
        ledger.create_run({**run_row("old", key, str(tmp_path), "failed"), "started_at": 1.0})
        ledger.hand_back("old", "verify")
        ledger.create_run({**run_row("now", key, str(tmp_path), "running"), "started_at": 2.0})

        assert ledger.next_resumable(config={}) is None
        assert ledger.resume_holds(config={})["old"]["run_id"] == "now"


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


def test_scout_overrides(ledger):
    ledger.set_scout_lens_override("docs", auto=True)
    ledger.set_scout_lens_override("docs", cooldown_hours=12)
    assert ledger.scout_lens_override("docs") == {"auto": True, "cooldown_hours": 12}
    assert ledger.scout_lens_override("tests") == {}
    with pytest.raises(ValueError):
        ledger.set_scout_lens_override("docs", auto=False, cooldown_hours=0)
    assert ledger.scout_lens_override("docs")["auto"] is True
    ledger.reset_scout_lens_override("docs")
    ledger.reset_scout_lens_override("docs")
    assert ledger.scout_lens_override("docs") == {}
