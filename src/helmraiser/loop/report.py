"""Morning report (spec §5 ``helmraiser report``): Landed, Awaiting your approval,
Failed, Cost — from the ledger, with each run's ``summary.md`` fragment where
one exists."""

from __future__ import annotations

import re
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from helmraiser.config import load_config
from helmraiser.ledger.db import Ledger
from helmraiser.ledger.rundir import RunDir
from helmraiser.logbook import run_entries
from helmraiser.loop.review import parse_verdict
from helmraiser.loop.spend import (
    WINDOWS,
    cost_entries,
    live_for,
    spend_by_provider,
    subscription_warnings,
    usage,
)
from helmraiser.task.project import load_project_defaults
from helmraiser.utils.markdown import parse_frontmatter

SINCE_SECONDS = {"24h": 24 * 3600, "7d": 7 * 24 * 3600, "30d": 30 * 24 * 3600}
_SINCE_RE = re.compile(r"^(\d+)([hd])$")
TAIL_LINES = 10
LOGBOOK_ENTRY_LIMIT = 20


def parse_since(text: str) -> float:
    """``24h`` / ``7d`` / ``30d`` (or any ``<n>h`` / ``<n>d``) as seconds."""
    if text in SINCE_SECONDS:
        return float(SINCE_SECONDS[text])
    match = _SINCE_RE.match(text.strip())
    if not match:
        raise ValueError(f"--since must look like 24h, 7d or 30d (got {text!r})")
    n, unit = int(match.group(1)), match.group(2)
    return float(n * (3600 if unit == "h" else 24 * 3600))


def _fragment_body(rundir: RunDir) -> str | None:
    """The detail sections of ``summary.md`` (from its first ``###`` on); the
    header bullets are regenerated from the ledger, which is the truth after
    ``approve`` / ``reject``."""
    if not rundir.summary_md.is_file():
        return None
    text = rundir.summary_md.read_text(encoding="utf-8")
    index = text.find("\n### ")
    return text[index + 1 :].strip() if index >= 0 else None


def _findings_from_review(rundir: RunDir) -> list[dict[str, Any]]:
    if not rundir.review_md.is_file():
        return []
    return parse_verdict(rundir.review_md.read_text(encoding="utf-8")).findings


