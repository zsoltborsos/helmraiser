"""Owner decisions survive plan truncation and govern resumed runs."""

import pytest

from helmraiser.loop.review import EVIDENCE_INLINE_LIMIT, VERDICT_INSTRUCTION, build_review_prompt
from helmraiser.loop.runner import GovernedRun
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

NOTE_LIMIT = 16 * 1024
NOTE_MARKER = "[Owner's approval note cut after 16384 bytes.]"


def _prompt(task, tmp_path, **options):
    evidence = tmp_path / "empty-evidence"
    evidence.mkdir(exist_ok=True)
    return build_review_prompt(
        task,
        diff_path=tmp_path / "diff.patch",
        diff_text="",
        verify_results=[],
        evidence_results=[],
        evidence_dir=evidence,
        **options,
    )


def test_owner_note_precedes_task_when_plan_is_cut(make_task, tmp_path):
    note = "  Revised contract: café\n" + "Owner decision.\n" * 600 + "UNIQUE NOTE END  \n"
    assert EVIDENCE_INLINE_LIMIT < len(note.encode()) < NOTE_LIMIT
    plan = "# Long plan\n" + "Plan line.\n" * 1000
    plan += f"\n## Owner's note on approval\n\n{note}\n"
    prompt = _prompt(make_task(), tmp_path, plan=plan, plan_approval_note=note)
    owner_section = prompt.split("## Task", 1)[0]
    assert "## Owner's decisions on approval" in owner_section
    assert note in owner_section
    assert "made by the owner after the task was written" in owner_section
    assert "override the task brief and acceptance block where they conflict" in owner_section
    assert "Judge the implementation against these decisions" in owner_section
    assert "do not report following them as a finding" in owner_section
    assert "## Approved plan" in prompt
    assert "[Plan cut after 8192 bytes.]" in prompt


@pytest.mark.parametrize(
    "note_options", [{}, {"plan_approval_note": None}, {"plan_approval_note": ""}]
)
@pytest.mark.parametrize(
    ("plan", "approved"),
    [(None, True), ("Approved steps\n", True), ("Unapproved steps\n", False), ("x" * 9000, True)],
    ids=["absent", "approved", "unapproved", "cut"],
)
def test_review_prompt_without_note_is_unchanged(make_task, tmp_path, note_options, plan, approved):
    plan_section = ""
    if plan is not None:
        if approved:
            plan_section = "\n\n## Approved plan\n\n"
        else:
            plan_section = (
                "\n\n## Plan\n\nA planning model wrote this plan for the task; the implementer "
                "should follow it and say so when deviating.\n\n"
            )
        plan_section += plan[:8192].rstrip("\n")
        if len(plan) > 8192:
            plan_section += "\n\n[Plan cut after 8192 bytes.]"
    expected = (
        "# Review: Fix the widget\n\n## Task\n\n## Goal\n\nMake the widget work.\n\n"
        "## Acceptance\n\nVerify (all must exit 0; Helmraiser runs them after you stop):\n"
        "- marker: `test -f done.txt`"
        + plan_section
        + "\n\n## Diff\n\n```diff\n(empty diff)\n```\n\n## Verify results\n\n"
        "(no verify steps)\n\n## Evidence\n\n"
        "(no evidence steps; evidence_checks may be an empty list)\n\n## Evidence files\n\n"
        "(evidence directory is empty)\n\n" + VERDICT_INSTRUCTION
    )
    assert (
        _prompt(make_task(), tmp_path, plan=plan, plan_approved=approved, **note_options)
        == expected
    )


def test_owner_note_does_not_require_plan_text(make_task, tmp_path):
    note = "  Override the old acceptance requirement.  \n"
    prompt = _prompt(make_task(), tmp_path, plan=None, plan_approval_note=note)
    assert note in prompt.split("## Task", 1)[0]
    assert "## Approved plan" not in prompt


def test_owner_note_at_limit_is_not_cut(make_task, tmp_path):
    note = "a" * NOTE_LIMIT
    prompt = _prompt(make_task(), tmp_path, plan_approval_note=note)
    assert note in prompt
    assert NOTE_MARKER not in prompt


