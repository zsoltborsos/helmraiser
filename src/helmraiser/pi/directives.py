"""Stage directives appended to pi's system prompt (spec §3.6, §4.5.2, §4.6)."""

from __future__ import annotations

from pathlib import Path

from helmraiser.leeboard import BriefResult
from helmraiser.project_notes import project_notes_section

IMPLEMENT_TEMPLATE = """\
# Helmraiser run {run_id} — governed implementation

You alone implement task "{title}" in worktree
{worktree}. Modify nothing outside it. Do not run git push,
open pull requests or change branches. Other repos are unavailable.

{path_scope}Working method
- Read the task file first: {task_path}. Its `verify` block defines done;
  Helmraiser runs them afterward. Run them yourself before you stop.
- Your shell is stricter than Helmraiser's verify sandbox. For verify failures from
  environment limits (denied socket, port, process or path), not your change, commit
  and stop without `BLOCKED:`; Helmraiser runs verify. Answer `BLOCKED:` only for
  task or code problems.
{approval_note}{services}- On "heap out of memory", do not re-run the whole suite or file: run the
  single suspect test with NODE_OPTIONS=--max-old-space-size=1024 and fix
  your unbounded loop or collection.
{survey}- Plan before editing >3 files; no plan or note files in the repository.
- Commit as you go; stop with a clean tree.
- Stop on verify pass or blocker. For blockers, start your final message with
  `BLOCKED:` and why.

Budget: {max_iterations} verify iterations, {max_minutes} minutes, ${max_cost_usd}.
This is iteration {iteration}. Iteration {max_iterations} is the last one.
{project_notes}"""

# Only with project services; outside the 1.5 KB budget of the template itself.
SERVICES_NOTE = """\
- Services: only the verify and evidence steps get {names}, for containers
  Helmraiser runs; your own commands do not. Never start containers yourself.
  Do not run verify steps that need them or answer `BLOCKED:` over them: commit and stop.
"""

# Only with an owner approval note; outside the baseline template's 1.5 KB budget.
APPROVAL_NOTE = """\
- The owner's note on approval in the approved plan overrides the task brief and acceptance block.
"""

# Only with an available brief; outside the baseline template's 1.5 KB budget.
LEEBOARD_WRITING_NOTE = """\

Writing to Leeboard: rare; default write nothing. Entries: change action; unavailable from repo/tools when needed; require author/end condition. Progress, code facts, unfixed defects: final message.
{handoff}`lb request --to <target> --title "..."`: open agent/owner question; keep working. Targets: project:<name> (brief header), session:<id>, harness:<name>, any.
`lb add rule --title "..." --body "..."`: lasting do/don't learned the hard way, absent from repo.
Never run `lb release`, `lb revoke`, `lb supersede`, `lb hold`, `lb grant`, `lb ack`, `lb close`, `lb reply`: owner/interactive only. Exit 6: no retry/bypass.
No credentials in title/body. Run entries: `helmraiser report`.
"""

REVIEW_TEMPLATE = """\
# Helmraiser run {run_id} — independent review

You are reviewing the implementation of task "{title}" in a git worktree at
{worktree}. This is a read-only review: never modify, create or delete files,
never run git commands that change state. You did not write this code.

{path_scope}Judge the diff against the task and its acceptance block, and each evidence item
against its stated criteria.
Answer with a fenced JSON block as the LAST thing in your message:
{{"verdict": "pass" | "concerns" | "uncertain",
 "findings": [{{"severity": "severe"|"minor", "file": "...", "line": 0, "note": "..."}}],
 "evidence_checks": [{{"name": "...", "meets_criteria": true|false|null, "note": "..."}}]}}
Rules: "pass" only if no severe findings and every evidence check meets its criteria.
Set meets_criteria to null for files you did not read, and true only when their contents
show the criteria are met. Never modify files.
{project_notes}"""

