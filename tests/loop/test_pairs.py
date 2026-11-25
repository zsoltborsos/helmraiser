"""A pair after its fork: pickability, the tie rule, the automatic pick, the end
conditions, retried pairs, abort and dependencies."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from helmraiser.ledger.db import Ledger, RunUnavailable, TaskActive, pair_loser
from helmraiser.loop import experiments, pairs
from helmraiser.loop.abort import abort_runs
from helmraiser.loop.experiments import fork_decision
from helmraiser.loop.reconcile import find_phantom_runs, reconcile
from helmraiser.loop.runner import run_record
from helmraiser.loop.status import build_status
from helmraiser.storage.state import StateManager
from helmraiser.task.model import TaskSpec
from tests.loop.test_dispatch_routing import NOW
from tests.loop.test_experiment_forking import ARMS, adapter, resume, start
from tests.loop.test_reconcile import FakeController, _dead_pid
from tests.runner.test_abort import FakeProcessController


@pytest.fixture(autouse=True)
def reading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experiments, "codex_weekly_fraction", lambda ledger, config, now: 0.2)


def claim_pair(
    ledger: Ledger, task: TaskSpec, config: dict[str, Any], labels: tuple[str, str] = ("B", "A")
) -> dict[str, Any]:
    """Claim queued ``task`` as a pair of the active experiment, without running it:
    X is ``starting`` and Y ``resuming`` at stage ``new``."""
    experiment = ledger.active_experiment()
    run_x, run_y = str(uuid4()), str(uuid4())
    ledger.claim_queued_run(
        run_record(task, run_x, pi_version="0.87.1", started_at=1000.0),
        snapshot=task.source_path.read_text(encoding="utf-8"),
        config=config,
        pair={
            "experiment_id": experiment["id"],
            "arm_x": labels[0],
            "arm_y": labels[1],
            "run": run_record(task, run_y, pi_version="0.87.1", started_at=1000.0),
            "events": [],
        },
    )
    pair = ledger.pair_for_run(run_x)
    assert pair is not None and (pair["run_x"], pair["run_y"]) == (run_x, run_y)
    return pair


def park(ledger: Ledger, run_id: str, kind: str = "merge") -> None:
    """As a run's end leaves it when it awaits an approval of ``kind``."""
    if kind == "merge":
        ledger.append_event(run_id, "helmraiser", "merge_decision", {"action": "keep"})
    ledger.update_run(run_id, status="awaiting_approval", stage=None)
    ledger.record_approval(run_id, kind, "pending")


def fail(ledger: Ledger, run_id: str, reason: str = "verify_exhausted") -> None:
    ledger.finish_run(run_id, "aborted" if reason.startswith("aborted") else "failed", reason)


def reject(ledger: Ledger, run_id: str) -> None:
    """As ``helmraiser reject`` leaves a run that awaited an approval."""
    ledger.decide_approval(int(ledger.pending_approval(run_id)["id"]), "rejected")
    ledger.finish_run(run_id, "failed", "rejected")


def reloaded(ledger: Ledger, pair: dict[str, Any]) -> dict[str, Any]:
    return ledger.pair_for_task(pair["task_id"])


# ── picking ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("costs", "iterations", "delivered"),
    [
        ((0.5, 0.2), (1, 1), "Y"),
        ((0.2, 0.5), (3, 1), "X"),
        ((0.3, 0.3), (2, 1), "Y"),
        ((0.3, 0.3), (1, 2), "X"),
        ((0.3, 0.3), (1, 1), "X"),
    ],
)
def test_tie_delivers_the_cheaper_arm_then_fewer_iterations_then_x(
    make_task, ledger, config, costs, iterations, delivered
):
    start(ledger)
    pair = claim_pair(ledger, make_task(), config)
    for run_id, cost, count in zip((pair["run_x"], pair["run_y"]), costs, iterations, strict=True):
        park(ledger, run_id)
        ledger.update_run(run_id, cost_usd=cost)
        for n in range(1, count + 1):
            ledger.start_iteration(run_id, n, f"{run_id}-implement", "a/impl")

    states = pairs.arms(ledger, pair)

    assert pairs.delivered_label(ledger, states, "tie") == delivered
    assert pairs.delivered_label(ledger, states, "X") == "X"
    assert pairs.delivered_label(ledger, states, "Y") == "Y"


