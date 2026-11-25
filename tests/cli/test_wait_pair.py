"""``helmraiser wait <task>`` on a task with a pair: the task is the unit. Over a
seeded ledger: no worker, no pi; ``--timeout 0`` answers at the first poll."""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app
from helmraiser.ledger.db import Ledger
from helmraiser.loop import experiments, pairs
from tests.cli.test_wait import KEYS
from tests.loop.conftest import config, data_dir, ledger, make_task  # noqa: F401 -- fixtures
from tests.loop.test_experiment_forking import start
from tests.loop.test_pairs import claim_pair, fail, park

runner = CliRunner()
LABEL_KEYS = KEYS - {"task_id", "project", "timed_out"}


@pytest.fixture(autouse=True)
def reading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experiments, "codex_weekly_fraction", lambda *_args: 0.2)


@pytest.fixture
def pair(make_task, ledger, config) -> dict[str, Any]:  # noqa: F811
    """A claimed pair: X is ``starting``, Y ``resuming``."""
    start(ledger)
    return claim_pair(ledger, make_task(), config)


def wait(ref: str, data_dir, *args: str, human: bool = False):  # noqa: F811
    return runner.invoke(
        app,
        [
            "wait",
            ref,
            "--timeout",
            "0",
            "--data-dir",
            str(data_dir),
            *([] if human else ["--json"]),
            *args,
        ],
    )


def test_wait_goes_on_while_an_arm_is_under_way(pair, ledger, data_dir):  # noqa: F811
    park(ledger, pair["run_x"])

    result = wait(pair["task_id"], data_dir)

    assert result.exit_code == 124, result.output
    row = json.loads(result.stdout)
    assert row["timed_out"] is True and row["run_id"] is None
    assert row["pair"]["state"] == "under_way"
    assert row["pair"]["x"]["pending"] == "merge" and row["pair"]["y"]["status"] == "resuming"


def test_wait_returns_when_both_arms_can_be_picked(pair, ledger, data_dir):  # noqa: F811
    for run_id, verdict, cost in ((pair["run_x"], "pass", 0.7), (pair["run_y"], "concerns", 0.3)):
        park(ledger, run_id)
        ledger.update_run(run_id, verdict=verdict, cost_usd=cost)
    ledger.set_task_status(pair["task_id"], "awaiting_approval")

    result = wait("fix-widget", data_dir)

    assert result.exit_code == 0, result.output
    row = json.loads(result.stdout)
    # The row keeps the shape of a run's; the runs are under the pair's labels.
    assert set(row) == KEYS | {"pair"}
    assert (row["task_id"], row["status"], row["run_id"], row["timed_out"]) == (
        pair["task_id"],
        "awaiting_approval",
        None,
        False,
    )
    waited = row["pair"]
    assert set(waited) == {"state", "picked", "pick", "decided_by", "x", "y"}
    assert (waited["state"], waited["picked"], waited["pick"]) == ("awaiting_pick", False, None)
    assert set(waited["x"]) == set(waited["y"]) == LABEL_KEYS
    for label, run_id, verdict, cost in (
        ("x", pair["run_x"], "pass", 0.7),
        ("y", pair["run_y"], "concerns", 0.3),
    ):
        arm = waited[label]
        assert (arm["run_id"], arm["status"], arm["pending"], arm["verdict"], arm["cost_usd"]) == (
            run_id,
            "awaiting_approval",
            "merge",
            verdict,
            cost,
        )
    # Never the arm: no arm name, no model.
    assert "arm" not in result.stdout and "test/arm-b" not in result.stdout


def test_wait_on_a_run_of_a_pair_keeps_the_runs_row(pair, ledger, data_dir):  # noqa: F811
    park(ledger, pair["run_x"])

    result = wait(pair["run_x"], data_dir)

    assert result.exit_code == 0, result.output
    row = json.loads(result.stdout)
    assert set(row) == KEYS and row["run_id"] == pair["run_x"] and row["pending"] == "merge"


def test_wait_returns_when_an_arm_awaits_a_scope_approval(pair, ledger, data_dir):  # noqa: F811
    park(ledger, pair["run_y"], "scope")
    ledger.append_event(
        pair["run_y"], "helmraiser", "scope_requested", {"paths": ["docs/**"], "reason": "docs"}
    )

    result = wait(pair["task_id"], data_dir)

    assert result.exit_code == 0, result.output
    waited = json.loads(result.stdout)["pair"]
    assert waited["state"] == "needs_approval"
    assert waited["y"]["pending"] == "scope"
    assert waited["y"]["scope_request"] == {"paths": ["docs/**"], "reason": "docs"}
    assert waited["x"]["status"] == "starting"
    human = wait(pair["task_id"], data_dir, human=True)
    assert "pair: needs approval" in human.output
    assert f"Y: awaiting scope approval — review - — $0.0000 — run {pair['run_y']}" in human.output
    assert "    scope request: docs/** — docs" in human.output
    assert "next: helmraiser approve <run> (or reject) for the arm that awaits it" in human.output


