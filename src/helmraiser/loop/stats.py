"""Read-only dashboard aggregates. All ledger reads are bulk, within one snapshot."""

from __future__ import annotations

import math
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from helmraiser.config import load_config
from helmraiser.ledger.db import Ledger
from helmraiser.loop.scout import (
    DROP_CLASSES,
    ScoutError,
    configured_focus_projects,
    draft_acceptance_scope_hash,
    draft_drop_class,
    head_commit,
    scout_due_at,
)
from helmraiser.loop.scout_lenses import LENSES, scout_lens_settings
from helmraiser.utils.markdown import parse_frontmatter

WINDOWS = {"24h": 86400, "7d": 604800, "30d": 2592000, "90d": 7776000, "all": None}


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts is not None else None


def _rate(n: int | float, d: int) -> float | None:
    return round(n / d, 3) if d else None


def _distribution(values: list[float]) -> dict[str, float | None]:
    values = sorted(values)

    def percentile(p: float) -> float | None:
        if not values:
            return None
        pos = (len(values) - 1) * p
        lo = math.floor(pos)
        return values[lo] + (values[math.ceil(pos)] - values[lo]) * (pos - lo)

    return {"median": percentile(0.5), "p90": percentile(0.9)}


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    result = dict.fromkeys(("landed", "failed", "aborted", "awaiting_approval", "active"), 0)
    for row in rows:
        status = row["status"]
        result[status if status in result else "active"] += 1
    return result