def test_pick_is_refused_until_both_arms_are_settled(make_task, ledger, config):
    start(ledger)
    pair = claim_pair(ledger, make_task(), config)
    park(ledger, pair["run_x"])

    def refusal(pick: str) -> str:
        with pytest.raises(pairs.PickRefused) as raised:
            pairs.delivered_label(ledger, pairs.arms(ledger, pair), pick)
        return str(raised.value)

    # Y has not started.
    assert refusal("X").startswith("arm Y is resuming; a pick needs both arms")
    assert not pairs.awaiting_pick(ledger, pair)
    # Y waits on a scope approval.
    park(ledger, pair["run_y"], "scope")
    assert refusal("X").startswith("arm Y is awaiting scope approval; a pick needs both arms")
    assert not pairs.awaiting_pick(ledger, pair)
    # Y was rejected: X can be delivered, Y cannot, and that is no tie.
    reject(ledger, pair["run_y"])
    assert pairs.delivered_label(ledger, pairs.arms(ledger, pair), "X") == "X"
    assert pairs.awaiting_pick(ledger, pair)
    assert refusal("Y") == (
        "arm Y cannot be delivered: it is failed (rejected), not awaiting merge approval"
    )
    assert refusal("tie").startswith("arm Y is failed (rejected); a tie needs both arms")
    # Both over: nothing to deliver.
    fail(ledger, pair["run_x"])
    assert "arm X cannot be delivered" in refusal("X")
    assert not pairs.awaiting_pick(ledger, pair)


def test_a_pick_is_recorded_once_with_an_event_on_each_run(make_task, ledger, config):
    start(ledger)
    pair = claim_pair(ledger, make_task(), config)

    assert pairs.record_pick(ledger, pair, "Y", decided_by="judged", reason="why", judge="who")
    assert not pairs.record_pick(ledger, pair, "X", decided_by="judged", reason="no", judge="no")

    row = reloaded(ledger, pair)
    assert (row["pick"], row["decided_by"], row["reason"], row["judge"]) == (
        "Y",
        "judged",
        "why",
        "who",
    )
    assert row["picked_at"] is not None
    for label, run_id in (("X", pair["run_x"]), ("Y", pair["run_y"])):
        (event,) = ledger.events(run_id, types=("pair_pick",))
        assert event["payload"] == {
            "label": label,
            "pick": "Y",
            "decided_by": "judged",
            "reason": "why",
            "judge": "who",
        }
    # The reveal names each label's arm with its overrides.
    assert pairs.reveal(ledger, row) == {
        "X": {"arm": "B", "overrides": ARMS["B"]},
        "Y": {"arm": "A", "overrides": {}},
    }


def test_a_ties_loser_is_the_run_that_pick_rejected():
    pair = {"run_x": "x", "run_y": "y", "pick": "tie"}
    rejected = {"status": "failed", "failure_reason": "rejected"}
    landed = {"status": "landed", "failure_reason": None}
    waiting = {"status": "awaiting_approval", "failure_reason": None}
    assert pair_loser(pair, {"x": rejected, "y": landed}) == "x"
    assert pair_loser(pair, {"x": landed, "y": rejected}) == "y"
    # Nothing rejected yet, or both rejected since: no single loser.
    assert pair_loser(pair, {"x": waiting, "y": waiting}) is None
    assert pair_loser(pair, {"x": rejected, "y": rejected}) is None
    assert pair_loser({**pair, "pick": "X"}, {}) == "y"
    assert pair_loser({**pair, "pick": "Y"}, {}) == "x"
    assert pair_loser({**pair, "pick": None}, {"x": rejected, "y": landed}) is None


# ── the automatic pick ────────────────────────────────────────────────


@pytest.mark.parametrize("failed", ["X", "Y"])
def test_auto_pick_when_one_arm_fails_and_the_other_awaits_merge(make_task, ledger, config, failed):
    start(ledger)
    pair = claim_pair(ledger, make_task(), config)
    loser = pair[f"run_{failed.lower()}"]
    winner = pair["run_y" if failed == "X" else "run_x"]
    park(ledger, winner)
    assert pairs.auto_pick(ledger, winner) is None  # the other arm is still under way
    fail(ledger, loser, "budget:cost")

    picked = pairs.auto_pick(ledger, loser)

    assert picked == pairs.other(failed)
    row = reloaded(ledger, pair)
    assert (row["pick"], row["decided_by"], row["reason"], row["judge"]) == (
        picked,
        "failure",
        "other arm failed: budget:cost",
        "helmraiser",
    )
    # Not delivered: the winner still awaits its merge approval.
    assert ledger.get_run(winner)["status"] == "awaiting_approval"
    assert ledger.pending_approval(winner)["kind"] == "merge"
    # Set once.
    assert pairs.auto_pick(ledger, loser) is None and pairs.auto_pick(ledger, winner) is None
    assert len(ledger.events(winner, types=("pair_pick",))) == 1


