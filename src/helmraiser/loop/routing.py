"""Pure paced subscription headroom calculations and fresh-dispatch model routing."""

from __future__ import annotations

import json
from calendar import monthrange
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from math import isfinite
from typing import Any

from helmraiser.loop import spend
from helmraiser.task.loader import profile_models
from helmraiser.task.model import ModelSpec, TaskSpec


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _live_start(window: str, resets_at: float) -> float | None:
    if window == "5h":
        return resets_at - spend.FIVE_HOURS
    if window == "week":
        return resets_at - spend.WEEK_SECONDS
    if window == "month":
        reset = datetime.fromtimestamp(resets_at, tz=UTC)
        if reset.month == 1:
            year, month = reset.year - 1, 12
        else:
            year, month = reset.year, reset.month - 1
        day = min(reset.day, monthrange(year, month)[1])
        return reset.replace(year=year, month=month, day=day).timestamp()
    return None


def calculate_headroom(
    usage_payload: dict[str, Any],
    config: dict[str, Any],
    now: float,
) -> dict[str, dict[str, Any]]:
    """Return paced allowance headroom for every configured or reported subscription."""
    routing = config.get("routing") or {}
    configured_target = _number(routing.get("target"))
    target_default = 0.95 if configured_target is None else configured_target
    configured_margin = _number(routing.get("margin"))
    margin = 0.02 if configured_margin is None else configured_margin
    configured_ramp = _number(routing.get("ramp_hours"))
    ramp_seconds = 24 * 3600 if configured_ramp is None else configured_ramp * 3600
    subscriptions = spend.subscriptions(config)
    result: dict[str, dict[str, Any]] = {}

    for name in sorted(set(subscriptions) | set(usage_payload)):
        sub_config = subscriptions.get(name) or {}
        stop_at = _number(sub_config.get("stop_at"))
        target = min(target_default, stop_at) if stop_at is not None else target_default
        usage_windows = usage_payload.get(name)
        output: dict[str, Any] = {"has_headroom": False}
        calculated: list[float | None] = []
        if isinstance(usage_windows, dict):
            for window in spend.SUBSCRIPTION_WINDOWS:
                reading = usage_windows.get(window)
                if not isinstance(reading, dict):
                    continue
                used = _number(reading.get("fraction"))
                resets_at = _number(reading.get("resets_at"))
                elapsed: float | None = None
                headroom: float | None = None
                if resets_at is not None and resets_at >= now:
                    if reading.get("source") == "live":
                        start = _live_start(window, resets_at)
                    else:
                        try:
                            start = spend.window_start(window, now, sub_config)
                        except (TypeError, ValueError, OverflowError):
                            start = None
                    if start is not None and start <= now and resets_at > start:
                        elapsed = (now - start) / (resets_at - start)
                        if used is not None:
                            if resets_at - now <= ramp_seconds:
                                headroom = target - used
                            else:
                                headroom = target * elapsed - used
                rounded_headroom = None if headroom is None else round(headroom, 6)
                output[window] = {
                    "used": None if used is None else round(used, 6),
                    "elapsed": None if elapsed is None else round(elapsed, 6),
                    "target": round(target, 6),
                    "headroom": rounded_headroom,
                }
                calculated.append(rounded_headroom)
        output["has_headroom"] = bool(calculated) and all(
            value is not None and value > margin for value in calculated
        )
        result[name] = output
    return result


def _under_max_used(figures: dict[str, Any], max_used: Any) -> bool:
    """Every reported window is used below ``max_used``, whatever the pace."""
    cap = _number(max_used)
    readings = [window.get("used") for window in figures.values() if isinstance(window, dict)]
    return (
        cap is not None
        and bool(readings)
        and all(used is not None and used < cap for used in readings)
    )


def overflow_setting(config: dict[str, Any]) -> str | None:
    """Return the configured overflow model, or None when unset."""
    setting = ((config.get("routing") or {}).get("implement") or {}).get("overflow")
    return setting.strip() or None if isinstance(setting, str) else None


def overflow_at(config: dict[str, Any]) -> float:
    """Return the usage threshold for a configured overflow implementer."""
    threshold = _number(((config.get("routing") or {}).get("implement") or {}).get("overflow_at"))
    return 0.9 if threshold is None else threshold


def _near_limit(figures: dict[str, Any], overflow_at: float) -> bool:
    return any(
        used is not None and used >= overflow_at
        for window in figures.values()
        if isinstance(window, dict)
        for used in [_number(window.get("used"))]
    )


def _measured(figures: dict[str, Any]) -> bool:
    return any(
        isinstance(window, dict) and _number(window.get("used")) is not None
        for window in figures.values()
    )


def _has_room(figures: dict[str, Any]) -> bool:
    return _measured(figures) and all(
        target is not None and used < target
        for window in figures.values()
        if isinstance(window, dict)
        for used in [_number(window.get("used"))]
        if used is not None
        for target in [_number(window.get("target"))]
    )


def _overflow_outcome(
    overflow: ModelSpec,
    default: ModelSpec,
    reviewer: ModelSpec | None,
    figures: dict[str, Any],
    config: dict[str, Any],
    held_providers: Mapping[str, Any],
) -> tuple[str, str | None]:
    if "/" not in overflow.model:
        return "invalid_overflow", None
    implement_provider = spend.provider(default.model)
    overflow_provider = spend.provider(overflow.model)
    if overflow_provider == implement_provider:
        return "overflow_same_provider", None
    implement_figures = figures[implement_provider]
    if implement_provider in held_providers:
        trigger = "provider_held"
    elif _near_limit(implement_figures, overflow_at(config)):
        trigger = "near_limit"
    else:
        return (
            "overflow_not_needed" if _measured(implement_figures) else "overflow_usage_unknown",
            None,
        )
    if reviewer is not None and reviewer.model == overflow.model:
        return "reviewer_equal", trigger
    if reviewer is not None and spend.provider(reviewer.model) in held_providers:
        return "review_held", trigger
    if overflow_provider in held_providers:
        return "overflow_held", trigger
    if not _has_room(figures[overflow_provider]):
        return "overflow_no_headroom", trigger
    return "implement_overflow", trigger


