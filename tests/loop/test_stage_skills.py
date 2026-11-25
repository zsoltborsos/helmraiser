"""Per-stage worker skill folders reach every worker pi session (``pi/skills.py``)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from helmraiser.ledger.db import Ledger
from helmraiser.loop.scout import execute_scout
from helmraiser.pi import skills
from helmraiser.pi.adapter import PiJsonAdapter
from tests.cli.test_scout_requests import config_for
from tests.loop.test_evidence_review_merge import verdict_text
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter


def skill(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: test\n---\n")
    return path


def commit_project_skill(project: Path) -> Path:
    """A committed ``.agents/skills/own`` skill, so run worktrees carry it too."""
    own = skill(project / ".agents" / "skills", "own")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "project skill"], cwd=project, check=True, capture_output=True
    )
    return own.relative_to(project)


def flags(*dirs: Path) -> tuple[str, ...]:
    return ("--no-skills", *(arg for d in dirs for arg in ("--skill", str(d))))


@pytest.mark.parametrize("folders", [True, False])
async def test_run_stages_get_their_stage_skills(make_task, make_run, tmp_project, folders):
    own = commit_project_skill(tmp_project)
    if folders:
        plan_skill = skill(skills.SKILLS_ROOT / "plan", "planning")
        implement_skill = skill(skills.SKILLS_ROOT / "implement", "tdd")
        (skills.SKILLS_ROOT / "review").mkdir()
    task = make_task(plan=True, max_iterations=2)
    implement = [PiScript(files=[("x", "1")]), PiScript(files=[("x", "2")])]
    adapter = ScriptedPiAdapter(
        {
            "plan": PiScript(final_text="1. Write done.txt."),
            "implement": [*implement, PiScript(files=[("done.txt", "ok\n")])],
            "review": PiScript(final_text=verdict_text("pass")),
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval", outcome.failure_reason
    assert outcome.escalated
    stages = [(c.stage, c.attempt) for c in adapter.calls]
    assert stages == [("plan", 1), ("implement", 1), ("implement", 1)] + [
        ("implement", 2),
        ("review", 1),
    ]
    worktree = adapter.calls[0].cwd
    if not folders:
        assert all(c.extra_args == () for c in adapter.calls)
        return
    expected = {
        "plan": flags(plan_skill, worktree / own),
        "implement": flags(implement_skill, worktree / own),  # the fallback too
        "review": flags(worktree / own),
    }
    for call in adapter.calls:
        assert call.extra_args == expected[call.stage], call.stage
    argv = PiJsonAdapter().build_argv(adapter.calls[-1])
    assert argv.index("--no-skills") < argv.index("--skill") < argv.index("--")


@pytest.mark.parametrize("folder", [True, False])
def test_scout_session_gets_scout_skills(tmp_project, tmp_path, monkeypatch, folder):
    own = commit_project_skill(tmp_project)
    if folder:
        scout_skill = skill(skills.SKILLS_ROOT / "scout", "codebase-design")
    monkeypatch.setattr("helmraiser.loop.scout._leeboard_brief", lambda repo: None)
    adapter = ScriptedPiAdapter({"scout": PiScript(final_text=json.dumps([]))})

    def intake(spec, *, feedback=None):  # type: ignore[no-untyped-def]
        raise AssertionError("no candidates, no intake")

    outcome = execute_scout(
        tmp_project,
        Ledger(tmp_path / "ledger.db"),
        config_for(tmp_project),
        tmp_path / "data",
        adapter,
        intake,
        force=True,
    )

    assert outcome.outcome == "succeeded", outcome.error
    (call,) = adapter.calls
    assert call.stage == "scout"
    assert call.extra_args == (flags(scout_skill, call.cwd / own) if folder else ())
