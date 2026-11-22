"""Named methods for a scout session."""

import math
from typing import Any

from helmraiser.config import DEFAULT_ROUTING
from helmraiser.ledger.db import Ledger

LENSES = {
    "state": "",
    "docs": """Find documentation that disagrees with the code. Read the project's user-facing and
agent-facing documents (README, docs/, AGENTS.md, CLAUDE.md, CURRENT_STATE.md, TASKS.md,
skill files) and the help strings of its commands. Check each concrete claim, such as a
command, flag, option, default, file path, config key, limit, behaviour or status, against
the code. Report a claim only when the code contradicts it, or when a document describes a
behaviour but omits a part of it that would make a reader act wrongly.

Evidence must always pair the document line with the code line that contradicts it, for
example "README.md:40" and "src/cli.py:112". Do not report style, tone, wording
preferences, or missing documentation for internal code.

In "change", give the corrected text, describing only what the code does. If the code
looks wrong rather than the document, set needs_owner_ruling and ask which one is
intended. Group the findings for one document area into one candidate, size S or M.
Verify hints: the project's existing documentation checks (link checker, doc tests,
markdown lint) if it has them, and a check that the stale text is gone, such as
"! grep -n 'old-flag' README.md".
""",
    "tests": """Find tests that give no real assurance. Look for:
1. Tests that cannot fail: they would still pass if the behaviour they name were removed
   or reverted. For example, they assert what a mock was told to return, assert on their
   own input, assert only a type or "not None", or catch and discard the failure.
2. Duplicates: two tests that prove the same behaviour through the same interface, with no
   different input or branch.
3. Implementation pinning: tests that assert private helpers, call order or internal
   structure, where a test of the behaviour exists or should exist instead.
4. Snapshots that capture incidental output rather than the behaviour.
5. Tests whose name promises a behaviour their body never exercises.
Look first at tests near recently changed code (recent_commits). Do not flag a test for
style alone.

Evidence: the test's file:line and the file:line of the production behaviour it should
prove; for a duplicate, both tests.

In "change", prefer strengthening the test so it fails when the behaviour is reverted.
Merge duplicates. Delete a test only when another named test already proves the same
behaviour. Never weaken an assertion. Require each strengthened or remaining test to be proved able to fail, by a check rather
than a report. In "verify_hints", propose one mutation check per test: a command that
breaks the named behaviour without changing any file in the worktree (for example a short
script that patches it with a mock, or that runs the test against a modified copy of the
source outside the worktree), runs only that test, and exits 0 only when the test fails.
Say that the check is red_on_base: on the unchanged code the weak test survives the
mutation, so the check fails until the test is strengthened. When the named test survives
its mutation, print the fixed marker `MUTANT SURVIVED: <test name>` before exiting nonzero.
Name that exact marker as the step's red_output in the hint. Never print the survival
marker for runner-loading errors or other infrastructure failures.

Verify hints: the mutation checks; the changed test files; the project's full test suite
and its coverage gate, if it has one, as evidence. Coverage must not drop.
""",
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


def scout_lens_settings(
    config: dict[str, Any],
    ledger: Ledger,
    *,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve settings, optionally previewing replacement overrides without writing."""
    scout = config.get("routing", {}).get("scout", {})
    configured = scout.get("lenses", {})
    if not isinstance(configured, dict):
        raise ValueError("routing.scout.lenses must be an object")
    result = {}
    for lens in LENSES:
        values = configured.get(lens, {})
        if not isinstance(values, dict):
            raise ValueError(f"routing.scout.lenses.{lens} must be an object")
        override = (
            overrides[lens]
            if overrides is not None and lens in overrides
            else ledger.scout_lens_override(lens)
        )
        row = {}
        sources = {}
        for field in ("auto", "cooldown_hours"):
            default = (
                (
                    scout.get("enabled", DEFAULT_ROUTING["scout"]["enabled"])
                    if lens == "state"
                    else False
                )
                if field == "auto"
                else scout.get("cooldown_hours", DEFAULT_ROUTING["scout"]["cooldown_hours"])
            )
            source = "ledger" if field in override else "config" if field in values else "default"
            value = override.get(field, values.get(field, default))
            if field == "auto":
                if not isinstance(value, bool):
                    raise ValueError(f"{lens}.auto must be boolean")
            elif (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                or (source != "default" and value == 0)
            ):
                raise ValueError(f"{lens}.cooldown_hours must be finite and positive")
            row[field] = value
            sources[field] = source
        row["source"] = sources
        result[lens] = row
    return result


def lens_text(name: str) -> str:
    try:
        return LENSES[name]
    except KeyError:
        raise ValueError(f"Unknown lens {name!r}; known lenses: {', '.join(LENSES)}") from None
