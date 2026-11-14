"""Implement-stage prompts (spec §4.3): initial, feedback and escalation."""

from __future__ import annotations

from typing import Any

from helmraiser.loop.verify import VerifyResult
from helmraiser.task.model import TaskSpec

HISTORY_LINES_PER_ITERATION = 20
REBASE_NOTE = "Rebase the run branch onto the current base branch."


def paths_block(task: TaskSpec) -> str:
    if task.paths is None:
        return ""
    return (
        "## Allowed paths\n\n"
        + "\n".join(f"- `{pattern}`" for pattern in task.paths)
        + "\n\nChanges outside these globs fail before verify. Restore/remove forbidden "
        "changes; do not broaden the declaration.\n\n"
    )


def acceptance_block(task: TaskSpec) -> str:
    lines = ["## Acceptance", "", "Verify (all must exit 0; Helmraiser runs them after you stop):"]
    for step in task.verify:
        lines.append(f"- {step.name}: `{step.run}`")
    if task.evidence:
        lines += ["", "Evidence (collected by Helmraiser after verify passes):"]
        for evidence in task.evidence:
            lines.append(f"- {evidence.name}: `{evidence.run}`")
    return "\n".join(lines)


def initial_prompt(task: TaskSpec, plan: str | None = None, approved: bool = True) -> str:
    prompt = f"{paths_block(task)}{task.body.strip()}\n\n{acceptance_block(task)}"
    if plan:
        if approved:
            heading = "## Approved plan"
            lead_in = "The owner approved this plan; follow it and say so if you must deviate."
        else:
            heading = "## Plan"
            lead_in = (
                "A planning model wrote this plan for the task; follow it and say so if you "
                "must deviate."
            )
        prompt += f"\n\n{heading}\n\n{lead_in}\n\n{plan.strip()}"
    return f"{prompt}\n\nBegin."


def plan_prompt(task: TaskSpec, *, wait_for_approval: bool) -> str:
    if task.paths is None:
        next_step = (
            "Helmraiser saves it and waits for approval. Include any open questions for the owner."
            if wait_for_approval
            else "The implementer starts from the plan straight away. Resolve open questions "
            "yourself; answer `BLOCKED:` with the reason only for blockers."
        )
    else:
        next_step = (
            "Helmraiser saves it and waits for approval. Include any open questions for the owner; "
            "list needed files outside the allowed paths under the plan's open questions."
            if wait_for_approval
            else "The implementer starts from the plan straight away. If the task needs a file "
            "outside the allowed paths, start the final message with one line "
            "`SCOPE: <path>[, <path>…] — <reason>`, followed by the full plan, written as if "
            "the paths were granted. Resolve other open questions yourself; answer `BLOCKED:` "
            "with the reason only for other blockers."
        )
    return (
        f"{paths_block(task)}{task.body.strip()}\n\n{acceptance_block(task)}\n\n"
        "Write an implementation plan for a less capable implementer. For every small step, "
        "name each file and function to change with line references, state the exact change "
        "and what must not change, name the one test file to run, and require a commit before "
        "the next step. Explain how every verify step will pass. Answer with the plan itself "
        f"(markdown) as your final message. {next_step} Do not implement anything."
    )


def feedback_prompt(results: list[VerifyResult]) -> str:
    heading = (
        "Task path scope failed; acceptance commands were skipped:"
        if any(r.name == "task-paths" and not r.passed for r in results)
        else "Verify failed. Helmraiser ran the acceptance commands; these did not pass:"
    )
    parts = [heading, ""]
    for result in results:
        if result.passed:
            continue
        parts.append(f"### {result.name} (exit {result.exit_code})")
        parts.append("```")
        parts.append(result.tail or "(no output)")
        parts.append("```")
        if result.log:
            parts.append(
                f"The full output of this step is in `{result.log}`. The excerpt above keeps "
                "the test failures found anywhere in it, the totals and the tail; spans "
                "marked `… omitted …` are only in the file. Read it before deciding what failed."
            )
        parts.append("")
    hints = [hint for r in results if not r.passed for hint in r.hints]
    if hints:
        parts += [
            "## Environment hints",
            "",
            "Helmraiser recognised known sandbox or environment signatures in this output. "
            "Code cannot supply a missing service or permission: apply a hint's own advice "
            "where it gives some, and if verify cannot pass without a change to the "
            "environment, stop and answer `BLOCKED:` with the reason instead of spending "
            "iterations on it.",
            "",
            *(f"- {hint}" for hint in dict.fromkeys(hints)),
            "",
        ]
    parts.append("Fix these and run the verify commands again before stopping.")
    return "\n".join(parts)