def test_wait_returns_the_picked_run_when_the_other_arm_failed(
    pair, ledger, data_dir  # noqa: F811
):
    fail(ledger, pair["run_x"], "verify_exhausted")
    park(ledger, pair["run_y"])
    assert pairs.auto_pick(ledger, pair["run_y"]) == "Y"

    result = wait(pair["task_id"], data_dir)

    assert result.exit_code == 0, result.output
    row = json.loads(result.stdout)
    # A picked pair is waited for as the run its pick chose, an ordinary run.
    assert (row["run_id"], row["status"], row["pending"]) == (
        pair["run_y"],
        "awaiting_approval",
        "merge",
    )
    waited = row["pair"]
    assert (waited["state"], waited["picked"], waited["pick"], waited["decided_by"]) == (
        "picked",
        True,
        "Y",
        "failure",
    )
    assert waited["x"]["failure_reason"] == "verify_exhausted"
    human = wait(pair["task_id"], data_dir, human=True)
    assert "pair: picked Y (failure)" in human.output
    assert f"next: helmraiser approve {pair['run_y']} (or reject)" in human.output


def test_wait_returns_with_exit_1_when_both_arms_failed(pair, ledger, data_dir):  # noqa: F811
    fail(ledger, pair["run_x"], "verify_exhausted")
    fail(ledger, pair["run_y"], "pi_contract: test/arm-b:high gave no usage")

    # The pair has failed, whatever its task's status says.
    assert wait(pair["task_id"], data_dir).exit_code == 1
    ledger.set_task_status(pair["task_id"], "failed")
    result = wait(pair["task_id"], data_dir)

    assert result.exit_code == 1, result.output
    row = json.loads(result.stdout)
    assert (row["status"], row["pair"]["state"]) == ("failed", "failed")
    # A failure reason names no model before the pick.
    assert row["pair"]["y"]["failure_reason"] == "pi_contract: hidden gave no usage"
    human = wait(pair["task_id"], data_dir, human=True)
    assert f"failed {pair['task_id']} pair X, Y" in human.output
    assert f"next: helmraiser retry {pair['task_id']}" in human.output


def test_wait_until_end_needs_both_arms_over(pair, ledger, data_dir):  # noqa: F811
    park(ledger, pair["run_x"])
    park(ledger, pair["run_y"])

    parked = wait(pair["task_id"], data_dir, "--until", "end")

    assert parked.exit_code == 124, parked.output
    assert json.loads(parked.stdout)["pair"]["state"] == "awaiting_pick"

    fail(ledger, pair["run_x"], "rejected")
    fail(ledger, pair["run_y"], "rejected")
    ledger.set_task_status(pair["task_id"], "failed")
    over = wait(pair["task_id"], data_dir, "--until", "end")

    assert over.exit_code == 1, over.output


def test_wait_follows_the_picked_run_through_a_rework(pair, ledger, data_dir):  # noqa: F811
    park(ledger, pair["run_x"])
    park(ledger, pair["run_y"])
    assert pairs.record_pick(ledger, pair, "X", decided_by="judged", reason="r", judge="j")
    fail(ledger, pair["run_y"], "rejected")
    ledger.update_run(pair["run_x"], status="resuming", stage="implement")

    result = wait(pair["task_id"], data_dir)

    # Y started last and is over; the wait is for X, which the pick chose.
    assert result.exit_code == 124, result.output
    row = json.loads(result.stdout)
    assert (row["run_id"], row["status"]) == (pair["run_x"], "resuming")
    assert row["pair"]["state"] == "picked"


def test_wait_for_a_pair_that_is_queued_again_waits_for_its_new_runs(
    pair, ledger: Ledger, data_dir  # noqa: F811
):
    fail(ledger, pair["run_x"])
    fail(ledger, pair["run_y"])
    ledger.set_task_status(pair["task_id"], "queued")

    result = wait(pair["task_id"], data_dir)

    assert result.exit_code == 124, result.output
    row = json.loads(result.stdout)
    assert row["status"] == "queued" and "pair" not in row
