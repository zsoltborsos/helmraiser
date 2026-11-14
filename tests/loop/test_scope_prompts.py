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
from helmraiser.loop.review import build_review_prompt


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


def test_plan_prompt_without_paths_keeps_blocked_and_open_questions_guidance(make_task):
    straight = plan_prompt(make_task(), wait_for_approval=False)
    assert "The implementer starts from the plan straight away." in straight
    assert "Resolve open questions yourself" in straight
    assert "answer `BLOCKED:` with the reason only for blockers." in straight
    first = plan_prompt(make_task(), wait_for_approval=True)
    assert "Helmraiser saves it and waits for approval." in first
    assert "Include any open questions for the owner." in first