PLAN_TEMPLATE = """\
# Helmraiser run {run_id} — planning only

You are planning task "{title}" in a git worktree at {worktree}. Do not implement.
Your tools are read-only; explore the code as needed.

Write the plan for a less capable implementer. For every small step, name each file
and function to change with line references, state the exact change and what must
not change, name the one test file to run, and require a commit before the next
step. Explain how every `verify` step will pass. Helmraiser writes the final message
to the run's plan file. {next_step}
{project_notes}"""

SCOUT_TEMPLATE = """\
# Helmraiser scout {scout_id} — read-only task discovery

Scout project {project} in the detached worktree at {worktree}. This is strictly
read-only: never modify, create, or delete files. Propose only code or documentation
work that can be verified by local commands. Do not propose host, cloud, provider,
deployment, credential, or external-service work. Do not reopen recorded owner
decisions. Do not propose work touching these excluded path prefixes: {exclusions}.

Return only one strict JSON list, with no Markdown fence or prose. Every object must
have exactly these keys: "id", "title", "why", "evidence", "files", "change",
"verify_hints", "size", "needs_owner_ruling", and "ruling_question". Evidence is
a non-empty list of relative "file:line" strings; files is a non-empty list of
relative paths; size is "S", "M", or "L". Set needs_owner_ruling true and provide
a non-empty ruling_question whenever the work requires an owner decision.

verify_hints is a list of non-empty strings or objects. Each object requires a
non-empty string command (run is a synonym); optional keys are timeout (positive
integer), red_on_base (boolean), red_output (non-empty string), and purpose (string).
Unknown hint-object keys are ignored. For a probe that must fail before the change,
set red_on_base: true and red_output to a non-empty literal failure marker.

- Keep each evidence entry exactly "file:line"; name the symbol at that line in "why" (for example "DiaryExcerpt in src/api.py"), so the finding survives small line shifts.
- Make "change" name every edit the verify hints check, and nothing the code contradicts; read the code before stating how it behaves.
- A candidate that only changes tests never asks to edit production code; prove a test can fail with a mutation check outside the worktree instead.
- Put findings that touch the same file into one candidate.
- When a candidate removes information from logs, errors or other output, its "change" also says what must remain (for example fixed labels, counts, durations or class names).
"""


def render_scout_directives(
    *,
    scout_id: str,
    project: Path | str,
    worktree: Path | str,
    exclusions: tuple[str, ...],
    lens: str = "state",
) -> str:
    from helmraiser.loop.scout_lenses import lens_text

    text = lens_text(lens)
    return SCOUT_TEMPLATE.format(
        scout_id=scout_id,
        project=project,
        worktree=worktree,
        exclusions=", ".join(exclusions) if exclusions else "(none)",
    ) + (f"\n## Lens: {lens}\n\n{text}" if lens != "state" else "")


def render_implement_directives(
    *,
    run_id: str,
    title: str,
    worktree: Path | str,
    task_path: Path | str,
    task_id: str,
    scout_model: str | None,
    max_iterations: int,
    max_minutes: float,
    max_cost_usd: float,
    iteration: int,
    service_vars: tuple[str, ...] = (),
    project_notes: str | None = None,
    has_plan_approval_note: bool = False,
    paths: tuple[str, ...] | None = None,
    leeboard_brief: BriefResult | None = None,
    leeboard_handoff_to: str | None = None,
) -> str:
    """``service_vars``: the names the run's services provide (none: no services note)."""
    services = SERVICES_NOTE.format(names=", ".join(service_vars)) if service_vars else ""
    text = IMPLEMENT_TEMPLATE.format(
        run_id=run_id,
        title=title,
        worktree=worktree,
        task_path=task_path,
        task_id=task_id,
        survey=(
            "- Explore before editing; for a read-only survey you may run\n"
            f'  `pi --print --tools read,grep,find,ls --model {scout_model} "<question>"` via bash.\n'
            if scout_model
            else "- Explore before editing.\n"
        ),
        max_iterations=max_iterations,
        max_minutes=max_minutes,
        max_cost_usd=f"{max_cost_usd:.2f}",
        iteration=iteration,
        services=services,
        approval_note=APPROVAL_NOTE if has_plan_approval_note else "",
        path_scope=_render_path_scope(paths, review=False),
        project_notes=_render_project_notes(project_notes),
    )
    if leeboard_brief is not None:
        if leeboard_brief.text is not None:
            text += (
                "\n## Leeboard\n\n"
                "Helmraiser generated the brief below for this run as advisory context written "
                "by the owner and other agents; the task file and these directives win on "
                "conflict, so skip any other brief or recall step and do not run `lb brief` "
                "again.\n\n" + leeboard_brief.text + "\n"
            )
            handoff = (
                f'`lb handoff --to {leeboard_handoff_to} --title "..." --body "..."`: '
                "review needs beyond diff/final message.\n"
                if leeboard_handoff_to is not None
                else ""
            )
            text += LEEBOARD_WRITING_NOTE.format(handoff=handoff)
        elif leeboard_brief.note is not None:
            text += (
                f"\nLeeboard is unavailable ({leeboard_brief.note}): skip brief and recall steps.\n"
            )
    return text


