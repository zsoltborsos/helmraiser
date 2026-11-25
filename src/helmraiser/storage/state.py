"""StateManager — SQLite-backed state management for Helmraiser runs.

Per-project state database at <project>/.helmraiser/state.db.
Holds per-project run rows, task locks, process leases, and abort requests.
"""

import shutil
import sqlite3
import threading
import time
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        topic_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'starting',
        started_at REAL NOT NULL,
        finished_at REAL
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
"""

BUSY_TIMEOUT_S = 30.0

TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "aborted"})


def _is_corruption(exc: sqlite3.DatabaseError) -> bool:
    """SQLITE_CORRUPT (any extended code) or SQLITE_NOTADB: the file itself is bad."""
    code = getattr(exc, "sqlite_errorcode", None)
    return code is not None and code & 0xFF in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)


@dataclass(frozen=True)
class ProcessLease:
    run_id: str
    attempt_id: str
    leader_pid: int
    pgid: int
    process_start_token: str


@dataclass(frozen=True)
class AbortRequest:
    status: str
    lease: ProcessLease | None = None


class StateManager:
    """Thread-safe SQLite state manager."""

    def __init__(self, db_path: Path | str | None = None):
        if db_path is None:
            db_path = Path.home() / ".helmraiser" / "state.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(
                str(self.db_path), timeout=BUSY_TIMEOUT_S, check_same_thread=False
            )
            weakref.finalize(self, conn.close)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return cast("sqlite3.Connection", self._local.conn)

    def close(self) -> None:
        """Close this thread's connection; the next call reopens it."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
        self._local.conn = None

    def __enter__(self) -> "StateManager":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _init_db(self):
        try:
            self._create_schema()
        except sqlite3.DatabaseError as exc:
            # Only a damaged file is recovered. "database is locked" and every other
            # error propagate and leave the file alone: it holds live leases and locks.
            if not _is_corruption(exc):
                raise
            self._recover_corrupt_db()
            self._create_schema()

    def _create_schema(self) -> None:
        """Create missing tables; a complete schema is only read, so opening the DB
        takes no write lock and never queues behind other writers."""
        conn = self._get_conn()
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master").fetchall()}
        if {"runs", "locks", "active_processes"} <= names:
            return
        with self._write_lock:
            # Not ``executescript``: it would commit the open transaction first.
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        conn.execute(statement)
            except BaseException:
                conn.rollback()
                raise
            conn.commit()

    def _recover_corrupt_db(self) -> Path | None:
        """Back up a corrupt DB file; remove it only if SQLite cannot dump it.

        The backup is always kept. Returns its path, or None if the file didn't exist.
        """
        if not self.db_path.exists():
            return None

        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        backup = self.db_path.with_suffix(f".db.corrupt.{ts}")
        shutil.copy2(self.db_path, backup)

        try:
            conn = sqlite3.connect(str(self.db_path))
            list(conn.iterdump())  # Check if DB is readable
            conn.close()

            # If iterdump succeeded, the DB wasn't fully corrupted — keep it
            return backup
        except sqlite3.DatabaseError:
            # Truly unreadable — leave the backup for forensic analysis
            pass

        self.db_path.unlink(missing_ok=True)
        self._local.conn = None
        return backup

    # ── Run lifecycle ──────────────────────────────────────────────────

    def create_run(self, run_id: str, topic_id: str) -> None:
        with self._write_lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO runs (run_id, topic_id, status, started_at) "
                "VALUES (?, ?, 'starting', ?)",
                (run_id, topic_id, time.time()),
            )
            conn.commit()

    def update_run_status(self, run_id: str, status: str) -> None:
        with self._write_lock:
            conn = self._get_conn()
            finished_at = time.time() if status in TERMINAL_RUN_STATUSES else None
            conn.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
                (status, finished_at, run_id),
            )
            conn.commit()

    def get_run(self, run_id: str) -> dict | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return dict(row)

    def request_abort(self, run_id: str) -> AbortRequest | None:
        """Atomically move an active run to aborting and return its lease."""
        with self._write_lock:
            conn = self._get_conn()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                conn.rollback()
                return None
            status = str(row["status"])
            if status in TERMINAL_RUN_STATUSES:
                conn.commit()
                return AbortRequest(status)
            conn.execute("UPDATE runs SET status = 'aborting' WHERE run_id = ?", (run_id,))
            row = conn.execute(
                "SELECT * FROM active_processes WHERE run_id = ?", (run_id,)
            ).fetchone()
            conn.commit()
            lease = (
                ProcessLease(
                    run_id=row["run_id"],
                    attempt_id=row["attempt_id"],
                    leader_pid=row["leader_pid"],
                    pgid=row["pgid"],
                    process_start_token=row["process_start_token"],
                )
                if row
                else None
            )
            return AbortRequest("aborting", lease)

    def register_process(self, lease: ProcessLease) -> bool:
        """Publish an attempt lease unless abort already won the race."""
        with self._write_lock:
            conn = self._get_conn()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", (lease.run_id,)
            ).fetchone()
            if row is None or row["status"] in {"aborting", "aborted"}:
                conn.rollback()
                return False
            conn.execute(
                "INSERT OR REPLACE INTO active_processes "
                "(run_id, attempt_id, leader_pid, pgid, process_start_token, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    lease.run_id,
                    lease.attempt_id,
                    lease.leader_pid,
                    lease.pgid,
                    lease.process_start_token,
                    time.time(),
                ),
            )
            conn.execute("UPDATE runs SET status = 'running' WHERE run_id = ?", (lease.run_id,))
            conn.commit()
            return True

    def clear_process(self, run_id: str, attempt_id: str) -> bool:
        """Clear only the attempt that owns the current lease."""
        with self._write_lock:
            conn = self._get_conn()
            cursor = conn.execute(
                "DELETE FROM active_processes WHERE run_id = ? AND attempt_id = ?",
                (run_id, attempt_id),
            )
            conn.commit()
            return cursor.rowcount == 1

    # ── Locks ──────────────────────────────────────────────────────────

    def acquire_lock(self, run_id: str, topic_id: str, pid: int) -> bool:
        with self._write_lock:
            conn = self._get_conn()
            now = time.time()
            try:
                conn.execute(
                    "INSERT INTO locks (run_id, topic_id, pid, acquired_at, heartbeat_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (run_id, topic_id, pid, now, now),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                # The failed INSERT opened a transaction; holding it would keep the
                # write lock and stall every other writer for the busy timeout.
                conn.rollback()
                return False

    def release_lock(self, run_id: str, topic_id: str) -> None:
        with self._write_lock:
            conn = self._get_conn()
            conn.execute(
                "DELETE FROM locks WHERE run_id = ? AND topic_id = ?",
                (run_id, topic_id),
            )
            conn.commit()

    def get_locked_run_id(self, topic_id: str) -> str | None:
        """Return the run ID holding the topic lock, if any."""
        lock = self.get_lock(topic_id)
        return str(lock["run_id"]) if lock else None

    def get_lock(self, topic_id: str) -> dict | None:
        """Return the task lock row, including its worker PID, if present."""
        row = (
            self._get_conn()
            .execute("SELECT * FROM locks WHERE topic_id = ?", (topic_id,))
            .fetchone()
        )
        return dict(row) if row else None

    def get_run_lock(self, run_id: str) -> dict | None:
        """Return the lock row that ``run_id`` holds, if any. A pair's two runs each
        hold a lock of their own topic, so a run's lock is found by run, not by task."""
        row = self._get_conn().execute("SELECT * FROM locks WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def get_process_lease(self, run_id: str) -> ProcessLease | None:
        """Return the persisted child-process identity for ``run_id``."""
        row = (
            self._get_conn()
            .execute("SELECT * FROM active_processes WHERE run_id = ?", (run_id,))
            .fetchone()
        )
        if row is None:
            return None
        return ProcessLease(
            run_id=str(row["run_id"]),
            attempt_id=str(row["attempt_id"]),
            leader_pid=int(row["leader_pid"]),
            pgid=int(row["pgid"]),
            process_start_token=str(row["process_start_token"]),
        )
