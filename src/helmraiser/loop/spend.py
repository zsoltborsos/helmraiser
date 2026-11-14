"""Ledger-derived provider spend and subscription usage helpers."""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from helmraiser.ledger.db import Ledger
from helmraiser.loop import live_usage as _live_usage

WINDOWS = {"5h": 5 * 3600, "7d": 7 * 86400, "30d": 30 * 86400}

# Subscription usage windows: rolling 5 h, weekly and monthly reset.
SUBSCRIPTION_WINDOWS = ("5h", "week", "month")
FIVE_HOURS = 5 * 3600
WEEK_SECONDS = 7 * 86400
DEFAULT_WINDOW_SHARE = {"5h": 0.2, "week": 0.5, "month": 1.0}
# A run's assumed spend on a subscription when deciding whether it may start.
DEFAULT_PROJECTION_USD = 1.0
# Live-only subscriptions (no limits table) report dollar spend over rolling
# windows instead of a limit-weighted estimate.
LIVE_ONLY_WINDOW_SECONDS = {"5h": FIVE_HOURS, "week": WEEK_SECONDS, "month": 30 * 86400}


def provider(model: str) -> str:
    """The subscription provider prefix of a model name."""
    return model.split("/", 1)[0]


def cost_entries(ledger: Ledger, since: float) -> list[tuple[float, str, float]]:
    """Timestamp/model/cost entries, including each run's non-iteration remainder."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ledger.spend_rows(since):
        grouped[str(row["run_id"])].append(row)

    entries: list[tuple[float, str, float]] = []
    for rows in grouped.values():
        first = rows[0]
        plan_cost = float(first["plan_cost_usd"] or 0.0)
        plan_model = first["model_plan"]
        if plan_model is not None and plan_cost > 1e-9:
            plan_timestamp = float(first["run_started_at"])
            if plan_timestamp >= since:
                entries.append((plan_timestamp, str(plan_model), plan_cost))
        iteration_total = 0.0
        for row in rows:
            if row["n"] is None:
                continue
            cost = float(row["iteration_cost_usd"] or 0.0)
            iteration_total += cost
            timestamp = float(row["iteration_ended_at"] or row["iteration_started_at"])
            if timestamp >= since:
                entries.append((timestamp, str(row["model"]), cost))
        latest_iteration = next((row for row in reversed(rows) if row["n"] is not None), None)
        if latest_iteration is not None and latest_iteration["iteration_ended_at"] is None:
            remainder_timestamp = float(latest_iteration["iteration_started_at"])
            remainder_model = str(latest_iteration["model"])
        else:
            remainder_timestamp = float(first["run_ended_at"] or first["run_started_at"])
            remainder_model = str(first["model_review"])
        remainder = float(first["run_cost_usd"] or 0.0) - iteration_total - plan_cost
        if remainder_timestamp >= since and remainder > 1e-9:
            entries.append((remainder_timestamp, remainder_model, remainder))
    return sorted(entries)


def spend_entries(ledger: Ledger, since: float) -> list[tuple[float, str, float]]:
    """Timestamp/provider/cost entries at or after ``since``."""
    return [(ts, provider(model), usd) for ts, model, usd in cost_entries(ledger, since)]


def spend_by_provider(ledger: Ledger, now: float) -> dict[str, dict[str, float]]:
    entries = spend_entries(ledger, now - WINDOWS["30d"])
    totals: dict[str, dict[str, float]] = {}
    for timestamp, name, usd in entries:
        if usd <= 0:
            continue
        values = totals.setdefault(name, dict.fromkeys(WINDOWS, 0.0))
        for window, seconds in WINDOWS.items():
            if timestamp >= now - seconds:
                values[window] += usd
    return {
        name: {window: round(values[window], 4) for window in WINDOWS}
        for name, values in sorted(totals.items())
    }


def subscriptions(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = config.get("subscriptions") or {}
    return {str(name): block for name, block in raw.items() if isinstance(block, dict)}


def caps_enforcement(ledger: Ledger, config: dict[str, Any]) -> tuple[bool, str]:
    switch = ledger.caps_switch()
    if switch is not None:
        return switch == "on", "switch"
    return True, "config"


def model_id(model: str) -> str:
    """The model name without provider prefix or ``:thinking`` suffix."""
    without_provider = model.split("/", 1)[1] if "/" in model else model
    return without_provider.split(":", 1)[0]


def model_limit(sub_config: dict[str, Any], model: str | None) -> float:
    """The subscription's monthly dollar limit for ``model``, or the default."""
    limits = sub_config.get("monthly_limit_usd") or {}
    if model is not None and model_id(model) in limits:
        return float(limits[model_id(model)])
    return float(sub_config.get("default_monthly_limit_usd", 15.0))


