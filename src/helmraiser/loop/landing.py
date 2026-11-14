"""Bounded git/GitHub operations for landing a pull-request run into its own base."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from helmraiser.code_snapshot import without_snapshot
from helmraiser.logbook import check_hold
from helmraiser.pipeline.worktree import WorktreeManager

COMMAND_TIMEOUT = 60.0
NO_CHECKS_GRACE = 60.0
CONFIRM_ATTEMPTS = 10
CONFIRM_INTERVAL = 3.0


@dataclass(frozen=True)
class LandingResult:
    payload: dict[str, Any]
    exit_code: int


def land_run(
    row: dict[str, Any],
    project: Path,
    manager: WorktreeManager,
    approve: Callable[[], tuple[str | None, str]],
    *,
    timeout: float = 1800,
    interval: float = 10,
    no_ci: bool = False,
    progress: Callable[[dict[str, str]], None] | None = None,
) -> LandingResult:
    """Land an eligible PR run, stopping at the first failure without rolling back approval."""
    steps: list[dict[str, str]] = []
    branch = str(row.get("branch_name") or "")
    base = str(row.get("base_branch") or "")
    run_id = str(row["run_id"])
    pr_url = str(row.get("pr_url") or "")
    head = str(row.get("head_commit") or "")
    local_status = "skipped"

    def record(name: str, status: str, detail: str) -> None:
        step = {"name": name, "status": status, "detail": detail}
        steps.append(step)
        if progress is not None:
            progress(step)

    def success() -> LandingResult:
        return LandingResult(
            {
                "landed": True,
                "run_id": run_id,
                "task_id": row["task_id"],
                "pr_url": pr_url,
                "base_branch": base,
                "branch_name": branch,
                "head": head,
                "local": local_status,
                "steps": steps,
            },
            0,
        )

    current = "preflight"
    try:
        preflight(project, branch, base)
        record(current, "ok", f"origin/{base}")
        current = "logbook"
        warnings = []
        for blocks in ("push", "merge"):
            check = check_hold(project, blocks=blocks)
            if check.status == "blocked":
                detail = check.full_text()
                record(current, "blocked", detail)
                holds = [
                    {key: hold.get(key) for key in ("id", "title", "release", "until")}
                    for hold in check.holds
                ]
                return LandingResult(
                    {"landed": False, "error": detail, "steps": steps, "holds": holds}, 3
                )
            if check.status == "unavailable":
                warnings.append(f"{blocks}: {check.detail}")
        record(current, "warn" if warnings else "ok", "; ".join(warnings) or "push and merge clear")
        current = "approve"
        if row["status"] == "landed":
            state = pr_state(project, pr_url)
            if state == "CLOSED":
                raise RuntimeError("pull request closed without merging")
            if state == "MERGED" and not head:
                head = _checked(
                    ["git", "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"], project
                )
            record(current, "skipped", "already approved")
            if state == "MERGED":
                for name in ("ci", "sync", "push", "local", "confirm"):
                    record(name, "skipped", "pull request already MERGED")
                return success()
        else:
            url, head = approve()
            if not url:
                raise RuntimeError("approval returned no pull request URL")
            pr_url = url
            action = "updated" if row.get("pr_url") else "opened"
            record(current, "ok", f"{action} {pr_url}")
        current = "ci"
        detail = wait_for_checks(
            project,
            pr_url,
            run_id,
            timeout=timeout,
            interval=interval,
            no_ci=no_ci,
            expected_head=head or None,
        )
        record(current, "ok", detail)
        current = "sync"
        head = sync_head(project, branch, base, run_id)
        record(current, "ok", f"origin/{base} is an ancestor; PR branch matches {head}")
        current = "push"
        push_base(project, base, head)
        record(current, "ok", f"pushed {head} to origin/{base}")
        current = "local"
        try:
            local = manager.fast_forward_base(base, head)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            local = {"status": "failed", "reason": "git_failed", "detail": str(exc)}
        local_status = local["status"]
        detail = local["reason"]
        if local_status != "updated":
            detail += f"; {local.get('detail', '')}; in a clean checkout of {base}, run git merge --ff-only {head}"
        record(current, "ok" if local_status == "updated" else "warn", detail)
        current = "confirm"
        confirm_merged(project, pr_url)
        record(current, "ok", "pull request MERGED")
        return success()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        record(current, "failed", str(exc))
        return LandingResult({"landed": False, "error": str(exc), "steps": steps}, 1)


def _run(
    argv: list[str], cwd: Path, *, timeout: float = COMMAND_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=timeout,
            env={**without_snapshot(os.environ), "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"{' '.join(argv)} failed: {exc}") from exc


def _checked(argv: list[str], cwd: Path) -> str:
    result = _run(argv, cwd)
    if result.returncode:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
            or f"{' '.join(argv)} exited {result.returncode}"
        )
    return result.stdout.strip()


def push_branch(worktree: Path, branch: str, remote: str = "origin") -> None:
    """Bounded approval push to the same branch, ignoring configured push mappings."""
    ref = f"refs/heads/{branch}"
    _checked(["git", "push", "--no-follow-tags", "-u", remote, f"{ref}:{ref}"], worktree)


def open_pull_request(cwd: Path, *, base: str, head: str, title: str, body: str) -> str:
    """Create the approval's PR with a bounded gh invocation."""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md") as body_file:
        body_file.write(body)
        body_file.flush()
        output = _checked(
            [
                "gh",
                "pr",
                "create",
                "--base",
                base,
                "--head",
                head,
                "--title",
                title,
                "--body-file",
                body_file.name,
            ],
            cwd,
        )
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("gh pr create returned no pull request URL")
    return lines[-1]


