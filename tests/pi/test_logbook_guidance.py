"""Implementation-only Logbook writing guidance and frozen baseline directives."""

import hashlib
import re
from pathlib import Path

import pytest

from helmraiser.logbook import BriefResult
from helmraiser.pi.directives import (
    render_implement_directives,
    render_plan_directives,
    render_review_directives,
    render_scout_directives,
)


def implement(**kwargs):
    return render_implement_directives(
        run_id="r1",
        title="T",
        worktree=Path("/w"),
        task_path=Path("/t.md"),
        task_id="t",
        scout_model="p/m",
        max_iterations=2,
        max_minutes=30,
        max_cost_usd=1.0,
        iteration=1,
        **kwargs,
    )


def _baseline_outputs():
    common = {"run_id": "r1", "title": "T", "worktree": Path("/w")}
    return {
        "implement": implement(),
        "plan": render_plan_directives(
            **common, task_id="t", wait_for_approval=False, paths=("src/",)
        ),
        "plan_approval": render_plan_directives(
            **common, task_id="t", wait_for_approval=True, paths=("src/",)
        ),
        "review": render_review_directives(**common),
        "review_context": render_review_directives(
            **common, project_notes="Use local tests.", paths=("src/", "tests/")
        ),
        "scout": render_scout_directives(
            scout_id="s1", project=Path("/p"), worktree=Path("/w"), exclusions=()
        ),
        "scout_exclusions": render_scout_directives(
            scout_id="s1", project=Path("/p"), worktree=Path("/w"), exclusions=("docs/", "vendor/")
        ),
    }


EXPECTED_SHA256 = {
    "implement": "9cee10171a94a6a7a7ab9e6027c3ce979f0a4b673306bf356a9d1e633e4d53a3",
    "plan": "8a0d9e08c95beea209492630def8e662441627f911f927ba0acf85abf8758210",
    "plan_approval": "c269f2a7c0a83734eaa25cec0e66a8c9fa8f32cbbcfda9a0f2395385e21b401a",
    "review": "b630dce1903d04c0e6448e10b79ac05c14f7d9d562b34bd3c838007abe5798a1",
    "review_context": "82628690fdb0df1e278c8bdd6a5182df861e2a0a8bc936b8f674da3c67136701",
    "scout": "0db1b6dbde4aa379c665de4d36ca34381d0757a204c97817f886d9de8fd65ad2",
    "scout_exclusions": "ffd54090efbc49778def7a18b44d05f7c3ef616d0709946538235d192be15560",
}


def test_logbook_baseline_bytes_unchanged():
    assert {
        name: hashlib.sha256(text.encode("utf-8")).hexdigest()
        for name, text in _baseline_outputs().items()
    } == EXPECTED_SHA256


@pytest.mark.parametrize("brief_text", ["# Logbook: project demo\nKeep {braces}", ""])
def test_logbook_available_guidance(brief_text):
    text = implement(logbook_brief=BriefResult(brief_text, None))
    assert "## Logbook" in text
    assert text.count("Writing to Logbook") == 1
    if brief_text:
        assert text.index(brief_text) < text.index("Writing to Logbook")
        assert text.count(brief_text) == 1
    assert "lb handoff" not in text
    for phrase in (
        "rare; default write nothing",
        "change action",
        "unavailable from repo/tools when needed",
        "require author/end condition",
        "Progress, code facts, unfixed defects: final message",
        'lb request --to <target> --title "..."',
        "open agent/owner question; keep working",
        "project:<name> (brief header), session:<id>, harness:<name>, any",
        'lb add rule --title "..." --body "..."',
        "lasting do/don't learned the hard way, absent from repo",
        "owner/interactive only",
        "Exit 6: no retry/bypass",
        "No credentials in title/body",
        "Run entries: `helmraiser report`",
    ):
        assert phrase in text


@pytest.mark.parametrize(
    "brief", [None, BriefResult(None, "lb not found on PATH"), BriefResult(None, None)]
)
def test_logbook_unavailable_no_guidance(brief):
    text = implement(logbook_brief=brief, logbook_handoff_to="session:abc-123")
    assert "Writing to Logbook" not in text
    assert "lb handoff" not in text
    assert "lb request" not in text
    assert "lb add rule" not in text


def test_logbook_handoff_guidance():
    text = implement(logbook_brief=BriefResult("brief", None), logbook_handoff_to="session:abc-123")
    assert 'lb handoff --to session:abc-123 --title "..." --body "..."' in text
    assert "review needs beyond diff/final message" in text


@pytest.mark.parametrize("target", [None, "session:abc-123", "session:" + "a" * 128])
def test_logbook_guidance_commands_and_bytes(target):
    text = implement(logbook_brief=BriefResult("brief", None), logbook_handoff_to=target)
    # Include the separator and final newline in the appended note's byte budget.
    note = text[text.index("\nWriting to Logbook") :]
    assert len(note.encode("utf-8")) <= 902
    commands = re.findall(r"\blb\s+(add\s+\w+|\w+)", note)
    expected = {
        "request",
        "add rule",
        "release",
        "revoke",
        "supersede",
        "hold",
        "grant",
        "ack",
        "close",
        "reply",
    }
    if target is not None:
        expected.add("handoff")
    assert set(commands) == expected
    assert len(commands) == len(expected)