def projected_spend(sub_config: dict[str, Any], max_cost_usd: float) -> float:
    """What one run is assumed to spend on this subscription before it starts.

    ``max_cost_usd`` caps the whole run, mostly codex review and fallback, so it is capped
    again by the subscription's ``projection_usd`` (default ``DEFAULT_PROJECTION_USD``):
    a cheap model with a small monthly limit would otherwise never start."""
    cap = float(sub_config.get("projection_usd", DEFAULT_PROJECTION_USD))
    return min(max_cost_usd, cap)


def window_share(sub_config: dict[str, Any], window: str) -> float:
    shares = sub_config.get("window_share") or {}
    return float(shares.get(window, DEFAULT_WINDOW_SHARE.get(window, 1.0)))


def _month_anchor(year: int, month: int, day: int, hour: int) -> datetime:
    day = min(day, monthrange(year, month)[1])
    return datetime(year, month, day, hour, tzinfo=UTC)


def window_start(window: str, now: float, sub_config: dict[str, Any]) -> float:
    """The earliest moment of ``window``'s current period at or before ``now`` (UTC).

    ``5h`` is rolling; ``week`` and ``month`` anchor on the subscription's reset
    weekday/hour and renewal day/hour, clamped to the month's length.
    """
    if window == "5h":
        return now - FIVE_HOURS
    if window == "week":
        reset = sub_config.get("week_reset") or {}
        weekday = int(reset.get("weekday", 0))
        hour = int(reset.get("hour_utc", 0))
        moment = datetime.fromtimestamp(now, tz=UTC)
        days_back = (moment.weekday() - weekday) % 7
        week_start = moment.replace(hour=hour, minute=0, second=0, microsecond=0).timestamp()
        week_start -= days_back * 86400
        if week_start > now:  # the reset weekday is today, later than now
            week_start -= WEEK_SECONDS
        return week_start
    if window == "month":
        reset = sub_config.get("month_reset") or {}
        day = int(reset.get("day", 1))
        hour = int(reset.get("hour_utc", 0))
        moment = datetime.fromtimestamp(now, tz=UTC)
        start = _month_anchor(moment.year, moment.month, day, hour)
        if start.timestamp() > now:
            previous = (
                (moment.year - 1, 12) if moment.month == 1 else (moment.year, moment.month - 1)
            )
            start = _month_anchor(previous[0], previous[1], day, hour)
        return start.timestamp()
    raise ValueError(f"unknown window: {window}")


def window_end(
    window: str,
    now: float,
    sub_config: dict[str, Any],
    entries: list[tuple[float, str, float]] | tuple[tuple[float, str, float], ...] = (),
) -> float:
    """When ``window`` next resets. ``5h``: the oldest entry in the window plus
    5 h, or ``now`` when there is none."""
    if window == "5h":
        relevant = [ts for ts, _model, _usd in entries if ts >= now - FIVE_HOURS]
        return min(relevant) + FIVE_HOURS if relevant else now
    start = window_start(window, now, sub_config)
    if window == "week":
        return start + WEEK_SECONDS
    reset = sub_config.get("month_reset") or {}
    day = int(reset.get("day", 1))
    hour = int(reset.get("hour_utc", 0))
    moment = datetime.fromtimestamp(now, tz=UTC)
    anchor = _month_anchor(moment.year, moment.month, day, hour)
    if anchor.timestamp() <= now:
        next_month = (moment.year, moment.month + 1) if moment.month < 12 else (moment.year + 1, 1)
        return _month_anchor(next_month[0], next_month[1], day, hour).timestamp()
    return anchor.timestamp()


