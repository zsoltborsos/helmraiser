"""``worker start|stop|status``: run the work queue in the background.

``start`` spawns ``python -m helmraiser work --watch --json …`` detached from the
terminal and records it, with its project scope and parallel limit, in
``<data_dir>/worker.json``; the worker adds ``code`` (``{commit, snapshot}``), the
code snapshot it runs (see ``helmraiser.code_snapshot``), and a private
``restart_children`` handoff for immediate re-exec and adoption. It lists its child runs in
``<data_dir>/worker.active.json``. ``stop`` writes ``<data_dir>/worker.stop``,
which the ``work`` loop honours between runs (see ``commands.work``). The worker
adds ``exit`` to its record when it ends (see ``update_record``). Every command
takes ``--json`` and ``--data-dir``.
"""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.console import Console

from helmraiser.cli.model_setup import require_work_models
from helmraiser.config import load_config
from helmraiser.ledger.db import ProjectScope, default_data_dir

console = Console()
worker_app = typer.Typer(help="Run the work queue in the background")

EXIT_ERROR = 1

RECORD_NAME = "worker.json"
ACTIVE_NAME = "worker.active.json"
STOP_NAME = "worker.stop"
LOG_NAME = "worker.log"


def data_base(data_dir: Path | None) -> Path:
    return data_dir.expanduser() if data_dir is not None else default_data_dir()


def record_path(data_dir: Path | None) -> Path:
    return data_base(data_dir) / RECORD_NAME


def stop_path(data_dir: Path | None) -> Path:
    return data_base(data_dir) / STOP_NAME


def log_path(data_dir: Path | None) -> Path:
    return data_base(data_dir) / LOG_NAME


def active_path(data_dir: Path | None) -> Path:
    return data_base(data_dir) / ACTIVE_NAME


def work_scope(projects: list[str] | None, config: dict[str, Any]) -> ProjectScope:
    """The projects ``work`` serves: the ``--project`` values, else every project but
    the config's ``worker_exclude_projects``."""
    if projects:
        return ProjectScope.of(projects)
    return ProjectScope.of(None, [str(p) for p in config.get("worker_exclude_projects") or []])


def max_parallel(config: dict[str, Any], override: int | None) -> int:
    """``--max-parallel``, else the config's ``max_parallel_runs`` (default 2)."""
    return override if override is not None else int(config.get("max_parallel_runs", 2))


