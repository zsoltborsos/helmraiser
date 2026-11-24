"""Ledger — the central SQLite database for tasks, runs and the queue.

Lives at ``$HELMRAISER_DATA_DIR/helmraiser.db`` (default ``~/.helmraiser/helmraiser.db``).
Same threading / WAL / busy_timeout pattern as ``storage/state.py``; every write
runs under ``_write_lock`` inside ``BEGIN IMMEDIATE``.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
import weakref
from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from helmraiser.config import load_config
from helmraiser.task.model import PRIORITY_RANK, TaskSpec

# Bump on schema changes: a new ledger is created at this ``user_version`` and a ledger
# at any other version is refused.
SCHEMA_VERSION = 11

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  id TEXT NOT NULL, project TEXT NOT NULL, title TEXT NOT NULL,
  priority INTEGER NOT NULL,
  status TEXT NOT NULL,
  policy TEXT, isolation TEXT NOT NULL DEFAULT 'none',
  source_path TEXT NOT NULL, snapshot TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, queued_at REAL
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  project TEXT NOT NULL, worktree_path TEXT, branch_name TEXT,
  base_branch TEXT, base_commit TEXT, head_commit TEXT,
  status TEXT NOT NULL,
  stage TEXT,
  policy TEXT NOT NULL, isolation TEXT NOT NULL,
  delivery TEXT NOT NULL DEFAULT 'merge', pr_url TEXT,
  model_implement TEXT NOT NULL, model_fallback TEXT, model_review TEXT NOT NULL,
  model_plan TEXT, plan_cost_usd REAL NOT NULL DEFAULT 0,
  escalated INTEGER NOT NULL DEFAULT 0,
  pi_version TEXT NOT NULL,
  cost_usd REAL NOT NULL DEFAULT 0, tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  verdict TEXT,
  failure_reason TEXT,
  started_at REAL NOT NULL, ended_at REAL
);
CREATE TABLE IF NOT EXISTS run_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  ts REAL NOT NULL, source TEXT NOT NULL,
  stage TEXT, session_id TEXT,
  type TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_events_run ON run_events(run_id, id);
CREATE TABLE IF NOT EXISTS iterations (
  run_id TEXT NOT NULL REFERENCES runs(run_id), n INTEGER NOT NULL,
  session_id TEXT NOT NULL, model TEXT NOT NULL,
  started_at REAL NOT NULL, ended_at REAL,
  agent_ok INTEGER, commit_sha TEXT,
  verify_passed INTEGER, verify_results TEXT,
  cost_usd REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, n)
);
CREATE TABLE IF NOT EXISTS evidence (
  run_id TEXT NOT NULL REFERENCES runs(run_id), name TEXT NOT NULL,
  ok INTEGER NOT NULL, exit_code INTEGER, artifacts TEXT NOT NULL,
  tail TEXT, PRIMARY KEY (run_id, name)
);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  kind TEXT NOT NULL,
  decision TEXT NOT NULL,
  note TEXT, decided_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_state (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scout_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL,
  lens TEXT NOT NULL DEFAULT 'state',
  requested_at REAL NOT NULL,
  started_at REAL,
  scout_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS scout_requests_pending_project ON scout_requests(project, lens) WHERE started_at IS NULL;
CREATE TABLE IF NOT EXISTS scouts (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  model TEXT NOT NULL,
  lens TEXT NOT NULL DEFAULT 'state',
  head_commit TEXT,
  request_id INTEGER REFERENCES scout_requests(id),
  started_at REAL NOT NULL,
  ended_at REAL,
  cost_usd REAL NOT NULL DEFAULT 0,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  outcome TEXT NOT NULL,
  drafts_written TEXT NOT NULL DEFAULT '[]',
  report_path TEXT NOT NULL,
  error TEXT
);
CREATE TABLE IF NOT EXISTS scout_drafts (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
  scout_id TEXT NOT NULL REFERENCES scouts(id),
  candidate TEXT NOT NULL,
  intake_status TEXT NOT NULL,
  intake_error TEXT,
  intake_notes TEXT NOT NULL DEFAULT '[]',
  acceptance_scope_sha256 TEXT,
  proposal TEXT,
  preflight TEXT
);
"""
TERMINAL_RUN_STATUSES = frozenset({"landed", "failed", "aborted"})
PARKED_RUN_STATUSES = frozenset({"awaiting_approval"})
# A run handed back to the worker (plan approved, or ``helmraiser retry``). It has no
# process; its ``stage`` is where the worker resumes it: ``start``, ``implement`` or
# ``verify``.
RESUMING_RUN_STATUS = "resuming"
# Runs that are not terminal but have no process: parked for the owner, or resuming.
IDLE_RUN_STATUSES = PARKED_RUN_STATUSES | {RESUMING_RUN_STATUS}
RUN_STATUSES = frozenset(
    {
        "starting",
        "running",
        "verifying",
        "reviewing",
        "awaiting_approval",
        RESUMING_RUN_STATUS,
        "landed",
        "failed",
        "aborting",
        "aborted",
    }
)
_SCOUT_COLUMNS = frozenset(
    {
        "lens",
        "id",
        "project",
        "model",
        "head_commit",
        "request_id",
        "started_at",
        "ended_at",
        "cost_usd",
        "tokens_in",
        "tokens_out",
        "outcome",
        "drafts_written",
        "report_path",
        "error",
    }
)
_SCOUT_OUTCOMES = frozenset({"running", "succeeded", "failed"})
_SCOUT_DRAFT_INTAKE_STATUSES = frozenset({"pending", "proposed", "intake_failed"})
_RUN_COLUMNS = frozenset(
    {
        "run_id",
        "task_id",
        "project",
        "worktree_path",
        "branch_name",
        "base_branch",
        "base_commit",
        "head_commit",
        "status",
        "stage",
        "policy",
        "isolation",
        "delivery",
        "pr_url",
        "model_implement",
        "model_fallback",
        "model_review",
        "model_plan",
        "plan_cost_usd",
        "escalated",
        "pi_version",
        "cost_usd",
        "tokens_in",
        "tokens_out",
        "verdict",
        "failure_reason",
        "started_at",
        "ended_at",
    }
)


