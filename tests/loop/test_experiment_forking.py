"""Forking a queued task into a pair: the decision, the claim and the pair's two runs."""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from helmraiser.ledger.db import Ledger, RunUnavailable
from helmraiser.loop import experiments, runner
from helmraiser.loop.experiments import Fork, RunArm, RunPair, fork_decision
from helmraiser.loop.reconcile import find_phantom_runs, reconcile
from helmraiser.loop.routing import route_dispatch
from helmraiser.loop.runner import GovernedRun, pair_lock_topic
from helmraiser.storage.state import StateManager
from helmraiser.task.model import ModelSpec
from tests.ledger.test_db import make_v11_ledger
from tests.loop.conftest import FakeSleep
from tests.loop.test_dispatch_routing import NOW, _readings
from tests.loop.test_planned_runs import PLAN, REVIEW
from tests.loop.test_reconcile import FakeController, _dead_pid
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

ARM_B_MODEL = ModelSpec("test/arm-b", "high")
ARMS = {"A": {}, "B": {"models": {"implement": {"model": "test/arm-b", "thinking": "high"}}}}


def start(ledger: Ledger, **options: Any) -> int:
    return ledger.start_experiment("e", ARMS, **{"quota_brake": 0.85, **options})


@pytest.fixture
def reading(monkeypatch: pytest.MonkeyPatch) -> dict[str, float | None]:
    """The codex weekly usage fraction that ``fork_decision`` reads."""
    value: dict[str, float | None] = {"fraction": 0.2}
    monkeypatch.setattr(
        experiments, "codex_weekly_fraction", lambda ledger, config, now: value["fraction"]
    )
    return value


def adapter() -> ScriptedPiAdapter:
    return ScriptedPiAdapter(
        {
            "plan": PiScript(final_text=PLAN),
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": PiScript(final_text=REVIEW),
        }
    )


