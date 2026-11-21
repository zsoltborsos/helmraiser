"""``helmraiser preflight``: run a project's commands the way verify would, before queueing.

A throwaway worktree of the primary checkout's HEAD (detached, in the project's
``worktree_dir``), the project's ``setup`` under its own sandbox settings, its
``[[services]]``, then each command under the verify sandbox settings (srt, the verify environment, the service
relays) with the runner's own helpers. Known sandbox signatures in a command's output get
a one-line hint (``loop/hints.py``; a refused loopback port that a service provides gets
none). Services, worktree and run directory are always removed, also on an error or
Ctrl-C. Nothing is written to the ledger.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from helmraiser.isolation.process import ProcessIsolation, tracked_denial_warnings, tracked_denials
from helmraiser.isolation.setup import prepare_setup
from helmraiser.ledger.rundir import RunDir
from helmraiser.loop.hints import SETUP_READONLY_HINT, sandbox_hints, setup_readonly_line
from helmraiser.loop.runner import (
    DEFAULT_SETUP_TIMEOUT,
    command_env,
    command_wrap,
    helmraiser_env,
    start_services,
    step_env,
)
from helmraiser.loop.services import RunningService, ServiceError, stop_services
from helmraiser.loop.shell import run_shell, tail_lines
from helmraiser.loop.verify import run_step
from helmraiser.pipeline.run_identity import resolve_run_id
from helmraiser.task.model import VerifyStep
from helmraiser.task.project import load_project_defaults, worktree_manager

# Kept whole so the hints see every line; the report shows only the tail.
FULL_OUTPUT_LINES = 1_000_000


class PreflightError(Exception):
    """Preflight cannot start: not a repository, no sandbox, or a broken project.toml."""


def _is_missing_target_diagnostic(line: str, path: str) -> bool:
    """Whether ``line`` is a known diagnostic whose missing target is ``path``.

    Anchoring diagnostics avoids treating prose and assertion messages that quote a
    diagnostic (for example, ``AssertionError: Could not find ... in response``) as
    evidence that the task-created file itself is missing.
    """
    escaped = re.escape(path)
    diagnostic_patterns = (
        rf"^\s*(?:Error:\s*)?Could not find\s+(['\"]){escaped}\1(?:\s|$)",
        rf"^\s*ERROR:\s+file or directory not found:\s*(['\"]?){escaped}\1(?:\s|$)",
        rf"^\s*(?:Error:\s*)?Cannot find module\s+(['\"]){escaped}\1(?:\s|$)",
        rf"^\s*\S*python\S*:\s+can't open file[^\n]*{escaped}" rf"[^\n]*No such file or directory",
        rf"^\s*(?:FileNotFoundError|OSError):[^\n]*No such file or directory" rf"[^\n]*{escaped}",
    )
    return any(re.search(pattern, line) is not None for pattern in diagnostic_patterns)


def _path_exists_at_head(repo: Path, path: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"HEAD:{path}"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def classify_preflight_step(
    repo: Path, result: dict[str, Any], candidate_files: tuple[str, ...]
) -> str:
    """Classify one preflight command, requiring same-line missing-target evidence."""
    if result.get("passed") or result.get("exit_code") == 0:
        return "fail" if result.get("red_on_base") else "green"
    if result.get("hints"):
        return "sandbox"
    if result.get("red_on_base"):
        return "expected red"
    output = str(result.get("output") or "")
    for line in output.splitlines():
        for path in candidate_files:
            if _is_missing_target_diagnostic(line, path) and not _path_exists_at_head(repo, path):
                return "expected red"
    return "fail"


def normalize_scout_preflight(
    repo: Path, payload: dict[str, Any], candidate_files: tuple[str, ...]
) -> dict[str, Any]:
    """Reduce internal preflight output to persistent scout-draft metadata."""
    steps = []
    for result in payload.get("commands") or []:
        classification = classify_preflight_step(repo, result, candidate_files)
        hints = result.get("hints") or []
        tail = str(result.get("tail") or "")
        if classification == "fail" and result.get("red_on_base"):
            diagnostic = (
                "Step was declared red on base but passed on the unchanged code, "
                "so it cannot detect the change."
            )
            tail = f"{tail}\n{diagnostic}" if tail else diagnostic
        steps.append(
            {
                "name": str(result.get("name") or result.get("command") or "command"),
                "exit_code": result.get("exit_code"),
                "seconds": float(result.get("seconds") or 0.0),
                "class": classification,
                "hint": str(hints[0]) if hints else None,
                "tail": tail,
            }
        )
    error = payload.get("error")
    counts = {
        classification: sum(step["class"] == classification for step in steps)
        for classification in ("fail", "sandbox")
    }
    if error and not steps:
        summary = "fail(1)"
    elif counts["fail"]:
        summary = f"fail({counts['fail']})"
    elif counts["sandbox"]:
        summary = f"sandbox({counts['sandbox']})"
    elif any(step["class"] == "expected red" for step in steps):
        summary = "expected red"
    else:
        summary = "green"
    return {"summary": summary, "error": str(error) if error else None, "steps": steps}


def failed_scout_preflight(error: str) -> dict[str, Any]:
    return {"summary": "fail(1)", "error": error, "steps": []}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)


def require_primary_checkout(repo: Path) -> None:
    """Refuse a linked worktree: ``.helmraiser/project.toml`` is read from the primary checkout.

    A path that is not a repository passes; ``run_preflight_steps`` reports that.
    """
    dirs = _git(repo, "rev-parse", "--absolute-git-dir", "--git-common-dir")
    lines = dirs.stdout.splitlines()
    if dirs.returncode != 0 or len(lines) != 2:
        return
    if Path(lines[0]).resolve() == (repo / lines[1]).resolve():
        return
    listed = _git(repo, "worktree", "list", "--porcelain").stdout.splitlines()
    primary = next(
        (line[len("worktree ") :] for line in listed if line.startswith("worktree ")), None
    )
    if primary is None:
        primary = str((repo / lines[1]).resolve().parent)
    raise PreflightError(
        f"{repo} is a linked worktree: preflight tries the primary checkout's HEAD and reads "
        f"its .helmraiser/project.toml, so run `helmraiser preflight {primary} --run <cmd>` instead"
    )


def create_throwaway_worktree(repo: Path, purpose: str, run_id: str) -> tuple[Path, str]:
    """Create a detached worktree at HEAD and return its path and commit."""
    head_result = _git(repo, "rev-parse", "HEAD")
    if head_result.returncode != 0:
        detail = (head_result.stderr or head_result.stdout).strip()
        raise PreflightError(f"git rev-parse HEAD failed: {detail}")
    head = head_result.stdout.strip()
    worktree = worktree_manager(repo).path_for(purpose, run_id)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    added = _git(repo, "worktree", "add", "--detach", str(worktree), head)
    if added.returncode != 0:
        detail = (added.stderr or added.stdout).strip()
        raise PreflightError(f"worktree: {detail}")
    return worktree, head


def remove_throwaway_worktree(repo: Path, worktree: Path) -> str | None:
    """``git worktree remove --force``; else delete it and prune. The reason on failure."""
    if _git(repo, "worktree", "remove", "--force", str(worktree)).returncode == 0:
        return None
    shutil.rmtree(worktree, ignore_errors=True)
    _git(repo, "worktree", "prune")
    return None if not worktree.exists() else f"could not remove the worktree {worktree}"


def run_preflight_steps(
    repo: Path,
    steps: Sequence[VerifyStep],
    *,
    config: dict[str, Any],
    data_dir: Path,
    tail: int = 40,
    progress: Callable[[str], None] = lambda _message: None,
) -> dict[str, Any]:
    """Run named verify ``steps`` in a throwaway worktree, as verify would."""
    top = _git(repo, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise PreflightError(f"{repo} is not a git repository")
    repo = Path(top.stdout.strip())
    try:
        defaults = load_project_defaults(repo)
    except ValueError as exc:
        raise PreflightError(str(exc)) from exc
    isolation = ProcessIsolation(config, data_dir=data_dir)
    isolation.set_project_deny_read(defaults.deny_read, repo)
    reason = isolation.check_available()
    if reason is not None:
        raise PreflightError(f"process isolation is unavailable: {reason}")
    run_id = resolve_run_id(None)
    configured_tmpdir = config.get("isolation", {}).get("tmpdir")
    temp_root = (
        Path(configured_tmpdir).expanduser() if configured_tmpdir else Path(tempfile.gettempdir())
    )
    rundir = RunDir(run_id, path=temp_root / f"helmraiser-preflight-{run_id}")
    worktree = worktree_manager(repo).path_for("preflight", run_id)
    head = ""
    warnings = tracked_denial_warnings(tracked_denials(repo, isolation.configured_deny_read()))
    payload: dict[str, Any] = {
        "repo": str(repo),
        "head": head,
        "run_id": run_id,
        "worktree": str(worktree),
        "setup": None,
        "services": [],
        "commands": [],
        "warnings": warnings,
        "error": None,
        "ok": False,
    }
    running: list[RunningService] = []
    worktree_created = False
    try:
        try:
            worktree, head = create_throwaway_worktree(repo, "preflight", run_id)
        except PreflightError as exc:
            payload["error"] = str(exc)
            return payload
        payload["worktree"] = str(worktree)
        payload["head"] = head
        worktree_created = True
        rundir.evidence.mkdir(parents=True, exist_ok=True)
        helmraiser = helmraiser_env(run_id, worktree, rundir.evidence, head)
        env = command_env(helmraiser, defaults.env)
        if defaults.setup:
            progress(f"setup: {defaults.setup}")
            setup_timeout = int(config.get("setup_timeout", DEFAULT_SETUP_TIMEOUT))
            setup_sandbox = "host" if defaults.setup_config.sandbox == "host" else "srt"
            if setup_sandbox == "srt":
                setup_env, write, cache = prepare_setup(repo, data_dir, defaults.setup_config, env)

                def wrap_setup(argv: list[str]) -> list[str]:
                    return isolation.wrap_setup(
                        argv,
                        worktree,
                        rundir.path,
                        write,
                        defaults.setup_config.allowed_domains,
                        setup_cache=cache,
                    )

                res = run_shell(defaults.setup, worktree, setup_timeout, setup_env, wrap_setup)
            else:
                res = run_shell(defaults.setup, worktree, setup_timeout, env)
            output = res.output
            if res.timed_out:
                output += f"\n[helmraiser] setup timed out after {setup_timeout}s"
            payload["setup"] = {
                "run": defaults.setup,
                "ok": res.exit_code == 0,
                "exit_code": res.exit_code,
                "seconds": res.seconds,
                "tail": tail_lines(output, tail),
                "sandbox": setup_sandbox,
            }
            if res.exit_code != 0:
                if (
                    setup_sandbox == "srt"
                    and not res.timed_out
                    and setup_readonly_line(res.output) is not None
                ):
                    payload["setup"]["hints"] = [SETUP_READONLY_HINT]
                payload["error"] = "setup: " + (
                    "timed out" if res.timed_out else f"exit {res.exit_code}"
                )
                return payload
        if defaults.services:
            progress("services: " + ", ".join(spec.name for spec in defaults.services))
            try:
                start_services(run_id, defaults.services, helmraiser, lambda *_: None, running)
            except ServiceError as exc:
                payload["error"] = str(exc)
                return payload
            finally:
                payload["services"] = [service.as_dict() for service in running]
        wrap = command_wrap(isolation, worktree, rundir.path, "verify", running)
        verify_env = step_env(env, running)
        for step in steps:
            progress(f"$ {step.run}")
            result = run_step(step, worktree, run_id, FULL_OUTPUT_LINES, verify_env, wrap)
            payload["commands"].append(
                {
                    "name": step.name,
                    "command": step.run,
                    "exit_code": result.exit_code,
                    "passed": result.passed,
                    **({"red_on_base": True} if step.red_on_base else {}),
                    "seconds": result.seconds,
                    "sandbox": result.sandbox,
                    "output": result.tail,
                    "tail": tail_lines(result.tail, tail),
                    "hints": sandbox_hints(result.tail, [s.host_port for s in running]),
                }
            )
        payload["ok"] = all(item["passed"] for item in payload["commands"])
        return payload
    finally:
        if defaults.services:
            try:
                _removed, error = stop_services(run_id)
            except Exception as exc:  # cleanup must not mask the result
                error = f"{type(exc).__name__}: {exc}"
            if error is not None:
                warnings.append(
                    f"services: cleanup failed ({error}); `helmraiser work` removes them later"
                )
        if worktree_created or worktree.exists():
            error = remove_throwaway_worktree(repo, worktree)
            if error is not None:
                warnings.append(error)
        shutil.rmtree(rundir.path, ignore_errors=True)


def run_preflight(
    repo: Path,
    commands: list[str],
    *,
    config: dict[str, Any],
    data_dir: Path,
    timeout: int = 600,
    tail: int = 40,
    progress: Callable[[str], None] = lambda _message: None,
) -> dict[str, Any]:
    """Compatibility wrapper for command strings used by ``helmraiser preflight``."""
    require_primary_checkout(repo)
    steps = [VerifyStep(f"command-{n}", command, timeout) for n, command in enumerate(commands, 1)]
    payload = run_preflight_steps(
        repo, steps, config=config, data_dir=data_dir, tail=tail, progress=progress
    )
    for result in payload["commands"]:
        result.pop("output", None)
        result.pop("name", None)
    return payload
