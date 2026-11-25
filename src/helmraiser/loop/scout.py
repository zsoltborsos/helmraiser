"""Read-only scout inputs, candidate contracts, draft rendering, and scout execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, Required, TypedDict

from helmraiser.ledger.db import TERMINAL_RUN_STATUSES, Ledger
from helmraiser.ledger.rundir import RunDir
from helmraiser.leeboard import find_lb, lb_environment, leeboard_identity
from helmraiser.loop import spend
from helmraiser.loop.preflight import (
    PreflightError,
    create_throwaway_worktree,
    remove_throwaway_worktree,
    run_preflight_steps,
)
from helmraiser.loop.preflight import classify_preflight_step as classify_preflight_step
from helmraiser.loop.preflight import failed_scout_preflight as failed_scout_preflight
from helmraiser.loop.preflight import normalize_scout_preflight as normalize_scout_preflight
from helmraiser.loop.redact import redact_output, step_environment_values
from helmraiser.loop.routing import calculate_headroom
from helmraiser.pi.adapter import session_id_for
from helmraiser.pi.directives import render_scout_directives
from helmraiser.pi.protocol import PiAdapter, PiEvent, PiRequest
from helmraiser.pi.skills import stage_skill_args
from helmraiser.pipeline.run_identity import resolve_run_id
from helmraiser.project_notes import (
    append_project_notes,
    inside_without_symlinks,
    read_project_notes,
)
from helmraiser.task.loader import TaskLoadError, origin_from_text, parse_task, write_task
from helmraiser.task.model import ID_PATTERN, ModelSpec, TaskSpec, VerifyStep
from helmraiser.task.paths import matches_paths, parse_paths
from helmraiser.task.project import effective_env, load_project_defaults, worktree_manager
from helmraiser.task.schema import validate_task
from helmraiser.utils.markdown import parse_frontmatter, serialize_frontmatter

SCOUT_TOOLS = ("read", "grep", "find", "ls")
DROP_CLASSES = ("wrong", "unwanted", "duplicate", "unusable")
PREPARATION_STOPPED_NOTE = "preparation stopped: the draft changed after Helmraiser last wrote it"
_CANDIDATE_KEYS = frozenset(
    {
        "id",
        "title",
        "why",
        "evidence",
        "files",
        "change",
        "verify_hints",
        "size",
        "needs_owner_ruling",
        "ruling_question",
    }
)
_EVIDENCE = re.compile(r"^(.+):([1-9][0-9]*)$")
_STATE_NAMES = ("CURRENT_STATE.md", "TASKS.md", "STATUS.md")


class ScoutVerifyHint(TypedDict, total=False):
    command: Required[str]
    timeout: int
    red_on_base: bool
    red_output: str
    purpose: str


@dataclass(frozen=True)
class ScoutCandidate:
    id: str
    title: str
    why: str
    evidence: tuple[str, ...]
    files: tuple[str, ...]
    change: str
    verify_hints: tuple[str | ScoutVerifyHint, ...]
    size: str
    needs_owner_ruling: bool
    ruling_question: str | None


class IntakeResult(Protocol):
    spec: TaskSpec
    proposal: dict[str, Any] | None
    notes: list[str]
    errors: list[str]


class IntakeCallback(Protocol):
    def __call__(self, spec: TaskSpec, *, feedback: str | None = None) -> IntakeResult: ...


class ScoutError(Exception):
    """A scout cannot start or violated its output contract."""

    def __init__(self, message: str, *, wait_reason: str | None = None):
        super().__init__(message)
        self.wait_reason = wait_reason


def _state_documents(repo: Path) -> list[dict[str, str]]:
    repo_real = repo.resolve()
    relatives = [Path(name) for name in _STATE_NAMES]
    projects = repo / "projects"
    if projects.is_dir() and inside_without_symlinks(repo_real, projects, Path("projects")):
        for child in sorted(path for path in projects.iterdir() if path.is_dir()):
            relatives.extend(Path("projects") / child.name / name for name in _STATE_NAMES)
    paths = [
        repo / relative
        for relative in relatives
        if (repo / relative).is_file()
        and inside_without_symlinks(repo_real, repo / relative, relative)
    ]
    documents = []
    for path in sorted(paths, key=lambda p: p.relative_to(repo).as_posix()):
        documents.append(
            {"path": path.relative_to(repo).as_posix(), "content": path.read_text(encoding="utf-8")}
        )
    return documents


def _recent_log(repo: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "log", "-40", "--oneline"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScoutError(f"git log failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ScoutError(f"git log failed: {detail}")
    return result.stdout.splitlines()


def head_commit(repo: Path) -> str:
    """Return the repository's current commit, or raise a user-facing scout error."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScoutError(f"git rev-parse HEAD failed: {exc}") from exc
    commit = result.stdout.strip()
    if result.returncode != 0 or not commit:
        detail = (result.stderr or result.stdout).strip() or "empty result"
        raise ScoutError(f"git rev-parse HEAD failed: {detail}")
    return commit


def _brief_titles(value: Any) -> list[str]:
    values = value if isinstance(value, list) else []
    titles: list[str] = []
    for item in values:
        title = item.get("title") if isinstance(item, dict) else None
        if isinstance(title, str) and title.strip():
            titles.append(title.strip())
    return titles


