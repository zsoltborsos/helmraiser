"""Stage directives (spec §3.6, §4.5.2, §4.6)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from helmraiser.cli.commands import dry_run_plan
from helmraiser.isolation.none import NoIsolation
from helmraiser.ledger.db import Ledger
from helmraiser.loop.runner import GovernedRun
from helmraiser.pi.directives import (
    render_implement_directives,
    render_plan_directives,
    render_review_directives,
)
from helmraiser.task.loader import parse_task
from helmraiser.task.model import ModelSpec
from tests.mocks.scripted_pi_adapter import ScriptedPiAdapter

WORKTREE = Path("/srv/projects/example/.helmraiser/worktrees/rm-heartbeat-injection")
TASK_PATH = Path("/home/user/.helmraiser/runs/3f2c1a9e-6b7d-4c1e-9a2b-1f0e8d7c6b5a/task.md")


def _implement(
    iteration: int = 1,
    service_vars: tuple[str, ...] = (),
    *,
    has_plan_approval_note: bool = False,
    project_notes: str | None = None,
    paths: tuple[str, ...] | None = None,
) -> str:
    return render_implement_directives(
        run_id="3f2c1a9e-6b7d-4c1e-9a2b-1f0e8d7c6b5a",
        title="Remove heartbeat prompt injection from AgentRunner",
        worktree=WORKTREE,
        task_path=TASK_PATH,
        task_id="rm-heartbeat-injection",
        scout_model="opencode-go/deepseek-v4.1-flash:high",
        max_iterations=4,
        max_minutes=90,
        max_cost_usd=5.0,
        iteration=iteration,
        service_vars=service_vars,
        has_plan_approval_note=has_plan_approval_note,
        paths=paths,
        project_notes=project_notes,
    )


@pytest.mark.parametrize("materialise", [False, True], ids=["preview", "governed"])
@pytest.mark.parametrize(
    "config, task_scout, expected",
    [
        (
            {"profiles": {"scout": {"model": "custom/survey", "thinking": "low"}}},
            None,
            "custom/survey:low",
        ),
        ({"profiles": {"scout": "custom/survey:low"}}, None, "custom/survey:low"),
        ({}, None, None),
        (
            {"profiles": {"scout": "custom/survey:low"}},
            "task/survey:high",
            "task/survey:high",
        ),
    ],
)
def test_scout_profile_fallback(
    tmp_project: Path, tmp_path: Path, materialise: bool, config: dict, task_scout, expected: str
):
    task = parse_task(
        f"---\nid: demo\ntitle: Demo\nproject: {tmp_project}\n"
        "models:\n  implement: p/impl\n  review: p/review\n---\nDo it.\n",
        tmp_project / "task.md",
        config,
    )
    models = dict(task.models)
    models.pop("scout", None)
    if task_scout is not None:
        models["scout"] = ModelSpec.parse(task_scout)
    task = replace(task, models=models)
    data_dir = tmp_path / "data"
    if materialise:
        ledger = Ledger(data_dir / "helmraiser.db")
        adapter = ScriptedPiAdapter({})
        run = GovernedRun(
            task,
            config=config,
            ledger=ledger,
            data_dir=data_dir,
            adapter_factory=lambda state, isolation: adapter,
            pi_version="test-version",
            isolation=NoIsolation(),
        )
        run._materialise()
        text = run.rundir.directives_md.read_text()
    else:
        text = dry_run_plan(task, config, data_dir, NoIsolation())["directives"]
    if expected is None:
        assert "--model" not in text
        assert "Explore before editing" in text
    else:
        assert f"--model {expected}" in text


def test_conditional_path_constraints():
    patterns = ("src/**/*.py", "docs/")
    assert _implement() == _implement(paths=None)
    text = _implement(paths=patterns)
    assert all(p in text for p in patterns)
    assert text.index("Allowed paths:") < text.index("Working method")
    assert "fail before Helmraiser runs verify" in text
    assert "do not broaden the declaration" in text
    assert "commit your in-scope work" in text
    assert "`SCOPE: <path>[, <path>…] — <reason>`" in text
    assert "SCOPE:" not in _implement()
    # The scope section stays short: it is outside the template's 1.5 KB budget.
    assert len(text.encode()) - len(_implement().encode()) < 400
    args = {"run_id": "r1", "title": "T", "worktree": WORKTREE}
    assert render_review_directives(**args) == render_review_directives(**args, paths=None)
    text = render_review_directives(**args, paths=patterns)
    assert all(p in text for p in patterns)
    assert "Check the diff against these allowed paths" in text


class TestImplementDirectives:
    def test_under_1_5_kb_with_realistic_values(self):
        assert len(_implement().encode("utf-8")) < 1536

    def test_substitutes_every_field(self):
        text = _implement(iteration=3)
        assert (
            "# Helmraiser run 3f2c1a9e-6b7d-4c1e-9a2b-1f0e8d7c6b5a — governed implementation"
            in text
        )
        assert 'task "Remove heartbeat prompt injection from AgentRunner"' in text
        assert str(WORKTREE) in text
        assert f"Read the task file first: {TASK_PATH}." in text
        assert "--model opencode-go/deepseek-v4.1-flash:high" in text
        assert ".helmraiser/context" not in text
        assert "plan.md" not in text
        assert "Budget: 4 verify iterations, 90 minutes, $5.00." in text
        assert "This is iteration 3. Iteration 4 is the last one." in text
        assert "{" not in text and "}" not in text

    def test_owner_approval_note_precedence_is_conditional(self):
        sentence = (
            "- The owner's note on approval in the approved plan overrides the task brief "
            "and acceptance block.\n"
        )
        default = _implement()
        assert _implement(has_plan_approval_note=False) == default
        enabled = _implement(has_plan_approval_note=True)
        assert sentence in enabled
        assert enabled.replace(sentence, "") == default
        assert len(enabled.encode()) - len(default.encode()) < 200

    def test_owner_note_keeps_services_and_project_notes(self):
        options = {"service_vars": ("DATABASE_URL",), "project_notes": "Use local tests."}
        enabled = _implement(has_plan_approval_note=True, **options)
        assert "overrides the task brief and acceptance block" in enabled
        assert "Services: only the verify and evidence steps get DATABASE_URL" in enabled
        assert "## Project notes (Helmraiser)" in enabled and "Use local tests." in enabled
        assert enabled.index("owner's note") < enabled.index("Services:")
        sentence = (
            "- The owner's note on approval in the approved plan overrides the task brief "
            "and acceptance block.\n"
        )
        assert enabled.replace(sentence, "") == _implement(**options)

    def test_rules(self):
        text = _implement()
        assert "Do not run git push" in text
        assert "`BLOCKED:`" in text
        assert "Run them yourself before you stop" in text

    def test_shell_environment_limits_are_not_blockers(self):
        text = " ".join(_implement().split())
        assert "Your shell is stricter than Helmraiser's verify sandbox." in text
        assert "environment limits (denied socket, port, process or path), not your change" in text
        assert "commit and stop without `BLOCKED:`" in text
        assert "Helmraiser runs verify." in text
        assert "Answer `BLOCKED:` only for task or code problems." in text

    def test_out_of_memory_rule(self):
        text = _implement()
        assert "heap out of memory" in text
        assert "single suspect test" in text
        assert "NODE_OPTIONS=--max-old-space-size=1024" in text
        flat = text.replace("\n  ", " ")
        assert "do not re-run the whole suite or file" in flat

    def test_no_services_note_without_services(self):
        assert "Services:" not in _implement()
        assert "container" not in _implement()

    def test_services_note_names_the_variables_and_who_gets_them(self):
        text = _implement(service_vars=("DATABASE_URL", "REDIS_URL"))
        flat = " ".join(text.split())
        assert (
            "- Services: only the verify and evidence steps get DATABASE_URL, REDIS_URL, for "
            "containers Helmraiser runs; your own commands do not. Never start containers yourself."
        ) in flat
        assert flat.index("Run them yourself") < flat.index("Services:")
        # The note is outside the template's 1.5 KB budget but stays short.
        assert len(text.encode()) - len(_implement().encode()) < 280


class TestReviewDirectives:
    def test_read_only_and_json_contract(self):
        text = render_review_directives(run_id="r1", title="T", worktree=WORKTREE)
        assert "read-only" in text
        assert "Never modify files." in text
        assert '"verdict": "pass" | "concerns" | "uncertain"' in text
        assert '"findings": [{"severity": "severe"|"minor"' in text
        assert '"evidence_checks": [{"name": "...", "meets_criteria": true|false|null' in text
        assert "LAST thing in your message" in text
        assert '"pass" only if no severe findings' in text
        assert "run r1" in text and 'task "T"' in text
        assert len(text.encode()) < 1536


class TestPlanDirectives:
    def test_plan_file_and_no_implementation(self):
        text = render_plan_directives(
            run_id="r1",
            title="T",
            worktree=WORKTREE,
            task_id="abc",
            wait_for_approval=True,
        )
        assert "the run's plan file" in text
        assert ".helmraiser/context" not in text
        assert "commits it" not in text
        assert "Do not implement." in text
        assert "waits for approval" in text
        assert str(WORKTREE) in text

    def test_plan_scope_directives_request_full_plan(self):
        text = render_plan_directives(
            run_id="r1",
            title="T",
            worktree=WORKTREE,
            task_id="abc",
            wait_for_approval=False,
            paths=("done.txt",),
        )
        assert "outside the allowed paths, start the final message with one line" in text
        assert "`SCOPE: <path>[, <path>…] — <reason>`" in text
        assert "followed by the full plan, written as if the paths were granted" in text
        assert "Resolve other open questions yourself" in text
        assert "only for other blockers" in text
        assert len(text.encode()) < 1536

    def test_plan_scope_directives_approval_first_uses_open_questions(self):
        text = render_plan_directives(
            run_id="r1",
            title="T",
            worktree=WORKTREE,
            task_id="abc",
            wait_for_approval=True,
            paths=("done.txt",),
        )
        assert "waits for approval" in text
        assert "List needed files outside the allowed paths under the plan's open questions" in text
        assert "SCOPE:" not in text
        assert len(text.encode()) < 1536


class TestPlanDirectivesWithoutPaths:
    """A task without ``paths:`` has no allowed paths and no scope request to make."""

    @pytest.mark.parametrize("wait_for_approval", [False, True])
    def test_no_scope_instruction_or_allowed_paths(self, wait_for_approval):
        text = render_plan_directives(
            run_id="r1",
            title="T",
            worktree=WORKTREE,
            task_id="abc",
            wait_for_approval=wait_for_approval,
        )
        assert "SCOPE" not in text
        assert "allowed paths" not in text.lower()
        assert len(text.encode()) < 1536

    def test_plan_first_keeps_open_questions_for_the_owner(self):
        text = render_plan_directives(
            run_id="r1", title="T", worktree=WORKTREE, task_id="abc", wait_for_approval=True
        )
        assert "Helmraiser waits for approval from the owner." in text
        assert "List questions for the owner under the plan's open questions." in text

    def test_straight_to_implementation_keeps_blocked_guidance(self):
        text = render_plan_directives(
            run_id="r1", title="T", worktree=WORKTREE, task_id="abc", wait_for_approval=False
        )
        assert "The implementer starts from the plan straight away." in text
        assert "Resolve open questions yourself" in text
        assert "answer `BLOCKED:` only for blockers" in text


def test_project_notes_are_in_implement_plan_and_review_directives():
    notes = "Use the sandbox-safe test command."
    implement = render_implement_directives(
        run_id="r1",
        title="T",
        worktree=WORKTREE,
        task_path=TASK_PATH,
        task_id="t",
        scout_model="provider/model",
        max_iterations=2,
        max_minutes=30,
        max_cost_usd=1.0,
        iteration=1,
        project_notes=notes,
    )
    plan = render_plan_directives(
        run_id="r1",
        title="T",
        worktree=WORKTREE,
        task_id="t",
        wait_for_approval=False,
        project_notes=notes,
    )
    review = render_review_directives(
        run_id="r1", title="T", worktree=WORKTREE, project_notes=notes
    )

    for text in (implement, plan, review):
        assert text.count("## Project notes (Helmraiser)") == 1
        assert text.count(notes) == 1


def test_directives_without_project_notes_have_no_notes_section():
    implement = render_implement_directives(
        run_id="r1",
        title="T",
        worktree=WORKTREE,
        task_path=TASK_PATH,
        task_id="t",
        scout_model="provider/model",
        max_iterations=2,
        max_minutes=30,
        max_cost_usd=1.0,
        iteration=1,
    )
    plan = render_plan_directives(
        run_id="r1", title="T", worktree=WORKTREE, task_id="t", wait_for_approval=False
    )
    review = render_review_directives(run_id="r1", title="T", worktree=WORKTREE)

    for text in (implement, plan, review):
        assert "Project notes" not in text


def test_implement_directive_baseline_keeps_other_repos_unavailable():
    text = _implement()
    assert "Other repos are unavailable." in text
    assert "skip brief and recall steps" not in text
