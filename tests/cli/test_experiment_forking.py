"""``helmraiser work`` under an active experiment: a queued task forks into a pair."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, commands
from helmraiser.ledger.db import Ledger
from helmraiser.loop import experiments
from helmraiser.storage.state import StateManager
from tests.conftest import FakePi

runner = CliRunner()

REVIEW_PASS = '```json\n{"verdict": "pass", "findings": [], "evidence_checks": []}\n```'
ARM_B_MODEL = "other-provider/arm-b:high"
ARMS = {"A": {}, "B": {"models": {"implement": {"model": "other-provider/arm-b"}}}}

TASK = """---
id: fix-widget
title: Fix the widget
project: {project}
status: draft
policy: {policy}
isolation: none
{extra}budget:
  max_cost_usd: 2.0
  max_minutes: 30
  max_iterations: 3
verify:
  - name: marker
    run: test -f done.txt
---
## Goal

Make the widget work.
"""


@pytest.fixture(autouse=True)
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configured_config: dict) -> dict:
    monkeypatch.setattr(commands, "load_config", lambda: configured_config)
    monkeypatch.setattr(commands, "get_intake_adapter", lambda config: None)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    return configured_config


@pytest.fixture
def reading(monkeypatch: pytest.MonkeyPatch) -> dict[str, float | None]:
    """The codex weekly usage fraction, and X on arm B for every pair."""
    value: dict[str, float | None] = {"fraction": 0.2}
    monkeypatch.setattr(
        experiments, "codex_weekly_fraction", lambda ledger, config, now: value["fraction"]
    )
    monkeypatch.setattr(experiments, "draw_labels", lambda: ("B", "A"))
    return value


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def pi(fake_pi: FakePi) -> FakePi:
    fake_pi.write_script(
        {
            "sessions": {
                "*-plan": {"text": "# Plan\n\n1. touch done.txt"},
                "*-implement": {"text": "Implemented.", "files": [["done.txt", "ok\n"]]},
                "*-review": {"text": REVIEW_PASS},
            }
        }
    )
    return fake_pi


def queue_task(project: Path, data_dir: Path, policy: str = "review", extra: str = "") -> str:
    path = project.parent / "tasks" / "fix-widget.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(TASK.format(project=project, policy=policy, extra=extra), encoding="utf-8")
    result = runner.invoke(
        app, ["intake", str(path), "--yes", "--json", "--data-dir", str(data_dir)]
    )
    assert result.exit_code == 0, result.output
    return str(json.loads(result.output)["task_id"])


def start_experiment(data_dir: Path, **options: Any) -> int:
    with Ledger(data_dir / "helmraiser.db") as ledger:
        return ledger.start_experiment("e", ARMS, **{"quota_brake": 0.85, **options})


def work(*args: str, data_dir: Path) -> list[dict[str, Any]]:
    result = runner.invoke(app, ["work", *args, "--json", "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.output
    return [json.loads(line) for line in result.output.strip().splitlines()]


def implement_models(pi: FakePi) -> dict[str, str]:
    """run ID → the model of its implement session."""
    return {
        call["session_id"].removesuffix("-implement"): call["argv"][
            call["argv"].index("--model") + 1
        ]
        for call in pi.calls()
        if (call["session_id"] or "").endswith("-implement")
    }


def test_work_runs_both_arms_of_a_queued_task(tmp_project, data_dir, pi, reading):
    task_id = queue_task(tmp_project, data_dir)
    experiment_id = start_experiment(data_dir)

    x, y, summary = work(data_dir=data_dir)

    assert x["status"] == y["status"] == "awaiting_approval", (x, y)
    assert x["task_id"] == y["task_id"] == task_id and x["run_id"] != y["run_id"]
    assert summary["summary"]["counts"]["awaiting_approval"] == 2
    with Ledger(data_dir / "helmraiser.db") as ledger:
        pair = ledger.get_pair(experiment_id, task_id)
        assert (pair["run_x"], pair["run_y"]) == (x["run_id"], y["run_id"])
        assert (pair["arm_x"], pair["arm_y"], pair["pick"]) == ("B", "A", None)
        assert ledger.get_task(task_id)["status"] == "awaiting_approval"
        arms = {
            run["run_id"]: ledger.events(run["run_id"], types=("experiment_arm",))[0]["payload"]
            for run in ledger.list_runs()
        }
        assert {run_id: arm["name"] for run_id, arm in arms.items()} == {
            x["run_id"]: "B",
            y["run_id"]: "A",
        }
        assert "run_resume" not in [e["type"] for e in ledger.events(y["run_id"])]
        assert ledger.get_run(y["run_id"])["model_implement"] != ARM_B_MODEL
        assert ledger.get_run(x["run_id"])["model_implement"] == "other-provider/arm-b"
    # Each arm ran with its own model, from the same base, in its own worktree.
    models = implement_models(pi)
    assert models[x["run_id"]] == "other-provider/arm-b"
    assert models[y["run_id"]] == "openai-codex/gpt-6.1-sol:high"
    assert x["base_commit"] == y["base_commit"] and x["worktree_path"] != y["worktree_path"]
    # Names carry no arm.
    for outcome in (x, y):
        assert outcome["branch_name"] == f"feature/fix-widget/{outcome['run_id']}"
    state = StateManager(tmp_project / ".helmraiser/state.db")
    assert state.get_run(x["run_id"])["topic_id"] == f"fix-widget/{x['run_id']}"
    assert state.get_run(y["run_id"])["topic_id"] == f"fix-widget/{y['run_id']}"


def test_once_starts_the_pair_and_a_second_pass_runs_the_other_arm(
    tmp_project, data_dir, pi, reading
):
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)

    x, _summary = work("--once", data_dir=data_dir)

    with Ledger(data_dir / "helmraiser.db") as ledger:
        waiting = ledger.get_run(ledger.pair_for_run(x["run_id"])["run_y"])
        assert (waiting["status"], waiting["stage"]) == ("resuming", "new")
        assert ledger.get_task(task_id)["status"] == "resuming"

    y, _summary = work("--once", data_dir=data_dir)

    assert y["run_id"] == waiting["run_id"] and y["status"] == "awaiting_approval"
    assert y["base_commit"] == x["base_commit"]


@pytest.mark.parametrize(
    "reason", ["no_experiment", "ended", "opt_out", "excluded", "plan_first", "paired"]
)
def test_task_runs_as_before_when_it_does_not_fork(tmp_project, data_dir, pi, reading, reason):
    task_id = queue_task(
        tmp_project,
        data_dir,
        policy="approve-plan-first" if reason == "plan_first" else "review",
        extra="experiment: false\n" if reason == "opt_out" else "",
    )
    if reason != "no_experiment":
        experiment_id = start_experiment(
            data_dir,
            exclude_projects=[str(tmp_project.resolve())] if reason == "excluded" else [],
        )
        with Ledger(data_dir / "helmraiser.db") as ledger:
            if reason == "ended":
                ledger.end_experiment(experiment_id, "stopped")
            if reason == "paired":
                ledger.create_pair(experiment_id, task_id, "A", "B")

    outcome, _summary = work(data_dir=data_dir)

    assert outcome["status"] == "awaiting_approval", outcome
    assert outcome["pending_approval"] == ("plan" if reason == "plan_first" else "merge")
    with Ledger(data_dir / "helmraiser.db") as ledger:
        assert [run["run_id"] for run in ledger.list_runs()] == [outcome["run_id"]]
        assert ledger.pair_for_run(outcome["run_id"]) is None
        assert ledger.events(outcome["run_id"], types=("experiment_arm",)) == []
        assert ledger.get_task(task_id)["status"] == "awaiting_approval"
    state = StateManager(tmp_project / ".helmraiser/state.db")
    assert state.get_run(outcome["run_id"])["topic_id"] == "fix-widget"
    if reason != "plan_first":
        assert implement_models(pi) == {outcome["run_id"]: "openai-codex/gpt-6.1-sol:high"}


@pytest.mark.parametrize("fraction", [0.85, None])
def test_at_the_brake_the_task_runs_unpaired_on_arm_a(tmp_project, data_dir, pi, reading, fraction):
    task_id = queue_task(tmp_project, data_dir)
    experiment_id = start_experiment(data_dir, quota_brake=0.85)
    reading["fraction"] = fraction

    outcome, _summary = work(data_dir=data_dir)

    assert outcome["status"] == "awaiting_approval", outcome
    with Ledger(data_dir / "helmraiser.db") as ledger:
        assert [run["run_id"] for run in ledger.list_runs()] == [outcome["run_id"]]
        assert ledger.get_pair(experiment_id, task_id) is None
        (arm,) = ledger.events(outcome["run_id"], types=("experiment_arm",))
        assert arm["payload"]["name"] == "A"
    state = StateManager(tmp_project / ".helmraiser/state.db")
    assert state.get_run(outcome["run_id"])["topic_id"] == "fix-widget"


@pytest.mark.parametrize(("fraction", "skipped"), [(0.2, True), (0.99, False)])
def test_skip_checks_the_models_of_every_arm_that_would_run(
    tmp_project, data_dir, pi, reading, fraction, skipped
):
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)
    reading["fraction"] = fraction
    with Ledger(data_dir / "helmraiser.db") as ledger:
        # Only arm B uses this provider.
        ledger.hold_provider("other-provider", time.time() + 3600, None)

    first, *_rest = work(data_dir=data_dir)

    with Ledger(data_dir / "helmraiser.db") as ledger:
        if skipped:
            assert first["skipped"] == task_id and first["reason"] == "quota:other-provider"
            assert not ledger.list_runs() and not pi.calls()
            assert ledger.get_task(task_id)["status"] == "queued"
        else:
            # Above the brake only arm A runs, and nothing holds its provider.
            assert first["status"] == "awaiting_approval"
            assert len(ledger.list_runs()) == 1


def test_supervisor_skip_checks_both_arms(tmp_project, data_dir, pi, reading, cli_env):
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)
    with Ledger(data_dir / "helmraiser.db") as ledger:
        ledger.hold_provider("other-provider", time.time() + 3600, None)

    result = runner.invoke(
        app, ["work", "--max-parallel", "2", "--json", "--data-dir", str(data_dir)]
    )

    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.output.strip().splitlines()]
    skips = [line for line in lines if line.get("skipped") == task_id]
    assert skips and skips[0]["reason"] == "quota:other-provider"
    assert not [line for line in lines if "started" in line]
    assert not pi.calls()


def test_dry_run_shows_the_task_without_an_arm(tmp_project, data_dir, pi, reading):
    queue_task(tmp_project, data_dir)
    start_experiment(data_dir)

    (plan,) = work("--dry-run", data_dir=data_dir)

    assert plan["dry_run"] is True
    assert plan["argv"][plan["argv"].index("--model") + 1] == "openai-codex/gpt-6.1-sol:high"
    with Ledger(data_dir / "helmraiser.db") as ledger:
        assert not ledger.list_runs()
