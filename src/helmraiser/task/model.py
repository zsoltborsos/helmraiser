"""Task dataclasses (task format, spec §1.4)."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

PRIORITIES: tuple[str, ...] = ("critical", "high", "normal", "low")
PRIORITY_RANK: dict[str, int] = {name: rank for rank, name in enumerate(PRIORITIES)}
STATUSES: tuple[str, ...] = (
    "draft",
    "dropped",
    "intake",
    "queued",
    "running",
    "awaiting_approval",
    "landed",
    "failed",
    "aborted",
)
POLICIES: tuple[str, ...] = ("auto", "review", "approve-plan-first")
DELIVERIES: tuple[str, ...] = ("merge", "pr")
ISOLATIONS: tuple[str, ...] = ("none", "process", "vm")
# A verify / evidence step's ``sandbox``: the only value is ``host``, which runs the
# step outside srt; valid only when the project sets ``require_approval = true``.
STEP_SANDBOXES: tuple[str, ...] = ("host",)
PROFILE_NAMES: tuple[str, ...] = (
    "intake",
    "plan",
    "implement",
    "fallback",
    "review",
    "scout",
    "judge",
)
ORIGIN_KINDS: tuple[str, ...] = ("claude-code", "fo")


class TaskOrigin(TypedDict):
    kind: str
    session: str


def parse_depends_on(value: Any) -> tuple[str, ...]:
    """Validate bare same-project dependency IDs, preserving declaration order."""
    if not isinstance(value, list):
        raise ValueError(f"depends_on must be a list, got {type(value).__name__}")
    for item in value:
        if not isinstance(item, str) or not ID_PATTERN.fullmatch(item):
            raise ValueError(f"depends_on contains invalid task id: {item!r}")
    return tuple(value)


def parse_origin(value: Any) -> TaskOrigin | None:
    """Validate and normalize a task's optional review origin."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("origin must be a mapping")
    if set(value) != {"kind", "session"}:
        raise ValueError("origin must contain exactly kind and session")
    kind = value["kind"]
    if kind not in ORIGIN_KINDS:
        raise ValueError("origin.kind must be one of claude-code, fo")
    session = value["session"]
    if not isinstance(session, str) or not session:
        raise ValueError("origin.session must be a non-empty string")
    if len(session) > 1024:
        raise ValueError("origin.session must be at most 1024 characters")
    if any(unicodedata.category(character) == "Cc" for character in session):
        raise ValueError("origin.session must not contain control characters")
    return {"kind": kind, "session": session}


def project_slug(project: Path | str) -> str:
    """Project path with ``/`` replaced by ``-`` (ledger task_id prefix)."""
    return str(project).replace("/", "-")


def task_key(project: Path | str, task_id: str) -> str:
    """Ledger primary key: ``<project-slug>:<id>``."""
    return f"{project_slug(project)}:{task_id}"


@dataclass(frozen=True)
class VerifyStep:
    name: str
    run: str
    timeout: int = 300
    sandbox: str | None = None  # "host": run unconfined (needs require_approval)
    red_on_base: bool = False
    red_output: str | None = None


@dataclass(frozen=True)
class EvidenceStep:
    name: str
    run: str
    artifacts: tuple[str, ...]
    criteria: str
    timeout: int = 600
    sandbox: str | None = None  # "host": run unconfined (needs require_approval)
    red_on_base: bool = False
    red_output: str | None = None


@dataclass(frozen=True)
class ModelSpec:
    """``provider/id[:thinking]``."""

    model: str
    thinking: str | None = None

    @classmethod
    def parse(cls, value: str | dict[str, Any]) -> ModelSpec:
        if isinstance(value, dict):
            thinking = value.get("thinking")
            return cls(model=str(value["model"]), thinking=str(thinking) if thinking else None)
        text = str(value).strip()
        if ":" in text:
            model, thinking = text.rsplit(":", 1)
            return cls(model=model, thinking=thinking or None)
        return cls(model=text)

    def render(self) -> str:
        return f"{self.model}:{self.thinking}" if self.thinking else self.model


@dataclass(frozen=True)
class Budget:
    max_cost_usd: float
    max_minutes: int
    max_iterations: int


@dataclass(frozen=True)
class TaskSpec:
    id: str
    title: str
    project: Path
    priority: str
    status: str
    policy: str | None
    isolation: str
    models: dict[str, ModelSpec]
    budget: Budget
    verify: tuple[VerifyStep, ...]
    evidence: tuple[EvidenceStep, ...]
    tags: tuple[str, ...]
    body: str
    source_path: Path
    # Frontmatter keys outside the schema (incl. ``intake``), kept for round-trips.
    extra: dict[str, Any] = field(default_factory=dict)
    origin: TaskOrigin | None = None
    # Worktree environment: ``setup`` runs once after the worktree is created;
    # ``env`` is applied to every pi session and verify/evidence command.
    setup: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    delivery: str = "merge"
    base: str | None = None
    plan: bool = False
    # Parsed files always set the explicit roles (possibly empty). None retains
    # compatibility serialization for directly constructed specs without provenance.
    explicit_models: frozenset[str] | None = None
    # Distinguish omitted plan from an explicitly supplied false.
    plan_explicit: bool = False
    depends_on: tuple[str, ...] = ()
    paths: tuple[str, ...] | None = None
    # Records a frontmatter budget mapping, not whether a resolved budget exists.
    budget_explicit: bool = False
    # ``experiment: false`` keeps the task out of the active live experiment.
    experiment: bool = True

    @property
    def task_id(self) -> str:
        return task_key(self.project, self.id)

    @property
    def priority_rank(self) -> int:
        return PRIORITY_RANK.get(self.priority, PRIORITY_RANK["normal"])