def build_stats(
    ledger: Ledger,
    since: str = "7d",
    project: str | None = None,
    *,
    config: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Return the complete JSON contract, without writing or reading run directories."""
    if since not in WINDOWS:
        raise ValueError("--since must be one of: " + ", ".join(WINDOWS))
    now = time.time() if now is None else now
    config = load_config() if config is None else config
    seconds = WINDOWS[since]
    cutoff = now - seconds if seconds is not None else float("-inf")
    with ledger.read_snapshot():
        history = ledger.list_runs(project=project)
        scouts = ledger.list_scouts(project=project)
        iterations = {r["run_id"]: r for r in ledger.stats_iterations(project)}
        detail = ledger.stats_history(project)
        drafts = ledger.stats_drafts(project)
        settings = scout_lens_settings(config, ledger)
    runs = [r for r in history if cutoff <= r["started_at"] <= now]
    selected_scouts = [s for s in scouts if cutoff <= s["started_at"] <= now]
    approvals: dict[str, list[dict[str, Any]]] = defaultdict(list)
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in detail["approvals"]:
        approvals[row["run_id"]].append(row)
    for row in detail["events"]:
        events[row["run_id"]].append(row)
    earlier: set[str] = set()
    first: set[str] = set()
    latest: dict[str, dict[str, Any]] = {}
    for row in sorted(history, key=lambda r: (r["started_at"], r["run_id"])):
        if row["task_id"] not in earlier:
            first.add(row["run_id"])
        earlier.add(row["task_id"])
        latest[row["task_id"]] = row
    landed_tasks = {r["task_id"] for r in history if r["status"] == "landed"}
    scope = {rid for rid, rows in approvals.items() if any(a["kind"] == "scope" for a in rows)}
    scope.update(
        rid for rid, rows in events.items() if any(e["type"] == "scope_requested" for e in rows)
    )
    rework = {
        rid
        for rid, rows in approvals.items()
        if any(a["kind"] == "merge" and a["decision"] == "rework" for a in rows)
    }
    merge = {
        rid
        for rid, rows in approvals.items()
        if any(a["kind"] == "merge" and a["decision"] != "pending" for a in rows)
    }
    ftr = {
        r["run_id"]
        for r in runs
        if r["status"] == "landed"
        and r["run_id"] in first
        and iterations.get(r["run_id"], {}).get("count") == 1
        and r["run_id"] not in scope | rework
    }
    ids = {r["run_id"] for r in runs}
    counts = _counts(runs)
    failures: dict[str, Any] = {}
    for row in sorted(runs, key=lambda r: (r["started_at"], r["run_id"]), reverse=True):
        if row["status"] not in {"failed", "aborted"}:
            continue
        cls = (row["failure_reason"] or "").split(":", 1)[0].strip() or "unknown"
        entry = failures.setdefault(cls, {"count": 0, "run_ids": []})
        entry["count"] += 1
        if len(entry["run_ids"]) < 3:
            entry["run_ids"].append(row["run_id"])
    active = []
    waits: dict[str, list[float]] = {k: [] for k in ("plan", "scope", "merge")}
    for row in runs:
        rid = row["run_id"]
        ends = sorted(
            (e for e in events[rid] if e["type"] == "run_end"), key=lambda e: (e["ts"], e["id"])
        )
        samples = [e["payload"].get("active_minutes") for e in ends]
        numeric = [v for v in samples if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if row["status"] == "landed" and numeric:
            active.append(sum(numeric))
        waiting = [e for e in ends if e["payload"].get("status") == "awaiting_approval"]
        decisions = sorted(
            (a for a in approvals[rid] if a["decision"] != "pending"),
            key=lambda a: (a["decided_at"], a["id"]),
        )
        used: set[int] = set()
        for i, event in enumerate(waiting):
            stop = waiting[i + 1]["ts"] if i + 1 < len(waiting) else float("inf")
            decision = next(
                (
                    a
                    for a in decisions
                    if a["id"] not in used and event["ts"] <= a["decided_at"] < stop
                ),
                None,
            )
            if decision is not None:
                used.add(decision["id"])
                if decision["kind"] in waits:
                    waits[decision["kind"]].append((decision["decided_at"] - event["ts"]) / 60)
    total = sum(r["cost_usd"] for r in runs)
    plan = sum(r["plan_cost_usd"] for r in runs)
    implement = sum(iterations.get(r["run_id"], {}).get("cost", 0) for r in runs)
    focus = {str(p) for p in configured_focus_projects(config)}
    projects = {project} if project is not None else focus | {s["project"] for s in scouts}
    by_scout: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for draft in drafts:
        by_scout[draft["scout_id"]].append(draft)

    def ever_queued(d: dict[str, Any]) -> bool:
        return d["status"] == "queued" or d["task_id"] in latest

    def outcome(d: dict[str, Any]) -> str:
        task = d["task_id"]
        if task in landed_tasks:
            return "landed"
        if d["status"] == "dropped":
            return "dropped"
        if task in latest and latest[task]["status"] in {"failed", "aborted"}:
            return "failed"
        return "queued" if ever_queued(d) else "pending"

    grid = []
    for proj in sorted(projects):
        scheduling_error = None
        current_head = None
        try:
            current_head = head_commit(Path(proj))
        except (OSError, RuntimeError, ValueError, ScoutError) as exc:
            scheduling_error = "repository unavailable: " + str(exc).splitlines()[0][:160]
        for lens in LENSES:
            full = [s for s in scouts if s["project"] == proj and s["lens"] == lens]
            cohort = [s for s in selected_scouts if s["project"] == proj and s["lens"] == lens]
            last = max(full, key=lambda s: (s["started_at"], s["id"]), default=None)
            ds = [d for s in cohort for d in by_scout[s["id"]]]
            outcomes = Counter(outcome(d) for d in ds)
            dropped_by_class = dict.fromkeys((*DROP_CLASSES, "unclassified"), 0)
            for d in ds:
                if outcome(d) == "dropped":
                    frontmatter = parse_frontmatter(d["snapshot"]).get("frontmatter") or {}
                    classification = draft_drop_class(frontmatter)
                    dropped_by_class[classification or "unclassified"] += 1
            hashes: Counter[str] = Counter()
            for d in ds:
                if not ever_queued(d):
                    continue
                baseline = d["acceptance_scope_sha256"]
                hashes[
                    (
                        "unknown"
                        if baseline is None
                        else (
                            "unchanged"
                            if baseline == draft_acceptance_scope_hash(d["snapshot"])
                            else "changed"
                        )
                    )
                ] += 1
            due = float("-inf")
            if scheduling_error is None:
                due = scout_due_at(
                    Path(proj),
                    ledger,
                    config,
                    lens=lens,
                    scouts=scouts,
                    settings=settings,
                    current_head=current_head,
                )
            grid.append(
                {
                    "project": proj,
                    "lens": lens,
                    "scouts": len(cohort),
                    "succeeded": sum(s["outcome"] == "succeeded" for s in cohort),
                    "failed": sum(s["outcome"] == "failed" for s in cohort),
                    "drafts_written": sum(len(s["drafts_written"]) for s in cohort),
                    "cost_usd": sum(s["cost_usd"] for s in cohort),
                    **{k: outcomes[k] for k in ("pending", "queued", "landed", "dropped")},
                    "failed_drafts": outcomes["failed"],
                    "dropped_by_class": dropped_by_class,
                    **{"queued_" + k: hashes[k] for k in ("unchanged", "changed", "unknown")},
                    "last_scout_ended_at": _iso(last["ended_at"]) if last else None,
                    "last_scout_ok": last["outcome"] == "succeeded" if last else None,
                    "last_scout_error": last.get("error") or None if last else None,
                    "last_scout_drafts": len(last["drafts_written"]) if last else None,
                    "auto": settings[lens]["auto"],
                    "cooldown_hours": settings[lens]["cooldown_hours"],
                    "next_eligible_at": _iso(due) if math.isfinite(due) else None,
                    "scheduling_error": scheduling_error,
                    "in_focus": proj in focus,
                }
            )

    def monday(ts: float) -> datetime:
        dt = datetime.fromtimestamp(ts, UTC)
        return dt.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=dt.weekday())

    start = (
        cutoff
        if seconds is not None
        else min(
            [r["started_at"] for r in runs] + [s["started_at"] for s in selected_scouts] + [now]
        )
    )
    week = max(monday(start), monday(now) - timedelta(weeks=25))
    weekly = []
    while week <= monday(now):
        end = (week + timedelta(weeks=1)).timestamp()
        wr = [r for r in runs if week.timestamp() <= r["started_at"] < end]
        ws = [s for s in selected_scouts if week.timestamp() <= s["started_at"] < end]
        wd = [d for s in ws for d in by_scout[s["id"]]]
        weekly.append(
            {
                "week": week.strftime("%G-W%V"),
                "landed": sum(r["status"] == "landed" for r in wr),
                "failed": sum(r["status"] == "failed" for r in wr),
                "first_time_right_rate": _rate(
                    sum(r["run_id"] in ftr for r in wr), sum(r["status"] == "landed" for r in wr)
                ),
                "drafts_written": sum(len(s["drafts_written"]) for s in ws),
                "drafts_landed": sum(outcome(d) == "landed" for d in wd),
            }
        )
        week += timedelta(weeks=1)
    verdicts = Counter(r["verdict"] or "none" for r in runs)
    return {
        "window": {
            "since": since,
            "since_seconds": seconds,
            "generated_at": _iso(now),
            "project": project,
        },
        "runs": {
            "overall": counts,
            "by_project": [
                {"project": p, **_counts([r for r in runs if r["project"] == p])}
                for p in sorted({r["project"] for r in runs})
            ],
        },
        "quality": {
            "first_time_right": {"count": len(ftr), "rate": _rate(len(ftr), counts["landed"])},
            "rework_rate": _rate(len(rework & ids), len(merge & ids)),
            "scope_request_rate": _rate(len(scope & ids), len(runs)),
            "retried_tasks": sum(n > 1 for n in Counter(r["task_id"] for r in runs).values()),
            "review_verdicts": {k: verdicts[k] for k in ("pass", "concerns", "uncertain", "none")},
        },
        "failures": dict(sorted(failures.items())),
        "cost": {
            "total_usd": total,
            "plan_usd": plan,
            "implement_usd": implement,
            "other_usd": max(0, total - plan - implement),
            "per_landed_usd": total / counts["landed"] if counts["landed"] else None,
        },
        "latency": {
            "active_minutes": _distribution(active),
            "approval_wait_minutes": {k: _distribution(v) for k, v in waits.items()},
        },
        "scouts": grid,
        "weekly": weekly,
    }