def is_live_only(sub_config: dict[str, Any]) -> bool:
    """True when the subscription has no limits table to estimate from."""
    return not (sub_config.get("monthly_limit_usd") or "default_monthly_limit_usd" in sub_config)


def live_for(
    ledger: Ledger, config: dict[str, Any], now: float, refresh: bool = False
) -> dict[str, dict]:
    """Live subscription usage for ``ledger``'s data directory."""
    return _live_usage.live_usage(Path(ledger.db_path).parent, config, now, refresh=refresh)


def _live_window(
    value: dict[str, Any], sub: dict[str, Any], estimate: dict[str, Any] | None
) -> dict[str, Any]:
    fraction = round(float(value["fraction"]), 6)
    resets = value.get("resets_at")
    window: dict[str, Any] = {
        "fraction": fraction,
        "spend_usd": 0.0 if estimate is None else estimate["spend_usd"],
        "resets_at": float(resets) if resets is not None else (estimate or {}).get("resets_at"),
        "warn_at": sub.get("warn_at"),
        "stop_at": sub.get("stop_at"),
        "source": "live",
        "live_fraction": fraction,
    }
    if estimate is not None:
        window["estimate_fraction"] = estimate["fraction"]
    return window


def _merge_live(
    windows: dict[str, Any], live_windows: dict[str, Any], sub: dict[str, Any]
) -> dict[str, Any]:
    for window in SUBSCRIPTION_WINDOWS:
        value = live_windows.get(window)
        if isinstance(value, dict) and "fraction" in value:
            windows[window] = _live_window(value, sub, windows.get(window))
        else:
            windows[window]["source"] = "estimate"
    return windows


def usage(
    ledger: Ledger, config: dict[str, Any], now: float, live: dict[str, dict] | None = None
) -> dict[str, dict[str, Any]]:
    """Subscription usage per window.

    ``{provider: {window: {fraction, spend_usd, resets_at, warn_at, stop_at,
    source}}}``. ``fraction`` is the effective value: the live subscription
    figure where available (with ``live_fraction`` and the kept
    ``estimate_fraction``), else the ledger estimate. Live-only subscriptions
    (no limits table, e.g. Codex) have windows only when live data exists.
    Each model's spend counts against its own monthly limit, so the estimate is
    ``sum(cost / (monthly_limit_of_model * window_share))``.
    """
    subs = subscriptions(config)
    if not subs:
        return {}
    live = live or {}
    plans = []
    since = now
    for name in sorted(subs):
        sub = subs[name]
        if is_live_only(sub):
            continue
        starts = {window: window_start(window, now, sub) for window in SUBSCRIPTION_WINDOWS}
        plans.append((name, sub, starts))
        earliest = min(starts.values())
        since = min(since, earliest)
    for name, live_windows in live.items():
        live_sub = subs.get(name)
        if not live_sub or not is_live_only(live_sub) or not isinstance(live_windows, dict):
            continue
        for window in live_windows:
            seconds = LIVE_ONLY_WINDOW_SECONDS.get(window)
            if seconds is not None:
                since = min(since, now - seconds)
    entries = cost_entries(ledger, since)
    payload: dict[str, dict[str, Any]] = {}
    for name, sub, starts in plans:
        provider_entries = [
            (ts, model, usd) for ts, model, usd in entries if provider(model) == name
        ]
        windows: dict[str, Any] = {}
        for window in SUBSCRIPTION_WINDOWS:
            share = window_share(sub, window)
            in_window = [
                (ts, model, usd) for ts, model, usd in provider_entries if ts >= starts[window]
            ]
            fraction = sum(usd / (model_limit(sub, model) * share) for _ts, model, usd in in_window)
            estimate_fraction = round(fraction, 6)
            windows[window] = {
                "fraction": estimate_fraction,
                "estimate_fraction": estimate_fraction,
                "spend_usd": round(sum(usd for _ts, _model, usd in in_window), 4),
                "resets_at": window_end(window, now, sub, provider_entries),
                "warn_at": sub.get("warn_at"),
                "stop_at": sub.get("stop_at"),
            }
        payload[name] = _merge_live(windows, live.get(name) or {}, sub)
    for name in sorted(set(live) | {n for n, sub in subs.items() if is_live_only(sub)}):
        if name in payload:
            continue
        sub = subs.get(name) or {}
        windows = live.get(name) or {}
        if not isinstance(windows, dict) or not windows:
            continue
        live_entries = []
        if is_live_only(sub):
            live_entries = [(ts, usd) for ts, model, usd in entries if provider(model) == name]
        provider_windows: dict[str, Any] = {}
        for window in SUBSCRIPTION_WINDOWS:
            value = windows.get(window)
            if not isinstance(value, dict) or "fraction" not in value:
                continue
            if is_live_only(sub):
                seconds = LIVE_ONLY_WINDOW_SECONDS.get(window, WEEK_SECONDS)
                spend_usd = round(sum(usd for ts, usd in live_entries if ts >= now - seconds), 4)
            else:
                spend_usd = 0.0
            provider_windows[window] = _live_window(value, sub, None) | {"spend_usd": spend_usd}
        if provider_windows:
            payload[name] = provider_windows
    return payload