def test_overlong_owner_note_has_explicit_marker(make_task, tmp_path):
    note = "a" * NOTE_LIMIT + "UNIQUE TAIL"
    prompt = _prompt(make_task(), tmp_path, plan_approval_note=note)
    assert "\n\n" + "a" * NOTE_LIMIT + "\n\n" + NOTE_MARKER in prompt
    assert "UNIQUE TAIL" not in prompt
    assert "[Plan cut" not in prompt


def test_owner_note_truncation_is_utf8_safe(make_task, tmp_path):
    note = "a" * (NOTE_LIMIT - 1) + "é" + "TAIL"
    prompt = _prompt(make_task(), tmp_path, plan_approval_note=note)
    assert "a" * (NOTE_LIMIT - 1) + "\n\n" + NOTE_MARKER in prompt
    assert "�" not in prompt and "é" not in prompt and "TAIL" not in prompt


async def test_resumed_review_reads_note_from_ledger(make_task, make_run, ledger, data_dir, config):
    plan = "# Plan\n\n1. Create done.txt.\n" + "Detailed plan line.\n" * 700
    note = "  Owner's revised contract: café\nUse the new acceptance rule.  \n"
    adapter = ScriptedPiAdapter(
        {
            "plan": PiScript(final_text=plan),
            "implement": PiScript(files=[("done.txt", "")]),
            "review": PiScript(
                final_text='```json\n{"verdict": "pass", "findings": [], "evidence_checks": []}\n```'
            ),
        }
    )
    first, _ = make_run(make_task(policy="approve-plan-first"), adapter)
    paused = await first.run()
    assert paused.pending_approval == "plan"
    ledger.decide_approval(int(ledger.pending_approval(first.run_id)["id"]), "approved", note)
    # Intentionally do not append the note to plan.md: the ledger is authoritative.
    resumed = GovernedRun.resume(
        first.run_id,
        config=config,
        ledger=ledger,
        adapter_factory=lambda state, isolation: adapter,
        pi_version="0.87.1",
        data_dir=data_dir,
        auth_check=lambda model: None,
    )
    outcome = await resumed.run()

    assert outcome.status == "awaiting_approval" and outcome.pending_approval == "merge"
    (review,) = adapter.calls_for("review")
    assert note in review.prompt.split("## Task", 1)[0]
    assert "## Owner's decisions on approval" in review.prompt
    assert "[Plan cut after 8192 bytes.]" in review.prompt
    assert (data_dir / "runs" / first.run_id / "plan.md").read_text() == plan


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        ([], None),
        ([("plan", "approved", "  café\n ")], "  café\n "),
        (
            [
                ("plan", "pending", "pending"),
                ("plan", "rejected", "rejected"),
                ("merge", "approved", "merge"),
            ],
            None,
        ),
        (
            [
                ("plan", "approved", "owner"),
                ("plan", "pending", "pending"),
                ("plan", "rejected", "rejected"),
                ("merge", "approved", "merge"),
            ],
            "owner",
        ),
        ([("plan", "approved", "old"), ("plan", "approved", "new")], "new"),
        ([("plan", "approved", "old"), ("plan", "approved", None)], None),
        ([("plan", "approved", "old"), ("plan", "approved", "")], None),
        ([("plan", "approved", " \n ")], " \n "),
    ],
    ids=[
        "none",
        "approved",
        "ignored",
        "unrelated-newer",
        "latest",
        "latest-null",
        "latest-empty",
        "whitespace",
    ],
)
async def test_plan_approval_note_filters_approval_rows(
    make_task, make_run, ledger, history, expected
):
    adapter = ScriptedPiAdapter({"plan": PiScript(final_text="# Plan")})
    run, _ = make_run(make_task(policy="approve-plan-first"), adapter)
    assert (await run.run()).pending_approval == "plan"
    for kind, decision, note in history:
        ledger.record_approval(run.run_id, kind, decision, note)
    assert run._plan_approval_note() == expected