def test_an_arm_rejected_before_its_merge_approval_loses_by_failure(make_task, ledger, config):
    start(ledger)
    pair = claim_pair(ledger, make_task(), config)
    park(ledger, pair["run_x"], "scope")
    reject(ledger, pair["run_x"])
    park(ledger, pair["run_y"])

    assert pairs.auto_pick(ledger, pair["run_x"]) == "Y"
    assert reloaded(ledger, pair)["reason"] == "other arm failed: rejected"


@pytest.mark.parametrize("case", ["both_failed", "both_aborted", "rejected", "scope", "unpaired"])
def test_no_auto_pick(make_task, ledger, config, case):
    start(ledger)
    pair = claim_pair(ledger, make_task(), config)
    run_x, run_y = pair["run_x"], pair["run_y"]
    if case == "both_failed":
        fail(ledger, run_x)
        fail(ledger, run_y)
    elif case == "both_aborted":
        fail(ledger, run_x, "aborted: by the owner")
        fail(ledger, run_y, "aborted: by the owner")
    elif case == "rejected":
        # X reached its merge approval and was rejected there: a judgement, for pick.
        park(ledger, run_x)
        reject(ledger, run_x)
        park(ledger, run_y)
    elif case == "scope":
        fail(ledger, run_x)
        park(ledger, run_y, "scope")
    else:
        fail(ledger, run_x)
        park(ledger, run_y)

    assert pairs.auto_pick(ledger, "no-such-run" if case == "unpaired" else run_x) is None
    assert pairs.auto_pick(ledger, "no-such-run" if case == "unpaired" else run_y) is None
    assert reloaded(ledger, pair)["pick"] is None


async def test_a_failed_arm_makes_the_other_win_when_its_run_ends(
    make_task, make_run, ledger, config, data_dir
):
    experiment_id = start(ledger)
    task = make_task()
    arm, pair = fork_decision(task, ledger, config, NOW).runs(labels=("A", "B"))
    run_x, _ = make_run(task, adapter(), arm=arm, pair=pair, auth_check=lambda model: "no login")

    outcome_x = await run_x.run()

    assert outcome_x.status == "failed"
    row = ledger.get_pair(experiment_id, task.task_id)
    assert row["pick"] is None
    outcome_y = await resume(row["run_y"], adapter(), config, ledger, data_dir).run()

    assert (outcome_y.status, outcome_y.pending_approval) == ("awaiting_approval", "merge")
    row = ledger.get_pair(experiment_id, task.task_id)
    assert (row["pick"], row["decided_by"], row["judge"]) == ("Y", "failure", "helmraiser")
    assert row["reason"] == f"other arm failed: {outcome_x.failure_reason}"
    assert ledger.get_run(row["run_y"])["status"] == "awaiting_approval"
    assert ledger.get_task(task.task_id)["status"] == "awaiting_approval"


async def test_both_arms_failing_leaves_the_pair_undecided(
    make_task, make_run, ledger, config, data_dir
):
    experiment_id = start(ledger)
    task = make_task()
    arm, pair = fork_decision(task, ledger, config, NOW).runs(labels=("A", "B"))
    run_x, _ = make_run(task, adapter(), arm=arm, pair=pair, auth_check=lambda model: "no login")
    await run_x.run()
    run_y_id = ledger.get_pair(experiment_id, task.task_id)["run_y"]

    run_y = resume(run_y_id, adapter(), config, ledger, data_dir)
    run_y.auth_check = lambda model: "no login"
    outcome_y = await run_y.run()

    assert outcome_y.status == "failed"
    assert ledger.get_pair(experiment_id, task.task_id)["pick"] is None
    assert ledger.get_task(task.task_id)["status"] == "failed"