def project_key(project: str | Path) -> str:
    """A project path as the ledger stores it: the task file's ``project`` with ``~``
    expanded (not resolved)."""
    return str(Path(project).expanduser())


@dataclass(frozen=True)
class ProjectScope:
    """The projects a ``work`` loop serves: ``include`` (``None``: every project)
    minus ``exclude``. Paths match as the ledger stores them (``project_key``)."""

    include: frozenset[str] | None = None
    exclude: frozenset[str] = frozenset()

    @classmethod
    def of(
        cls, include: Iterable[str | Path] | None = None, exclude: Iterable[str | Path] = ()
    ) -> ProjectScope:
        return cls(
            None if include is None else frozenset(project_key(p) for p in include),
            frozenset(project_key(p) for p in exclude),
        )

    def matches(self, project: str) -> bool:
        if self.include is not None and project not in self.include:
            return False
        return project not in self.exclude

    def without(self, projects: Iterable[str]) -> ProjectScope:
        return ProjectScope(self.include, self.exclude | frozenset(projects))

    def sql(self, column: str) -> tuple[str, list[Any]]:
        """An ``AND …`` filter on ``column`` and its parameters."""
        if self.include is not None and not self.include:
            return " AND 0", []
        sql = ""
        params: list[Any] = []
        if self.include is not None:
            sql += f" AND {column} IN ({', '.join('?' for _ in self.include)})"
            params.extend(sorted(self.include))
        if self.exclude:
            sql += f" AND {column} NOT IN ({', '.join('?' for _ in self.exclude)})"
            params.extend(sorted(self.exclude))
        return sql, params


def default_data_dir() -> Path:
    """``$HELMRAISER_DATA_DIR`` (default ``~/.helmraiser``)."""
    return Path(os.environ.get("HELMRAISER_DATA_DIR", "~/.helmraiser")).expanduser()


def default_db_path(data_dir: Path | None = None) -> Path:
    return (data_dir or default_data_dir()) / "helmraiser.db"


class TaskActive(ValueError):
    """A nonterminal run owns the task's scheduling declarations."""


class RunUnavailable(ValueError):
    """Admission changed after selection; no run has been claimed."""

    def __init__(self, hold: dict[str, Any]):
        super().__init__(str(hold))
        self.hold = hold


class ScoutRequestUnavailable(ValueError):
    """The request cannot be claimed: it is gone, consumed, or waiting on safety gates."""

    def __init__(self, message: str, *, wait_reason: str | None = None) -> None:
        super().__init__(message)
        self.wait_reason = wait_reason


