"""Load and write task files (spec §1.3–1.4).

``load_task`` resolves model profiles and budgets from config and accepts the
legacy topic sugar (``quality_gates`` strings, old ``status`` values). Files are
never rewritten here; ``write_task`` is used by ``intake`` and status transitions.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from helmraiser.config import DEFAULT_BUDGET, DEFAULT_PROFILES
from helmraiser.task.model import (
    PROFILE_NAMES,
    Budget,
    EvidenceStep,
    ModelSpec,
    TaskOrigin,
    TaskSpec,
    VerifyStep,
    parse_depends_on,
    parse_origin,
)
from helmraiser.task.paths import parse_paths
from helmraiser.task.project import load_project_defaults
from helmraiser.utils.markdown import parse_frontmatter, serialize_frontmatter

SCHEMA_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "title",
        "project",
        "priority",
        "status",
        "policy",
        "plan",
        "delivery",
        "base",
        "isolation",
        "models",
        "budget",
        "verify",
        "evidence",
        "tags",
        "quality_gates",
        "setup",
        "env",
        "origin",
        "depends_on",
        "paths",
    }
)

LEGACY_STATUS: dict[str, str] = {
    "todo": "draft",
    "in_progress": "running",
    "review": "awaiting_approval",
    "done": "landed",
}
LEGACY_PRIORITY: dict[str, str] = {"medium": "normal"}


class TaskLoadError(ValueError):
    """Raised when a task file cannot be turned into a TaskSpec at all."""


def resolve_models(
    overrides: dict[str, Any] | None,
    config: dict[str, Any],
    project_models: Mapping[str, str] | None = None,
) -> dict[str, ModelSpec]:
    """Built-in and config profiles, then a project's ``[models]``, then the task's ``models``.

    A higher layer replaces the whole ``provider/id:thinking`` value.
    """
    profiles: dict[str, Any] = {**DEFAULT_PROFILES, **config.get("profiles", {})}
    resolved: dict[str, ModelSpec] = {}

    def configured(value: Any) -> bool:
        model = value.get("model") if isinstance(value, dict) else value
        return isinstance(model, str) and bool(model.strip())

    for name in PROFILE_NAMES:
        profile = profiles.get(name)
        if profile is not None and configured(profile):
            resolved[name] = ModelSpec.parse(profile)
    for name, value in (project_models or {}).items():
        if configured(value):
            resolved[name] = ModelSpec.parse(value)
    for name, value in (overrides or {}).items():
        if configured(value):
            resolved[str(name)] = ModelSpec.parse(value)
    return resolved


def profile_models(project: Path, config: dict[str, Any]) -> dict[str, ModelSpec]:
    """The profile defaults for a project's tasks: config profiles overlaid with its [models]."""
    return resolve_models(None, config, load_project_defaults(project).models)


def resolve_budget(overrides: dict[str, Any] | None, config: dict[str, Any]) -> Budget:
    merged: dict[str, Any] = {**DEFAULT_BUDGET, **config.get("budget", {}), **(overrides or {})}
    return Budget(
        max_cost_usd=float(merged["max_cost_usd"]),
        max_minutes=int(merged["max_minutes"]),
        max_iterations=int(merged["max_iterations"]),
    )


def _step_sandbox(raw: dict[str, Any]) -> str | None:
    value = raw.get("sandbox")
    return str(value) if value is not None else None


def _step_red_on_base(raw: dict[str, Any], kind: str, n: int) -> bool:
    value = raw.get("red_on_base", False)
    if not isinstance(value, bool):
        name = str(raw.get("name") or f"{kind}-{n}")
        raise TaskLoadError(f"{kind} step '{name}': red_on_base must be a boolean")
    return value


