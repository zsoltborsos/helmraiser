"""Scope appears before task content in every task-bearing prompt."""

from dataclasses import replace
from pathlib import Path

import pytest

from helmraiser.loop.prompts import (
    escalation_prompt,
    initial_prompt,
    plan_prompt,
    scope_declined_prompt,
)
from helmraiser.loop.review import OWNER_NOTE_INLINE_LIMIT, ScopeDecision, build_review_prompt


def test_scope_declined_prompt():
    text = scope_declined_prompt(["tests/old_test.txt", "docs/notes.md"], "Keep the behaviour")
    assert text.startswith(
        "Scope request declined (tests/old_test.txt, docs/notes.md): Keep the behaviour\n"
    )
    assert "The task's paths: is unchanged" in text
    assert "Do not edit the declined files" in text
    assert "inside the allowed paths" in text
    assert "SCOPE: or BLOCKED:" in text
    assert "Read the task file again because it may have changed" in text
    assert "`" not in text


def prompts(task):
    return [
        initial_prompt(task, "Our plan"),
        plan_prompt(task, wait_for_approval=False),
        escalation_prompt(task, [], "base-sha"),
        build_review_prompt(
            task,
            diff_path=Path("diff"),
            diff_text="diff",
            verify_results=[],
            evidence_results=[],
            evidence_dir=Path("evidence"),
            plan="Our plan",
        ),
    ]


def test_scope_prompts(make_task):
    task = make_task()
    baseline = prompts(task)
    assert baseline == prompts(replace(task, paths=None))
    assert all("Allowed paths" not in p for p in baseline)
    scoped = prompts(replace(task, paths=("src/**/*.py", "docs/")))
    for text in scoped:
        for pattern in ("src/**/*.py", "docs/"):
            assert text.index(pattern) < text.index(task.body.strip())
        assert "Acceptance" in text
    assert scoped[0].endswith("Begin.")
    assert "Our plan" in scoped[0] and "Our plan" in scoped[3]
    assert "no iteration history" in scoped[2]
    assert "Check the diff against the allowed paths" in scoped[3]


def test_plan_scope_prompt_requests_paths_before_full_plan(make_task):
    text = plan_prompt(make_task(paths=("done.txt",)), wait_for_approval=False)
    assert "The implementer starts from the plan straight away" in text
    assert "outside the allowed paths, start the final message with one line" in text
    assert "`SCOPE: <path>[, <path>…] — <reason>`" in text
    assert "followed by the full plan, written as if the paths were granted" in text
    assert "Resolve other open questions yourself" in text
    assert "only for other blockers" in text


def test_plan_scope_prompt_approval_first_uses_open_questions(make_task):
    text = plan_prompt(make_task(paths=("done.txt",)), wait_for_approval=True)
    assert "waits for approval" in text
    assert "list needed files outside the allowed paths under the plan's open questions" in text
    assert "SCOPE:" not in text


@pytest.mark.parametrize("wait_for_approval", [False, True])
def test_plan_prompt_without_paths_has_no_scope_instruction(make_task, wait_for_approval):
    text = plan_prompt(make_task(), wait_for_approval=wait_for_approval)
    assert "SCOPE" not in text
    assert "allowed paths" not in text.lower()
    assert text.endswith("Do not implement anything.")


def review_prompt(task, tmp_path, **options):
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


@pytest.mark.parametrize("decision", ["approved", "declined"])
def test_review_scope_decision_paths_and_note(make_task, tmp_path, decision):
    note = "  Owner decision: café\nKeep this whitespace.  \n"
    text = review_prompt(
        make_task(),
        tmp_path,
        scope_decisions=[ScopeDecision(decision, ("tests/old_test.txt", "docs/notes.md"), note)],
    )
    section = text.split("## Task", 1)[0]
    assert "## Owner's scope decisions" in section
    assert f"### Decision 1: {decision.capitalize()}" in section
    assert "Paths: `tests/old_test.txt`, `docs/notes.md`" in section
    assert note in section
    assert "override the task brief and acceptance block where they conflict" in section
    assert "do not report following them as a finding" in section


