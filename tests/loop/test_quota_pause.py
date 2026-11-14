"""Quota errors hold one provider and may escalate to a fallback provider."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from helmraiser.cli import app, caps_commands, commands, monitor
from helmraiser.config import SUBSCRIPTION_TEMPLATES
from helmraiser.ledger.db import Ledger
from helmraiser.loop.budget import PAUSE_SECONDS
from helmraiser.task.model import ModelSpec
from tests.conftest import FakePi, configured_test_config
from tests.loop.conftest import FakeClock
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

runner = CliRunner()

TASK = """---
id: fix-widget
title: Fix the widget
project: {project}
status: draft
policy: review
isolation: none
{models}{budget}verify:
  - name: marker
    run: test -f done.txt
---
## Goal

Make the widget work.
"""


async def test_quota_escalates_to_fallback_on_another_provider(make_task, make_run, ledger):
    task = make_task()
    task.models["implement"] = ModelSpec.parse("opencode-go/implement")
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(ok=False, error_kind="quota", error_message="429 rate limit hit"),
                PiScript(files=[("done.txt", "ok\n")]),
            ]
        }
    )
    clock = FakeClock(time.time())
    run, sleep = make_run(task, adapter, clock=clock)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval"
    assert outcome.escalated and outcome.iterations == 2
    assert outcome.paused is None and ledger.is_paused() is None
    assert ledger.provider_holds(clock.now) == {
        "opencode-go": {
            "until": clock.now + PAUSE_SECONDS,
            "detail": "429 rate limit hit",
        }
    }
    implement_calls = adapter.calls_for("implement")
    assert [call.model.render() for call in implement_calls] == [
        "opencode-go/implement",
        "openai-codex/gpt-6-astra:high",
    ]
    assert implement_calls[1].session_id == f"{run.run_id}-implement-2"
    assert sleep.calls == []
    events = ledger.events(run.run_id)
    assert "provider_held" in [event["type"] for event in events]
    escalated = next(event for event in events if event["type"] == "escalated")
    assert escalated["payload"]["reason"] == "quota"


@pytest.mark.parametrize("fallback", [True, False])
async def test_quota_without_cross_provider_fallback_fails(make_task, make_run, ledger, fallback):
    task = make_task(fallback=fallback)
    adapter = ScriptedPiAdapter(
        {"implement": PiScript(ok=False, error_kind="quota", error_message="429 rate limit hit")}
    )
    clock = FakeClock(time.time())
    run, _ = make_run(task, adapter, clock=clock)

    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == "quota: 429 rate limit hit"
    assert outcome.paused is None and ledger.is_paused() is None
    assert ledger.provider_holds(clock.now)["openai-codex"]["until"] == pytest.approx(
        clock.now + PAUSE_SECONDS
    )
    assert len(adapter.calls) == 1


@pytest.fixture
def cli_config(monkeypatch: pytest.MonkeyPatch) -> dict:
    config = configured_test_config()
    config["subscriptions"] = copy.deepcopy(SUBSCRIPTION_TEMPLATES)
    monkeypatch.setattr(commands, "load_config", lambda: config)
    monkeypatch.setattr(caps_commands, "load_config", lambda: config)
    monkeypatch.setattr(commands, "get_intake_adapter", lambda config: None)
    return config


def _queue_task(
    tmp_project: Path, data_dir: Path, budget: str = "", *, models: dict | None = None
) -> str:
    path = tmp_project / "topics" / "fix-widget.md"
    path.parent.mkdir(exist_ok=True)
    model_block = yaml.safe_dump({"models": models}) if models else ""
    path.write_text(
        TASK.format(project=tmp_project, budget=budget, models=model_block), encoding="utf-8"
    )
    result = runner.invoke(
        app, ["intake", str(path), "--yes", "--json", "--data-dir", str(data_dir)]
    )
    assert result.exit_code == 0, result.output
    return str(json.loads(result.output)["task_id"])


def test_work_fails_without_pausing_when_pi_reports_quota(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, cli_config: dict
):
    task_id = _queue_task(tmp_project, data_dir)
    fake_pi.write_script(
        {"default": {"stop_reason": "error", "error_message": "429 Too Many Requests"}}
    )

    result = runner.invoke(app, ["work", "--once", "--json", "--data-dir", str(data_dir)])

    assert result.exit_code == 1, result.output
    lines = [json.loads(line) for line in result.output.strip().splitlines()]
    outcome = lines[0]
    assert outcome["status"] == "failed" and outcome["failure_reason"].startswith("quota:")
    assert lines[-1]["paused"] is None
    summary = lines[-1]["summary"]
    assert summary["counts"]["failed"] == 1
    assert summary["paused"] is None
    assert "openai-codex" in summary["held_providers"]
    ledger = Ledger(data_dir / "helmraiser.db")
    assert ledger.is_paused() is None
    assert "openai-codex" in ledger.provider_holds(time.time())
    assert ledger.get_task(task_id)["status"] == "failed"
    # Exactly one implement spawn; no 30 s retry for quota.
    assert [c["session_id"] for c in fake_pi.calls() if c["session_id"]] == [
        f"{outcome['run_id']}-implement"
    ]


def test_work_exits_3_when_already_paused(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, cli_config: dict
):
    _queue_task(tmp_project, data_dir)
    Ledger(data_dir / "helmraiser.db").pause_queue("manual")
    result = runner.invoke(app, ["work", "--data-dir", str(data_dir)])
    assert result.exit_code == 3
    assert "paused" in result.output.lower() and "manual" in result.output
    assert fake_pi.calls() == []


def test_work_skips_held_provider_and_runs_next_provider(
    tmp_project: Path,
    tmp_path: Path,
    data_dir: Path,
    fake_pi: FakePi,
    cli_config: dict,
    git_init,
):
    _use_go_profiles(cli_config)
    # This task must keep its provider when profiles change for the next task.
    skipped_task = _queue_task(tmp_project, data_dir, models=cli_config["profiles"])
    other = tmp_path / "other-provider"
    other.mkdir()
    git_init(other)
    cli_config["profiles"] = configured_test_config()["profiles"]
    codex_task = _queue_task(other, data_dir)
    ledger = Ledger(data_dir / "helmraiser.db")
    until = time.time() + 600
    ledger.hold_provider("opencode-go", until, "quota exhausted")
    fake_pi.write_script({"default": {"files": [["done.txt", "ok\n"]]}})

    result = runner.invoke(app, ["work", "--once", "--json", "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    skipped, outcome, batch = [json.loads(line) for line in result.output.strip().splitlines()]
    assert skipped == {
        "skipped": skipped_task,
        "reason": "quota:opencode-go",
        "until": until,
    }
    assert ledger.get_run(outcome["run_id"])["task_id"] == codex_task
    assert batch["summary"]["skipped_task_ids"] == [skipped_task]
    assert "opencode-go" in batch["summary"]["held_providers"]


def test_work_skips_task_whose_review_provider_is_held(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, cli_config: dict
):
    cli_config["profiles"]["implement"]["model"] = "opencode-go/implement"
    task_id = _queue_task(tmp_project, data_dir)
    ledger = Ledger(data_dir / "helmraiser.db")
    ledger.hold_provider("openai-codex", time.time() + 600, "quota exhausted")

    result = runner.invoke(app, ["work", "--once", "--json", "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    skipped = json.loads(result.output.strip().splitlines()[0])
    assert skipped["skipped"] == task_id and skipped["reason"] == "quota:openai-codex"
    assert fake_pi.calls() == []


def test_expired_hold_no_longer_blocks(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, cli_config: dict
):
    _use_go_profiles(cli_config)
    _queue_task(tmp_project, data_dir)
    ledger = Ledger(data_dir / "helmraiser.db")
    ledger.hold_provider("opencode-go", time.time() - 1, "quota exhausted")
    fake_pi.write_script({"default": {"files": [["done.txt", "ok\n"]]}})

    result = runner.invoke(app, ["work", "--once", "--json", "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    assert any(call["session_id"].endswith("-implement") for call in fake_pi.calls())
    assert ledger.provider_holds(time.time()) == {}


def _use_go_profiles(config: dict) -> None:
    for name in ("implement", "fallback", "review"):
        config["profiles"][name]["model"] = f"opencode-go/{name}"


def _seed_go_spend(ledger: Ledger, task_id: str, project: Path, cost: float = 5.0) -> float:
    started = time.time() - 60
    ledger.create_run(
        {
            "run_id": "prior",
            "task_id": task_id,
            "project": str(project),
            "status": "landed",
            "policy": "review",
            "isolation": "none",
            "model_implement": "opencode-go/implement",
            "model_review": "opencode-go/review",
            "pi_version": "0.87.1",
            "cost_usd": cost,
            "started_at": started,
            "ended_at": started,
        }
    )
    return started


def test_work_skips_task_at_stop_at_and_runs_codex_task(
    tmp_project: Path,
    tmp_path: Path,
    data_dir: Path,
    fake_pi: FakePi,
    cli_config: dict,
    git_init,
):
    cli_config["subscriptions"]["opencode-go"]["stop_at"] = 0.9
    _use_go_profiles(cli_config)
    # This task must keep its provider when profiles change for the next task.
    task_id = _queue_task(tmp_project, data_dir, models=cli_config["profiles"])
    ledger = Ledger(data_dir / "helmraiser.db")
    # Unknown go model: default $15 monthly, 5h share 0.2 -> $3 allowance.
    # $2.85 is 95% of it.
    started = _seed_go_spend(ledger, task_id, tmp_project, cost=2.85)
    other = tmp_path / "other"
    other.mkdir()
    git_init(other)
    cli_config["profiles"] = configured_test_config()["profiles"]
    codex_task = _queue_task(other, data_dir)
    fake_pi.write_script({"default": {"files": [["done.txt", "ok\n"]]}})

    result = runner.invoke(app, ["work", "--once", "--json", "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    skipped, outcome, _summary = [json.loads(line) for line in result.output.strip().splitlines()]
    assert skipped["skipped"] == task_id
    assert skipped["reason"] == "usage:opencode-go:5h"
    assert skipped["until"] == pytest.approx(started + 5 * 3600)
    assert ledger.is_paused() is None
    assert ledger.get_run(outcome["run_id"])["task_id"] == codex_task
    # The batch summary carries the usage warning for the 95% window.
    assert any(
        w.startswith("opencode-go 5h usage 95% (warn at 80%)")
        for w in _summary["summary"]["warnings"]
    )


def test_work_runs_go_task_at_95_percent_without_stop_at(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, cli_config: dict
):
    _use_go_profiles(cli_config)
    task_id = _queue_task(tmp_project, data_dir)
    _seed_go_spend(Ledger(data_dir / "helmraiser.db"), task_id, tmp_project, cost=2.85)
    fake_pi.write_script({"default": {"files": [["done.txt", "ok\n"]]}})

    result = runner.invoke(app, ["work", "--once", "--json", "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    assert any(call["session_id"].endswith("-implement") for call in fake_pi.calls())


def test_go_task_under_stop_at_runs(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, cli_config: dict
):
    cli_config["subscriptions"]["opencode-go"]["stop_at"] = 0.9
    _use_go_profiles(cli_config)
    # $1 task budget: 5h projection is 1/3 of the $3 window allowance.
    task_id = _queue_task(tmp_project, data_dir, budget="budget: {max_cost_usd: 1.0}\n")
    _seed_go_spend(Ledger(data_dir / "helmraiser.db"), task_id, tmp_project, cost=0.5)
    fake_pi.write_script({"default": {"files": [["done.txt", "ok\n"]]}})

    result = runner.invoke(app, ["work", "--once", "--json", "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    assert any(call["session_id"].endswith("-implement") for call in fake_pi.calls())


@pytest.mark.parametrize("override", ["switch", "flag"])
def test_caps_overrides_allow_work(
    tmp_project: Path,
    data_dir: Path,
    fake_pi: FakePi,
    cli_config: dict,
    override: str,
):
    cli_config["subscriptions"]["opencode-go"]["stop_at"] = 0.9
    _use_go_profiles(cli_config)
    task_id = _queue_task(tmp_project, data_dir)
    ledger = Ledger(data_dir / "helmraiser.db")
    _seed_go_spend(ledger, task_id, tmp_project)
    fake_pi.write_script({"default": {"files": [["done.txt", "ok\n"]]}})
    args = ["work", "--once", "--json", "--data-dir", str(data_dir)]
    if override == "switch":
        changed = runner.invoke(app, ["caps", "off", "--json", "--data-dir", str(data_dir)])
        assert json.loads(changed.output)["enforced"] is False
    else:
        args.insert(2, "--no-caps")

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert any(call["session_id"].endswith("-implement") for call in fake_pi.calls())
    assert ledger.caps_switch() == ("off" if override == "switch" else None)


def _never_startable_config(config: dict, projection_usd: float) -> None:
    """A $15-limit model (the default), default window shares, stop_at 0.7."""
    config["subscriptions"]["opencode-go"]["stop_at"] = 0.7
    config["subscriptions"]["opencode-go"]["projection_usd"] = projection_usd
    _use_go_profiles(config)


def _write_task(tmp_project: Path) -> Path:
    path = tmp_project / "topics" / "fix-widget.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(TASK.format(project=tmp_project, budget="", models=""), encoding="utf-8")
    return path


def test_work_says_a_never_startable_task_will_never_start(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, cli_config: dict
):
    _never_startable_config(cli_config, projection_usd=1.0)
    task_id = _queue_task(tmp_project, data_dir)
    # Raised after queueing: $5 / ($15 * 0.2) = 167% of the 5h window at zero usage.
    cli_config["subscriptions"]["opencode-go"]["projection_usd"] = 5.0

    result = runner.invoke(app, ["work", "--once", "--json", "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    skipped = json.loads(result.output.strip().splitlines()[0])
    assert skipped["skipped"] == task_id
    assert skipped["reason"] == "never_startable:opencode-go:5h"
    assert "until" not in skipped
    assert skipped["model"] == "opencode-go/implement"
    assert skipped["projected"] == pytest.approx(1.6667)
    assert fake_pi.calls() == []

    text = runner.invoke(app, ["work", "--once", "--data-dir", str(data_dir)])
    assert f"skipped {task_id}: never_startable:opencode-go:5h: will never start" in text.output


def test_intake_refuses_a_never_startable_task_when_caps_are_enforced(
    tmp_project: Path, data_dir: Path, cli_config: dict
):
    _never_startable_config(cli_config, projection_usd=5.0)
    path = _write_task(tmp_project)

    result = runner.invoke(app, ["intake", str(path), "--yes", "--data-dir", str(data_dir)])

    assert result.exit_code != 0
    for part in (
        "refusing to queue fix-widget",
        "never_startable:opencode-go:5h",
        "opencode-go/implement",
        "167%",
        "stop_at 70%",
        "projection_usd",
    ):
        assert part in result.output
    assert Ledger(data_dir / "helmraiser.db").list_tasks(status="queued") == []

    proposed = runner.invoke(
        app, ["intake", str(path), "--propose", "--json", "--data-dir", str(data_dir)]
    )
    assert proposed.exit_code == 0, proposed.output
    notes = json.loads(proposed.output)["notes"]
    assert any(
        n.startswith("never_startable:opencode-go:5h: will never start")
        and n.endswith("intake --yes will refuse it")
        for n in notes
    )


def test_intake_queues_at_the_default_projection(
    tmp_project: Path, data_dir: Path, cli_config: dict
):
    _never_startable_config(cli_config, projection_usd=1.0)

    task_id = _queue_task(tmp_project, data_dir)

    assert Ledger(data_dir / "helmraiser.db").get_task(task_id)["status"] == "queued"


def test_intake_warns_about_a_never_startable_task_when_caps_are_off(
    tmp_project: Path, data_dir: Path, cli_config: dict
):
    _never_startable_config(cli_config, projection_usd=5.0)
    changed = runner.invoke(app, ["caps", "off", "--json", "--data-dir", str(data_dir)])
    assert json.loads(changed.output)["enforced"] is False
    path = _write_task(tmp_project)

    result = runner.invoke(
        app, ["intake", str(path), "--yes", "--json", "--data-dir", str(data_dir)]
    )

    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["status"] == "queued"
    assert any(
        n.startswith("never_startable:opencode-go:5h (caps are not enforced") for n in out["notes"]
    )


def test_status_lists_never_startable_tasks(
    tmp_project: Path, data_dir: Path, cli_config: dict, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(monitor, "load_config", lambda: cli_config)
    _never_startable_config(cli_config, projection_usd=1.0)
    task_id = _queue_task(tmp_project, data_dir)

    quiet = json.loads(runner.invoke(app, ["status", "--json", "--data-dir", str(data_dir)]).output)
    assert quiet["never_startable"] == []

    cli_config["subscriptions"]["opencode-go"]["projection_usd"] = 5.0
    out = json.loads(runner.invoke(app, ["status", "--json", "--data-dir", str(data_dir)]).output)
    [row] = out["never_startable"]
    assert row["task_id"] == task_id and row["id"] == "fix-widget"
    assert row["reason"] == "never_startable:opencode-go:5h"
    assert row["stop_at"] == 0.7 and row["projection_usd"] == 5.0
    assert row["text"].startswith("will never start")

    text = runner.invoke(app, ["status", "--data-dir", str(data_dir)])
    # Rich wraps the long line; compare without whitespace.
    assert f"Neverstarts:{task_id}:willneverstart" in "".join(text.output.split())

    runner.invoke(app, ["caps", "off", "--json", "--data-dir", str(data_dir)])
    off = json.loads(runner.invoke(app, ["status", "--json", "--data-dir", str(data_dir)]).output)
    assert off["never_startable"] == []