def retry_implement_prompt(results: list[VerifyResult], reason: str) -> str:
    """The first prompt of a run handed back by ``helmraiser retry --implement``. Only a
    budget failure comes back with no failed verify result: the work was cut short."""
    intro = (
        f"The owner handed this run back to you after it ended failed ({reason}). The task "
        "file may have changed since: read it again. The branch keeps your earlier work"
    )
    if any(not result.passed for result in results):
        return f"{intro}; these are the last verify results.\n\n" + feedback_prompt(results)
    return (
        f"{intro}, all of it committed. Compare it with the task (`git log`, `git diff`), "
        "finish what is still missing, and run the verify commands before stopping. If "
        "nothing is missing, stop without changes: Helmraiser verifies the branch as it stands."
    )


def retry_restart_prompt(
    task: TaskSpec, reason: str, plan: str | None = None, approved: bool = True
) -> str:
    """The first prompt when retry restarts a run with no committed work."""
    return (
        f"The owner handed this run back to you after it ended failed ({reason}). "
        "Nothing is committed on the branch. The task file may have changed since: read it "
        "again. Start the task from the beginning.\n\n"
        + initial_prompt(task, plan, approved=approved)
    )


def scope_approved_prompt(paths: list[str], note: str | None) -> str:
    """The first prompt of a run resumed by ``helmraiser approve`` on a scope request."""
    text = (
        f"The owner approved these paths: {', '.join(f'`{p}`' for p in paths)}. "
        "Continue the task."
    )
    return text + f"\n\n## Owner's note on approval\n\n{note}" if note else text


def scope_declined_prompt(paths: list[str], note: str) -> str:
    """The first implement prompt after the owner declines a scope request."""
    return (
        f"Scope request declined ({', '.join(paths)}): {note}\n\n"
        "The task's paths: is unchanged. Do not edit the declined files. Find another way "
        "inside the allowed paths; if that is impossible, answer with a SCOPE: or BLOCKED: "
        "line as usual. Read the task file again because it may have changed."
    )


def rebase_instructions(base_branch: str) -> str:
    return (
        "## Rebase request\n\n"
        f"{base_branch} has moved on since this branch was cut, so landing the run failed "
        "or would fail. Rebase before doing anything else.\n\n"
        f"Run `git rebase {base_branch}` in this worktree.\n\n"
        "Resolve every conflicted file keeping the intent of both sides: the task's change "
        f"and what {base_branch} gained. Never take one side wholesale or drop the task's "
        "change. Run `git add` and `git rebase --continue` until the rebase finishes.\n\n"
        "No `git merge`, no squashing or rewording the branch's commits.\n\n"
        "Never leave a rebase in progress. If it cannot be resolved, run `git rebase --abort` "
        "and answer with a first line `BLOCKED: <reason>`.\n\n"
        "After the rebase, run the verify commands again and commit any further fix."
    )


