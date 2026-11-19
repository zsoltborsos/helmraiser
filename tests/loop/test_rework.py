"""Approver feedback and the resumed governed pipeline."""

import pytest

from helmraiser.loop.prompts import review_feedback_prompt, rework_prompt
from helmraiser.loop.runner import GovernedRun
from tests.loop.test_evidence_review_merge import verdict_text
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter


def hand_back_rework(ledger, run_id, task, note="Please fix this", findings=None, *, rebase=False):
    allowance = min(2, task.budget.max_iterations)
    pending = ledger.pending_approval(run_id)
    assert ledger.hand_back(
        run_id,
        "implement",
        rework={
            **({"rebase": True} if rebase else {}),
            "approval_id": pending["id"],
            "note": note,
            "findings": findings or [],
            "iteration_allowance": allowance,
            "extra_cost_usd": task.budget.max_cost_usd * allowance / task.budget.max_iterations,
            "extra_minutes": task.budget.max_minutes * allowance / task.budget.max_iterations,
        },
    )


def resume_rework(run_id, adapter, config, ledger, data_dir, **overrides):
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


def test_rework_prompt_rebase_block_comes_first(make_task):
    task = make_task()
    note = "  Keep the whole note\nincluding whitespace.  \n"
    prompt = rework_prompt(task, note, [], rebase_onto="main")
    assert prompt.startswith("## Rebase request")
    for command in ("git rebase main", "git rebase --continue", "git rebase --abort", "BLOCKED:"):
        assert command in prompt
    assert prompt.index("## Rebase request") < prompt.index(note) < prompt.index("## Acceptance")


def test_rework_prompt_without_rebase_is_unchanged(make_task):
    task = make_task()
    prompt = rework_prompt(task, "Fix this", [])
    assert prompt == rework_prompt(task, "Fix this", [], rebase_onto=None)
    assert "## Rebase request" not in prompt


@pytest.mark.parametrize("severities", [[], ["minor"], ["severe", "minor"]])
def test_rework_prompt_order_scope_and_full_note(make_task, severities):
    task = make_task(
        paths=("done.txt",),
        evidence=[
            {
                "name": "proof",
                "run": "echo ok > $HELMRAISER_EVIDENCE_DIR/proof.txt",
                "artifacts": ["proof.txt"],
                "criteria": "ok",
            }
        ],
    )
    note = "  café\n" + "long note\n" * 700 + "終わり  \n"
    findings = [
        {"severity": s, "file": "widget.py", "line": 5, "note": f"fix {s}"} for s in severities
    ]
    prompt = rework_prompt(task, note, findings)
    assert note in prompt
    assert prompt.index(note) < prompt.index("Address every review") < prompt.index("## Acceptance")
    assert "`done.txt`" in prompt and "proof.txt" in prompt
    assert "optional" not in prompt and "Verify failed" not in prompt
    if not findings:
        assert "No reviewer findings" in prompt
    for finding in findings:
        assert finding["note"] in prompt