def _last_verify_tails(iterations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for iteration in reversed(iterations):
        results = iteration.get("verify_results") or []
        if results:
            return [
                {
                    "name": r.get("name"),
                    "passed": bool(r.get("passed")),
                    "exit_code": r.get("exit_code"),
                    "tail": "\n".join((r.get("tail") or "").splitlines()[-TAIL_LINES:]),
                }
                for r in results
                if not r.get("passed")
            ]
    return []


def _task_base(row: Any) -> str | None:
    """The task's own ``base``, else its project's ``[defaults] base``."""
    base = parse_frontmatter(str(row["snapshot"]))["frontmatter"].get("base")
    if base is None:
        try:
            base = load_project_defaults(Path(str(row["project"]))).base
        except ValueError:
            return None
    return str(base) if base else None


def render_path_scope_failures(failures: list[dict[str, Any]]) -> list[str]:
    """Keep complete scope diagnostics, including failures repaired later."""
    if not failures:
        return []
    lines = ["", "### Task path scope failures", ""]
    for failure in failures:
        lines += [f"#### Iteration {failure['n']}", "", "```", failure["diagnostic"], "```", ""]
    return lines


APPROVAL_SUBJECTS = {"plan": "the plan", "merge": "the merge", "scope": "the scope request"}


def pending_scope_request(ledger: Ledger, run_id: str) -> dict[str, Any] | None:
    """The paths and reason of the run's pending scope approval (its last ``scope_requested``)."""
    pending = ledger.pending_approval(run_id)
    if pending is None or pending["kind"] != "scope":
        return None
    requests = ledger.events(run_id, types=("scope_requested",))
    if not requests:
        return None
    payload = requests[-1]["payload"]
    return {"paths": list(payload["paths"]), "reason": payload["reason"]}


def render_scope_request(run_id: str, request: dict[str, Any]) -> list[str]:
    paths = ", ".join(f"`{p}`" for p in request["paths"])
    return [
        f"- scope request: {paths} — {request['reason'] or '(no reason given)'}",
        f"- approve a subset: `helmraiser approve {run_id} --paths <path>[,<path>…]`",
        f'- decline: `helmraiser approve {run_id} --decline --note "<why>"`',
    ]


def render_reworks(reworks: list[dict[str, Any]]) -> list[str]:
    """Header bullets, so summary fragments do not duplicate ledger-derived history."""
    if not reworks:
        return []
    lines = [f"- rework rounds: {len(reworks)}"]
    for request in reworks:
        lines.append(f"- rework {request['round']} note:")
        lines.extend(f"  {line}" for line in request["note"].split("\n"))
    return lines


def build_report(
    ledger: Ledger,
    data_dir: Path,
    since_seconds: float,
    now: float | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = now if now is not None else time.time()
    config = config if config is not None else load_config()
    cutoff = now - since_seconds
    task_rows = ledger.list_tasks()
    titles = {row["task_id"]: row["title"] for row in task_rows}
    task_bases = {row["task_id"]: _task_base(row) for row in task_rows}
    landed: list[dict[str, Any]] = []
    awaiting: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    by_model: dict[str, float] = {}

    for run in ledger.list_runs():
        run_id = str(run["run_id"])
        status = str(run["status"])
        in_window = float(run["ended_at"] or run["started_at"]) >= cutoff
        if status == "awaiting_approval":
            pass  # always actionable, whatever its age
        elif status not in ("landed", "failed", "aborted") or not in_window:
            continue
        iterations = ledger.iterations(run_id)
        events = ledger.events(
            run_id, types=("review_feedback", "path_scope_failed", "landed_outside")
        )
        rundir = RunDir(run_id, data_dir)
        entry: dict[str, Any] = {
            "run_id": run_id,
            "task_id": run["task_id"],
            "title": titles.get(str(run["task_id"]), str(run["task_id"])),
            "status": status,
            "cost_usd": round(float(run["cost_usd"] or 0.0), 4),
            "iterations": len(iterations),
            "escalated": bool(run["escalated"]),
            "review_rounds": sum(event["type"] == "review_feedback" for event in events),
            "verdict": run["verdict"],
            "failure_reason": run["failure_reason"],
            "branch_name": run["branch_name"],
            "base": task_bases.get(run["task_id"]),
            "pr_url": run["pr_url"],
            "started_at": _iso(run["started_at"]),
            "ended_at": _iso(run["ended_at"]),
            "fragment": _fragment_body(rundir),
        }
        reworks = ledger.reworks(run_id)
        if reworks:
            entry.update(rework_rounds=len(reworks), reworks=reworks)
        outside = [e["payload"] for e in events if e["type"] == "landed_outside"]
        if outside:
            entry["landed_outside"] = {"commit": outside[-1]["commit"], "note": outside[-1]["note"]}
        if status == "landed":
            landed.append(entry)
        elif status == "awaiting_approval":
            pending = ledger.pending_approval(run_id)
            entry["pending"] = pending["kind"] if pending else "merge"
            entry["plan_path"] = str(rundir.plan_md) if entry["pending"] == "plan" else None
            if entry["pending"] == "scope":
                entry["scope_request"] = pending_scope_request(ledger, run_id)
            entry["findings"] = _findings_from_review(rundir)
            awaiting.append(entry)
        else:
            entry["verify_tails"] = _last_verify_tails(iterations)
            scope_failures = [e["payload"] for e in events if e["type"] == "path_scope_failed"]
            if scope_failures:
                entry["path_scope_failures"] = scope_failures
            failed.append(entry)

    included = landed + awaiting + failed
    logbook = run_entries(entry["run_id"] for entry in included)
    for entry in included:
        authored = logbook[entry["run_id"]]
        entry["logbook_entries"] = [asdict(item) for item in authored[:LOGBOOK_ENTRY_LIMIT]]
        if len(authored) > LOGBOOK_ENTRY_LIMIT:
            entry["logbook_more"] = len(authored) - LOGBOOK_ENTRY_LIMIT

    for _timestamp, model, cost in cost_entries(ledger, cutoff):
        by_model[model] = by_model.get(model, 0.0) + cost

    return {
        "generated_at": datetime.fromtimestamp(now, tz=UTC).isoformat(),
        "since_seconds": int(since_seconds),
        "landed": landed,
        "awaiting": awaiting,
        "failed": failed,
        "cost": {
            "by_model": {m: round(c, 4) for m, c in sorted(by_model.items())},
            "total_usd": round(sum(by_model.values()), 4),
            "spend": spend_by_provider(ledger, now),
            "warnings": subscription_warnings(
                config, usage(ledger, config, now, live=live_for(ledger, config, now))
            ),
        },
    }


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat() if ts else None


def _header(entry: dict[str, Any]) -> list[str]:
    lines = [
        f"## {entry['title']} — {entry['status']}",
        "",
        f"- task: `{entry['task_id']}`",
        f"- run: `{entry['run_id']}`",
        f"- cost: ${entry['cost_usd']:.4f}; iterations: {entry['iterations']}"
        + (" (escalated)" if entry["escalated"] else ""),
        f"- verdict: {entry['verdict'] or '-'}",
    ]
    if entry.get("logbook_entries"):
        count = len(entry["logbook_entries"]) + entry.get("logbook_more", 0)
        lines.append(f"- logbook entries: {count}")
    lines += render_reworks(entry.get("reworks", []))
    if entry.get("review_rounds"):
        lines.append(f"- review rounds: {entry['review_rounds']}")
    if entry.get("branch_name"):
        lines.append(f"- branch: `{entry['branch_name']}`")
    if entry.get("base"):
        lines.append(f"- base: `{entry['base']}`")
    if entry.get("pr_url"):
        lines.append(f"- pr: {entry['pr_url']}")
    if entry.get("landed_outside"):
        outside = entry["landed_outside"]
        lines.append(f"- landed outside Helmraiser: `{outside['commit'][:12]}` — {outside['note']}")
    return lines


def _render_logbook_entries(entry: dict[str, Any]) -> list[str]:
    if not entry.get("logbook_entries"):
        return []
    lines = ["", "### Logbook entries", ""]
    for item in entry["logbook_entries"]:
        title = " ".join(item["title"].split()) or "(untitled)"
        if len(title) > 200:
            title = title[:199] + "…"
        lines.append(f"- [{item['id']}] {item['kind']} {item['status']} {item['scope']}: {title}")
    if entry.get("logbook_more"):
        lines.append(f"… {entry['logbook_more']} more — lb list")
    return lines


def render_report(report: dict[str, Any], since_label: str) -> str:
    out = [f"# Helmraiser report — last {since_label} ({report['generated_at'][:16]}Z)", ""]

    out.append(f"# Landed ({len(report['landed'])})")
    out.append("")
    for entry in report["landed"]:
        out += _header(entry)
        if entry["fragment"]:
            out += ["", entry["fragment"]]
        out += _render_logbook_entries(entry)
        out.append("")
    if not report["landed"]:
        out += ["(none)", ""]

    out.append(f"# Awaiting your approval ({len(report['awaiting'])})")
    out.append("")
    for entry in report["awaiting"]:
        out += _header(entry)
        what = APPROVAL_SUBJECTS.get(entry["pending"], "the merge")
        if entry["pending"] == "plan":
            out.append(f"- plan: `{entry['plan_path']}`")
        if entry.get("scope_request"):
            out += render_scope_request(entry["run_id"], entry["scope_request"])
        out.append(
            f"- next: `helmraiser approve {entry['run_id']}` or "
            f"`helmraiser reject {entry['run_id']}` — {what}"
        )
        if entry["pending"] == "merge":
            out.append(f"- or: `helmraiser rework {entry['run_id']} --note \"<feedback>\"`")
        if entry["fragment"]:
            out += ["", entry["fragment"]]
        elif entry["findings"]:
            out += ["", "### Reviewer findings", ""]
            for f in entry["findings"]:
                out.append(
                    f"- [{f.get('severity', '?')}] {f.get('file', '?')}:{f.get('line', 0)} — "
                    f"{f.get('note', '')}"
                )
        out += _render_logbook_entries(entry)
        out.append("")
    if not report["awaiting"]:
        out += ["(none)", ""]

    out.append(f"# Failed ({len(report['failed'])})")
    out.append("")
    for entry in report["failed"]:
        out += _header(entry)
        out.append(f"- reason: {entry['failure_reason'] or '-'}")
        if entry["verify_tails"]:
            out += ["", "### Last verify failures", ""]
            for r in entry["verify_tails"]:
                out += [f"- {r['name']}: exit {r['exit_code']}", "", "```", r["tail"], "```", ""]
        elif entry["fragment"]:
            out += ["", entry["fragment"]]
        if entry.get("path_scope_failures") and (entry["verify_tails"] or not entry["fragment"]):
            out += render_path_scope_failures(entry["path_scope_failures"])
        out += _render_logbook_entries(entry)
        out.append("")
    if not report["failed"]:
        out += ["(none)", ""]

    cost = report["cost"]
    out += ["# Cost", "", "| model | usd |", "|---|---|"]
    for model, usd in cost["by_model"].items():
        out.append(f"| {model} | {usd:.4f} |")
    out.append(f"| **total** | **{cost['total_usd']:.4f}** |")
    out += ["", "## Spend", "", "| provider | 5h | 7d | 30d |", "|---|---|---|---|"]
    for name in sorted(cost["spend"]):
        cells = []
        for window in WINDOWS:
            value = cost["spend"].get(name, {}).get(window, 0.0)
            cells.append(f"${value:.4f}")
        out.append(f"| {name} | " + " | ".join(cells) + " |")
    for warning in cost["warnings"]:
        out.append("")
        out.append(warning)
    out.append("")
    return "\n".join(out)
