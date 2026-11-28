"""The long-running ``work`` loop (``--watch``, ``--max-parallel``, ``worker start``).

A supervisor runs each eligible project's next run in its own child process,
``python -m helmraiser work --once --json --supervised --project <p>``, with up to
``max_parallel`` children at once. The children take their run from the ledger as
``work --once`` always does (resumable runs first, the task lock, the atomic
``claim_resumable``); the supervisor only picks which projects to start. In watch mode,
after runnable work is exhausted, eligible focus projects may use spare slots for
``python -m helmraiser scout`` children. Project siblings require opt-in disjoint paths;
scouts never overlap a project child. It starts resumable and queued runs before
scouts, and starts nothing while the queue is
paused or for a task that ``work`` would skip (invalid, provider out of quota, over the
spend caps).

With ``watch`` it never exits on its own: when nothing is eligible it re-checks every
``poll_s`` seconds, and at once when a child finishes. A stop request
(``worker.stop``) lets the running children finish, then it exits. Without ``watch``
it exits as soon as nothing is running and nothing is eligible.

An error in one project's work (its reconcile, loading and checking its next task with
the project's ``project.toml``, starting its child, or a child that fails without a
run) does not stop the supervisor: it is reported once per distinct error (an event
line and a desktop notice), listed in ``worker.active.json`` until that step works
again, and keeps the project out for ``PROJECT_ERROR_BACKOFF_S`` while the others are
served. Errors outside one project (the ledger) still end it.

Given ``code_version`` and ``reexec``, it restarts itself on new code: every ``poll_s``
it compares ``code_version()`` (the checkout's HEAD) with the version it runs
(``running_code``, the commit of its code snapshot, else HEAD when it started), and
once that changes it records its children and immediately calls ``reexec`` with the
new version, replacing the process (same pid and argv) on the new snapshot. The new
image adopts the running children, which keep their slots and their original code
snapshots (see ``helmraiser.code_snapshot``).

When it ends it adds ``exit`` (``{at, reason}``: ``stopped``, ``max_runs`` or
``drained``) to the background worker's record, when it is that worker.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import typer

from helmraiser.cli.base_sync import HeldBaseSync, base_sync_text
from helmraiser.cli.worker_commands import read_record, update_record, write_active
from helmraiser.code_snapshot import PACKAGE_PATH, source_repo
from helmraiser.config import load_config
from helmraiser.ledger.db import (
    IDLE_RUN_STATUSES,
    RESUMING_RUN_STATUS,
    TERMINAL_RUN_STATUSES,
    Ledger,
    ProjectScope,
    ScoutRequestUnavailable,
    is_busy,
)
from helmraiser.ledger.rundir import RunDir
from helmraiser.loop.cleanup import clean_worktrees
from helmraiser.loop.reconcile import find_phantom_runs, reconcile
from helmraiser.loop.scout import ScoutError, configured_focus_projects, scout_gate
from helmraiser.loop.status import write_status
from helmraiser.runner.process_control import PosixProcessController, ProcessController
from helmraiser.storage.state import StateManager
from helmraiser.task.project import effective_run_limit, worktree_manager

# A child that ends without a run (an error, or another worker got there first)
# keeps its project out of the next checks for this long.
NO_RUN_BACKOFF_S = 300.0
# A project whose work raised an error is left out for this long.
PROJECT_ERROR_BACKOFF_S = 300.0
WORKTREE_SWEEP_INTERVAL_S = 3600.0
# ``work``'s exit code for a paused queue: a child without a run for that reason
# did not fail.
EXIT_PAUSED = 3

OUTCOME_KEYS = frozenset({"run_id", "status", "iterations", "cost_usd"})
SCOUT_OUTCOME_KEYS = frozenset({"scout_id", "outcome", "written_task_ids", "report_path"})
RESTART_CHILDREN_ENV = "HELMRAISER_RESTART_CHILDREN"


class ChildProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...


def _read_child_stat(pid: int, proc_root: Path = Path("/proc")) -> tuple[str, str] | None:
    """Process state and Linux start-time identity (stat field 22)."""
    try:
        text = (proc_root / str(pid) / "stat").read_text()
        fields = text[text.rindex(")") + 1 :].split()
        if len(fields[0]) != 1 or not fields[19].isdigit():
            return None
        return fields[0], fields[19]
    except (OSError, ValueError, IndexError):
        return None


class AdoptedProcess:
    """Poll an exec-preserved child without reconstructing Popen.

    If its original exit status is unavailable, completion uses exit code 1.
    Never wait on a PID without the identity captured at spawn time.
    """

    def __init__(self, pid: int, start_token: str | None) -> None:
        self.pid = pid
        self.start_token = start_token
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        identity = _read_child_stat(self.pid) if self.pid > 1 else None
        if not self.start_token or identity is None or identity[1] != self.start_token:
            self.returncode = 1
        else:
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
                if pid:
                    self.returncode = os.waitstatus_to_exitcode(status)
            except ChildProcessError:
                identity = _read_child_stat(self.pid)
                if (
                    identity is None
                    or identity[1] != self.start_token
                    or identity[0] in {"Z", "X", "x"}
                ):
                    self.returncode = 1
        return self.returncode


# (project, file for the child's stdout) → the started child
Spawn = Callable[[str, Path], ChildProcess]
# (project, stdout file, assigned scout id, optional request id) → child
ScoutSpawn = Callable[[str, Path, str, int | None, str], ChildProcess]
# a queued task row → (JSON fields, human text) when ``work`` would skip it
Skip = Callable[[dict[str, Any]], tuple[dict[str, Any], str] | None]
# (the new code, announce called just before the exec) → why the process could not be
# replaced; it returns only then
Reexec = Callable[["CodeVersion", Callable[[], None]], str]


@dataclass(frozen=True)
class CodeVersion:
    """Committed Helmraiser code: a commit of the checkout and the git tree of its
    ``src/helmraiser``."""

    commit: str
    tree: str

    @property
    def short(self) -> str:
        return self.commit[:8]


def code_version(repo: Path | None = None, ref: str = "HEAD") -> CodeVersion | None:
    """The ``CodeVersion`` of ``ref`` in the Helmraiser checkout ``repo`` (default
    ``source_repo()``: ``HELMRAISER_SOURCE_REPO`` on a snapshot, else the checkout the
    package is loaded from); ``None`` without a checkout, or when git fails.

    Only committed state counts: the tree changes only when a commit changes the
    package's files, so a docs-only commit or an uncommitted edit in the checkout
    does not restart the worker."""
    where = repo if repo is not None else source_repo()
    if where is None:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(where), "rev-parse", ref, f"{ref}:{PACKAGE_PATH}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = result.stdout.split()
    if result.returncode != 0 or len(lines) != 2:
        return None
    return CodeVersion(commit=lines[0], tree=lines[1])


@dataclass
class Child:
    project: str
    process: ChildProcess
    output: Path
    started_at: float
    run_id: str | None = None
    kind: str = "run"
    scout_id: str | None = None
    request_id: int | None = None
    start_token: str | None = None
    code_commit: str | None = None
    lens: str = "state"

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "pid": self.process.pid,
            "project": self.project,
            "run_id": self.run_id,
            "kind": self.kind,
            "started_at": self.started_at,
        }
        if self.kind == "scout":
            payload["scout_id"] = self.scout_id
            payload["lens"] = self.lens
        return payload

    def restart_dict(self) -> dict[str, Any]:
        """Private handoff; keep public child/event payloads unchanged."""
        return {
            **self.as_dict(),
            "scout_id": self.scout_id,
            "request_id": self.request_id,
            "output": str(self.output.resolve()),
            "start_token": self.start_token,
            "code_commit": self.code_commit,
        }


def read_restart_children(data_dir: Path) -> list[Child]:
    """Inspect this process's handoff without consuming it (also used by pruning)."""
    from helmraiser.loop.scout_lenses import LENSES

    record = read_record(data_dir)
    entries: Any = None
    if record is not None and record.get("pid") == os.getpid():
        entries = record.get("restart_children")
    if entries is None:
        try:
            payload = json.loads(os.environ.get(RESTART_CHILDREN_ENV, "null"))
            if isinstance(payload, dict) and payload.get("pid") == os.getpid():
                entries = payload.get("children")
        except ValueError:
            pass
    if not isinstance(entries, list):
        return []
    children: list[Child] = []
    pids: set[int] = set()
    run_ids: set[str] = set()
    output_dir = (data_dir / "worker-children").resolve()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        pid, project = entry.get("pid"), entry.get("project")
        started_at, output = entry.get("started_at"), entry.get("output")
        kind = entry.get("kind")
        if (
            type(pid) is not int
            or pid <= 1
            or pid == os.getpid()
            or pid in pids
            or not isinstance(project, str)
            or not project
            or (
                entry.get("run_id") is not None
                and (not isinstance(entry["run_id"], str) or entry["run_id"] in run_ids)
            )
            or any(
                c.project == project and (c.kind == "scout" or kind == "scout") for c in children
            )
            or kind not in ("run", "scout")
            or not isinstance(entry.get("lens", "state"), str)
            or entry.get("lens", "state") not in LENSES
            or not isinstance(started_at, (int, float))
            or isinstance(started_at, bool)
            or not math.isfinite(started_at)
            or not isinstance(output, str)
            or not Path(output).is_absolute()
            or not Path(output).resolve().is_relative_to(output_dir)
            or Path(output).resolve() == output_dir
            or any(
                entry.get(key) is not None and not isinstance(entry[key], str)
                for key in ("run_id", "scout_id", "start_token", "code_commit")
            )
            or (entry.get("request_id") is not None and type(entry["request_id"]) is not int)
            or (kind == "scout" and not entry.get("scout_id"))
        ):
            continue
        children.append(
            Child(
                project=project,
                process=AdoptedProcess(pid, entry.get("start_token")),
                output=Path(output),
                started_at=float(started_at),
                kind=kind,
                run_id=entry.get("run_id"),
                scout_id=entry.get("scout_id"),
                request_id=entry.get("request_id"),
                start_token=entry.get("start_token"),
                code_commit=entry.get("code_commit"),
                lens=entry.get("lens", "state"),
            )
        )
        pids.add(pid)
        if entry.get("run_id") is not None:
            run_ids.add(entry["run_id"])
    return children


