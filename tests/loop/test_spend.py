"""Provider spend projection from ledger runs and iterations."""

from __future__ import annotations

import copy
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from helmraiser.config import SUBSCRIPTION_TEMPLATES
from helmraiser.loop.spend import (
    SUBSCRIPTION_WINDOWS,
    WINDOWS,
    never_startable,
    never_startable_text,
    provider,
    spend_by_provider,
    spend_entries,
    stop_block,
    subscription_warnings,
    usage,
    window_end,
    window_start,
)
from helmraiser.task.model import Budget, ModelSpec


def test_provider_uses_model_prefix():
    assert provider("openai-codex/gpt-6:high") == "openai-codex"
    assert provider("local") == "local"


def test_entries_remainder_boundaries_and_running_run(ledger, make_task):
    now = 2_000_000_000.0
    task = make_task()
    ledger.upsert_task(task, "snapshot")
    ledger.create_run(
        {
            "run_id": "finished",
            "task_id": task.task_id,
            "project": str(task.project),
            "status": "landed",
            "policy": "review",
            "isolation": "none",
            "model_implement": "openai-codex/impl",
            "model_review": "opencode-go/review",
            "pi_version": "0.87.1",
            "cost_usd": 3.0,
            "started_at": now - 100,
            "ended_at": now - 50,
        }
    )
    ledger.create_run(
        {
            "run_id": "running",
            "task_id": task.task_id,
            "project": str(task.project),
            "status": "running",
            "policy": "review",
            "isolation": "none",
            "model_implement": "openai-codex/impl",
            "model_review": "openai-codex/review",
            "pi_version": "0.87.1",
            "cost_usd": 0.5,
            "started_at": now - WINDOWS["5h"],
        }
    )
    conn = ledger._get_conn()
    conn.execute(
        "INSERT INTO iterations (run_id,n,session_id,model,started_at,ended_at,cost_usd) "
        "VALUES ('finished',1,'s','openai-codex/impl',?,?,2.0)",
        (now - 90, now - 80),
    )
    conn.commit()

    entries = spend_entries(ledger, now - WINDOWS["30d"])
    assert (now - 80, "openai-codex", 2.0) in entries
    assert (now - 50, "opencode-go", 1.0) in entries
    assert (now - WINDOWS["5h"], "openai-codex", 0.5) in entries

    spend = spend_by_provider(ledger, now)
    assert spend["openai-codex"] == {"5h": 2.5, "7d": 2.5, "30d": 2.5}
    assert spend["opencode-go"] == {"5h": 1.0, "7d": 1.0, "30d": 1.0}


def test_live_remainder_uses_open_iteration_provider(ledger, make_task):
    now = 2_000_000_000.0
    task = make_task()
    ledger.create_run(
        {
            "run_id": "mid-iteration",
            "task_id": task.task_id,
            "project": str(task.project),
            "status": "running",
            "policy": "review",
            "isolation": "none",
            "model_implement": "opencode-go/implement",
            "model_review": "openai-codex/review",
            "pi_version": "0.87.1",
            "cost_usd": 0.75,
            "started_at": now - 60,
        }
    )
    conn = ledger._get_conn()
    conn.execute(
        "INSERT INTO iterations (run_id,n,session_id,model,started_at) VALUES (?,?,?,?,?)",
        ("mid-iteration", 1, "implement", "opencode-go/implement", now - 30),
    )
    conn.commit()

    entries = spend_entries(ledger, now - WINDOWS["5h"])

    assert entries == [
        (now - 30, "opencode-go", 0.0),
        (now - 30, "opencode-go", 0.75),
    ]


# ── Subscription usage meter ──────────────────────────────────────────


def _seed_cost(ledger, make_task, model: str, cost: float, *, when: float | None = None):
    task = make_task()
    ledger.upsert_task(task, "snapshot")
    ended = when if when is not None else time.time() - 60
    ledger.create_run(
        {
            "run_id": f"run-{model}-{cost}-{ended}",
            "task_id": task.task_id,
            "project": str(task.project),
            "status": "landed",
            "policy": "review",
            "isolation": "none",
            "model_implement": model,
            "model_review": model,
            "pi_version": "0.87.1",
            "cost_usd": cost,
            "started_at": ended,
            "ended_at": ended,
        }
    )


def _sub_config(**overrides: Any) -> dict:
    sub = copy.deepcopy(SUBSCRIPTION_TEMPLATES["opencode-go"])
    sub.update(overrides)
    return {"subscriptions": {"opencode-go": sub}}


