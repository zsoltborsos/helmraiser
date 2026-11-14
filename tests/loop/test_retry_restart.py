"""A commitless retry restarts work, preserving the run and its history."""

from dataclasses import replace
from pathlib import Path
from shlex import quote

import pytest

from helmraiser.loop import prompts
from helmraiser.loop.runner import GovernedRun
from helmraiser.pi.protocol import Usage
from helmraiser.task.loader import render_task
from tests.loop.conftest import FakeClock
from tests.loop.test_evidence_review_merge import verdict_text
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter


def resume_restart(run_id, adapter, config, ledger, data_dir, **overrides):
    return GovernedRun.resume(
        run_id,
        config=config,
        ledger=ledger,
        data_dir=data_dir,
        adapter_factory=lambda state, isolation: adapter,
        pi_version="fake",
        auth_check=lambda model: None,
        **overrides,
    )


def amend_task(ledger, task, **changes):
    task = replace(task, **changes)
    snapshot = render_task(task)
    task.source_path.write_text(snapshot)
    ledger.set_task_snapshot(task.task_id, snapshot)
    return task


def passing_adapter():
    return ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "done")]),
            "review": PiScript(final_text=verdict_text("pass")),
        }
    )


@pytest.mark.parametrize("approved", [True, False])
def test_restart_prompt_prepends_the_initial_prompt(make_task, approved):
    task = make_task()
    paragraph = (
        "The owner handed this run back to you after it ended failed (blocked: wrong task). "
        "Nothing is committed on the branch. The task file may have changed since: read it "
        "again. Start the task from the beginning."
    )
    assert prompts.retry_restart_prompt(task, "blocked: wrong task", "Saved plan", approved) == (
        paragraph + "\n\n" + prompts.initial_prompt(task, "Saved plan", approved)
    )


@pytest.mark.parametrize("fallback_cost", [0.1, 0.6])
async def test_restart_runs_a_first_iteration_with_the_retry_allowances(
    make_task, make_run, ledger, config, data_dir, fallback_cost
):
    task = make_task(
        max_iterations=2,
        max_cost_usd=1,
        max_minutes=3,
        evidence=[
            {
                "name": "proof",
                "run": 'test -f done.txt && echo verified > "$HELMRAISER_EVIDENCE_DIR/proof.txt"',
                "artifacts": ["proof.txt"],
                "criteria": "proof.txt says verified",
            }
        ],
    )
    clock = FakeClock()
    first_adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(action=lambda cwd: clock.advance(40)),
                PiScript(action=lambda cwd: clock.advance(40)),
                PiScript(
                    final_text="BLOCKED: wrong task",
                    usage=Usage(1, 1, 0.4),
                    action=lambda cwd: clock.advance(40),
                ),
            ]
        }
    )
    first, _ = make_run(task, first_adapter, clock=clock)
    original = await first.run()
    assert original.failure_reason == "blocked: wrong task"
    assert original.escalated and original.iterations == 3 and original.minutes == 2
    worktree = Path(original.worktree_path)
    assert first._git_state() == (original.base_commit, "")
    previous_events = ledger.events(first.run_id)
    previous_iterations = ledger.iterations(first.run_id)
    clock.advance(100000)
    assert ledger.hand_back(first.run_id, "start")
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(usage=Usage(1, 1, 0.1), action=lambda cwd: clock.advance(30)),
                PiScript(usage=Usage(1, 1, 0.1), action=lambda cwd: clock.advance(30)),
                PiScript(
                    files=[("done.txt", "done")],
                    usage=Usage(1, 1, fallback_cost),
                    action=lambda cwd: clock.advance(30),
                ),
            ],
            "review": PiScript(
                final_text=verdict_text(
                    "pass", evidence_checks=[{"name": "proof", "meets_criteria": True}]
                )
            ),
        }
    )
    run = resume_restart(first.run_id, adapter, config, ledger, data_dir, clock=clock)
    outcome = await run.run()
    assert outcome.status == ("awaiting_approval" if fallback_cost == 0.1 else "failed")
    assert outcome.failure_reason == (None if fallback_cost == 0.1 else "budget:cost")
    assert outcome.cost_usd == pytest.approx(0.6 + fallback_cost)
    assert outcome.minutes == 1.5
    assert outcome.iterations == 6
    calls = adapter.calls_for("implement")
    assert [c.model for c in calls] == [
        task.models["implement"],
        task.models["implement"],
        task.models["fallback"],
    ]
    assert calls[0].session_id == calls[1].session_id == f"{first.run_id}-implement"
    assert calls[0].prompt == prompts.retry_restart_prompt(
        task, "blocked: wrong task", approved=False
    )
    assert task.body.strip() in calls[0].prompt and "Verify failed" not in calls[0].prompt
    assert len(adapter.calls_for("review")) == (1 if fallback_cost == 0.1 else 0)
    if fallback_cost == 0.1:
        assert outcome.pending_approval == "merge" and outcome.verify_results[0]["passed"]
        assert outcome.verdict["verdict"] == "pass"
        assert [(r["name"], r["ok"]) for r in outcome.evidence_results] == [("proof", True)]
        assert (run.rundir.evidence / "proof.txt").read_text() == "verified\n"
        assert "proof.txt" in adapter.calls_for("review")[0].prompt
    else:
        assert outcome.evidence_results == []
        assert not (run.rundir.evidence / "proof.txt").exists()
    assert (outcome.run_id, outcome.worktree_path, outcome.branch_name, outcome.base_commit) == (
        original.run_id,
        str(worktree),
        original.branch_name,
        original.base_commit,
    )
    assert [i["n"] for i in ledger.iterations(first.run_id)] == [1, 2, 3, 4, 5, 6]
    assert ledger.iterations(first.run_id)[:3] == previous_iterations
    events = ledger.events(first.run_id)
    assert events[: len(previous_events)] == previous_events
    resume = next(e for e in events if e["type"] == "run_resume")
    assert resume["payload"] == {
        "task_id": task.task_id,
        "from": "retry",
        "stage": "start",
        "restarted_without_commits": True,
    }
    assert len([e for e in events if e["type"] == "escalated"]) == 2
    resumed_events = events[len(previous_events) :]
    assert not any(e["type"] in ("worktree_created", "base_sync") for e in resumed_events)
    if fallback_cost == 0.1:
        types = [e["type"] for e in resumed_events]
        assert (
            types.index("verify_result")
            < types.index("evidence_result")
            < types.index("review_verdict")
        )
    else:
        assert not any(e["type"] == "evidence_result" for e in resumed_events)


