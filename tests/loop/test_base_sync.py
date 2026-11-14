"""New PR runs synchronize on the host before capturing their local base."""

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from helmraiser.isolation.process import ProcessIsolation
from helmraiser.loop.runner import GovernedRun
from helmraiser.pipeline.worktree import WorktreeManager
from helmraiser.task.loader import render_task
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter
from tests.pipeline.test_base_sync import git, setup_remote

PLAN = "# Plan\n\nCreate done.txt and verify."
REVIEW = '```json\n{"verdict": "pass", "findings": [], "evidence_checks": []}\n```'


def plan_adapter():
    return ScriptedPiAdapter({"plan": PiScript(final_text=PLAN)})


def assert_base_sync(run, outcome, ledger, expected, status, reason):
    assert outcome.status == "awaiting_approval", outcome
    assert outcome.pending_approval == "plan"
    assert outcome.failure_reason is None
    assert outcome.base_commit == expected
    assert run.worktrees.metadata(run.run_id)["base_commit"] == expected
    assert ledger.get_run(run.run_id)["base_commit"] == expected
    assert git(Path(outcome.worktree_path), "rev-parse", "HEAD") == expected
    events = ledger.events(run.run_id)
    syncs = [e for e in events if e["type"] == "base_sync"]
    assert len(syncs) == 1
    payload = syncs[0]["payload"]
    assert payload["status"] == status and payload["reason"] == reason
    assert payload["to_commit"] == expected
    types = [e["type"] for e in events]
    assert types.index("base_sync") < types.index("worktree_created")
    jsonl = [json.loads(line) for line in run.rundir.events_jsonl.read_text().splitlines()]
    assert [e["payload"] for e in jsonl if e["event_type"] == "base_sync"] == [payload]
    return payload


@pytest.mark.parametrize("process_isolation", [False, True])
async def test_base_sync_pr_run_captures_upstream(
    tmp_project, tmp_path, make_task, make_run, ledger, monkeypatch, process_isolation
):
    _, _, old, new = setup_remote(tmp_project, tmp_path)
    task = make_task(delivery="pr", policy="approve-plan-first")
    overrides = {}
    if process_isolation:
        task = replace(task, isolation="process")
        isolation = ProcessIsolation({})
        monkeypatch.setattr(isolation, "check_available", lambda: None)
        overrides["isolation"] = isolation
    original = subprocess.run
    fetches = []

    def host_fetch(command, **kwargs):
        if command[:2] == ["git", "fetch"]:
            fetches.append(command)
            assert kwargs["cwd"] == tmp_project
            assert kwargs["timeout"] == 60
        # Scripted pi does not spawn srt; base synchronization must not either.
        assert "srt" not in command
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", host_fetch)
    run, _ = make_run(task, plan_adapter(), **overrides)
    outcome = await run.run()
    payload = assert_base_sync(run, outcome, ledger, new, "synced", "fast_forward")
    assert payload["from_commit"] == old
    assert payload["upstream_commit"] == new
    assert len(fetches) == 1
    assert (tmp_project / "README.md").read_text() == "upstream content\n"