async def test_reconcile_of_a_dead_arm_picks_the_other(
    make_task, make_run, ledger, config, data_dir, tmp_project
):
    start(ledger)
    task = make_task()
    arm, pair = fork_decision(task, ledger, config, NOW).runs(labels=("A", "B"))
    run_x, _ = make_run(task, adapter(), arm=arm, pair=pair)
    await run_x.run()
    run_y_id = ledger.pair_for_run(run_x.run_id)["run_y"]
    run_y = resume(run_y_id, adapter(), config, ledger, data_dir)
    assert run_y.claim_resume()
    state = StateManager(tmp_project / ".helmraiser" / "state.db")
    state.release_lock(run_y_id, run_y.lock_topic)
    assert state.acquire_lock(run_y_id, run_y.lock_topic, _dead_pid())

    reconcile(ledger, find_phantom_runs(ledger), FakeController())

    row = ledger.pair_for_run(run_x.run_id)
    assert (row["pick"], row["decided_by"]) == ("X", "failure")
    assert row["reason"].startswith("other arm failed: orphaned: worker")
    assert ledger.get_run(run_x.run_id)["status"] == "awaiting_approval"


async def test_a_paired_run_waits_for_the_pick_under_policy_auto(
    make_task, make_run, ledger, config, data_dir
):
    start(ledger)
    task = make_task(policy="auto")
    arm, pair = fork_decision(task, ledger, config, NOW).runs(labels=("A", "B"))
    run_x, _ = make_run(task, adapter(), arm=arm, pair=pair)

    outcome_x = await run_x.run()
    run_y_id = ledger.pair_for_run(run_x.run_id)["run_y"]
    outcome_y = await resume(run_y_id, adapter(), config, ledger, data_dir).run()

    for outcome in (outcome_x, outcome_y):
        assert (outcome.status, outcome.pending_approval) == ("awaiting_approval", "merge")
    assert ledger.pair_for_run(run_x.run_id)["pick"] is None


# ── end conditions ────────────────────────────────────────────────────


def test_the_experiment_ends_when_its_decided_pairs_reach_end_pairs(make_task, ledger, config):
    experiment_id = start(ledger, end_pairs=2)
    first = claim_pair(ledger, make_task(task_id="one"), config)

    assert pairs.record_pick(ledger, first, "tie", decided_by="tie", reason="same", judge="j")
    assert ledger.active_experiment()["id"] == experiment_id
    for run_id in (first["run_x"], first["run_y"]):
        fail(ledger, run_id)
    # A pick by failure counts as decided too.
    second = claim_pair(ledger, make_task(task_id="two"), config)
    park(ledger, second["run_x"])
    fail(ledger, second["run_y"])
    assert pairs.auto_pick(ledger, second["run_y"]) == "X"

    ended = ledger.experiment_by_id(experiment_id)
    assert (ended["status"], ended["end_reason"]) == ("ended", "end_pairs")
    assert ledger.active_experiment() is None


def test_a_pair_stays_pickable_after_its_experiment_ended(make_task, ledger, config):
    experiment_id = start(ledger, end_pairs=1)
    pair = claim_pair(ledger, make_task(), config)
    ledger.end_experiment(experiment_id, "stopped")
    park(ledger, pair["run_x"])
    park(ledger, pair["run_y"])

    assert pairs.awaiting_pick(ledger, pair)
    assert pairs.record_pick(ledger, pair, "X", decided_by="judged", reason="r", judge="j")

    assert ledger.experiment_by_id(experiment_id)["end_reason"] == "stopped"
    assert ledger.experiment_pair_counts(experiment_id)["judged_wins"] == {"A": 0, "B": 1}


def test_fork_decision_ends_the_experiment_at_its_end_date(make_task, ledger, config):
    experiment_id = start(ledger, end_at=NOW)
    task = make_task()

    assert fork_decision(task, ledger, config, NOW - 1) is not None
    assert ledger.active_experiment() is not None
    assert fork_decision(task, ledger, config, NOW) is None

    ended = ledger.experiment_by_id(experiment_id)
    assert (ended["status"], ended["end_reason"]) == ("ended", "end_at")
    assert fork_decision(task, ledger, config, NOW + 1) is None


# ── a retried pair ────────────────────────────────────────────────────


