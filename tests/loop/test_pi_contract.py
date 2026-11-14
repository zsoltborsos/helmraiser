"""A pi that breaks the JSON usage contract fails the run at once and pauses the queue.

Budget caps rely on pi's usage, so a ``contract`` session (no readable usage) is
fatal in every stage: no retry, no next iteration, no escalation, no ``uncertain``
verdict. The queue pauses globally because pi is shared by every project.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, monitor
from helmraiser.ledger.db import Ledger
from helmraiser.pi.events import UNREADABLE_USAGE_MESSAGE
from helmraiser.pi.protocol import Usage
from tests.conftest import FakePi
from tests.loop.test_evidence_review_merge import verdict_text
from tests.loop.test_quota_pause import _queue_task, cli_config  # noqa: F401
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

runner = CliRunner()

SEVERE = [{"severity": "severe", "file": "done.txt", "line": 1, "note": "race remains"}]
REASON = f"pi_contract: {UNREADABLE_USAGE_MESSAGE}"


def _broken(**overrides) -> PiScript:
    fields = {
        "ok": False,
        "error_kind": "contract",
        "error_message": UNREADABLE_USAGE_MESSAGE,
        "files": [("done.txt", "ok\n")],
    }
    fields.update(overrides)
    return PiScript(**fields)


def _pause_reason(run_id: str) -> str:
    return (
        f"pi 0.87.1 broke the JSON usage contract (run {run_id[:8]}); "
        "fix Helmraiser, then run helmraiser queue resume"
    )


def _assert_contract_failure(outcome, run, ledger: Ledger, sleep) -> None:
    assert outcome.status == "failed"
    assert outcome.failure_reason == REASON
    assert sleep.calls == []
    paused = ledger.is_paused()
    assert paused == {"reason": _pause_reason(run.run_id), "until": None}
    assert outcome.paused == paused
    row = ledger.get_run(run.run_id)
    assert row["status"] == "failed" and row["failure_reason"] == REASON
    events = [e["type"] for e in ledger.events(run.run_id)]
    assert "retry" not in events and "escalated" not in events
    assert "queue_paused" in events


async def test_implement_contract_failure_is_fatal_and_pauses(make_task, make_run, ledger):
    task = make_task(max_iterations=4, fallback=True)
    adapter = ScriptedPiAdapter({"implement": _broken(usage=Usage(10, 5, 0.25))})
    run, sleep = make_run(task, adapter)

    outcome = await run.run()

    _assert_contract_failure(outcome, run, ledger, sleep)
    assert len(adapter.calls_for("implement")) == 1
    assert adapter.calls_for("review") == []
    assert not outcome.escalated
    # Usage recorded so far stays recorded.
    assert outcome.cost_usd == pytest.approx(0.25)
    assert ledger.get_run(run.run_id)["cost_usd"] == pytest.approx(0.25)
    (iteration,) = ledger.iterations(run.run_id)
    assert iteration["agent_ok"] == 0 and iteration["ended_at"] is not None
    # The branch and worktree are kept, as for any failure.
    assert outcome.branch_name and Path(outcome.worktree_path).is_dir()


async def test_contract_failure_after_a_protocol_retry_is_still_fatal(make_task, make_run, ledger):
    task = make_task()
    protocol = PiScript(ok=False, error_kind="protocol", error_message="cut short")
    adapter = ScriptedPiAdapter({"implement": [protocol, _broken()]})
    run, sleep = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "failed" and outcome.failure_reason == REASON
    assert sleep.calls == [30.0]  # the protocol error's retry, nothing after it
    assert len(adapter.calls_for("implement")) == 2
    assert ledger.is_paused() is not None


@pytest.mark.parametrize("policy", ["review", "approve-plan-first"])
async def test_plan_contract_failure_is_fatal(make_task, make_run, ledger, policy: str):
    task = make_task(plan=True, policy=policy)
    adapter = ScriptedPiAdapter({"plan": _broken(final_text="1. do it")})
    run, sleep = make_run(task, adapter)

    outcome = await run.run()

    _assert_contract_failure(outcome, run, ledger, sleep)
    assert adapter.calls_for("implement") == []


async def test_review_contract_failure_is_fatal_not_uncertain(make_task, make_run, ledger):
    task = make_task()
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": _broken(final_text=verdict_text("pass"), files=[]),
        }
    )
    run, sleep = make_run(task, adapter)

    outcome = await run.run()

    _assert_contract_failure(outcome, run, ledger, sleep)
    assert outcome.verdict is None
    assert len(adapter.calls_for("review")) == 1


async def test_review_fix_contract_failure_is_fatal(make_task, make_run, ledger):
    adapter = ScriptedPiAdapter(
        {
            "implement": [PiScript(files=[("done.txt", "first\n")]), _broken(files=[])],
            "review": PiScript(final_text=verdict_text("concerns", SEVERE)),
        }
    )
    run, sleep = make_run(make_task(), adapter)

    outcome = await run.run()

    _assert_contract_failure(outcome, run, ledger, sleep)
    assert len(adapter.calls_for("implement")) == 2
    assert len(adapter.calls_for("review")) == 1
    events = [e["type"] for e in ledger.events(run.run_id)]
    assert "review_round_abandoned" not in events


async def test_fallback_contract_failure_is_fatal(make_task, make_run, ledger):
    task = make_task(max_iterations=1, fallback=True)
    adapter = ScriptedPiAdapter({"implement": [PiScript(files=[("other.txt", "x\n")]), _broken()]})
    run, sleep = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "failed" and outcome.failure_reason == REASON
    assert outcome.escalated  # verify failed with the first model, then fallback broke
    assert len(adapter.calls_for("implement")) == 2
    assert ledger.is_paused() is not None


# ── through the CLI, with the fake pi dropping usage ─────────────────


NO_USAGE_EVENTS = [
    {"type": "agent_start"},
    {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "done"}],
            "stopReason": "stop",
        },
    },
    {"type": "agent_settled"},
]


def test_work_pauses_the_queue_on_a_broken_pi(
    tmp_project: Path,
    tmp_path: Path,
    data_dir: Path,
    fake_pi: FakePi,
    cli_config: dict,  # noqa: F811
    git_init,
    monkeypatch: pytest.MonkeyPatch,
):
    first = _queue_task(tmp_project, data_dir)
    other = tmp_path / "other"
    other.mkdir()
    git_init(other)
    second = _queue_task(other, data_dir)
    fake_pi.write_script(
        {"version": "0.99.2", "default": {"events": NO_USAGE_EVENTS, "files": [["done.txt", ""]]}}
    )

    result = runner.invoke(app, ["work", "--json", "--data-dir", str(data_dir)])

    assert result.exit_code == 3, result.output
    outcome, batch = [json.loads(line) for line in result.output.strip().splitlines()]
    assert outcome["status"] == "failed"
    assert outcome["failure_reason"] == REASON
    run_id = outcome["run_id"]
    reason = (
        f"pi 0.99.2 broke the JSON usage contract (run {run_id[:8]}); "
        "fix Helmraiser, then run helmraiser queue resume"
    )
    assert outcome["paused"] == {"reason": reason, "until": None}
    assert batch["paused"] == {"reason": reason, "until": None}
    ledger = Ledger(data_dir / "helmraiser.db")
    started = {ledger.get_run(run_id)["task_id"]}
    assert started <= {first, second}
    assert {first, second} - started  # the other task was never started
    assert [c["session_id"] for c in fake_pi.calls() if c["session_id"]] == [f"{run_id}-implement"]

    monkeypatch.setattr(monitor, "load_config", lambda: cli_config)
    status = runner.invoke(app, ["status", "--data-dir", str(data_dir)])
    assert "paused" in status.output.lower()
    assert f"broketheJSONusagecontract(run{run_id[:8]})" in "".join(status.output.split())

    resumed = runner.invoke(app, ["queue", "resume", "--data-dir", str(data_dir)])
    assert resumed.exit_code == 0 and ledger.is_paused() is None