def _leeboard_brief(repo: Path) -> dict[str, list[str]] | None:
    try:
        executable = find_lb()
        if executable is None:
            return None
        env = lb_environment(leeboard_identity(None))
        env.pop("LEEBOARD_SESSION", None)  # Scouts must not write a session cursor.
        result = subprocess.run(
            [executable, "brief", "--cwd", str(repo), "--json"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=30,
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    filtered = {
        "holds": _brief_titles(payload.get("holds")),
        "grants": _brief_titles(payload.get("grants")),
        "rules": _brief_titles(payload.get("rules")),
    }
    return filtered


def draft_drop_class(frontmatter: dict[str, Any]) -> str | None:
    value = frontmatter.get("drop_class")
    return value if isinstance(value, str) and value in DROP_CLASSES else None


def collect_scout_inputs(
    repo: Path,
    ledger: Ledger,
    exclusions: tuple[str, ...],
    project_notes: str | None = None,
) -> dict[str, Any]:
    project = str(repo.resolve())
    tasks = ledger.list_tasks(project=project)
    task_rows = [
        {"id": str(row["id"]), "title": str(row["title"]), "status": str(row["status"])}
        for row in tasks
    ]
    dropped = []
    for row in tasks:
        if row["status"] != "dropped":
            continue
        parsed = parse_frontmatter(str(row["snapshot"]))
        frontmatter = parsed.get("frontmatter") or {}
        dropped.append(
            {
                "id": str(row["id"]),
                "title": str(row["title"]),
                "reason": str(frontmatter.get("drop_reason") or ""),
                "class": draft_drop_class(frontmatter),
            }
        )
    return {
        "project": project,
        "state_documents": _state_documents(repo),
        "recent_commits": _recent_log(repo),
        "tasks": task_rows,
        "dropped_drafts": dropped,
        "exclusions": list(exclusions),
        "leeboard": _leeboard_brief(repo),
        "project_notes": project_notes,
    }


def build_scout_prompt(inputs: dict[str, Any], cap: int) -> str:
    return (
        f"Find at most {cap} useful candidate tasks. Respect the supplied owner context and "
        "avoid every existing or dropped task. Treat project_notes as owner-maintained "
        "constraints on every candidate, especially its verify_hints. Return only the JSON "
        "list required by the system directive.\n\nScout inputs:\n"
        + json.dumps(inputs, indent=2, sort_keys=True, ensure_ascii=False)
    )


def _safe_relative_path(value: str, exclusions: tuple[str, ...]) -> bool:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    canonical = path.as_posix()
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:/", normalized)
        or path.is_absolute()
        or ".." in path.parts
        or canonical == "."
    ):
        return False
    return not any(
        canonical == prefix or canonical.startswith(prefix + "/") for prefix in exclusions
    )


def _strings(value: Any, field: str, *, nonempty: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or (nonempty and not value):
        requirement = "a non-empty list" if nonempty else "a list"
        raise ScoutError(f"candidate {field} must be {requirement} of strings")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ScoutError(f"candidate {field} must contain non-empty strings")
    return tuple(item.strip() for item in value)


def _verify_hints(value: Any) -> tuple[str | ScoutVerifyHint, ...]:
    if not isinstance(value, list):
        raise ScoutError("candidate verify_hints must be a list")
    hints: list[str | ScoutVerifyHint] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            hints.append(item.strip())
            continue
        if not isinstance(item, dict):
            raise ScoutError("candidate verify_hints must contain non-empty strings or objects")
        for key in ("command", "run", "red_output", "purpose"):
            if key in item and not isinstance(item[key], str):
                raise ScoutError(f"candidate verify_hints {key} must be a string")
        command = item.get("command", "")
        if not command.strip():
            command = item.get("run", "")
        if not command.strip():
            raise ScoutError("candidate verify_hints requires a non-empty command or run")
        hint: ScoutVerifyHint = {"command": command}
        if "timeout" in item:
            if type(item["timeout"]) is not int or item["timeout"] <= 0:
                raise ScoutError("candidate verify_hints timeout must be a positive integer")
            hint["timeout"] = item["timeout"]
        if "red_on_base" in item:
            if not isinstance(item["red_on_base"], bool):
                raise ScoutError("candidate verify_hints red_on_base must be a boolean")
            hint["red_on_base"] = item["red_on_base"]
        if "red_output" in item:
            if not item["red_output"].strip():
                raise ScoutError("candidate verify_hints red_output must be a non-empty string")
            hint["red_output"] = item["red_output"]
        if "purpose" in item:
            hint["purpose"] = item["purpose"]
        hints.append(hint)
    return tuple(hints)


def _render_verify_hint(hint: str | ScoutVerifyHint) -> str:
    if isinstance(hint, str):
        return f"- {hint}"
    markers = []
    if hint.get("red_on_base"):
        markers.append("red_on_base")
    if "red_output" in hint:
        markers.append("red_output: " + json.dumps(hint["red_output"], ensure_ascii=False))
    if "timeout" in hint:
        markers.append(f"timeout: {hint['timeout']} s")
    label = hint.get("purpose", "").strip() or "Check"
    if markers:
        label += " (" + ", ".join(markers) + ")"
    command = "\n".join("  " + line for line in hint["command"].split("\n"))
    # A longer fence also preserves commands containing Markdown fences.
    fence = "`" * max(3, max((len(m[0]) + 1 for m in re.finditer(r"`+", command)), default=3))
    return f"- {label}\n\n  {fence}sh\n{command}\n  {fence}"


def parse_candidates(
    text: str, exclusions: tuple[str, ...], *, rejected: list[str] | None = None
) -> list[ScoutCandidate]:
    try:
        payload = json.loads(text.strip())
    except ValueError as exc:
        raise ScoutError(f"malformed scout JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ScoutError("scout response must be a JSON list")
    candidates: list[ScoutCandidate] = []
    for number, raw in enumerate(payload, start=1):
        try:
            candidates.append(_parse_candidate(raw, number, exclusions))
        except ScoutError as exc:
            candidate_id = raw.get("id") if isinstance(raw, dict) else None
            label = (
                candidate_id.strip()
                if isinstance(candidate_id, str) and candidate_id.strip()
                else str(number)
            )
            if rejected is not None:
                rejected.append(f"{label}: {exc}")
    return candidates


def _parse_candidate(raw: Any, number: int, exclusions: tuple[str, ...]) -> ScoutCandidate:
    if not isinstance(raw, dict):
        raise ScoutError(f"candidate {number} must be an object")
    if set(raw) != _CANDIDATE_KEYS:
        missing = sorted(_CANDIDATE_KEYS - set(raw))
        extra = sorted(set(raw) - _CANDIDATE_KEYS)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unexpected " + ", ".join(extra))
        raise ScoutError(f"candidate {number} has invalid keys ({'; '.join(detail)})")
    candidate_id = raw["id"]
    if not isinstance(candidate_id, str) or not ID_PATTERN.fullmatch(candidate_id):
        raise ScoutError(f"candidate {number} id must be a safe task id")
    strings: dict[str, str] = {}
    for field in ("title", "why", "change"):
        value = raw[field]
        if not isinstance(value, str) or not value.strip():
            raise ScoutError(f"candidate {candidate_id} {field} must be a non-empty string")
        strings[field] = value.strip()
    evidence = _strings(raw["evidence"], "evidence", nonempty=True)
    for item in evidence:
        match = _EVIDENCE.fullmatch(item)
        if match is None or not _safe_relative_path(match.group(1), exclusions):
            raise ScoutError(f"candidate {candidate_id} has invalid evidence path: {item}")
    files = _strings(raw["files"], "files", nonempty=True)
    for path in files:
        if not _safe_relative_path(path, exclusions):
            raise ScoutError(f"candidate {candidate_id} has invalid file path: {path}")
    verify_hints = _verify_hints(raw["verify_hints"])
    if raw["size"] not in ("S", "M", "L"):
        raise ScoutError(f"candidate {candidate_id} size must be S, M, or L")
    needs_ruling = raw["needs_owner_ruling"]
    if not isinstance(needs_ruling, bool):
        raise ScoutError(f"candidate {candidate_id} needs_owner_ruling must be a boolean")
    question = raw["ruling_question"]
    if question is not None and not isinstance(question, str):
        raise ScoutError(f"candidate {candidate_id} ruling_question must be a string or null")
    question = question.strip() if isinstance(question, str) else None
    if needs_ruling and not question:
        raise ScoutError(f"candidate {candidate_id} needs a non-empty ruling_question")
    return ScoutCandidate(
        id=candidate_id,
        title=strings["title"],
        why=strings["why"],
        evidence=evidence,
        files=files,
        change=strings["change"],
        verify_hints=verify_hints,
        size=str(raw["size"]),
        needs_owner_ruling=needs_ruling,
        ruling_question=question,
    )


def _normalized_title(title: str) -> str:
    return " ".join(title.split()).casefold()


def render_draft(
    candidate: ScoutCandidate,
    repo: Path,
    scout_id: str,
    project_notes: str | None = None,
    lens: str = "state",
) -> str:
    frontmatter = {
        "id": candidate.id,
        "title": candidate.title,
        "project": str(repo.resolve()),
        "policy": "review",
        "isolation": "process",
        "status": "draft",
        "source": f"scout {scout_id}",
        "paths": list(dict.fromkeys(candidate.files)),
        "size": candidate.size,
        "tags": ["scout"] + ([f"scout-{lens}"] if lens != "state" else []),
    }
    hints = candidate.verify_hints or ("Determine during intake.",)
    body = (
        "## Goal\n\n"
        f"{candidate.why}\n\n"
        + "Evidence:\n"
        + "\n".join(f"- `{item}`" for item in candidate.evidence)
        + "\n\n## Change\n\n"
        + candidate.change
        + "\n\nFiles:\n"
        + "\n".join(f"- `{item}`" for item in candidate.files)
        + "\n\n## Verify hints\n\n"
        + "\n".join(_render_verify_hint(item) for item in hints)
        + "\n"
    )
    body = append_project_notes(body, project_notes)
    return serialize_frontmatter(frontmatter, body) + "\n"


def render_scout_report(
    *,
    scout_id: str,
    candidates: list[ScoutCandidate],
    drafted: list[str],
    skipped: list[str],
    usage: dict[str, Any],
    preparations: list[dict[str, Any]],
    error: str | None = None,
    raw_tail: str | None = None,
    rejected: list[str] | None = None,
    lens: str = "state",
) -> str:
    lines = [f"# Scout {scout_id}", "", f"Lens: {lens}", "", "## Candidates", ""]
    lines.extend(f"- `{item.id}` — {item.title} ({item.size})" for item in candidates)
    if not candidates:
        lines.append("- None.")
    lines += ["", "## Drafted", ""]
    lines.extend(f"- `{item}`" for item in drafted)
    if not drafted:
        lines.append("- None.")
    lines += ["", "## Draft preparation", ""]
    for item in preparations:
        lines.append(f"### `{item['task_id']}`")
        lines.append("")
        lines.append(f"- Intake: {item['intake_status']}")
        if item.get("intake_error"):
            lines.append(f"- Intake error: {item['intake_error']}")
        for note in item.get("intake_notes", []):
            lines.append(f"- Intake note: {note}")
        lines.append("- Proposal:")
        lines.append(f"```json\n{json.dumps(item.get('proposal'), indent=2, sort_keys=True)}\n```")
        preflight = item.get("preflight")
        lines.append(f"- Preflight: {preflight['summary'] if preflight else 'not run'}")
        if preflight and preflight.get("error"):
            lines.append(f"- Preflight error: {preflight['error']}")
        for step in (preflight or {}).get("steps", []):
            lines.append(
                f"  - {step['name']}: exit {step['exit_code']}, {step['seconds']:.2f}s, "
                f"{step['class']}; hint={step['hint'] or 'none'}"
            )
            if step["tail"]:
                lines.append(f"```text\n{step['tail']}\n```")
        lines.append("")
    if not preparations:
        lines.append("- None.")
    lines += ["", "## Skipped duplicates", ""]
    lines.extend(f"- {item}" for item in skipped)
    if not skipped:
        lines.append("- None.")
    lines += ["", "## Rejected candidates", ""]
    lines.extend(f"- {item}" for item in rejected or [])
    if not rejected:
        lines.append("- None.")
    lines += ["", "## Owner rulings", ""]
    rulings = [item for item in candidates if item.needs_owner_ruling]
    lines.extend(f"- **{item.title}:** {item.ruling_question}" for item in rulings)
    if not rulings:
        lines.append("- None.")
    lines += ["", "## Usage", "", f"```json\n{json.dumps(usage, sort_keys=True)}\n```"]
    if error is not None:
        lines += ["", "## Failure", "", error]
    if raw_tail:
        lines += ["", "## Raw response tail", "", f"```text\n{raw_tail}\n```"]
    return "\n".join(lines) + "\n"


def candidate_dict(candidate: ScoutCandidate) -> dict[str, Any]:
    """JSON-ready candidate data for reports and CLI responses."""
    return asdict(candidate)


@dataclass(frozen=True)
class ScoutOutcome:
    scout_id: str
    outcome: str
    written_task_ids: tuple[str, ...]
    written_paths: tuple[str, ...]
    rulings: tuple[dict[str, str], ...]
    skipped_duplicates: tuple[str, ...]
    report_path: str
    usage: dict[str, Any]
    error: str | None = None

    lens: str = "state"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def draft_rows(
    ledger: Ledger, *, project: str | None = None, now: float | None = None
) -> list[dict[str, Any]]:
    current = time.time() if now is None else now
    rows = []
    for row in ledger.list_tasks(status="draft", project=project):
        parsed = parse_frontmatter(str(row["snapshot"]))
        frontmatter = parsed.get("frontmatter") or {}
        source = str(frontmatter.get("source") or "")
        preparation = ledger.get_scout_draft(str(row["task_id"]))
        scout = ledger.get_scout(preparation["scout_id"]) if preparation else None
        path = Path(str(row["source_path"])).resolve()
        try:
            origin = origin_from_text(path.read_text(encoding="utf-8"))
        except (OSError, TaskLoadError):
            origin = None
        rows.append(
            {
                "task_id": str(row["task_id"]),
                "project": str(row["project"]),
                "path": str(path),
                "origin": origin,
                "id": str(row["id"]),
                "title": str(row["title"]),
                "size": str(frontmatter.get("size") or "?"),
                "lens": scout["lens"] if scout else "state",
                "source": source,
                "kind": "scout" if source.startswith("scout") else "manual",
                "age": max(0, int(current - float(row["created_at"]))),
                "intake_status": preparation["intake_status"] if preparation else "not_run",
                "intake_error": preparation["intake_error"] if preparation else None,
                "intake_notes": preparation["intake_notes"] if preparation else [],
                "proposal": preparation["proposal"] if preparation else None,
                "preflight": preparation["preflight"] if preparation else None,
            }
        )
    return sorted(rows, key=lambda item: (item["project"], item["id"]))


def resolve_draft(ledger: Ledger, task: str) -> dict[str, Any]:
    tasks = ledger.list_tasks()
    exact = [row for row in tasks if row["task_id"] == task]
    if exact:
        matches = exact
    else:
        matches = [row for row in tasks if row["id"] == task]
        if len(matches) > 1:
            ids = ", ".join(sorted(str(row["task_id"]) for row in matches))
            raise ScoutError(f"ambiguous task id: {task}; matches: {ids}")
    if not matches:
        raise ScoutError(f"unknown task: {task}")
    row = matches[0]
    if row["status"] != "draft":
        raise ScoutError(f"task is not a draft: {row['task_id']} (status {row['status']})")
    return row


def drop_draft(
    ledger: Ledger,
    row: dict[str, Any],
    reason: str,
    now: float | None = None,
    *,
    drop_class: str | None = None,
) -> dict[str, Any]:
    if drop_class is not None and drop_class not in DROP_CLASSES:
        raise ScoutError(f"drop class must be one of: {', '.join(DROP_CLASSES)}")
    reason = reason.strip()
    if not reason:
        raise ScoutError("drop reason must not be blank")
    source = Path(str(row["source_path"]))
    if not source.is_file():
        raise ScoutError(f"draft file not found: {source}")
    text = source.read_text(encoding="utf-8")
    parsed = parse_frontmatter(text)
    frontmatter = parsed.get("frontmatter") or {}
    spec = parse_task(text, source.resolve(), {})
    if spec.task_id != row["task_id"]:
        raise ScoutError(f"draft file identity does not match ledger task {row['task_id']}")
    timestamp = time.time() if now is None else now
    frontmatter["status"] = "dropped"
    frontmatter["drop_reason"] = reason
    if drop_class is not None:
        frontmatter["drop_class"] = drop_class
    frontmatter["dropped_at"] = datetime.fromtimestamp(timestamp, tz=UTC).isoformat()
    updated = serialize_frontmatter(frontmatter, str(parsed["body"])) + "\n"
    dropped = parse_task(updated, source.resolve(), {})
    errors = validate_task(dropped)
    if errors:
        raise ScoutError(f"dropped draft is invalid: {'; '.join(errors)}")
    source.write_text(updated, encoding="utf-8")
    ledger.upsert_task(dropped, updated)
    result = dict(row)
    result.update(
        status="dropped",
        drop_reason=reason,
        drop_class=drop_class,
        dropped_at=frontmatter["dropped_at"],
    )
    return result


def _routing_scout(config: dict[str, Any]) -> dict[str, Any]:
    routing = config.get("routing") or {}
    scout = routing.get("scout") or {}
    if not isinstance(scout, dict):
        raise ScoutError("routing.scout must be an object")
    return scout


def _configured_projects(values: Any) -> set[Path]:
    if not isinstance(values, list):
        return set()
    return {Path(str(value)).expanduser().resolve() for value in values}


def draft_acceptance_scope_hash(snapshot: str) -> str:
    """Canonical acceptance/scope fingerprint; null collections become empty.

    Preserve list order and internal body whitespace. Only exact paths recorded
    as intake-added related tests are removed; other intake metadata is ignored.
    """
    from helmraiser.utils.markdown import parse_frontmatter

    parsed = parse_frontmatter(snapshot.replace("\r\n", "\n").replace("\r", "\n"))
    if parsed["error"]:
        raise ValueError(parsed["error"])
    fm = parsed["frontmatter"]
    added = (fm.get("intake") or {}).get("related_tests_added") or []
    value = {key: fm.get(key) or [] for key in ("verify", "evidence", "paths")}
    value["paths"] = [path for path in value["paths"] if path not in added]
    value["budget"] = fm.get("budget") or {}
    value["body"] = parsed["body"].strip()
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def configured_focus_projects(config: dict[str, Any]) -> tuple[Path, ...]:
    """Resolved focus projects in deterministic supervisor order."""
    return tuple(sorted(_configured_projects(_routing_scout(config).get("focus_projects"))))


def configured_paused_projects(config: dict[str, Any]) -> tuple[Path, ...]:
    """Resolved paused scout projects in deterministic order."""
    return tuple(sorted(_configured_projects(_routing_scout(config).get("paused_projects"))))


def _nonnegative_number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ScoutError(f"{name} must be a non-negative number") from exc
    if not math.isfinite(number) or number < 0:
        raise ScoutError(f"{name} must be a non-negative number")
    return number


def scout_due_at(
    repo: Path,
    ledger: Ledger,
    config: dict[str, Any],
    *,
    lens: str = "state",
    scouts: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
    current_head: str | None = None,
) -> float:
    from helmraiser.loop.scout_lenses import scout_lens_settings

    cooldown = (settings if settings is not None else scout_lens_settings(config, ledger))[lens][
        "cooldown_hours"
    ]
    successful = [
        row
        for row in (scouts if scouts is not None else ledger.list_scouts(str(repo)))
        if (scouts is None or row["project"] == str(repo))
        and row["outcome"] == "succeeded"
        and row["lens"] == lens
    ]
    if not successful:
        return float("-inf")
    latest = max(successful, key=lambda row: float(row["ended_at"] or row["started_at"]))
    last_at = float(latest["ended_at"] or latest["started_at"])
    return last_at + (
        cooldown * 3600
        if latest.get("head_commit")
        == (current_head if current_head is not None else head_commit(repo))
        else 0
    )


def scout_gate(
    repo: Path,
    ledger: Ledger,
    config: dict[str, Any],
    now: float,
    *,
    refresh: bool = True,
    ignore_enabled_and_cooldown: bool = False,
    lens: str = "state",
    automatic: bool = False,
) -> None:
    """Raise ``ScoutError`` unless a scout may start for ``repo`` now. ``refresh=False``
    reads live usage from its cache (the worker checks every tick and must not call the
    usage APIs that often); a scout itself checks again with a fresh reading. Requested
    scouts may skip only the enabled and unchanged-HEAD/cooldown checks."""
    from helmraiser.loop.scout_lenses import lens_text, scout_lens_settings

    lens_text(lens)
    scout = _routing_scout(config)
    resolved = repo.expanduser().resolve()
    enabled = (
        scout_lens_settings(config, ledger)[lens]["auto"]
        if automatic
        else scout.get("enabled", False)
    )
    if not ignore_enabled_and_cooldown and not enabled:
        raise ScoutError("routing.scout.enabled is false")
    if resolved in configured_paused_projects(config):
        raise ScoutError(f"project is paused for scouting: {resolved}")
    if resolved not in configured_focus_projects(config):
        raise ScoutError(f"project is not in routing.scout.focus_projects: {resolved}")
    if ledger.is_paused() is not None:
        raise ScoutError("queue is paused", wait_reason="queue paused")
    if ledger.list_tasks(status="queued", project=str(resolved)):
        raise ScoutError("project has queued work", wait_reason="project busy")
    active = [
        row
        for row in ledger.list_runs(project=str(resolved))
        if str(row["status"]) not in TERMINAL_RUN_STATUSES
    ]
    if active:
        raise ScoutError(
            f"project has a non-terminal run: {active[0]['status']}",
            wait_reason="project busy",
        )
    maximum = _nonnegative_number(scout.get("max_pending_drafts", 3), "max_pending_drafts")
    pending = len(ledger.list_tasks(status="draft", project=str(resolved)))
    if pending >= maximum:
        raise ScoutError(
            f"project already has {pending} pending drafts (maximum {maximum:g})",
            wait_reason="draft cap",
        )
    if not ignore_enabled_and_cooldown:
        if scout_due_at(resolved, ledger, config, lens=lens) > now:
            raise ScoutError("repository HEAD is unchanged and scout cooldown has not elapsed")
    model = str(scout.get("model") or "").strip()
    subscription = spend.provider(model)
    subscriptions = spend.subscriptions(config)
    if not model or subscription not in subscriptions:
        raise ScoutError(
            f"scout subscription is not configured: {subscription or 'unknown'}",
            wait_reason="no headroom",
        )
    live = spend.live_for(ledger, config, now, refresh=refresh)
    usage = spend.usage(ledger, config, now, live=live)
    headroom = calculate_headroom(usage, config, now).get(subscription)
    if not headroom or not headroom.get("has_headroom"):
        raise ScoutError(
            f"scout subscription has no headroom: {subscription}", wait_reason="no headroom"
        )


def effective_scout_limit(requested: int | None, config: dict[str, Any]) -> int:
    maximum = int(_routing_scout(config).get("max_drafts_per_run", 3))
    if maximum < 1:
        raise ScoutError("routing.scout.max_drafts_per_run must be positive")
    if requested is not None and requested < 1:
        raise ScoutError("--max must be positive")
    return min(maximum if requested is None else requested, maximum)


async def _ignore_event(_event: PiEvent) -> None:
    return None


def _usage_dict(tokens_in: int, tokens_out: int, cost_usd: float) -> dict[str, Any]:
    return {"tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost_usd}


def _draft_unchanged(ledger: Ledger, task_id: str, path: Path, written: str) -> bool:
    """True while the draft is still exactly what preparation last wrote.

    Intake and preflight take minutes, so the owner may drop, queue or edit the draft
    meanwhile; preparation must not rewrite it then.
    """
    task = ledger.get_task(task_id)
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return task is not None and task["status"] == "draft" and current == written


def _record_preparation_stopped(ledger: Ledger, task_id: str) -> None:
    """Add the stop note to the scout draft, leaving its intake state as it is."""
    draft = ledger.get_scout_draft(task_id)
    if draft is None:
        return
    ledger.set_scout_draft_intake(
        task_id,
        status=draft["intake_status"],
        proposal=draft["proposal"],
        error=draft["intake_error"],
        notes=[*(draft["intake_notes"] or []), PREPARATION_STOPPED_NOTE],
    )


def _validate_draft_destination(repo: Path, path: Path) -> None:
    """Refuse symlinked task paths and any destination resolving outside ``repo``."""
    task_dir = repo / ".helmraiser" / "tasks"
    for component in (repo / ".helmraiser", task_dir, path):
        if component.is_symlink():
            raise ScoutError(f"unsafe symlink in draft destination: {component}")
    try:
        resolved = path.resolve(strict=False)
    except RuntimeError as exc:
        raise ScoutError(f"unsafe draft destination: {path}") from exc
    if not resolved.is_relative_to(repo.resolve()):
        raise ScoutError(f"draft destination escapes the repository: {path}")


def execute_scout(
    repo: Path,
    ledger: Ledger,
    config: dict[str, Any],
    data_dir: Path,
    adapter: PiAdapter,
    intake: IntakeCallback,
    *,
    requested: int | None = None,
    force: bool = False,
    gate_checked: bool = False,
    now: float | None = None,
    scout_id: str | None = None,
    request_id: int | None = None,
    lens: str = "state",
    automatic: bool = False,
) -> ScoutOutcome:
    """Run one scout session and persist its report, record, and accepted drafts."""
    from helmraiser.loop.scout_lenses import lens_text

    lens_text(lens)
    repo = repo.expanduser().resolve()
    current_time = time.time() if now is None else now
    limit = effective_scout_limit(requested, config)
    if not force and not gate_checked:
        scout_gate(
            repo,
            ledger,
            config,
            current_time,
            ignore_enabled_and_cooldown=request_id is not None,
            lens=lens,
            automatic=automatic,
        )
    scout_config = _routing_scout(config)
    model = ModelSpec.parse(str(scout_config.get("model")))
    commit = head_commit(repo)
    scout_id = scout_id or resolve_run_id(None)
    rundir = RunDir(scout_id, data_dir).create()
    report_path = rundir.path / "scout.md"
    ledger.create_scout(
        {
            "id": scout_id,
            "project": str(repo),
            "model": model.render(),
            "lens": lens,
            "head_commit": commit,
            "request_id": request_id,
            "report_path": str(report_path),
        },
        force=force,
    )
    candidates: list[ScoutCandidate] = []
    written_ids: list[str] = []
    written_paths: list[str] = []
    skipped: list[str] = []
    rejected: list[str] = []
    preparations: list[dict[str, Any]] = []
    usage = _usage_dict(0, 0, 0.0)
    raw_tail: str | None = None
    worktree: Path | None = None
    try:
        defaults = load_project_defaults(repo)
        notes = read_project_notes(repo)
        project_notes = notes.text if notes is not None else None
        inputs = collect_scout_inputs(repo, ledger, defaults.scout_exclude, project_notes)
        prompt = build_scout_prompt(inputs, limit)
        expected_worktree = worktree_manager(repo).path_for("scout", scout_id)
        worktree = expected_worktree
        directives = render_scout_directives(
            scout_id=scout_id,
            project=repo,
            worktree=expected_worktree,
            exclusions=defaults.scout_exclude,
            lens=lens,
        )
        directives_path = rundir.path / "directives-scout.md"
        directives_path.write_text(directives, encoding="utf-8")
        worktree, _head = create_throwaway_worktree(repo, "scout", scout_id)
        request = PiRequest(
            prompt=prompt,
            session_id=session_id_for(scout_id, "scout"),
            model=model,
            cwd=worktree,
            session_dir=rundir.sessions,
            system_prompt_path=directives_path,
            tools=SCOUT_TOOLS,
            context_files=False,
            timeout_s=int((config.get("budget") or {}).get("max_minutes", 90)) * 60,
            stage="scout",
            extra_args=stage_skill_args("scout", worktree),
        )
        result = asyncio.run(adapter.run(request, _ignore_event, scout_id))
        usage = _usage_dict(result.usage.tokens_in, result.usage.tokens_out, result.usage.cost_usd)
        raw_tail = "\n".join(result.final_text.splitlines()[-40:])
        if not result.ok:
            raise ScoutError(result.error_message or result.error_kind or "pi scout failed")
        candidates = parse_candidates(result.final_text, defaults.scout_exclude, rejected=rejected)

        existing = ledger.list_tasks(project=str(repo))
        seen_ids = {str(row["id"]) for row in existing}
        seen_titles = {_normalized_title(str(row["title"])) for row in existing}
        task_dir = repo / ".helmraiser" / "tasks"
        pending: list[tuple[str, tuple[str, ...]]] = []
        for row in sorted(existing, key=lambda row: str(row["id"])):
            if row["status"] == "draft":
                declarations = parse_frontmatter(str(row["snapshot"]))["frontmatter"].get("paths")
                if declarations:
                    pending.append((str(row["id"]), parse_paths(declarations)))
        accepted: list[tuple[str, tuple[str, ...]]] = []
        prepared = []
        for candidate in candidates:
            if candidate.needs_owner_ruling:
                continue
            title_key = _normalized_title(candidate.title)
            if candidate.id in seen_ids or title_key in seen_titles:
                skipped.append(f"{candidate.id}: duplicate id or title")
                continue
            path = task_dir / f"{candidate.id}.md"
            _validate_draft_destination(repo, path)
            if path.exists():
                skipped.append(f"{candidate.id}: task file already exists")
                seen_ids.add(candidate.id)
                seen_titles.add(title_key)
                continue
            files = tuple(dict.fromkeys(candidate.files))
            conflict = None
            for other_id, patterns in pending:
                shared = [file for file in files if matches_paths(file, patterns)]
                if shared:
                    conflict = (other_id, shared)
                    break
            if conflict is None:
                for other_id, other_files in accepted:
                    shared = [file for file in files if file in other_files]
                    if shared:
                        conflict = (other_id, shared)
                        break
            if conflict is not None:
                other_id, shared = conflict
                skipped.append(f"{candidate.id}: overlaps {other_id} on {', '.join(shared)}")
                continue
            text = render_draft(candidate, repo, scout_id, project_notes, lens=lens)
            spec = parse_task(text, path.resolve(), config)
            errors = validate_task(spec)
            if errors:
                raise ScoutError(f"rendered draft {candidate.id} is invalid: {'; '.join(errors)}")
            prepared.append((candidate, path, text, spec))
            accepted.append((candidate.id, files))
            seen_ids.add(candidate.id)
            seen_titles.add(title_key)
            if len(prepared) >= limit:
                break
        for candidate, path, text, spec in prepared:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            ledger.upsert_task(spec, text)
            ledger.create_scout_draft(
                spec.task_id,
                scout_id,
                candidate_dict(candidate),
                acceptance_scope_sha256=draft_acceptance_scope_hash(text),
            )
            written_ids.append(spec.task_id)
            written_paths.append(str(path))
            original_spec = spec
            written = text
            feedback = None
            intake_notes: list[str] = []
            for attempt in range(2):
                try:
                    intake_outcome = (
                        intake(original_spec)
                        if feedback is None
                        else intake(original_spec, feedback=feedback)
                    )
                    if intake_outcome.errors:
                        raise ValueError("; ".join(intake_outcome.errors))
                except Exception as exc:
                    ledger.set_scout_draft_intake(
                        spec.task_id,
                        status="intake_failed",
                        proposal=None,
                        error=str(exc),
                        notes=intake_notes,
                    )
                    break
                if not _draft_unchanged(ledger, spec.task_id, path, written):
                    _record_preparation_stopped(ledger, spec.task_id)
                    break
                intake_notes = list(dict.fromkeys([*intake_notes, *intake_outcome.notes]))
                write_task(intake_outcome.spec, path)
                written = path.read_text(encoding="utf-8")
                ledger.upsert_task(intake_outcome.spec, written)
                ledger.set_scout_draft_hash(spec.task_id, draft_acceptance_scope_hash(written))
                ledger.set_scout_draft_intake(
                    spec.task_id,
                    status="proposed",
                    proposal=intake_outcome.proposal,
                    error=None,
                    notes=intake_notes,
                )
                steps = [
                    *intake_outcome.spec.verify,
                    *(
                        VerifyStep(
                            step.name,
                            step.run,
                            step.timeout,
                            step.sandbox,
                            step.red_on_base,
                            step.red_output,
                        )
                        for step in intake_outcome.spec.evidence
                    ),
                ]
                try:
                    preflight_payload = run_preflight_steps(
                        repo,
                        steps,
                        config=config,
                        data_dir=data_dir,
                    )
                    preflight = normalize_scout_preflight(repo, preflight_payload, candidate.files)
                except PreflightError as exc:
                    preflight = failed_scout_preflight(str(exc))
                ledger.set_scout_draft_preflight(spec.task_id, preflight)
                failed = [
                    (step, check)
                    for step, check in zip(preflight["steps"], steps, strict=False)
                    if step["class"] in {"fail", "sandbox"}
                ]
                if attempt or not failed:
                    break
                if not _draft_unchanged(ledger, spec.task_id, path, written):
                    _record_preparation_stopped(ledger, spec.task_id)
                    break
                intake_notes.append(
                    "re-proposed after a failed acceptance check: "
                    + ", ".join(step["name"] for step, _ in failed)
                )
                secrets = step_environment_values(
                    effective_env(intake_outcome.spec, defaults),
                    defaults.services,
                    preflight_payload.get("services") or [],
                )
                feedback = "\n\n".join(
                    f"Step: {step['name']}\nCommand: {check.run}\n"
                    f"Output:\n{redact_output(step['tail'], secrets)[-2000:]}"
                    for step, check in failed
                )
            preparation = ledger.get_scout_draft(spec.task_id)
            if preparation is not None:
                preparations.append(preparation)
        report_path.write_text(
            render_scout_report(
                scout_id=scout_id,
                lens=lens,
                candidates=candidates,
                drafted=written_ids,
                skipped=skipped,
                usage=usage,
                preparations=preparations,
                rejected=rejected,
            ),
            encoding="utf-8",
        )
        ledger.finish_scout(
            scout_id,
            outcome="succeeded",
            cost_usd=float(usage["cost_usd"]),
            tokens_in=int(usage["tokens_in"]),
            tokens_out=int(usage["tokens_out"]),
            drafts_written=written_ids,
            error=None,
        )
        rulings = tuple(
            {"id": item.id, "title": item.title, "question": item.ruling_question or ""}
            for item in candidates
            if item.needs_owner_ruling
        )
        return ScoutOutcome(
            scout_id=scout_id,
            lens=lens,
            outcome="succeeded",
            written_task_ids=tuple(written_ids),
            written_paths=tuple(written_paths),
            rulings=rulings,
            skipped_duplicates=tuple(skipped),
            report_path=str(report_path),
            usage=usage,
        )
    except Exception as exc:  # persist every started-scout failure before returning
        error = str(exc)
        report_path.write_text(
            render_scout_report(
                scout_id=scout_id,
                lens=lens,
                candidates=candidates,
                drafted=written_ids,
                skipped=skipped,
                usage=usage,
                preparations=preparations,
                error=error,
                raw_tail=raw_tail,
                rejected=rejected,
            ),
            encoding="utf-8",
        )
        ledger.finish_scout(
            scout_id,
            outcome="failed",
            cost_usd=float(usage["cost_usd"]),
            tokens_in=int(usage["tokens_in"]),
            tokens_out=int(usage["tokens_out"]),
            drafts_written=written_ids,
            error=error,
        )
        return ScoutOutcome(
            scout_id=scout_id,
            lens=lens,
            outcome="failed",
            written_task_ids=tuple(written_ids),
            written_paths=tuple(written_paths),
            rulings=(),
            skipped_duplicates=tuple(skipped),
            report_path=str(report_path),
            usage=usage,
            error=error,
        )
    finally:
        if worktree is not None and worktree.exists():
            problem = remove_throwaway_worktree(repo, worktree)
            if problem is not None:
                print(f"helmraiser scout: {problem}", file=sys.stderr)
