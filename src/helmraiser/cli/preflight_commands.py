"""``helmraiser preflight``: try a project's commands in the real sandbox before queueing."""

from __future__ import annotations

import json
import shlex
import signal
import sys
from pathlib import Path
from types import FrameType
from typing import Any, NoReturn

import typer

from helmraiser.config import load_config
from helmraiser.ledger.db import default_data_dir
from helmraiser.loop.preflight import PreflightError, run_preflight

EXIT_ERROR = 1
EXIT_VALIDATION = 2
EXIT_INTERRUPTED = 130


def _fail(message: str, *, as_json: bool, code: int) -> NoReturn:
    typer.echo(json.dumps({"error": message}) if as_json else f"Error: {message}")
    raise typer.Exit(code=code)


def _interrupt(_signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def _print(payload: dict[str, Any]) -> None:
    typer.echo(f"preflight {payload['repo']} at {payload['head'][:12]} in {payload['worktree']}")
    setup = payload["setup"]
    if setup is not None:
        state = "ok" if setup["ok"] else f"exit {setup['exit_code']}"
        typer.echo(f"setup: {state} in {setup['seconds']}s")
        if not setup["ok"]:
            for line in setup["tail"].splitlines():
                typer.echo(f"  | {line}")
            for hint in setup.get("hints", []):
                typer.echo(f"  hint: {hint}")
    for service in payload["services"]:
        provides = ", ".join(service["provides"])
        typer.echo(f"service: {service['name']} 127.0.0.1:{service['host_port']} ({provides})")
    for command in payload["commands"]:
        typer.echo(f"$ {command['command']}")
        exit_code = command["exit_code"]
        state = "timed out" if exit_code is None else f"exit {exit_code}"
        typer.echo(f"  {state} in {command['seconds']}s ({command['sandbox']})")
        for line in command["tail"].splitlines():
            typer.echo(f"  | {line}")
        for hint in command["hints"]:
            typer.echo(f"  hint: {hint}")
    for warning in payload["warnings"]:
        typer.echo(f"[warn] {warning}")
    cleanup = "worktree removed" if not Path(payload["worktree"]).exists() else "worktree left"
    if payload["services"]:
        cleanup += ", services stopped"
    total = len(payload["commands"])
    failed = sum(not command["passed"] for command in payload["commands"])
    if payload["error"]:
        typer.echo(f"stopped: {payload['error']}; {cleanup}")
    elif failed:
        typer.echo(f"{failed} of {total} commands failed; {cleanup}")
    else:
        typer.echo(f"{total} of {total} commands passed; {cleanup}")


def preflight(
    repo: Path = typer.Argument(..., help="Repository whose HEAD to try"),
    command: list[str] | None = typer.Argument(
        None, help="After --: one command (a single argument is a shell command line)"
    ),
    run: list[str] = typer.Option([], "--run", help="A shell command to run; repeatable"),
    timeout: int = typer.Option(600, "--timeout", help="Seconds per command"),
    tail: int = typer.Option(40, "--tail", help="Output lines to show per command"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override HELMRAISER_DATA_DIR"),
) -> None:
    """Run commands as verify would (setup, services, verify sandbox settings) in a throwaway
    worktree of HEAD, with hints for known sandbox failures. Writes nothing to the ledger."""
    commands = list(run)
    if command:
        commands.append(command[0] if len(command) == 1 else shlex.join(command))
    if not commands:
        _fail(
            "no command: pass --run <cmd> or -- <command…>", as_json=as_json, code=EXIT_VALIDATION
        )
    base = data_dir.expanduser() if data_dir is not None else default_data_dir()

    def progress(message: str) -> None:
        if not as_json and sys.stderr.isatty():
            typer.echo(message, err=True)

    previous = signal.signal(signal.SIGTERM, _interrupt)
    try:
        payload = run_preflight(
            repo.expanduser().resolve(),
            commands,
            config=load_config(),
            data_dir=base,
            timeout=timeout,
            tail=tail,
            progress=progress,
        )
    except PreflightError as exc:
        _fail(str(exc), as_json=as_json, code=EXIT_ERROR)
    except KeyboardInterrupt:
        _fail("interrupted; cleaned up", as_json=as_json, code=EXIT_INTERRUPTED)
    finally:
        signal.signal(signal.SIGTERM, previous)
    if as_json:
        typer.echo(json.dumps(payload))
    else:
        _print(payload)
    if not payload["ok"]:
        raise typer.Exit(code=EXIT_ERROR)
