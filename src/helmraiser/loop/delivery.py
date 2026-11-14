"""Push run branches and open pull requests for remote delivery."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from helmraiser.task.model import TaskSpec

MAX_BODY_LINES = 99


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RuntimeError(str(exc)) from exc


def push_branch(worktree: Path, branch: str, remote: str = "origin") -> None:
    """Push ``branch`` from its run worktree and configure its upstream."""
    result = _run(["git", "push", "-u", remote, branch], worktree)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def open_pull_request(cwd: Path, *, base: str, head: str, title: str, body: str) -> str:
    """Create a pull request with ``gh`` and return the URL it prints."""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md") as body_file:
        body_file.write(body)
        body_file.flush()
        result = _run(
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
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("gh pr create returned no pull request URL")
    return lines[-1]


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _shift_headings(body: str) -> list[str]:
    """Task body lines with every Markdown heading demoted one level (outside fences)."""
    shifted: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        elif not in_fence:
            line = re.sub(r"^(#{1,5})(\s)", r"#\1\2", line)
        shifted.append(line)
    return shifted


def pr_body(
    task: TaskSpec,
    run_id: str,
    verdict: Any,
    verify_results: list[Any],
    evidence_results: list[Any],
    cost_usd: float,
) -> str:
    """Render a concise (<100 line) governed-run summary for a pull request."""
    verdict_name = _value(verdict, "verdict", verdict or "-")
    forced = _value(verdict, "forced", None)
    verdict_line = f"- Verdict: {verdict_name}"
    if forced:
        verdict_line += f" (forced: {forced})"
    checks = {
        str(_value(check, "name")): check
        for check in (_value(verdict, "evidence_checks", []) or [])
        if isinstance(check, dict)
    }
    lines = [
        "## Helmraiser run",
        "",
        f"- Task: `{task.id}` — {task.title}",
        f"- Run: `{run_id}`",
        verdict_line,
        f"- Cost: ${cost_usd:.4f}",
        "",
        "## Findings",
    ]
    findings = _value(verdict, "findings", []) or []
    if findings:
        for finding in findings:
            severity = _value(finding, "severity", "?")
            location = _value(finding, "file", "?")
            line = _value(finding, "line", 0)
            note = _value(finding, "note", "")
            lines.append(f"- [{severity}] {location}:{line} — {note}")
    else:
        lines.append("- None")

    lines += ["", "## Verify"]
    for result in verify_results:
        passed = bool(_value(result, "passed", False))
        status = "pass" if passed else f"FAIL (exit {_value(result, 'exit_code', '?')})"
        lines.append(f"- {_value(result, 'name', 'verify')}: {status}")
    if not verify_results:
        lines.append("- No verify results")

    lines += ["", "## Evidence"]
    for result in evidence_results:
        name = str(_value(result, "name", "evidence"))
        status = "ok" if bool(_value(result, "ok", False)) else "NOT OK"
        check = checks.get(name)
        if check is None:
            suffix = ""
        else:
            meets = _value(check, "meets_criteria", None)
            if meets is None:
                suffix = " — reviewer: not read"
            elif meets:
                suffix = ""
            else:
                note = _value(check, "note", "")
                suffix = f" — reviewer: not met: {note}" if note else " — reviewer: not met"
        lines.append(f"- {name}: {status}{suffix}")
    if not evidence_results:
        lines.append("- No evidence results")

    lines += ["", "## Task", ""]
    lines.extend(_shift_headings(task.body.strip()))
    if len(lines) > MAX_BODY_LINES:
        lines = lines[: MAX_BODY_LINES - 1] + ["…"]
    return "\n".join(lines).rstrip() + "\n"