def restore_children(data_dir: Path) -> list[Child]:
    """Consume the handoff before startup reconciliation or publication."""
    children = read_restart_children(data_dir)
    update_record(os.getpid(), {"restart_children": None}, data_dir)
    os.environ.pop(RESTART_CHILDREN_ENV, None)
    return children


@dataclass
class SupervisorResult:
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    skipped: set[str] = field(default_factory=set)
    paused: dict[str, Any] | None = None
    stopped: bool = False


def is_outcome(data: Any) -> bool:
    return isinstance(data, dict) and OUTCOME_KEYS <= set(data)


def is_scout_outcome(data: Any) -> bool:
    return isinstance(data, dict) and SCOUT_OUTCOME_KEYS <= set(data)


def outcome_text(outcome: dict[str, Any]) -> list[str]:
    lines = [
        f"{outcome['status']} {outcome.get('task_id')} run {outcome['run_id']} — "
        f"{outcome.get('iterations', 0)} iteration(s), ${float(outcome['cost_usd']):.4f}, "
        f"{float(outcome.get('minutes') or 0.0):.1f} min"
    ]
    if outcome.get("failure_reason"):
        lines.append(f"  reason: {outcome['failure_reason']}")
    if outcome.get("pr_url"):
        lines.append(f"  pr: {outcome['pr_url']}")
    if outcome.get("pending_approval"):
        lines.append(f"  next: helmraiser approve {outcome['run_id']} (or reject)")
    return lines