@pytest.mark.parametrize("history", ["missing", "failed", "timed_out", "fails_again"])
async def test_restart_reruns_setup_that_never_passed(
    make_task, make_run, ledger, config, data_dir, history
):
    command = "if test -f setup-marker; then true; else touch setup-marker; exit 1; fi"
    task = make_task(setup=command if history in ("failed", "fails_again") else None)
    first_adapter = ScriptedPiAdapter({"implement": PiScript(final_text="BLOCKED: wrong task")})
    first, _ = make_run(task, first_adapter)
    original = await first.run()
    assert original.status == "failed"
    if history == "missing":
        amend_task(ledger, task, setup="touch setup-marker")
    elif history == "timed_out":
        amend_task(ledger, task, setup="touch setup-marker")
        ledger.append_event(
            first.run_id, "helmraiser", "setup_start", {"run": "touch setup-marker"}
        )
        ledger.append_event(
            first.run_id, "helmraiser", "setup_result", {"ok": False, "exit_code": 124}
        )
    elif history == "fails_again":
        amend_task(ledger, task, setup="exit 7")
    else:
        assert original.failure_reason.startswith("setup:")
        assert first_adapter.calls == []
    assert ledger.hand_back(first.run_id, "start")
    adapter = passing_adapter()
    run = resume_restart(first.run_id, adapter, config, ledger, data_dir)
    outcome = await run.run()
    events = ledger.events(first.run_id)
    assert not [e for e in events if e["type"] == "setup_skipped"]
    results = [e for e in events if e["type"] == "setup_result"]
    if history == "fails_again":
        assert outcome.status == "failed" and outcome.failure_reason.startswith("setup:")
        assert adapter.calls == [] and not results[-1]["payload"]["ok"]
    else:
        assert outcome.status == "awaiting_approval", outcome.failure_reason
        assert results[-1]["payload"]["ok"] is True
        implement = [e for e in events if e["type"] == "iteration_start"][-1]
        assert results[-1]["id"] < implement["id"]
    assert run.state.get_locked_run_id(task.id) is None
    assert len([e for e in events if e["type"] == "worktree_created"]) == 1
    assert not [e for e in events if e["type"] == "base_sync" and e["id"] > results[-1]["id"]]


@pytest.mark.parametrize("later_attempt", [None, "failed", "unfinished"])
async def test_restart_skips_setup_that_already_passed(
    make_task, make_run, ledger, config, data_dir, tmp_path, later_attempt
):
    marker = tmp_path / "setup-count"
    command = f"echo ran >> {quote(str(marker))}"
    task = make_task(setup=command)
    first, _ = make_run(
        task, ScriptedPiAdapter({"implement": PiScript(final_text="BLOCKED: wrong task")})
    )
    await first.run()
    if later_attempt:
        ledger.append_event(first.run_id, "helmraiser", "setup_start", {"run": command})
        if later_attempt == "failed":
            ledger.append_event(first.run_id, "helmraiser", "setup_result", {"ok": False})
    before = ledger.events(first.run_id, types=("setup_start", "setup_result"))
    assert ledger.hand_back(first.run_id, "start")
    outcome = await resume_restart(first.run_id, passing_adapter(), config, ledger, data_dir).run()
    assert outcome.status == "awaiting_approval", outcome.failure_reason
    skipped = ledger.events(first.run_id, types=("setup_skipped",))
    after = ledger.events(first.run_id, types=("setup_start", "setup_result"))
    if later_attempt is None:
        assert after == before
        assert [e["payload"] for e in skipped] == [{"reason": "passed earlier"}]
        assert marker.read_text() == "ran\n"
    else:
        assert len(after) == len(before) + 2 and skipped == []
        assert marker.read_text() == "ran\nran\n"