def read_record(data_dir: Path | None) -> dict[str, Any] | None:
    try:
        record = json.loads(record_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def write_record(record: dict[str, Any], data_dir: Path | None) -> Path:
    path = record_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def update_record(
    pid: int, changes: dict[str, Any], data_dir: Path | None
) -> dict[str, Any] | None:
    """Merge ``changes`` into ``worker.json`` when it records ``pid``, and return the
    record; ``None`` when it records another worker or none. It takes the start lock,
    so a worker that gets here before ``worker start`` has written its record waits
    for it."""
    path = record_path(data_dir)
    if not path.exists():
        return None
    with path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        record = read_record(data_dir)
        if record is None or record.get("pid") != pid:
            return None
        if changes:
            record.update(changes)
            write_record(record, data_dir)
        return record


EXIT_REASONS = {"stopped": "stopped", "max_runs": "--max runs done", "drained": "queue drained"}


def exit_text(ended: dict[str, Any]) -> str:
    """A record's ``exit`` as text: ``stopped``, ``error: <why>``, …"""
    reason = str(ended.get("reason"))
    if reason == "error":
        return f"error: {ended.get('error')}"
    return EXIT_REASONS.get(reason, reason)


def pid_alive(pid: int) -> bool:
    """True while the pid exists and its cmdline mentions ``work``.

    The cmdline check keeps a reused pid from being mistaken for the worker.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True
    return b"work" in cmdline


def worker_state(data_dir: Path | None) -> str:
    record = read_record(data_dir)
    if record is None:
        return "none"
    return "running" if pid_alive(int(record["pid"])) else "exited"


def worker_serves(data_dir: Path | None, project: Path) -> bool:
    """Whether the recorded worker's scope covers ``project`` (paths compared
    resolved). A record without ``projects`` (an older worker) is read from its argv."""
    record = read_record(data_dir) or {}
    if "projects" in record:
        include = record["projects"]
    else:
        argv = [str(a) for a in record.get("argv") or []]
        include = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "--project"] or None
    exclude = record.get("exclude_projects") or []
    target = project.expanduser().resolve()

    def named(paths: list[str]) -> bool:
        return any(Path(p).expanduser().resolve() == target for p in paths)

    return (include is None or named(include)) and not named(exclude)


def write_active(payload: dict[str, Any], data_dir: Path | None) -> None:
    """Replace ``worker.active.json`` atomically."""
    path = active_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".worker-active-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def worker_payload(data_dir: Path | None) -> dict[str, Any]:
    """The worker record with a ``state`` key; ``{"state": "none"}`` without one. A
    running worker also gets ``children``: the runs it has in flight, and ``errors``:
    its current per-project errors, and ``skips``: current scheduling skip reasons,
    from ``worker.active.json``. The matching running worker's active payload also
    supplies its live parallel limit when present."""
    record = read_record(data_dir)
    if record is None:
        return {"state": "none"}
    payload = dict(record)
    running = pid_alive(int(record["pid"]))
    payload["state"] = "running" if running else "exited"
    if running:
        try:
            active = json.loads(active_path(data_dir).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            active = {}
        mine = isinstance(active, dict) and active.get("pid") == record["pid"]
        if mine and "max_parallel_runs" in active:
            payload["max_parallel_runs"] = active["max_parallel_runs"]
        payload["children"] = list(active.get("children") or []) if mine else []
        payload["errors"] = list(active.get("errors") or []) if mine else []
        payload["skips"] = list(active.get("skips") or []) if mine else []
    return payload


def worker_argv(
    projects: list[str] | None,
    max_runs: int | None,
    max_parallel_runs: int | None,
    no_caps: bool,
    no_notify: bool,
    data_dir: Path | None,
) -> list[str]:
    """The detached command: ``python -m helmraiser work --watch --json <options>``."""
    argv = [sys.executable, "-m", "helmraiser", "work", "--watch", "--json"]
    for project in projects or []:
        argv += ["--project", project]
    if max_runs is not None:
        argv += ["--max", str(max_runs)]
    if max_parallel_runs is not None:
        argv += ["--max-parallel", str(max_parallel_runs)]
    if no_caps:
        argv.append("--no-caps")
    if no_notify:
        argv.append("--no-notify")
    if data_dir is not None:
        argv += ["--data-dir", str(data_dir)]
    return argv


def _tail(path: Path, lines: int = 5) -> list[str]:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return content[-lines:]


TAIL_WIDTH = 120


def compact_line(line: str, width: int = TAIL_WIDTH) -> str:
    """A ``worker.log`` line as one short line: a JSON event summarised (a finished
    child's line carries whole run outcomes), any other line as it is; either cut to
    ``width`` characters."""
    try:
        data: Any = json.loads(line)
    except ValueError:
        data = None
    text = _event_text(data) if isinstance(data, dict) and data else line
    return text if len(text) <= width else text[: width - 1] + "…"


def _event_text(event: dict[str, Any]) -> str:
    started, finished = event.get("started"), event.get("finished")
    scout_started, scout_finished = event.get("scout_started"), event.get("scout_finished")
    error, summary = event.get("project_error"), event.get("summary")
    if isinstance(started, dict):
        text = f"started {started.get('project')} (pid {started.get('pid')})"
        return text + (f", resuming run {started['run_id']}" if started.get("run_id") else "")
    if isinstance(finished, dict):
        runs = [run for run in finished.get("runs") or [] if isinstance(run, dict)]
        done = ", ".join(f"{run.get('status')} {run.get('task_id')}" for run in runs)
        return (
            f"finished {finished.get('project')} (pid {finished.get('pid')}, "
            f"exit {finished.get('exit_code')}): {done or 'no run'}"
        )
    if isinstance(scout_started, dict):
        return f"scout started {scout_started.get('project')} (pid {scout_started.get('pid')})"
    if isinstance(scout_finished, dict):
        return (
            f"scout finished {scout_finished.get('project')} "
            f"(pid {scout_finished.get('pid')}, exit {scout_finished.get('exit_code')}, "
            f"{scout_finished.get('outcome') or 'no outcome'}): "
            f"{scout_finished.get('drafts', 0)} draft(s)"
        )
    if isinstance(error, dict):
        return f"error: {error.get('project')} ({error.get('stage')}): {error.get('error')}"
    if isinstance(summary, dict) and isinstance(summary.get("counts"), dict):
        counts = ", ".join(f"{k} {v}" for k, v in summary["counts"].items() if v)
        return f"summary: {counts or 'no runs'}"
    if len(event) == 1:
        key, value = next(iter(event.items()))
        return f"{key}: {value if isinstance(value, str) else json.dumps(value)}"
    return json.dumps(event)


def _fail(message: str, *, as_json: bool) -> NoReturn:
    if as_json:
        typer.echo(json.dumps({"error": message}))
    else:
        console.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(code=EXIT_ERROR)


@worker_app.command()
def start(
    project: list[str] | None = typer.Option(
        None, "--project", help="Serve only this repository (repeatable)"
    ),
    max_runs: int | None = typer.Option(None, "--max", help="Run at most N tasks"),
    max_parallel_runs: int | None = typer.Option(
        None,
        "--max-parallel",
        min=1,
        help="Run up to N projects at once (config max_parallel_runs)",
    ),
    no_caps: bool = typer.Option(False, "--no-caps", help="Disable spend caps for this worker"),
    no_notify: bool = typer.Option(False, "--no-notify", help="Disable run notifications"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Run the work queue in the background, detached from this terminal. The worker
    stays up, waiting when nothing is eligible, until ``helmraiser worker stop``."""
    config = load_config()
    base = data_base(data_dir)
    require_work_models(
        config, base, work_scope(project, config), max_runs=max_runs, as_json=as_json
    )
    base.mkdir(parents=True, exist_ok=True)
    projects = list(project) if project else None
    argv = worker_argv(projects, max_runs, max_parallel_runs, no_caps, no_notify, data_dir)
    scope = work_scope(projects, config)
    log = log_path(data_dir)
    # The record file doubles as the start lock: flock serializes the
    # check-and-spawn, so concurrent starts cannot both launch a worker (the
    # later record would otherwise orphan the first, leaving it unstoppable).
    with record_path(data_dir).open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        record = read_record(data_dir)
        if record is not None and pid_alive(int(record["pid"])):
            _fail(f"a worker is already running (pid {record['pid']})", as_json=as_json)
        with log.open("ab") as log_handle:
            process = subprocess.Popen(
                argv,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
        new_record = {
            "pid": process.pid,
            "started_at": datetime.now(UTC).isoformat(),
            "argv": argv,
            "log": str(log),
            "projects": sorted(scope.include) if scope.include is not None else None,
            "exclude_projects": sorted(scope.exclude),
            "max_parallel_runs": max_parallel(config, max_parallel_runs),
        }
        write_record(new_record, data_dir)
    if as_json:
        typer.echo(json.dumps(new_record))
    else:
        typer.echo(f"worker started (pid {process.pid}), log {log}")


@worker_app.command()
def status(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Show whether the background worker is running, with the log tail: one short line
    per event (``--json`` keeps the raw lines)."""
    payload = worker_payload(data_dir)
    if payload["state"] != "none":
        payload = {**payload, "tail": _tail(log_path(data_dir))}
    if as_json:
        typer.echo(json.dumps(payload))
        return
    state = payload["state"]
    if state == "none":
        typer.echo("no worker")
        return
    typer.echo(f"worker: {state} (pid {payload['pid']}, started {payload['started_at']})")
    typer.echo(f"argv: {shlex.join(payload['argv'])}")
    typer.echo(f"log: {payload['log']}")
    code = payload.get("code")
    if isinstance(code, dict) and code.get("commit"):
        typer.echo(f"code: {str(code['commit'])[:7]} (snapshot)")
    if state == "exited" and isinstance(payload.get("exit"), dict):
        typer.echo(f"exit: {exit_text(payload['exit'])} (at {payload['exit'].get('at')})")
    if state == "running" and "max_parallel_runs" in payload:
        children = payload.get("children") or []
        typer.echo(f"runs: {len(children)} of {payload['max_parallel_runs']}")
        for child in children:
            suffix = f", run {str(child['run_id'])[:8]}" if child.get("run_id") else ""
            typer.echo(f"  {child['project']} (pid {child['pid']}){suffix}")
    for error in payload.get("errors") or []:
        typer.echo(f"error: {error['project']} ({error['stage']}): {error['error']}")
    for line in payload["tail"]:
        typer.echo(f"  {compact_line(line)}")


@worker_app.command()
def stop(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Ask the background worker to finish its current runs and stop."""
    record = read_record(data_dir)
    if record is None or not pid_alive(int(record["pid"])):
        if as_json:
            typer.echo(json.dumps({"stopped": False, "message": "no worker is running"}))
        else:
            typer.echo("no worker is running")
        return
    data_base(data_dir).mkdir(parents=True, exist_ok=True)
    stop_path(data_dir).write_text("stop\n", encoding="utf-8")
    message = "stop requested; the worker finishes its current runs first"
    if as_json:
        typer.echo(json.dumps({"stopped": True, "message": message}))
    else:
        typer.echo(message)