class Supervisor:
    def __init__(
        self,
        *,
        ledger: Ledger,
        data_dir: Path,
        scope: ProjectScope,
        max_parallel: int,
        watch: bool,
        max_runs: int | None,
        poll_s: float,
        base_sync_interval_s: float = 300.0,
        stop_file: Path,
        spawn: Spawn,
        skip: Skip,
        notify: Callable[[dict[str, Any]], None],
        as_json: bool,
        alert: Callable[[str], None] = lambda text: None,
        code_version: Callable[[], CodeVersion | None] | None = None,
        reexec: Reexec | None = None,
        running_code: CodeVersion | None = None,
        controller: ProcessController | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        run_config: dict[str, Any] | None = None,
        scout_config: dict[str, Any] | None = None,
        spawn_scout: ScoutSpawn | None = None,
        children: list[Child] | None = None,
        begin_pass: Callable[[], None] | None = None,
    ) -> None:
        self.ledger = ledger
        self.data_dir = data_dir
        self.scope = scope
        self.max_parallel = max_parallel
        self.watch = watch
        self.max_runs = max_runs
        self.poll_s = poll_s
        self.tick = min(1.0, poll_s)
        self.stop_file = stop_file
        self.spawn = spawn
        self.skip = skip
        self.begin_pass = begin_pass
        self.notify = notify
        self.alert = alert
        self.code_version = code_version
        self.reexec = reexec
        self.running_code = running_code
        self.as_json = as_json
        self.controller = controller or PosixProcessController()
        self.sleep = sleep
        self.clock = clock
        self.base_sync = HeldBaseSync(base_sync_interval_s, clock=clock)
        self.wall_clock = wall_clock
        self.run_config = load_config() if run_config is None else run_config
        self.registered: set[int] = set()
        self.scout_config = scout_config
        self.spawn_scout = spawn_scout
        self.children = list(children or [])
        self.started = sum(child.kind == "run" for child in self.children)
        self.stopping = False
        self.backoff: dict[str, float] = {}
        self.reported_skips: dict[str, str] = {}
        # project → its current error: {project, stage, error, since, retry_at}
        self.errors: dict[str, dict[str, Any]] = {}
        self.error_times: dict[str, float] = {}
        self.alerted: set[tuple[str, str]] = set()
        self.paused: dict[str, Any] | None = None
        self.last_worktree_sweep: float | None = None
        self.code: CodeVersion | None = None  # the code this process started on
        self.restart_to: CodeVersion | None = None  # new code detected by the latest poll
        self.result = SupervisorResult()

    # ── loop ──────────────────────────────────────────────────────────

    def run(self) -> SupervisorResult:
        self._publish()
        if self.code_version is not None and self.reexec is not None:
            self.code = self.running_code or self.code_version()
        next_check = self.clock()
        next_code_check = self.clock() + self.poll_s
        try:
            while True:
                # A ledger held past the busy timeout (an fsync stall, a long writer)
                # fails this tick only; the next one tries again, and its reconcile
                # closes a run whose child died on the same lock.
                try:
                    if self._reap() or self._identify_children():
                        next_check = self.clock()  # a slot opened: look again at once
                    if not self.stopping and self.stop_file.exists():
                        self.stop_file.unlink(missing_ok=True)
                        self.stopping = True
                        self.result.stopped = True
                        self._say(
                            {"stopping": True, "running": len(self.children)},
                            f"stop requested; waiting for {len(self.children)} running run(s)",
                        )
                    if not self.stopping and self.code is not None and self.restart_to is None:
                        if self.clock() >= next_code_check:
                            next_code_check = self.clock() + self.poll_s
                            self._check_code()
                    if not self.stopping and self.restart_to is not None:
                        self._restart()  # returns only when the process was not replaced
                        next_check = self.clock()
                    checked, spawned = False, 0
                    if (
                        not self.stopping
                        and self.restart_to is None
                        and not self._limit_reached()
                        and self.clock() >= next_check
                    ):
                        spawned = self._fill()
                        checked = True
                        next_check = self.clock() + self.poll_s
                    if not self.children:
                        if self.stopping or self._limit_reached():
                            break
                        if not self.watch and checked and not spawned:
                            break
                except sqlite3.OperationalError as exc:
                    if not is_busy(exc):
                        raise
                    self._ledger_busy("tick", exc)
                self.sleep(self.tick)
        finally:
            self._publish()
        reason = (
            "stopped" if self.result.stopped else "max_runs" if self._limit_reached() else "drained"
        )
        # Only the recorded background worker has a record to update.
        update_record(
            os.getpid(),
            {"exit": {"at": datetime.now(UTC).isoformat(), "reason": reason}},
            self.data_dir,
        )
        self.result.paused = self.ledger.is_paused()
        return self.result

    def _limit_reached(self) -> bool:
        return self.max_runs is not None and self.started >= self.max_runs

    def _may_start(self) -> bool:
        return (
            not self.stopping
            and self.restart_to is None
            and not self._limit_reached()
            and len(self.children) < self.max_parallel
        )

    # ── restarting on new code ────────────────────────────────────────

    def _check_code(self) -> None:
        current = self.code_version() if self.code_version is not None else None
        if current is None or self.code is None or current.tree == self.code.tree:
            return
        self.restart_to = current
        running = len(self.children)
        self._say(
            {"code_changed": {"from": self.code.commit, "to": current.commit, "running": running}},
            f"helmraiser code changed ({self.code.short} → {current.short}); "
            f"restarting now; keeping {running} running child(ren)",
        )

    def _restart(self) -> None:
        old, new = self.code, self.restart_to
        assert old is not None and new is not None and self.reexec is not None
        text = f"worker restarted on new code ({old.short} → {new.short})"

        def announce() -> None:
            restarted = {"at": datetime.now(UTC).isoformat(), "from": old.commit, "to": new.commit}
            update_record(os.getpid(), {"restarted": restarted}, self.data_dir)
            self._say({"restarting": {"from": old.commit, "to": new.commit}}, text)
            self.alert(text)

        handoff = []
        for child in self.children:
            entry = child.restart_dict()
            if child.kind == "run" and child.run_id is None:
                entry["run_id"] = self._child_run_id(child)
            handoff.append(entry)
        update_record(os.getpid(), {"restart_children": handoff}, self.data_dir)
        os.environ[RESTART_CHILDREN_ENV] = json.dumps({"pid": os.getpid(), "children": handoff})
        problem = self.reexec(new, announce)
        update_record(os.getpid(), {"restart_children": None}, self.data_dir)
        os.environ.pop(RESTART_CHILDREN_ENV, None)
        self._say(
            {"restart_failed": {"to": new.commit, "error": problem}},
            f"not restarted on new code ({new.short}): {problem}; staying on {old.short}",
        )
        self.alert(f"worker not restarted on new code ({new.short}): {problem}")
        # Serve on; try again only when the code changes again.
        self.code, self.restart_to = new, None

    def _child_run_id(self, child: Child) -> str | None:
        """Best-effort ID lookup, only with a matching run and child task lock."""
        path = Path(child.project) / ".helmraiser" / "state.db"
        if not path.is_file():
            return None
        try:
            state = StateManager(path)
            for run in self.ledger.list_runs(project=child.project):
                lock = state.get_run_lock(str(run["run_id"]))
                if lock and lock["pid"] == child.process.pid:
                    return str(run["run_id"])
        except Exception:
            pass  # Optional metadata must not prevent an otherwise valid restart.
        return None

    def _identify_children(self) -> bool:
        changed = False
        for child in self.children:
            if child.kind != "run" or child.process.pid in self.registered:
                continue
            run_id = self._child_run_id(child)
            if run_id is not None and (child.run_id is None or child.run_id == run_id):
                child.run_id = run_id
                self.registered.add(child.process.pid)
                changed = True
        if changed:
            self._publish()
        return changed

    def _run_scope(self) -> ProjectScope:
        self._identify_children()
        blocked = self._backing_off(set(self.backoff))
        for project in {child.project for child in self.children}:
            children = [child for child in self.children if child.project == project]
            if any(c.kind == "scout" or c.process.pid not in self.registered for c in children):
                blocked.add(project)
                continue
            try:
                pairs = [
                    self.ledger.pair_for_run(c.run_id) if c.run_id is not None else None
                    for c in children
                ]
                # Both runs of a pair take one slot.
                slots = {
                    ("pair", pair["id"]) if pair is not None else ("pid", child.process.pid)
                    for child, pair in zip(children, pairs, strict=True)
                }
                if len(slots) >= effective_run_limit(
                    Path(project), self.run_config
                ) and not self._arm_waiting([pair for pair in pairs if pair is not None]):
                    blocked.add(project)
            except Exception as exc:
                self._project_error(project, "task", exc)
                blocked.add(project)
        return self.scope.without(blocked)

    def _arm_waiting(self, pairs: list[dict[str, Any]]) -> bool:
        """One of ``pairs`` (those of a project's children) has a run that waits for a
        child: the second run before its start, or either run handed back. It shares
        the slot of the pair's running run, so the project stays open at its run limit;
        the ledger's own admission still holds the project's other tasks."""
        running = {child.run_id for child in self.children}
        for pair in pairs:
            for run_id in (pair["run_x"], pair["run_y"]):
                row = self.ledger.get_run(run_id) if run_id not in running else None
                if row is not None and row["status"] == RESUMING_RUN_STATUS:
                    return True
        return False

    # ── starting children ─────────────────────────────────────────────

    def _sweep_worktrees(self) -> None:
        if not self.watch:
            return
        now = self.clock()
        if (
            self.last_worktree_sweep is not None
            and now - self.last_worktree_sweep < WORKTREE_SWEEP_INTERVAL_S
        ):
            return
        self.last_worktree_sweep = now
        try:
            busy = {child.project for child in self.children}
            projects = sorted(
                {
                    str(row["project"])
                    for row in self.ledger.list_runs()
                    if self.scope.matches(str(row["project"])) and row["project"] not in busy
                }
            )
        except Exception as exc:
            self._say({"cleanup_error": str(exc)}, f"worktree cleanup failed: {exc}")
            return
        for project in projects:
            try:
                entries = clean_worktrees(
                    self.ledger,
                    config=self.run_config,
                    project=Path(project),
                    now=self.wall_clock(),
                )
                removed = [entry for entry in entries if entry["action"] == "removed"]
                if removed:
                    self._say(
                        {"cleaned": removed},
                        "\n".join(
                            f"cleaned {entry['run_id']} ({entry['reason']}): "
                            f"{entry['worktree_path']}"
                            for entry in removed
                        ),
                    )
            except Exception as exc:
                self._say(
                    {"cleanup_error": str(exc), "project": project},
                    f"worktree cleanup failed for {project}: {exc}",
                )

            try:
                # A deleted project keeps its ledger rows; there is nothing to maintain.
                if not Path(project).is_dir() or any(
                    row["status"] in {"starting", "running"}
                    for row in self.ledger.list_runs(project=project)
                ):
                    continue
                worktree_manager(Path(project)).run_maintenance()
            except Exception as exc:
                self._say(
                    {"maintenance_error": str(exc), "project": project},
                    f"git maintenance failed for {project}: {exc}",
                )

    def _fill(self) -> int:
        self._identify_children()
        in_flight = {child.project for child in self.children}
        self._reconcile(self.scope.without(self._backing_off(set(self.errors))))
        paused = self.ledger.is_paused()
        if paused:
            if self.paused is None:
                self._say({"paused": paused}, f"queue paused: {paused.get('reason')}; waiting")
            self.paused = paused
            return 0
        self.paused = None
        self._sweep_worktrees()
        sync_scope = self.scope.without(in_flight | self._backing_off(set(self.backoff)))
        for payload in self.base_sync.check(self.ledger, sync_scope, config=self.run_config):
            self._say({"base_sync": payload}, base_sync_text(payload))
        if self.begin_pass is not None:
            self.begin_pass()
        skips: dict[str, tuple[dict[str, Any], str]] = {}
        spawned = 0
        while self._may_start():
            # Recomputed each time: a start, or a project error, changes it.
            scope = self._run_scope()
            resumable = self.ledger.next_resumable(
                scope=scope,
                config=self.run_config,
                exclude={c.run_id for c in self.children if c.run_id is not None},
            )
            if resumable is not None:
                project, run_id = str(resumable["project"]), str(resumable["run_id"])
            else:
                row = self._next_task(scope, skips)
                if row is None:
                    break
                project, run_id = str(row["project"]), None
            if self._start(project, run_id):
                spawned += 1
        self._report_skips(skips)
        if self.watch and self.spawn_scout is not None and self.scout_config is not None:
            spawned += self._fill_scouts()
        return spawned

    def _fill_scouts(self) -> int:
        spawned = 0
        config = self.scout_config or {}
        for request in self.ledger.list_scout_requests():
            if not self._may_start():
                break
            project = str(request["project"])
            in_flight = {child.project for child in self.children}
            if project in in_flight or project in self._backing_off(set(self.backoff)):
                continue
            if not self.scope.matches(project):
                continue
            try:
                scout_gate(
                    Path(project),
                    self.ledger,
                    config,
                    self.wall_clock(),
                    refresh=False,
                    ignore_enabled_and_cooldown=True,
                    lens=request["lens"],
                )
            except ScoutError:
                continue
            except Exception as exc:
                self._project_error(project, "scout", exc)
                continue
            if self._start_scout(project, int(request["id"])):
                spawned += 1
        from helmraiser.loop.scout import scout_due_at
        from helmraiser.loop.scout_lenses import scout_lens_settings

        settings = scout_lens_settings(config, self.ledger)
        candidates = []
        for repo in configured_focus_projects(config):
            for lens, values in settings.items():
                if not values["auto"]:
                    continue
                try:
                    due = scout_due_at(repo, self.ledger, config, lens=lens)
                    candidates.append((due, str(repo), lens))
                except Exception as exc:
                    self._project_error(str(repo), "scout", exc)
        for _, project, lens in sorted(candidates):
            repo = Path(project)
            if not self._may_start():
                break
            project = str(repo)
            in_flight = {child.project for child in self.children}
            if project in in_flight or project in self._backing_off(set(self.backoff)):
                continue
            if not self.scope.matches(project):
                continue
            try:
                scout_gate(
                    repo,
                    self.ledger,
                    config,
                    self.wall_clock(),
                    refresh=False,
                    lens=lens,
                    automatic=True,
                )
            except ScoutError:
                continue
            except Exception as exc:
                self._project_error(project, "scout", exc)
                continue
            if self._start_scout(project, None, lens):
                spawned += 1
        return spawned

    def _backing_off(self, projects: set[str]) -> set[str]:
        now = self.clock()
        return {project for project in projects if self.backoff.get(project, 0.0) > now}

    def _next_task(
        self, scope: ProjectScope, skips: dict[str, tuple[dict[str, Any], str]]
    ) -> dict[str, Any] | None:
        while True:
            row = self.ledger.next_queued(scope=scope, exclude=set(skips), config=self.run_config)
            if row is None:
                return None
            project = str(row["project"])
            try:
                skip = self.skip(row)
            except Exception as exc:  # the task, or its project.toml, does not load
                self._project_error(project, "task", exc)
                scope = scope.without({project})
                continue
            self._clear_error(project, "task")
            if skip is None:
                return row
            skips[str(row["task_id"])] = skip

    def _report_skips(self, skips: dict[str, tuple[dict[str, Any], str]]) -> None:
        """Say each skip once, and again only when its reason changes."""
        for task_id, (payload, text) in skips.items():
            if self.reported_skips.get(task_id) != text:
                self._say({"skipped": task_id, **payload}, f"skipped {task_id}: {text}")
        current = {task_id: text for task_id, (_payload, text) in skips.items()}
        changed = current != self.reported_skips
        self.reported_skips = current
        self.result.skipped = set(skips)
        if changed:
            self._publish()

    def _start(self, project: str, run_id: str | None) -> bool:
        output = self.data_dir / "worker-children" / f"{uuid.uuid4().hex}.jsonl"
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            process = self.spawn(project, output)
        except Exception as exc:
            output.unlink(missing_ok=True)
            self._project_error(project, "start", exc)
            return False
        self._clear_error(project, "start")
        stat = _read_child_stat(process.pid)
        child = Child(
            project,
            process,
            output,
            self.wall_clock(),
            run_id,
            start_token=stat[1] if stat else None,
            code_commit=os.environ.get("HELMRAISER_CODE_SNAPSHOT"),
        )
        self.children.append(child)
        self.started += 1
        text = f"started {project} (pid {process.pid})"
        if run_id is not None:
            text += f", resuming run {run_id}"
        self._say({"started": child.as_dict()}, text)
        self._publish()
        return True

    def _start_scout(self, project: str, request_id: int | None, lens: str = "state") -> bool:
        assert self.spawn_scout is not None
        request = self.ledger.get_scout_request(request_id) if request_id is not None else None
        lens = request["lens"] if request else lens
        scout_id = str(uuid.uuid4())
        # The child consumes the request atomically with its scout record. Until
        # then it stays pending, including if this worker dies before spawning.
        output = self.data_dir / "worker-children" / f"{uuid.uuid4().hex}.jsonl"
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            process = self.spawn_scout(project, output, scout_id, request_id, lens)
        except Exception as exc:
            output.unlink(missing_ok=True)
            self._project_error(project, "scout", exc)
            return False
        stat = _read_child_stat(process.pid)
        child = Child(
            project,
            process,
            output,
            self.wall_clock(),
            start_token=stat[1] if stat else None,
            code_commit=os.environ.get("HELMRAISER_CODE_SNAPSHOT"),
            kind="scout",
            scout_id=scout_id,
            request_id=request_id,
            lens=lens,
        )
        self.children.append(child)
        self._say(
            {"scout_started": child.as_dict()},
            f"scout started {project} lens={lens} (pid {process.pid})",
        )
        self._publish()
        return True

    def _reconcile(self, scope: ProjectScope, *, dead_worker_pids: set[int] | None = None) -> None:
        """Reconcile phantom runs one project at a time, so that one project's broken
        state cannot keep the other projects' phantoms open."""
        scope = scope.without(
            {c.project for c in self.children if c.kind == "run" and c.run_id is None}
        )
        protected = {c.run_id for c in self.children if c.run_id is not None}
        projects = sorted(
            {
                str(run["project"])
                for run in self.ledger.list_runs()
                if str(run["status"]) not in TERMINAL_RUN_STATUSES
                and str(run["status"]) not in IDLE_RUN_STATUSES
                and scope.matches(str(run["project"]))
            }
        )
        for project in projects:
            try:
                project_scope = ProjectScope(frozenset({project}))
                phantoms = find_phantom_runs(
                    self.ledger,
                    project_scope,
                    dead_worker_pids=dead_worker_pids,
                    exclude_run_ids=protected,
                )
                reconciled = reconcile(self.ledger, phantoms, self.controller)
            except Exception as exc:
                self._project_error(project, "reconcile", exc)
                continue
            self._clear_error(project, "reconcile")
            if reconciled:
                self._say(
                    {"reconciled": reconciled},
                    "\n".join(
                        f"reconciled {item['run_id']}: {item['reason']}" for item in reconciled
                    ),
                )

    # ── project errors ────────────────────────────────────────────────

    def _project_error(self, project: str, stage: str, error: BaseException | str) -> None:
        """Report an error in one project's work, keep the project out for
        ``PROJECT_ERROR_BACKOFF_S``, and notify once per distinct error."""
        text = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        self.backoff[project] = self.clock() + PROJECT_ERROR_BACKOFF_S
        previous = self.errors.get(project)
        now = self.wall_clock()
        self.error_times[project] = now
        entry = {
            "project": project,
            "stage": stage,
            "error": text,
            "since": previous["since"] if previous else now,
            "retry_at": now + PROJECT_ERROR_BACKOFF_S,
        }
        self.errors[project] = entry
        self._say(
            {"project_error": entry},
            f"{project}: {stage} error: {text}; "
            f"not tried again for {PROJECT_ERROR_BACKOFF_S / 60:g} min",
        )
        if (project, text) not in self.alerted:
            self.alerted.add((project, text))
            if isinstance(error, BaseException):  # the traceback once, for the log
                traceback.print_exception(error, file=sys.stderr)
            self.alert(f"{Path(project).name}: {text}")
        self._publish()

    def _clear_error(self, project: str, stage: str, started_at: float | None = None) -> None:
        """Only a later attempt can clear an infrastructure error from a sibling."""
        if started_at is not None and started_at <= self.error_times.get(project, float("-inf")):
            return
        if self.errors.get(project, {}).get("stage") == stage:
            del self.errors[project]
            self.error_times.pop(project, None)
            self._publish()

    # ── finished children ─────────────────────────────────────────────

    def _reap(self) -> bool:
        done = []
        for child in self.children:
            exit_code = child.process.poll()
            if exit_code is not None:
                done.append((child, exit_code))
        for child, exit_code in done:
            self.children.remove(child)
            self._finish(child, exit_code)
            self.registered.discard(child.process.pid)
            if child.kind == "run":
                self._identify_children()
                self._reconcile(
                    ProjectScope(frozenset({child.project})),
                    dead_worker_pids={child.process.pid},
                )
        if done:
            self._publish()
        return bool(done)

    def _finish(self, child: Child, exit_code: int) -> None:
        if child.kind == "scout":
            self._finish_scout(child, exit_code)
            return
        try:
            lines = child.output.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        child.output.unlink(missing_ok=True)
        outcomes: list[dict[str, Any]] = []
        other: list[Any] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                data: Any = json.loads(line)
            except ValueError:
                data = line
            if is_outcome(data):
                outcomes.append(data)
            elif not (isinstance(data, dict) and "runs" in data):  # the child's own summary
                other.append(data)
        self.result.outcomes.extend(outcomes)
        if outcomes:
            self._clear_error(child.project, "run", child.started_at)
        else:
            self.backoff[child.project] = self.clock() + NO_RUN_BACKOFF_S
        finished = {
            **child.as_dict(),
            "exit_code": exit_code,
            "runs": outcomes,
            "output": other,
        }
        if self.as_json:
            typer.echo(json.dumps({"finished": finished}))
        else:
            for outcome in outcomes:
                for text in outcome_text(outcome):
                    typer.echo(text)
            for item in other:
                if isinstance(item, dict) and item.get("error"):
                    typer.echo(f"{child.project}: error: {item['error']}")
            if not outcomes:
                typer.echo(
                    f"{child.project}: pid {child.process.pid} exited ({exit_code}) without a run;"
                    f" not tried again for {NO_RUN_BACKOFF_S / 60:g} min"
                )
        for outcome in outcomes:
            self.notify(outcome)
        if not outcomes and exit_code not in (0, EXIT_PAUSED):
            errors = [str(i["error"]) for i in other if isinstance(i, dict) and i.get("error")]
            self._project_error(
                child.project,
                "run",
                errors[-1] if errors else f"exited ({exit_code}) without a run; see the log",
            )

    def _finish_scout(self, child: Child, exit_code: int) -> None:
        try:
            lines = child.output.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        child.output.unlink(missing_ok=True)
        outcomes: list[dict[str, Any]] = []
        other: list[Any] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                data: Any = json.loads(line)
            except ValueError:
                data = line
            if is_scout_outcome(data):
                outcomes.append(data)
            else:
                other.append(data)
        outcome = outcomes[0] if len(outcomes) == 1 else None
        if outcome is not None and outcome.get("scout_id") != child.scout_id:
            other.append({"error": "scout outcome id does not match assigned id"})
            outcome = None
        drafts = list(outcome.get("written_task_ids") or []) if outcome is not None else []
        finished = {
            **child.as_dict(),
            "exit_code": exit_code,
            "scout_id": child.scout_id,
            "outcome": outcome.get("outcome") if outcome else None,
            "drafts": len(drafts),
            "report": outcome.get("report_path") if outcome else None,
            "output": other,
        }
        self._say(
            {"scout_finished": finished},
            f"scout finished {child.project} lens={child.lens} (pid {child.process.pid}, exit {exit_code}, "
            f"{finished['outcome'] or 'no outcome'}): {len(drafts)} draft(s)",
        )
        if drafts:
            self.alert(f"{len(drafts)} drafts for {Path(child.project).name} — helmraiser drafts")
        succeeded = exit_code == 0 and outcome is not None and outcome.get("outcome") == "succeeded"
        if succeeded:
            self._clear_error(child.project, "scout")
            return
        if outcome is None and child.request_id is not None:
            request = self.ledger.get_scout_request(child.request_id)
            waiting = any(
                isinstance(item, dict) and item.get("scout_waiting") is True for item in other
            )
            if waiting or request is None or request["scout_id"] not in (None, child.scout_id):
                # A changed safety gate, cancellation, or competing child prevented
                # this scout from starting. Do not turn that into a failed scout.
                self._clear_error(child.project, "scout")
                return
        errors = [
            str(item["error"]) for item in other if isinstance(item, dict) and item.get("error")
        ]
        detail = outcome.get("error") if outcome is not None else None
        error = str(detail or (errors[-1] if errors else f"scout exited ({exit_code})"))
        if outcome is None and child.scout_id is not None:
            scout = self.ledger.get_scout(child.scout_id)
            report = RunDir(child.scout_id, self.data_dir).create().path / "scout.md"
            if not report.exists():
                report.write_text(
                    f"# Scout {child.scout_id}\n\nLens: {child.lens}\n\n## Failure\n\n{error}\n",
                    encoding="utf-8",
                )
            if scout is None:
                scout_config = (self.scout_config or {}).get("routing", {}).get("scout", {})
                try:
                    self.ledger.create_scout(
                        {
                            "id": child.scout_id,
                            "project": child.project,
                            "model": str(scout_config.get("model") or "unknown"),
                            "request_id": child.request_id,
                            "lens": child.lens,
                            "started_at": child.started_at,
                            "ended_at": self.wall_clock(),
                            "outcome": "failed",
                            "error": error,
                            "report_path": str(report),
                        }
                    )
                except ScoutRequestUnavailable:
                    # Cancellation/another child can win even during failure handling.
                    self._clear_error(child.project, "scout")
                    return
            if scout is not None and scout["outcome"] == "running":
                self.ledger.finish_scout(
                    child.scout_id,
                    outcome="failed",
                    cost_usd=float(scout["cost_usd"] or 0.0),
                    tokens_in=int(scout["tokens_in"] or 0),
                    tokens_out=int(scout["tokens_out"] or 0),
                    drafts_written=list(scout["drafts_written"]),
                    error=error,
                )
        self._project_error(child.project, "scout", error)

    # ── output ────────────────────────────────────────────────────────

    def _say(self, payload: dict[str, Any], text: str) -> None:
        typer.echo(json.dumps(payload) if self.as_json else text)

    def _publish(self) -> None:
        """List the children in ``worker.active.json`` when this process is the
        recorded background worker, and rewrite ``status.json``."""
        record = read_record(self.data_dir)
        if record is not None and record.get("pid") == os.getpid():
            write_active(
                {
                    "pid": os.getpid(),
                    "max_parallel_runs": self.max_parallel,
                    "children": [child.as_dict() for child in self.children],
                    "errors": list(self.errors.values()),
                    "skips": [
                        {"task_id": task_id, "reason": reason}
                        for task_id, reason in self.reported_skips.items()
                    ],
                },
                self.data_dir,
            )
        try:
            write_status(self.ledger, self.data_dir)
        except sqlite3.OperationalError as exc:
            if not is_busy(exc):
                raise
            self._ledger_busy("status", exc)

    def _ledger_busy(self, stage: str, exc: sqlite3.OperationalError) -> None:
        self._say(
            {"ledger_busy": {"stage": stage, "error": str(exc)}},
            f"ledger busy during {stage}: {exc}; trying again next tick",
        )
