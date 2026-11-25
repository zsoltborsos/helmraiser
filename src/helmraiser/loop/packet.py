"""A run's diff, and the pick packet: the one file a judge reads to pick between the
two runs of a pair.

The packet names the runs X and Y and holds the task's brief and, per run, the
outcome, cost, verify and evidence results, Helmraiser's review and the diff. It
holds no model, skill, arm, run ID, session, branch or worktree name, and no output
tail of a verify or evidence step: those can carry paths and model names.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from helmraiser.code_snapshot import without_snapshot
from helmraiser.ledger.db import Ledger
from helmraiser.ledger.rundir import RunDir
from helmraiser.loop.blinding import Blind
from helmraiser.loop.budget import completed_active_minutes
from helmraiser.loop.pairs import LABELS, Arm
from helmraiser.utils.markdown import parse_frontmatter

_JSON_BLOCK = re.compile(r"^```json[ \t]*\r?\n.*?^```[ \t]*$", re.M | re.S)
_BACKTICKS = re.compile(r"`{3,}")


class DiffUnavailable(ValueError):
    """The run has no diff to show; the message says why."""


def run_diff(run: dict[str, Any], data_dir: Path) -> tuple[str, str]:
    """The run's diff against its base commit, and where it is from: ``patch``, the
    ``diff.patch`` that the run wrote before its review, or ``git``, a
    ``git diff base_commit..head`` in the project when there is no such file. The
    head is the run's recorded head commit, or its branch while it has none."""
    run_id = str(run["run_id"])
    patch = RunDir(run_id, data_dir).diff_patch
    if patch.is_file():
        return patch.read_text(encoding="utf-8", errors="replace"), "patch"
    base = run["base_commit"]
    head = run["head_commit"] or (
        f"refs/heads/{run['branch_name']}" if run["branch_name"] else None
    )
    if not base or not head:
        raise DiffUnavailable(f"run {run_id} has no base commit or branch yet")
    try:
        result = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--no-textconv", f"{base}..{head}"],
            cwd=str(run["project"]),
            capture_output=True,
            check=False,
            timeout=60,
            env=without_snapshot(os.environ),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DiffUnavailable(f"git diff failed for run {run_id}: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise DiffUnavailable(f"git diff failed for run {run_id}: {detail}")
    return result.stdout.decode("utf-8", errors="replace"), "git"


def _replacer(replacements: dict[str, str]) -> Callable[[str], str]:
    """Replace each key by its value, the longest key first."""
    keys = sorted((key for key in replacements if key), key=len, reverse=True)
    if not keys:
        return lambda text: text
    pattern = re.compile("|".join(re.escape(key) for key in keys))
    return lambda text: pattern.sub(lambda match: replacements[match.group(0)], text)


def _fenced(text: str, language: str = "") -> list[str]:
    """``text`` in a fence that is longer than any run of backticks inside it."""
    longest = max((len(run) for run in _BACKTICKS.findall(text)), default=2)
    fence = "`" * (longest + 1)
    return [fence + language, text.rstrip("\n"), fence]


def build_packet(
    ledger: Ledger, pair: dict[str, Any], states: dict[str, Arm], data_dir: Path
) -> str:
    """The pick packet of a pair whose two arms are settled, as Markdown."""
    task = ledger.get_task(str(pair["task_id"]))
    parsed: dict[str, Any] = parse_frontmatter(str(task["snapshot"])) if task else {}
    steps = (parsed.get("frontmatter") or {}).get("evidence")
    criteria = {
        str(step.get("name")): " ".join(str(step.get("criteria") or "").split())
        for step in (steps if isinstance(steps, list) else [])
        if isinstance(step, dict)
    }
    # What identifies a run: replaced wherever it appears, in the diffs too.
    identity: dict[str, str] = {str(data_dir): "<data-dir>", str(data_dir.resolve()): "<data-dir>"}
    for arm in states.values():
        run = arm.run
        identity[arm.run_id] = "<run>"
        identity[arm.run_id[:8]] = "<run>"
        if run["branch_name"]:
            identity[str(run["branch_name"])] = "<branch>"
        if run["worktree_path"]:
            identity[str(run["worktree_path"])] = "<worktree>"
            identity[Path(str(run["worktree_path"])).name] = "<worktree>"
    anonymous = _replacer(identity)
    # In what Helmraiser and its reviewer wrote about a run, paths lose their roots
    # and model names are hidden as well.
    roots = {**identity, str(Path.home()): "~"}
    if task:
        roots[str(task["project"])] = "<project>"
    for arm in states.values():
        if arm.run["worktree_path"]:
            roots[str(arm.run["worktree_path"]) + "/"] = ""
    unrooted = _replacer(roots)
    blind = Blind(ledger, pair)

    def clean(value: Any) -> str:
        return unrooted(blind.text(str(value)) or "")

    lines = [
        f"# Pick packet: {task['title'] if task else 'task'}",
        "",
        "Two runs of the task below, X and Y, were made independently from the same base "
        "commit. Everything that says how either was made is left out.",
        "",
        "## The task",
        "",
        str(parsed.get("body") or "").strip(),
        "",
    ]
    for label in LABELS:
        lines += _arm_section(ledger, states[label], data_dir, criteria, clean, anonymous)
    return "\n".join(lines).rstrip("\n") + "\n"


def _arm_section(
    ledger: Ledger,
    arm: Arm,
    data_dir: Path,
    criteria: dict[str, str],
    clean: Callable[[Any], str],
    anonymous: Callable[[str], str],
) -> list[str]:
    run, run_id = arm.run, arm.run_id
    iterations = ledger.iterations(run_id)
    minutes = completed_active_minutes(
        ledger.events(run_id, types=("run_start", "run_resume", "run_end")),
        started_at=float(run["started_at"]),
        ended_at=run["ended_at"],
    )
    lines = [
        f"## {arm.label}",
        "",
        f"- outcome: {clean(arm.describe())}",
        f"- review verdict: {run['verdict'] or 'none'}",
        f"- cost: ${float(run['cost_usd'] or 0.0):.4f}; minutes: {minutes:.1f}; "
        f"iterations: {len(iterations)}",
        "",
        f"### Verify ({arm.label})",
        "",
    ]
    verify: list[dict[str, Any]] = next(
        (i["verify_results"] for i in reversed(iterations) if i.get("verify_results")), []
    )
    lines += [
        f"- {clean(result.get('name'))}: {'pass' if result.get('passed') else 'fail'}"
        for result in verify
    ] or ["- no verify step ran"]
    lines += ["", f"### Evidence ({arm.label})", ""]
    evidence = ledger.evidence(run_id)
    for row in evidence:
        name = str(row["name"])
        lines.append(f"- {clean(name)}: {'pass' if row['ok'] else 'fail'}")
        if criteria.get(name):
            lines.append(f"  - criteria: {clean(criteria[name])}")
    if not evidence:
        lines.append("- no evidence step ran")
    lines += ["", f"### Review ({arm.label})", ""]
    lines += _review(ledger, run_id, data_dir, clean)
    lines += ["", f"### Diff ({arm.label})", ""]
    try:
        diff = anonymous(run_diff(run, data_dir)[0])
    except DiffUnavailable:
        diff = ""
    lines += _fenced(diff, "diff") if diff.strip() else ["(no diff)"]
    lines.append("")
    return lines


def _review(ledger: Ledger, run_id: str, data_dir: Path, clean: Callable[[Any], str]) -> list[str]:
    """The reviewer's summary (its review without the verdict block) and the verdict
    that Helmraiser recorded: findings and evidence checks."""
    verdicts = ledger.events(run_id, types=("review_verdict",))
    if not verdicts:
        return ["(not reviewed)"]
    verdict = verdicts[-1]["payload"]
    lines: list[str] = []
    review_md = RunDir(run_id, data_dir).review_md
    if review_md.is_file():
        summary = _JSON_BLOCK.sub("", review_md.read_text(encoding="utf-8", errors="replace"))
        if summary.strip():
            lines += [*_fenced(clean(summary.strip()), "text"), ""]
    if verdict.get("forced"):
        lines.append(f"- verdict set by Helmraiser: {clean(verdict['forced'])}")
    if verdict.get("error"):
        lines.append(f"- verdict not readable: {clean(verdict['error'])}")
    for finding in verdict.get("findings") or []:
        where = f"{finding.get('file', '?')}:{finding.get('line', 0)}"
        lines.append(
            f"- [{clean(finding.get('severity', '?'))}] {clean(where)} — "
            f"{clean(finding.get('note', ''))}"
        )
    if not verdict.get("findings"):
        lines.append("- no findings")
    for check in verdict.get("evidence_checks") or []:
        note = f" — {clean(check['note'])}" if check.get("note") else ""
        lines.append(
            f"- evidence {clean(check.get('name', '?'))}: "
            f"meets_criteria={json.dumps(check.get('meets_criteria'))}{note}"
        )
    return lines
