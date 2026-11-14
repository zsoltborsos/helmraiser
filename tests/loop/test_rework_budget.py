"""Rework allowances and cumulative active-time accounting."""

import pytest

from helmraiser.loop.budget import BudgetGuard, completed_active_minutes
from helmraiser.pi.protocol import Usage
from helmraiser.task.loader import render_task
from helmraiser.task.model import Budget, ModelSpec
from tests.loop.conftest import FakeClock
from tests.loop.test_evidence_review_merge import verdict_text
from tests.loop.test_rework import hand_back_rework, resume_rework
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter


@pytest.mark.parametrize("limit,allowance", [(1, 1), (3, 2), (4, 2)])
async def test_rework_shared_iteration_exhaustion(
    make_task,
    make_run,
    ledger,
    data_dir,
    config,
    limit,
    allowance,
):
    task = make_task(max_iterations=limit)
    first, _ = make_run(
        task,
        ScriptedPiAdapter(
            {
                "implement": PiScript(files=[("done.txt", "first")]),
                "review": PiScript(final_text=verdict_text("pass")),
            }
        ),
    )
    await first.run()
    ledger.update_run(first.run_id, escalated=1)
    hand_back_rework(ledger, first.run_id, task)
    adapter = ScriptedPiAdapter({"implement": PiScript()})
    resumed = resume_rework(first.run_id, adapter, config, ledger, data_dir)
    outcome = await resumed.run()
    assert (outcome.status, outcome.failure_reason) == ("failed", "budget:iterations")
    assert outcome.iterations == 1 + allowance
    assert len(adapter.calls) == allowance
    assert all(c.model == task.models["implement"] for c in adapter.calls)
    assert [i["n"] for i in ledger.iterations(first.run_id)] == list(range(1, allowance + 2))
    assert not ledger.get_run(first.run_id)["escalated"]


@pytest.mark.parametrize("limit", [1, 4])
async def test_rework_severe_repairs_share_allowance(
    make_task,
    make_run,
    ledger,
    data_dir,
    config,
    limit,
):
    task = make_task(max_iterations=limit)
    first, _ = make_run(
        task,
        ScriptedPiAdapter(
            {
                "implement": PiScript(files=[("done.txt", "first")]),
                "review": PiScript(final_text=verdict_text("pass")),
            }
        ),
    )
    await first.run()
    hand_back_rework(ledger, first.run_id, task)
    severe = [{"severity": "severe", "file": "done.txt", "line": 1, "note": "race"}]
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(files=[("done.txt", "rework")]),
                PiScript(files=[("done.txt", "fix")]),
            ],
            "review": [
                PiScript(final_text=verdict_text("concerns", severe)),
                PiScript(final_text=verdict_text("pass")),
            ],
        }
    )
    outcome = await resume_rework(first.run_id, adapter, config, ledger, data_dir).run()
    assert outcome.status == "awaiting_approval"
    assert len(adapter.calls_for("implement")) == min(2, limit)
    assert len(adapter.calls_for("review")) == min(2, limit)
    assert outcome.verdict["verdict"] == ("concerns" if limit == 1 else "pass")


@pytest.mark.parametrize("cost,expected", [(0.4, "awaiting_approval"), (0.8, "failed")])
async def test_rework_cumulative_cost_cap(
    make_task,
    make_run,
    ledger,
    data_dir,
    config,
    cost,
    expected,
):
    task = make_task(max_cost_usd=1)
    first, _ = make_run(
        task,
        ScriptedPiAdapter(
            {
                "implement": PiScript(files=[("done.txt", "first")], usage=Usage(1, 1, 0.8)),
                "review": PiScript(final_text=verdict_text("pass")),
            }
        ),
    )
    await first.run()
    hand_back_rework(ledger, first.run_id, task)
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "rework")], usage=Usage(1, 1, cost)),
            "review": PiScript(final_text=verdict_text("pass")),
        }
    )
    run = resume_rework(first.run_id, adapter, config, ledger, data_dir)
    outcome = await run.run()
    assert outcome.status == expected
    assert outcome.cost_usd == pytest.approx(0.8 + cost)
    assert run.guard.max_cost_usd == 1.5
    if expected == "failed":
        assert outcome.failure_reason == "budget:cost"


