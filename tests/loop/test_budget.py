"""Budget guard (spec §4.2): cost / time caps and mid-session termination."""

from __future__ import annotations

import pytest

from helmraiser.loop.budget import BudgetGuard
from helmraiser.pi.protocol import Usage
from helmraiser.runner.process_control import ProcessIdentity
from helmraiser.storage.state import ProcessLease, StateManager
from helmraiser.task.model import Budget
from tests.loop.conftest import FakeClock, assistant_message_end
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter


class TestBudgetGuard:
    def test_cost_cap(self):
        guard = BudgetGuard(Budget(1.0, 60, 4), 0.0, clock=lambda: 0.0)
        assert guard.exceeded() is None
        guard.settle(0.6)
        assert guard.exceeded() is None
        guard.set_streaming(0.5)
        assert guard.cost_usd == 1.1 and guard.exceeded() == "cost"
        guard.settle(0.5)  # streaming folded into settled
        assert guard.cost_usd == 1.1 and guard.streaming_cost == 0.0

    def test_time_cap_and_remaining(self):
        clock = FakeClock(1000.0)
        guard = BudgetGuard(Budget(9.0, 10, 4), 1000.0, clock)
        assert guard.remaining_seconds() == 600
        clock.advance(601)
        assert guard.exceeded() == "time" and guard.remaining_seconds() == 0

    def test_cost_wins_over_time(self):
        clock = FakeClock(0.0)
        guard = BudgetGuard(Budget(1.0, 1, 4), 0.0, clock)
        clock.advance(120)
        guard.settle(2.0)
        assert guard.exceeded() == "cost"


@pytest.mark.parametrize("rpc", [False, True])
async def test_cost_cap_checked_before_each_spawn(make_task, make_run, ledger, config, rpc):
    config["pi_rpc_implement"] = rpc
    task = make_task(max_cost_usd=1.0, max_iterations=4)
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(files=[("x", "1")], usage=Usage(100, 10, 0.8)),
                PiScript(files=[("done.txt", "")], usage=Usage(100, 10, 0.8)),
            ]
        }
    )
    run, _ = make_run(task, adapter)
    outcome = await run.run()
    # Iteration 1 fails verify at $0.80 (under cap); iteration 2 settles at $1.60 → over,
    # even though its verify would have passed.
    assert (outcome.status, outcome.failure_reason) == ("failed", "budget:cost")
    assert len(adapter.calls) == 2 and outcome.cost_usd == 1.6
    assert all(req.stage == "implement" and req.rpc is rpc for req in adapter.calls)
    row = ledger.get_run(run.run_id)
    assert row["cost_usd"] == 1.6 and row["tokens_in"] == 200 and row["tokens_out"] == 20
    assert "budget_exceeded" in [e["type"] for e in ledger.events(run.run_id)]


@pytest.mark.parametrize("rpc", [False, True])
async def test_cost_cap_mid_session_terminates_through_the_lease(
    make_task, make_run, ledger, tmp_project, config, rpc
):
    config["pi_rpc_implement"] = rpc
    task = make_task(max_cost_usd=1.0)
    terminated: list[ProcessIdentity] = []

    class Controller:
        def identify(self, pid):
            raise AssertionError("not used")

        def is_running(self, identity):
            return False

        def terminate(self, identity):
            terminated.append(identity)
            return True

        def kill(self, identity):
            return True

    class LeasingAdapter(ScriptedPiAdapter):
        """Publishes a lease like ExecutionEngine would, so the guard has something to stop."""

        def __init__(self, state: StateManager):
            super().__init__(
                {
                    "implement": PiScript(
                        files=[("done.txt", "")],
                        usage=Usage(0, 0, 2.5),
                        events=[assistant_message_end(2.5)],
                    )
                }
            )
            self.state = state

        async def run(self, req, on_event, run_id):
            assert req.stage == "implement" and req.rpc is rpc
            self.state.register_process(ProcessLease(run_id, "attempt-1", 4242, 4242, "tok"))
            try:
                return await super().run(req, on_event, run_id)
            finally:
                self.state.clear_process(run_id, "attempt-1")

    state = StateManager(tmp_project / ".helmraiser" / "state.db")
    run, _ = make_run(task, LeasingAdapter(state), process_controller=Controller())
    outcome = await run.run()

    assert (outcome.status, outcome.failure_reason) == ("failed", "budget:cost")
    assert terminated == [ProcessIdentity(4242, 4242, "tok")]
    assert state.get_run(run.run_id)["status"] == "failed"
    assert state.get_locked_run_id("fix-widget") is None
    assert state.request_abort(run.run_id).lease is None
    row = ledger.get_run(run.run_id)
    assert row["cost_usd"] == outcome.cost_usd == 2.5
    assert row["tokens_in"] == row["tokens_out"] == 0
    events = ledger.events(run.run_id)
    exceeded = [e for e in events if e["type"] == "budget_exceeded"]
    assert exceeded[0]["payload"] == {"kind": "cost", "during": "session"}


async def test_time_cap_fails_the_run(make_task, make_run):
    task = make_task(max_minutes=10, max_iterations=4)
    clock = FakeClock()

    class SlowAdapter(ScriptedPiAdapter):
        async def run(self, req, on_event, run_id):
            clock.advance(11 * 60)
            return await super().run(req, on_event, run_id)

    adapter = SlowAdapter({"implement": PiScript(files=[("x", "1")])})
    run, _ = make_run(task, adapter, clock=clock)
    outcome = await run.run()
    assert (outcome.status, outcome.failure_reason) == ("failed", "budget:time")
    assert len(adapter.calls) == 1 and outcome.minutes == 11.0


async def test_timeout_after_wall_clock_exhaustion_is_a_budget_failure(make_task, make_run):
    task = make_task(max_minutes=10)
    clock = FakeClock()

    class HangingAdapter(ScriptedPiAdapter):
        async def run(self, req, on_event, run_id):
            clock.advance(11 * 60)
            return await super().run(req, on_event, run_id)

    adapter = HangingAdapter({"implement": PiScript(ok=False, error_kind="timeout")})
    run, _ = make_run(task, adapter, clock=clock)
    outcome = await run.run()
    assert outcome.failure_reason == "budget:time"
