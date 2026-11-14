"""Subscription allowance routing headroom."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest

from helmraiser.config import DEFAULT_CONFIG, SUBSCRIPTION_TEMPLATES
from helmraiser.loop.routing import calculate_headroom, emit_routing_event

WEEK = 7 * 86400
WEEK_START = datetime(2026, 9, 28, tzinfo=UTC).timestamp()


def _config() -> dict:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["subscriptions"] = copy.deepcopy(SUBSCRIPTION_TEMPLATES)
    return config


def _week(fraction, now: float, *, source: str = "live", resets_at=None):
    return {
        "openai-codex": {
            "week": {
                "fraction": fraction,
                "resets_at": WEEK_START + WEEK if resets_at is None else resets_at,
                "source": source,
            }
        }
    }


@pytest.mark.parametrize(
    ("now", "used", "expected_elapsed", "expected_headroom", "has_headroom"),
    [
        (WEEK_START, 0.0, 0.0, 0.0, False),
        (WEEK_START + WEEK / 2, 0.2, 0.5, 0.275, True),
        (WEEK_START + WEEK, 0.3, 1.0, 0.65, True),
        (WEEK_START + WEEK - 12 * 3600, 0.3, pytest.approx(13 / 14), 0.65, True),
    ],
)
def test_weekly_pacing_and_ramp(now, used, expected_elapsed, expected_headroom, has_headroom):
    result = calculate_headroom(_week(used, now), _config(), now)["openai-codex"]

    assert result["week"]["elapsed"] == expected_elapsed
    assert result["week"]["headroom"] == expected_headroom
    assert result["has_headroom"] is has_headroom


def test_go_five_hour_window_blocks_other_positive_windows():
    now = WEEK_START + WEEK / 2
    usage = {
        "opencode-go": {
            "5h": {"fraction": 0.94, "resets_at": now + 3600, "source": "live"},
            "week": {"fraction": 0.2, "resets_at": WEEK_START + WEEK, "source": "live"},
            "month": {"fraction": 0.2, "resets_at": now + 15 * 86400, "source": "live"},
        }
    }

    result = calculate_headroom(usage, _config(), now)["opencode-go"]

    assert result["5h"]["headroom"] == pytest.approx(0.01)
    assert result["week"]["headroom"] > 0.02
    assert result["month"]["headroom"] > 0.02
    assert result["has_headroom"] is False


@pytest.mark.parametrize(
    "reading",
    [
        {"fraction": None, "resets_at": WEEK_START + WEEK, "source": "live"},
        {"fraction": 0.2, "resets_at": None, "source": "live"},
    ],
)
def test_missing_reading_fails_closed(reading):
    usage = {"openai-codex": {"week": reading}}
    result = calculate_headroom(usage, _config(), WEEK_START + WEEK / 2)["openai-codex"]

    assert result["week"]["headroom"] is None
    assert result["has_headroom"] is False


def test_stale_live_reset_fails_closed_but_exact_reset_is_valid():
    now = WEEK_START + WEEK
    stale = calculate_headroom(_week(0.3, now, resets_at=now - 1), _config(), now)["openai-codex"]
    exact = calculate_headroom(_week(0.3, now, resets_at=now), _config(), now)["openai-codex"]

    assert stale["week"]["elapsed"] is None
    assert stale["week"]["headroom"] is None
    assert stale["has_headroom"] is False
    assert exact["week"]["elapsed"] == 1.0
    assert exact["week"]["headroom"] == 0.65
    assert exact["has_headroom"] is True


@pytest.mark.parametrize(
    ("window", "duration"),
    [("5h", 5 * 3600), ("week", WEEK), ("month", 30 * 86400)],
)
@pytest.mark.parametrize("ramp_hours", [24, 1000])
def test_live_window_starting_in_future_fails_closed(window, duration, ramp_hours):
    config = _config()
    config["routing"]["ramp_hours"] = ramp_hours
    now = WEEK_START
    usage = {
        "opencode-go": {
            window: {"fraction": 0.2, "resets_at": now + duration + 1, "source": "live"}
        }
    }

    result = calculate_headroom(usage, config, now)["opencode-go"]

    assert result[window]["elapsed"] is None
    assert result[window]["headroom"] is None
    assert result["has_headroom"] is False


def test_target_is_clamped_to_subscription_stop_at():
    config = _config()
    config["subscriptions"]["openai-codex"]["stop_at"] = 0.8
    now = WEEK_START + WEEK / 2

    result = calculate_headroom(_week(0.2, now), config, now)["openai-codex"]

    assert result["week"]["target"] == 0.8
    assert result["week"]["headroom"] == 0.2


def test_margin_is_strict():
    config = _config()
    config["routing"]["margin"] = 0.02
    now = WEEK_START + WEEK / 2

    result = calculate_headroom(_week(0.455, now), config, now)["openai-codex"]

    assert result["week"]["headroom"] == 0.02
    assert result["has_headroom"] is False


def test_margin_uses_published_six_decimal_headroom():
    config = _config()
    config["routing"]["margin"] = 0.02
    now = WEEK_START + WEEK / 2

    result = calculate_headroom(_week(0.4549996, now), config, now)["openai-codex"]

    assert result["week"]["headroom"] == 0.02
    assert result["has_headroom"] is False


def test_estimated_window_uses_configured_start_and_reading_reset():
    now = WEEK_START + WEEK / 4
    result = calculate_headroom(_week(0.1, now, source="estimate"), _config(), now)["openai-codex"]

    assert result["week"]["elapsed"] == 0.25
    assert result["week"]["headroom"] == 0.1375


def test_live_month_starts_at_corresponding_previous_month_reset():
    reset = datetime(2026, 3, 31, 17, tzinfo=UTC).timestamp()
    start = datetime(2026, 2, 28, 17, tzinfo=UTC).timestamp()
    now = start + (reset - start) / 2
    usage = {"openai-codex": {"month": {"fraction": 0.2, "resets_at": reset, "source": "live"}}}

    result = calculate_headroom(usage, _config(), now)["openai-codex"]

    assert result["month"]["elapsed"] == 0.5
    assert result["month"]["headroom"] == 0.275


def test_configured_subscription_without_readings_fails_closed():
    result = calculate_headroom({}, _config(), WEEK_START)

    assert result["openai-codex"] == {"has_headroom": False}
    assert result["opencode-go"] == {"has_headroom": False}


def test_routing_event_has_exact_contract():
    events = []

    emit_routing_event(
        lambda event, payload: events.append((event, payload)),
        lever="scout",
        subscription="openai-codex",
        used=0.28,
        elapsed=0.46,
        headroom=0.157,
        choice="run",
    )

    assert events == [
        (
            "routing",
            {
                "lever": "scout",
                "subscription": "openai-codex",
                "used": 0.28,
                "elapsed": 0.46,
                "headroom": 0.157,
                "choice": "run",
            },
        )
    ]
