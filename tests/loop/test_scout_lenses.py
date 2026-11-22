import pytest

from helmraiser.loop.scout import build_scout_prompt
from helmraiser.loop.scout_lenses import lens_text
from helmraiser.pi.directives import render_scout_directives

BASE = '# Helmraiser scout id — read-only task discovery\n\nScout project repo in the detached worktree at tree. This is strictly\nread-only: never modify, create, or delete files. Propose only code or documentation\nwork that can be verified by local commands. Do not propose host, cloud, provider,\ndeployment, credential, or external-service work. Do not reopen recorded owner\ndecisions. Do not propose work touching these excluded path prefixes: (none).\n\nReturn only one strict JSON list, with no Markdown fence or prose. Every object must\nhave exactly these keys: "id", "title", "why", "evidence", "files", "change",\n"verify_hints", "size", "needs_owner_ruling", and "ruling_question". Evidence is\na non-empty list of relative "file:line" strings; files is a non-empty list of\nrelative paths; size is "S", "M", or "L". Set needs_owner_ruling true and provide\na non-empty ruling_question whenever the work requires an owner decision.\n\nverify_hints is a list of non-empty strings or objects. Each object requires a\nnon-empty string command (run is a synonym); optional keys are timeout (positive\ninteger), red_on_base (boolean), red_output (non-empty string), and purpose (string).\nUnknown hint-object keys are ignored. For a probe that must fail before the change,\nset red_on_base: true and red_output to a non-empty literal failure marker.\n\n- Keep each evidence entry exactly "file:line"; name the symbol at that line in "why" (for example "DiaryExcerpt in src/api.py"), so the finding survives small line shifts.\n- Make "change" name every edit the verify hints check, and nothing the code contradicts; read the code before stating how it behaves.\n- A candidate that only changes tests never asks to edit production code; prove a test can fail with a mutation check outside the worktree instead.\n- Put findings that touch the same file into one candidate.\n- When a candidate removes information from logs, errors or other output, its "change" also says what must remain (for example fixed labels, counts, durations or class names).\n'
TEXTS = {
    "docs": 'Find documentation that disagrees with the code. Read the project\'s user-facing and\nagent-facing documents (README, docs/, AGENTS.md, CLAUDE.md, CURRENT_STATE.md, TASKS.md,\nskill files) and the help strings of its commands. Check each concrete claim, such as a\ncommand, flag, option, default, file path, config key, limit, behaviour or status, against\nthe code. Report a claim only when the code contradicts it, or when a document describes a\nbehaviour but omits a part of it that would make a reader act wrongly.\n\nEvidence must always pair the document line with the code line that contradicts it, for\nexample "README.md:40" and "src/cli.py:112". Do not report style, tone, wording\npreferences, or missing documentation for internal code.\n\nIn "change", give the corrected text, describing only what the code does. If the code\nlooks wrong rather than the document, set needs_owner_ruling and ask which one is\nintended. Group the findings for one document area into one candidate, size S or M.\nVerify hints: the project\'s existing documentation checks (link checker, doc tests,\nmarkdown lint) if it has them, and a check that the stale text is gone, such as\n"! grep -n \'old-flag\' README.md".\n',
    "tests": 'Find tests that give no real assurance. Look for:\n1. Tests that cannot fail: they would still pass if the behaviour they name were removed\n   or reverted. For example, they assert what a mock was told to return, assert on their\n   own input, assert only a type or "not None", or catch and discard the failure.\n2. Duplicates: two tests that prove the same behaviour through the same interface, with no\n   different input or branch.\n3. Implementation pinning: tests that assert private helpers, call order or internal\n   structure, where a test of the behaviour exists or should exist instead.\n4. Snapshots that capture incidental output rather than the behaviour.\n5. Tests whose name promises a behaviour their body never exercises.\nLook first at tests near recently changed code (recent_commits). Do not flag a test for\nstyle alone.\n\nEvidence: the test\'s file:line and the file:line of the production behaviour it should\nprove; for a duplicate, both tests.\n\nIn "change", prefer strengthening the test so it fails when the behaviour is reverted.\nMerge duplicates. Delete a test only when another named test already proves the same\nbehaviour. Never weaken an assertion. Require each strengthened or remaining test to be proved able to fail, by a check rather\nthan a report. In "verify_hints", propose one mutation check per test: a command that\nbreaks the named behaviour without changing any file in the worktree (for example a short\nscript that patches it with a mock, or that runs the test against a modified copy of the\nsource outside the worktree), runs only that test, and exits 0 only when the test fails.\nSay that the check is red_on_base: on the unchanged code the weak test survives the\nmutation, so the check fails until the test is strengthened. When the named test survives\nits mutation, print the fixed marker `MUTANT SURVIVED: <test name>` before exiting nonzero.\nName that exact marker as the step\'s red_output in the hint. Never print the survival\nmarker for runner-loading errors or other infrastructure failures.\n\nVerify hints: the mutation checks; the changed test files; the project\'s full test suite\nand its coverage gate, if it has one, as evidence. Coverage must not drop.\n',
    "guardrails": """Find project rules that nothing enforces, and checks that cannot catch what they guard.
Read the rules the project states for itself: AGENTS.md, CLAUDE.md, CONTRIBUTING, docs on
development or testing, .helmraiser/notes.md, and the header comments of guard scripts and
guard tests. Then read the checks: test files that guard a rule, lint and type-check
configuration, CI workflow files, pre-commit or git hooks, and scripts the CI runs.
Look for:
1. A guard that cannot fail on the violation it names. For example, it scans the wrong
   paths or file types, it is left out of CI or the default test run, it ignores its own
   errors ("|| true", continue-on-error, a broad except, exit 0 when its input is missing),
   it passes when it finds nothing to check, or it compares against a moving branch.
2. A rule that a machine could check (a forbidden import, call, dependency, pattern or file
   location, or a required header, marker or test), stated in the project's own documents,
   that no existing check enforces.
3. A guard whose condition differs from the rule it cites.
Report only rules the project states itself; never invent policy. Do not report style
preferences, broad wishes such as "more tests", or rules no command could check.

Evidence: the rule's file:line and the guard's file:line. For an unenforced rule, give the
rule's file:line and the file:line of the check that should enforce it, or of the closest
existing guard.

In "change", fix the guard, or add the smallest check in the style the project already
uses (an existing guard test file, the lint configuration or a CI step). Say what the
check must still allow. If the code already breaks the rule, list each place, and set
needs_owner_ruling unless the rule plainly decides the fix. Describe each gap as a missing
check, in neutral terms; never describe how to get around a guard.

Verify hints: the guard itself, green after the change; one probe per guard that proves it
now catches a violation: a command that writes a violating copy or fixture outside the
worktree, runs only that guard against it, prints "GUARD MISSED: <guard name>" and exits
non-zero when the guard accepts it, and exits 0 when the guard rejects it. The probe is
red_on_base, with red_output "GUARD MISSED: <guard name>". The project's full test suite
as evidence.
""",
}


