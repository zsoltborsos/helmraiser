"""Shared, fail-closed admission policy for sibling runs."""

from pathlib import Path
from typing import Any

from helmraiser.ledger.db import PARKED_RUN_STATUSES, TERMINAL_RUN_STATUSES
from helmraiser.ledger.dependencies import dependency_relation
from helmraiser.task.paths import parse_paths, paths_overlap
from helmraiser.task.project import effective_run_limit
from helmraiser.utils.markdown import parse_frontmatter


def _snapshot_paths(row: dict[str, Any]) -> tuple[str, ...]:
    parsed = parse_frontmatter(str(row.get("snapshot") or ""))
    if parsed.get("error"):
        raise ValueError(str(parsed["error"]))
    return parse_paths(parsed["frontmatter"].get("paths"))


def variant_of(row: dict[str, Any] | None) -> str | None:
    parsed = parse_frontmatter(str((row or {}).get("snapshot") or ""))
    value = (parsed.get("frontmatter") or {}).get("variant_of")
    return str(value) if value else None


def project_hold(
    candidate: dict[str, Any],
    tasks: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    resume_id: str | None = None,
) -> dict[str, Any] | None:
    rows = {t["task_id"]: t for t in tasks}
    # Variants of one task (``variant_of``) are compared side by side from the same base:
    # a parked variant keeps its own worktree and branch and does not hold its siblings.
    variant = variant_of(candidate)
    # A pair's two runs (each run's ``pair`` is its pair's id, see ``Ledger._with_pairs``)
    # share one task and one slot: the other run of a resuming candidate's pair holds it
    # on neither slots, paths nor dependencies.
    own = next((r for r in runs if r["run_id"] == resume_id), None) if resume_id else None
    pair = own.get("pair") if own is not None else None
    holders = sorted(
        (
            r
            for r in runs
            if r["status"] not in TERMINAL_RUN_STATUSES
            and r["run_id"] != resume_id
            and not (pair is not None and r.get("pair") == pair)
            and not (
                variant
                and r["status"] in PARKED_RUN_STATUSES
                and variant_of(rows.get(r["task_id"])) == variant
            )
        ),
        key=lambda r: r["started_at"],
    )
    if not holders:
        return None

    def hold(
        run: dict[str, Any], reason: str | None = None, detail: str | None = None
    ) -> dict[str, Any]:
        pending = run.get("pending")
        if run["status"] in PARKED_RUN_STATUSES and pending is None:
            pending = "merge"
        result = {"run_id": run["run_id"], "status": run["status"], "pending": pending}
        if reason:
            result["reason"] = reason
            result["other_task_id"] = rows.get(run["task_id"], {}).get("id", run["task_id"])
        if detail:
            result["detail"] = detail
        return result

    try:
        limit = effective_run_limit(Path(candidate["project"]), config)
    except (ValueError, OSError) as exc:
        return hold(holders[0], "project_busy", str(exc))
    if limit == 1:
        if resume_id:
            holders = [r for r in holders if r["status"] != "resuming"]
        return hold(holders[0]) if holders else None
    # Resumable runs must not hold each other, on slots or paths: only one resumed
    # earlier holds, so the oldest go first.
    if own is not None:
        holders = [
            r
            for r in holders
            if r["status"] != "resuming"
            or (r["started_at"], r["run_id"]) < (own["started_at"], own["run_id"])
        ]
    # A resumable candidate already owns a slot; queued candidates need a new one.
    # Both runs of a pair take one slot.
    if len({r.get("pair") or r["run_id"] for r in holders}) >= limit:
        return hold(holders[0], "project_busy")
    for run in holders:
        other = rows.get(run["task_id"])
        try:
            if other is None:
                raise ValueError("missing holder task snapshot")
            left, right = _snapshot_paths(candidate), _snapshot_paths(other)
        except (ValueError, TypeError, AttributeError) as exc:
            return hold(run, "project_busy", f"paths required on both tasks: {exc}")
        if paths_overlap(left, right):
            return hold(run, "paths_overlap")
        relation = dependency_relation(tasks, str(candidate["id"]), str(other["id"]))
        if relation is not False:
            result = hold(run, "depends_on")
            result["unmet"] = [
                {
                    "id": other["id"],
                    "reason": "concurrent_dependency",
                    "text": (
                        f"cannot run concurrently with {other['id']}: dependency relationship"
                        if relation
                        else f"cannot establish dependency independence from {other['id']}"
                    ),
                }
            ]
            return result
    return None
