"""Validation of frozen live experiment definitions (not arm execution)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from helmraiser.pi import skills
from helmraiser.task.loader import resolve_models
from helmraiser.task.model import PROFILE_NAMES, ModelSpec


class ExperimentError(ValueError):
    """An invalid experiment definition."""


@dataclass(frozen=True)
class ExperimentDefinition:
    name: str
    arms: dict[str, Any]
    end_pairs: int
    end_at: float | None
    quota_brake: float
    exclude_projects: tuple[str, ...]


def _mapping(value: Any, key: str, allowed: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExperimentError(f"{key}: expected a mapping")
    for field in value:
        if field not in allowed:
            raise ExperimentError(f"{key}.{field}: unknown key")
    return value


def _text(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentError(f"{key}: expected a nonblank string")
    return value


def validate_experiment(definition: Any, config: dict[str, Any]) -> ExperimentDefinition:
    """Normalize overrides without mutating inputs or expanding config profiles."""
    data = _mapping(
        definition,
        "experiment",
        {"name", "arms", "end_pairs", "end_at", "quota_brake", "exclude_projects"},
    )
    name = _text(data.get("name"), "name")
    raw_arms = _mapping(data.get("arms"), "arms", {"A", "B"})
    if set(raw_arms) != {"A", "B"}:
        raise ExperimentError("arms: expected exactly A and B")
    base = resolve_models(None, config)
    arms: dict[str, Any] = {}
    for arm, raw in raw_arms.items():
        key = f"arms.{arm}"
        source = _mapping(raw, key, {"models", "skills", "pi_rpc_implement"})
        normalized: dict[str, Any] = {}
        effective = dict(base)
        if "models" in source:
            models = _mapping(source["models"], f"{key}.models", set(PROFILE_NAMES))
            parsed = {}
            for stage, value in models.items():
                model_key = f"{key}.models.{stage}"
                if isinstance(value, Mapping):
                    spec = _mapping(value, model_key, {"model", "thinking"})
                    _text(spec.get("model"), f"{model_key}.model")
                    if spec.get("thinking") is not None and not isinstance(spec["thinking"], str):
                        raise ExperimentError(f"{model_key}.thinking: expected string or null")
                    value = dict(spec)
                else:
                    _text(value, model_key)
                model = ModelSpec.parse(value)
                _text(model.model, model_key)
                parsed[stage] = {"model": model.model}
                if model.thinking is not None:
                    parsed[stage]["thinking"] = model.thinking
                effective[stage] = model
            normalized["models"] = parsed
        if "skills" in source:
            mode = _text(source["skills"], f"{key}.skills")
            if mode not in {"default", "none", "discover"}:
                if (
                    mode in {".", ".."}
                    or "/" in mode
                    or "\\" in mode
                    or not (skills.SKILL_SETS_ROOT / mode).is_dir()
                ):
                    raise ExperimentError(f"{key}.skills: missing or invalid named skill set")
            normalized["skills"] = mode
        if "pi_rpc_implement" in source:
            if not isinstance(source["pi_rpc_implement"], bool):
                raise ExperimentError(f"{key}.pi_rpc_implement: expected boolean")
            normalized["pi_rpc_implement"] = source["pi_rpc_implement"]
        implement, review = effective.get("implement"), effective.get("review")
        if implement and review and implement.model == review.model:
            raise ExperimentError(f"{key}.models: reviewer must differ from implement model")
        arms[arm] = normalized
    end_pairs = data.get("end_pairs", 30)
    if type(end_pairs) is not int or not 0 < end_pairs <= 9223372036854775807:
        raise ExperimentError("end_pairs: expected a positive integer at most 9223372036854775807")
    quota = data.get("quota_brake", 0.85)
    if (
        isinstance(quota, bool)
        or not isinstance(quota, (int, float))
        or not 0 < quota <= 1
        or not math.isfinite(quota)
    ):
        raise ExperimentError("quota_brake: expected a finite number greater than 0 and at most 1")
    end_at = None
    if data.get("end_at") is not None:
        value = _text(data["end_at"], "end_at")
        try:
            date = datetime.fromisoformat(value)
            end_at = (
                date.replace(tzinfo=UTC).timestamp()
                if date.tzinfo is None
                else date.astimezone(UTC).timestamp()
            )
            # Float rounding at datetime's upper bound can produce year 10000.
            datetime.fromtimestamp(end_at, UTC)
        except (ValueError, OverflowError) as exc:
            raise ExperimentError("end_at: expected an ISO date or datetime") from exc
    exclusions = data.get("exclude_projects", [])
    if not isinstance(exclusions, list):
        raise ExperimentError("exclude_projects: expected a list")
    paths = []
    for index, value in enumerate(exclusions):
        key = f"exclude_projects.{index}"
        try:
            paths.append(str(Path(_text(value, key)).expanduser().resolve()))
        except (ValueError, OSError, RuntimeError) as exc:
            raise ExperimentError(f"{key}: invalid project path: {exc}") from exc
    return ExperimentDefinition(name, arms, end_pairs, end_at, float(quota), tuple(paths))
