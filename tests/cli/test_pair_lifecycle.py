"""A pair from its fork to a decision: ``pick``, the automatic pick, ``approve`` and
``land`` refusing an arm, ``retry`` and ``abort`` on both arms, the end conditions,
dependents and the status output."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app
from helmraiser.ledger.db import Ledger
from helmraiser.loop import experiments
from helmraiser.loop.status import status_path
from helmraiser.task.loader import render_task, write_task
from tests.cli.test_experiment_forking import (  # noqa: F401 -- shared fixtures
    ARMS,
    REVIEW_PASS,
    cli_env,
    data_dir,
    queue_task,
    reading,
    start_experiment,
    work,
)
from tests.cli.test_scope_request import REQUEST, REQUESTED
from tests.conftest import FakePi
from tests.ledger.test_db import make_spec

runner = CliRunner()

REASON = "X handles the empty widget"
JUDGE = "claude-code/oracle (opus, xhigh)"
PICK = ("--reason", REASON, "--judge", JUDGE)


def invoke(*args: str, data_dir: Path):  # noqa: F811
    return runner.invoke(app, [*args, "--data-dir", str(data_dir)])


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def script(pi: FakePi, *, good: bool = True, cost: float = 0.05) -> None:
    """What the next sessions do: write the marker that verify wants, or nothing."""
    implement: dict[str, Any] = {"text": "Implemented.", "cost": cost}
    if good:
        implement["files"] = [["done.txt", "ok\n"]]
    pi.write_script(
        {"sessions": {"*-implement": implement, "*-review": {"text": REVIEW_PASS, "cost": 0.02}}}
    )


def work_once(data_dir: Path) -> dict[str, Any]:  # noqa: F811
    """One run of ``work --once``, whatever its end."""
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.output.splitlines()[0])


def run_arms(
    data_dir: Path, pi: FakePi, x: bool = True, y: bool = True  # noqa: F811
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run X, then Y: each reaches its merge approval (true) or fails verify."""
    script(pi, good=x)
    outcome_x = work_once(data_dir)
    script(pi, good=y)
    outcome_y = work_once(data_dir)
    for outcome, good in ((outcome_x, x), (outcome_y, y)):
        assert outcome["status"] == ("awaiting_approval" if good else "failed"), outcome
    return outcome_x, outcome_y


def open_ledger(data_dir: Path) -> Ledger:  # noqa: F811
    return Ledger(data_dir / "helmraiser.db")


@pytest.fixture
def pi(fake_pi: FakePi) -> FakePi:
    script(fake_pi)
    return fake_pi