def git(project: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=project, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def commit_on_main(project: Path) -> str:
    (project / "later.txt").write_text("later\n")
    git(project, "add", "later.txt")
    git(project, "commit", "-q", "-m", "later")
    return git(project, "rev-parse", "HEAD")


def resume(run_id: str, pi: ScriptedPiAdapter, config, ledger, data_dir, **overrides: Any):
    return GovernedRun.resume(
        run_id,
        config=config,
        ledger=ledger,
        data_dir=data_dir,
        adapter_factory=lambda state, isolation: pi,
        pi_version="0.87.1",
        auth_check=lambda model: None,
        **{"sleep": FakeSleep(), **overrides},
    )


# ── the decision ──────────────────────────────────────────────────────


def test_no_fork_without_an_active_experiment(make_task, ledger, config, monkeypatch):
    monkeypatch.setattr(
        experiments, "codex_weekly_fraction", lambda *args: pytest.fail("brake read")
    )
    task = make_task()
    assert fork_decision(task, ledger, config, NOW) is None
    ledger.end_experiment(start(ledger), "stopped")
    assert fork_decision(task, ledger, config, NOW) is None


def test_no_fork_on_a_ledger_without_experiment_tables(make_task, config, tmp_path, reading):
    path = tmp_path / "v11.db"
    make_v11_ledger(path, run_status="running")
    with Ledger(path) as old:
        assert fork_decision(make_task(), old, config, NOW) is None


def test_fork_into_a_pair_below_the_brake(make_task, ledger, config, reading):
    experiment_id = start(ledger)
    task = make_task()

    fork = fork_decision(task, ledger, config, NOW)

    assert fork is not None and fork.paired and fork.experiment_id == experiment_id
    assert fork.arms["A"] == RunArm("A")
    assert fork.arms["B"] == RunArm("B", {"implement": ARM_B_MODEL})
    assert [t.models["implement"] for t in fork.tasks(task, config)] == [
        task.models["implement"],
        ARM_B_MODEL,
    ]


@pytest.mark.parametrize("reason", ["opt_out", "plan_first", "excluded", "paired"])
def test_no_fork(make_task, ledger, config, reading, tmp_project, tmp_path, reason):
    task = make_task(policy="approve-plan-first" if reason == "plan_first" else "review")
    if reason == "excluded":
        # Exclusions are stored resolved; the task may name its project through a link.
        link = tmp_path / "link"
        link.symlink_to(tmp_project)
        task = replace(task, project=link)
    experiment_id = start(
        ledger, exclude_projects=[str(tmp_project.resolve())] if reason == "excluded" else []
    )
    if reason == "opt_out":
        task = replace(task, experiment=False)
    if reason == "paired":
        # A re-queued task whose pair exists runs normally.
        ledger.create_pair(experiment_id, task.task_id, "A", "B")

    assert fork_decision(task, ledger, config, NOW) is None


@pytest.mark.parametrize("fraction", [0.85, 0.99, None])
def test_at_or_above_the_brake_or_without_a_reading_the_task_runs_unpaired_on_arm_a(
    make_task, ledger, config, reading, fraction
):
    start(ledger, quota_brake=0.85)
    reading["fraction"] = fraction
    task = make_task()

    fork = fork_decision(task, ledger, config, NOW)

    assert fork is not None and not fork.paired
    assert fork.runs() == (RunArm("A"), None)
    assert fork.tasks(task, config) == [task]


def test_brake_reads_the_codex_weekly_window(ledger, config, monkeypatch):
    readings = _readings(go=0.9, codex=0.4)
    monkeypatch.setattr(experiments, "live_for", lambda ledger, config, now: {})
    monkeypatch.setattr(experiments, "usage", lambda ledger, config, now, live: readings)
    assert experiments.codex_weekly_fraction(ledger, config, NOW) == 0.4
    del readings["openai-codex"]
    assert experiments.codex_weekly_fraction(ledger, config, NOW) is None


def test_labels_are_a_random_draw(monkeypatch):
    fork = Fork(7, {"A": RunArm("A"), "B": RunArm("B", skills="none")}, paired=True)
    assert {experiments.draw_labels() for _ in range(64)} == {("A", "B"), ("B", "A")}
    assert fork.runs(labels=("B", "A")) == (fork.arms["B"], RunPair(7, fork.arms["A"]))
    monkeypatch.setattr(experiments.random, "sample", lambda population, k: ["A", "B"])
    assert fork.runs() == (fork.arms["A"], RunPair(7, fork.arms["B"]))


# ── the pair's two runs ───────────────────────────────────────────────


async def test_pair_runs_two_fresh_pipelines_from_one_base(
    make_task, make_run, ledger, config, data_dir, tmp_project, reading
):
    experiment_id = start(ledger)
    task = make_task()
    fork = fork_decision(task, ledger, config, NOW)
    arm, pair = fork.runs(labels=("B", "A"))
    pi_x, pi_y = adapter(), adapter()
    run_x, _ = make_run(task, pi_x, arm=arm, pair=pair)

    outcome_x = await run_x.run()

    assert outcome_x.status == "awaiting_approval", outcome_x.failure_reason
    row = ledger.get_pair(experiment_id, task.task_id)
    assert (row["run_x"], row["arm_x"], row["arm_y"]) == (run_x.run_id, "B", "A")
    run_y_id = row["run_y"]
    waiting = ledger.get_run(run_y_id)
    assert (waiting["status"], waiting["stage"]) == ("resuming", "new")
    # X is parked and Y has not started: the task is still to be worked on.
    assert ledger.get_task(task.task_id)["status"] == "resuming"
    main = commit_on_main(tmp_project)

    run_y = resume(run_y_id, pi_y, config, ledger, data_dir)
    assert run_y.claim_resume()
    outcome_y = await run_y.run()

    assert outcome_y.status == "awaiting_approval", outcome_y.failure_reason
    assert outcome_y.pending_approval == "merge"
    assert ledger.get_task(task.task_id)["status"] == "awaiting_approval"
    # Each run has its own arm: models in the sessions and in the ledger.
    assert pi_x.calls_for("implement")[0].model == ARM_B_MODEL
    assert pi_y.calls_for("implement")[0].model == task.models["implement"]
    assert ledger.get_run(run_x.run_id)["model_implement"] == ARM_B_MODEL.render()
    assert ledger.get_run(run_y_id)["model_implement"] == task.models["implement"].render()
    assert run_y.arm == RunArm("A")
    # Y ran the fresh pipeline, not a resume.
    types = [e["type"] for e in ledger.events(run_y_id)]
    assert types[:2] == ["experiment_arm", "run_start"]
    assert "run_resume" not in types and "worktree_created" in types and "preflight" in types
    assert types.count("experiment_arm") == 1
    assert ledger.events(run_y_id, types=("experiment_arm",))[0]["payload"] == {
        "name": "A",
        "overrides": {},
        "replaced": {},
    }
    assert ledger.events(run_x.run_id, types=("experiment_arm",))[0]["payload"]["name"] == "B"
    assert ledger.get_run(run_y_id)["started_at"] >= ledger.get_run(run_x.run_id)["started_at"]
    assert run_y.rundir.task_md.read_text() == task.source_path.read_text()
    # Both arms start from identical code, although main has moved.
    assert outcome_y.base_commit == outcome_x.base_commit != main
    assert not (Path(outcome_y.worktree_path) / "later.txt").exists()
    assert outcome_y.worktree_path != outcome_x.worktree_path
    assert outcome_y.branch_name == f"feature/{task.id}/{run_y_id}"
    # Each run locked a topic of its own, and released it.
    assert run_x.lock_topic == pair_lock_topic(task.id, run_x.run_id)
    assert run_y.lock_topic == pair_lock_topic(task.id, run_y_id) != run_x.lock_topic
    state = StateManager(tmp_project / ".helmraiser" / "state.db")
    assert state.get_run(run_x.run_id)["topic_id"] == run_x.lock_topic
    assert state.get_run(run_y_id)["topic_id"] == run_y.lock_topic
    assert state.get_run_lock(run_x.run_id) is None and state.get_run_lock(run_y_id) is None


async def test_both_runs_hold_their_locks_at_once(
    make_task, make_run, ledger, config, data_dir, tmp_project, reading
):
    start(ledger)
    task = make_task()
    arm, pair = fork_decision(task, ledger, config, NOW).runs(labels=("A", "B"))
    state = StateManager(tmp_project / ".helmraiser" / "state.db")
    seen: dict[str, Any] = {}

    def start_y(_worktree: Path) -> None:
        # While X implements, another worker claims Y: the task id is not their lock.
        run_y_id = ledger.pair_for_run(run_x.run_id)["run_y"]
        run_y = resume(run_y_id, adapter(), config, ledger, data_dir)
        seen["claimed"] = run_y.claim_resume()
        seen["locks"] = (state.get_run_lock(run_x.run_id), state.get_run_lock(run_y_id))
        seen["task"] = ledger.get_task(task.task_id)["status"]
        seen["run_y"] = run_y

    pi_x = adapter()
    pi_x.scripts["implement"] = PiScript(files=[("done.txt", "ok\n")], action=start_y)
    run_x, _ = make_run(task, pi_x, arm=arm, pair=pair)

    outcome_x = await run_x.run()

    assert seen["claimed"]
    lock_x, lock_y = seen["locks"]
    assert lock_x["topic_id"] != lock_y["topic_id"] and state.get_lock(task.id) is None
    assert lock_x["pid"] == lock_y["pid"] == os.getpid()
    assert seen["task"] == "running"
    # X ends while Y is running: the task stays running until Y ends too.
    assert outcome_x.status == "awaiting_approval"
    assert ledger.get_task(task.task_id)["status"] == "running"
    outcome_y = await seen["run_y"].run()
    assert outcome_y.base_commit == outcome_x.base_commit
    assert ledger.get_task(task.task_id)["status"] == "awaiting_approval"


async def test_second_run_copies_the_routing_of_the_first(
    make_task, make_run, ledger, config, data_dir, reading
):
    config["routing"]["plan"]["enabled"] = True
    start(ledger)
    original = make_task()
    task, decisions = route_dispatch(original, config, _readings(), NOW)
    assert task.plan and not original.plan
    arm, pair = fork_decision(task, ledger, config, NOW).runs(labels=("B", "A"))
    run_x, _ = make_run(task, adapter(), arm=arm, pair=pair, routing_decisions=decisions)
    await run_x.run()
    run_y_id = ledger.pair_for_run(run_x.run_id)["run_y"]
    assert [e["payload"] for e in ledger.events(run_y_id, types=("routing",))] == decisions

    pi_y = adapter()
    outcome = await resume(run_y_id, pi_y, config, ledger, data_dir).run()

    assert outcome.status == "awaiting_approval", outcome.failure_reason
    assert [call.stage for call in pi_y.calls] == ["plan", "implement", "review"]
    assert len(ledger.events(run_y_id, types=("routing",))) == len(decisions)


async def test_second_run_takes_its_own_base_when_the_first_left_none(
    make_task, make_run, ledger, config, data_dir, tmp_project, reading
):
    start(ledger)
    task = make_task()
    arm, pair = fork_decision(task, ledger, config, NOW).runs(labels=("A", "B"))
    run_x, _ = make_run(task, adapter(), arm=arm, pair=pair, auth_check=lambda model: "no login")
    outcome_x = await run_x.run()
    assert outcome_x.status == "failed" and outcome_x.base_commit is None
    assert ledger.get_task(task.task_id)["status"] == "resuming"
    main = commit_on_main(tmp_project)

    run_y = resume(ledger.pair_for_run(run_x.run_id)["run_y"], adapter(), config, ledger, data_dir)
    outcome_y = await run_y.run()

    assert outcome_y.status == "awaiting_approval", outcome_y.failure_reason
    assert outcome_y.base_commit == main
    assert run_y.sleep.calls == []
    assert ledger.get_task(task.task_id)["status"] == "awaiting_approval"


async def test_second_run_waits_for_the_base_of_a_first_run_that_is_starting(
    make_task, make_run, ledger, config, data_dir, tmp_project, reading
):
    start(ledger)
    task = make_task()
    arm, pair = fork_decision(task, ledger, config, NOW).runs(labels=("A", "B"))
    run_x, _ = make_run(task, adapter(), arm=arm, pair=pair, auth_check=lambda model: "no login")
    await run_x.run()
    # As if X were still between its claim and its worktree.
    ledger.update_run(run_x.run_id, status="starting", ended_at=None, failure_reason=None)
    first = git(tmp_project, "rev-parse", "HEAD")
    commit_on_main(tmp_project)
    run_y_id = ledger.pair_for_run(run_x.run_id)["run_y"]
    calls: list[float] = []

    async def sleep(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) == 2:
            ledger.update_run(run_x.run_id, base_branch="main", base_commit=first)

    outcome_y = await resume(run_y_id, adapter(), config, ledger, data_dir, sleep=sleep).run()

    assert outcome_y.status == "awaiting_approval", outcome_y.failure_reason
    assert calls == [runner.PIN_POLL_SECONDS] * 2
    assert outcome_y.base_commit == first


async def test_second_run_fails_when_a_live_first_run_never_records_a_base(
    make_task, make_run, ledger, config, data_dir, reading, monkeypatch
):
    monkeypatch.setattr(runner, "PIN_WAIT_POLLS", 3)
    start(ledger)
    task = make_task()
    arm, pair = fork_decision(task, ledger, config, NOW).runs(labels=("A", "B"))
    run_x, _ = make_run(task, adapter(), arm=arm, pair=pair, auth_check=lambda model: "no login")
    await run_x.run()
    ledger.update_run(run_x.run_id, status="starting", ended_at=None, failure_reason=None)
    pi_y = adapter()
    run_y = resume(ledger.pair_for_run(run_x.run_id)["run_y"], pi_y, config, ledger, data_dir)

    outcome = await run_y.run()

    assert outcome.status == "failed" and outcome.failure_reason.startswith("pair: ")
    assert len(run_y.sleep.calls) == 3 and not pi_y.calls
    assert outcome.worktree_path is None


async def test_unpaired_run_on_arm_a_creates_no_pair(make_task, make_run, ledger, config, reading):
    experiment_id = start(ledger)
    reading["fraction"] = None
    task = make_task()
    arm, pair = fork_decision(task, ledger, config, NOW).runs()
    run, _ = make_run(task, adapter(), arm=arm, pair=pair)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval", outcome.failure_reason
    assert ledger.get_pair(experiment_id, task.task_id) is None
    assert ledger.pair_for_run(run.run_id) is None
    assert [r["run_id"] for r in ledger.list_runs()] == [run.run_id]
    assert ledger.events(run.run_id, types=("experiment_arm",))[0]["payload"]["name"] == "A"
    assert run.lock_topic == task.id
    assert ledger.get_task(task.task_id)["status"] == "awaiting_approval"


async def test_claim_refused_when_the_experiment_ended_after_the_decision(
    make_task, make_run, ledger, config, reading
):
    experiment_id = start(ledger)
    task = make_task()
    arm, pair = fork_decision(task, ledger, config, NOW).runs()
    pi = adapter()
    run, _ = make_run(task, pi, arm=arm, pair=pair)
    ledger.end_experiment(experiment_id, "stopped")

    with pytest.raises(RunUnavailable) as refused:
        await run.run()

    assert refused.value.hold == {"reason": "experiment_changed"}
    assert not ledger.list_runs() and not pi.calls
    assert ledger.get_task(task.task_id)["status"] == "queued"
    assert run.state.get_run_lock(run.run_id) is None
    # The next pass decides again: the task runs as without experiments.
    assert fork_decision(task, ledger, config, NOW) is None


def test_a_pair_needs_the_arm_of_its_claiming_run(make_task, make_run):
    with pytest.raises(ValueError, match="pair needs the arm"):
        make_run(make_task(), adapter(), pair=RunPair(1, RunArm("B")))


# ── reconcile ─────────────────────────────────────────────────────────


async def test_reconcile_finds_and_releases_a_paired_runs_lock(
    make_task, make_run, ledger, config, data_dir, tmp_project, reading
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

    # Y's worker is this process, alive: not a phantom, found by the run's own lock.
    assert find_phantom_runs(ledger) == []

    # Its worker dies between the claim and the end of the run.
    state.release_lock(run_y_id, run_y.lock_topic)
    assert state.acquire_lock(run_y_id, run_y.lock_topic, _dead_pid())
    (phantom,) = find_phantom_runs(ledger)
    assert (phantom.run_id, phantom.topic_id) == (run_y_id, task.id)
    assert phantom.worker_pid is not None and phantom.lock_topic == run_y.lock_topic

    (item,) = reconcile(ledger, [phantom], FakeController())

    assert item["run_id"] == run_y_id and item["status"] == "failed"
    assert state.get_run_lock(run_y_id) is None
    assert ledger.get_run(run_y_id)["status"] == "failed"
    # X still awaits approval: the task shows it.
    assert ledger.get_task(task.task_id)["status"] == "awaiting_approval"