def rework_prompt(
    task: TaskSpec,
    note: str,
    findings: list[dict[str, Any]],
    *,
    rebase_onto: str | None = None,
    published_pr: bool = False,
) -> str:
    """Owner feedback first, after any base update; scope and acceptance remain in force."""
    prefix = rebase_instructions(rebase_onto) + "\n\n" if rebase_onto is not None else ""
    if published_pr:
        prefix = (
            "## Published pull request\n\n"
            "Do not rewrite published commits or force push. Preserve fast-forward updates "
            "to the existing PR.\n\n"
        )
        if rebase_onto is not None:
            prefix += (
                f"Merge the named local base with `git merge --no-edit {rebase_onto}`. "
                "Resolve conflicts preserving both sides' intent, stage resolutions and "
                "complete the merge with `git commit --no-edit`. Never leave a merge or "
                "rebase unfinished. If resolution is impossible, abort the operation and "
                "answer with a first line `BLOCKED: <reason>`. Re-run the normal gates.\n\n"
            )
    return prefix + (
        f"## Approver's rework note\n\n{note}\n\n"
        + review_feedback_prompt(findings, rework=True)
        + "\n\nThe branch retains your earlier work. Do not broaden scope or acceptance.\n\n"
        + paths_block(task)
        + task.body.strip()
        + "\n\n"
        + acceptance_block(task)
        + "\n\nBegin."
    )


NO_CHANGES_PROMPT = (
    "No changes were made: the worktree has the same HEAD and a clean status as before "
    "your last turn. Implement the task (edit files, commit) and run the verify commands "
    "before stopping."
)


def retry_prompt(error_message: str | None) -> str:
    detail = f" ({error_message.strip()})" if error_message else ""
    return (
        f"Your previous turn ended with an error{detail}. Continue the task from the current "
        "state of the worktree and run the verify commands before stopping."
    )


def history_summary(iterations: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in iterations:
        results = item.get("verify_results") or []
        header = f"Iteration {item['n']} ({item['model']}):"
        if item.get("agent_ok") is False:
            header += " agent session failed."
        elif item.get("verify_passed") is None:
            header += " no verify run (no changes made)."
        else:
            failed = [r for r in results if not r.get("passed")]
            header += f" {len(results) - len(failed)}/{len(results)} verify steps passed."
        block = [header]
        scope_blocks: list[str] = []
        for r in results:
            if r.get("passed"):
                continue
            tail = str(r.get("tail") or "").splitlines()
            block.append(f"  - {r['name']} (exit {r.get('exit_code')}):")
            if r["name"] == "task-paths":
                scope_blocks += [f"      {t}" for t in tail]
            else:
                block += [f"      {t}" for t in tail[-6:]]
        lines += block[:HISTORY_LINES_PER_ITERATION] + scope_blocks
    return "\n".join(lines) if lines else "(no iteration history)"


def review_feedback_prompt(findings: list[dict[str, Any]], *, rework: bool = False) -> str:
    """Ask an implementer to address the actionable part of a review."""
    severe = [finding for finding in findings if finding.get("severity") == "severe"]
    minor = [finding for finding in findings if finding.get("severity") != "severe"]
    lines = [
        (
            "Address every review finding below, add regression tests where feasible, "
            "stay within the task's scope, and commit the fix."
            if rework
            else "The independent review found severe issues. Fix every severe finding below, add a "
            "regression test for each where feasible, stay within the task's scope, and commit the fix."
        ),
        "",
        "## Severe findings",
    ]
    if rework and not findings:
        lines.append("No reviewer findings were recorded; address the approver's note.")
    for finding in severe:
        lines.append(
            f"- {finding.get('file', '?')}:{finding.get('line', '?')}: "
            f"{finding.get('note', '')}"
        )
    if minor:
        lines += ["", "## Minor findings" if rework else "## Minor findings (optional)"]
        for finding in minor:
            lines.append(
                f"- {finding.get('file', '?')}:{finding.get('line', '?')}: "
                f"{finding.get('note', '')}"
            )
    return "\n".join(lines)


def escalation_prompt(task: TaskSpec, iterations: list[dict[str, Any]], base_commit: str) -> str:
    return (
        f"{paths_block(task)}{task.body.strip()}\n\n{acceptance_block(task)}\n\n"
        "## What was tried so far\n\n"
        f"{history_summary(iterations)}\n\n"
        "The previous model could not make verify pass. Start by reading the current diff "
        f"(`git diff {base_commit}`)."
    )
