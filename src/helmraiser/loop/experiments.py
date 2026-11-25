"""Live experiments: validation of frozen definitions, per-run arm values, and the
decision whether a queued task forks into a pair."""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from helmraiser.ledger.db import Ledger
from helmraiser.loop.spend import live_for, usage
from helmraiser.pi import skills
from helmraiser.task.loader import resolve_models
from helmraiser.task.model import PROFILE_NAMES, ModelSpec, TaskSpec

# Arm A is the control: today's setup.
ARM_NAMES = ("A", "B")
CONTROL_ARM = "A"
# The quota brake reads the codex subscription's weekly window of ``spend.usage``.
BRAKE_PROVIDER = "openai-codex"
BRAKE_WINDOW = "week"


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


@dataclass(frozen=True)
class RunArm:
    """One run's immutable overrides, converted from a validated arm."""

    name: str
    models: Mapping[str, ModelSpec] = field(default_factory=dict)
    skills: str | None = None
    pi_rpc_implement: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "models", MappingProxyType(dict(self.models)))

    @classmethod
    def from_overrides(cls, name: str, overrides: Mapping[str, Any]) -> RunArm:
        return cls(
            name,
            models={
                stage: ModelSpec.parse(value)
                for stage, value in overrides.get("models", {}).items()
            },
            skills=overrides.get("skills"),
            pi_rpc_implement=overrides.get("pi_rpc_implement"),
        )

    def overrides(self) -> dict[str, Any]:
        """A fresh JSON-ready payload, retaining explicitly disabled RPC."""
        result: dict[str, Any] = {}
        if self.models:
            result["models"] = {
                stage: {
                    "model": model.model,
                    **({"thinking": model.thinking} if model.thinking is not None else {}),
                }
                for stage, model in self.models.items()
            }
        if self.skills is not None:
            result["skills"] = self.skills
        if self.pi_rpc_implement is not None:
            result["pi_rpc_implement"] = self.pi_rpc_implement
        return result


def apply_arm(
    task: TaskSpec, arm: RunArm, config: dict[str, Any]
) -> tuple[TaskSpec, dict[str, Any]]:
    """``task`` with the arm's models, and the values the arm replaces (the
    ``replaced`` field of the run's ``experiment_arm`` event)."""
    replaced: dict[str, Any] = {}
    if arm.models:
        replaced["models"] = {
            stage: task.models[stage].render() if stage in task.models else None
            for stage in arm.models
        }
    if arm.skills is not None:
        replaced["skills"] = "default"
    if arm.pi_rpc_implement is not None:
        replaced["pi_rpc_implement"] = bool(config.get("pi_rpc_implement", False))
    return replace(task, models={**task.models, **arm.models}), replaced


@dataclass(frozen=True)
class RunPair:
    """Given to the run that claims a task as a pair (run X): the experiment and the
    arm of the pair's other run (run Y), which that claim records."""

    experiment_id: int
    arm: RunArm


def draw_labels() -> tuple[str, str]:
    """The arms behind the blind labels X and Y: a random draw per task."""
    arm_x, arm_y = random.sample(ARM_NAMES, 2)
    return arm_x, arm_y


@dataclass(frozen=True)
class Fork:
    """How a queued task starts under the active experiment: as a pair, one run per
    arm, or (``paired`` false, at or above the quota brake) as one unpaired run on
    the control arm."""

    experiment_id: int
    arms: Mapping[str, RunArm]
    paired: bool
    # The arms behind X and Y of the pair the task already has (a retried pair), which
    # its new runs keep. None: a new pair, with a new draw.
    labels: tuple[str, str] | None = None

    def tasks(self, task: TaskSpec, config: dict[str, Any]) -> list[TaskSpec]:
        """``task`` as each of its runs would get it, for the checks before a start."""
        names = ARM_NAMES if self.paired else (CONTROL_ARM,)
        return [apply_arm(task, self.arms[name], config)[0] for name in names]

    def runs(self, labels: tuple[str, str] | None = None) -> tuple[RunArm, RunPair | None]:
        """The claiming run's arm and, for a pair, what it records for the other run.
        ``labels`` are the arms behind X and Y (default: those of the task's existing
        pair, else ``draw_labels``)."""
        if not self.paired:
            return self.arms[CONTROL_ARM], None
        arm_x, arm_y = labels or self.labels or draw_labels()
        return self.arms[arm_x], RunPair(self.experiment_id, self.arms[arm_y])


def codex_weekly_fraction(ledger: Ledger, config: dict[str, Any], now: float) -> float | None:
    """The codex weekly usage fraction the quota brake compares; None without a reading."""
    readings = usage(ledger, config, now, live=live_for(ledger, config, now))
    fraction = ((readings.get(BRAKE_PROVIDER) or {}).get(BRAKE_WINDOW) or {}).get("fraction")
    return None if fraction is None else float(fraction)


def fork_decision(
    task: TaskSpec, ledger: Ledger, config: dict[str, Any], now: float
) -> Fork | None:
    """Decide, when a worker is about to claim queued ``task``, whether it forks.

    None: the task runs as without experiments, with no arm. That is so when the
    ledger has no experiment tables yet or no experiment is active, the task sets
    ``experiment: false`` or is plan-approval-first, its project is excluded, or the
    experiment has a pair for it that is picked or has a run that did not fail (a
    re-queued task). Otherwise the task forks into a pair, unless codex weekly usage
    is at or above the experiment's quota brake or has no reading: then it runs
    unpaired on the control arm.

    An unpicked pair whose two runs both ended as failed or aborted (a retried pair)
    is claimed again: the fork carries its labels, and the claim gives its row the new
    runs.

    An active experiment whose ``end_at`` has passed is ended here, with reason
    ``end_at``, and the task does not fork.
    """
    if not ledger.experiments_available:
        return None
    experiment = ledger.active_experiment()
    if experiment is None:
        return None
    if experiment["end_at"] is not None and now >= float(experiment["end_at"]):
        ledger.end_experiment(int(experiment["id"]), "end_at")
        return None
    if task.experiment is False or task.policy == "approve-plan-first":
        return None
    # Exclusions are stored resolved.
    if str(task.project.expanduser().resolve()) in experiment["exclude_projects"]:
        return None
    labels = None
    pair = ledger.get_pair(int(experiment["id"]), task.task_id)
    if pair is not None:
        runs = [ledger.get_run(str(pair[key])) for key in ("run_x", "run_y") if pair[key]]
        if (
            pair["pick"] is not None
            or len(runs) != 2
            or any(run is None or run["status"] not in ("failed", "aborted") for run in runs)
        ):
            return None
        labels = (str(pair["arm_x"]), str(pair["arm_y"]))
    arms = {name: RunArm.from_overrides(name, experiment["arms"][name]) for name in ARM_NAMES}
    fraction = codex_weekly_fraction(ledger, config, now)
    paired = fraction is not None and fraction < float(experiment["quota_brake"])
    return Fork(int(experiment["id"]), arms, paired, labels)


def _mapping(value: Any, key: str, allowed: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExperimentError(f"{key}: expected a mapping")
    for name in value:
        if name not in allowed:
            raise ExperimentError(f"{key}.{name}: unknown key")
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