def test_a_failed_unpicked_pair_is_claimed_again_with_its_labels(make_task, ledger, config):
    experiment_id = start(ledger)
    task = make_task()
    pair = claim_pair(ledger, task, config, labels=("B", "A"))
    # One arm still under way, or delivered: the re-queued task runs normally.
    fail(ledger, pair["run_x"])
    assert fork_decision(task, ledger, config, NOW) is None
    fail(ledger, pair["run_y"], "aborted: by the owner")

    fork = fork_decision(task, ledger, config, NOW)

    assert fork is not None and fork.paired and fork.labels == ("B", "A")
    arm, other = fork.runs()
    assert (arm.name, other.arm.name) == ("B", "A")

    ledger.set_task_status(task.task_id, "queued")
    with pytest.raises(RunUnavailable) as refused:
        claim_pair(ledger, task, config, labels=("A", "B"))  # not the pair's arms
    assert refused.value.hold == {"reason": "experiment_changed"}
    again = claim_pair(ledger, task, config, labels=("B", "A"))

    assert again["id"] == pair["id"] and again["experiment_id"] == experiment_id
    assert (again["arm_x"], again["arm_y"], again["pick"]) == ("B", "A", None)
    assert {again["run_x"], again["run_y"]}.isdisjoint({pair["run_x"], pair["run_y"]})
    assert ledger.pair_for_run(pair["run_x"]) is None
    assert ledger.experiment_pair_counts(experiment_id)["total"] == 1


def test_a_picked_pair_is_not_claimed_again(make_task, ledger, config):
    start(ledger)
    task = make_task()
    pair = claim_pair(ledger, task, config)
    fail(ledger, pair["run_x"])
    fail(ledger, pair["run_y"])
    ledger.set_pair_pick(pair["id"], "X", decided_by="judged")

    assert fork_decision(task, ledger, config, NOW) is None
    ledger.set_task_status(task.task_id, "queued")
    with pytest.raises(RunUnavailable) as refused:
        claim_pair(ledger, task, config)
    assert refused.value.hold == {"reason": "experiment_changed"}


def test_task_pair_resolves_a_task_id_and_no_run_id(make_task, ledger, config):
    start(ledger)
    task = make_task()
    make_task(task_id="unpaired")
    pair = claim_pair(ledger, task, config)

    assert pairs.task_pair(ledger, task.task_id)["id"] == pair["id"]
    assert pairs.task_pair(ledger, task.id)["id"] == pair["id"]
    assert ledger.pair_for_task(task.task_id)["id"] == pair["id"]
    assert pairs.task_pair(ledger, pair["run_x"]) is None
    assert pairs.task_pair(ledger, "unpaired") is None
    assert pairs.task_pair(ledger, "no-such-task") is None


def test_a_scope_approval_may_change_the_snapshot_beside_the_other_arm(make_task, ledger, config):
    start(ledger)
    task = make_task()
    pair = claim_pair(ledger, task, config)
    park(ledger, pair["run_x"], "scope")
    snapshot = task.source_path.read_text(encoding="utf-8")
    changed = snapshot + "\nMore.\n"

    # Y (not started) shares the task: it does not freeze the snapshot against X.
    assert ledger.set_task_snapshot(task.task_id, changed, parked_run=pair["run_x"])
    assert ledger.get_task(task.task_id)["snapshot"] == changed
    # Without a parked run of its own, the task's snapshot stays frozen.
    with pytest.raises(TaskActive):
        ledger.set_task_snapshot(task.task_id, snapshot, parked_run=pair["run_y"])
    with pytest.raises(TaskActive):
        ledger.set_task_snapshot(task.task_id, snapshot)


# ── abort ─────────────────────────────────────────────────────────────