def test_usage_weights_spend_by_each_models_limit(ledger, make_task):
    now = 1_790_000_000.0
    _seed_cost(ledger, make_task, "opencode-go/glm-5.3-flash", 12.0, when=now - 60)
    _seed_cost(ledger, make_task, "opencode-go/kimi-k3", 3.0, when=now - 30)
    config = _sub_config()

    meter = usage(ledger, config, now)
    windows = meter["opencode-go"]
    # Each model's spend counts against its own limit: glm-5.3-flash contributes
    # 12 / ($60 * 0.2) = 1.0, kimi-k3 adds 3 / ($15 * 0.2) = 1.0.
    assert windows["5h"]["fraction"] == pytest.approx(2.0)
    assert windows["5h"]["estimate_fraction"] == pytest.approx(2.0)
    assert windows["5h"]["spend_usd"] == pytest.approx(15.0)
    assert windows["week"]["fraction"] == pytest.approx(0.8)
    assert windows["month"]["fraction"] == pytest.approx(0.4)


def test_usage_unknown_model_uses_default_limit(ledger, make_task):
    now = 1_790_000_000.0
    _seed_cost(ledger, make_task, "opencode-go/mystery:high", 3.0, when=now - 60)

    windows = usage(ledger, _sub_config(), now)["opencode-go"]
    assert windows["5h"]["fraction"] == pytest.approx(1.0)
    assert windows["5h"]["spend_usd"] == pytest.approx(3.0)


def test_usage_ignores_other_providers(ledger, make_task):
    now = 1_790_000_000.0
    _seed_cost(ledger, make_task, "openai-codex/gpt-6-astra", 9.0, when=now - 60)

    windows = usage(ledger, _sub_config(), now)["opencode-go"]
    assert windows["5h"]["fraction"] == 0.0
    assert windows["5h"]["spend_usd"] == 0.0
    assert "openai-codex" not in usage(ledger, _sub_config(), now)


def test_window_start_week():
    sub = _sub_config()["subscriptions"]["opencode-go"]
    # Sunday 2026-01-25 13:00 UTC -> last Monday 00:00.
    sunday = datetime(2026, 1, 25, 13, 0, tzinfo=UTC).timestamp()
    assert datetime.fromtimestamp(window_start("week", sunday, sub), tz=UTC) == datetime(
        2026, 1, 19, 0, 0, tzinfo=UTC
    )
    # Monday 2026-01-26 before 00:00 UTC -> the previous Monday.
    monday_early = datetime(2026, 1, 26, 0, 0, tzinfo=UTC).timestamp() - 1
    assert datetime.fromtimestamp(window_start("week", monday_early, sub), tz=UTC) == datetime(
        2026, 1, 19, 0, 0, tzinfo=UTC
    )
    # Monday just after 00:00 -> the same Monday.
    monday_late = datetime(2026, 1, 26, 0, 0, tzinfo=UTC).timestamp() + 30
    assert datetime.fromtimestamp(window_start("week", monday_late, sub), tz=UTC) == datetime(
        2026, 1, 26, 0, 0, tzinfo=UTC
    )


def test_window_start_month():
    sub = _sub_config()["subscriptions"]["opencode-go"]
    # Before the 17th 17:00 -> the previous month's anchor (clamped to 30 days).
    early = datetime(2026, 2, 10, 12, 0, tzinfo=UTC).timestamp()
    assert datetime.fromtimestamp(window_start("month", early, sub), tz=UTC) == datetime(
        2026, 1, 17, 17, 0, tzinfo=UTC
    )
    # After the 17th 17:00 -> this month's anchor.
    late = datetime(2026, 2, 20, 9, 0, tzinfo=UTC).timestamp()
    assert datetime.fromtimestamp(window_start("month", late, sub), tz=UTC) == datetime(
        2026, 2, 17, 17, 0, tzinfo=UTC
    )
    # A 30-day month clamps day 17 fine, and the previous anchor lands in a
    # 31-day month without clamping.
    assert datetime.fromtimestamp(
        window_start("month", datetime(2026, 4, 1, 0, 30, tzinfo=UTC).timestamp(), sub), tz=UTC
    ) == datetime(2026, 3, 17, 17, 0, tzinfo=UTC)


def test_window_end_rolling_5h_uses_oldest_entry(ledger, make_task):
    now = 1_790_000_000.0
    _seed_cost(ledger, make_task, "opencode-go/glm-5.3-flash", 1.0, when=now - 4 * 3600)
    sub = _sub_config()["subscriptions"]["opencode-go"]

    windows = usage(ledger, _sub_config(), now)["opencode-go"]
    assert windows["5h"]["resets_at"] == pytest.approx(now - 4 * 3600 + 5 * 3600)
    assert datetime.fromtimestamp(windows["week"]["resets_at"], tz=UTC) == datetime(
        2026, 9, 28, 0, 0, tzinfo=UTC
    )
    assert datetime.fromtimestamp(windows["month"]["resets_at"], tz=UTC) == datetime(
        2026, 10, 17, 17, 0, tzinfo=UTC
    )
    # Empty window: resets right now.
    assert window_end("5h", now, sub) == now