def preflight(project: Path, branch: str, base: str) -> None:
    """Read-only admission checks; no fetch, approval or ref update."""
    if not branch or not base:
        raise RuntimeError("run requires branch_name and base_branch")
    if shutil.which("gh") is None:
        raise RuntimeError("gh not found on PATH")
    _checked(["git", "check-ref-format", f"refs/heads/{branch}"], project)
    _checked(["git", "check-ref-format", f"refs/heads/{base}"], project)
    remotes = _checked(["git", "remote"], project).splitlines()
    if "origin" not in remotes:
        raise RuntimeError("primary checkout has no origin remote")
    values = []
    for key in ("remote", "merge"):
        result = _run(["git", "config", "--get-all", f"branch.{base}.{key}"], project)
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr.strip() or "cannot read base upstream")
        values.append(result.stdout.splitlines())
    remote, sources = values
    if (remote or sources) and (remote != ["origin"] or sources != [f"refs/heads/{base}"]):
        raise RuntimeError(f"unsupported upstream for {base}; land requires origin/{base}")


def sync_head(project: Path, branch: str, base: str, run_id: str) -> str:
    """Fetch the base and require ancestry plus exact local/remote PR head equality."""
    _checked(
        [
            "git",
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "origin",
            f"+refs/heads/{base}:refs/remotes/origin/{base}",
        ],
        project,
    )
    head = _checked(["git", "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"], project)
    result = _run(
        ["git", "merge-base", "--is-ancestor", f"refs/remotes/origin/{base}", head], project
    )
    if result.returncode == 1:
        raise RuntimeError(
            f"origin/{base} is not an ancestor of {branch}; update local {base} from "
            f"origin if it is behind, then helmraiser rework {run_id} --rebase"
        )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "cannot check origin base ancestry")
    ref = f"refs/heads/{branch}"
    output = _checked(["git", "ls-remote", "--heads", "origin", ref], project)
    remote_head = next(
        (line.split()[0] for line in output.splitlines() if line.split()[1:] == [ref]), "missing"
    )
    if remote_head != head:
        raise RuntimeError(f"origin/{branch} head {remote_head} differs from local head {head}")
    return head