# ── pick ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("as_json", [False, True])
def test_pick_rejects_the_loser_then_delivers_the_winner(
    tmp_project, data_dir, pi, reading, as_json  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    experiment_id = start_experiment(data_dir)
    x, y, _ = work(data_dir=data_dir)

    result = invoke(
        "pick", "fix-widget", "X", *PICK, *(["--json"] if as_json else []), data_dir=data_dir
    )

    assert result.exit_code == 0, result.output
    head = git(tmp_project, "rev-parse", "HEAD")
    with open_ledger(data_dir) as ledger:
        pair = ledger.get_pair(experiment_id, task_id)
        assert (pair["pick"], pair["decided_by"], pair["reason"], pair["judge"]) == (
            "X",
            "judged",
            REASON,
            JUDGE,
        )
        assert pair["picked_at"] is not None
        loser, winner = ledger.get_run(y["run_id"]), ledger.get_run(x["run_id"])
        assert (loser["status"], loser["failure_reason"]) == ("failed", "rejected")
        rejection = ledger.approvals(y["run_id"])[-1]
        assert (rejection["kind"], rejection["decision"], rejection["note"]) == (
            "merge",
            "rejected",
            "pair pick: Y lost",
        )
        assert (winner["status"], winner["head_commit"]) == ("landed", head)
        approval = ledger.approvals(x["run_id"])[-1]
        assert (approval["decision"], approval["note"]) == ("approved", REASON)
        assert ledger.get_task(task_id)["status"] == "landed"
    # The loser's branch is kept, its worktree is gone; the winner is in the base.
    assert y["branch_name"] in git(tmp_project, "branch", "--list")
    assert not Path(y["worktree_path"]).exists()
    assert (tmp_project / "done.txt").read_text() == "ok\n"
    if as_json:
        assert json.loads(result.output) == {
            "picked": "X",
            "task_id": task_id,
            "decided_by": "judged",
            "reason": REASON,
            "judge": JUDGE,
            "delivered": "X",
            "winner": {
                "label": "X",
                "run_id": x["run_id"],
                "status": "landed",
                "branch_name": x["branch_name"],
                "head": head,
            },
            "loser": {
                "label": "Y",
                "run_id": y["run_id"],
                "status": "failed",
                "branch_name": y["branch_name"],
                "worktree_removed": True,
                "worktree_error": None,
            },
            # The pick reveals the setups: which arm each label was.
            "reveal": {
                "X": {"arm": "B", "overrides": ARMS["B"]},
                "Y": {"arm": "A", "overrides": {}},
            },
            "experiment": {"name": "e", "status": "active", "end_reason": None},
        }
    else:
        lines = result.output.splitlines()
        assert lines[0] == f"picked X for {task_id}, judged by {JUDGE}"
        assert f"  Y rejected: run {y['run_id']}" in lines
        assert f"  branch kept: {y['branch_name']}" in lines
        assert f"  X landed: run {x['run_id']} → {head[:12]}" in lines
        assert (
            '  X was arm B: {"models": {"implement": {"model": "other-provider/arm-b"}}}' in lines
        )
        assert "  Y was arm A: {}" in lines


def test_pick_delivers_a_pull_request_and_the_last_pick_ends_the_experiment(
    tmp_project, tmp_path, data_dir, pi, reading, monkeypatch  # noqa: F811
):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(tmp_project, "remote", "add", "origin", str(remote))
    bin_dir = tmp_path / "gh-bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\necho https://example.test/pr/1\n", encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    task_id = queue_task(tmp_project, data_dir, extra="delivery: pr\n")
    experiment_id = start_experiment(data_dir, end_pairs=1)
    x, y, _ = work(data_dir=data_dir)

    # ``land`` approves too: it is refused like ``approve``.
    refused = invoke("land", y["run_id"], "--json", data_dir=data_dir)
    assert refused.exit_code == 2, refused.output
    assert f"use `helmraiser pick {task_id}`" in json.loads(refused.output)["error"]

    # A run of the pair names its task.
    result = invoke("pick", y["run_id"], "y", *PICK, "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["winner"] == {
        "label": "Y",
        "run_id": y["run_id"],
        "status": "landed",
        "branch_name": y["branch_name"],
        "pr_url": "https://example.test/pr/1",
    }
    assert payload["loser"]["run_id"] == x["run_id"] and payload["loser"]["status"] == "failed"
    assert payload["experiment"] == {"name": "e", "status": "ended", "end_reason": "end_pairs"}
    with open_ledger(data_dir) as ledger:
        delivered = ledger.get_run(y["run_id"])
        assert (delivered["status"], delivered["pr_url"]) == (
            "landed",
            "https://example.test/pr/1",
        )
        assert ledger.get_pair(experiment_id, task_id)["pick"] == "Y"
        assert ledger.active_experiment() is None
    assert git(remote, "rev-parse", f"refs/heads/{y['branch_name']}") == delivered["head_commit"]
    human = invoke("experiment", "status", data_dir=data_dir)
    assert human.output.strip() == "none"


def test_tie_is_recorded_as_a_tie_and_delivers_the_cheaper_arm(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    experiment_id = start_experiment(data_dir)
    script(pi, cost=0.05)
    x = work_once(data_dir)
    script(pi, cost=0.01)
    y = work_once(data_dir)

    result = invoke("pick", task_id, "tie", *PICK, "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert (payload["picked"], payload["decided_by"], payload["delivered"]) == ("tie", "tie", "Y")
    with open_ledger(data_dir) as ledger:
        pair = ledger.get_pair(experiment_id, task_id)
        assert (pair["pick"], pair["decided_by"]) == ("tie", "tie")
        assert ledger.get_run(y["run_id"])["status"] == "landed"
        assert ledger.approvals(x["run_id"])[-1]["note"] == "pair pick: X lost"
        assert ledger.experiment_pair_counts(experiment_id)["ties"] == 1
        assert ledger.get_task(task_id)["status"] == "landed"


def test_pick_is_refused_without_a_pair(tmp_project, data_dir, pi, reading):  # noqa: F811
    queue_task(tmp_project, data_dir)
    outcome, _summary = work(data_dir=data_dir)

    for ref in ("fix-widget", outcome["run_id"]):
        result = invoke("pick", ref, "X", *PICK, "--json", data_dir=data_dir)
        assert result.exit_code == 1, result.output
        assert "has no pair to pick" in json.loads(result.output)["error"]
    bad = invoke("pick", "fix-widget", "Z", *PICK, "--json", data_dir=data_dir)
    assert bad.exit_code == 2 and "pick X, Y or tie" in json.loads(bad.output)["error"]
    with open_ledger(data_dir) as ledger:
        assert ledger.get_run(outcome["run_id"])["status"] == "awaiting_approval"


def test_pick_is_refused_while_an_arm_is_under_way(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    experiment_id = start_experiment(data_dir)
    x = work_once(data_dir)

    result = invoke("pick", task_id, "X", *PICK, "--json", data_dir=data_dir)

    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["error"].startswith("arm Y is resuming; a pick needs")
    with open_ledger(data_dir) as ledger:
        assert ledger.get_pair(experiment_id, task_id)["pick"] is None
        assert ledger.get_run(x["run_id"])["status"] == "awaiting_approval"


def test_pick_of_a_failed_arm_is_refused_and_a_pair_is_picked_once(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)
    x, y, _ = work(data_dir=data_dir)
    # Rejected at its merge approval: no automatic pick, the pair still needs one.
    assert invoke("reject", x["run_id"], "--json", data_dir=data_dir).exit_code == 0

    for choice, message in (
        ("X", "arm X cannot be delivered: it is failed (rejected)"),
        ("tie", "a tie needs both arms awaiting merge approval"),
    ):
        result = invoke("pick", task_id, choice, *PICK, "--json", data_dir=data_dir)
        assert result.exit_code == 1, result.output
        assert message in json.loads(result.output)["error"]
    with open_ledger(data_dir) as ledger:
        assert ledger.pair_for_task(task_id)["pick"] is None
        assert ledger.get_run(y["run_id"])["status"] == "awaiting_approval"

    picked = invoke("pick", task_id, "Y", *PICK, "--json", data_dir=data_dir)

    assert picked.exit_code == 0, picked.output
    payload = json.loads(picked.output)
    assert payload["loser"] == {
        "label": "X",
        "run_id": x["run_id"],
        "status": "failed",
        "branch_name": x["branch_name"],
    }
    assert payload["winner"]["status"] == "landed"
    again = invoke("pick", task_id, "Y", *PICK, "--json", data_dir=data_dir)
    assert again.exit_code == 1, again.output
    assert "is already picked (Y, decided by judged)" in json.loads(again.output)["error"]


def test_a_refused_delivery_keeps_the_pick_and_approve_delivers_it(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)
    x, y, _ = work(data_dir=data_dir)
    (tmp_project / "dirty.txt").write_text("uncommitted\n")
    git(tmp_project, "add", "dirty.txt")

    result = invoke("pick", task_id, "X", *PICK, "--json", data_dir=data_dir)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["error"].startswith("finalize_refused:") and payload["delivered"] is None
    assert payload["winner"]["status"] == "awaiting_approval"
    assert payload["reveal"]["X"]["arm"] == "B"
    with open_ledger(data_dir) as ledger:
        assert ledger.pair_for_task(task_id)["pick"] == "X"
        assert ledger.get_run(y["run_id"])["status"] == "failed"
        assert ledger.get_task(task_id)["status"] == "awaiting_approval"
    # The winner is an ordinary run now.
    git(tmp_project, "rm", "-q", "--cached", "dirty.txt")
    (tmp_project / "dirty.txt").unlink()
    approved = invoke("approve", x["run_id"], "--json", data_dir=data_dir)
    assert approved.exit_code == 0, approved.output
    assert json.loads(approved.output)["status"] == "landed"


# ── approve ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("as_json", [False, True])
def test_approve_is_refused_for_an_arm_awaiting_merge(
    tmp_project, data_dir, pi, reading, as_json  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)
    x, y, _ = work(data_dir=data_dir)

    result = invoke("approve", x["run_id"], *(["--json"] if as_json else []), data_dir=data_dir)

    assert result.exit_code == 2, result.output
    message = f"this run is one arm of a pair; use `helmraiser pick {task_id}`"
    if as_json:
        assert json.loads(result.output) == {"error": message}
    else:
        assert "".join(message.split()) in "".join(result.output.split())
    with open_ledger(data_dir) as ledger:
        assert ledger.get_run(x["run_id"])["status"] == "awaiting_approval"
        assert ledger.pending_approval(x["run_id"])["kind"] == "merge"


@pytest.mark.parametrize("decline", [False, True])
def test_a_scope_request_of_an_arm_is_approved_or_declined_as_usual(
    tmp_project, data_dir, fake_pi, reading, decline  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir, extra='paths: ["done.txt"]\n')
    start_experiment(data_dir)
    fake_pi.write_script(
        {"sessions": {"*-implement": {"text": REQUEST, "files": [["done.txt", "part\n"]]}}}
    )
    x = work_once(data_dir)
    assert (x["status"], x["pending_approval"]) == ("awaiting_approval", "scope")

    result = invoke(
        "approve",
        x["run_id"],
        *(["--decline", "--note", "stay in scope"] if decline else []),
        "--json",
        data_dir=data_dir,
    )

    # The other arm has not started: it does not freeze the task against this one.
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["declined" if decline else "approved"] == "scope"
    assert payload["paths"] == REQUESTED and payload["status"] == "resuming"
    with open_ledger(data_dir) as ledger:
        assert ledger.pair_for_task(task_id)["pick"] is None
        assert ledger.get_run(x["run_id"])["stage"] == "implement"


def test_rejecting_an_arm_that_waits_on_scope_leaves_the_other_to_win(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir, extra='paths: ["done.txt"]\n')
    start_experiment(data_dir)
    x = work_once(data_dir)
    pi.write_script(
        {"sessions": {"*-implement": {"text": REQUEST, "files": [["done.txt", "part\n"]]}}}
    )
    y = work_once(data_dir)
    assert (y["status"], y["pending_approval"]) == ("awaiting_approval", "scope")
    with open_ledger(data_dir) as ledger:
        assert ledger.pair_for_task(task_id)["pick"] is None

    assert invoke("reject", y["run_id"], "--json", data_dir=data_dir).exit_code == 0

    with open_ledger(data_dir) as ledger:
        pair = ledger.pair_for_task(task_id)
        assert (pair["pick"], pair["decided_by"]) == ("X", "failure")
        assert pair["reason"] == "other arm failed: rejected"
        assert ledger.get_run(x["run_id"])["status"] == "awaiting_approval"
    written = json.loads(status_path(data_dir).read_text())
    assert written["awaiting_pick_count"] == 0 and written["awaiting_count"] == 1


def test_the_arm_left_by_a_failure_is_picked_but_not_delivered(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    experiment_id = start_experiment(data_dir)

    x, y = run_arms(data_dir, pi, x=False)

    with open_ledger(data_dir) as ledger:
        pair = ledger.get_pair(experiment_id, task_id)
        assert (pair["pick"], pair["decided_by"], pair["judge"]) == ("Y", "failure", "helmraiser")
        assert pair["reason"] == f"other arm failed: {x['failure_reason']}"
        assert ledger.get_run(y["run_id"])["status"] == "awaiting_approval"
        assert ledger.get_task(task_id)["status"] == "awaiting_approval"
        assert ledger.experiment_pair_counts(experiment_id)["failure_wins"] == {"A": 1, "B": 0}
    assert not (tmp_project / "done.txt").exists()
    status = json.loads(invoke("status", "--json", data_dir=data_dir).output)
    (row,) = status["awaiting"]
    assert row["pair"] == {
        "task_id": task_id,
        "label": "Y",
        "picked": True,
        "awaiting_pick": False,
    }
    assert status["awaiting_pick_count"] == 0
    refused = invoke("pick", task_id, "Y", *PICK, "--json", data_dir=data_dir)
    assert refused.exit_code == 1
    assert "is already picked (Y, decided by failure)" in json.loads(refused.output)["error"]

    # The coordinator reviews and approves it as any run.
    approved = invoke("approve", y["run_id"], "--json", data_dir=data_dir)

    assert approved.exit_code == 0, approved.output
    assert json.loads(approved.output)["status"] == "landed"
    assert (tmp_project / "done.txt").exists()


def test_the_picked_arm_is_retried_as_the_tasks_latest_run(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)
    x, y = run_arms(data_dir, pi, y=False)
    with open_ledger(data_dir) as ledger:
        assert ledger.pair_for_task(task_id)["pick"] == "X"
        # As after a rework that failed verify: the winner failed, after the loser started.
        assert ledger.get_run(y["run_id"])["started_at"] > ledger.get_run(x["run_id"])["started_at"]
        ledger.finish_run(x["run_id"], "failed", "verify_failed")
        ledger.set_task_status(task_id, "failed", run_id=x["run_id"])

    lost = invoke("retry", y["run_id"], "--json", data_dir=data_dir)
    assert lost.exit_code == 2 and "has moved on" in json.loads(lost.output)["error"]
    result = invoke("retry", x["run_id"], "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert (payload["retried"], payload["run_id"]) == ("verify_failed", x["run_id"])
    assert payload["status"] == "resuming"


# ── retry ─────────────────────────────────────────────────────────────


def test_retry_of_a_failed_pair_runs_both_arms_again_as_the_same_pair(
    tmp_project, data_dir, pi, reading, monkeypatch  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    experiment_id = start_experiment(data_dir)
    x, y = run_arms(data_dir, pi, x=False, y=False)
    with open_ledger(data_dir) as ledger:
        first = ledger.get_pair(experiment_id, task_id)
        assert first["pick"] is None and ledger.get_task(task_id)["status"] == "failed"

    # One arm alone is refused, naming the retry of the task.
    for run_id in (x["run_id"], y["run_id"]):
        alone = invoke("retry", run_id, "--json", data_dir=data_dir)
        assert alone.exit_code == 2, alone.output
        assert json.loads(alone.output)["error"] == (
            f"run {run_id} is one arm of a pair; retry both arms with "
            f"`helmraiser retry {task_id}`"
        )
    result = invoke("retry", "fix-widget", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload.pop("eligible")["task_ids"] == [task_id]
    assert payload == {
        "retried": "pair",
        "task_id": task_id,
        "status": "queued",
        "previous_runs": {"X": x["run_id"], "Y": y["run_id"]},
    }
    # The labels are the pair's, not a new draw.
    monkeypatch.setattr(experiments, "draw_labels", lambda: pytest.fail("labels drawn again"))
    script(pi)
    new_x, new_y, _ = work(data_dir=data_dir)

    assert new_x["status"] == new_y["status"] == "awaiting_approval"
    with open_ledger(data_dir) as ledger:
        again = ledger.get_pair(experiment_id, task_id)
        assert again["id"] == first["id"]
        assert (again["run_x"], again["run_y"]) == (new_x["run_id"], new_y["run_id"])
        assert (again["arm_x"], again["arm_y"], again["pick"]) == ("B", "A", None)
        assert {again["run_x"], again["run_y"]}.isdisjoint({x["run_id"], y["run_id"]})
        for run_id, arm in ((new_x["run_id"], "B"), (new_y["run_id"], "A")):
            (event,) = ledger.events(run_id, types=("experiment_arm",))
            assert event["payload"]["name"] == arm
        assert ledger.experiment_pair_counts(experiment_id)["total"] == 1
        assert len(ledger.list_runs(task_id=task_id)) == 4
    # The new pair is picked as any.
    assert invoke("pick", task_id, "X", *PICK, "--json", data_dir=data_dir).exit_code == 0


def test_retry_of_a_pair_needs_both_arms_over(tmp_project, data_dir, pi, reading):  # noqa: F811
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)
    script(pi, good=False)
    work_once(data_dir)

    result = invoke("retry", task_id, "--json", data_dir=data_dir)

    assert result.exit_code == 2, result.output
    error = json.loads(result.output)["error"]
    assert "cannot be retried: arm Y is resuming; both arms must have failed" in error
    flags = invoke("retry", task_id, "--implement", "--json", data_dir=data_dir)
    assert flags.exit_code == 2 and "apply to a run" in json.loads(flags.output)["error"]


# ── abort ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("by_task", [False, True])
def test_abort_of_one_arm_aborts_both(
    tmp_project, data_dir, pi, reading, monkeypatch, by_task  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)
    x = work_once(data_dir)
    monkeypatch.setenv("HELMRAISER_DATA_DIR", str(data_dir))
    with open_ledger(data_dir) as ledger:
        run_y = ledger.pair_for_task(task_id)["run_y"]

    result = runner.invoke(
        app, ["abort", task_id if by_task else x["run_id"], "--project-dir", str(tmp_project)]
    )

    assert result.exit_code == 0, result.output
    assert sorted(result.output.splitlines()) == sorted(
        f"run_id={run_id} status=aborted" for run_id in (x["run_id"], run_y)
    )
    with open_ledger(data_dir) as ledger:
        assert ledger.get_run(x["run_id"])["status"] == "aborted"
        assert ledger.get_run(run_y)["status"] == "aborted"
        assert ledger.pair_for_task(task_id)["pick"] is None
        assert ledger.get_task(task_id)["status"] == "aborted"
    # Nothing is left for the worker to start.
    assert json.loads(invoke("work", "--json", data_dir=data_dir).output) == {
        "runs": [],
        "paused": None,
    }


# ── end conditions ────────────────────────────────────────────────────


def test_a_task_claimed_after_the_end_date_ends_the_experiment_and_runs_once(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    experiment_id = start_experiment(data_dir, end_at=time.time() - 1)

    outcome, _summary = work(data_dir=data_dir)

    assert outcome["status"] == "awaiting_approval", outcome
    with open_ledger(data_dir) as ledger:
        ended = ledger.experiment_by_id(experiment_id)
        assert (ended["status"], ended["end_reason"]) == ("ended", "end_at")
        assert ledger.get_pair(experiment_id, task_id) is None
        assert ledger.events(outcome["run_id"], types=("experiment_arm",)) == []
    # Without a pair the run is approved as ever.
    assert invoke("approve", outcome["run_id"], "--json", data_dir=data_dir).exit_code == 0


# ── dependents ────────────────────────────────────────────────────────


def test_a_dependent_starts_after_the_picked_winner_lands(
    tmp_project, tmp_path, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)
    x, y, _ = work(data_dir=data_dir)
    dependent = replace(
        make_spec(tmp_project, "dependent", depends_on=("fix-widget",)),
        source_path=tmp_path / "dependent.md",
    )
    write_task(dependent, dependent.source_path)
    with open_ledger(data_dir) as ledger:
        ledger.upsert_task(dependent, render_task(dependent))
        assert ledger.next_queued() is None
        # The loser is the task's latest run: it must not stand for the task.
        assert ledger.get_run(y["run_id"])["started_at"] > ledger.get_run(x["run_id"])["started_at"]

    result = invoke("pick", task_id, "X", *PICK, "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["eligible"]["task_ids"] == [dependent.task_id]
    with open_ledger(data_dir) as ledger:
        assert ledger.get_run(y["run_id"])["status"] == "failed"
        assert ledger.held_tasks() == {}
        assert ledger.next_queued()["task_id"] == dependent.task_id


# ── status ────────────────────────────────────────────────────────────


def test_status_shows_the_pair_by_label_and_never_the_arm(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)
    x, y, _ = work(data_dir=data_dir)

    status = json.loads(invoke("status", "--json", data_dir=data_dir).output)

    assert status["awaiting_count"] == 2 and status["awaiting_pick_count"] == 1
    pairs = {row["run_id"]: row["pair"] for row in status["awaiting"]}
    assert pairs == {
        x["run_id"]: {"task_id": task_id, "label": "X", "picked": False, "awaiting_pick": True},
        y["run_id"]: {"task_id": task_id, "label": "Y", "picked": False, "awaiting_pick": True},
    }
    assert "other-provider/arm-b" not in json.dumps(status)
    written = json.loads(status_path(data_dir).read_text())
    assert written["awaiting_pick_count"] == 1
    human = invoke("status", data_dir=data_dir).output
    assert "merge (pair X)" in human and "merge (pair Y)" in human
    assert f"pair awaiting pick: helmraiser pick {task_id} X|Y|tie" in human

    assert invoke("pick", task_id, "Y", *PICK, "--json", data_dir=data_dir).exit_code == 0
    status = json.loads(invoke("status", "--json", data_dir=data_dir).output)
    decided = {"task_id": task_id, "picked": True, "awaiting_pick": False}
    assert status["awaiting_pick_count"] == 0 and status["awaiting"] == []
    assert [row["pair"] for row in status["landed"]] == [{**decided, "label": "Y"}]
    assert [row["pair"] for row in status["failed"]] == [{**decided, "label": "X"}]


def test_status_of_an_unpaired_run_is_unchanged(tmp_project, data_dir, pi, reading):  # noqa: F811
    queue_task(tmp_project, data_dir)
    work(data_dir=data_dir)

    status = json.loads(invoke("status", "--json", data_dir=data_dir).output)

    (row,) = status["awaiting"]
    assert "pair" not in row and status["awaiting_pick_count"] == 0
    human = invoke("status", data_dir=data_dir).output
    assert "(pair " not in human and "awaiting pick" not in human