def test_subscription_warnings_one_line_per_window_over_warn_at(ledger, make_task):
    now = 1_790_000_000.0
    _seed_cost(ledger, make_task, "opencode-go/mystery", 2.5, when=now - 60)
    config = _sub_config()

    lines = subscription_warnings(config, usage(ledger, config, now))
    assert len(lines) == 1
    assert lines[0].startswith("opencode-go 5h usage 83% (warn at 80%), resets ")
    assert "UTC" in lines[0]

    # Under warn_at: nothing.
    under = _sub_config(warn_at=0.9)
    assert subscription_warnings(under, usage(ledger, under, now)) == []


def test_stop_block_skips_only_when_stop_at_set(ledger, make_task):
    now = 1_790_000_000.0
    _seed_cost(ledger, make_task, "opencode-go/mystery", 2.85, when=now - 60)
    config = _sub_config()

    class CodexTask:
        models = {"implement": ModelSpec.parse("openai-codex/gpt-6.1-sol:medium")}
        budget = Budget(5.0, 90, 4)

    class GoTask:
        models = {"implement": ModelSpec.parse("opencode-go/mystery:high")}
        budget = Budget(0.3, 90, 4)

    usage_payload = usage(ledger, config, now)
    # stop_at unset: no block even at 95% usage.
    assert stop_block(usage_payload, config, CodexTask, now) is None

    armed = _sub_config(stop_at=0.9)
    armed_payload = usage(ledger, armed, now)
    # Codex task: not on the subscription, never blocked.
    assert stop_block(armed_payload, armed, CodexTask, now) is None
    # Go task at 95% with a tiny projection is blocked on the 5h window.
    block = stop_block(armed_payload, armed, GoTask, now)
    assert block is not None and block["reason"] == "usage:opencode-go:5h"
    assert block["until"] == armed_payload["opencode-go"]["5h"]["resets_at"]
    # Below stop_at even with the projection: it runs.
    under = _sub_config(stop_at=0.99)
    tiny = GoTask
    tiny.budget = Budget(0.0, 90, 4)
    assert stop_block(usage(ledger, under, now), under, tiny, now) is None


def test_projection_is_capped_so_a_small_limit_model_can_start() -> None:
    now = 1_790_000_000.0
    sub = {
        "monthly_limit_usd": {"small": 15},
        "window_share": {"5h": 0.2},
        "stop_at": 0.7,
    }
    config = {"subscriptions": {"opencode-go": sub}}

    class SmallTask:
        models = {"implement": ModelSpec.parse("opencode-go/small:medium")}
        budget = Budget(5.0, 90, 4)  # the whole run's cap, mostly codex review

    quiet = {"opencode-go": {"5h": {"fraction": 0.08, "resets_at": now + 3600}}}
    # Uncapped, $5 / ($15 * 0.2) = 1.67 would block forever; capped at $1 it is 0.33.
    assert stop_block(quiet, config, SmallTask, now) is None
    busy = {"opencode-go": {"5h": {"fraction": 0.4, "resets_at": now + 3600}}}
    assert stop_block(busy, config, SmallTask, now)["reason"] == "usage:opencode-go:5h"
    # projection_usd is the knob.
    sub["projection_usd"] = 0.3
    assert stop_block(busy, config, SmallTask, now) is None


def test_never_startable_when_the_projection_alone_reaches_stop_at() -> None:
    now = 1_790_000_000.0
    # A $15-limit model (the default), the default window shares, stop_at 0.7.
    config = _sub_config(stop_at=0.7, projection_usd=5.0)
    sub = config["subscriptions"]["opencode-go"]

    class GoTask:
        models = {"implement": ModelSpec.parse("opencode-go/unlisted:high")}
        budget = Budget(5.0, 90, 4)

    never = never_startable(config, GoTask)
    assert never == {
        "reason": "never_startable:opencode-go:5h",
        "model": "opencode-go/unlisted",
        "projected": pytest.approx(1.6667),
        "stop_at": 0.7,
        "projection_usd": 5.0,
    }
    assert "will never start: projected $5 on opencode-go/unlisted is 167%" in (
        never_startable_text(never)
    )
    # stop_block says so even with nothing used, and without an ``until``.
    quiet = {
        "opencode-go": {w: {"fraction": 0.0, "resets_at": now + 60} for w in SUBSCRIPTION_WINDOWS}
    }
    assert stop_block(quiet, config, GoTask, now) == never

    # At the default $1: 0.33 of the 5h window, startable; a busy window holds it as before.
    sub["projection_usd"] = 1.0
    assert never_startable(config, GoTask) is None
    assert stop_block(quiet, config, GoTask, now) is None
    busy = copy.deepcopy(quiet)
    busy["opencode-go"]["5h"]["fraction"] = 0.4
    assert stop_block(busy, config, GoTask, now) == {
        "reason": "usage:opencode-go:5h",
        "until": now + 60,
        "fraction": 0.4,
        "projection_usd": 1.0,
    }
    # No stop_at, or a live-only subscription: never never-startable.
    sub["projection_usd"] = 5.0
    sub["stop_at"] = None
    assert never_startable(config, GoTask) is None
    live_only = {"subscriptions": {"opencode-go": {"live": "x", "stop_at": 0.01}}}
    assert never_startable(live_only, GoTask) is None