class Ledger:
    """Thread-safe SQLite ledger."""

    def __init__(self, db_path: Path | str | None = None, *, read_only: bool = False):
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.read_only = read_only
        if not read_only:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        try:
            self._init_db()
        except Exception:
            self.close()
            raise

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            target = (
                self.db_path.resolve().as_uri() + "?mode=ro"
                if self.read_only
                else str(self.db_path)
            )
            conn = sqlite3.connect(target, uri=self.read_only, timeout=30, check_same_thread=False)
            weakref.finalize(self, conn.close)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            if self.read_only:
                conn.execute("PRAGMA query_only=ON")
            else:
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

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _init_db(self) -> None:
        """Create the schema in a new ledger; refuse a ledger at another version.

        A ledger at ``SCHEMA_VERSION`` is only read, so opening it takes no write lock
        and never queues behind other writers. Creation runs under ``BEGIN IMMEDIATE``
        and checks the file again under that lock: another process may have created
        the schema meanwhile.
        """
        if self._schema_is_current(self._get_conn()):
            return
        if self.read_only:
            raise ValueError("ledger has no schema; cannot create it read-only")
        with self._write() as conn:
            if self._schema_is_current(conn):
                return
            # Not ``executescript``: it would commit the open transaction first.
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _schema_is_current(self, conn: sqlite3.Connection) -> bool:
        """True at ``SCHEMA_VERSION``, False for an empty file; any other version raises.

        One statement reads both, so another process creating the schema in between
        cannot make a new file look like a ledger at version 0."""
        version, has_schema = conn.execute(
            "SELECT user_version, EXISTS (SELECT 1 FROM sqlite_master) FROM pragma_user_version"
        ).fetchone()
        if version == SCHEMA_VERSION:
            return True
        if version == 0 and not has_schema:
            return False
        raise ValueError(
            f"ledger {self.db_path} has schema version {version}; "
            f"this Helmraiser expects version {SCHEMA_VERSION}"
        )

    @contextmanager
    def read_snapshot(self) -> Iterator[None]:
        """Read related rows consistently, releasing the snapshot before the next poll."""
        conn = self._get_conn()
        conn.execute("BEGIN")
        try:
            yield
        finally:
            conn.rollback()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            raise ValueError("ledger is read-only")
        with self._write_lock:
            conn = self._get_conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            conn.commit()

    # ── Tasks ─────────────────────────────────────────────────────────

    def _snapshot_is_active(self, conn: sqlite3.Connection, task_id: str, snapshot: str) -> bool:
        """Freeze declarations inside the same transaction used to change them."""
        terminal = sorted(TERMINAL_RUN_STATUSES)
        row = conn.execute(
            "SELECT t.snapshot, r.run_id, r.status FROM tasks t "
            "JOIN runs r ON r.task_id = t.task_id WHERE t.task_id = ? "
            f"AND r.status NOT IN ({', '.join('?' for _ in terminal)}) "
            "ORDER BY r.started_at LIMIT 1",
            [task_id, *terminal],
        ).fetchone()
        if row is None:
            return False
        if row["snapshot"] != snapshot:
            raise TaskActive(
                f"task {task_id} has nonterminal run {row['run_id']} ({row['status']}); "
                "its snapshot cannot change until the run settles"
            )
        return True

    def upsert_task(self, spec: TaskSpec, snapshot: str) -> str:
        """Insert or refresh a task; active runs freeze its snapshot and task state."""
        now = time.time()
        task_id = spec.task_id
        with self._write() as conn:
            if self._snapshot_is_active(conn, task_id, snapshot):
                return task_id  # An identical re-intake must not requeue an active task.
            row = conn.execute(
                "SELECT queued_at FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            queued_at = row["queued_at"] if row else None
            if spec.status == "queued" and queued_at is None:
                queued_at = now
            elif spec.status != "queued":
                queued_at = None
            conn.execute(
                "INSERT INTO tasks (task_id, id, project, title, priority, status, policy, "
                "isolation, source_path, snapshot, created_at, updated_at, queued_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET "
                "title = excluded.title, priority = excluded.priority, "
                "status = excluded.status, policy = excluded.policy, "
                "isolation = excluded.isolation, source_path = excluded.source_path, "
                "snapshot = excluded.snapshot, updated_at = excluded.updated_at, "
                "queued_at = excluded.queued_at",
                (
                    task_id,
                    spec.id,
                    str(spec.project),
                    spec.title,
                    PRIORITY_RANK.get(spec.priority, PRIORITY_RANK["normal"]),
                    spec.status,
                    spec.policy,
                    spec.isolation,
                    str(spec.source_path),
                    snapshot,
                    now,
                    now,
                    queued_at,
                ),
            )
        return task_id

    def set_task_status(self, task_id: str, status: str) -> bool:
        now = time.time()
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ?, "
                "queued_at = CASE WHEN ? = 'queued' THEN COALESCE(queued_at, ?) ELSE queued_at END "
                "WHERE task_id = ?",
                (status, now, status, now, task_id),
            )
            return cursor.rowcount == 1

    def dequeue_task(self, task_id: str) -> bool:
        """``queued`` → ``draft`` in one statement; False if the task was not queued."""
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET status = 'draft', updated_at = ?, queued_at = NULL "
                "WHERE task_id = ? AND status = 'queued'",
                (time.time(), task_id),
            )
            return cursor.rowcount == 1

    def set_task_snapshot(
        self, task_id: str, snapshot: str, *, parked_run: str | None = None
    ) -> bool:
        """Replace retry text only while no nonterminal run owns its declarations.

        ``parked_run``: rework's run, which may own them while it awaits approval.
        """
        with self._write() as conn:
            terminal = sorted(TERMINAL_RUN_STATUSES)
            owners = conn.execute(
                "SELECT run_id, status FROM runs WHERE task_id = ? "
                f"AND status NOT IN ({', '.join('?' for _ in terminal)})",
                [task_id, *terminal],
            ).fetchall()
            parked = [(r["run_id"], r["status"]) for r in owners] == [
                (parked_run, "awaiting_approval")
            ]
            if not parked and self._snapshot_is_active(conn, task_id, snapshot):
                return True
            cursor = conn.execute(
                "UPDATE tasks SET snapshot = ?, updated_at = ? WHERE task_id = ?",
                (snapshot, time.time(), task_id),
            )
            return cursor.rowcount == 1

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = (
            self._get_conn().execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        )
        return dict(row) if row else None

    def list_tasks(
        self, status: str | None = None, *, project: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        sql = "SELECT * FROM tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if status is None:
            sql += " ORDER BY status, priority, queued_at, created_at"
        else:
            sql += " ORDER BY priority, queued_at, created_at"
        rows = self._get_conn().execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def next_queued(
        self,
        project: str | None = None,
        exclude: Collection[str] = (),
        *,
        scope: ProjectScope | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """The queued task ``work`` would pick next.

        Priority asc, then queued_at asc; explicitly excluded task IDs, tasks outside
        ``scope`` and held tasks are skipped. Default-limit projects are exclusive;
        higher limits admit only safe disjoint siblings (see ``held_tasks``).
        """
        conn = self._get_conn()
        params: list[Any] = []
        sql = "SELECT t.* FROM tasks t WHERE t.status = 'queued'"
        if project is not None:
            sql += " AND t.project = ?"
            params.append(project)
        if scope is not None:
            scope_sql, scope_params = scope.sql("t.project")
            sql += scope_sql
            params.extend(scope_params)
        excluded = tuple(exclude)
        if excluded:
            sql += f" AND t.task_id NOT IN ({', '.join('?' for _ in excluded)})"
            params.extend(excluded)
        sql += " ORDER BY t.priority ASC, t.queued_at ASC, t.created_at ASC"
        held = self.held_tasks(project=project, scope=scope, config=config)
        return next(
            (
                dict(row)
                for row in conn.execute(sql, params).fetchall()
                if row["task_id"] not in held
            ),
            None,
        )

    def held_tasks(
        self,
        project: str | None = None,
        *,
        scope: ProjectScope | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Queued tasks held by a nonterminal project run or unmet dependencies.

        Run holds retain ``{run_id, status, pending}`` of the oldest holder;
        dependency holds add ``{reason: depends_on, unmet}``. Holds are dynamic,
        never a stored task status.
        """
        from helmraiser.ledger.dependencies import dependency_holds
        from helmraiser.ledger.scheduling import project_hold

        config = load_config() if config is None else config
        held: dict[str, dict[str, Any]] = {}

        candidates: dict[str, list[dict[str, Any]]] = {}
        for task in self.list_tasks(status="queued", project=project):
            if scope is None or scope.matches(str(task["project"])):
                candidates.setdefault(str(task["project"]), []).append(task)
        for candidate_project, rows in candidates.items():
            tasks = self.list_tasks(project=candidate_project)
            runs = self.list_runs(project=candidate_project)
            for run in runs:
                approval = self.pending_approval(run["run_id"])
                run["pending"] = approval["kind"] if approval else None
            for row in rows:
                hold = project_hold(row, tasks, runs, config)
                if hold:
                    held[row["task_id"]] = hold
            dependencies = dependency_holds(tasks, runs, candidates=rows)
            for task_id, hold in dependencies.items():
                held.setdefault(task_id, {}).update(hold)
        return held

    def resume_holds(self, *, config: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        """Project-admission holds on resuming runs, keyed by run ID."""
        from helmraiser.ledger.scheduling import project_hold

        config = load_config() if config is None else config
        held: dict[str, dict[str, Any]] = {}
        candidates: dict[str, list[dict[str, Any]]] = {}
        for run in self.list_runs(status=RESUMING_RUN_STATUS):
            candidates.setdefault(str(run["project"]), []).append(run)
        for candidate_project, rows in candidates.items():
            tasks = self.list_tasks(project=candidate_project)
            runs = self.list_runs(project=candidate_project)
            for run in runs:
                approval = self.pending_approval(run["run_id"])
                run["pending"] = approval["kind"] if approval else None
            for row in rows:
                task = self.get_task(row["task_id"])
                if task is None:
                    continue
                hold = project_hold(task, tasks, runs, config, resume_id=row["run_id"])
                if hold is not None:
                    held[row["run_id"]] = hold
        return held

    # ── Scouts ────────────────────────────────────────────────────────

    @staticmethod
    def _unique_projects(projects: Iterable[str | Path]) -> list[str]:
        return list(dict.fromkeys(project_key(project) for project in projects))

    def request_scouts(
        self,
        projects: Iterable[str | Path],
        requested_at: float | None = None,
        *,
        lens: str = "state",
    ) -> tuple[list[str], list[str]]:
        """Create at most one pending request per project, preserving input order."""
        requested: list[str] = []
        already_pending: list[str] = []
        timestamp = time.time() if requested_at is None else requested_at
        with self._write() as conn:
            for project in self._unique_projects(projects):
                pending = conn.execute(
                    "SELECT 1 FROM scout_requests "
                    "WHERE project = ? AND lens = ? AND started_at IS NULL",
                    (project, lens),
                ).fetchone()
                if pending is not None:
                    already_pending.append(project)
                    continue
                conn.execute(
                    "INSERT INTO scout_requests (project, requested_at, lens) VALUES (?, ?, ?)",
                    (project, timestamp, lens),
                )
                requested.append(project)
        return requested, already_pending

    def cancel_scout_requests(
        self, projects: Iterable[str | Path], *, lens: str | None = None
    ) -> tuple[list[str], list[str]]:
        """Delete pending requests, preserving input order in both result lists."""
        cancelled: list[str] = []
        not_pending: list[str] = []
        with self._write() as conn:
            for project in self._unique_projects(projects):
                cursor = conn.execute(
                    "DELETE FROM scout_requests WHERE project = ? AND started_at IS NULL"
                    + (" AND lens = ?" if lens is not None else ""),
                    (project, lens) if lens is not None else (project,),
                )
                (cancelled if cursor.rowcount else not_pending).append(project)
        return cancelled, not_pending

    def list_scout_requests(self, *, pending_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM scout_requests"
        if pending_only:
            sql += " WHERE started_at IS NULL"
        sql += " ORDER BY requested_at ASC, id ASC"
        return [dict(row) for row in self._get_conn().execute(sql).fetchall()]

    def get_scout_request(self, request_id: int) -> dict[str, Any] | None:
        row = (
            self._get_conn()
            .execute("SELECT * FROM scout_requests WHERE id = ?", (request_id,))
            .fetchone()
        )
        return dict(row) if row else None

    def create_scout(self, scout: dict[str, Any], *, force: bool = False) -> str:
        """Persist a scout and consume its request atomically, if it has one.

        Do this in the child, not before spawning: worker death must never leave
        a consumed request without a scout. Competing children cannot both start.
        Recheck pause and project work under the write lock before claiming, unless
        explicitly forced. Unrequested scouts retain their caller's gate policy.
        """
        record = {"lens": "state", "outcome": "running", "started_at": time.time(), **scout}
        unknown = set(record) - _SCOUT_COLUMNS
        if unknown:
            raise ValueError(f"unknown scout columns: {', '.join(sorted(unknown))}")
        if record["outcome"] not in _SCOUT_OUTCOMES:
            raise ValueError(f"invalid scout outcome: {record['outcome']}")
        if isinstance(record.get("drafts_written"), list):
            record["drafts_written"] = json.dumps(record["drafts_written"])
        columns = list(record)
        with self._write() as conn:
            if record.get("request_id") is not None:
                project = project_key(record["project"])
                if not force:
                    # is_paused reads this connection, inside BEGIN IMMEDIATE.
                    if self.is_paused() is not None:
                        raise ScoutRequestUnavailable("queue is paused", wait_reason="queue paused")
                    queued = conn.execute(
                        "SELECT 1 FROM tasks WHERE project = ? AND status = 'queued' LIMIT 1",
                        (project,),
                    ).fetchone()
                    if queued is not None:
                        raise ScoutRequestUnavailable(
                            "project has queued work", wait_reason="project busy"
                        )
                    terminal = sorted(TERMINAL_RUN_STATUSES)
                    active = conn.execute(
                        "SELECT status FROM runs WHERE project = ? "
                        f"AND status NOT IN ({', '.join('?' for _ in terminal)}) LIMIT 1",
                        (project, *terminal),
                    ).fetchone()
                    if active is not None:
                        raise ScoutRequestUnavailable(
                            f"project has a non-terminal run: {active['status']}",
                            wait_reason="project busy",
                        )
                cursor = conn.execute(
                    "UPDATE scout_requests SET started_at = ?, scout_id = ? "
                    "WHERE id = ? AND project = ? AND lens = ? AND started_at IS NULL",
                    (
                        record["started_at"],
                        record["id"],
                        record["request_id"],
                        project,
                        record["lens"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise ScoutRequestUnavailable("scout request is no longer pending")
            conn.execute(
                f"INSERT INTO scouts ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [record[column] for column in columns],
            )
        return str(record["id"])

    def finish_scout(
        self,
        scout_id: str,
        *,
        outcome: str,
        cost_usd: float,
        tokens_in: int,
        tokens_out: int,
        drafts_written: list[str],
        ended_at: float | None = None,
        error: str | None = None,
    ) -> bool:
        if outcome not in _SCOUT_OUTCOMES - {"running"}:
            raise ValueError(f"invalid scout outcome: {outcome}")
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE scouts SET outcome = ?, cost_usd = ?, tokens_in = ?, tokens_out = ?, "
                "drafts_written = ?, ended_at = ?, error = ? WHERE id = ?",
                (
                    outcome,
                    cost_usd,
                    tokens_in,
                    tokens_out,
                    json.dumps(drafts_written),
                    time.time() if ended_at is None else ended_at,
                    error,
                    scout_id,
                ),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _decode_scout(row: sqlite3.Row) -> dict[str, Any]:
        decoded = dict(row)
        decoded["drafts_written"] = json.loads(decoded["drafts_written"])
        return decoded

    def get_scout(self, scout_id: str) -> dict[str, Any] | None:
        row = self._get_conn().execute("SELECT * FROM scouts WHERE id = ?", (scout_id,)).fetchone()
        return self._decode_scout(row) if row else None

    def list_scouts(self, project: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM scouts"
        params: list[Any] = []
        if project is not None:
            sql += " WHERE project = ?"
            params.append(project)
        sql += " ORDER BY started_at DESC"
        return [self._decode_scout(row) for row in self._get_conn().execute(sql, params).fetchall()]

    def stats_iterations(self, project: str | None = None) -> list[dict[str, Any]]:
        return self._stats_rows(
            "SELECT i.run_id, COUNT(*) AS count, SUM(i.cost_usd) AS cost "
            "FROM iterations i JOIN runs r ON r.run_id = i.run_id WHERE 1=1",
            project,
            " GROUP BY i.run_id",
        )

    def _stats_rows(self, sql: str, project: str | None, suffix: str = "") -> list[dict[str, Any]]:
        params = () if project is None else (project,)
        if project is not None:
            sql += " AND r.project = ?"
        return [dict(row) for row in self._get_conn().execute(sql + suffix, params)]

    def stats_history(self, project: str | None = None) -> dict[str, list[dict[str, Any]]]:
        approvals = self._stats_rows(
            "SELECT a.* FROM approvals a JOIN runs r ON r.run_id=a.run_id WHERE 1=1", project
        )
        events = self._stats_rows(
            "SELECT e.* FROM run_events e JOIN runs r ON r.run_id=e.run_id "
            "WHERE e.type IN ('run_end', 'scope_requested')",
            project,
        )
        for event in events:
            event["payload"] = json.loads(event["payload"])
        return {"approvals": approvals, "events": events}

    def stats_drafts(self, project: str | None = None) -> list[dict[str, Any]]:
        return self._stats_rows(
            "SELECT d.*, r.snapshot, r.status, r.queued_at FROM scout_drafts d "
            "JOIN tasks r ON r.task_id=d.task_id WHERE 1=1",
            project,
        )

    def set_scout_draft_hash(self, task_id: str, value: str) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE scout_drafts SET acceptance_scope_sha256=? WHERE task_id=?",
                (value, task_id),
            )

    def create_scout_draft(
        self,
        task_id: str,
        scout_id: str,
        candidate: dict[str, Any],
        *,
        acceptance_scope_sha256: str | None = None,
    ) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO scout_drafts "
                "(task_id, scout_id, candidate, intake_status, acceptance_scope_sha256) VALUES (?, ?, ?, 'pending', ?)",
                (task_id, scout_id, json.dumps(candidate), acceptance_scope_sha256),
            )

    def set_scout_draft_intake(
        self,
        task_id: str,
        *,
        status: str,
        proposal: dict[str, Any] | None,
        error: str | None,
        notes: list[str] | None = None,
    ) -> bool:
        if status not in _SCOUT_DRAFT_INTAKE_STATUSES:
            raise ValueError(f"invalid scout draft intake status: {status}")
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE scout_drafts SET intake_status = ?, proposal = ?, intake_error = ?, "
                "intake_notes = ? WHERE task_id = ?",
                (
                    status,
                    json.dumps(proposal) if proposal is not None else None,
                    error,
                    json.dumps(notes or []),
                    task_id,
                ),
            )
            return cursor.rowcount == 1

    def set_scout_draft_preflight(self, task_id: str, preflight: dict[str, Any]) -> bool:
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE scout_drafts SET preflight = ? WHERE task_id = ?",
                (json.dumps(preflight), task_id),
            )
            return cursor.rowcount == 1

    def get_scout_draft(self, task_id: str) -> dict[str, Any] | None:
        row = (
            self._get_conn()
            .execute("SELECT * FROM scout_drafts WHERE task_id = ?", (task_id,))
            .fetchone()
        )
        if row is None:
            return None
        decoded = dict(row)
        for field in ("candidate", "proposal", "preflight", "intake_notes"):
            if decoded[field] is not None:
                decoded[field] = json.loads(decoded[field])
        return decoded

    # ── Runs ──────────────────────────────────────────────────────────

    def create_run(self, run: dict[str, Any]) -> str:
        """Insert a run row from a dict of column values (see ``runs`` DDL)."""
        record = {"status": "starting", "started_at": time.time(), **run}
        unknown = set(record) - _RUN_COLUMNS
        if unknown:
            raise ValueError(f"unknown run columns: {', '.join(sorted(unknown))}")
        columns = list(record)
        with self._write() as conn:
            conn.execute(
                f"INSERT INTO runs ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [record[c] for c in columns],
            )
        return str(record["run_id"])

    def claim_queued_run(
        self, run: dict[str, Any], *, snapshot: str, config: dict[str, Any]
    ) -> str:
        """Recheck admission and publish ownership in one serialized transaction."""
        record = {"status": "starting", "started_at": time.time(), **run}
        unknown = set(record) - _RUN_COLUMNS
        if unknown:
            raise ValueError(f"unknown run columns: {', '.join(sorted(unknown))}")
        with self._write() as conn:
            task = self.get_task(record["task_id"])
            if not task or task["status"] != "queued" or task["snapshot"] != snapshot:
                raise RunUnavailable({"reason": "task_changed"})
            if task["project"] != record["project"]:
                raise RunUnavailable({"reason": "project_changed"})
            if self.is_paused():
                raise RunUnavailable({"reason": "queue_paused"})
            hold = self.held_tasks(project=task["project"], config=config).get(task["task_id"])
            if hold:
                raise RunUnavailable(hold)
            columns = list(record)
            conn.execute(
                f"INSERT INTO runs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [record[c] for c in columns],
            )
            conn.execute(
                "UPDATE tasks SET status = 'running', updated_at = ? WHERE task_id = ?",
                (time.time(), task["task_id"]),
            )
        return str(record["run_id"])

    def update_run(self, run_id: str, **fields: Any) -> bool:
        if not fields:
            return False
        unknown = set(fields) - _RUN_COLUMNS
        if unknown:
            raise ValueError(f"unknown run columns: {', '.join(sorted(unknown))}")
        assignments = ", ".join(f"{col} = ?" for col in fields)
        with self._write() as conn:
            cursor = conn.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?",
                [*fields.values(), run_id],
            )
            return cursor.rowcount == 1

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._get_conn().execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(
        self,
        since: float | None = None,
        status: str | None = None,
        project: str | None = None,
        *,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(since)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        sql = "SELECT * FROM runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY started_at DESC"
        rows = self._get_conn().execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def hand_back(
        self,
        run_id: str,
        stage: str,
        *,
        rework: dict[str, Any] | None = None,
        reopen_pr: dict[str, Any] | None = None,
    ) -> bool:
        """Mark a run (and its task) ``resuming`` at ``stage`` for the worker to pick up."""
        if reopen_pr is not None and (rework is None or stage != "implement"):
            return False
        now = time.time()
        with self._write() as conn:
            if rework is not None:
                run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
                approval = conn.execute(
                    "SELECT id, kind FROM approvals WHERE run_id = ? AND decision = 'pending' "
                    "ORDER BY id DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                if reopen_pr is not None:
                    if (
                        run is None
                        or run["status"] != "landed"
                        or run["delivery"] != "pr"
                        or not run["pr_url"]
                        or run["pr_url"] != reopen_pr["pr_url"]
                        or approval is not None
                        or rework["approval_id"] is not None
                    ):
                        return False
                    terminal = sorted(TERMINAL_RUN_STATUSES)
                    if conn.execute(
                        "SELECT 1 FROM runs WHERE task_id = ? AND run_id != ? "
                        f"AND status NOT IN ({', '.join('?' for _ in terminal)}) LIMIT 1",
                        (run["task_id"], run_id, *terminal),
                    ).fetchone():
                        return False
                elif (
                    stage != "implement"
                    or run is None
                    or run["status"] != "awaiting_approval"
                    or approval is None
                    or approval["kind"] != "merge"
                    or approval["id"] != rework["approval_id"]
                ):
                    return False
                rounds = conn.execute(
                    "SELECT COUNT(*) FROM run_events WHERE run_id = ? AND type = 'rework_requested'",
                    (run_id,),
                ).fetchone()[0]
                iterations = conn.execute(
                    "SELECT COALESCE(MAX(n), 0) FROM iterations WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
                payload = {**rework, "round": rounds + 1, "iterations_before": iterations}
                if reopen_pr is not None:
                    conn.execute(
                        "UPDATE runs SET head_commit = NULL, worktree_path = ?, base_commit = ? "
                        "WHERE run_id = ?",
                        (reopen_pr["worktree_path"], reopen_pr["base_commit"], run_id),
                    )
                    conn.execute(
                        "INSERT INTO run_events (run_id, ts, source, stage, type, payload) "
                        "VALUES (?, ?, 'helmraiser', 'implement', 'rework_reopened', ?)",
                        (run_id, now, json.dumps(reopen_pr)),
                    )
                else:
                    conn.execute(
                        "UPDATE approvals SET decision = 'rework', note = ?, decided_at = ? WHERE id = ?",
                        (rework["note"], now, approval["id"]),
                    )
                conn.execute(
                    "INSERT INTO run_events (run_id, ts, source, stage, type, payload) "
                    "VALUES (?, ?, 'helmraiser', 'implement', 'rework_requested', ?)",
                    (run_id, now, json.dumps(payload)),
                )
            cursor = conn.execute(
                "UPDATE runs SET status = ?, stage = ? WHERE run_id = ?",
                (RESUMING_RUN_STATUS, stage, run_id),
            )
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? "
                "WHERE task_id = (SELECT task_id FROM runs WHERE run_id = ?)",
                (RESUMING_RUN_STATUS, now, run_id),
            )
            return cursor.rowcount == 1

    def next_resumable(
        self,
        project: str | None = None,
        *,
        scope: ProjectScope | None = None,
        config: dict[str, Any] | None = None,
        exclude: Collection[str] = (),
    ) -> dict[str, Any] | None:
        """The oldest ``resuming`` run ``work`` would resume next.

        At limit 1 a run waits while another is neither terminal nor resuming.
        Higher limits allow safe siblings using the candidate's existing slot."""
        from helmraiser.ledger.scheduling import project_hold

        config = load_config() if config is None else config
        params: list[Any] = [RESUMING_RUN_STATUS]
        sql = "SELECT r.* FROM runs r WHERE r.status = ?"
        if project is not None:
            sql += " AND r.project = ?"
            params.append(project)
        if scope is not None:
            scope_sql, scope_params = scope.sql("r.project")
            sql += scope_sql
            params.extend(scope_params)
        sql += " ORDER BY r.started_at ASC"
        for row in self._get_conn().execute(sql, params).fetchall():
            if row["run_id"] in exclude:
                continue
            task = self.get_task(row["task_id"])
            if (
                task
                and project_hold(
                    task,
                    self.list_tasks(project=row["project"]),
                    self.list_runs(project=row["project"]),
                    config,
                    resume_id=row["run_id"],
                )
                is None
            ):
                return dict(row)
        return None

    def claim_resumable(
        self, run_id: str, *, checked: bool = False, config: dict[str, Any] | None = None
    ) -> bool:
        """``resuming`` → ``running`` in one statement; False if another worker (or a
        ``reject``) got there first."""
        with self._write() as conn:
            if checked:
                run = self.get_run(run_id)
                if not run or self.is_paused():
                    return False
                # Exclude other candidates, not other holders, to check this run only.
                others = {
                    r["run_id"]
                    for r in self.list_runs(project=run["project"])
                    if r["run_id"] != run_id
                }
                if (
                    self.next_resumable(project=run["project"], config=config, exclude=others)
                    is None
                ):
                    return False
            cursor = conn.execute(
                "UPDATE runs SET status = 'running' WHERE run_id = ? AND status = ?",
                (run_id, RESUMING_RUN_STATUS),
            )
            return cursor.rowcount == 1

    def finish_run(
        self,
        run_id: str,
        status: str,
        failure_reason: str | None = None,
        *,
        ended_at: float | None = None,
    ) -> bool:
        """``ended_at`` defaults to now; pass the run's earlier end to keep it."""
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE runs SET status = ?, failure_reason = ?, ended_at = ? WHERE run_id = ?",
                (status, failure_reason, time.time() if ended_at is None else ended_at, run_id),
            )
            return cursor.rowcount == 1

    # ── Events ────────────────────────────────────────────────────────

    def append_event(
        self,
        run_id: str,
        source: str,
        type: str,
        payload: dict[str, Any],
        stage: str | None = None,
        session_id: str | None = None,
    ) -> int:
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO run_events (run_id, ts, source, stage, session_id, type, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    time.time(),
                    source,
                    stage,
                    session_id,
                    type,
                    json.dumps(payload, default=str),
                ),
            )
            return int(cursor.lastrowid or 0)

    def reworks(self, run_id: str) -> list[dict[str, Any]]:
        """Ordered approver rework requests (not automatic review feedback)."""
        return [e["payload"] for e in self.events(run_id, types=("rework_requested",))]

    def events(
        self, run_id: str, since_id: int = 0, types: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        """A run's events in order; ``types`` filters in SQL, so others are never decoded."""
        sql = "SELECT * FROM run_events WHERE run_id = ? AND id > ?"
        if types:
            sql += f" AND type IN ({', '.join('?' * len(types))})"
        rows = self._get_conn().execute(f"{sql} ORDER BY id", (run_id, since_id, *types)).fetchall()
        result = []
        for row in rows:
            event = dict(row)
            event["payload"] = json.loads(event["payload"])
            result.append(event)
        return result

    # ── Iterations / evidence / approvals ─────────────────────────────

    def start_iteration(self, run_id: str, n: int, session_id: str, model: str) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO iterations (run_id, n, session_id, model, started_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, n, session_id, model, time.time()),
            )

    def finish_iteration(
        self,
        run_id: str,
        n: int,
        *,
        agent_ok: bool,
        commit_sha: str | None,
        verify_passed: bool | None,
        verify_results: list[dict[str, Any]],
        cost_usd: float = 0.0,
    ) -> bool:
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE iterations SET ended_at = ?, agent_ok = ?, commit_sha = ?, "
                "verify_passed = ?, verify_results = ?, cost_usd = ? WHERE run_id = ? AND n = ?",
                (
                    time.time(),
                    int(agent_ok),
                    commit_sha,
                    None if verify_passed is None else int(verify_passed),
                    json.dumps(verify_results, default=str),
                    cost_usd,
                    run_id,
                    n,
                ),
            )
            return cursor.rowcount == 1

    def record_verify(
        self,
        run_id: str,
        n: int,
        *,
        commit_sha: str | None,
        verify_passed: bool,
        verify_results: list[dict[str, Any]],
    ) -> bool:
        """Replace iteration ``n``'s verify outcome (a retried run re-verifies its last)."""
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE iterations SET commit_sha = ?, verify_passed = ?, verify_results = ? "
                "WHERE run_id = ? AND n = ?",
                (
                    commit_sha,
                    int(verify_passed),
                    json.dumps(verify_results, default=str),
                    run_id,
                    n,
                ),
            )
            return cursor.rowcount == 1

    def iterations(self, run_id: str) -> list[dict[str, Any]]:
        rows = (
            self._get_conn()
            .execute("SELECT * FROM iterations WHERE run_id = ? ORDER BY n", (run_id,))
            .fetchall()
        )
        result = []
        for row in rows:
            item = dict(row)
            if item["verify_results"] is not None:
                item["verify_results"] = json.loads(item["verify_results"])
            result.append(item)
        return result

    def spend_rows(self, since: float) -> list[dict[str, Any]]:
        """Runs and all their iterations relevant to spend since ``since``.

        A CTE first identifies relevant runs so attribution needs one query while
        still seeing old iterations when calculating each run's remainder.
        """
        rows = (
            self._get_conn()
            .execute(
                "WITH relevant AS ("
                " SELECT DISTINCT r.run_id FROM runs r LEFT JOIN iterations i ON i.run_id = r.run_id"
                " WHERE COALESCE(r.ended_at, r.started_at) >= ?"
                " OR COALESCE(i.ended_at, i.started_at) >= ?"
                ") SELECT r.run_id, r.model_review, r.model_plan, r.plan_cost_usd,"
                " r.cost_usd AS run_cost_usd,"
                " r.started_at AS run_started_at, r.ended_at AS run_ended_at,"
                " i.n, i.model, i.cost_usd AS iteration_cost_usd,"
                " i.started_at AS iteration_started_at, i.ended_at AS iteration_ended_at"
                " FROM relevant x JOIN runs r ON r.run_id = x.run_id"
                " LEFT JOIN iterations i ON i.run_id = r.run_id"
                " ORDER BY r.run_id, i.n",
                (since, since),
            )
            .fetchall()
        )
        return [dict(row) for row in rows]

    def record_evidence(
        self,
        run_id: str,
        name: str,
        *,
        ok: bool,
        exit_code: int | None,
        artifacts: list[str],
        tail: str | None = None,
    ) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO evidence (run_id, name, ok, exit_code, artifacts, tail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, name, int(ok), exit_code, json.dumps(artifacts), tail),
            )

    def evidence(self, run_id: str) -> list[dict[str, Any]]:
        rows = (
            self._get_conn()
            .execute("SELECT * FROM evidence WHERE run_id = ? ORDER BY rowid", (run_id,))
            .fetchall()
        )
        result = []
        for row in rows:
            item = dict(row)
            item["artifacts"] = json.loads(item["artifacts"])
            result.append(item)
        return result

    def record_approval(
        self, run_id: str, kind: str, decision: str, note: str | None = None
    ) -> int:
        """Record ``approved``, ``declined``, ``rejected``, ``rework``, or a ``pending`` plan/merge
        approval that waits for the owner (resolved later by ``decide_approval``)."""
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO approvals (run_id, kind, decision, note, decided_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, kind, decision, note, time.time()),
            )
            return int(cursor.lastrowid or 0)

    def decide_approval(self, approval_id: int, decision: str, note: str | None = None) -> bool:
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE approvals SET decision = ?, note = ?, decided_at = ? WHERE id = ?",
                (decision, note, time.time(), approval_id),
            )
            return cursor.rowcount == 1

    def pending_approval(self, run_id: str) -> dict[str, Any] | None:
        row = (
            self._get_conn()
            .execute(
                "SELECT * FROM approvals WHERE run_id = ? AND decision = 'pending' "
                "ORDER BY id DESC LIMIT 1",
                (run_id,),
            )
            .fetchone()
        )
        return dict(row) if row else None

    def approvals(self, run_id: str) -> list[dict[str, Any]]:
        rows = (
            self._get_conn()
            .execute("SELECT * FROM approvals WHERE run_id = ? ORDER BY id", (run_id,))
            .fetchall()
        )
        return [dict(r) for r in rows]

    # ── Queue state ───────────────────────────────────────────────────

    def _get_state(self, conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM queue_state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    @staticmethod
    def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT OR REPLACE INTO queue_state (key, value) VALUES (?, ?)", (key, value))

    def pause_queue(self, reason: str, until: float | None = None) -> None:
        """Pause dispatch until ``until`` (epoch seconds; None = until resumed)."""
        with self._write() as conn:
            self._set_state(conn, "paused_until", "" if until is None else repr(float(until)))
            self._set_state(conn, "pause_reason", reason)

    def resume_queue(self) -> None:
        with self._write() as conn:
            conn.execute(
                "DELETE FROM queue_state "
                "WHERE key IN ('paused_until', 'pause_reason') OR key LIKE 'provider_hold:%'"
            )

    def hold_provider(self, provider: str, until: float, detail: str | None) -> None:
        value = json.dumps({"until": float(until), "detail": detail})
        with self._write() as conn:
            self._set_state(conn, f"provider_hold:{provider}", value)

    def provider_holds(self, now: float) -> dict[str, dict[str, Any]]:
        """Active provider holds, dropping expired entries as they are read."""
        with self._write() as conn:
            rows = conn.execute(
                "SELECT key, value FROM queue_state WHERE key LIKE 'provider_hold:%' ORDER BY key"
            ).fetchall()
            holds: dict[str, dict[str, Any]] = {}
            expired: list[str] = []
            for row in rows:
                value = json.loads(str(row["value"]))
                if float(value["until"]) <= now:
                    expired.append(str(row["key"]))
                    continue
                holds[str(row["key"]).split(":", 1)[1]] = {
                    "until": float(value["until"]),
                    "detail": value.get("detail"),
                }
            if expired:
                conn.executemany(
                    "DELETE FROM queue_state WHERE key = ?", [(key,) for key in expired]
                )
            return holds

    def clear_provider_hold(self, provider: str) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM queue_state WHERE key = ?", (f"provider_hold:{provider}",))

    def is_paused(self) -> dict[str, Any] | None:
        """``{"reason", "until"}`` while paused, else None."""
        conn = self._get_conn()
        raw_until = self._get_state(conn, "paused_until")
        if raw_until is None:
            return None
        until = float(raw_until) if raw_until else None
        if until is not None and until <= time.time():
            return None
        return {"reason": self._get_state(conn, "pause_reason") or "", "until": until}

    def scout_lens_override(self, lens: str) -> dict[str, Any]:
        raw = self._get_state(self._get_conn(), f"scout_lens:{lens}")
        return json.loads(raw) if raw else {}

    def set_scout_lens_override(
        self, lens: str, *, auto: bool | None = None, cooldown_hours: float | None = None
    ) -> None:
        from helmraiser.loop.scout_lenses import lens_text

        lens_text(lens)
        if auto is None and cooldown_hours is None:
            raise ValueError("provide auto or cooldown_hours")
        if auto is not None and not isinstance(auto, bool):
            raise ValueError("auto must be boolean")
        if cooldown_hours is not None and (
            isinstance(cooldown_hours, bool)
            or not isinstance(cooldown_hours, (int, float))
            or not math.isfinite(cooldown_hours)
            or cooldown_hours <= 0
        ):
            raise ValueError("cooldown_hours must be finite and positive")
        with self._write() as conn:
            key = f"scout_lens:{lens}"
            raw = self._get_state(conn, key)
            value = json.loads(raw) if raw else {}
            if auto is not None:
                value["auto"] = auto
            if cooldown_hours is not None:
                value["cooldown_hours"] = cooldown_hours
            self._set_state(conn, key, json.dumps(value))

    def reset_scout_lens_override(self, lens: str) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM queue_state WHERE key = ?", (f"scout_lens:{lens}",))

    def caps_switch(self) -> str | None:
        value = self._get_state(self._get_conn(), "caps_enforced")
        return value if value in ("on", "off") else None

    def set_caps_switch(self, value: str | None) -> None:
        if value not in (None, "on", "off"):
            raise ValueError("caps switch must be 'on', 'off', or None")
        with self._write() as conn:
            if value is None:
                conn.execute("DELETE FROM queue_state WHERE key = 'caps_enforced'")
            else:
                self._set_state(conn, "caps_enforced", value)