def test_guardrails_text():
    assert lens_text("guardrails") == TEXTS["guardrails"]
    text = render_scout_directives(
        scout_id="id", project="repo", worktree="tree", exclusions=(), lens="guardrails"
    )
    assert "red_on_base" in text
    assert 'red_output "GUARD MISSED: <guard name>"' in text
    assert "violating copy or fixture outside the\nworktree" in text
    assert "non-zero when the guard accepts it" in text
    assert "exits 0 when the guard rejects it" in text
    assert "The project's full test suite\nas evidence." in text


def test_mutation_failure_marker():
    text = render_scout_directives(
        scout_id="id", project="repo", worktree="tree", exclusions=(), lens="tests"
    )
    assert "MUTANT SURVIVED: <test name>" in text
    assert "When the named test survives" in text
    assert "before exiting nonzero" in text
    assert "red_output" in text
    assert "Never print the survival\nmarker for runner-loading errors" in text


def test_state_compatibility():
    args = {"scout_id": "id", "project": "repo", "worktree": "tree", "exclusions": ()}
    assert render_scout_directives(**args) == BASE
    assert render_scout_directives(**args, lens="state") == BASE
    assert (
        build_scout_prompt({}, 2)
        == "Find at most 2 useful candidate tasks. Respect the supplied owner context and avoid every existing or dropped task. Treat project_notes as owner-maintained constraints on every candidate, especially its verify_hints. Return only the JSON list required by the system directive.\n\nScout inputs:\n{}"
    )


@pytest.mark.parametrize("lens", ["docs", "tests", "guardrails"])
def test_lens_section(lens):
    assert (
        render_scout_directives(
            scout_id="id", project="repo", worktree="tree", exclusions=(), lens=lens
        )
        == BASE + f"\n## Lens: {lens}\n\n" + TEXTS[lens]
    )


CONSISTENCY_RULES = (
    'Keep each evidence entry exactly "file:line"; name the symbol at that line in "why" '
    '(for example "DiaryExcerpt in src/api.py"), so the finding survives small line shifts.',
    'Make "change" name every edit the verify hints check, and nothing the code '
    "contradicts; read the code before stating how it behaves.",
    "A candidate that only changes tests never asks to edit production code; prove a test "
    "can fail with a mutation check outside the worktree instead.",
    "Put findings that touch the same file into one candidate.",
    "When a candidate removes information from logs, errors or other output, its "
    '"change" also says what must remain (for example fixed labels, counts, durations or class names).',
)


