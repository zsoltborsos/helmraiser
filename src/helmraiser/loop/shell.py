"""``/bin/bash -o pipefail -c`` runner shared by setup, verify and evidence steps.

With a ``wrap`` (process isolation's command sandbox settings) the bash argv runs inside srt
with stdin on ``/dev/null``: an inherited descriptor is outside srt's socket filter.

A command longer than ``SCRIPT_THRESHOLD`` bytes runs from a script file in ``/tmp``
(writable and readable in the command sandbox) instead of ``-c``: srt and the service relay
quote the command into one shell argument, which Linux caps at 128 KiB.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from helmraiser.isolation.retry import SandboxLaunch

# Turns the bash argv into the sandboxed argv (``srt --settings <file> -- ...``).
Wrap = Callable[[list[str]], list[str]]

# Where a verify / evidence step ran: inside srt, unsandboxed (``isolation: none``),
# or unconfined because the step says ``sandbox: host``.
SANDBOX_SRT = "srt"
SANDBOX_NONE = "none"
SANDBOX_HOST = "host"
UNCONFINED_NOTE = "ran unconfined (sandbox: host)"
SCRIPT_THRESHOLD = 4096
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


@dataclass(frozen=True)
class ShellResult:
    exit_code: int | None  # None when the command timed out or could not start
    seconds: float
    output: str  # stdout + stderr
    timed_out: bool = False
    error: str | None = None


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


def tail_lines(text: str, lines: int) -> str:
    return "\n".join(text.rstrip().splitlines()[-lines:]) if lines > 0 else ""


def step_sandbox(step_sandbox: str | None, wrap: Wrap | None) -> str:
    """``host`` for a ``sandbox: host`` step, else ``srt`` with a wrap, else ``none``."""
    if step_sandbox == SANDBOX_HOST:
        return SANDBOX_HOST
    return SANDBOX_SRT if wrap is not None else SANDBOX_NONE


def run_shell(
    command: str, cwd: Path, timeout: int, env: dict[str, str], wrap: Wrap | None = None
) -> ShellResult:
    started = time.monotonic()
    script: str | None = None
    if len(command.encode("utf-8")) > SCRIPT_THRESHOLD:
        fd, script = tempfile.mkstemp(prefix="helmraiser-step-", suffix=".sh", dir="/tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(command)
        argv = ["/bin/bash", "-o", "pipefail", script]
    else:
        argv = ["/bin/bash", "-o", "pipefail", "-c", command]
    try:
        return _run(argv, cwd, timeout, env, wrap, started)
    finally:
        if script is not None:
            os.unlink(script)


def _run(
    argv: list[str],
    cwd: Path,
    timeout: int,
    env: dict[str, str],
    wrap: Wrap | None,
    started: float,
) -> ShellResult:
    for attempt in range(2):
        launch: list[str] | None = None
        try:
            launch = wrap(argv) if wrap is not None else argv
            remaining = timeout if attempt == 0 else timeout - (time.monotonic() - started)
            if remaining <= 0:
                return ShellResult(None, round(time.monotonic() - started, 3), "", timed_out=True)
            proc = subprocess.run(
                launch.guarded_argv() if isinstance(launch, SandboxLaunch) else launch,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=remaining,
                env=env,
                stdin=subprocess.DEVNULL if wrap is not None else None,
            )
            if attempt == 0 and isinstance(launch, SandboxLaunch):
                path = launch.retry_path(proc.returncode, proc.stdout, proc.stderr)
                if path is not None:
                    launch.record_retry(path)
                    continue
            return ShellResult(
                proc.returncode,
                round(time.monotonic() - started, 3),
                _strip_ansi(proc.stdout + proc.stderr),
            )
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or b"").decode("utf-8", errors="replace") if exc.stdout else ""
            output += (exc.stderr or b"").decode("utf-8", errors="replace") if exc.stderr else ""
            return ShellResult(
                None, round(time.monotonic() - started, 3), _strip_ansi(output), timed_out=True
            )
        except OSError as exc:
            return ShellResult(None, round(time.monotonic() - started, 3), str(exc), error=str(exc))
        finally:
            if isinstance(launch, SandboxLaunch):
                launch.cleanup()
    raise AssertionError("sandbox launch attempts exhausted")