SHORT_PROVIDERS = {"opencode-go": "go", "openai-codex": "codex"}
WINDOW_LABELS = {"5h": "5h", "week": "wk", "month": "mo"}
PROVIDER_ORDER = ("opencode-go", "openai-codex")


def short_provider(name: str) -> str:
    return SHORT_PROVIDERS.get(name, name)


def _ordered(names: Any) -> list[str]:
    known = [name for name in PROVIDER_ORDER if name in names]
    return known + sorted(n for n in names if n not in known)


def usage_summary(
    usage_payload: dict[str, Any], headroom_payload: dict[str, Any] | None = None
) -> str:
    """Compact subscription usage, optionally with elapsed time and headroom."""
    parts = []
    for name in _ordered(usage_payload):
        windows = usage_payload[name]
        window_parts = []
        for window in SUBSCRIPTION_WINDOWS:
            if not windows.get(window):
                continue
            text = f"{WINDOW_LABELS[window]} {float(windows[window]['fraction']):.0%}"
            if headroom_payload is not None:
                routing = (headroom_payload.get(name) or {}).get(window) or {}
                elapsed = routing.get("elapsed")
                headroom = routing.get("headroom")
                if elapsed is not None:
                    text += f" · {float(elapsed):.0%} elapsed"
                else:
                    text += " · elapsed unknown"
                if headroom is not None:
                    text += f" · headroom {float(headroom) * 100:+.0f} pts"
                else:
                    text += " · headroom unknown"
            window_parts.append(text)
        if window_parts:
            separator = " | " if headroom_payload is not None else " · "
            parts.append(f"{short_provider(name)} " + separator.join(window_parts))
    return "  ·  ".join(parts)


def subscription_warnings(config: dict[str, Any], usage_payload: dict[str, Any]) -> list[str]:
    """One line per window at or over its subscription's ``warn_at``."""
    lines = []
    for name in sorted(usage_payload):
        for window in SUBSCRIPTION_WINDOWS:
            data = (usage_payload.get(name) or {}).get(window)
            warn_at = data.get("warn_at") if data else None
            if not data or warn_at is None or float(data["fraction"]) < float(warn_at):
                continue
            resets_at = data.get("resets_at")
            resets = (
                datetime.fromtimestamp(float(resets_at), tz=UTC).strftime("%a %H:%M UTC")
                if resets_at is not None
                else "at an unknown time"
            )
            lines.append(
                f"{name} {window} usage {float(data['fraction']):.0%} "
                f"(warn at {float(warn_at):.0%}), resets {resets}"
            )
    return lines


def _stop_models(task: Any) -> tuple[str | None, list[str]]:
    """The task's implement model and the subscriptions its models use."""
    implement_model = task.models["implement"].model if "implement" in task.models else None
    used = {
        provider(task.models[name].model)
        for name in ("implement", "fallback", "review")
        if name in task.models
    }
    return implement_model, sorted(used)


def _projected_fraction(
    sub: dict[str, Any], name: str, implement_model: str | None, window: str, budget_usd: float
) -> float:
    """The task's projected share of ``window``: 0 for a live-only subscription."""
    if is_live_only(sub):
        return 0.0
    limit_model = (
        implement_model
        if implement_model is not None and provider(implement_model) == name
        else None
    )
    return projected_spend(sub, budget_usd) / (
        model_limit(sub, limit_model) * window_share(sub, window)
    )


