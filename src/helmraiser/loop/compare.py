"""Read-only, blind comparison of two runs: selection, prompt and verdict."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterator
from typing import Any

from helmraiser.code_snapshot import without_snapshot
from helmraiser.ledger.db import PARKED_RUN_STATUSES, TERMINAL_RUN_STATUSES, Ledger
from helmraiser.ledger.scheduling import variant_of
from helmraiser.loop.model_report import run_setup, setup_label
from helmraiser.utils.markdown import parse_frontmatter

DIFF_LIMIT = 80_000
REVIEWS_LIMIT = 12_000
FINISHED = TERMINAL_RUN_STATUSES | PARKED_RUN_STATUSES
CRITERIA = ("correctness", "tests", "scope", "quality")
VERDICT_KEYS = ("winner", "confidence", "scores", "decisive", "defects", "merge_advice")


class CompareError(ValueError):
    """An unusable selection (CLI exit 2)."""


class UnknownRun(CompareError):
    """An unknown or ambiguous run reference (CLI exit 1)."""


def resolve_run(ledger: Ledger, ref: str) -> dict[str, Any]:
    exact = ledger.get_run(ref)
    if exact is not None:
        return exact
    matches = [r for r in ledger.list_runs() if r["run_id"].startswith(ref)]
    if not matches:
        raise UnknownRun("unknown run: " + ref)
    if len(matches) != 1:
        raise UnknownRun(
            "ambiguous run prefix " + ref + ": " + ", ".join(sorted(r["run_id"] for r in matches))
        )
    return matches[0]


def check_runs(a: dict[str, Any], b: dict[str, Any]) -> None:
    if a["run_id"] == b["run_id"]:
        raise CompareError("cannot compare a run with itself")
    if a["project"] != b["project"]:
        raise CompareError("runs are in different projects")
    for run in (a, b):
        if run["status"] not in FINISHED:
            raise CompareError(f"run {run['run_id'][:8]} is still {run['status']}")


def variant_runs(
    ledger: Ledger, task_id: str, project: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    tasks = [t for t in ledger.list_tasks(project=project) if variant_of(t) == task_id]
    projects = sorted({t["project"] for t in tasks})
    if project is None and len(projects) > 1:
        raise CompareError(
            task_id
            + " has variants in several projects: "
            + ", ".join(projects)
            + "; pass --project"
        )
    tasks.sort(key=lambda t: t["id"])
    if len(tasks) != 2:
        raise CompareError(
            f"expected two variants of {task_id}, found {len(tasks)}: "
            + ", ".join(t["id"] for t in tasks)
        )
    runs = ledger.list_runs()
    selected = []
    for task in tasks:
        candidates = [r for r in runs if r["task_id"] == task["task_id"]]
        if not candidates:
            raise CompareError(f"variant {task['id']} has no run")
        selected.append(max(candidates, key=lambda r: (r["started_at"], r["run_id"])))
    return selected[0], selected[1]


def _run_diff(run: dict[str, Any]) -> str:
    if not run["base_commit"] or not run["head_commit"]:
        return ""
    try:
        result = subprocess.run(
            [
                "git",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--stat",
                "--patch",
                run["base_commit"],
                run["head_commit"],
            ],
            cwd=run["project"],
            capture_output=True,
            check=False,
            timeout=60,
            env=without_snapshot(os.environ),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode:
        return ""
    diff = result.stdout.decode("utf-8", errors="replace")
    if len(diff) > DIFF_LIMIT:
        return diff[:DIFF_LIMIT] + "\n[diff cut after 80000 characters]\n"
    return diff


def run_facts(ledger: Ledger, run: dict[str, Any]) -> dict[str, Any]:
    task = ledger.get_task(run["task_id"])
    brief = parse_frontmatter(task["snapshot"])["body"] if task else ""
    reviews = [
        {
            key: event["payload"][key]
            for key in ("verdict", "findings", "summary")
            if key in event["payload"]
        }
        for event in ledger.events(run["run_id"], types=("review_verdict",))
    ]
    return {
        "run_id": run["run_id"],
        "task_id": run["task_id"],
        "project": run["project"],
        "status": run["status"],
        "failure_reason": run["failure_reason"],
        "iterations": len(ledger.iterations(run["run_id"])),
        "escalated": bool(run["escalated"]),
        "minutes": (
            round((run["ended_at"] - run["started_at"]) / 60, 1)
            if run["ended_at"] is not None
            else None
        ),
        "cost_usd": run["cost_usd"],
        "setup": run_setup(run),
        "brief": brief,
        "reviews": reviews,
        "diff": _run_diff(run),
    }


PROMPT_HEAD = """You are judging two independent implementations of the same task, A and B, made from
the same base commit by different (undisclosed) model setups. Judge only the work.

Criteria, in order: (1) correctness against the task's goal and acceptance block,
including edge cases and concurrency where relevant; (2) whether tests really prove the
behaviour (not just exist); (3) scope discipline (only what was asked, no stray edits);
(4) code quality and fit with the surrounding code; (5) risk of a subtle defect a
reviewer could miss. Do not reward length. Ties are allowed when neither is better.

Answer with ONLY a JSON object:
{"winner": "A" | "B" | "tie", "confidence": "low" | "medium" | "high",
 "scores": {"A": {"correctness": 1-5, "tests": 1-5, "scope": 1-5, "quality": 1-5},
            "B": {...}},
 "decisive": "<one sentence: what decided it>",
 "defects": {"A": ["..."], "B": ["..."]},
 "merge_advice": "<one or two sentences for the project owner>"}
