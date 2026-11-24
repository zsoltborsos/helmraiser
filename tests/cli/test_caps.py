"""Subscription usage switch commands: ``caps`` show and ``on|off|reset``."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, caps_commands
from helmraiser.config import DEFAULT_CONFIG, SUBSCRIPTION_TEMPLATES
from helmraiser.ledger.db import Ledger
from tests.ledger.test_db import make_spec

runner = CliRunner()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture(autouse=True)
def config(monkeypatch: pytest.MonkeyPatch):
    value = copy.deepcopy(DEFAULT_CONFIG)
    value["subscriptions"] = copy.deepcopy(SUBSCRIPTION_TEMPLATES)
    monkeypatch.setattr(caps_commands, "load_config", lambda: value)
    return value


def invoke(data_dir: Path, *args: str):
    return runner.invoke(app, ["caps", *args, "--data-dir", str(data_dir)])


def _seed_go_spend(
    data_dir: Path, cost: float, tmp_path: Path, model: str = "opencode-go/glm-5.3-flash"
) -> None:
    ledger = Ledger(data_dir / "helmraiser.db")
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    spec = make_spec(project, "t")
    ledger.upsert_task(spec, "snapshot")
    started = time.time() - 60
    ledger.create_run(
        {
            "run_id": "prior",
            "task_id": spec.task_id,
            "project": str(project),
            "status": "landed",
            "policy": "review",
            "isolation": "none",
            "model_implement": model,
            "model_review": model,
            "pi_version": "0.87.1",
            "cost_usd": cost,
            "started_at": started,
            "ended_at": started,
        }
    )


def test_caps_without_subscriptions(data_dir, config):
    config["subscriptions"] = {}
    human = invoke(data_dir)
    result = invoke(data_dir, "--json")
    assert human.exit_code == result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["usage"] == {}
    assert payload["routing"]["headroom"] == {}
    assert "caps enforcement:" in human.output
    assert "opencode-go" not in human.output
    assert "openai-codex" not in human.output


def test_caps_without_subscriptions_keeps_dollar_spend(data_dir, config, tmp_path):
    config["subscriptions"] = {}
    _seed_go_spend(data_dir, 3.0, tmp_path)
    result = invoke(data_dir, "--json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["usage"] == {}
    assert payload["spend"]["opencode-go"]["5h"] == pytest.approx(3.0)


def test_caps_show_human_and_json(data_dir: Path):
    human = invoke(data_dir)
    assert human.exit_code == 0
    assert "caps enforcement: on (config)" in human.output
    assert "opencode-go" in human.output and "0%" in human.output
    assert "warn" in human.output and "80%" in human.output
    assert "elapsedheadroom" not in human.output
    assert "opencode-goweek" not in human.output

    result = invoke(data_dir, "--json")
    payload = json.loads(result.output)
    assert payload["enforced"] is True and payload["source"] == "config"
    assert "headroom" in payload["routing"]
    windows = payload["usage"]["opencode-go"]
    assert set(windows) == {"5h", "week", "month"}
    assert windows["5h"] == {
        "fraction": 0.0,
        "estimate_fraction": 0.0,
        "spend_usd": 0.0,
        "resets_at": windows["5h"]["resets_at"],
        "warn_at": 0.8,
        "stop_at": None,
        "source": "estimate",
    }


def test_caps_show_fraction_spend_and_resets(data_dir: Path, tmp_path: Path):
    # glm-5.3-flash: $60 monthly, so the week allowance is $30; $12 is 40%.
    _seed_go_spend(data_dir, 12.0, tmp_path)

    payload = json.loads(invoke(data_dir, "--json").output)
    windows = payload["usage"]["opencode-go"]
    assert windows["week"]["fraction"] == pytest.approx(0.4)
    assert windows["week"]["spend_usd"] == pytest.approx(12.0)
    assert windows["month"]["fraction"] == pytest.approx(0.2)
    assert windows["5h"]["fraction"] == pytest.approx(1.0)

    human = invoke(data_dir)
    assert "40%" in human.output and "$12.0000" in human.output
    assert "resets" in human.output and "UTC" in human.output


def test_caps_show_codex_dollar_spend(data_dir: Path, tmp_path: Path):
    ledger = Ledger(data_dir / "helmraiser.db")
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    spec = make_spec(project, "t")
    ledger.upsert_task(spec, "snapshot")
    started = time.time() - 60
    ledger.create_run(
        {
            "run_id": "codex",
            "task_id": spec.task_id,
            "project": str(project),
            "status": "landed",
            "policy": "review",
            "isolation": "none",
            "model_implement": "openai-codex/gpt-6-astra",
            "model_review": "openai-codex/gpt-6-astra",
            "pi_version": "0.87.1",
            "cost_usd": 2.5,
            "started_at": started,
            "ended_at": started,
        }
    )

    human = invoke(data_dir)
    assert "codex spend: $2.5000/5h $2.5000/7d $2.5000/30d" in human.output

    payload = json.loads(invoke(data_dir, "--json").output)
    assert payload["spend"]["openai-codex"]["5h"] == pytest.approx(2.5)
    assert "openai-codex" not in payload["usage"]


def test_caps_show_warning_line(data_dir: Path, tmp_path: Path):
    # Unknown model: default $15, week allowance $7.50; $6 is 80%.
    _seed_go_spend(data_dir, 6.0, tmp_path, "opencode-go/mystery")

    result = invoke(data_dir)
    assert result.exit_code == 0
    assert "opencode-go week usage 80% (warn at 80%), resets " in result.output


def test_caps_on_off_reset_with_json(data_dir: Path, config: dict):
    off = json.loads(invoke(data_dir, "off", "--json").output)
    assert off["enforced"] is False and off["source"] == "switch"
    on = json.loads(invoke(data_dir, "on", "--json").output)
    assert on["enforced"] is True and on["source"] == "switch"
    reset = json.loads(invoke(data_dir, "reset", "--json").output)
    assert reset["enforced"] is True and reset["source"] == "config"


def test_caps_on_off_reset_human_output(data_dir: Path) -> None:
    for command, expected in (
        ("off", "off (switch)"),
        ("on", "on (switch)"),
        ("reset", "on (config)"),
    ):
        result = invoke(data_dir, command)
        assert result.exit_code == 0, result.output
        assert f"caps enforcement: {expected}" in result.output


def test_caps_show_live_and_estimate(monkeypatch, data_dir, tmp_path):
    import helmraiser.loop.spend as spend

    _seed_go_spend(data_dir, 12.0, tmp_path)
    live = {
        "opencode-go": {"week": {"fraction": 0.07, "resets_at": time.time() + 300, "status": "ok"}},
        "openai-codex": {"week": {"fraction": 0.61, "resets_at": time.time() + 300}},
    }
    monkeypatch.setattr(spend, "live_for", lambda *args, **kwargs: live)

    human = invoke(data_dir)
    assert human.exit_code == 0
    assert "7% (est 40%)" in human.output
    assert "61%" in human.output

    payload = json.loads(invoke(data_dir, "--json").output)
    windows = payload["usage"]["opencode-go"]
    assert windows["week"]["source"] == "live"
    assert windows["week"]["fraction"] == pytest.approx(0.07)
    assert windows["week"]["live_fraction"] == pytest.approx(0.07)
    assert windows["week"]["estimate_fraction"] == pytest.approx(0.4)
    assert windows["5h"]["source"] == "estimate"
    assert payload["usage"]["openai-codex"]["week"] == {
        "fraction": pytest.approx(0.61),
        "spend_usd": 0.0,
        "resets_at": live["openai-codex"]["week"]["resets_at"],
        "warn_at": 0.8,
        "stop_at": None,
        "source": "live",
        "live_fraction": pytest.approx(0.61),
    }


def test_caps_shows_live_headroom(monkeypatch, data_dir):
    import helmraiser.loop.spend as spend

    week = 7 * 86400
    start = 1_791_158_400.0
    now = start + 0.46 * week
    live = {
        "openai-codex": {
            "week": {"fraction": 0.28, "resets_at": start + week},
        },
        "opencode-go": {
            "5h": {"fraction": 0.94, "resets_at": now + 3600},
            "week": {"fraction": 0.28, "resets_at": start + week},
            "month": {"fraction": 0.20, "resets_at": now + 15 * 86400},
        },
    }
    monkeypatch.setattr(caps_commands.time, "time", lambda: now)
    monkeypatch.setattr(spend, "live_for", lambda *args, **kwargs: live)

    payload = json.loads(invoke(data_dir, "--json").output)

    codex = payload["routing"]["headroom"]["openai-codex"]
    assert payload["usage"]["openai-codex"]["week"]["fraction"] == 0.28
    assert codex["week"] == {
        "used": 0.28,
        "elapsed": 0.46,
        "target": 0.95,
        "headroom": 0.157,
    }
    assert codex["has_headroom"] is True
    go = payload["routing"]["headroom"]["opencode-go"]
    assert payload["usage"]["opencode-go"]["5h"]["fraction"] == 0.94
    assert go["5h"]["headroom"] == pytest.approx(0.01)
    assert go["has_headroom"] is False

    human = invoke(data_dir)
    assert human.exit_code == 0, human.output
    assert "46%" in human.output and "+16 pts" in human.output
    assert "80%" in human.output and "+1 pts" in human.output


def test_caps_shows_live_window_without_reset(monkeypatch, data_dir):
    import helmraiser.loop.spend as spend

    live = {"openai-codex": {"week": {"fraction": 0.28, "resets_at": None}}}
    monkeypatch.setattr(spend, "live_for", lambda *args, **kwargs: live)
    monkeypatch.setenv("COLUMNS", "160")

    result = invoke(data_dir, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["usage"]["openai-codex"]["week"]["resets_at"] is None
    routing = payload["routing"]["headroom"]["openai-codex"]
    assert routing["week"]["elapsed"] is None
    assert routing["week"]["headroom"] is None
    assert routing["has_headroom"] is False

    human = invoke(data_dir)
    assert human.exit_code == 0, human.output
    row = next(line for line in human.output.splitlines() if "openai-codex" in line)
    assert row.split() == [
        "openai-codex",
        "week",
        "28%",
        "-",
        "unknown",
        "$0.0000",
        "unknown",
        "80%",
        "-",
    ]


def test_caps_refresh_forces_a_live_refresh(monkeypatch, data_dir):
    import helmraiser.loop.spend as spend

    seen: list[bool] = []

    def fake_live(ledger, config, now, refresh=False):
        seen.append(refresh)
        return {}

    monkeypatch.setattr(spend, "live_for", fake_live)

    invoke(data_dir, "--refresh")
    invoke(data_dir)

    assert seen == [True, False]
