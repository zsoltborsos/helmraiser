"""Transport selection belongs only at the implement spawn boundary."""

import pytest

from helmraiser.loop.runner import RETRY_DELAY_SECONDS
from helmraiser.pi.protocol import Usage
from tests.loop.test_planned_runs import PLAN, REVIEW
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter


@pytest.mark.parametrize("flag", [None, False, True])
@pytest.mark.parametrize(
    "stage", [None, "implement", "plan", "review", "intake", "scout", "compare"]
)
async def test_spawn_transport_boundary(make_task, make_run, config, monkeypatch, flag, stage):
    if flag is None:
        config.pop("pi_rpc_implement", None)
    else:
        config["pi_rpc_implement"] = flag
    task = make_task()
    adapter = ScriptedPiAdapter()
    run, _ = make_run(task, adapter)
    run.worktree = task.project
    run.base_commit = "a" * 40
    captured = []

    async def session(req, enforce_budget):
        captured.append(req)
        return await adapter.run(req, lambda event: None, run.run_id)

    monkeypatch.setattr(run, "_run_session", session)
    await run._spawn(
        "Begin.", "session", task.models["implement"], 1, **({"stage": stage} if stage else {})
    )
    [req] = captured
    assert req.rpc is (flag is True and stage in (None, "implement"))
    assert req.stage == (stage or "implement")


@pytest.mark.parametrize("flag", [False, True])
async def test_planned_run_feedback_keeps_implement_transport(
    make_task, make_run, ledger, config, flag
):
    config["pi_rpc_implement"] = flag
    task = make_task([("content", "grep -q ok done.txt")], plan=True)
    adapter = ScriptedPiAdapter(
        {
            "plan": PiScript(final_text=PLAN),
            "implement": [
                PiScript(files=[("done.txt", "nope\n")]),
                PiScript(files=[("done.txt", "ok\n")]),
            ],
            "review": PiScript(final_text=REVIEW),
        }
    )
    run, sleep = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval"
    assert outcome.iterations == 2
    assert [req.stage for req in adapter.calls] == ["plan", "implement", "implement", "review"]
    assert [req.rpc for req in adapter.calls] == [False, flag, flag, False]
    first, second = adapter.calls_for("implement")
    assert first.session_id == second.session_id == f"{run.run_id}-implement"
    assert second.attempt == first.attempt == 1
    assert PLAN in first.prompt
    assert "Verify failed" in second.prompt and "### content (exit 1)" in second.prompt
    assert [row["verify_passed"] for row in ledger.iterations(run.run_id)] == [0, 1]
    assert sleep.calls == []


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("kind", ["provider", "protocol"])
async def test_retry_preserves_transport_and_usage(make_task, make_run, config, flag, kind):
    config["pi_rpc_implement"] = flag
    task = make_task()
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(
                    ok=False, error_kind=kind, error_message="try again", usage=Usage(10, 5, 0.1)
                ),
                PiScript(files=[("done.txt", "ok\n")], usage=Usage(20, 7, 0.2)),
            ]
        }
    )
    run, sleep = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval"
    first, second = adapter.calls_for("implement")
    assert first.rpc is second.rpc is flag
    assert first.session_id == second.session_id == f"{run.run_id}-implement"
    assert first.prompt == second.prompt
    assert sleep.calls == [RETRY_DELAY_SECONDS]
    assert outcome.cost_usd == pytest.approx(0.3)
    assert run.usage == Usage(30, 12, pytest.approx(0.3))