@pytest.fixture
def abort_env(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """``abort`` finds the ledger through HELMRAISER_DATA_DIR."""
    monkeypatch.setenv("HELMRAISER_DATA_DIR", str(data_dir))


@pytest.mark.parametrize("ref", ["live", "parked", "task"])
def test_abort_of_one_arm_aborts_both_and_picks_neither(
    make_task, ledger, config, tmp_project, abort_env, ref
):
    start(ledger)
    task = make_task()
    pair = claim_pair(ledger, task, config)
    live, parked = pair["run_x"], pair["run_y"]
    # X is live with no worker left; Y is parked for its merge approval.
    park(ledger, parked)
    state = StateManager(tmp_project / ".helmraiser" / "state.db")
    state.create_run(live, f"{task.id}/{live}")
    state.create_run(parked, f"{task.id}/{parked}")
    state.update_run_status(parked, "succeeded")

    aborted = abort_runs(
        tmp_project,
        {"live": live, "parked": parked, "task": task.task_id}[ref],
        FakeProcessController(),
    )

    # The parked arm first: the end of the live one must not make it win.
    assert aborted == [parked, live]
    assert [ledger.get_run(run_id)["status"] for run_id in aborted] == ["aborted", "aborted"]
    assert reloaded(ledger, pair)["pick"] is None
    assert ledger.get_task(task.task_id)["status"] == "aborted"


def test_abort_of_two_parked_arms_picks_neither(make_task, ledger, config, tmp_project, abort_env):
    start(ledger)
    task = make_task()
    pair = claim_pair(ledger, task, config)
    # X waits on a scope approval, Y on its merge approval: neither has a process, and
    # X's abort alone would leave Y to win by failure.
    park(ledger, pair["run_x"], "scope")
    park(ledger, pair["run_y"])
    state = StateManager(tmp_project / ".helmraiser" / "state.db")
    for run_id in (pair["run_x"], pair["run_y"]):
        state.create_run(run_id, f"{task.id}/{run_id}")
        state.update_run_status(run_id, "succeeded")

    aborted = abort_runs(tmp_project, pair["run_x"], FakeProcessController())

    assert aborted == [pair["run_x"], pair["run_y"]]
    assert [ledger.get_run(run_id)["status"] for run_id in aborted] == ["aborted", "aborted"]
    assert reloaded(ledger, pair)["pick"] is None


def test_abort_closes_an_arm_that_has_not_started(
    make_task, ledger, config, tmp_project, abort_env
):
    start(ledger)
    task = make_task()
    pair = claim_pair(ledger, task, config)
    state = StateManager(tmp_project / ".helmraiser" / "state.db")
    state.create_run(pair["run_x"], f"{task.id}/{pair['run_x']}")

    aborted = abort_runs(tmp_project, pair["run_x"], FakeProcessController())

    # Y had no process and no state row: it must not start after the abort.
    assert aborted == [pair["run_y"], pair["run_x"]]
    row = ledger.get_run(pair["run_y"])
    assert (row["status"], row["failure_reason"]) == ("aborted", "aborted: before it started")
    assert ledger.get_run(pair["run_x"])["status"] == "aborted"
    assert ledger.next_resumable(config=config) is None
    assert reloaded(ledger, pair)["pick"] is None


def test_abort_of_a_picked_pairs_loser_leaves_the_winner(
    make_task, ledger, config, tmp_project, abort_env
):
    start(ledger)
    task = make_task()
    pair = claim_pair(ledger, task, config)
    park(ledger, pair["run_y"])
    fail(ledger, pair["run_x"])
    assert pairs.auto_pick(ledger, pair["run_x"]) == "Y"
    state = StateManager(tmp_project / ".helmraiser" / "state.db")
    for run_id in (pair["run_x"], pair["run_y"]):
        state.create_run(run_id, f"{task.id}/{run_id}")
        state.update_run_status(run_id, "failed" if run_id == pair["run_x"] else "succeeded")

    assert abort_runs(tmp_project, pair["run_x"], FakeProcessController()) == [pair["run_x"]]

    assert ledger.get_run(pair["run_y"])["status"] == "awaiting_approval"


def test_abort_of_an_unknown_ref(tmp_project, ledger, abort_env):
    assert abort_runs(tmp_project, str(uuid4()), FakeProcessController()) == []
    with pytest.raises(ValueError, match="UUID"):
        abort_runs(tmp_project, "no-such-task", FakeProcessController())


# ── status ────────────────────────────────────────────────────────────


def test_status_names_the_pair_by_label_and_counts_pairs_awaiting_pick(
    make_task, ledger, config, data_dir
):
    start(ledger)
    task = make_task()
    make_task(task_id="unpaired")
    pair = claim_pair(ledger, task, config)

    status = build_status(ledger, config=config, data_dir=data_dir)

    assert status["awaiting_pick_count"] == 0
    (running,) = status["running"]
    (resuming,) = status["resuming"]
    assert running["pair"] == {
        "task_id": task.task_id,
        "label": "X",
        "picked": False,
        "awaiting_pick": False,
    }
    assert resuming["pair"] == {**running["pair"], "label": "Y"}

    park(ledger, pair["run_x"])
    park(ledger, pair["run_y"])
    assert build_status(ledger, config=config, data_dir=data_dir)["awaiting_pick_count"] == 1
    assert pairs.run_pair(ledger, pair["run_y"]) == {
        "task_id": task.task_id,
        "label": "Y",
        "picked": False,
        "awaiting_pick": True,
    }
    pairs.record_pick(ledger, pair, "X", decided_by="judged", reason="r", judge="j")
    assert build_status(ledger, config=config, data_dir=data_dir)["awaiting_pick_count"] == 0
    assert pairs.run_pair(ledger, pair["run_x"]) == {
        "task_id": task.task_id,
        "label": "X",
        "picked": True,
        "awaiting_pick": False,
    }
    assert pairs.run_pair(ledger, "no-such-run") is None