@pytest.mark.parametrize(
    "verdict,policy,delivery",
    [
        ("concerns", "review", "merge"),
        ("uncertain", "review", "pr"),
        ("uncertain", "auto", "merge"),
    ],
)
async def test_rework_resumes_same_pipeline_twice(
    make_task,
    make_run,
    ledger,
    data_dir,
    config,
    verdict,
    policy,
    delivery,
):
    # Park the original minor finding for owner rework, not automatic feedback.
    config["review_feedback_rounds"] = 0
    task = make_task(
        policy=policy,
        delivery=delivery,
        evidence=[
            {
                "name": "proof",
                "run": "echo ok > $HELMRAISER_EVIDENCE_DIR/proof.txt",
                "artifacts": ["proof.txt"],
                "criteria": "ok",
            }
        ],
    )
    findings = [{"severity": "minor", "file": "done.txt", "line": 1, "note": "clarify widget"}]
    first_adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "first")]),
            "review": PiScript(final_text=verdict_text(verdict, findings)),
        }
    )
    first, _ = make_run(task, first_adapter)
    original = await first.run()
    assert original.status == "awaiting_approval"
    assert "## Owner's rework requests" not in first_adapter.calls_for("review")[0].prompt
    for round_n in (1, 2):
        note = f"café round {round_n}\nrequired change"
        hand_back_rework(ledger, first.run_id, task, note, findings)
        adapter = ScriptedPiAdapter(
            {
                "implement": PiScript(files=[("done.txt", str(round_n))]),
                "review": PiScript(
                    final_text=verdict_text(
                        "pass",
                        evidence_checks=[{"name": "proof", "meets_criteria": True}],
                    )
                ),
            }
        )
        resumed = resume_rework(first.run_id, adapter, config, ledger, data_dir)
        assert resumed.claim_resume()
        outcome = await resumed.run()
        assert outcome.status == "awaiting_approval", outcome.failure_reason
        assert outcome.pending_approval == "merge"
        assert outcome.verdict["verdict"] == "pass"
        assert (
            outcome.run_id,
            outcome.worktree_path,
            outcome.branch_name,
            outcome.base_commit,
        ) == (
            original.run_id,
            original.worktree_path,
            original.branch_name,
            original.base_commit,
        )
        assert outcome.iterations == 1 + round_n
        prompt = adapter.calls_for("implement")[0].prompt
        assert prompt.index(note) < prompt.index("clarify widget") < prompt.index("## Acceptance")
        assert "Verify failed" not in prompt
        assert outcome.verify_results[0]["passed"] and outcome.evidence_results[0]["ok"]
        assert len(adapter.calls_for("review")) == 1
        # The reviewer judges against the task as amended by every rework note so far.
        owner_section = adapter.calls_for("review")[0].prompt.split("## Task", 1)[0]
        assert "## Owner's rework requests" in owner_section
        assert "amend the task brief and acceptance block" in owner_section
        assert "do not report following them as a finding" in owner_section
        notes = [f"café round {n}\nrequired change" for n in range(1, round_n + 1)]
        assert all(note in owner_section for note in notes)
        assert [owner_section.index(note) for note in notes] == sorted(
            owner_section.index(note) for note in notes
        )
        assert ledger.pending_approval(first.run_id)["kind"] == "merge"
        summary = resumed.rundir.summary_md.read_text()
        assert f"rework rounds: {round_n}" in summary and note.replace("\n", "\n  ") in summary
        assert "helmraiser rework" in summary
        assert (data_dir / "runs" / first.run_id / f"review-{round_n}.md").is_file()
        event = next(e for e in reversed(ledger.events(first.run_id)) if e["type"] == "run_resume")
        assert event["payload"]["from"] == "rework"
        assert event["payload"]["rework_round"] == round_n
    assert [a["decision"] for a in ledger.approvals(first.run_id)] == [
        "rework",
        "rework",
        "pending",
    ]


@pytest.mark.parametrize("approved", [True, False])
async def test_rework_preserves_plan_approval_distinction(
    make_task,
    make_run,
    ledger,
    data_dir,
    config,
    approved,
):
    task = make_task(policy="approve-plan-first" if approved else "review", plan=True)
    adapter = ScriptedPiAdapter(
        {
            "plan": PiScript(final_text="# Saved plan\nKeep this design"),
            "implement": PiScript(files=[("done.txt", "initial")]),
            "review": PiScript(final_text=verdict_text("pass")),
        }
    )
    first, _ = make_run(task, adapter)
    await first.run()
    if approved:
        ledger.decide_approval(
            ledger.pending_approval(first.run_id)["id"], "approved", "owner decision"
        )
        ledger.hand_back(first.run_id, "implement")
        await resume_rework(first.run_id, adapter, config, ledger, data_dir).run()
    hand_back_rework(ledger, first.run_id, task)
    rework_adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "fixed")]),
            "review": PiScript(final_text=verdict_text("pass")),
        }
    )
    outcome = await resume_rework(first.run_id, rework_adapter, config, ledger, data_dir).run()
    assert outcome.status == "awaiting_approval"
    for call in rework_adapter.calls:
        assert "Keep this design" in call.prompt
        assert ("## Approved plan" in call.prompt) == approved
    assert not rework_adapter.calls_for("plan")


def test_default_review_feedback_requires_every_finding():
    assert review_feedback_prompt([]) == (
        "The independent review found issues. Fix every finding below, add a "
        "regression test for each where feasible, stay within the task's scope, and commit the fix."
    )
    prompt = review_feedback_prompt([{"severity": "minor"}])
    assert "## Minor findings" in prompt and "optional" not in prompt
    assert "## Severe findings" not in prompt