def push_base(project: Path, base: str, head: str) -> None:
    """Non-force push of exactly the checked head to exactly the run's origin base."""
    _checked(["git", "push", "--no-follow-tags", "origin", f"{head}:refs/heads/{base}"], project)


def wait_for_checks(
    project: Path,
    pr_url: str,
    run_id: str,
    *,
    timeout: float = 1800,
    interval: float = 10,
    no_ci: bool = False,
    expected_head: str | None = None,
) -> str:
    """Wait for the expected PR head and its CI within one shared timeout."""
    deadline = monotonic() + timeout
    no_checks_since: float | None = None
    waiting_for_head = expected_head is not None
    while True:
        waiting = (
            f"pull request head has not reached {expected_head}"
            if waiting_for_head
            else "checks still pending"
        )
        timeout_text = f"{waiting} after {timeout:g} s; run helmraiser land {run_id} again"
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RuntimeError(timeout_text)
        try:
            argv = (
                ["gh", "pr", "view", pr_url, "--json", "headRefOid"]
                if waiting_for_head
                else ["gh", "pr", "checks", pr_url]
            )
            result = _run(argv, project, timeout=min(COMMAND_TIMEOUT, remaining))
        except RuntimeError:
            if monotonic() >= deadline:
                raise RuntimeError(timeout_text) from None
            raise
        now = monotonic()
        if now >= deadline:
            raise RuntimeError(timeout_text)
        output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        if waiting_for_head:
            if result.returncode:
                raise RuntimeError(output or f"gh pr view exited {result.returncode}")
            try:
                parsed = json.loads(result.stdout)
            except ValueError as exc:
                raise RuntimeError(f"invalid JSON from gh pr view: {output}") from exc
            observed = parsed.get("headRefOid") if isinstance(parsed, dict) else None
            if not isinstance(observed, str) or not observed:
                raise RuntimeError(f"unexpected pull request head: {observed}")
            if observed == expected_head:
                waiting_for_head = False
            else:
                sleep(max(0, min(interval, deadline - now)))
            continue
        if "no checks reported" in output.lower():
            if no_ci:
                return "no checks reported (--no-ci)"
            if no_checks_since is None:
                no_checks_since = now
            if now - no_checks_since >= NO_CHECKS_GRACE:
                raise RuntimeError(
                    "no checks reported after the grace period; use --no-ci only for a repository without CI"
                )
        else:
            no_checks_since = None
            if result.returncode == 0:
                return output or "all checks passed"
            if result.returncode != 8:
                raise RuntimeError(output or f"gh pr checks exited {result.returncode}")
        delay = min(interval, deadline - now)
        if no_checks_since is not None:
            delay = min(delay, NO_CHECKS_GRACE - (now - no_checks_since))
        sleep(max(0, delay))


def confirm_merged(project: Path, pr_url: str) -> None:
    """Allow GitHub a short bounded delay to recognize the non-force base push."""
    for attempt in range(CONFIRM_ATTEMPTS):
        state = pr_state(project, pr_url)
        if state == "MERGED":
            return
        if state == "CLOSED":
            raise RuntimeError("pull request closed without merging")
        if attempt + 1 < CONFIRM_ATTEMPTS:
            sleep(CONFIRM_INTERVAL)
    raise RuntimeError("pushed, but the pull request still shows OPEN")


def pr_state(project: Path, pr_url: str) -> str:
    """Read and validate GitHub's PR state without jq or a merge mutation."""
    output = _checked(["gh", "pr", "view", pr_url, "--json", "state"], project)
    try:
        parsed = json.loads(output)
        state = parsed.get("state") if isinstance(parsed, dict) else None
    except ValueError as exc:
        raise RuntimeError(f"invalid JSON from gh pr view: {output}") from exc
    if state not in ("OPEN", "CLOSED", "MERGED"):
        raise RuntimeError(f"unexpected pull request state: {state}")
    return str(state)