"""


def _prompt_block(label: str, facts: dict[str, Any]) -> str:
    return (
        "\n## Implementation "
        + label
        + "\n\n- outcome: "
        + str(facts["status"])
        + " ("
        + str(facts["failure_reason"] or "no failure")
        + "); iterations: "
        + str(facts["iterations"])
        + "; escalated: "
        + str(facts["escalated"])
        + "\n- automated review rounds (oldest first):\n```json\n"
        + json.dumps(facts["reviews"], indent=1)[:REVIEWS_LIMIT]
        + "\n```\n\n### Diff "
        + label
        + "\n```diff\n"
        + str(facts["diff"] or "(no diff)")
        + "\n```\n"
    )


def build_prompt(brief: str, a: dict[str, Any], b: dict[str, Any]) -> str:
    return (
        PROMPT_HEAD
        + "\n# The task\n\n"
        + brief
        + "\n"
        + _prompt_block("A", a)
        + _prompt_block("B", b)
        + "\nJudge A against B as instructed. Reply with the JSON object only."
    )


def _json_candidates(text: str) -> Iterator[Any]:
    blocks = re.findall(r"^```(?:json)?[ \t]*\r?\n(.*?)^```[ \t]*$", text, re.M | re.S)
    for block in reversed(blocks):
        try:
            yield json.loads(block)
        except ValueError:
            continue
    decoder = json.JSONDecoder()
    for i, char in enumerate(text):
        if char == "{":
            try:
                candidate, _ = decoder.raw_decode(text, i)
            except ValueError:
                continue
            yield candidate


def parse_judgement(text: str) -> dict[str, Any]:
    for candidate in _json_candidates(text):
        if isinstance(candidate, dict) and "winner" in candidate:
            return _validate_judgement(candidate)
    raise ValueError("judge answer contains no JSON verdict")


def _validate_judgement(value: dict[str, Any]) -> dict[str, Any]:
    for key, allowed in (("winner", ("A", "B", "tie")), ("confidence", ("low", "medium", "high"))):
        if not isinstance(value.get(key), str) or value[key] not in allowed:
            raise ValueError("judge answer: invalid " + key)
    scores = value.get("scores")
    if not isinstance(scores, dict):
        raise ValueError("judge answer: scores must contain A and B")
    for label in ("A", "B"):
        row = scores.get(label)
        if not isinstance(row, dict) or set(row) != set(CRITERIA):
            raise ValueError("judge answer: invalid scores for " + label)
        if any(type(score) is not int or not 1 <= score <= 5 for score in row.values()):
            raise ValueError("judge answer: scores must be integers 1-5 for " + label)
    for key in ("decisive", "merge_advice"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError("judge answer: " + key + " must be a non-empty string")
    defects = value.get("defects")
    if not isinstance(defects, dict):
        raise ValueError("judge answer: defects must contain A and B lists")
    for label in ("A", "B"):
        items = defects.get(label)
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise ValueError("judge answer: defects must be a list of strings for " + label)
    return {key: value[key] for key in VERDICT_KEYS}


def build_result(
    a: dict[str, Any],
    b: dict[str, Any],
    verdict: dict[str, Any],
    *,
    judge_model: str,
    judge_cost: float,
    variant_of: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    runs = {
        label: {k: v for k, v in facts.items() if k not in ("brief", "reviews", "diff")}
        for label, facts in (("A", a), ("B", b))
    }
    result = {
        "judge": {"model": judge_model, "cost_usd": judge_cost},
        "runs": runs,
        "verdict": verdict,
        "winner_run_id": (
            runs[verdict["winner"]]["run_id"] if verdict["winner"] != "tie" else None
        ),
        "warnings": warnings,
    }
    if variant_of is not None:
        result["variant_of"] = variant_of
    return result


def render_markdown(result: dict[str, Any]) -> str:
    a, b = result["runs"]["A"], result["runs"]["B"]
    judge, verdict = result["judge"], result["verdict"]
    lines = ["# Comparison: A vs B", ""]
    if "variant_of" in result:
        lines += ["Variants of " + result["variant_of"], ""]
    lines += [
        f"Judge: {judge['model']}; cost: ${judge['cost_usd']:.4f}",
        "",
        "| field | A | B |",
        "| --- | --- | --- |",
    ]
    for field in ("run", "task", "status", "iterations", "escalated", "minutes", "cost", "setup"):
        cells = []
        for run in (a, b):
            if field == "run":
                cell = run["run_id"][:8]
            elif field == "task":
                cell = run["task_id"]
            elif field == "escalated":
                cell = "yes" if run["escalated"] else "no"
            elif field == "cost":
                cell = f"${run['cost_usd']:.4f}"
            elif field == "setup":
                cell = setup_label(run["setup"])
            else:
                cell = "-" if run[field] is None else str(run[field])
            cells.append(cell)
        lines.append("| " + field + " | " + " | ".join(cells) + " |")
    winner = verdict["winner"]
    winner_line = "tie" if winner == "tie" else winner + " (" + result["winner_run_id"] + ")"
    lines += [
        "",
        "## Verdict",
        "",
        "Winner: " + winner_line,
        "Confidence: " + verdict["confidence"],
        "",
        verdict["decisive"],
        "",
        "| criterion | A | B |",
        "| --- | --- | --- |",
    ]
    for criterion in CRITERIA:
        lines.append(
            f"| {criterion} | {verdict['scores']['A'][criterion]} | "
            f"{verdict['scores']['B'][criterion]} |"
        )
    for label in ("A", "B"):
        lines += ["", "### Defects " + label, ""]
        lines += ["- " + defect for defect in verdict["defects"][label]] or ["- none"]
    lines += ["", "### Merge advice", "", verdict["merge_advice"], ""]
    lines += ["- warning: " + warning for warning in result["warnings"]]
    return "\n".join(lines)