async def test_rework_active_time_and_fractional_extensions(
    make_task,
    make_run,
    ledger,
    data_dir,
    config,
):
    task = make_task(max_minutes=3, max_iterations=3, fallback=False)
    clock = FakeClock()

    class TimedAdapter(ScriptedPiAdapter):
        async def run(self, req, on_event, run_id):
            if req.stage == "implement":
                clock.advance(120)
            return await super().run(req, on_event, run_id)

    first, _ = make_run(
        task,
        TimedAdapter(
            {
                "implement": PiScript(files=[("done.txt", "first")]),
                "review": PiScript(final_text=verdict_text("pass")),
            }
        ),
        clock=clock,
    )
    await first.run()
    clock.advance(100000)  # approval waiting is not active
    for round_n in (1, 2):
        hand_back_rework(ledger, first.run_id, task)
        adapter = TimedAdapter(
            {
                "implement": PiScript(files=[("done.txt", str(round_n))]),
                "review": PiScript(final_text=verdict_text("pass")),
            }
        )
        run = resume_rework(first.run_id, adapter, config, ledger, data_dir, clock=clock)
        outcome = await run.run()
        assert outcome.status == "awaiting_approval"
        assert outcome.minutes == (round_n + 1) * 2
        assert run.guard.max_minutes == 3 + round_n * 2
        assert run.guard.max_cost_usd == pytest.approx(5 * (1 + round_n * 2 / 3))
        clock.advance(100000)
    hand_back_rework(ledger, first.run_id, task)
    clock.advance(100000)

    class OverTime(TimedAdapter):
        async def run(self, req, on_event, run_id):
            clock.advance(240)
            return await super().run(req, on_event, run_id)

    run = resume_rework(
        first.run_id, OverTime({"implement": PiScript()}), config, ledger, data_dir, clock=clock
    )
    outcome = await run.run()
    assert outcome.failure_reason == "budget:time"


@pytest.mark.parametrize("error_kind", ["timeout", "quota"])
async def test_rework_early_escalation_uses_only_remaining_iteration(
    make_task,
    make_run,
    ledger,
    data_dir,
    config,
    error_kind,
):
    task = make_task(max_minutes=10)
    task.models["fallback"] = ModelSpec.parse("opencode-go/fallback")
    snapshot = render_task(task)
    task.source_path.write_text(snapshot)
    ledger.upsert_task(task, snapshot)
    clock = FakeClock()
    first, _ = make_run(
        task,
        ScriptedPiAdapter(
            {
                "implement": PiScript(files=[("done.txt", "first")]),
                "review": PiScript(final_text=verdict_text("pass")),
            }
        ),
        clock=clock,
    )
    await first.run()
    note = "Approver café\nFix the confusing widget message."
    findings = [{"severity": "minor", "file": "done.txt", "line": 1, "note": "clarify widget"}]
    hand_back_rework(ledger, first.run_id, task, note, findings)

    class EarlyEscalationAdapter(ScriptedPiAdapter):
        async def run(self, req, on_event, run_id):
            if len(self.calls) == 0:
                clock.advance(60)
            return await super().run(req, on_event, run_id)

    adapter = EarlyEscalationAdapter(
        {
            "implement": [PiScript(error_kind=error_kind), PiScript(files=[("done.txt", "fixed")])],
            "review": PiScript(final_text=verdict_text("pass")),
        }
    )
    run = resume_rework(first.run_id, adapter, config, ledger, data_dir, clock=clock)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval"
    calls = adapter.calls_for("implement")
    assert [c.model for c in calls] == [task.models["implement"], task.models["fallback"]]
    assert calls[1].session_id != calls[0].session_id
    for call in calls:
        assert (
            call.prompt.index(note)
            < call.prompt.index("clarify widget")
            < call.prompt.index("## Acceptance")
        )
        assert "optional" not in call.prompt
    assert "## What was tried so far" in calls[1].prompt
    assert outcome.iterations == 3
    assert (
        "Budget: 1" in run.rundir.directives_md.read_text()
        or "Iteration 1/1" in run.rundir.directives_md.read_text()
    )


def test_rework_guard_defaults_and_prior_minutes():
    clock = FakeClock(100)
    default = BudgetGuard(Budget(3, 10, 3), 100, clock)
    assert default.remaining_seconds() == 600
    guard = BudgetGuard(
        Budget(3, 10, 3),
        100,
        clock,
        prior_minutes=9,
        extra_cost_usd=2,
        extra_minutes=10 * 2 / 3,
    )
    assert guard.max_cost_usd == 5
    assert guard.max_minutes == pytest.approx(50 / 3)
    assert guard.remaining_seconds() == 460
    clock.advance(60)
    assert guard.minutes == 10
    assert default.minutes == 1
    guard.settle(5.1)
    assert guard.exceeded() == "cost"


@pytest.mark.parametrize(
    "events,ended,expected",
    [
        ([], 220, 2),
        ([], None, 0),
        ([], 90, 0),
        (
            [
                {"type": "run_start", "ts": 100, "payload": {}},
                {"type": "run_end", "ts": 220, "payload": {}},
                {"type": "run_resume", "ts": 10000, "payload": {}},
                {"type": "run_end", "ts": 10180, "payload": {}},
            ],
            10180,
            5,
        ),
        (
            [
                {"type": "run_start", "ts": 100, "payload": {}},
                {"type": "run_end", "ts": 220, "payload": {"active_minutes": 1.25}},
                {"type": "run_resume", "ts": 10000, "payload": {}},
                {"type": "run_end", "ts": 10180, "payload": {"active_minutes": 0.5}},
            ],
            10180,
            1.75,
        ),
    ],
)
def test_rework_completed_segments(events, ended, expected):
    assert completed_active_minutes(events, started_at=100, ended_at=ended) == expected