def _verify_steps(fm: dict[str, Any]) -> tuple[VerifyStep, ...]:
    steps: list[VerifyStep] = []
    for n, gate in enumerate(fm.get("quality_gates") or [], start=1):
        steps.append(VerifyStep(name=f"gate-{n}", run=str(gate)))
    for n, raw in enumerate(fm.get("verify") or [], start=1):
        if isinstance(raw, str):
            steps.append(VerifyStep(name=f"verify-{n}", run=raw))
            continue
        if not isinstance(raw, dict):
            raise TaskLoadError(f"verify[{n}] must be a mapping")
        steps.append(
            VerifyStep(
                name=str(raw.get("name") or f"verify-{n}"),
                run=str(raw.get("run") or ""),
                timeout=int(raw.get("timeout", 300)),
                sandbox=_step_sandbox(raw),
                red_on_base=_step_red_on_base(raw, "verify", n),
                red_output=raw.get("red_output"),
            )
        )
    return tuple(steps)


def _criteria(raw: Any) -> str:
    """Evidence criteria as one string; a list of sentences is joined by single spaces."""
    if isinstance(raw, list):
        return " ".join(str(item).strip() for item in raw)
    return str(raw or "").strip()


def _evidence_steps(fm: dict[str, Any]) -> tuple[EvidenceStep, ...]:
    steps: list[EvidenceStep] = []
    for n, raw in enumerate(fm.get("evidence") or [], start=1):
        if not isinstance(raw, dict):
            raise TaskLoadError(f"evidence[{n}] must be a mapping")
        artifacts = raw.get("artifacts") or []
        if isinstance(artifacts, str):
            artifacts = [artifacts]
        steps.append(
            EvidenceStep(
                name=str(raw.get("name") or f"evidence-{n}"),
                run=str(raw.get("run") or ""),
                artifacts=tuple(str(a) for a in artifacts),
                criteria=_criteria(raw.get("criteria")),
                timeout=int(raw.get("timeout", 600)),
                sandbox=_step_sandbox(raw),
                red_on_base=_step_red_on_base(raw, "evidence", n),
                red_output=raw.get("red_output"),
            )
        )
    return tuple(steps)


def origin_from_text(content: str) -> TaskOrigin | None:
    """Read and validate an optional origin from task-file text."""
    parsed = parse_frontmatter(content)
    if parsed.get("error"):
        raise TaskLoadError(str(parsed["error"]))
    try:
        return parse_origin(parsed["frontmatter"].get("origin"))
    except ValueError as exc:
        raise TaskLoadError(str(exc)) from exc


def parse_task(content: str, source_path: Path, config: dict[str, Any]) -> TaskSpec:
    """Build a TaskSpec from markdown text (see ``load_task``)."""
    parsed = parse_frontmatter(content)
    if parsed.get("error"):
        raise TaskLoadError(f"{source_path}: {parsed['error']}")
    fm: dict[str, Any] = parsed["frontmatter"]
    if not fm:
        raise TaskLoadError(f"no frontmatter found in {source_path}")

    status = str(fm.get("status") or "draft")
    status = LEGACY_STATUS.get(status, status)
    priority = str(fm.get("priority") or "normal")
    priority = LEGACY_PRIORITY.get(priority, priority)
    policy = fm.get("policy")
    tags = fm.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    project_raw = fm.get("project")
    project = Path(str(project_raw)).expanduser() if project_raw else Path("")
    project_defaults = load_project_defaults(project) if project.is_absolute() else None
    project_delivery = project_defaults.delivery if project_defaults else None
    project_base = project_defaults.base if project_defaults else None
    base_raw = fm["base"] if "base" in fm else project_base

    models_raw = fm.get("models")
    budget_raw = fm.get("budget")
    extra = {k: v for k, v in fm.items() if k not in SCHEMA_KEYS}
    setup_raw = fm.get("setup")
    env_raw = fm.get("env") or {}
    if not isinstance(env_raw, dict):
        raise TaskLoadError("env must be a mapping of KEY: VALUE")
    try:
        origin = parse_origin(fm.get("origin"))
        depends_on = parse_depends_on(fm.get("depends_on", []))
        paths = parse_paths(fm["paths"]) if "paths" in fm else None
    except ValueError as exc:
        raise TaskLoadError(str(exc)) from exc

    return TaskSpec(
        id=str(fm.get("id") or ""),
        title=str(fm.get("title") or ""),
        project=project,
        priority=priority,
        status=status,
        policy=str(policy) if policy else None,
        isolation=str(fm.get("isolation") or "process"),
        models=resolve_models(
            models_raw if isinstance(models_raw, dict) else None,
            config,
            project_defaults.models if project_defaults else None,
        ),
        budget=resolve_budget(budget_raw if isinstance(budget_raw, dict) else None, config),
        budget_explicit=isinstance(budget_raw, dict),
        verify=_verify_steps(fm),
        evidence=_evidence_steps(fm),
        tags=tuple(str(t) for t in tags),
        body=parsed["body"],
        source_path=source_path,
        extra=extra,
        origin=origin,
        setup=str(setup_raw).strip() if setup_raw else None,
        env={str(k): str(v) for k, v in env_raw.items()},
        delivery=str(fm.get("delivery") or project_delivery or "merge"),
        base=str(base_raw).strip() if base_raw is not None else None,
        plan=fm.get("plan", False),
        explicit_models=(
            frozenset(str(name) for name, value in models_raw.items() if value)
            if isinstance(models_raw, dict)
            else frozenset()
        ),
        plan_explicit="plan" in fm,
        depends_on=depends_on,
        paths=paths,
    )


