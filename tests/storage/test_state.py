"""Tests for StateManager — SQLite-backed state management."""

import gc
import os
import sqlite3
import sys
import tempfile
import threading
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from helmraiser.storage.state import ProcessLease, StateManager


class TestStateManagerRunLifecycle:
    """StateManager handles run lifecycle correctly."""

    def _make_manager(self, db_path=None, tmpdir=None):
        if db_path is None:
            if tmpdir is None:
                tmpdir = tempfile.mkdtemp()
            db = Path(tmpdir) / "state.db"
        mgr = StateManager(db_path=db_path or db)
        return mgr, tmpdir

    def test_create_run(self):
        mgr, tmpdir = self._make_manager()
        mgr.create_run("run-123", "topic-456")
        run = mgr.get_run("run-123")
        assert run is not None
        assert run["run_id"] == "run-123"
        assert run["topic_id"] == "topic-456"
        assert run["status"] == "starting"

    def test_fresh_database_has_no_phase_schema(self, tmp_path):
        db = tmp_path / "state.db"
        with StateManager(db) as mgr:
            mgr.create_run("run", "topic")
            assert mgr.get_run("run")["status"] == "starting"

        conn = sqlite3.connect(db)
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        finally:
            conn.close()
        assert {"runs", "locks", "active_processes"} <= tables
        assert "phases" not in tables
        assert not {"phase", "hub_url"} & columns

    def test_update_run_status(self):
        mgr, _ = self._make_manager()
        mgr.create_run("run-123", "topic-456")
        mgr.update_run_status("run-123", "running")
        run = mgr.get_run("run-123")
        assert run["status"] == "running"

    def test_get_run_returns_none_for_missing(self):
        mgr, _ = self._make_manager()
        assert mgr.get_run("nonexistent") is None

    def test_create_run_creates_db_if_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "subdir" / "state.db"
            mgr = StateManager(db_path=db)
            mgr.create_run("run-123", "topic-456")
            assert db.exists()