def route_dispatch(
    task: TaskSpec,
    config: dict[str, Any],
    usage_payload: dict[str, Any],
    now: float,
    held_providers: Mapping[str, Any] | None = None,
) -> tuple[TaskSpec, list[dict[str, Any]]]:
    """Preview paced fallback/plan choices and usage- or hold-triggered overflow.

    Preserve task intent; record no events. Overflow is independent of the paced levers.
    """
    routing = config.get("routing") or {}
    plan_config = routing.get("plan") or {}
    implement_config = routing.get("implement") or {}
    overflow_text = overflow_setting(config)
    if not plan_config.get("enabled") and not implement_config.get("enabled") and not overflow_text:
        return task, []
    headroom = calculate_headroom(usage_payload, config, now)
    decisions: list[dict[str, Any]] = []
    if plan_config.get("enabled"):
        model = task.models.get("plan")
        subscription = spend.provider(model.model) if model else "unknown"
        figures = headroom.get(subscription, {"has_headroom": False})
        original = task.plan
        if task.policy == "approve-plan-first":
            reason = "policy_plan"
        elif task.plan_explicit:
            reason = "explicit_plan"
        elif task.plan is True:
            reason = "already_planned"
        elif task.plan is False and figures["has_headroom"]:
            task = replace(task, plan=True)
            reason = "plan_headroom"
        elif task.plan is False and _under_max_used(figures, plan_config.get("max_used")):
            task = replace(task, plan=True)
            reason = "plan_under_max_used"
        else:
            reason = "no_plan_headroom"
        decisions.append(
            {
                "lever": "plan",
                "from": original,
                "to": task.plan,
                "reason": reason,
                "headroom": {subscription: figures},
            }
        )
    if implement_config.get("enabled") or overflow_text:
        profiles = profile_models(task.project, config)
        default = profiles.get("implement")
        candidate = profiles.get("fallback")
        current = task.models.get("implement")
        overflow = ModelSpec.parse(overflow_text) if overflow_text else None
        outcome = None
        trigger = None
        subscriptions = {
            spend.provider(model.model) if model else "unknown" for model in (default, candidate)
        }
        if overflow is not None:
            subscriptions.add(spend.provider(overflow.model))
        figures = {
            name: headroom.get(name, {"has_headroom": False}) for name in sorted(subscriptions)
        }
        original_model = current.render() if current else None
        if task.explicit_models is None:
            reason = "unknown_implement_provenance"
        elif "implement" in task.explicit_models:
            reason = "explicit_implement"
        elif default is None or candidate is None or current is None:
            reason = "missing_profile"
        else:
            outcome, trigger = (
                _overflow_outcome(
                    overflow,
                    default,
                    task.models.get("review"),
                    figures,
                    config,
                    held_providers or {},
                )
                if overflow is not None
                else ("no_implement_trigger", None)
            )
            reason = outcome
            if overflow is not None and outcome == "implement_overflow":
                task = replace(task, models={**task.models, "implement": overflow})
            elif implement_config.get("enabled"):
                fallback_headroom = figures[spend.provider(candidate.model)]
                implement_headroom = figures[spend.provider(default.model)]
                threshold = _number(implement_config.get("codex_headroom"))
                threshold = 0.15 if threshold is None else threshold
                if not fallback_headroom["has_headroom"]:
                    reason = "no_fallback_headroom"
                elif not implement_headroom["has_headroom"]:
                    reason = "implement_no_headroom"
                elif (
                    task.extra.get("size") in ("M", "L")
                    and min(
                        window["headroom"]
                        for window in fallback_headroom.values()
                        if isinstance(window, dict)
                    )
                    > threshold
                ):
                    reason = "large_task_headroom"
                else:
                    reason = "no_implement_trigger"
                if reason in ("implement_no_headroom", "large_task_headroom"):
                    reviewer = task.models.get("review")
                    if reviewer is not None and candidate.model == reviewer.model:
                        reason = "reviewer_equal"
                    elif candidate.render() == current.render():
                        reason = "already_fallback"
                    else:
                        task = replace(task, models={**task.models, "implement": candidate})
        decision = {
            "lever": "implement",
            "from": original_model,
            "to": task.models["implement"].render() if current else None,
            "reason": reason,
            "headroom": figures,
        }
        if overflow_text:
            decision["overflow"] = {
                "model": overflow_text,
                "outcome": outcome if outcome is not None else reason,
                "trigger": trigger,
            }
        decisions.append(decision)
    return task, decisions


def format_dispatch_decision(payload: dict[str, Any]) -> str:
    """Render a dispatch transition with complete, stable headroom figures."""
    return (
        f"{payload['lever']}: {payload['reason']}; {payload['from']} -> {payload['to']}; "
        f"headroom={json.dumps(payload['headroom'], sort_keys=True)}"
    )


def emit_routing_event(
    emit: Callable[[str, dict[str, Any]], None],
    *,
    lever: str,
    subscription: str,
    used: float,
    elapsed: float,
    headroom: float,
    choice: str,
) -> None:
    """Emit one routing decision through a run's event callback."""
    emit(
        "routing",
        {
            "lever": lever,
            "subscription": subscription,
            "used": used,
            "elapsed": elapsed,
            "headroom": headroom,
            "choice": choice,
        },
    )