def load_task(path: Path | str, config: dict[str, Any]) -> TaskSpec:
    """Load a task file, applying profile resolution and legacy mapping."""
    source = Path(path)
    if not source.is_file():
        raise TaskLoadError(f"task file not found: {source}")
    return parse_task(source.read_text(encoding="utf-8"), source.resolve(), config)


def spec_to_frontmatter(spec: TaskSpec) -> dict[str, Any]:
    fm: dict[str, Any] = {
        "id": spec.id,
        "title": spec.title,
        "project": str(spec.project),
        "priority": spec.priority,
        "status": spec.status,
        "isolation": spec.isolation,
        "budget": {
            "max_cost_usd": spec.budget.max_cost_usd,
            "max_minutes": spec.budget.max_minutes,
            "max_iterations": spec.budget.max_iterations,
        },
        "verify": [
            {
                "name": s.name,
                "run": s.run,
                "timeout": s.timeout,
                **({"sandbox": s.sandbox} if s.sandbox is not None else {}),
                **({"red_on_base": True} if s.red_on_base else {}),
                **({"red_output": s.red_output} if s.red_output is not None else {}),
            }
            for s in spec.verify
        ],
    }
    roles = (
        ("plan", "implement", "fallback", "review")
        if spec.explicit_models is None
        else sorted(spec.explicit_models)
    )
    models = {name: spec.models[name].render() for name in roles if name in spec.models}
    if models:
        fm["models"] = models
    if spec.policy is not None:
        fm["policy"] = spec.policy
    if spec.paths is not None:
        fm["paths"] = list(spec.paths)
    if spec.depends_on:
        fm["depends_on"] = list(spec.depends_on)
    if spec.origin is not None:
        fm["origin"] = dict(spec.origin)
    if spec.plan_explicit or spec.plan is True:
        fm["plan"] = spec.plan
    fm["delivery"] = spec.delivery
    if spec.base is not None:
        fm["base"] = spec.base
    if spec.evidence:
        fm["evidence"] = [
            {
                "name": e.name,
                "run": e.run,
                "artifacts": list(e.artifacts),
                "criteria": e.criteria,
                "timeout": e.timeout,
                **({"sandbox": e.sandbox} if e.sandbox is not None else {}),
                **({"red_on_base": True} if e.red_on_base else {}),
                **({"red_output": e.red_output} if e.red_output is not None else {}),
            }
            for e in spec.evidence
        ]
    if spec.tags:
        fm["tags"] = list(spec.tags)
    if spec.setup:
        fm["setup"] = spec.setup
    if spec.env:
        fm["env"] = dict(spec.env)
    for key, value in spec.extra.items():
        fm.setdefault(key, value)
    return fm


def render_task(spec: TaskSpec) -> str:
    return serialize_frontmatter(spec_to_frontmatter(spec), spec.body) + "\n"


def write_task(spec: TaskSpec, path: Path | str) -> Path:
    """Write ``spec`` as a task file; ``load_task`` of the result round-trips."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_task(spec), encoding="utf-8")
    return target
