import pytest

from helmraiser.loop.scout import build_scout_prompt
from helmraiser.pi.directives import render_scout_directives

BASE = '# Helmraiser scout id — read-only task discovery\n\nScout project repo in the detached worktree at tree. This is strictly\nread-only: never modify, create, or delete files. Propose only code or documentation\nwork that can be verified by local commands. Do not propose host, cloud, provider,\ndeployment, credential, or external-service work. Do not reopen recorded owner\ndecisions. Do not propose work touching these excluded path prefixes: (none).\n\nReturn only one strict JSON list, with no Markdown fence or prose. Every object must\nhave exactly these keys: "id", "title", "why", "evidence", "files", "change",\n"verify_hints", "size", "needs_owner_ruling", and "ruling_question". Evidence is\na non-empty list of relative "file:line" strings; files is a non-empty list of\nrelative paths; size is "S", "M", or "L". Set needs_owner_ruling true and provide\na non-empty ruling_question whenever the work requires an owner decision.\n'
TEXTS = {
    "docs": 'Find documentation that disagrees with the code. Read the project\'s user-facing and\nagent-facing documents (README, docs/, AGENTS.md, CLAUDE.md, CURRENT_STATE.md, TASKS.md,\nskill files) and the help strings of its commands. Check each concrete claim, such as a\ncommand, flag, option, default, file path, config key, limit, behaviour or status, against\nthe code. Report a claim only when the code contradicts it, or when a document describes a\nbehaviour but omits a part of it that would make a reader act wrongly.\n\nEvidence must always pair the document line with the code line that contradicts it, for\nexample "README.md:40" and "src/cli.py:112". Do not report style, tone, wording\npreferences, or missing documentation for internal code.\n\nIn "change", give the corrected text, describing only what the code does. If the code\nlooks wrong rather than the document, set needs_owner_ruling and ask which one is\nintended. Group the findings for one document area into one candidate, size S or M.\nVerify hints: the project\'s existing documentation checks (link checker, doc tests,\nmarkdown lint) if it has them, and a check that the stale text is gone, such as\n"! grep -n \'old-flag\' README.md".\n',
    "tests": 'Find tests that give no real assurance. Look for:\n1. Tests that cannot fail: they would still pass if the behaviour they name were removed\n   or reverted. For example, they assert what a mock was told to return, assert on their\n   own input, assert only a type or "not None", or catch and discard the failure.\n2. Duplicates: two tests that prove the same behaviour through the same interface, with no\n   different input or branch.\n3. Implementation pinning: tests that assert private helpers, call order or internal\n   structure, where a test of the behaviour exists or should exist instead.\n4. Snapshots that capture incidental output rather than the behaviour.\n5. Tests whose name promises a behaviour their body never exercises.\nLook first at tests near recently changed code (recent_commits). Do not flag a test for\nstyle alone.\n\nEvidence: the test\'s file:line and the file:line of the production behaviour it should\nprove; for a duplicate, both tests.\n\nIn "change", prefer strengthening the test so it fails when the behaviour is reverted.\nMerge duplicates. Delete a test only when another named test already proves the same\nbehaviour. Never weaken an assertion. Require the implementer to prove each strengthened\nor remaining test: temporarily break the named behaviour, show that the test fails,\nrestore it, and report the result in the run summary. That proof cannot be a verify step,\nbecause intake runs verify on the unchanged code.\n\nVerify hints: the project\'s full test suite, and its coverage gate if it has one; coverage\nmust not drop.\n',
}


def test_state_compatibility():
    args = {"scout_id": "id", "project": "repo", "worktree": "tree", "exclusions": ()}
    assert render_scout_directives(**args) == BASE
    assert render_scout_directives(**args, lens="state") == BASE
    assert (
        build_scout_prompt({}, 2)
        == "Find at most 2 useful candidate tasks. Respect the supplied owner context and avoid every existing or dropped task. Treat project_notes as owner-maintained constraints on every candidate, especially its verify_hints. Return only the JSON list required by the system directive.\n\nScout inputs:\n{}"
    )


@pytest.mark.parametrize("lens", ["docs", "tests"])
def test_lens_section(lens):
    assert (
        render_scout_directives(
            scout_id="id", project="repo", worktree="tree", exclusions=(), lens=lens
        )
        == BASE + f"\n## Lens: {lens}\n\n" + TEXTS[lens]
    )


def test_unknown():
    with pytest.raises(ValueError, match="state, docs, tests"):
        render_scout_directives(
            scout_id="id", project="repo", worktree="tree", exclusions=(), lens="bad"
        )