@pytest.mark.parametrize(
    "case, status, reason",
    [
        ("dirty", "skipped", "dirty_checkout"),
        ("ahead", "skipped", "not_fast_forward"),
        ("diverged", "skipped", "not_fast_forward"),
        ("no_upstream", "skipped", "no_upstream"),
        ("fetch_failure", "failed", "fetch_failed"),
        ("timeout", "failed", "fetch_failed"),
    ],
)
async def test_base_sync_nonfatal_run_start(
    tmp_project, tmp_path, make_task, make_run, ledger, monkeypatch, case, status, reason
):
    _, _, old, new = setup_remote(tmp_project, tmp_path)
    if case == "dirty":
        (tmp_project / "README.md").write_text("preserve edits")
    elif case in {"ahead", "diverged"}:
        if case == "ahead":
            git(tmp_project, "fetch", "origin")
            git(tmp_project, "merge", "--ff-only", new)
        (tmp_project / "local.txt").write_text("local")
        git(tmp_project, "add", "local.txt")
        git(tmp_project, "commit", "-m", "local change")
        old = git(tmp_project, "rev-parse", "HEAD")
    elif case == "no_upstream":
        git(tmp_project, "config", "--unset", "branch.main.remote")
    elif case == "fetch_failure":
        git(tmp_project, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    else:
        original = subprocess.run

        def timeout(command, **kwargs):
            if command[:2] == ["git", "fetch"]:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return original(command, **kwargs)

        monkeypatch.setattr(subprocess, "run", timeout)
    run, _ = make_run(make_task(delivery="pr", policy="approve-plan-first"), plan_adapter())
    payload = assert_base_sync(run, await run.run(), ledger, old, status, reason)
    assert payload["from_commit"] == old
    assert git(tmp_project, "rev-parse", "HEAD") == old
    if case == "dirty":
        assert (tmp_project / "README.md").read_text() == "preserve edits"


async def test_base_sync_merge_delivery_never_fetches(
    tmp_project, tmp_path, make_task, make_run, ledger, monkeypatch
):
    _, _, old, _ = setup_remote(tmp_project, tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("merge delivery must not synchronize")

    monkeypatch.setattr(WorktreeManager, "sync_base", forbidden)
    original = subprocess.run

    def no_fetch(command, **kwargs):
        assert command[:2] != ["git", "fetch"]
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", no_fetch)
    run, _ = make_run(make_task(delivery="merge", policy="approve-plan-first"), plan_adapter())
    outcome = await run.run()
    assert outcome.pending_approval == "plan"
    assert outcome.base_commit == old
    assert not [e for e in ledger.events(run.run_id) if e["type"] == "base_sync"]
    assert git(tmp_project, "rev-parse", "HEAD") == old


@pytest.mark.parametrize("base_source", ["explicit", "project_default"])
async def test_base_sync_intended_base(
    tmp_project, tmp_path, make_task, make_run, ledger, base_source
):
    _, _, old, new = setup_remote(tmp_project, tmp_path)
    git(tmp_project, "checkout", "-b", "other")
    if base_source == "project_default":
        config_path = tmp_project / ".helmraiser" / "project.toml"
        config_path.parent.mkdir(exist_ok=True)
        config_path.write_text('[defaults]\nbase = "main"\n')
    task = make_task(
        delivery="pr",
        policy="approve-plan-first",
        base="main" if base_source == "explicit" else None,
    )
    assert task.base == "main"
    run, _ = make_run(task, plan_adapter())
    assert_base_sync(run, await run.run(), ledger, new, "synced", "fast_forward")
    assert git(tmp_project, "rev-parse", "HEAD") == old
    assert git(tmp_project, "symbolic-ref", "--short", "HEAD") == "other"


async def test_base_sync_resume_keeps_captured_base(
    tmp_project, tmp_path, make_task, make_run, ledger, config, data_dir, monkeypatch
):
    _, writer, _, new = setup_remote(tmp_project, tmp_path)
    adapter = ScriptedPiAdapter(
        {
            "plan": PiScript(final_text=PLAN),
            "implement": PiScript(files=[("done.txt", "done")]),
            "review": PiScript(final_text=REVIEW),
        }
    )
    run, _ = make_run(make_task(delivery="pr", policy="approve-plan-first"), adapter)
    paused = await run.run()
    assert_base_sync(run, paused, ledger, new, "synced", "fast_forward")
    (writer / "later.txt").write_text("later")
    git(writer, "add", "later.txt")
    git(writer, "commit", "-m", "later upstream commit")
    git(writer, "push", "origin", "main")
    pending = ledger.pending_approval(run.run_id)
    ledger.decide_approval(int(pending["id"]), "approved", "go")

    def forbidden(*args, **kwargs):
        raise AssertionError("resumed runs must not synchronize")

    monkeypatch.setattr(WorktreeManager, "sync_base", forbidden)
    resumed = GovernedRun.resume(
        run.run_id,
        config=config,
        ledger=ledger,
        adapter_factory=lambda state, isolation: adapter,
        pi_version="0.87.1",
        data_dir=data_dir,
        auth_check=lambda model: None,
    )
    outcome = await resumed.run()
    assert outcome.status == "awaiting_approval", outcome
    assert outcome.pending_approval == "merge"
    assert outcome.base_commit == new
    assert resumed.worktrees.metadata(run.run_id)["base_commit"] == new
    assert len([e for e in ledger.events(run.run_id) if e["type"] == "base_sync"]) == 1
    assert git(tmp_project, "rev-parse", "main") == new


async def test_base_sync_releases_dependency_ancestry(
    tmp_project, tmp_path, make_task, make_run, ledger
):
    from tests.ledger.test_db import run_row

    _, _, _, new = setup_remote(tmp_project, tmp_path)
    task = make_task(delivery="pr", policy="approve-plan-first")
    upstream = replace(task, id="prerequisite", status="landed")
    dependent = replace(task, id="dependent", depends_on=("prerequisite",), base="main")
    for spec in (upstream, dependent):
        ledger.upsert_task(spec, render_task(spec))
    ledger.create_run(
        {
            **run_row("prerequisite-run", upstream.task_id, str(tmp_project), "landed"),
            "head_commit": new,
        }
    )
    assert ledger.next_queued(exclude=(task.task_id,)) is None
    assert ledger.held_tasks()[dependent.task_id]["reason"] == "depends_on"
    run, _ = make_run(task, plan_adapter())
    assert_base_sync(run, await run.run(), ledger, new, "synced", "fast_forward")
    # The paused run still holds the project; release only that hold to query selection.
    ledger.update_run(run.run_id, status="aborted")
    ledger.set_task_status(task.task_id, "aborted")
    assert ledger.next_queued()["task_id"] == dependent.task_id
    assert dependent.task_id not in ledger.held_tasks()
