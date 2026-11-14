"""Opt-in plan stage: plan without parking, then implement and review."""

from __future__ import annotations

import pytest

from helmraiser.loop.spend import cost_entries
from helmraiser.pi.protocol import Usage
from tests.loop.conftest import FakeClock
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

PLAN = "1. Change `done.txt` at line 1.\n2. Run `tests/test_marker.py` and commit."
REVIEW = '```json\n{"verdict": "pass", "findings": [], "evidence_checks": []}\n```'


async def test_planned_run_uses_plan_model_and_carries_plan_through_review(
    make_task, make_run, ledger
):
    task = make_task(plan=True, policy="review")
    adapter = ScriptedPiAdapter(
        {
            "plan": PiScript(final_text=PLAN, usage=Usage(10, 5, 0.03)),
            "implement": PiScript(files=[("done.txt", "ok\n")], usage=Usage(10, 5, 0.10)),
            "review": PiScript(final_text=REVIEW, usage=Usage(10, 5, 0.02)),
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval"
    assert outcome.pending_approval == "merge"
    (plan_req,) = adapter.calls_for("plan")
    assert plan_req.model == task.models["plan"]
    assert "starts from the plan straight away" in plan_req.prompt
    (implement_req,) = adapter.calls_for("implement")
    assert "## Plan" in implement_req.prompt and PLAN in implement_req.prompt
    assert "## Approved plan" not in implement_req.prompt
    (review_req,) = adapter.calls_for("review")
    assert "## Plan" in review_req.prompt and PLAN in review_req.prompt
    assert "## Approved plan" not in review_req.prompt
    assert ledger.pending_approval(run.run_id)["kind"] == "merge"

    row = ledger.get_run(run.run_id)
    assert row is not None
    assert row["model_plan"] == task.models["plan"].render()
    assert row["plan_cost_usd"] == pytest.approx(0.03)
    entries = cost_entries(ledger, 0)
    assert any(model == task.models["plan"].render() and cost == 0.03 for _, model, cost in entries)
    assert any(
        model == task.models["review"].render() and cost == pytest.approx(0.02)
        for _, model, cost in entries
    )


async def test_task_without_plan_skips_plan_session(make_task, make_run):
    task = make_task()
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter)

    await run.run()

    assert adapter.calls_for("plan") == []


@pytest.mark.parametrize(
    "script, reason",
    [
        (PiScript(error_kind="quota", error_message="limit"), "quota: limit"),
        (PiScript(final_text=""), "empty plan"),
    ],
)
async def test_failed_plan_continues_without_plan(
    make_task, make_run, ledger, script: PiScript, reason: str
):
    task = make_task(plan=True)
    adapter = ScriptedPiAdapter(
        {
            "plan": script,
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": PiScript(final_text=REVIEW),
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval"
    (implement_req,) = adapter.calls_for("implement")
    assert "## Plan" not in implement_req.prompt
    failed = [event for event in ledger.events(run.run_id) if event["type"] == "plan_failed"]
    assert len(failed) == 1 and failed[0]["payload"]["reason"] == reason


async def test_blocked_plan_fails_run(make_task, make_run):
    task = make_task(plan=True)
    adapter = ScriptedPiAdapter({"plan": PiScript(final_text="BLOCKED: requirements conflict")})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert (outcome.status, outcome.failure_reason) == ("failed", "blocked: requirements conflict")
    assert adapter.calls_for("implement") == []


async def test_plan_timeout_and_implement_slice_use_their_own_starts(make_task, make_run):
    task = make_task(plan=True, max_minutes=100)
    clock = FakeClock()

    class SlowPlanAdapter(ScriptedPiAdapter):
        async def run(self, req, on_event, run_id):
            if req.stage == "plan":
                clock.advance(20 * 60)
            return await super().run(req, on_event, run_id)

    adapter = SlowPlanAdapter(
        {
            "plan": PiScript(final_text=PLAN),
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": PiScript(final_text=REVIEW),
        }
    )
    run, _ = make_run(task, adapter, clock=clock)

    await run.run()

    assert adapter.calls_for("plan")[0].timeout_s <= 0.25 * 100 * 60
    # 80 minutes remain when implementation starts; the first model gets 60%.
    assert adapter.calls_for("implement")[0].timeout_s <= 0.6 * 80 * 60
    assert adapter.calls_for("implement")[0].timeout_s > 0.6 * 60 * 60
