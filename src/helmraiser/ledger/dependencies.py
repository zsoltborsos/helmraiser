"""Read-only, invocation-local dependency eligibility from ledger snapshots."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from helmraiser.task.loader import parse_task
from helmraiser.task.model import parse_depends_on
from helmraiser.utils.markdown import parse_frontmatter


class _DependencyError(ValueError):
    def __init__(self, reason: str, text: str):
        super().__init__(text)
        self.reason = reason


def _snapshot_dependencies(row: dict[str, Any]) -> tuple[str, ...]:
    parsed = parse_frontmatter(str(row.get("snapshot") or ""))
    if parsed.get("error"):
        raise ValueError(str(parsed["error"]))
    return parse_depends_on(parsed["frontmatter"].get("depends_on", []))


def _cycle_path(start: str, graph: dict[str, tuple[str, ...]]) -> list[str] | None:
    """Iterative DFS: completed shared nodes are not cycles."""
    path = [start]
    active = {start}
    completed: set[str] = set()
    stack = [iter(graph.get(start, ()))]
    while stack:
        node = next(stack[-1], None)
        if node is None:
            finished = path.pop()
            completed.add(finished)
            active.remove(finished)
            stack.pop()
        elif node in active:
            return [*path[path.index(node) :], node]
        elif node not in completed:
            path.append(node)
            active.add(node)
            stack.append(iter(graph.get(node, ())))
    return None


def dependency_relation(tasks: list[dict[str, Any]], left_id: str, right_id: str) -> bool | None:
    """Check both reachable snapshot graphs, failing closed on unknown declarations."""
    rows = {str(row["id"]): row for row in tasks}

    def reaches(start: str, target: str) -> bool | None:
        graph: dict[str, tuple[str, ...]] = {}
        pending = [start]
        uncertain = False
        while pending:
            node = pending.pop()
            if node == target:
                return True
            if node in graph:
                continue
            row = rows.get(node)
            try:
                if row is None:
                    raise ValueError("unknown dependency")
                graph[node] = _snapshot_dependencies(row)
            except (ValueError, TypeError, AttributeError):
                graph[node] = ()
                uncertain = True
            pending.extend(graph[node])
        return None if uncertain or _cycle_path(start, graph) else False

    left, right = reaches(left_id, right_id), reaches(right_id, left_id)
    if left is True or right is True:
        return True
    return None if left is None or right is None else False


def _git(project: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )


def _base_tip(row: dict[str, Any], cache: dict[tuple[str, str], str]) -> str:
    project = str(row["project"])
    try:
        spec = parse_task(str(row["snapshot"]), Path(row["source_path"]), {})
        ref = f"refs/heads/{spec.base}" if spec.base is not None else "HEAD"
        key = (project, ref)
        if key in cache:
            return cache[key]
        if ref == "HEAD":
            result = _git(project, "symbolic-ref", "--quiet", "HEAD")
            ref = result.stdout.strip()
            if result.returncode or not ref.startswith("refs/heads/"):
                raise ValueError("primary checkout is detached or has no local branch")
        result = _git(project, "rev-parse", "--verify", f"{ref}^{{commit}}")
        if result.returncode:
            raise ValueError(f"{ref}: {result.stderr.strip()}")
        cache[key] = result.stdout.strip()
        return cache[key]
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        raise _DependencyError("base_unavailable", str(exc)) from exc


def _item(id: str, reason: str, text: str) -> dict[str, str]:
    return {"id": id, "reason": reason, "text": f"{id}: {text}"}


def _unmet_dependency(
    id: str,
    candidate: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    runs: dict[str, list[dict[str, Any]]],
    graph: dict[str, tuple[str, ...]],
    invalid: dict[str, str],
    bases: dict[tuple[str, str], str],
    ancestry: dict[tuple[str, str, str], tuple[int, str]],
) -> dict[str, str] | None:
    dependency = tasks.get(id)
    if dependency is None:
        return _item(id, "unknown", "unknown dependency id")
    if id in invalid:
        return _item(id, "invalid", f"invalid depends_on: {invalid[id]}")
    cycle = _cycle_path(id, graph)
    if cycle:
        return _item(id, "cycle", f"dependency cycle: {' -> '.join(cycle)}")
    history = runs.get(id, [])
    latest = max(history, key=lambda r: (r.get("started_at") or 0, r["run_id"]), default=None)
    if latest and latest["status"] in ("failed", "aborted"):
        status = str(latest["status"])
        return _item(id, status, f"latest run {latest['run_id']} {status}")
    landed = [r for r in history if r["status"] == "landed"]
    if not landed:
        status = latest["status"] if latest else dependency["status"]
        return _item(id, "not_landed", f"not landed ({status})")
    heads = list(dict.fromkeys(str(r["head_commit"]) for r in landed if r.get("head_commit")))
    if not heads:
        return _item(id, "missing_head", "landed runs have no delivered head commit")
    try:
        base = _base_tip(candidate, bases)
    except _DependencyError as exc:
        return _item(id, exc.reason, f"{exc.reason}: {exc}")
    project = str(candidate["project"])
    errors: list[str] = []
    for head in heads:
        key = (project, head, base)
        if key not in ancestry:
            try:
                result = _git(project, "merge-base", "--is-ancestor", head, base)
                ancestry[key] = (result.returncode, result.stderr.strip())
            except (OSError, subprocess.SubprocessError) as exc:
                ancestry[key] = (2, str(exc))
        code, text = ancestry[key]
        if code == 0:
            return None
        if code != 1:
            errors.append(f"{head}: {text}")
    if errors:
        return _item(id, "git_error", f"git_error: {'; '.join(errors)}")
    return _item(id, "not_in_base", "delivered head not in local base")


def dependency_holds(
    tasks: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    *,
    candidates: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Compute task-specific holds; never mutate git or ledger state."""
    holds: dict[str, dict[str, Any]] = {}
    bases: dict[tuple[str, str], str] = {}
    ancestry: dict[tuple[str, str, str], tuple[int, str]] = {}
    for project in dict.fromkeys(str(c["project"]) for c in candidates):
        indexed = {str(t["id"]): t for t in tasks if t["project"] == project}
        by_key = {t["task_id"]: id for id, t in indexed.items()}
        history: dict[str, list[dict[str, Any]]] = {}
        for run in runs:
            if run["project"] == project and run["task_id"] in by_key:
                history.setdefault(by_key[run["task_id"]], []).append(run)
        graph: dict[str, tuple[str, ...]] = {}
        invalid: dict[str, str] = {}
        for id, row in indexed.items():
            try:
                graph[id] = _snapshot_dependencies(row)
            except ValueError as exc:
                invalid[id] = str(exc)
        for candidate in candidates:
            if candidate["project"] != project or candidate["status"] != "queued":
                continue
            id = str(candidate["id"])
            if id in invalid:
                unmet = [_item(id, "invalid", f"invalid depends_on: {invalid[id]}")]
            else:
                unmet = []
                for dependency in dict.fromkeys(graph.get(id, ())):
                    item = _unmet_dependency(
                        dependency, candidate, indexed, history, graph, invalid, bases, ancestry
                    )
                    if item is not None:
                        unmet.append(item)
            if unmet:
                holds[str(candidate["task_id"])] = {"reason": "depends_on", "unmet": unmet}
    return holds