def test_review_scope_decisions_keep_input_order(make_task, tmp_path):
    text = review_prompt(
        make_task(),
        tmp_path,
        scope_decisions=[
            ScopeDecision("declined", ("old.txt",), "First decision"),
            ScopeDecision("approved", ("new.txt",), "Second decision"),
        ],
    )
    assert text.index("### Decision 1: Declined") < text.index("First decision")
    assert text.index("First decision") < text.index("### Decision 2: Approved")
    assert text.index("### Decision 2: Approved") < text.index("Second decision")
    assert text.index("Second decision") < text.index("## Task")


def test_review_scope_decision_without_note(make_task, tmp_path):
    text = review_prompt(
        make_task(), tmp_path, scope_decisions=[ScopeDecision("approved", ("extra.txt",), None)]
    )
    assert "### Decision 1: Approved\n\nPaths: `extra.txt`" in text
    assert "None" not in text


def test_review_scope_decision_without_paths(make_task, tmp_path):
    text = review_prompt(
        make_task(), tmp_path, scope_decisions=[ScopeDecision("approved", (), "Historical note")]
    )
    assert "### Decision 1: Approved\n\nPaths unavailable in the ledger" in text
    assert "Historical note" in text


def test_review_without_scope_decisions_is_unchanged(make_task, tmp_path):
    task = make_task()
    original = review_prompt(task, tmp_path)
    assert original == review_prompt(task, tmp_path, scope_decisions=())
    assert "## Owner's scope decisions" not in original


def test_review_scope_note_at_limit(make_task, tmp_path):
    note = "a" * OWNER_NOTE_INLINE_LIMIT
    text = review_prompt(
        make_task(), tmp_path, scope_decisions=[ScopeDecision("approved", ("extra.txt",), note)]
    )
    assert note in text
    assert "[Scope decision note cut" not in text


@pytest.mark.parametrize("multibyte", [False, True])
def test_review_scope_note_cut_is_utf8_safe(make_task, tmp_path, multibyte):
    prefix = "a" * (OWNER_NOTE_INLINE_LIMIT - (1 if multibyte else 0))
    note = prefix + ("é" if multibyte else "") + "UNIQUE TAIL"
    text = review_prompt(
        make_task(), tmp_path, scope_decisions=[ScopeDecision("approved", ("extra.txt",), note)]
    )
    marker = f"[Scope decision note cut after {OWNER_NOTE_INLINE_LIMIT} bytes.]"
    assert prefix + "\n\n" + marker in text
    assert "UNIQUE TAIL" not in text
    assert "é" not in text and "�" not in text


def test_review_scope_notes_have_independent_limits(make_task, tmp_path):
    text = review_prompt(
        make_task(),
        tmp_path,
        scope_decisions=[
            ScopeDecision("declined", ("old.txt",), "a" * OWNER_NOTE_INLINE_LIMIT + "FIRST TAIL"),
            ScopeDecision("approved", ("new.txt",), "b" * OWNER_NOTE_INLINE_LIMIT + "SECOND TAIL"),
        ],
    )
    assert "a" * OWNER_NOTE_INLINE_LIMIT in text
    assert "b" * OWNER_NOTE_INLINE_LIMIT in text
    assert text.count(f"[Scope decision note cut after {OWNER_NOTE_INLINE_LIMIT} bytes.]") == 2
    assert "FIRST TAIL" not in text and "SECOND TAIL" not in text


def test_plan_prompt_without_paths_keeps_blocked_and_open_questions_guidance(make_task):
    straight = plan_prompt(make_task(), wait_for_approval=False)
    assert "The implementer starts from the plan straight away." in straight
    assert "Resolve open questions yourself" in straight
    assert "answer `BLOCKED:` with the reason only for blockers." in straight
    first = plan_prompt(make_task(), wait_for_approval=True)
    assert "Helmraiser saves it and waits for approval." in first
    assert "Include any open questions for the owner." in first