async def test_restart_reruns_setup_when_the_task_changed_it(
    make_task, make_run, ledger, config, data_dir, tmp_path
):
    marker = tmp_path / "setup-count"
    task = make_task(setup=f"echo old >> {quote(str(marker))}")
    first, _ = make_run(
        task, ScriptedPiAdapter({"implement": PiScript(final_text="BLOCKED: wrong task")})
    )
    await first.run()
    command = f"echo new >> {quote(str(marker))}"
    amend_task(ledger, task, setup=command)
    assert ledger.hand_back(first.run_id, "start")
    outcome = await resume_restart(first.run_id, passing_adapter(), config, ledger, data_dir).run()
    assert outcome.status == "awaiting_approval", outcome.failure_reason
    assert marker.read_text() == "old\nnew\n"
    assert ledger.events(first.run_id, types=("setup_start",))[-1]["payload"]["run"] == command
    assert ledger.events(first.run_id, types=("setup_skipped",)) == []


@pytest.mark.parametrize("policy", ["review", "approve-plan-first", "optional-failure"])
async def test_restart_plans_when_the_task_needs_a_plan_and_has_none(
    make_task, make_run, ledger, config, data_dir, policy
):
    task = make_task()
    first, _ = make_run(
        task, ScriptedPiAdapter({"implement": PiScript(final_text="BLOCKED: wrong task")})
    )
    original = await first.run()
    amend_task(ledger, task, plan=True, policy="review" if policy == "optional-failure" else policy)
    assert ledger.hand_back(first.run_id, "start")
    adapter = passing_adapter()
    adapter.scripts["plan"] = (
        PiScript(final_text="")
        if policy == "optional-failure"
        else PiScript(final_text="# New plan\nFollow this design")
    )
    run = resume_restart(first.run_id, adapter, config, ledger, data_dir)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval", outcome.failure_reason
    assert outcome.run_id == original.run_id and outcome.worktree_path == original.worktree_path
    if policy == "approve-plan-first":
        assert [c.stage for c in adapter.calls] == ["plan"]
        assert outcome.pending_approval == "plan" and outcome.iterations == 1
        pending = ledger.pending_approval(first.run_id)
        assert pending["kind"] == "plan"
        ledger.decide_approval(pending["id"], "approved", "Follow it")
        assert ledger.hand_back(first.run_id, "implement")
        adapter = passing_adapter()
        outcome = await resume_restart(first.run_id, adapter, config, ledger, data_dir).run()
        assert outcome.status == "awaiting_approval" and outcome.pending_approval == "merge"
        assert outcome.iterations == 2
        assert "## Approved plan" in adapter.calls_for("implement")[0].prompt
        assert "The owner handed this run back" not in adapter.calls_for("implement")[0].prompt
    else:
        assert [c.stage for c in adapter.calls] == ["plan", "implement", "review"]
        assert outcome.pending_approval == "merge" and outcome.verify_results[0]["passed"]
        prompt = adapter.calls_for("implement")[0].prompt
        if policy == "optional-failure":
            assert ledger.events(first.run_id, types=("plan_failed",))
            assert "## Plan" not in prompt
        else:
            assert "## Plan" in prompt and "## Approved plan" not in prompt
            assert "Follow this design" in prompt
    assert len(ledger.events(first.run_id, types=("worktree_created",))) == 1


@pytest.mark.parametrize("approved", [True, False])
async def test_restart_reuses_a_saved_plan(make_task, make_run, ledger, config, data_dir, approved):
    task = make_task(plan=True, policy="approve-plan-first" if approved else "review")
    adapter = ScriptedPiAdapter(
        {
            "plan": PiScript(final_text="# Saved plan\nKeep this design"),
            "implement": PiScript(final_text="BLOCKED: wrong task"),
        }
    )
    first, _ = make_run(task, adapter)
    await first.run()
    if approved:
        ledger.decide_approval(ledger.pending_approval(first.run_id)["id"], "approved", None)
        assert ledger.hand_back(first.run_id, "implement")
        await resume_restart(first.run_id, adapter, config, ledger, data_dir).run()
    assert ledger.get_run(first.run_id)["status"] == "failed"
    assert ledger.hand_back(first.run_id, "start")
    adapter = passing_adapter()
    outcome = await resume_restart(first.run_id, adapter, config, ledger, data_dir).run()
    assert outcome.status == "awaiting_approval", outcome.failure_reason
    assert not adapter.calls_for("plan")
    for call in adapter.calls:
        assert "Keep this design" in call.prompt
        assert ("## Approved plan" in call.prompt) == approved