@pytest.mark.parametrize("lens", ["state", "docs", "tests", "guardrails"])
def test_consistency_rules(lens):
    directive = render_scout_directives(
        scout_id="id", project="repo", worktree="tree", exclusions=(), lens=lens
    )
    assert "src/api.py:206 DiaryExcerpt" not in directive
    for rule in CONSISTENCY_RULES:
        assert directive.count(f"- {rule}\n") == 1


@pytest.mark.parametrize("lens", ["state", "docs", "tests", "guardrails"])
def test_structured_hint_contract(lens):
    directive = render_scout_directives(
        scout_id="id", project="repo", worktree="tree", exclusions=(), lens=lens
    )
    for clause in (
        "verify_hints is a list of non-empty strings or objects",
        "non-empty string command (run is a synonym)",
        "timeout (positive\ninteger)",
        "red_on_base (boolean)",
        "red_output (non-empty string)",
        "purpose (string)",
        "Unknown hint-object keys are ignored",
        "For a probe that must fail before the change",
        "set red_on_base: true and red_output to a non-empty literal failure marker",
    ):
        assert clause in directive


def test_unknown():
    with pytest.raises(ValueError, match="state, docs, tests, guardrails"):
        render_scout_directives(
            scout_id="id", project="repo", worktree="tree", exclusions=(), lens="bad"
        )


@pytest.mark.parametrize("lens", ["state", "docs", "tests", "guardrails"])
@pytest.mark.parametrize("failed", [False, True])
def test_execution(tmp_project, tmp_path, monkeypatch, lens, failed):
    import json
    from pathlib import Path

    from helmraiser.ledger.db import Ledger
    from helmraiser.loop.scout import draft_rows, execute_scout
    from tests.cli.test_scout_drafts import candidate
    from tests.cli.test_scout_requests import config_for
    from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

    ledger = Ledger(tmp_path / "ledger.db")
    adapter = ScriptedPiAdapter(
        {
            "scout": PiScript(
                final_text=json.dumps([candidate()]),
                ok=not failed,
                error_message="failure" if failed else None,
            )
        }
    )
    monkeypatch.setattr("helmraiser.loop.scout._logbook_brief", lambda repo: None)

    def intake(spec):
        raise ValueError("intake intentionally unavailable")

    outcome = execute_scout(
        tmp_project,
        ledger,
        config_for(tmp_project),
        tmp_path / "data",
        adapter,
        intake,
        force=True,
        lens=lens,
    )
    assert outcome.lens == lens
    assert outcome.as_dict()["lens"] == lens
    assert outcome.outcome == ("failed" if failed else "succeeded")
    assert ledger.get_scout(outcome.scout_id)["lens"] == lens
    assert f"Lens: {lens}" in Path(outcome.report_path).read_text()
    request = adapter.calls_for("scout")[0]
    directive = request.system_prompt_path.read_text()
    if lens != "state":
        assert directive.endswith(f"\n## Lens: {lens}\n\n" + TEXTS[lens])
    else:
        assert "## Lens:" not in directive
    if not failed:
        assert draft_rows(ledger)[0]["lens"] == lens
        from helmraiser.task.loader import parse_frontmatter

        tags = parse_frontmatter(Path(outcome.written_paths[0]).read_text())["frontmatter"]["tags"]
        assert tags == (["scout"] if lens == "state" else ["scout", f"scout-{lens}"])


def test_settings_precedence_and_validation(tmp_path):
    from helmraiser.ledger.db import Ledger
    from helmraiser.loop.scout_lenses import scout_lens_settings

    with Ledger(tmp_path / "ledger.db") as ledger:
        settings = scout_lens_settings({}, ledger)
        assert list(settings) == ["state", "docs", "tests", "guardrails"]
        assert settings["guardrails"] == {
            "auto": False,
            "cooldown_hours": 24,
            "source": {"auto": "default", "cooldown_hours": "default"},
        }
        assert settings["state"] == {
            "auto": True,
            "cooldown_hours": 24,
            "source": {"auto": "default", "cooldown_hours": "default"},
        }
        config = {
            "routing": {
                "scout": {
                    "enabled": False,
                    "cooldown_hours": 0,
                    "lenses": {"docs": {"auto": True, "cooldown_hours": 12}},
                }
            }
        }
        assert scout_lens_settings(config, ledger)["state"]["cooldown_hours"] == 0
        ledger.set_scout_lens_override("docs", auto=False)
        row = scout_lens_settings(config, ledger)["docs"]
        assert row == {
            "auto": False,
            "cooldown_hours": 12,
            "source": {"auto": "ledger", "cooldown_hours": "config"},
        }
        ledger.reset_scout_lens_override("docs")
        assert scout_lens_settings(config, ledger)["docs"]["auto"] is True
        for value in (0, -1, float("inf"), float("nan"), True, "12"):
            config["routing"]["scout"]["lenses"]["docs"]["cooldown_hours"] = value
            with pytest.raises(ValueError, match="cooldown_hours"):
                scout_lens_settings(config, ledger)