class TestStateManagerProcessLeases:
    def test_abort_unknown_run(self, tmp_path):
        mgr = StateManager(tmp_path / "state.db")
        assert mgr.request_abort("missing") is None

    def test_abort_is_idempotent_for_terminal_run(self, tmp_path):
        mgr = StateManager(tmp_path / "state.db")
        mgr.create_run("run", "topic")
        mgr.update_run_status("run", "succeeded")
        request = mgr.request_abort("run")
        assert request is not None
        assert request.status == "succeeded"
        assert request.lease is None

    def test_abort_returns_exact_runs_active_lease(self, tmp_path):
        mgr = StateManager(tmp_path / "state.db")
        mgr.create_run("run-one", "topic")
        mgr.create_run("run-two", "topic")
        lease = ProcessLease("run-two", "attempt", 42, 42, "token")
        assert mgr.register_process(lease)

        request = mgr.request_abort("run-two")

        assert request is not None
        assert request.status == "aborting"
        assert request.lease == lease
        assert mgr.get_run("run-one")["status"] == "starting"
        assert mgr.get_run("run-two")["status"] == "aborting"

    def test_abort_wins_spawn_registration_race(self, tmp_path):
        mgr = StateManager(tmp_path / "state.db")
        mgr.create_run("run", "topic")
        assert mgr.request_abort("run").status == "aborting"
        assert not mgr.register_process(ProcessLease("run", "attempt", 42, 42, "token"))

    def test_old_attempt_cannot_clear_new_attempt(self, tmp_path):
        mgr = StateManager(tmp_path / "state.db")
        mgr.create_run("run", "topic")
        assert mgr.register_process(ProcessLease("run", "old", 42, 42, "old-token"))
        assert mgr.register_process(ProcessLease("run", "new", 43, 43, "new-token"))
        assert not mgr.clear_process("run", "old")
        assert mgr.request_abort("run").lease.attempt_id == "new"

    def test_legacy_statuses_migrate_to_canonical_values(self, tmp_path):
        db = tmp_path / "state.db"
        mgr = StateManager(db)
        mgr.create_run("run", "topic")
        mgr._get_conn().execute("UPDATE runs SET status = 'cancelled' WHERE run_id = 'run'")
        mgr._get_conn().commit()
        mgr._local.conn = None

        migrated = StateManager(db)
        assert migrated.get_run("run")["status"] == "aborted"

    def test_legacy_schema_without_hub_url_is_left_unchanged(self, tmp_path):
        db = tmp_path / "state.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE runs ("
            "run_id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, status TEXT NOT NULL, "
            "phase INTEGER NOT NULL DEFAULT 0, started_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO runs (run_id, topic_id, status, started_at) VALUES ('run', 'topic', 'starting', 0)"
        )
        conn.commit()
        before = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        conn.close()

        with StateManager(db) as mgr:
            assert mgr.get_run("run")["topic_id"] == "topic"
            mgr.create_run("new-run", "new-topic")
            assert mgr.get_run("new-run")["status"] == "starting"

        conn = sqlite3.connect(db)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        finally:
            conn.close()
        assert columns == before
        assert "hub_url" not in columns

    def test_old_schema_database_keeps_legacy_schema_and_supports_current_operations(
        self, tmp_path
    ):
        db = tmp_path / "state.db"
        conn = sqlite3.connect(db)
        # Frozen pre-retirement schema, not the production schema under test.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                topic_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'starting',
                phase INTEGER NOT NULL DEFAULT 0,
                hub_url TEXT,
                started_at REAL NOT NULL,
                finished_at REAL
            );

            CREATE TABLE IF NOT EXISTS phases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                phase INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                output_file TEXT,
                started_at REAL,
                completed_at REAL,
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS locks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                topic_id TEXT NOT NULL,
                pid INTEGER NOT NULL,
                acquired_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                UNIQUE(topic_id)
            );

            CREATE TABLE IF NOT EXISTS active_processes (
                run_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL,
                leader_pid INTEGER NOT NULL,
                pgid INTEGER NOT NULL,
                process_start_token TEXT NOT NULL,
                started_at REAL NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS locks_topic_id ON locks(topic_id);
            """)
        conn.execute(
            "INSERT INTO runs (run_id, topic_id, status, phase, hub_url, started_at) "
            "VALUES ('pre-existing', 'topic-old', 'running', 3, 'http://hub', 1.0)"
        )
        conn.execute(
            "INSERT INTO phases (run_id, phase, status) VALUES ('pre-existing', 3, 'running')"
        )
        conn.commit()
        conn.close()

        with StateManager(db) as mgr:
            assert mgr.get_run("pre-existing")["topic_id"] == "topic-old"
            mgr.update_run_status("pre-existing", "failed")
            assert mgr.get_run("pre-existing")["status"] == "failed"
            mgr.create_run("run-1", "topic-1")
            assert mgr.get_run("run-1")["status"] == "starting"
            mgr.update_run_status("run-1", "running")
            assert mgr.get_run("run-1")["status"] == "running"

            assert mgr.acquire_lock("run-1", "topic-1", 4242)
            assert not mgr.acquire_lock("run-2", "topic-1", 4343)
            assert mgr.get_locked_run_id("topic-1") == "run-1"
            mgr.release_lock("run-1", "topic-1")
            assert mgr.get_lock("topic-1") is None
            assert mgr.acquire_lock("run-2", "topic-1", 4343)
            mgr.release_lock("run-2", "topic-1")

            lease = ProcessLease("run-1", "attempt-1", 4242, 4242, "tok")
            assert mgr.register_process(lease)
            assert mgr.get_process_lease("run-1") == lease
            request = mgr.request_abort("run-1")
            assert request is not None and request.status == "aborting"
            assert request.lease == lease
            assert mgr.clear_process("run-1", "attempt-1")
            assert mgr.get_process_lease("run-1") is None
            mgr.update_run_status("run-1", "aborted")
            assert mgr.get_run("run-1")["status"] == "aborted"

        conn = sqlite3.connect(db)
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
            assert "phases" in tables
            assert {"phase", "hub_url"} <= columns
            assert conn.execute(
                "SELECT phase, hub_url FROM runs WHERE run_id = 'pre-existing'"
            ).fetchone() == (3, "http://hub")
            assert conn.execute("SELECT run_id, phase, status FROM phases").fetchall() == [
                ("pre-existing", 3, "running")
            ]
        finally:
            conn.close()

    def test_opening_a_current_db_does_not_wait_for_the_write_lock(self, tmp_path):
        db = tmp_path / "state.db"
        StateManager(db).close()
        writer = sqlite3.connect(db, isolation_level=None)
        writer.execute("BEGIN IMMEDIATE")
        read = []

        def open_and_read():
            with StateManager(db) as mgr:
                read.append(mgr.get_run("run"))

        thread = threading.Thread(target=open_and_read, daemon=True)
        try:
            thread.start()
            thread.join(timeout=5)
            assert read == [None]
        finally:
            writer.execute("COMMIT")
            writer.close()
            thread.join()

    def test_duplicate_topic_locks_without_the_index_are_migrated(self, tmp_path):
        db = tmp_path / "state.db"
        StateManager(db).close()
        conn = sqlite3.connect(db)
        conn.execute("DROP INDEX locks_topic_id")
        conn.execute("CREATE TABLE locks_old AS SELECT * FROM locks")
        conn.execute("DROP TABLE locks")
        conn.execute(
            "CREATE TABLE locks (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, "
            "topic_id TEXT NOT NULL, pid INTEGER NOT NULL, acquired_at REAL NOT NULL, "
            "heartbeat_at REAL NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO locks (run_id, topic_id, pid, acquired_at, heartbeat_at) "
            "VALUES (?, 'topic', 1, 0, 0)",
            [("first",), ("second",)],
        )
        conn.commit()
        conn.close()

        with StateManager(db) as mgr:
            assert mgr.get_locked_run_id("topic") == "first"
        conn = sqlite3.connect(db)
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(locks)")}
        conn.close()
        assert "locks_topic_id" in indexes


class TestStateManagerLocks:
    """StateManager handles locks atomically."""

    def _make_manager(self):
        tmpdir = tempfile.mkdtemp()
        db = Path(tmpdir) / "state.db"
        mgr = StateManager(db_path=db)
        return mgr, tmpdir

    def test_acquire_lock_succeeds_when_free(self):
        mgr, _ = self._make_manager()
        result = mgr.acquire_lock("run-123", "topic-456", pid=os.getpid())
        assert result is True

    def test_acquire_lock_fails_when_already_locked(self):
        mgr, _ = self._make_manager()
        mgr.acquire_lock("run-123", "topic-456", pid=os.getpid())
        result = mgr.acquire_lock("run-123", "topic-456", pid=os.getpid() + 1)
        assert result is False

    def test_different_run_id_cannot_lock_same_topic(self):
        mgr, _ = self._make_manager()
        assert mgr.acquire_lock("run-one", "topic-456", pid=os.getpid()) is True
        assert mgr.acquire_lock("run-two", "topic-456", pid=os.getpid()) is False

    def test_release_lock(self):
        mgr, _ = self._make_manager()
        mgr.acquire_lock("run-123", "topic-456", pid=os.getpid())
        mgr.release_lock("run-123", "topic-456")
        # Should be able to acquire again
        result = mgr.acquire_lock("run-123", "topic-456", pid=os.getpid() + 1)
        assert result is True

    def test_concurrent_lock_acquisition(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "state.db"
            mgr = StateManager(db_path=db)
            results = []
            lock = threading.Lock()

            def try_lock(thread_id):
                result = mgr.acquire_lock("run-123", "topic-456", pid=os.getpid())
                with lock:
                    results.append(result)

            threads = [threading.Thread(target=try_lock, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            # Exactly one wins; the losers answer False instead of timing out.
            assert sorted(results) == [False, False, False, True]

    def test_losing_lock_releases_the_write_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "state.db"
            mgr = StateManager(db_path=db)
            assert mgr.acquire_lock("run-123", "topic-456", pid=os.getpid()) is True
            assert mgr.acquire_lock("run-123", "topic-456", pid=os.getpid()) is False
            other = sqlite3.connect(str(db), timeout=0.2)
            try:
                other.execute("DELETE FROM locks WHERE run_id = 'nobody'")
                other.commit()
            finally:
                other.close()


class TestStateManagerDisasterPath:
    """StateManager handles disaster scenarios gracefully."""

    def test_corrupt_db_file_backs_up_and_recreates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "state.db"
            db.write_text("this is not a sqlite database", encoding="utf-8")
            mgr = StateManager(db_path=db)
            # Should work despite corrupt file
            mgr.create_run("run-123", "topic-456")
            run = mgr.get_run("run-123")
            assert run is not None
            # Corrupt backup should exist
            backups = list(Path(tmpdir).glob("*.db.corrupt.*"))
            assert len(backups) == 1
            # Backup should contain the original corrupt content
            assert backups[0].read_bytes() == b"this is not a sqlite database"
            # A new clean DB should exist at the original path
            assert db.exists()
            # Verify the new DB is valid
            mgr2 = StateManager(db_path=db)
            mgr2.create_run("run-456", "topic-789")
            assert mgr2.get_run("run-456") is not None

    def test_locked_db_at_open_raises_and_keeps_every_file(self, tmp_path, monkeypatch):
        """A busy DB is not corrupt: opening it must not back up, delete or recreate
        state.db (the lock leases of running runs live there)."""
        import helmraiser.storage.state as state_module

        db = tmp_path / "state.db"
        with StateManager(db) as mgr:
            mgr.create_run("run", "topic")
        conn = sqlite3.connect(db)
        conn.execute("UPDATE runs SET status = 'cancelled'")  # a legacy status: open migrates
        conn.commit()
        conn.close()
        monkeypatch.setattr(state_module, "BUSY_TIMEOUT_S", 0.2)
        writer = sqlite3.connect(db, isolation_level=None)
        writer.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                StateManager(db)
        finally:
            writer.execute("ROLLBACK")
            writer.close()

        assert list(tmp_path.glob("*.corrupt.*")) == []
        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT run_id, status FROM runs").fetchall()
        conn.close()
        assert rows == [("run", "cancelled")]

    def test_recovery_keeps_a_readable_db_and_its_backup(self, tmp_path):
        db = tmp_path / "state.db"
        mgr = StateManager(db)
        mgr.create_run("run", "topic")
        mgr.close()

        mgr._recover_corrupt_db()

        backups = list(tmp_path.glob("state.db.corrupt.*"))
        assert len(backups) == 1
        for path in (db, backups[0]):
            conn = sqlite3.connect(path)
            assert conn.execute("SELECT run_id FROM runs").fetchall() == [("run",)]
            conn.close()

    def test_corrupt_db_file_attempted_recovery(self):
        """When SQLite can partially read the corrupt file, it recovers rows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "state.db"
            # Create a valid DB with data, then corrupt it slightly
            mgr = StateManager(db_path=db)
            mgr.create_run("run-123", "topic-456")
            # Close the connection so we can corrupt the file
            mgr._local.conn.close()
            mgr._local.conn = None
            # Append garbage to simulate partial corruption
            with open(db, "ab") as f:
                f.write(b"\x00\x00\x00CORRUPTED_TAIL")
            # Re-open — should recover or recreate
            mgr2 = StateManager(db_path=db)
            mgr2.create_run("run-456", "topic-789")
            run = mgr2.get_run("run-456")
            assert run is not None


def test_state_manager_close_reopen_context_and_gc(tmp_path):
    """close() reopens on next use; context manager closes; GC emits no ResourceWarning."""
    mgr = StateManager(db_path=tmp_path / "close.db")
    mgr.create_run("run-1", "topic-1")
    assert mgr.get_run("run-1") is not None
    mgr.close()
    assert mgr.get_run("run-1") is not None  # a read after close reopens

    with StateManager(db_path=tmp_path / "ctx.db") as ctx:
        ctx.create_run("run-2", "topic-2")
    assert ctx._local.conn is None

    obj = StateManager(db_path=tmp_path / "gc.db")
    obj.create_run("run-3", "topic-3")
    obj.get_run("run-3")
    gc.collect()  # sweep garbage from unrelated tests before recording
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        del obj
        gc.collect()
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]
