"""``approve-plan-first`` (spec §4.6): plan stage → awaiting_approval(plan) →
resume at implement with the plan in the prompt → review → awaiting merge approval."""

from __future__ import annotations

import subprocess
from pathlib import Path

from helmraiser.ledger.db import Ledger
from helmraiser.loop.runner import GovernedRun
from helmraiser.pi.protocol import Usage
from helmraiser.storage.state import StateManager
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

PLAN = "# Plan\n\n1. Create done.txt.\n2. Run verify.\n\nOpen question: none."
REVIEW = '```json\n{"verdict": "pass", "findings": [], "evidence_checks": []}\n```'


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


async def test_plan_stage_waits_for_approval(
    make_task, make_run, ledger: Ledger, data_dir, tmp_project
):
    task = make_task(policy="approve-plan-first")
    adapter = ScriptedPiAdapter({"plan": PiScript(final_text=PLAN, usage=Usage(10, 5, 0.03))})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval" and outcome.pending_approval == "plan"
    assert outcome.iterations == 0 and outcome.cost_usd == 0.03
    (req,) = adapter.calls
    assert req.stage == "plan" and req.session_id == f"{run.run_id}-plan"
    assert req.model.render() == "openai-codex/gpt-6.1-sol:high"  # plan model
    assert req.tools == ("read", "grep", "find", "ls") and req.context_files is True
    assert req.system_prompt_path == data_dir / "runs" / run.run_id / "directives-plan.md"
    assert "planning only" in req.system_prompt_path.read_text()
    assert "waits for approval" in req.system_prompt_path.read_text()
    assert "Make the widget work." in req.prompt and "Do not implement anything." in req.prompt

    # Helmraiser stored the plan in the run directory without touching the worktree.
    worktree = Path(outcome.worktree_path)
    plan_path = data_dir / "runs" / run.run_id / "plan.md"
    assert plan_path.read_text() == PLAN + "\n"
    assert _git(worktree, "rev-parse", "HEAD") == outcome.base_commit
    assert _git(worktree, "status", "--porcelain") == ""
    assert outcome.head_commit is None

    # Ledger: pending plan approval, run awaiting, task awaiting, lock released.
    pending = ledger.pending_approval(run.run_id)
    assert pending["kind"] == "plan" and pending["decision"] == "pending"
    row = ledger.get_run(run.run_id)
    assert row["status"] == "awaiting_approval" and row["stage"] is None
    assert ledger.get_task(task.task_id)["status"] == "awaiting_approval"
    assert (
        StateManager(tmp_project / ".helmraiser" / "state.db").get_locked_run_id("fix-widget")
        is None
    )
    events = [e for e in ledger.events(run.run_id) if e["source"] == "helmraiser"]
    types = [e["type"] for e in events]
    assert "plan_written" in types and "iteration_start" not in types
    stage_events = [e["payload"]["stage"] for e in events if e["type"] == "stage_start"]
    assert stage_events == ["plan"]
    plan_event = next(e for e in events if e["type"] == "plan_written")
    assert plan_event["payload"] == {"path": str(plan_path), "chars": len(PLAN)}
    summary = (data_dir / "runs" / run.run_id / "summary.md").read_text()
    assert f"helmraiser approve {run.run_id}" in summary and "the plan" in summary


async def test_project_notes_reach_plan_directives(make_task, make_run, tmp_project, data_dir):
    notes = "Tests must not make external calls."
    notes_path = tmp_project / ".helmraiser" / "notes.md"
    notes_path.parent.mkdir(exist_ok=True)
    notes_path.write_text(notes, encoding="utf-8")
    task = make_task(policy="approve-plan-first")
    adapter = ScriptedPiAdapter({"plan": PiScript(final_text=PLAN)})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.pending_approval == "plan"
    directives = (data_dir / "runs" / run.run_id / "directives-plan.md").read_text(encoding="utf-8")
    assert directives.count("## Project notes (Helmraiser)") == 1
    assert directives.count(notes) == 1