def test_live_only_stop_does_not_invent_a_projected_dollar_limit(ledger) -> None:
    now = 1_790_000_000.0
    config = {
        "subscriptions": {"openai-codex": {"live": "omarchy-codex", "warn_at": 0.8, "stop_at": 0.8}}
    }

    class CodexTask:
        models = {"implement": ModelSpec.parse("openai-codex/gpt-6.1-sol:medium")}
        budget = Budget(20.0, 90, 4)

    below = {"openai-codex": {"5h": {"fraction": 0.01, "resets_at": now + 3600, "stop_at": 0.8}}}
    assert stop_block(below, config, CodexTask, now) is None

    at_limit = copy.deepcopy(below)
    at_limit["openai-codex"]["5h"]["fraction"] = 0.8
    block = stop_block(at_limit, config, CodexTask, now)
    assert block is not None and block["reason"] == "usage:openai-codex:5h"


def test_live_only_spend_uses_the_full_live_window(ledger, make_task) -> None:
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC).timestamp()
    _seed_cost(
        ledger,
        make_task,
        "openai-codex/gpt-6-astra",
        3.0,
        when=now - 20 * 86400,
    )
    live = {"openai-codex": {"month": {"fraction": 0.2, "resets_at": now + 10 * 86400}}}

    config = {
        "subscriptions": {"openai-codex": copy.deepcopy(SUBSCRIPTION_TEMPLATES["openai-codex"])}
    }
    windows = usage(ledger, config, now, live=live)["openai-codex"]

    assert windows["month"]["spend_usd"] == pytest.approx(3.0)


def test_week_start_before_the_reset_hour_on_a_non_reset_day() -> None:
    from datetime import UTC, datetime

    from helmraiser.loop.spend import window_start

    sub = {"week_reset": {"weekday": 0, "hour_utc": 9}}
    # Sunday 2026-09-27 05:00 UTC: the last reset was Monday 2026-09-21 09:00 UTC.
    now = datetime(2026, 9, 27, 5, 0, tzinfo=UTC).timestamp()
    assert window_start("week", now, sub) == datetime(2026, 9, 21, 9, 0, tzinfo=UTC).timestamp()
    # Monday 2026-09-28 08:00 UTC, before that day's reset: still the 21st.
    now = datetime(2026, 9, 28, 8, 0, tzinfo=UTC).timestamp()
    assert window_start("week", now, sub) == datetime(2026, 9, 21, 9, 0, tzinfo=UTC).timestamp()
    # Monday 2026-09-28 10:00 UTC, after it: the 28th.
    now = datetime(2026, 9, 28, 10, 0, tzinfo=UTC).timestamp()
    assert window_start("week", now, sub) == datetime(2026, 9, 28, 9, 0, tzinfo=UTC).timestamp()


def test_usage_reads_only_the_relevant_windows(monkeypatch) -> None:
    import time

    from helmraiser.loop import spend

    seen: list[float] = []
    monkeypatch.setattr(spend, "cost_entries", lambda ledger, since: seen.append(since) or [])
    now = time.time()
    spend.usage(object(), _sub_config(), now)  # type: ignore[arg-type]
    assert seen and now - seen[0] <= 31 * 86400


@pytest.mark.parametrize("config", [{}, {"subscriptions": {}}])
def test_usage_without_subscriptions_is_empty(ledger, config):
    live = {"opencode-go": {"5h": {"fraction": 0.9, "resets_at": 1_790_003_600.0}}}
    assert usage(ledger, config, 1_790_000_000.0, live=live) == {}


def test_subscription_warnings_without_a_reset_time_do_not_crash():
    usage_payload = {"openai-codex": {"week": {"fraction": 0.9, "warn_at": 0.8, "resets_at": None}}}

    lines = subscription_warnings({}, usage_payload)

    assert lines == ["openai-codex week usage 90% (warn at 80%), resets at an unknown time"]