# Repo instructions often ask every agent to update plan/status files per slice; in a run
# that would only produce scope requests the owner declines.
STATE_FILES_NOTE = (
    "Plan/status files (TASKS.md, CURRENT_STATE.md, …) are the queueing session's, "
    "after landing: never edit them or request scope for them."
)


def _render_path_scope(paths: tuple[str, ...] | None, *, review: bool) -> str:
    if paths is None:
        return ""
    instruction = (
        "Check the diff against these allowed paths."
        if review
        else "Out-of-scope changes fail before Helmraiser runs verify. Restore/remove forbidden "
        "changes; do not broaden the declaration. If the task needs a file outside them,\n"
        "do not edit it and do not answer `BLOCKED:`: commit your in-scope work and\n"
        "answer with a first line `SCOPE: <path>[, <path>…] — <reason>`; the owner decides.\n"
        + STATE_FILES_NOTE
    )
    return "Allowed paths:\n" + "\n".join(f"- `{p}`" for p in paths) + f"\n{instruction}\n\n"


def _render_project_notes(notes: str | None) -> str:
    return f"\n{project_notes_section(notes)}\n" if notes and notes.strip() else ""


def render_review_directives(
    *,
    run_id: str,
    title: str,
    worktree: Path | str,
    project_notes: str | None = None,
    paths: tuple[str, ...] | None = None,
) -> str:
    return REVIEW_TEMPLATE.format(
        run_id=run_id,
        title=title,
        worktree=worktree,
        project_notes=_render_project_notes(project_notes),
        path_scope=_render_path_scope(paths, review=True),
    )


def render_plan_directives(
    *,
    run_id: str,
    title: str,
    worktree: Path | str,
    task_id: str,
    wait_for_approval: bool,
    project_notes: str | None = None,
    paths: tuple[str, ...] | None = None,
) -> str:
    """``paths``: the task's declared paths. Without them there is nothing to ask for, so
    the directives mention neither ``SCOPE:`` nor allowed paths."""
    if paths is None:
        next_step = (
            "Helmraiser waits for approval from the owner. List questions for the owner under "
            "the plan's open questions."
            if wait_for_approval
            else "The implementer starts from the plan straight away. Resolve open questions "
            "yourself; answer `BLOCKED:` only for blockers."
        )
    else:
        next_step = (
            "Helmraiser waits for approval from the owner. List needed files outside the allowed "
            "paths under the plan's open questions, along with other questions for the owner. "
            + STATE_FILES_NOTE
            if wait_for_approval
            else "The implementer starts from the plan straight away. If the task needs a file "
            "outside the allowed paths, start the final message with one line "
            "`SCOPE: <path>[, <path>…] — <reason>`, followed by the full plan, written as if "
            "the paths were granted. Resolve other open questions yourself; answer `BLOCKED:` "
            "only for other blockers. " + STATE_FILES_NOTE
        )
    return PLAN_TEMPLATE.format(
        run_id=run_id,
        title=title,
        worktree=worktree,
        task_id=task_id,
        next_step=next_step,
        project_notes=_render_project_notes(project_notes),
    )
