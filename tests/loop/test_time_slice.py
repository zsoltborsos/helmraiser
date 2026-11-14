"""Implement time slicing, timeout preservation, and review time allocation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import helmraiser.loop.runner as runner_module
from tests.loop.conftest import FakeClock
from tests.loop.test_evidence_review_merge import verdict_text
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.mark.parametrize("fallback", [True, False])
async def test_first_model_timeout_is_sliced_only_with_different_fallback(
    make_task, make_run, fallback
):
    clock = FakeClock()
    task = make_task(max_minutes=50, fallback=fallback)
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter, clock=clock)

    await run.run()

    timeout = adapter.calls_for("implement")[0].timeout_s
    if fallback:
        assert timeout <= 30 * 60
    else:
        assert timeout == 50 * 60


async def test_first_model_timeout_commits_partial_work_and_escalates(
    make_task, make_run, ledger, tmp_project
):
    task = make_task(max_minutes=50, policy="auto", delivery="merge")
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(
                    ok=False,
                    error_kind="timeout",
                    error_message="session exceeded 1800s",
                    files=[("partial.txt", "kept\n")],
                ),
                PiScript(files=[("done.txt", "ok\n")]),
            ],
            "review": PiScript(final_text=verdict_text("pass")),
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "landed"
    implement_calls = adapter.calls_for("implement")
    assert [call.attempt for call in implement_calls] == [1, 2]
    assert implement_calls[1].model.render() == task.models["fallback"].render()
    subjects = _git(tmp_project, "log", "-2", "--format=%s").splitlines()
    assert subjects == [
        "helmraiser: iteration 2 of fix-widget",
        "helmraiser: partial work of iteration 1 (timed out)",
    ]
    escalated = next(event for event in ledger.events(run.run_id) if event["type"] == "escalated")
    assert escalated["payload"]["reason"] == "timeout"


async def test_expired_first_model_slice_escalates_before_next_iteration(
    make_task, make_run, monkeypatch, ledger
):
    task = make_task(max_minutes=50)
    clock = FakeClock()
    real_run_verify = runner_module.run_verify
    verify_calls = 0

    def advance_after_verify(*args, **kwargs):
        nonlocal verify_calls
        results = real_run_verify(*args, **kwargs)
        verify_calls += 1
        if verify_calls == 1:
            clock.advance(31 * 60)
        return results

    monkeypatch.setattr(runner_module, "run_verify", advance_after_verify)
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(files=[("attempt.txt", "first\n")]),
                PiScript(files=[("done.txt", "fallback\n")]),
            ]
        }
    )
    run, _ = make_run(task, adapter, clock=clock)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval"
    calls = adapter.calls_for("implement")
    assert [call.attempt for call in calls] == [1, 2]
    assert calls[1].model.render() == task.models["fallback"].render()
    escalated = next(event for event in ledger.events(run.run_id) if event["type"] == "escalated")
    assert escalated["payload"]["reason"] == "timeout"


async def test_fallback_timeout_commits_partial_work_then_fails(make_task, make_run):
    task = make_task(max_iterations=1)
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(files=[("first.txt", "failed verify\n")]),
                PiScript(
                    ok=False,
                    error_kind="timeout",
                    error_message="fallback stuck",
                    files=[("fallback-partial.txt", "kept\n")],
                ),
            ]
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert (outcome.status, outcome.failure_reason) == ("failed", "timeout: fallback stuck")
    worktree = Path(outcome.worktree_path)
    assert (worktree / "fallback-partial.txt").read_text() == "kept\n"
    assert _git(worktree, "log", "-1", "--format=%s") == (
        "helmraiser: partial work of iteration 2 (timed out)"
    )


async def test_over_budget_fallback_timeout_still_commits_partial_work(make_task, make_run):
    task = make_task(max_minutes=10, max_iterations=1)
    clock = FakeClock()

    class OverBudgetFallbackAdapter(ScriptedPiAdapter):
        async def run(self, req, on_event, run_id):
            if req.stage == "implement" and req.attempt == 2:
                clock.advance(11 * 60)
            return await super().run(req, on_event, run_id)

    adapter = OverBudgetFallbackAdapter(
        {
            "implement": [
                PiScript(files=[("first.txt", "failed verify\n")]),
                PiScript(
                    ok=False,
                    error_kind="timeout",
                    files=[("fallback-partial.txt", "kept\n")],
                ),
            ]
        }
    )
    run, _ = make_run(task, adapter, clock=clock)

    outcome = await run.run()

    assert (outcome.status, outcome.failure_reason) == ("failed", "budget:time")
    worktree = Path(outcome.worktree_path)
    assert (worktree / "fallback-partial.txt").read_text() == "kept\n"
    assert _git(worktree, "log", "-1", "--format=%s") == (
        "helmraiser: partial work of iteration 2 (timed out)"
    )


async def test_review_gets_minimum_time_when_run_budget_is_nearly_spent(make_task, make_run):
    task = make_task(max_minutes=50)
    clock = FakeClock()

    class SlowImplementAdapter(ScriptedPiAdapter):
        async def run(self, req, on_event, run_id):
            if req.stage == "implement":
                clock.advance(48 * 60)
            return await super().run(req, on_event, run_id)

    adapter = SlowImplementAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter, clock=clock)

    await run.run()

    assert adapter.calls_for("review")[0].timeout_s == 10 * 60