def never_startable(config: dict[str, Any], task: Any) -> dict[str, Any] | None:
    """Why the task can never start under ``stop_at``, or None.

    The task's projection *alone* (at zero usage) reaches ``stop_at`` for some window,
    so no window reset lets it start. Returns ``{"reason":
    "never_startable:<provider>:<window>", "model", "projected", "stop_at",
    "projection_usd"}``: ``model`` is the one whose monthly limit is used,
    ``projected`` the fraction of the window and ``projection_usd`` the assumed
    dollar spend.
    """
    subs = subscriptions(config)
    implement_model, used = _stop_models(task)
    for name in used:
        sub = subs.get(name) or {}
        stop_at = sub.get("stop_at")
        if not stop_at:
            continue
        for window in SUBSCRIPTION_WINDOWS:
            projected = _projected_fraction(
                sub, name, implement_model, window, task.budget.max_cost_usd
            )
            if projected >= float(stop_at):
                return {
                    "reason": f"never_startable:{name}:{window}",
                    "model": (
                        implement_model
                        if implement_model is not None and provider(implement_model) == name
                        else f"{name} (default limit)"
                    ),
                    "projected": round(projected, 4),
                    "stop_at": float(stop_at),
                    "projection_usd": projected_spend(sub, task.budget.max_cost_usd),
                }
    return None


def never_startable_text(block: dict[str, Any]) -> str:
    """One line: why the task will never start and what to change."""
    _kind, name, window = str(block["reason"]).split(":", 2)
    return (
        f"will never start: projected ${block['projection_usd']:g} on {block['model']} is "
        f"{block['projected']:.0%} of the {name} {window} window, at or over stop_at "
        f"{block['stop_at']:.0%}; lower budget.max_cost_usd or {name} projection_usd, "
        "raise stop_at, or pick a model with a larger monthly limit"
    )


def stop_block(
    usage_payload: dict[str, Any],
    config: dict[str, Any],
    task: Any,
    now: float,
) -> dict[str, Any] | None:
    """Why a task with this budget may not start now under ``stop_at``, or None.

    A task is skipped when any window of a subscription used by its implement,
    fallback or review model would reach ``stop_at``. Estimated subscriptions
    include the task's projected fraction; live-only subscriptions cannot
    project from a dollar limit and use the current live fraction alone.
    Returns ``{"reason": "usage:<provider>:<window>", "until": <resets_at>}``, or the
    ``never_startable`` block (no ``until``) when no reset would ever let it start.
    """
    subs = subscriptions(config)
    if not subs:
        return None
    never = never_startable(config, task)
    if never is not None:
        return never
    implement_model, used = _stop_models(task)
    for name in used:
        sub = subs.get(name)
        stop_at = sub.get("stop_at") if sub else None
        if not stop_at:
            continue
        for window in SUBSCRIPTION_WINDOWS:
            data = (usage_payload.get(name) or {}).get(window)
            if data is None:
                continue
            projected = _projected_fraction(
                sub or {}, name, implement_model, window, task.budget.max_cost_usd
            )
            if float(data["fraction"]) + projected >= float(stop_at):
                return {
                    "reason": f"usage:{name}:{window}",
                    "until": data["resets_at"],
                    "fraction": float(data["fraction"]),
                    "projection_usd": (
                        0.0
                        if is_live_only(sub or {})
                        else projected_spend(sub or {}, task.budget.max_cost_usd)
                    ),
                }
    return None


def caps_payload(
    ledger: Ledger, config: dict[str, Any], now: float, refresh: bool = False
) -> dict[str, Any]:
    from helmraiser.loop.routing import calculate_headroom  # local: routing uses window helpers

    enforced, source = caps_enforcement(ledger, config)
    usage_payload = usage(ledger, config, now, live=live_for(ledger, config, now, refresh=refresh))
    return {
        "enforced": enforced,
        "source": source,
        "usage": usage_payload,
        "routing": {"headroom": calculate_headroom(usage_payload, config, now)},
        "spend": spend_by_provider(ledger, now),
    }