async def test_approve_resumes_at_implement_with_the_plan(
    make_task, make_run, ledger: Ledger, data_dir, config, tmp_project
):
    task = make_task(policy="approve-plan-first")
    adapter = ScriptedPiAdapter(
        {
            "plan": PiScript(final_text=PLAN, usage=Usage(10, 5, 0.03)),
            "implement": PiScript(files=[("done.txt", "")], usage=Usage(10, 5, 0.10)),
            "review": PiScript(final_text=REVIEW, usage=Usage(10, 5, 0.02)),
        }
    )
    first, _ = make_run(task, adapter)
    paused = await first.run()
    assert paused.pending_approval == "plan"
    pending = ledger.pending_approval(first.run_id)
    ledger.decide_approval(int(pending["id"]), "approved", "go")

    resumed = GovernedRun.resume(
        first.run_id,
        config=config,
        ledger=ledger,
        adapter_factory=lambda state, isolation: adapter,
        pi_version="0.87.1",
        data_dir=data_dir,
    )
    outcome = await resumed.run()

    assert outcome.run_id == first.run_id
    assert outcome.status == "awaiting_approval" and outcome.pending_approval == "merge"
    assert outcome.failure_reason is None
    assert outcome.iterations == 1 and round(outcome.cost_usd, 2) == 0.15
    assert outcome.worktree_path == paused.worktree_path
    assert outcome.base_commit == paused.base_commit

    # Implement ran in the same worktree with the plan verbatim in its prompt.
    (implement,) = adapter.calls_for("implement")
    assert implement.session_id == f"{first.run_id}-implement"
    assert implement.cwd == Path(paused.worktree_path)
    assert "## Approved plan" in implement.prompt and PLAN in implement.prompt
    assert implement.prompt.endswith("Begin.")
    assert (Path(outcome.worktree_path) / "done.txt").exists()
    assert (data_dir / "runs" / first.run_id / "plan.md").read_text() == PLAN + "\n"

    # Then review as usual; behaves as ``review`` policy afterwards.
    (review,) = adapter.calls_for("review")
    assert review.session_id == f"{first.run_id}-review"
    assert "## Approved plan" in review.prompt and PLAN in review.prompt
    assert outcome.verdict["verdict"] == "pass"
    row = ledger.get_run(first.run_id)
    assert row["status"] == "awaiting_approval" and row["verdict"] == "pass"
    assert round(row["cost_usd"], 2) == 0.15 and row["tokens_in"] == 30
    assert ledger.pending_approval(first.run_id)["kind"] == "merge"
    assert [a["decision"] for a in ledger.approvals(first.run_id)] == ["approved", "pending"]
    types = [e["type"] for e in ledger.events(first.run_id) if e["source"] == "helmraiser"]
    assert types.index("run_resume") > types.index("run_end")
    assert types.count("run_end") == 2
    assert _git(tmp_project, "rev-parse", "HEAD") == outcome.base_commit  # nothing merged yet


async def test_plan_blocked_fails_the_run(make_task, make_run, ledger):
    task = make_task(policy="approve-plan-first")
    adapter = ScriptedPiAdapter({"plan": PiScript(final_text="BLOCKED: the spec is contradictory")})
    run, _ = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.status == "failed"
    assert outcome.failure_reason == "blocked: the spec is contradictory"
    assert ledger.pending_approval(run.run_id) is None


async def test_plan_session_error_wins_over_blocked_text(make_task, make_run, ledger):
    task = make_task(policy="approve-plan-first")
    adapter = ScriptedPiAdapter(
        {"plan": PiScript(final_text="BLOCKED: out of quota", error_kind="quota")}
    )
    run, _ = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.failure_reason is not None
    assert not outcome.failure_reason.startswith("blocked")


async def test_resume_unknown_run(ledger, config, data_dir):
    import pytest

    with pytest.raises(ValueError, match="unknown run"):
        GovernedRun.resume(
            "nope",
            config=config,
            ledger=ledger,
            adapter_factory=lambda s, i: ScriptedPiAdapter(),
            pi_version="0.87.1",
            data_dir=data_dir,
        )
