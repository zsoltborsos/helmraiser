"""Maintenance ownership is established before fresh sessions, never on resume."""

import json
import subprocess

import pytest

from helmraiser.loop.runner import GovernedRun
from helmraiser.pipeline.worktree import WorktreeManager
from tests.loop.test_base_sync import PLAN, REVIEW
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter
from tests.pipeline.test_worktree import git


def maintenance_events(run, ledger):
    payloads = [e["payload"] for e in ledger.events(run.run_id) if e["type"] == "git_maintenance"]
    jsonl = [json.loads(line) for line in run.rundir.events_jsonl.read_text().splitlines()]
    assert [e["payload"] for e in jsonl if e["event_type"] == "git_maintenance"] == payloads
    return payloads


@pytest.mark.parametrize("previous", [None, "false", "true"])
async def test_fresh_run_records_maintenance_change(
    tmp_project, make_task, make_run, ledger, previous
):
    if previous is not None:
        git(tmp_project, "config", "--local", "maintenance.auto", previous)

    def check_setting(cwd):
        assert git(cwd, "config", "--local", "--get", "maintenance.auto") == "false"

    adapter = ScriptedPiAdapter({"plan": PiScript(final_text=PLAN, action=check_setting)})
    run, _ = make_run(make_task(policy="approve-plan-first"), adapter)
    assert (await run.run()).pending_approval == "plan"
    expected = (
        [] if previous == "false" else [{"set": "maintenance.auto=false", "previous": previous}]
    )
    assert maintenance_events(run, ledger) == expected


async def test_maintenance_disabled_before_pr_sync(tmp_project, make_task, make_run, monkeypatch):
    original = WorktreeManager.sync_base
    calls = []

    def sync(manager, **kwargs):
        assert git(tmp_project, "config", "--local", "--get", "maintenance.auto") == "false"
        calls.append("sync")
        return original(manager, **kwargs)

    monkeypatch.setattr(WorktreeManager, "sync_base", sync)
    adapter = ScriptedPiAdapter({"plan": PiScript(final_text=PLAN)})
    run, _ = make_run(make_task(delivery="pr", policy="approve-plan-first"), adapter)
    assert (await run.run()).pending_approval == "plan"
    assert calls == ["sync"]


@pytest.mark.parametrize(
    "error", [RuntimeError("broken"), OSError("broken"), subprocess.SubprocessError("broken")]
)
async def test_maintenance_failure_does_not_fail_run(
    make_task, make_run, ledger, monkeypatch, error
):
    def fail(manager):
        raise error

    monkeypatch.setattr(WorktreeManager, "disable_auto_maintenance", fail)
    run, _ = make_run(
        make_task(policy="approve-plan-first"),
        ScriptedPiAdapter({"plan": PiScript(final_text=PLAN)}),
    )
    assert (await run.run()).pending_approval == "plan"
    assert maintenance_events(run, ledger) == [{"error": "broken"}]


async def test_resume_does_not_disable_auto_maintenance(
    make_task, make_run, ledger, config, data_dir, monkeypatch
):
    adapter = ScriptedPiAdapter(
        {
            "plan": PiScript(final_text=PLAN),
            "implement": PiScript(files=[("done.txt", "done")]),
            "review": PiScript(final_text=REVIEW),
        }
    )
    run, _ = make_run(make_task(policy="approve-plan-first"), adapter)
    assert (await run.run()).pending_approval == "plan"
    pending = ledger.pending_approval(run.run_id)
    ledger.decide_approval(int(pending["id"]), "approved", "go")

    def forbidden(*args, **kwargs):
        pytest.fail("resume must not disable maintenance")

    monkeypatch.setattr(WorktreeManager, "disable_auto_maintenance", forbidden)
    resumed = GovernedRun.resume(
        run.run_id,
        config=config,
        ledger=ledger,
        adapter_factory=lambda state, isolation: adapter,
        pi_version="0.87.1",
        data_dir=data_dir,
        auth_check=lambda model: None,
    )
    assert (await resumed.run()).pending_approval == "merge"
    assert maintenance_events(resumed, ledger) == [
        {"set": "maintenance.auto=false", "previous": None}
    ]
