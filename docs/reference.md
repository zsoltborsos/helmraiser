# CLI and configuration reference

Start with the [README](../README.md) for installation, safety boundaries and a complete
first task. This reference covers the detailed workflow and operational edge cases.
Passing acceptance commands is not a proof of correctness. A reviewer cannot grant
merge authority, but a passing verdict is required for opt-in `auto` delivery.
See also the [execution model](architecture/execution-model.md).

## Contents

- [Requirements](#requirements), [model setup](#models-setup), [workflow commands](#quick-start)
- [Scout drafts](#scout-drafts), [task files](#task-files), [project notes](#project-notes)
- [Project configuration](#projecttoml), [models and budgets](#model-profiles-and-budgets), [stage skills](#stage-skill-folders)
- [Policies and delivery](#policies), [isolation](#isolation), [services](#services-for-verify)
- [Sandbox preflight](#trying-commands-in-the-sandbox), [monitoring](#monitoring), [status contract](#status-contract)
- [Unattended operation](#running-unattended), [retries](#retrying-a-failed-run), [rework](#reworking-a-run-awaiting-merge-approval)
- [Leeboard](#leeboard-optional), [troubleshooting](#troubleshooting)

## Requirements

- **Linux only.** The default `process` isolation uses bubblewrap.
- Python 3.12+ and git.
- [pi](https://github.com/earendil-works/pi) (`@earendil-works/pi-coding-agent`),
  configured with at least one provider.
- For default `process` isolation: `srt` (`@anthropic-ai/sandbox-runtime`), `bwrap`,
  `socat` and `rg` on `PATH`. See [Isolation](#isolation) for installation and
  [Troubleshooting](#troubleshooting) for readiness failures.
- `gh`, logged in, for `delivery: pr`; see [Policies](#policies).
- Optional: Docker, needed only for project `[[services]]`;
  see [Services for verify](#services-for-verify).

Helmraiser runs whatever pi is installed and has no supported version range: `work` runs `pi --version`
before it starts, fails when pi is missing or prints no version, and records the version
on every run. Budget caps depend on the usage pi reports, so an assistant message without
readable usage (`input`, `output`, `cost.total`) fails the run at once with
`pi_contract: …` and pauses the whole queue (see [Troubleshooting](#troubleshooting)). Other breaks in pi's
JSON output show up as `protocol` errors. Each run also checks provider auth for the
implement model first.

## Models setup

Helmraiser ships **no model defaults**. Set the roles you need in
`~/.helmraiser/config.json` to models available through your configured pi providers:

| Command | Required model settings |
| --- | --- |
| `work` (all forms), `worker start` | `plan`, `implement`, `fallback`, `review` |
| `intake` | `intake`, only when it must propose acceptance; valid acceptance needs no model |
| `scout` | `routing.scout.model` |
| `compare` | `judge` |
| `eval review` | `review`, `judge` |

Missing settings refuse model work before a session or ledger write, listing the
missing roles (`--json`: `models_not_configured`). Task `models` and project
`[models]` overrides take precedence over config profiles. Model-free commands such
as `status`, `caps`, `queue`, `add`, `show`, `log`, `report`, `version` and approval
commands remain usable without models.

Copy and adapt [examples/config.json](../examples/config.json), a tried-and-working
November 2026 setup using an OpenAI Codex subscription and OpenCode Go—not universal
defaults. The repository-shipped [helmraiser-setup skill](../skills/helmraiser-setup/SKILL.md)
helps an agent discover your models, agree a configuration, preserve existing settings
with a backup, and check readiness. You start it yourself with `/helmraiser-setup`;
agents do not load it on their own. Its real-model test run is opt-in and requires
consent to spend. From the Helmraiser clone root, choose your agent's install command:

```bash
# Claude Code
mkdir -p ~/.claude/skills && ln -s "$PWD/skills/helmraiser-setup" ~/.claude/skills/helmraiser-setup
# pi
mkdir -p ~/.agents/skills && ln -s "$PWD/skills/helmraiser-setup" ~/.agents/skills/helmraiser-setup
```

Or start with placeholders:

```json
{
  "profiles": {
    "intake": {"model": "<provider>/<model>"},
    "plan": {"model": "<provider>/<model>"},
    "implement": {"model": "<provider>/<model>"},
    "fallback": {"model": "<provider>/<model>"},
    "review": {"model": "<provider>/<review-model>"},
    "scout": {"model": "<provider>/<model>"},
    "judge": {"model": "<provider>/<model>"}
  },
  "routing": {
    "scout": {"model": "<provider>/<model>:high"}
  }
}
```

These are placeholders, not usable IDs: replace them with actual `provider/model`
IDs. The review model must differ from the implement model. The `scout` profile
serves implementation surveys; `routing.scout.model` configures task discovery.
See [Model profiles and budgets](#model-profiles-and-budgets) for the full defaults.

Run `helmraiser doctor` to check readiness. It reports a `pi auth: <profile>` row for
each profile; missing models show `not configured` without an auth check.
Models rejected by `pi auth check` make readiness fail
and the command exit non-zero. It also checks other readiness conditions, so
successful authentication alone does not guarantee readiness. When intake asks
its model and the session fails, it reports `intake session failed (...)`;
intake fails if its model is unusable. A task with an already-valid acceptance
block may skip the intake model.

## Install

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
export PATH="$PWD/.venv/bin:$PATH"
helmraiser version
```

## Quick start

The [helmraiser-tasks skill](../skills/helmraiser-tasks/SKILL.md) guides agents through writing task files, proving acceptance, queueing with intake, reviewing runs, approving, landing, reworking and retrying. From this checkout’s root, install it for Claude Code with `mkdir -p ~/.claude/skills && ln -s "$PWD/skills/helmraiser-tasks" ~/.claude/skills/`, or for pi with `mkdir -p ~/.agents/skills && ln -s "$PWD/skills/helmraiser-tasks" ~/.agents/skills/`. These use an absolute symlink target; keep the source checkout available.

```bash
# 1. Register a task file (or create a draft: helmraiser add "text" --title "…" --project <repo>)
helmraiser add ~/code/myrepo/.helmraiser/tasks/fix-parser.md

# Optional: discover draft tasks, then inspect the owner's draft list
helmraiser scout ~/code/myrepo
helmraiser drafts

# 2. Intake: check acceptance first; --check --yes queues only if the check passes
helmraiser intake ~/code/myrepo/.helmraiser/tasks/fix-parser.md --check
helmraiser intake ~/code/myrepo/.helmraiser/tasks/fix-parser.md --check --yes \
  --origin fo:/absolute/path/to/the/session.jsonl

# 3. Inspect the queue
helmraiser queue list

# 4. Run it (--once: one task; no flag: drain the queue)
#    work first reconciles orphaned runs left by dead workers
helmraiser work --once
#    or keep a worker up in the background: it runs up to 2 runs at once
#    and waits for new work (approve, retry, intake) instead of exiting
helmraiser worker start

# 5. Next morning
helmraiser report            # Landed / Awaiting your approval / Failed / Cost
helmraiser log <run>         # timeline of one run (id or unique prefix)
helmraiser show <ref>        # run id, task id or unique run prefix; state and limits
helmraiser wait <ref> --json # block until owner attention or completion

# 6. Decide on runs that wait for you
helmraiser approve <run>     # rebase if needed, ff-merge into the base branch, remove worktree
helmraiser land <run>        # delivery: pr — holds, approve, CI, push into origin base, confirm
helmraiser approve <run> --paths a,b  # a scope request: add (some of) its paths, resume
helmraiser approve <run> --decline --note "…"  # say no to the paths and resume with advice
helmraiser reject <run> --note "wrong approach"  # worktree + metadata removed; branch kept
helmraiser rework <run> --note "fix the minor findings"  # same worktree, worker implements again
helmraiser rework <run> --rebase  # the base moved: the implementer rebases, resolves conflicts; verify and review again
helmraiser retry <run>       # fix the task file (or raise its budget), re-verify; without committed work, restart
helmraiser retry <run> --implement  # …or implement from its last verify failure, or continue after a budget stop; same restart without work
helmraiser retry <run> --adopt-base # record a host rebase onto the run's base branch, then retry
helmraiser record-landed <run> --commit <rev> --note "…"  # a failed run whose branch was merged by hand: record it landed, release its dependents
```

For `approve` (including `--decline`), `reject`, `rework` and `record-landed`, `--note -` reads the note from stdin, removes one trailing newline and rejects empty or whitespace-only input.

`add --title` writes a new draft to `<repo>/.helmraiser/tasks/<slug>.md`. On a terminal, `intake`
without `--yes` or `--check` opens the merged task in `$VISUAL`/`$EDITOR`; the saved result is validated
and queued, and closing without saving cancels. Without a terminal, or with `--json`, it
prints the proposal and exits 1.
`intake --origin <kind>:<session>` records the queueing session as the review origin.
It splits at the first colon (so later colons remain in the session), accepts `fo` or
`claude-code`, and replaces any origin already in the file and ledger snapshot: the
session that queues the task owns its review. The option also works with `--propose`
and editor-based intake; an intake-model proposal cannot set or change the origin.
If the task file already has a valid acceptance block, intake does not ask the model.
Intake never proposes `policy: auto` unless you pass `--allow-auto`. It never proposes
`sandbox: host` either: it removes `sandbox` from every proposed step, with a note.
The intake prompt includes a project environment section with the effective setup command
(task setup overrides the project default) and sorted, merged environment variable names,
never their values. Declared services are listed by name with their sorted provided variable
names, omitting images, ports, service environment settings and provided values. When services
are declared, the prompt instructs steps to use those variables rather than start containers
or services. Verify and evidence run from the repository root after setup, with no virtual
environment activated: use worktree tool paths such as `.venv/bin/python` or the project's own
runner such as `npm test`.
The intake prompt says that verify and evidence run the project's tests and commands and
never read, grep, parse or check the existence of source files. For documentation-only
Markdown or plain-text tasks, one step may check that removed stale wording no longer
appears in the affected document; this excludes help strings and other text embedded in code.
The intake prompt also requests `$HELMRAISER_BASE_SHA` rather than literal base commit IDs, suitable step timeouts following project notes (defaults: 300 seconds for verify and 600 for evidence), only configured lint and type commands, and checks covering every part of the described change.
Intake is instructed to set `red_on_base: true` only on steps that check the task's
change against an existing file, such as that stale-text check or an assertion on current
command output, so the step must fail before the change. Each such step must name
`red_output`: short fixed text unique to the intended failure (not generic `Error` or
`FAILED`), such as an assertion message or script-generated marker.
A proposed step that still
does (it greps or reads with `grep`, `rg`, `cat`, `readFileSync`, `open` or
`Path(…).read_text()`, parses with `createSourceFile` or `ast.parse`, or runs `test -f`
on a path under `src/`, `lib/`, `app/`, `packages/` and the like, or with a source-file
suffix) is kept, with a note naming it (`task/structural.py`). Paths under
`$HELMRAISER_EVIDENCE_DIR`, build output and text files such as `README.md` do not count.
Notes are printed before the editor opens, and `--json` output carries them.

The prompt instructs the intake model to keep verify fast because it runs after every
implementation attempt: use tests covering the changed code, plus the project's lint or
type checks when code changes. Put the full test suite and its coverage gate only in
evidence, which runs once after verify passes; do not duplicate checks in both.
When frontmatter supplies `size`, the task section shows `Task size: S` (or the supplied
value) before the task body; without size, no line is added. Proposed budgets for size S
should be $3, 60 minutes and 3 iterations; for M, $6, 120 minutes and
4 iterations. For L or unknown size, size the budget to the work. Preserve an existing
budget. These are instructions to the intake model, not validated limits.
Owner-maintained project notes take precedence over this general guidance for verify and evidence steps, budgets and timeouts.

The intake model's pi session (tools `read`, `grep`, `find`, `ls`, in the primary
checkout) runs inside `srt` with the same read rules and network allowlist as a run
(see [Isolation](#isolation)). It may write only to a temporary session directory and
`~/.pi/agent`; its session files are then kept in `<data-dir>/intake/<id>/sessions`.
Without a working `srt`, intake refuses to ask the model unless
`isolation.intake_unsandboxed` is `true` in `~/.helmraiser/config.json`.

Before accepting with `--yes`, use `helmraiser intake <file> --check`. Check-only never
opens an editor or writes to the ledger, and leaves an already-valid file byte-for-byte
unchanged. If intake needs a model proposal, it saves the validated proposal as a draft
without an approval stamp, even if the check fails. `--check --propose` is also check-only.
`--check --yes` queues and approves only after a successful check; on check failure it
leaves the file and ledger unchanged.

For a newly queued task with `paths:`, `intake --yes` (with or without `--check`)
appends the tracked related tests it detects, up to 20 tests. A related test must use
the same language family (Python or JS/TS) and either match the source name within
the same nearest tracked package root, or import the declared source through a full
Python module path or resolved JS/TS path. Package roots are directories containing
a tracked `pyproject.toml`, `setup.py`, `setup.cfg`, or `package.json`, falling back
to the repository root when none exists. If it finds more than
20, it adds none and reports the total with an advisory to review the missing scope.
Check-only reports `Would add related tests to paths:` without changing scope or
addition metadata. Its preview is conditional: check-only does not inspect ledger
state, and additions require a task that is neither queued nor active in the ledger.
Already-queued tasks (in either the file or ledger), active runs,
`--propose`, editor-based intake, and tasks without `paths:` are not auto-expanded.
Successful additions are listed in human output and recorded in
`intake.related_tests_added` in the file and queued snapshot. Queueing and check-only
JSON include `related_tests_added` (this invocation's actual additions),
`related_tests_would_add` (preview paths, conditional on ledger eligibility as explained
in `notes`), and `related_tests_count` (total
candidates detected). Lists are empty when no addition or preview occurred.
Intake serializes the augmented task through the normal task renderer for both the
file and queued snapshot; original YAML formatting and comments are not preserved.
It still updates status, approval metadata, accepted proposal fields and an explicit
`--origin`.

The check runs verify commands, then evidence commands, serially with each step's own
timeout and explicit sandbox setting. It uses the same preflight machinery as scout:
a throwaway worktree of the project's committed HEAD (not dirty working-copy content),
project setup and services, and the verify sandbox settings. No extra pi session runs
beyond intake's model when a proposal is needed. Each step reports `name: class`:
`green`, `expected red`, `sandbox`, or `fail`; failures include the diagnostic excerpt
and any sandbox hint. Exit 0 means all steps are green or expected red; otherwise it
exits 2 without queueing. A step with `red_on_base: true` is expected red only after a
completed nonzero exit whose captured output contains its `red_output` as a literal,
case-sensitive substring. Missing or mismatched markers produce `fail` with a tail
diagnostic; a task without the field still loads. Timeouts and launch failures
remain failures, and sandbox hints retain precedence. If it passes, it is `fail`: the diagnostic says
it was declared red on base but passed on unchanged code, so it cannot detect the change.
This diagnostic appears in human failure output and JSON `check.steps[].tail`.
For unmarked steps, expected red requires a known missing-target diagnostic for a
literal file mentioned in the task's paths or body that is absent at HEAD. Globs,
directory-only declarations and targets mentioned only in commands do not qualify.
`--json` includes a `check` object with `summary`, `error`, and normalized `steps`.

To review a proposal before queueing, use `intake --propose`: it asks the intake model as
today, writes the proposed acceptance block into the task file with `status: draft` (no
approval stamp) and registers it as a draft, without queueing. Read the file, adjust it if
needed, then `intake <file> --check --yes` checks and queues it with any detected
related-test scope additions — the acceptance model is not asked again.

Every command takes `--json` and `--data-dir` (default `$HELMRAISER_DATA_DIR`, else
`~/.helmraiser`). Exit codes are 0 for ok, 1 for an error, 2 for a validation failure and
3 when the queue is paused or `land` encounters an active [Leeboard hold](#leeboard-optional).
`wait` uses its own [exit codes](#waiting-for-a-run).

## Scout drafts

Both `scout` and `drafts request` accept `--lens NAME` (default `state`);
unknown names exit 2 and list the known lenses. Each session follows one method:

- `state`: find useful tasks from project state and existing context.
- `docs`: find concrete documentation claims contradicted by code, pairing document and code lines.
- `tests`: find tests that give no real assurance and propose stronger behavioural checks.
- `guardrails`: find project-stated rules without enforcing checks and guards that cannot catch the violations they name.

Automatic worker scouting uses per-lens settings. Start gates and the pending-draft
cap remain per project; successful history and cooldown are per project and lens.
Requests run first, then automatic pairs oldest due first (never-scouted first;
changed HEAD uses completion time, unchanged HEAD uses completion plus cooldown;
ties use resolved project path then lens name). Different lenses never overlap on one project.

`helmraiser drafts settings [--json]` shows all lenses.
`helmraiser drafts settings set <lens> [--auto on|off] [--cooldown-hours N] [--json]`
writes partial ledger overrides; N must be finite and positive.
`helmraiser drafts settings reset <lens> [--json]` removes that lens's overrides.
All three commands accept `--data-dir`. JSON is `{"lenses": {"state": {"auto": true,
"cooldown_hours": 24, "source": {"auto": "default", "cooldown_hours": "default"}}, ...}}`.
Each field resolves independently: ledger, explicit
`routing.scout.lenses.<lens>.auto` / `.cooldown_hours` config, then default.
The default inherits global `routing.scout.enabled` for state (false for other lenses)
and global `cooldown_hours` for every lens. Missing globals default to true and 24.
Sources are `ledger`, `config`, or `default`; inherited globals count as `default`.
A global zero cooldown is valid; per-lens cooldowns must be positive.

**`routing.scout.enabled` is not a global automatic veto:** an explicit per-lens
`auto` (config or ledger) can enable automation even with `routing.scout.enabled=false`.
Use queue pause to stop all starts. Direct `scout` checks `routing.scout.enabled`
unless forced; requests bypass enablement and cooldown. There is one pending request per `(project, lens)`.
Cancellation without `--lens` cancels every pending lens for each project;
`--cancel --lens NAME` cancels only that lens. Requested scouts already bypass
cooldown and unchanged HEAD, so another lens can be requested at the same HEAD,
subject to the remaining gates below.

Reports name the lens. Human draft and scout listings show it; `scout --json`,
every row of `drafts --json` / `drafts list --json`, and every `status --json` row
in `scouts.pending`, `scouts.running`, and `scouts.recent` include `lens`.
Non-state drafts have both `scout` and `scout-<lens>` tags; state drafts are unchanged.

`helmraiser scout <repo> [--lens NAME] [--max N] [--force] [--json]` runs one read-only task-discovery
session. Without `--force`, scouting must be enabled; the repository must be a focus
project and not paused; the global queue must not be paused; the project must have no
queued task or non-terminal run (including one running, resuming, or awaiting approval);
pending drafts must be below `max_pending_drafts`; HEAD must have changed since the
latest successful scout for the selected lens or its `cooldown_hours` must have elapsed; and the scout model's
subscription must have routing headroom. `--force` bypasses every start gate, including
the global queue pause, but not output validation or the per-run cap. `--max` must be
positive, defaults to `max_drafts_per_run`, and is capped by it.

`helmraiser drafts request <repo> [<repo>…] [--lens NAME] [--json]` asks the background worker to scout
specific focus projects. Every argument is normalized to its git root. Validation is
all-or-nothing: every repository must be in `routing.scout.focus_projects` and none may
be in `paused_projects`. Repeating a pending request reports `already pending` and is
not an error. `helmraiser drafts request --cancel <repo>…` cancels pending requests even
if routing configuration changed after they were made.

A requested scout bypasses only `routing.scout.enabled` and the unchanged-HEAD/cooldown
gate. It still waits for a queue pause, queued or unfinished project work, the pending
draft cap, scout-subscription headroom, and a shared `max_parallel_runs` worker slot.
Requests run oldest first. The scout child consumes its request atomically with creating
its ledger record; a worker crash before spawning leaves the request pending. A request
remains consumed if its scout fails; the failed attempt appears in status rather than
being silently retried.

Helmraiser gathers existing root and `projects/*/` state/task/status documents, 40 recent
git subjects, every ledger task title, dropped-draft reasons, `[scout] exclude` prefixes,
owner-maintained `.helmraiser/notes.md`, and optional [Leeboard context](#leeboard-optional). It
then runs exactly one scout-model pi session under srt in a detached throwaway worktree
of HEAD, with only `read`, `grep`, `find` and `ls`; the worktree is removed afterwards.
The report is `<data-dir>/runs/<scout-id>/scout.md`.
Verify hints accept strings or structured command objects carrying purpose, timeout and
red-on-base/output markers, with multiline commands preserved in drafts.
Invalid candidates are skipped and listed under “Rejected candidates”; malformed JSON
or non-list responses still fail scouting.
The scout directive requests exact `file:line` evidence entries with symbol names in `why`, code-grounded changes covering every verify hint, outside-worktree mutation checks without production edits for test-only candidates, and one candidate for findings touching the same file.

Valid non-ruling candidates become `.helmraiser/tasks/<id>.md` files with `status: draft`,
`policy: review`, `isolation: process`, size, `tags: [scout]`, and
`source: scout <scout-id>`. Drafts omit delivery and inherit the project's default,
falling back to `merge` when none is configured. Existing IDs and normalized titles are skipped. Candidates
that need an owner ruling appear only in the report.
Draft `paths` contain the candidate's files in first-occurrence order, without duplicates.
Candidates overlapping earlier accepted candidates or ledger-recorded pending drafts of
any kind are skipped, with `<id>: overlaps <other id> on <file>, <file>` in the report.
Pending declarations use normal path matching (including globs and directories); the
first conflict is chosen by pending task ID, then same-run acceptance order.

Each generated draft then receives the same acceptance proposal as `intake --propose`
and remains `status: draft` without an approval stamp or queueing. Helmraiser preflights
its proposed verify steps first, followed by evidence steps, serially in one preflight
on unchanged committed code, using project setup and services, the verify sandbox
settings, a throwaway worktree, and each step's own timeout. Results are classified as
`green`, `expected red` (a missing path the task says it creates, or an explicitly
`red_on_base: true` step with a completed nonzero exit and its literal `red_output`
in captured output, without a sandbox hint), `sandbox`, or `fail`. Missing/mismatched
markers fail with a tail diagnostic; marked passes, timeouts and launch failures are
not expected red. Sandbox hints retain precedence.
Marked passing steps are `fail` with the same diagnostic as intake checks.
If any step is `fail` or `sandbox`, scout asks intake once more using the original
acceptance-free draft and those steps' names, commands, and real diagnostics (the last
2,000 characters per output tail, after redaction: values of the project and task `env`
and of the variables running services provide, when six characters or more, show as
`<redacted:NAME>`, and the password of a `scheme://user:password@host` URL as `<redacted>`). Green and expected-red steps do not trigger a retry.
The second proposal and preflight replace the first even if still failing. Intake notes
retain `re-proposed after a failed acceptance check: <step names>` from the first attempt,
also shown in the scout report. If retry intake fails, the first valid draft and preflight
remain available with `intake_failed` and the retry note. Standalone `helmraiser intake`
does not retry.
Intake and preflight take minutes, so before it rewrites a draft (and before the retry
intake) scout checks that the ledger task is still `draft` and the file is exactly what it
last wrote. If you dropped, queued or edited the draft meanwhile, preparation stops
without touching it and adds `preparation stopped: the draft changed after Helmraiser
last wrote it` to the draft's intake notes, also shown in the scout report.
An intake failure is retained on the reviewable draft and does not stop later drafts.
The scout report records each proposal, intake result, and preflight result.

Use `helmraiser drafts [--project P] [--all] [--json]` (or `drafts list`) to list project, ID, title, size, source,
age, kind, and the human preflight summary. Only scout drafts are listed by default;
`--all` includes manual drafts. JSON `kind` is `scout` when the snapshot source starts
with `scout`, otherwise `manual`. Drop accepts either kind. JSON also includes intake status/error, proposal,
normalized preflight steps, `task_id` (the argument `drafts drop` takes), `path` (the
absolute ledger source path), and `origin` (the
current file's `{kind, session}` mapping, or `null`). `helmraiser drafts drop <task>
--reason TEXT [--class CLASS]` keeps the file but marks it `dropped`.
`--reason` remains required. Optional classes are `wrong` (the finding is not real),
`unwanted` (real, but not worth doing), `duplicate` (already tracked, already fixed,
or the same as another draft), and `unusable` (real and wanted, but the draft needed
too much rework to queue). The class is stored as `drop_class` next to `drop_reason`
only when supplied. JSON output includes `"class"` or null; human output shows
`class=CLASS` or `class=unclassified`. Later scouts receive the reason and class
(null for missing or unrecognized stored values). To accept a draft,
review it and run `helmraiser intake --yes <file>` as usual; nothing is automatically
queued.

## Task files

A task is a markdown file with YAML frontmatter. The body is the brief given to the
implementer. `intake` snapshots the file into the ledger. Later edits have no effect
until you run `intake` again. The file does not have to be committed. A complete
example for a disposable repository ([first-task setup](../README.md#quick-start),
[`examples/tasks/slug-cli.md`](../examples/tasks/slug-cli.md)):

```markdown
---
id: slug-cli
title: Implement a Python slug CLI against the existing acceptance tests
project: ~/code/helmraiser-demo
status: draft
policy: review
delivery: merge
isolation: process
paths: [slug.py]
verify:
  - name: slug-behavior
    run: python3 test_slug.py
budget:
  max_cost_usd: 2.0
  max_minutes: 20
  max_iterations: 2
---
## Goal
Create `slug.py`, using only Python's standard library. Accept one text argument,
lowercase ASCII letters, retain digits, replace runs outside `a-z` and `0-9` with
hyphens and strip outer hyphens. Print the result and a newline. Empty or
punctuation-only input prints an empty line. Leave `test_slug.py` unchanged.
```

| Key | Required | Meaning |
|-----|----------|---------|
| `id` | yes | `[a-z0-9][a-z0-9._-]*`; the ledger key is `<project-slug>:<id>` |
| `title` | yes | One line |
| `project` | yes | Absolute path to the git repository |
| `priority` | no | `critical`, `high`, `normal` (default) or `low` |
| `depends_on` | no | List of bare task IDs in the same project; held until their delivered heads are in this task's local base (see [Dependency holds](#dependency-holds)) |
| `paths` | no | Allowed repository-relative POSIX globs, e.g. `["src/**/*.py", "tests/"]`; checked before verify |
| `status` | no | `draft` (default), `dropped`, `intake`, `queued`, `running`, `awaiting_approval`, `landed`, `failed` or `aborted`; Helmraiser writes `draft` or `queued` at intake and `dropped` when a draft is dropped. Run progress after that lives in the ledger (`status`, `report`), not in the file |
| `policy` | to queue | `auto`, `review` or `approve-plan-first` |
| `origin` | no | Review origin as `{kind: claude-code | fo, session: <id>}`; `session` is a non-empty string of at most 1024 characters with no control characters |
| `plan` | no | `true` runs the `plan` profile before implementation without waiting for plan approval; default `false` |
| `delivery` | no | `merge` (default) or `pr`; new PR runs safely synchronize the base from its configured upstream (see [Policies](#policies)) |
| `base` | no | Local branch from which to create the run worktree and target delivery; defaults to the primary checkout's current branch; PR synchronization never resets an ahead/diverged base |
| `isolation` | no | `process` (default), `none` or `vm`; intake keeps the file's isolation unless the proposal is stronger |
| `verify` | to queue | List of `{name, run, timeout, sandbox, red_on_base, red_output}` (`red_on_base` is an optional boolean, default false; optional `red_output` must be a non-whitespace string and is allowed only with `red_on_base: true`; timeout defaults to 300 s); a bare string is also accepted |
| `evidence` | for `auto` | List of `{name, run, artifacts, criteria, timeout, sandbox, red_on_base, red_output}` (`red_on_base` is an optional boolean, default false; optional `red_output` must be a non-whitespace string and is allowed only with `red_on_base: true`; timeout defaults to 600 s); commands write into `$HELMRAISER_EVIDENCE_DIR`. Both red fields affect base acceptance checks only, never implementation-run success criteria (also true for verify) |
| `models` | no | Per-task profile override, e.g. `implement: provider/id:thinking`; overrides the project's `[models]` and the config's `profiles` |
| `budget` | no | Per-task override of `max_cost_usd`, `max_minutes`, `max_iterations` |
| `setup` | no | Shell command run in the fresh worktree; a retry restart re-runs it unless the same command's latest attempt passed |
| `env` | no | `KEY: value` for every pi session and verify/evidence command |
| `tags` | no | Free-form |

`paths`, when declared, must be a non-empty list of non-empty relative patterns:
absolute patterns and literal `..` segments are rejected at `add`/`intake`.
Matching is case-sensitive: `*` and `?` stay within one path segment; a complete
`**` segment matches zero or more segments. A trailing `/` permits all descendants
of that directory (not a file with the directory's name). Other punctuation is
literal. Optional `.` and empty separator segments are normalized for matching
only. Intake preserves the owner's patterns, order and duplicates unchanged.
Omitting `paths` leaves scope unrestricted and preserves existing run behavior.
Intake adds a note (`notes` in `--json`), never an error, for each existing project
file with an extension that a verify command names outside `paths`: `verify runs X,
which is outside paths: add it if the change needs to edit it`.
Intake also adds an advisory note (`notes` in `--json`), never an error, when
`budget.max_cost_usd` is $20 or more, suggesting smaller tasks linked with `depends_on`.
Intake also adds one note listing tracked files and directories named in the body
that `paths` does not cover. Fenced code blocks count; URLs do not. The appended
`## Project notes (` section through EOF is excluded. A standalone `Evidence:` line
and its following list are excluded until a blank line or Markdown ATX heading
(one to six `#` characters followed by whitespace or end of line); that heading is
checked normally. There is no body note when `paths` is omitted or the project is not a git repository. The note is
advisory and does not change the declared paths.
Intake detects tracked tests outside `paths` that import or are named after in-scope
JS/TS or Python source files. New queueing through `--yes` appends all candidates
when there are at most 20; above that cap, none are added and a bounded advisory
lists the first ten names and the remaining count. Other intake modes remain
advisory, except check-only previews eligible additions. The detector can miss
indirect, inventory, golden and fixture tests; authors still need to grep for them.
Added test files participate in the scheduler's disjoint-paths check and the
`task-paths` scope gate just like owner-declared paths. Shared tests can therefore
serialize previously disjoint tasks, and the gate permits the whole test file.
Implementer scope prompts mark recorded additions as permitted scope, not required
edits: update only what the requested behavior forces, keeping assertions at least
as strict. Custom verify path guards and the SCOPE request mechanism are unchanged.

For ordered tasks, set owner-controlled dependencies in frontmatter:

```yaml
depends_on: [compose-network, compose-services]
```

Omitted or `[]` means no dependencies. IDs must match the task ID pattern; a
self-dependency is rejected at `add`/`intake`. Unknown IDs may be added later.

To be queued, a task also needs an existing git repository and a reviewer model that
differs from the implementer.

Every setup, verify and evidence command runs under `bash -o pipefail` and sees
`HELMRAISER_RUN_ID`, `HELMRAISER_WORKTREE`, `HELMRAISER_EVIDENCE_DIR` and
`HELMRAISER_BASE_SHA` (the recorded base commit), plus the task's `env`.
Verify steps should diff against `$HELMRAISER_BASE_SHA` rather than a local branch.
With `isolation: process`, setup, verify and evidence commands run inside `srt`
(see [Isolation](#isolation)). Setup has its own sandbox settings (package cache, registry access); an
explicit setup or step `sandbox: host` runs outside, which only a project with
`require_approval = true` allows.

**Failure output.** A passing step records the last `verify_tail_lines` lines of its
output. A failed verify step, or an evidence step that is not ok, records an excerpt of
the whole output instead, at most `verify_excerpt_lines` lines and
`verify_excerpt_chars` characters. First come the lines that look like test failures
anywhere in it, each with some context: TAP `not ok` and its YAML block, node:test `✖`
and `failing tests:`, vitest/jest `FAIL`, `●`, `×` and error headers, pytest failure
sections, `E` lines and `FAILED` lines, Python tracebacks. They take at most three
quarters of the caps. Then come the runner's totals (`# tests`, `ℹ fail`, pytest's
final line, `Tests  …`), then the tail. Kept lines stay in output order, and each cut
span reads `… N lines omitted …`. With no failure match the excerpt is the tail alone.
The full output goes to `logs/` in the run directory, and the feedback to the
implementer (and the review prompt, for evidence) names that file. The patterns are in
`src/helmraiser/loop/excerpt.py`.

**Environment hints.** Such a step also records `hints`: the known sandbox and
environment signatures that `helmraiser preflight` recognises (see
[Trying commands in the sandbox](#trying-commands-in-the-sandbox)), found anywhere in its
full output (`src/helmraiser/loop/hints.py`). A refused connection to a loopback port
gets a hint unless one of the run's `[[services]]` provides that port; the other hints
describe srt's policy and apply only to a step that ran inside srt. The implementer's
feedback lists them under `## Environment hints`, with the advice that code cannot
supply a missing service or permission and that it should answer `BLOCKED:` with the
reason rather than spend iterations on one. `summary.md` shows each step's hints under
its line in `Last verify` and `Evidence`, and a failed run's hints as `- environment
hint: …` lines after its failure reason; `work` prints them under the step's line.
`status --json` gives each failed row `hints`, those of the run's last verify results.

**Environment.** Runs do not inherit the worker's environment as is
(`src/helmraiser/isolation/environment.py`):

- Setup, verify and evidence commands get the worker's environment without
  secret-shaped names: `SSH_AUTH_SOCK`, `SSH_AGENT_PID`, `GPG_AGENT_INFO`, `CLAUDECODE`,
  `DBUS_SESSION_BUS_ADDRESS` (the desktop keyring), `DISPLAY`, `WAYLAND_DISPLAY`, and
  any name that matches, ignoring case, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`,
  `*PASSWD*`, `*_KEY`, `*APIKEY*`, `*API_KEY*`, `*CREDENTIAL*`, `CLAUDE_*`,
  `ANTHROPIC_*`, `OPENAI_*`, `GH_*`, `GITHUB_*`, `AWS_*`, `GOOGLE_*`, `AZURE_*`,
  `CLOUDFLARE_*` or `CF_*`.
- pi sessions (and the `srt` that wraps them) get only `PATH`, `HOME`, `USER`,
  `LOGNAME`, `SHELL`, `LANG`, `LANGUAGE`, `LC_*`, `TERM`, `TZ`, `TMPDIR`, the XDG base
  and runtime directories, `NODE_OPTIONS`, `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`,
  `SSL_CERT_DIR`, the HTTP proxy variables and pi's own `PI_CODING_AGENT_DIR`,
  `PI_PACKAGE_DIR`, `PI_OFFLINE`, `PI_SKIP_VERSION_CHECK`, `PI_TELEMETRY` and
  `PI_CACHE_RETENTION`, plus [Leeboard's optional environment](#leeboard-optional).
  Provider API keys are not passed; pi uses `~/.pi/agent/auth.json`.
- Both command and pi-session environments remove `VIRTUAL_ENV`, `VIRTUAL_ENV_PROMPT`
  and `PYTHONHOME`, plus every exact PATH entry matching the inherited
  `<VIRTUAL_ENV>/bin` or the directory of `sys.executable` when the worker interpreter
  is in a venv (`sys.prefix != sys.base_prefix`). Other PATH entries keep their order.
  Bare Python must not silently use the worker's editable install instead of worktree
  code. Select the project interpreter explicitly, for example `.venv/bin/python` or
  `export PATH="$PWD/.venv/bin:$PATH"` inside commands.

Both then add the `HELMRAISER_*` variables and the task's `env` merged over the project's
`[defaults.env]`. Naming a variable there is the opt-in for anything stripped or not on
the list, for example `GH_TOKEN = "$GH_TOKEN"` or `JAVA_HOME = "$JAVA_HOME"` (values are
expanded from the worker's full environment). Explicit `PATH` or `VIRTUAL_ENV` overrides
are preserved too. `helmraiser doctor` lists stripped variable names, never their values,
plus the stripped PATH entries.

## Project notes

A repository may keep owner-maintained sandbox and project lessons in
`.helmraiser/notes.md`. It is local and ignored with the rest of `.helmraiser`; it is not a
committed task requirement or a way to bypass the sandbox settings. Helmraiser reads it in its
host process, refuses symlinks in the path, and caps content supplied to models at 8
KiB.

The notes constrain scout discovery and verify hints, are appended to generated drafts,
constrain intake's proposed verify and evidence commands, and reach implement, plan and
review directives. If the identical notes text is already in a task body, Helmraiser does
not duplicate it. `helmraiser doctor [repo]` reports whether the file exists and its full
byte size.

For example:

```markdown
# Sandbox and project lessons

- The `tsx` CLI opens Unix sockets blocked by srt. Use
  `node --import tsx --test ...`, not `tsx --test` or npm scripts that call it.
- Keep commits Biome-clean; run Biome explicitly before committing with `HUSKY=0`.
- Tests, verify, and evidence must make no external calls.
```

## `project.toml`

Per-repository defaults live in `<repo>/.helmraiser/project.toml`. A task's own `setup`
replaces the project default, its `env` is merged key by key over the project's, and its
`delivery` and `base` override the corresponding project defaults. Values may reference
`$HELMRAISER_WORKTREE` and the other `HELMRAISER_*` variables.
Unknown keys are rejected with an error naming the key.

By default, run worktrees live outside the repository at
`<worktree_root>/<repo-name>-<hash>/worktree-<id>-<run>`, with adjacent `<run>.json`
metadata. The hash is the first 12 hexadecimal characters of SHA-256 of the resolved
repository path, keeping same-named repositories separate. The global `worktree_root`
key in `config.json` defaults to `null`, meaning `$HELMRAISER_DATA_DIR/worktrees`
(default `~/.helmraiser/worktrees`). A configured string expands environment variables
and `~`; relative roots resolve from the current working directory. Set it, for example,
to `/data/helmraiser-worktrees` when pnpm's package store must share that filesystem.

An optional project `worktree_dir` overrides the global root and repository suffix
entirely. It expands environment variables such as `$HOME`; relative paths resolve
from the repository. Use it for projects whose package store needs a different
filesystem. Keeping worktrees external also avoids nested-checkout failures in repo
scanners and projects whose tests reject a nested `.git`. Project state
(`.helmraiser/state.db`), tasks and `finalize.lock` stay in the repository.
See [`examples/project.toml`](../examples/project.toml):

```toml
# Optional concurrency opt-in; top-level, not a task default (global default: 1).
max_runs_per_project = 2

[defaults]
setup = "python3 -m venv .venv && .venv/bin/pip install -q -e '.[dev]'"
# Optional same-filesystem package-store override:
# worktree_dir = "/data/helmraiser-worktrees/my-project"
delivery = "pr"
base = "programme/x"
# Reject policy: auto during intake, queue validation, and work dispatch.
require_approval = true

[setup]
# npm/pip caches are automatic in sandboxed setup. Extra tool write roots only:
write = []
allowed_domains = ["pypi.org", "files.pythonhosted.org"]
# sandbox = "host" # unconfined; requires defaults.require_approval = true

[sandbox]
# Relative paths are denied in both the primary checkout and each run worktree.
deny_read = ["secrets", "~/private"]

[scout]
# Repository-relative path prefixes that scouts must not propose touching.
exclude = ["projects/legacy"]

# Optional: override the model profiles for this project's tasks.
[models]
implement = "opencode-go/deepseek-v4.1-flash:medium"
fallback = "openai-codex/gpt-6.1-sol:high"

[defaults.env]
PYTHONDONTWRITEBYTECODE = "1"
```

`[defaults] require_approval = true` rejects tasks with `policy: auto`: `intake` refuses
such a task file before asking the intake model, ignores `--allow-auto` for other tasks
(with a note), and `work` skips an auto task that is already queued.
It is also what allows a verify/evidence step, or `[setup]`, to say
`sandbox = "host"`; without it project loading or task validation fails. Unconfined
setup is marked by a `step_unconfined` event, `setup_sandbox: "host"` in the outcome,
and `ran unconfined (sandbox: host)` in `summary.md`.

The `[models]` table sets this project's profile defaults. Keys are profile names
(`intake`, `plan`, `implement`, `fallback`, `review`, `scout`, `judge`) and values are
`provider/id[:thinking]`. Precedence is a task's own `models:` block, then the project's
`[models]`, then the config's `profiles` (with the built-in defaults under them). Project
models are defaults, not explicit task choices, so dispatch routing treats a task without
its own `models.implement` as routable. An unknown key fails the load and names the key,
and the reviewer must still differ from the implementer after resolution.
For process isolation, `[sandbox] deny_read` adds read-denied paths while retaining
Helmraiser's built-in denials. Relative entries apply beneath both the primary checkout
and the run worktree; absolute and `~` entries are used as specified. A relative entry
(here or in the config's `isolation.deny_read`) that matches a file tracked in the
repository makes that file read as missing or empty inside srt, so pi could commit its
deletion. At run start `work` checks the entries with `git ls-files` in the primary
checkout; a match records a `sandbox_tracked_deny` event and a warning in the run's
`summary.md` and output, and the run goes on. `doctor` reports such entries for the
current repository.

If `setup` fails, the run fails with `setup: exit N: <last output line>`. When a simple
command failed during setup, the reason instead reads `setup: exit N (last failed command:
<command>) — <last output line>`; an ERR trap records the last failed command, which is
not necessarily the one that ended the script. The full setup tail stays in the
`setup_result` event and in the run's `summary.md`. Its default timeout is 600 s.
For a non-timeout failure inside srt containing `EROFS` or "read-only file system"
(case-insensitive), the reason uses the first matching output line instead of the last,
with the existing detail cap. The event and summary also show a hint to add the directory
to `[setup].write` or point the tool's cache at `/tmp`. Unconfined setup, timeouts and
unrelated failures keep their existing reasons and get no such hint.
`[setup].write` paths expand environment variables and `~`; relative paths resolve from
the primary repository. They add write roots only, not read carve-outs. Omitted
`allowed_domains` defaults exactly to `registry.npmjs.org`, `pypi.org` and
`files.pythonhosted.org`; an explicit empty list disables setup network access.

Process-isolated setup inside srt automatically uses Helmraiser-owned caches at
`<data-dir>/setup-cache/<project_slug(project)>/npm` and `/pip`. Before setup starts,
Helmraiser creates the cache directories and missing parents on the host with mode `0700`.
It defaults `npm_config_cache` and `PIP_CACHE_DIR` independently only when absent from
the setup environment: project/task overrides and inherited worker values (even empty
values) win. Only this project's cache root is added to setup's readable and writable roots;
the rest of the data dir remains denied. No registry domains are added.
Pi sessions, verify and evidence get no automatic cache variables or cache access.
`[setup] sandbox = "host"` and `isolation: none` do not prepare these caches.

pnpm's store is **not** managed. Choose a directory on the worktree's filesystem,
set `npm_config_store_dir` in `[defaults.env]`, and list it in `[setup].write`.
`helmraiser doctor` warns when the store is on another filesystem than the worktrees,
is missing from `[setup].write` for sandboxed setup, or is not named while setup runs
pnpm. For example, if `/data` also holds the worktrees:

```toml
[defaults.env]
npm_config_store_dir = "/data/.pnpm-store"

[setup]
write = ["$npm_config_store_dir"]
allowed_domains = ["registry.npmjs.org"]
```

## Model profiles and budgets

`~/.helmraiser/config.json` (or `$HELMRAISER_CONFIG_DIR/config.json`) is created on
first use. Fresh configurations contain no model IDs and `"subscriptions": {}`: no subscription is
metered or fetched live unless named under `subscriptions`. Missing keys are filled
in on load. The subscription block below is an opt-in example, not a default;
the other settings show the defaults.

`opencode-go` and `openai-codex` are known templates. `"opencode-go": {}` supplies
the complete Go limits table, window shares, resets and other template settings;
Codex supplies live-only settings. Explicit values override template keys, including
whole nested dictionaries.

```json
{
  "profiles": {
    "intake":    {"thinking": "high"},
    "plan":      {"thinking": "high"},
    "implement": {"thinking": "high"},
    "fallback":  {"thinking": "xhigh"},
    "review":    {"thinking": "high"},
    "scout":     {"thinking": "high"},
    "judge":     {"thinking": "high"}
  },
  "budget": {
    "max_cost_usd": 5.0, "max_minutes": 90, "max_iterations": 4
  },
  "subscriptions": {
    "opencode-go": {},
    "openai-codex": {}
  },
  "routing": {
    "target": 0.95,
    "margin": 0.02,
    "ramp_hours": 24,
    "scout": {
      "enabled": true,
      "focus_projects": [],
      "paused_projects": [],
      "max_pending_drafts": 3,
      "max_drafts_per_run": 3,
      "cooldown_hours": 24,
      "model": null
    },
    "plan": {"enabled": false},
    "implement": {"enabled": false, "codex_headroom": 0.15}
  },
  "intake": {"avoid_models": []},
  "verify_tail_lines": 120,
  "verify_excerpt_lines": 300,
  "verify_excerpt_chars": 30000,
  "review_feedback_rounds": 1,
  "first_model_time_share": 0.6,
  "plan_time_share": 0.25,
  "review_min_minutes": 10,
  "notify": true,
  "max_parallel_runs": 2,
  "max_runs_per_project": 1,
  "worker_poll_seconds": 30,
  "base_sync_interval_seconds": 300,
  "worker_exclude_projects": [],
  "worker_restart_on_code_change": true,
  "pi_bin": "pi",
  "pi_rpc_implement": false
}
```

- **Implement transport.** `pi_rpc_implement` (default `false`) runs implement-stage
  pi sessions over RPC when enabled; steering is not yet available.

- **Intake model guidance.** Global `intake.avoid_models` is a list of model-ID
  patterns, default `[]`. A non-empty list adds prompt guidance naming those entries;
  it is not an enforced model-selection filter.

- **Worker settings.** `max_runs_per_project` (default 1) limits nonterminal runs per
  project; a top-level setting of the same name in `project.toml` overrides it. Both
  values must be positive integers. `max_parallel_runs` is how many child processes the long-running worker
  (`work --watch`, `worker start`) runs at once; `--max-parallel N` overrides it for one
  invocation. `worker_poll_seconds` is how often it re-checks the queue while nothing is
  eligible. `base_sync_interval_seconds` (default 300) independently limits attempts
  to synchronize dependency-held PR bases per (project, local base branch), including
  failed or skipped attempts. Its cooldown lasts for the supervisor's lifetime.
  `worker_exclude_projects` lists repositories that `work` without
  `--project` leaves alone (foreground and background alike), for a project whose runs
  another session drives with its own `work --project <repo>`. Paths match the task
  files' `project` with `~` expanded. An explicit `--project` wins over the list.
  `worker_restart_on_code_change` (`true` by default) lets `work --watch` restart itself
  when a commit changes Helmraiser's code (see [Running the queue](#running-the-queue)).

- **Escalation ladder.** The implement model gets `max_iterations` verify iterations in
  one pi session. With a different fallback configured, its sessions are capped at the
  first `first_model_time_share` of the run (0.6 by default). A timeout or exhausted time
  share preserves partial work in a commit and escalates. Helmraiser also escalates when
  the iterations run out. The fallback gets a fresh session, the failure history and
  half the iterations. If those also run out, the run fails with `verify_exhausted`.
  After verify passes, parsed, non-uncertain reviews with any findings—including
  minor-only `pass` reviews—are fed back to the current implement model for half-size
  fix rounds. `review_feedback_rounds` bounds these rounds (default `1`); set it to `0`
  to disable review feedback. Only remaining severe findings can trigger review-driven
  escalation once to a different fallback model.
- **Helmraiser's own commits.** Changes pi leaves uncommitted are committed as
  `helmraiser: iteration 2 of <task>` (also `review feedback …` and `partial work of …`).
  When most of the last 20 non-merge subjects at the run's base commit are Conventional
  Commits (`type(scope)!: subject`), the prefix is `chore(helmraiser): ` instead.
- **Per-run caps.** Cost, wall time and iterations are checked before each spawn. Cost
  is also checked while pi streams, at each `message_end`. A breach fails the run with
  `budget:<cost|time|iterations>`; the `budget_exceeded` event of an iterations stop also
  has `iterations` (used), `limit` and `limit_source`: `max_iterations`, or
  `rework_allowance` when a rework's allowance ran out first (`helmraiser show <run>` reports
  the same figures for any run, see [Showing one run](#showing-one-run)). Review sessions receive at least
  `review_min_minutes` (10 by default), even when less run time remains; they may exceed
  the run's wall-time cap by that allowance.
- **Subscription usage.** Metering is opt-in: only providers named under
  `subscriptions` are metered or fetched live. Providers with a limits table are
  metered per window from the ledger; live-only subscriptions use live readings. Each model's spend counts against its own monthly limit, so
  usage is `sum(cost / (monthly_limit_of_model * window_share))` — the same one
  percentage per window the provider's dashboard shows. Windows: rolling 5 hours,
  weekly at `week_reset` and monthly at `month_reset` (UTC). A window at or over
  `warn_at` prints a warning line (usage %, the threshold, the reset time) in `caps`,
  `status`, `report` and the `work` batch summary. `stop_at` (null by default) is the
  only hard stop Helmraiser applies itself: with enforcement on, `work` skips a task
  whose implement, fallback or review model is on that subscription when any window's
  usage plus the task's projected usage (`min(max_cost_usd, projection_usd) / (limit of
  its implement model * share)`; `projection_usd` defaults to $1, because `max_cost_usd`
  caps the whole run, mostly codex review and fallback) would reach `stop_at`, and
  continues with the next task. Such a task is held until the window resets. A task
  whose projected usage *alone* reaches `stop_at` in some window can never start: the
  skip reason is `never_startable:<provider>:<window>` and the log says `will never
  start`, naming the model, the projection and `stop_at`. `status` lists these tasks
  (`Never starts:` lines, `never_startable` in `status.json`), and with enforcement on
  `intake` refuses to queue one (`--propose` notes that `--yes` will refuse it); with
  enforcement off it only adds a note. Fix it by lowering the task's `max_cost_usd` or
  the subscription's `projection_usd`, raising `stop_at`, or picking a model with a
  larger monthly limit. Use
  `work --no-caps` for one invocation. Unnamed providers stay unmetered with no
  live fetches: ledger dollar-spend reporting only.

- **Routing headroom.** For each subscription window with a reading and known reset,
  Helmraiser reports `headroom = target × elapsed − used`, where `elapsed` is the share
  of the window that has passed. `routing.target` defaults to 0.95 and is clamped to
  the subscription's `stop_at`. During the final `routing.ramp_hours` (24 by default),
  headroom is instead `target − used`. A subscription has headroom only when every
  reported window is strictly greater than `routing.margin` (0.02 by default); missing
  or incomplete readings fail closed. Manual and worker-driven scouting use this
  headroom as a start gate. When `routing.scout.enabled` is true, `work --watch` uses
  spare child slots to scout idle `focus_projects` in worker scope, excluding
  `paused_projects`, while respecting `max_pending_drafts`, `cooldown_hours`, and the
  recorded HEAD.
- **Dispatch routing (opt-in).** `routing.plan.enabled` and
  `routing.implement.enabled` both default to `false`. When planning routing is enabled,
  a task that omits `plan` gets an unapproved plan stage if its effective plan model's
  subscription has headroom. Pacing keeps bursty use from ever showing headroom, so
  `routing.plan.max_used` (unset by default) also plans while every reported window of
  that subscription is used below that fraction, recorded as `plan_under_max_used`.
  Explicit `plan: false` (or `true`) is preserved; routing adds no plan approval and does
  not change the task's policy.
  When implementation routing is enabled, a task without its own `models.implement`
  uses the configured **fallback profile** as its initial implementer when either:
  - the implement profile's subscription has no headroom and the fallback's has; or
  - the task's `size` is exactly `M` or `L`, the fallback subscription has headroom,
    and the minimum headroom across its reported windows is strictly greater than
    `routing.implement.codex_headroom` (default `0.15`, i.e. 15 percentage points).

  The first trigger does not require `size`; tasks without size never take the second.
  Missing or incomplete fallback readings prevent selection; missing implement readings
  count as no implement headroom. Explicit implementers always win, even when equal to
  the default. A candidate matching the effective reviewer model is never selected
  (thinking level does not make it a different model). Other roles, including the task's
  fallback, are unchanged. Routing runs before provider holds and caps at fresh dispatch;
  the supervisor previews the same choices. Routed choices remain fixed on resume/retry,
  without editing the task snapshot. Each enabled lever records a `routing` event,
  including keep decisions, and a line in `summary.md`. `helmraiser log <run>` shows the
  decisions; `helmraiser log <run> --json` retains complete headroom figures.
- **Overflow implementer (opt-in).** `routing.implement.overflow` is an unset-by-default
  `provider/id[:thinking]` string, independent of `routing.implement.enabled`. A task
  without its own `models.implement` starts on this model when any window of the implement
  profile's subscription has `used >= routing.implement.overflow_at` (default `0.9`, read
  only when overflow is set), or the implement provider is under a hold. This is usage,
  not pacing headroom: codex behind linear pace does not overflow, and a missing reading
  does not count. The overflow's own subscription needs at least one measured window,
  with every measured window strictly below its target (`routing.target` clamped to its
  `stop_at`), and its provider must not be held.

  Overflow is evaluated before the two fallback triggers and wins when selectable;
  the fallback and reviewer stay as configured. The reviewer must differ from the
  overflow model (thinking level does not make it different). When the reviewer's
  provider is under a hold, the task waits (`quota:<provider>`), as for any task: with
  the implement profile and reviewer both on codex, a codex hold keeps the task queued.
  Selection records `reason: implement_overflow`; the `routing` event also has an
  `overflow` object with `model`, `outcome` and `trigger` (`near_limit`, `provider_held`
  or `null`). Keep outcomes are `overflow_not_needed`, `overflow_usage_unknown`,
  `overflow_no_headroom`, `overflow_held`, `overflow_same_provider`, `invalid_overflow`,
  `review_held` and `reviewer_equal`; explicit implementers and the existing provenance
  and profile guards still win.

### Spend and caps

`helmraiser caps` shows per-subscription usage %, elapsed window share, routing headroom,
spend, reset time, `warn_at` and `stop_at`, plus dollar spend for unmetered providers.
`status` shows the same elapsed/headroom values compactly on its usage line. OpenCode Go
first reads Omarchy's `opencode.json` under `$XDG_STATE_HOME/omarchy/agents/usage/`
(default `~/.local/state/omarchy/agents/usage/`). Records older than 15 minutes or marked
`limitsStale` are refreshed with `omarchy-agent-usage-opencode --limits-only`; stale
records remain usable when collection fails. Without usable record windows, Helmraiser
falls back to the OpenCode Go usage API using `OPENCODE_API_KEY` or pi's
`~/.pi/agent/auth.json` key. Codex limits come from Omarchy's `codex.json` record,
refreshed the same way with `omarchy-agent-usage-codex --limits-only`.

Live values are used for warnings and stops while the ledger estimate remains visible
in the same usage cell and supplies per-model attribution. Live results are cached for
five minutes in `<data-dir>/live-usage.json`; `helmraiser caps --refresh` bypasses the
cache. Set a subscription's `live` field to null to disable its source.

`helmraiser caps on|off` stores an explicit enforcement switch in the ledger;
`helmraiser caps reset` clears the switch, and enforcement is then on. Enforcement only
matters when a subscription sets `stop_at`. All forms support `--json`.
- **Scout.** The implementer may run a read-only `pi --print` with the `scout` model for
  surveys. The implementer directives are in `src/helmraiser/pi/directives.py` and include
  the optional [Leeboard brief](#leeboard-optional).

### Stage skill folders

By default every worker pi session loads the skills pi discovers itself. A stage folder
`~/.local/share/helmraiser/skills/<stage>/` replaces that discovery for one stage. The
stages are `intake`, `scout`, `plan`, `implement` (the fallback model's sessions too) and
`review`; `work --dry-run` shows the implement flags. The review replay's own sessions
and the blind comparison judge do not read the folders.

- **No folder:** no change; pi discovers skills as usual.
- **Folder present:** the session runs with `--no-skills`, then one `--skill <dir>` for
  each child directory that holds a `SKILL.md`, sorted by name. Symlinked directories
  count; other entries are ignored. The project's own skills follow: the `.pi/skills` and
  `.agents/skills` skills from the session's directory up to its repository root,
  nearest first.
- **Empty folder:** `--no-skills` and the project's own skills only.

```text
~/.local/share/helmraiser/skills/
  plan/tdd -> ~/projects/skills-hub/tdd
  implement/tdd -> ~/projects/skills-hub/tdd
  review/            (empty: project skills only)
```

Under process isolation each passed skill directory, and every symlink target inside it,
is opened for reading (`allowRead`), so a skill linked from a project that
`isolation.projects_root` denies still loads. Credential denials, configured `deny_read`
and Helmraiser's own data stay closed.

Helmraiser ships no worker skills. A suggested starting set, not yet evaluated: `tdd`,
`diagnosing-bugs`, `codebase-design`, `domain-modeling` and `writing-for-agents` for
`plan` and `implement`; `codebase-design` for `scout`; an empty folder for `intake` and
`review`.

## Policies

The run sequence is setup → optional plan → implement → path scope check → verify → evidence → review →
policy decision. With `plan: true`, the planning model writes a detailed plan and the
implementer starts immediately; `approve-plan-first` instead parks after planning until
the owner approves.

When `paths` is declared, Helmraiser checks the cumulative net changes from the
captured base commit to the worktree after every implement iteration, before
acceptance commands run. This includes committed, staged and unstaged changes,
deletions and untracked non-ignored files; both old and new rename paths must be
allowed. Ignored untracked run artifacts are excluded naturally. Tracked files
are not exempt merely because they live in `.helmraiser` or another configuration
directory.

A scope failure skips acceptance commands and feeds every offending path and all
allowed globs back to the implementer to restore/remove forbidden changes. It
consumes an iteration like a verify failure, including normal fallback and
exhaustion behavior. Complete diagnostics are recorded in ledger events,
summaries and reports, even after repair or review-round rollback. The same gate
applies to review fixes and verify-only retries. Implementer and review prompts
both list the declared scope.

**Scope requests.** When the implementer needs a file outside `paths` (often an existing
test that encodes the old behaviour), the directive tells it not to edit the file, to
commit its in-scope work and to answer with the first line `SCOPE: <path>[, <path>…] —
<reason>`. In an implement iteration with no out-of-scope changes, Helmraiser commits any
uncommitted work, records `scope_requested` (`{n, paths, reason}`) and parks the run as
`awaiting_approval` with a pending `scope` approval, keeping its worktree and branch. It
runs no verify and no further iteration. Requested paths that `paths` already covers are
dropped. A request whose iteration also changed out-of-scope files fails at the path
scope check (`failed`, `verify_failed: task-paths`). A request that names no valid new
relative path, or comes from a task without `paths`, fails as `blocked: …`.
`helmraiser approve <run>` adds the requested paths to the `paths:` list of the task file
with a minimal text edit (the rest of the file is left as it is), re-reads the task as
`rework` does and hands the run back at `resuming` / `implement`. `--paths a,b` approves
only those of the requested paths; any other path is refused (exit 2) and the file is
left unchanged, as it is when the edited task is invalid. `--paths` on any other
approval is refused (exit 2). The resumed run continues in the implement session that asked (the
fallback's, when the fallback asked) with the prompt `The owner approved these paths:
…. Continue the task.`, followed by the approval's `--note` if given; `run_resume`
records `"from": "scope"`.

`helmraiser approve <run> --decline --note "<why>"` instead answers no without ending the
run. It requires a non-empty, non-whitespace `--note`, refuses `--paths` and any run not
awaiting a scope approval (exit 2). It does not touch `paths:` or any other byte of the
task file. It re-reads and validates the owner's task file before deciding, so edits to
the body or verify contract become the ledger snapshot and the run's `task.md`; an
invalid file leaves the run parked without recording a decline. The pending scope
approval row becomes `decision: "declined"` with the note. The `handed_back` payload is
`{"stage": "implement", "scope": {"paths": [], "declined": [<requested paths>], "note": <note>}}`.
The run resumes in the requesting implement session (the fallback's when it asked),
keeping its branch's commits. Its first implement prompt starts
`Scope request declined (<paths>): <note>` (paths joined by `, `), tells it not to edit
the declined files, to find another way inside the allowed paths and to read the task
file again. The reviewer receives every approved or declined scope decision, oldest first,
under `## Owner's scope decisions`, including the actual approved subset or declined paths
and the owner's note, if given. These decisions override the task brief and acceptance
block where they conflict; following them is not a finding. Each note is limited to
16 KiB of UTF-8 text, with an explicit cut marker when truncated. `log` shows the decline
in the timeline.
If the implementer asks for the same or any other paths again, it parks as a normal new
scope request with a new pending approval; the earlier row stays `declined`. No declined
paths are remembered to refuse later requests automatically.
Unlike a decline, `helmraiser reject <run>` ends it `failed` (`rejected`).
`summary.md`, `report`, `work` and `status` show the pending request's paths and reason,
and the JSON of `work`, `report` and `status --json` awaiting rows add `scope_request`
(`{paths, reason}`).
The plan stage can also raise `SCOPE:` when it would otherwise proceed straight to
implementation: Helmraiser saves the following full plan, parks without recording an
iteration, and starts implementation with that plan after approval or decline. With
`approve-plan-first`, needed out-of-path files instead belong in the plan's open questions
for the owner; review-fix rounds still do not recognise `SCOPE:`.
A task without `paths` has no allowed paths to ask for, so its plan directives and plan
prompt mention neither `SCOPE:` nor allowed paths. If the planner of a `plan: true` task
starts its final message with a `SCOPE:` line anyway, Helmraiser drops the line, records
`scope_line_ignored` (`{line}`; `log` shows the line) and keeps the plan that follows. With
no plan after the line, the run fails with `plan: the planning session produced no plan`.
An `approve-plan-first` plan is saved as written.

Declared paths are also what lets runs of one project
overlap: above the default `max_runs_per_project` of 1, a sibling run starts only when
every nonterminal run of the project declares `paths` and their scopes are disjoint
(see [Running unattended](#running-unattended)).
Variants of one task, which compare model setups side by side from the same base, set
`variant_of: <task id>` in their frontmatter: a run of one variant that awaits approval
keeps its own worktree and branch and does not hold another variant with the same
`variant_of`. A running variant still holds as usual.

After verify passes, Helmraiser runs the evidence steps. Every file in
`$HELMRAISER_EVIDENCE_DIR` is shown to the reviewer. Text files up to 8 KiB are inline
until a 64 KiB cumulative limit; others are available by path. Oversized JUnit XML
(`testsuites`/`testsuite` roots) is supplemented by an inline summary of each suite's
`tests`, `failures`, `errors`, `skipped` and `time` totals, every failing or erroring
test with the first lines of its message, and up to 300 test names with a count of the
rest. When a summary does not fit the inline budget, the complete failure list is
written to a `.readable.txt` sidecar beside the original and an explicit excerpt is
inlined. Other oversized text with very long lines gains a line-wrapped `.readable.txt`
copy referenced in the prompt. Original evidence is never modified; malformed XML and
binary files keep the path-only fallback. A different model then reviews the
diff, the verify results and the evidence in a clean, read-only context. It answers
`pass`, `concerns` or `uncertain`. A `pass` is downgraded when an evidence check is not
met or not assessed. A missing or malformed verdict, a failed review session, or a failed
evidence step forces `uncertain`. Any findings, severe or minor, in a parsed review
that is not `uncertain` trigger a fix/verify/evidence/review round before policy is
decided (bounded by `review_feedback_rounds`); remaining severe findings may trigger the fallback.
A round that cannot verify or exceeds the budget is rolled back to the reviewed commit.
For findings that remain after the rounds, an uncertain verdict, or your own concerns, use
`helmraiser rework <run> --note "<feedback>"` to hand a run awaiting merge approval back to the implementer
rather than fixing it by hand or ending it with `reject`.

| Policy | Outcome |
|--------|---------|
| `review` | Always `awaiting_approval`; branch and worktree are kept for `approve` / `reject` |
| `auto` | Lands (fast-forward merge into the base branch, worktree removed) only when the verdict is `pass`. Any other verdict downgrades to `awaiting_approval` |
| `approve-plan-first` | A plan session runs first and the run waits with a pending `plan` approval. `approve` records the approval, appends its `--note` to `plan.md` (which the implementer reads) and hands the run back to the worker: the run becomes `resuming` at stage `implement` and `approve` returns at once. The next `work` loop resumes the same run and worktree at the implement stage. After that, the run waits for merge approval as with `review` |

**The `auto` rule:** `policy: auto` is valid only with `isolation: process` (or `vm`)
**and** at least one evidence step. `process` isolation needs `srt` installed (see
below). `vm` is not available, so in practice `auto` means `isolation: process`.

**Delivery.** `delivery: merge` performs the local fast-forward described above.
`delivery: pr` pushes the run branch and runs `gh pr create` against the run's captured
base branch. It removes the worktree, keeps the local branch, and records the run as
`landed` with its `pr_url` as soon as the PR opens, not when it merges. PR delivery
requires `gh` on `PATH` and logged in, and does
not require the primary checkout to be clean because it does not merge into it.

Before rebasing for PR approval, `approve` and `land` synchronize the local base with
its configured upstream, fast-forwarding when safe. Approval refuses if it cannot
prove the local base contains that upstream; dirty or other-worktree checkouts are
never moved by this check.

**Landing a pull request.** `helmraiser land <run-or-unique-prefix> [--json]` takes a
`delivery: pr` run awaiting **merge** approval (or an already approved run with a PR)
through preflight, optional [Leeboard holds](#leeboard-optional), the same delivery as `approve`, CI,
and a fresh fetch of `origin/<base>`. It requires that fetched base to be an ancestor
of the run head and the remote PR branch to equal that head, then non-force pushes
that exact head to `origin`'s run base, attempts a safe local fast-forward, and confirms
the PR shows `MERGED`. No `gh pr merge`, force-push or branch deletion is used.
The base must have no upstream configured or track exactly `origin/<base>`.
Direct write permission to `<base>` is required: a protected base can reject the push,
and `land` reports git's refusal.

CI polling defaults to `--timeout 1800` seconds and `--interval 10` seconds; interval
`0` is allowed. Missing checks are retried for a 60-second grace before failing closed.
Use `--no-ci` only for repositories without CI: it accepts absent checks immediately,
but existing checks still must pass. A failed or timed-out landing leaves the approved
run `landed` with its PR; re-running is safe and skips approval. An already merged PR
returns without refreshing the local base. Local checkout warnings do not fail a
successful remote merge; follow the printed manual command in a clean base checkout.
PR dependencies remain held until the local base contains the recorded head.

Before creating a **new PR run's worktree**, the host runner fetches the base branch's
configured upstream (not necessarily `origin` or the same branch name), with a
60-second timeout, and fast-forwards the local base only if it is an ancestor of
that upstream. A base checked out in the primary checkout must have no tracked
changes, including tracked `.helmraiser` files; a successful fast-forward updates
its working tree and index too. Untracked files alone do not block synchronization,
but an incoming tracked file collision can cause the update to fail safely.
An unchecked-out base is updated directly; a base checked out in another worktree
is never touched. Ahead/diverged bases, missing upstreams, and fetch/update failures
retain the local base and **do not prevent the run from starting**. Ledger and JSONL
`base_sync` events record `synced`, `skipped`, or `failed`, the reason, and applicable
from/to and upstream commits. Merge delivery and resumed runs do not synchronize.
This synchronization eligibility is separate from PR delivery's cleanliness rules.

Before selection, the worker also synchronizes queued PR tasks held **only** by
`depends_on`, when every unmet dependency is `not_in_base`. It uses the same upstream
and safe fast-forward checks, without creating a run. These no-run outcomes appear
in CLI output / `worker.log`, not ledger run events. Foreground `work` checks once
per invocation; dry runs and supervised children skip this preselection check.

Here `landed` records delivery, not inclusion in a dependent task's local base:
PR dependencies stay held until a merge containing the recorded head reaches that
local branch. Squash merges never satisfy this recorded-head ancestry check.

## Isolation

The `isolation` level applies per task:

- `none`: setup, pi, verify and evidence run as you in the worktree. They can read other
  projects. Opt in per task; the default is `process`, and intake proposals cannot
  lower it.
- `process`: every pi session runs as
  `srt --settings <run-dir>/srt-settings.json -- pi …`, that is inside
  [sandbox-runtime](https://github.com/anthropics/sandbox-runtime) (`srt`) over
  bubblewrap. The per-run sandbox settings are described below. It allows writes only to the
  worktree, the run's `sessions/` directory, `/tmp`, the primary checkout's `.git`
  (so pi can commit; `.git/hooks` and `.git/config` stay read-only) and `~/.pi/agent`
  (pi's lock files and OAuth refresh; every entry that already exists there except
  `auth.json` stays read-only), plus optional [Leeboard access](#leeboard-optional).
  Network access is limited
  to `isolation.allowed_domains` in `~/.helmraiser/config.json` (default: the OpenAI,
  ChatGPT and opencode endpoints plus `github.com`).
- `vm`: reserved. It always reports unavailable.

Helmraiser removes untracked empty srt mount-point placeholders at the worktree root after sandboxed steps, including timeouts, and before automatic commits, preserving tracked and non-empty paths.

Helmraiser sets repository-local `maintenance.auto=false` before fresh runs: automatic
ref packing can remove branch-ref directories bind-mounted into live sessions. The
watch worker runs `git maintenance run --auto` during its hourly sweep only when the
project has no live run, skipping projects whose directory no longer exists.
`helmraiser doctor` warns when a repository lacks the setting.

**What a sandboxed pi cannot read.** Reads are allowed everywhere except:

- Credential and private locations, denied whether or not they exist: `~/.ssh`,
  `~/.secrets`, `~/.gnupg`, `~/.aws`, `~/.config/gh`, `~/.config/.wrangler`, `~/.config/rustdesk`,
  `~/.claude`, `~/.codex`, `~/.gemini`, `~/.config/opencode`, `~/.local/share/opencode`,
  `~/.config/kilo`, `~/.local/share/kilo`, `~/.local/share/keyrings`,
  `~/.password-store`, the Chromium, Chrome, Brave and Firefox profiles, `~/.docker`,
  `~/.npmrc`, `~/.pypirc`, `~/.netrc`, `~/.git-credentials`, `~/.kube`, Tailscale's
  state, `~/.helmraiser`, `~/.pi/agent/sessions` (pi's interactive sessions; `auth.json`
  and the rest of `~/.pi/agent` stay readable).
  Keep credentials an interactive agent needs but Helmraiser runs must not read in
  `~/.secrets/<project>/`.
- The data directory that holds the run directory: the ledger, the config and every
  other run. The run's own directory stays readable.
- `isolation.deny_read` in `~/.helmraiser/config.json`, then the project's
  `[sandbox] deny_read`. Both take absolute, `~` and relative paths; relative entries
  apply beneath both the primary checkout and the run worktree.
- Other projects. For each directory in `isolation.projects_root` (a path or a list),
  every top-level directory is denied except the one that holds the task's repository
  (or its worktree, run directory or git common dir). A repository at
  `<root>/alpha/app` keeps all of `<root>/alpha` readable.
  Without `projects_root`, other projects stay readable.

A denied directory that holds something the run needs, such as `~/.helmraiser` around the
run directory, is denied entry by entry around it (srt cannot re-open a write path
beneath a read-only carve-out). What pi loads from other projects stays readable: the
local paths in `~/.pi/agent/settings.json` (`packages` and the resource lists) and the
targets of symlinks in `~/.pi/agent` and its `skills`, `prompts`, `themes` and
`extensions` directories, such as the global `AGENTS.md` and linked skills, unless they
lie inside a credential or configured denial. So do the skills of a
[stage skill folder](#stage-skill-folders). See also [Leeboard access](#leeboard-optional).

```json
{
  "isolation": {
    "projects_root": ["~/projects/personal", "~/projects/work"],
    "deny_read": ["~/vault", ".env.local"]
  }
}
```

Install `srt` for `process` isolation (Linux; it also needs `bwrap`, `socat` and
`rg` on `PATH`):

```bash
npm install -g @anthropic-ai/sandbox-runtime
```

Helmraiser runs `srt` by an absolute path resolved once per process and refreshed if
the executable disappears; set `isolation.srt` in `config.json` to an absolute
executable path to override it, and use `helmraiser doctor` to see the path.
When `PATH` yields a mise shim, Helmraiser replaces it with the real executable from
`mise which srt`, run in your home directory so a project's own `mise.toml` cannot
break resolution.

Set `isolation.tmpdir` in Helmraiser's `config.json` to choose writable temporary
storage for every sandboxed session and step (default `/tmp/claude`). It accepts
an absolute path with `~` expansion; choose a directory on disk when `/tmp` is a
small tmpfs. The directory is shared by all runs and is not cleaned by Helmraiser.
If a denied path disappears before bubblewrap starts, Helmraiser rebuilds the sandbox
settings and retries once, recording `sandbox_retry`; commands that have started are not retried.

**What runs where under `process`.**

| Step | Where | Writes | Network |
|------|-------|--------|---------|
| pi sessions (implement, plan, review; intake) | `srt`, `<run-dir>/srt-settings.json` | as above (intake: its session dir and `~/.pi/agent`) | `allowed_domains` |
| `setup` | `srt`, `<run-dir>/srt-settings-setup.json`, before pi starts | worktree, `/tmp`, per-project npm/pip cache, `[setup].write` | `[setup].allowed_domains` |
| `verify`, `evidence` | `srt`, `<run-dir>/srt-settings-verify.json` / `srt-settings-evidence.json` | the worktree, `<run-dir>/evidence` and `/tmp` | `allowed_domains`, plus `127.0.0.1:<port>` of each service through a relay |
| a step with `sandbox: host` | outside `srt` | anywhere you can | unrestricted |
| project services (`[[services]]`) | `docker run` on the host, before implement | the container | published on `127.0.0.1:<port>` only |

Verify and evidence run code that pi wrote, so they get the same read denials as pi,
without the carve-outs for what pi itself loads. They cannot write the rest of the run
directory (the diff, events and summary the reviewer and you read), the git common dir
or `~/.pi/agent`. Unix sockets stay blocked (srt's seccomp filter; Helmraiser never sets
`allowAllUnixSockets`), stdin is `/dev/null`, and the environment is the one described
under Environment, without secret-shaped names. A tool that opens a Unix socket of its
own, such as the `tsx` CLI's IPC server, fails there with `EPERM`; a tool using a
model cache under a denied directory cannot see its cache. Point such a tool at the worktree or `/tmp` from `setup` or `[defaults.env]`.

Browsers: full Chromium aborts inside srt, even with `--no-sandbox`, because its process
singleton opens a Unix socket (`process_singleton_posix: socket() Operation not permitted`).
The `chrome-headless-shell` build runs: a puppeteer UI script and a Playwright suite
(`launchOptions.executablePath` pointing at
`~/.cache/puppeteer/chrome-headless-shell/…/chrome-headless-shell`) passed under the
verify sandbox settings in `helmraiser preflight`. Try a browser step there before queueing it.

`sandbox: host` on a verify or evidence step runs it outside `srt`, with the same
environment. It is valid only when the project's `.helmraiser/project.toml` sets
`require_approval = true`, and intake never proposes it. Such a step's result carries
`"sandbox": "host"`, the run records a `step_unconfined` event, and `summary.md` and the
reviewer's prompt mark it `ran unconfined (sandbox: host)`. Every verify and evidence
result records where it ran: `srt`, `host` or `none` (`isolation: none`).

Setup uses the same read denials as the other run policies, except for its owned
per-project npm/pip cache. It has no pi read carve-outs, Git/run-directory writes,
service ports or Unix sockets. It gets the environment without secret-shaped names,
plus absent-only defaults for `npm_config_cache` and `PIP_CACHE_DIR`. `[setup] sandbox = "host"` is the approval-gated, marked escape
hatch; `isolation: none` remains unconfined.

`work` refuses to start a task whose isolation level is unavailable. For `process`,
`srt --version` and `bwrap --version` must succeed and `socat` and `rg` must be on
`PATH`.

### Services for verify

A project whose tests need a database or another TCP service declares it in
`.helmraiser/project.toml`. Helmraiser runs each one with Docker on the host; verify and
evidence get the variables it `provides`:

```toml
[[services]]
name = "postgres"                    # a-z, 0-9 and '-'; container helmraiser-<run8>-postgres
image = "postgres:16"
env = { POSTGRES_PASSWORD = "helmraiser", POSTGRES_DB = "app_test" }  # expanded like [defaults.env]
port = 5432                          # the container's TCP port
host_port = 55440                    # optional: a fixed 127.0.0.1 port (default: a free one)
command = ["postgres", "-c", "fsync=off"]  # optional
ready_timeout = 60                   # optional, seconds (default 60)
ready = ["pg_isready", "-h", "127.0.0.1", "-U", "postgres"]  # optional, run with docker exec
provides = { DATABASE_URL = "postgresql://postgres:helmraiser@{host}:{port}/app_test" }

[[services]]
name = "qdrant"
image = "qdrant/qdrant:latest"
port = 6333
provides = { QDRANT_URL = "http://{host}:{port}" }
```

Projects raising their run limit must not make verify/evidence commands depend on
fixed host ports, fixed container names, or fixed Compose project names. Use
Helmraiser-provided service URLs and run-specific names instead. Explicit service
`host_port` settings remain fixed and can therefore conflict between siblings.

`{port}` is the host port Helmraiser chose (or `host_port`, for a project whose tests
hard-code one; a taken fixed port fails the run instead of moving) and `{host}` is
`127.0.0.1`. `ready` is an
argv that must also exit 0 inside the container before the service counts as ready,
for a server that accepts TCP before it accepts queries (Postgres) or to check a reply
(`["redis-cli", "ping"]`). Unknown keys, duplicate names, a variable provided twice, a
duplicate `host_port`, a port outside 1–65535, an empty image, a `ready` that is not a non-empty list of
strings or another placeholder make the file invalid.

- **Lifecycle.** After setup (and after an approved plan), before the first implement
  session, Helmraiser picks a free port on `127.0.0.1`, runs
  `docker run -d --name helmraiser-<run8>-<name> --label helmraiser.run=<run id> -p
  127.0.0.1:<port>:<container port> -e NAME … <image> <command…>` (the `env` values
  reach Docker through its environment, not its argv), reads the mapping back with
  `docker port`, and waits until the port holds a TCP connection open and, with
  `ready`, `docker exec <container> <ready…>` exits 0. A service that
  cannot start fails the run with `services: <name>: docker run failed: <Docker's error>`, one that
  does not answer in time with `services: <name> not ready after <n>s`, before any
  model time is spent. Helmraiser tries up to three free ports when Docker reports the port
  taken. A taken fixed `host_port` is not retried: the failure names what holds it, a
  running container (`docker ps --filter publish=<port>`) or else a listening process
  (`ss`), when it can tell. `helmraiser retry` accepts these failures. The run records `service_start`, `service_ready`,
  `service_failed` and `service_stop` events; `summary.md` and the outcome list each
  service with its port and the names it provides.
- **Cleanup.** Every way a run ends (landed, awaiting approval, failed, aborted, over
  budget, an error, a cancelled worker) removes the containers labelled with its run
  ID, with their anonymous volumes (`docker rm -f -v`). If that fails, the run gets a
  warning. The next `helmraiser work` removes labelled containers whose run this ledger
  knows and is over; a container of a run the ledger has never seen belongs to another
  data directory and is left alone. `helmraiser doctor` lists both.
- **Under `process`.** Each verify or evidence step's sandbox settings allow the literal
  `127.0.0.1:<port>` of each service, and the step starts behind one `socat` relay per
  service, inside srt: it listens on the same port in the sandbox and forwards through
  srt's proxy, whose address and credentials it reads from `HTTP_PROXY` at runtime.
  Clients connect to `127.0.0.1:<port>` as usual, database drivers included. The
  relays stop when the step ends; the step's exit status is kept, and relay messages
  are appended to its output without any line that holds the proxy credentials. A
  bootstrap failure (no `socat`, no srt proxy, a relay that never listens) fails the
  step with exit 125.
- **Elsewhere.** A `sandbox: host` step and an `isolation: none` task get the same
  variables and connect to the published port directly. pi sessions get neither the
  variables nor the ports. The implement directive names the provided variables, says
  that only verify and evidence get them, and tells the implementer not to start
  containers itself, nor to run the verify steps that need them or answer `BLOCKED:`
  over them, but to commit and stop. A `BLOCKED:` answer that leaves a new commit and a
  clean tree in a run with services goes to verify (event `blocked_to_verify`); if
  verify fails, the run fails with that `blocked: …` reason, with no further iteration.

Security notes:

- The private `0700` setup npm/pip cache is shared between runs of one project,
  never between projects. It is not the user's own `~/.npm` or pip cache. Only that
  project's sandboxed setup gets access; pi, verify and evidence do not.
- Unix sockets stay blocked inside srt; Helmraiser never sets `allowAllUnixSockets`.
- The Docker socket is never inside srt: Docker runs on the host, outside the sandbox.
- Only the chosen `127.0.0.1:<port>` literals are reachable; any other loopback port is
  refused by srt's proxy (`403 Forbidden`), and `localhost:<port>` does not match.
- What remains: anything else listening on an allowed port (which is why Helmraiser
  chooses the ports), srt's proxy credentials, which a step can read from its
  environment as before, and the services themselves, which the step's code can use
  as it likes.

### Trying commands in the sandbox

`helmraiser preflight <repo> [--run CMD]… [-- CMD…]` runs commands as verify would, before
you queue a task that depends on them:

```bash
helmraiser preflight ~/code/myrepo -- npx tsx --test src      # one command, as argv
helmraiser preflight ~/code/myrepo --run "npm ci" --run "npm test" --json
```

`<repo>` must be the repository's primary checkout. A linked worktree (a run's worktree,
for one) is refused with exit 1, naming the primary checkout to use: preflight tries the
primary checkout's HEAD and reads its `.helmraiser/project.toml`, which a run's worktree does
not have.

It adds a detached worktree of the repository's HEAD under
`<worktree_root>/<repo-name>-<hash>` (or the project's explicit `worktree_dir`),
runs the project's `setup` under
`srt-settings-setup.json` (or its approved host escape), starts its `[[services]]` as a
run does, and runs each command under the verify command sandbox settings
(`srt-settings-verify.json`) with the verify environment, the service variables and the
relays. It uses the project's `project.toml` only: no task's `setup` or `env`. A single
argument after `--` is a shell command line; several are joined as one argv. For each
command it prints the exit code, the duration and the last `--tail` lines (40), plus a
hint for known sandbox signatures: `EPERM` on `listen`/`connect` or a `.sock`/`.pipe`
(a Unix socket; for tsx, run `node --import tsx`), `ECONNREFUSED`/"connection refused"
on `127.0.0.1` on a port no declared service provides (declare a service), `EACCES`/"permission denied" (a denied path, or one of
the names srt masks as `/dev/null` in writable roots, such as `.mcp.json`, named on the
line or the previous non-blank one, also with "not a regular file") and "operation
not permitted". A failing `setup` or service stops it before the commands. Sandboxed
setup failures with `EROFS` or "read-only file system" show the same read-only-path hint
as a run, even when `--tail` hides the diagnostic (not for timeouts or host setup).
Sandboxed setup reuses the repository's persistent npm/pip cache in the real data dir,
not the temporary run directory; the cache survives cleanup. Preflight's temporary run
directory lives in `helmraiser-preflight-<id>` under `isolation.tmpdir`, or the system
temporary directory when unset, and is removed afterwards. The services,
the worktree and the temporary run directory are removed on every exit, Ctrl-C and
SIGTERM included; nothing is written to the ledger. Exit codes: 0 when every command
passed, 1 otherwise, 2 without a command, 130 when interrupted. `--timeout` (600 s) applies
per command.

## Monitoring

| Command | What it shows |
|---------|---------------|
| `helmraiser doctor` | Pi version and profile authentication, isolation availability, and data directory and ledger readiness. A missing ledger is informational and is created on first use, not by doctor; an existing ledger must open successfully. The reserved `vm` row is informational (`reserved; not implemented`). It includes informational [Leeboard diagnostics](#leeboard-optional). It also reports informational pnpm store filesystem and setup-write warnings without affecting readiness. It also lists the environment names stripped from runs, orphaned worktree metadata and phantom ledger runs for the current repository; phantom runs are informational until the next `work` reconciles them. When the repository declares `[[services]]`, it checks that `docker info` answers (not ready otherwise) and lists orphaned service containers |
| `helmraiser doctor --exposure [repo]` | Emits 12 stable component/probe rows (pi session, command, setup and service container × loopback, `~/.ssh` and SSH agent), or one JSON object. Loopback is a listener on a random host `127.0.0.1` (and `::1`) port, tried directly, through every proxy in the component's environment (HTTP `CONNECT`, forwarded `GET`, SOCKS5) and, from the container, through `host.docker.internal` and its gateway. Each row carries a positive control (`control: ok\|failed\|n/a`, `[control …]` in text): an allowed listener, a readable token file, or the same probe run on the host; a failed control makes the row `error`, never `blocked`. Exit 0 means every available target was blocked; 1 means exposed/error; 2 means evidence is incomplete because srt or Docker was skipped. An absent host SSH agent is a neutral skip. |
| `helmraiser status` | Queue depth, running runs, runs waiting for the worker to resume them, awaiting approvals, held tasks with their holder runs or unmet-dependency reasons, failures and landings in the last 24 h (`--json`: `failed` and `landed` lists, by when the run ended), provider spend and subscription routing headroom. It also rewrites `status.json` |
| `helmraiser stats [--since 24h\|7d\|30d\|90d\|all] [--project PATH] [--json]` | Read-only dashboard outcome, quality, cost, latency, scout funnel and weekly aggregates |
| `helmraiser report [--since 24h\|7d\|30d] [--models [--by model\|setup]]` | Morning report in markdown: Landed, Awaiting your approval, Failed, Cost; `--models` appends a per-implement-model table, or per model setup with `--by setup` |
| `helmraiser preflight <repo> [--run CMD]… [-- CMD…]` | Runs commands in a throwaway worktree of HEAD with the project's setup, services and the verify sandbox settings; prints each exit code, duration, output tail and hints for known sandbox failures. No ledger rows (see [Trying commands in the sandbox](#trying-commands-in-the-sandbox)) |
| `helmraiser clean [--project PATH] [--dry-run] [--json] [--data-dir PATH]` | Removes clean superseded or old failed/aborted worktrees, keeping branches (see [Cleaning up failed runs’ worktrees](#cleaning-up-failed-runs-worktrees)) |
| `helmraiser compare <run-a> <run-b> [--json]` / `helmraiser compare --variant-of <task> [--project P] [--json]` | Judges two runs blind, then reveals their setups (see [Comparing runs](#comparing-runs)) |
| `helmraiser eval review <cases.json> --variant V… [--case ID]… [--out DIR] [--max-cost USD] [--repeat N] [--review-model M] [--skill DIR]… [--dry-run] [--json]` / `helmraiser eval report <out> [--json]` | Replays past review stages under reviewer variants and scores them against labelled defects (see [Review replay](#review-replay)) |
| `helmraiser scout <repo> [--lens NAME] [--max N] [--force]` | Runs one read-only scout session and writes validated, non-ruling candidates as draft task files (see [Scout drafts](#scout-drafts)) |
| `helmraiser drafts [--project P]` / `drafts drop <task> --reason TEXT [--class CLASS]` | Lists pending scout drafts, or retains one as dropped with its reason and optional class |
| `helmraiser drafts request <repo>…` / `drafts request --cancel <repo>…` | Requests safe worker-driven scouts for chosen focus projects, or cancels pending requests |
| `helmraiser wait <ref>` | Blocks silently until owner attention or completion; one result object with `--json` (see [Waiting for a run](#waiting-for-a-run)) |
| `helmraiser log <run> [-f] [--since-id N]` | A run's ledger events as a timeline, or JSONL with `--json`; `-f` follows until the run is terminal or awaiting approval (plan, scope or merge); invoke it again after resumption to continue following. |
| `helmraiser show <ref> [--json]` | State and limits by run ID, short/full task ID, or unique run prefix (see [Showing one run](#showing-one-run)) |
| `helmraiser attach <run> [--stage implement\|review\|plan] [--exec]` | Prints the `pi --session-dir … --session …` command that opens the run's pi session; `--exec` runs it |
| `helmraiser queue list\|next\|pause\|resume` | Inspect and control dispatch; `pause --minutes N` auto-resumes, and `resume` also clears provider holds |
| `helmraiser queue remove <task>` | Take one `queued` task out of the queue by full task id (`<project-slug>:<id>`) or a unique bare id; it returns to `draft`, the file is untouched, and `intake` queues it again |
| `helmraiser caps [on\|off\|reset]` | Show subscription usage, elapsed window share, routing headroom and dollar spend, or control enforcement |
| `helmraiser worker start [--project P]… [--max N] [--max-parallel N] [--no-caps] [--no-notify]` | Starts `work --watch --json` with those options in the background, detached from the terminal; stdout and stderr go to `<data-dir>/worker.log`. The pid, start time, argv, log path, project scope (`projects`: the `--project` values or `null` for all; `exclude_projects`: from config when no `--project` is given) and `max_parallel_runs` are recorded in `<data-dir>/worker.json`. Refuses a second start while a worker is running |
| `helmraiser worker status` | `running`, `exited` or `none` for the background worker, with pid, start time, argv, log path, its runs against its limit (`runs: 1 of 2` and one line per run's project) and the last 5 log lines |
| `helmraiser worker stop` | Writes the stop request the worker honours; it starts no new run, lets its running runs finish, then prints the batch summary and exits. A no-op (exit 0) without a running worker |
| `helmraiser abort <run-id> --project-dir <repo>` | Stops one exact run through its process lease (SIGTERM, then SIGKILL after a grace period); if no worker is alive, it also closes the ledger run as aborted |

`<run>` accepts a full run ID or a unique prefix. `abort` needs the full UUID4.

### Waiting for a run

```bash
helmraiser wait <ref> --json
helmraiser wait <ref> --until end --timeout 600 --interval 5 --json
```

`<ref>` resolves in this order: exact run ID, task ID, unique run-ID prefix.
A task ID is either the full `<project-slug>:<id>` (including a leading `-`,
without needing `--`) or a bare ID unique across all projects. Ambiguity errors
list the matching full task IDs or run IDs. Run refs stay pinned to that run;
task refs follow the latest run by `started_at`, re-read on each poll. If a task
starts out `queued`, all its existing runs are ignored as history: wait for a
new run. A nonqueued task without a run is an error naming its status.

`--until attention` (default) stops at `awaiting_approval` (plan, scope or merge)
or a terminal status. `--until end` stops only at `landed`, `failed` or `aborted`.
In particular, `starting` and `aborting` keep waiting. `--timeout` defaults to
unlimited; zero checks once without sleeping. `--interval` defaults to 10 seconds.
Both accept finite seconds; timeout must be nonnegative and interval at least 1.
The condition is checked before sleeping, and sleeps never pass the deadline.

Waiting only reads the ledger: it writes no status file, starts no work and takes
no owner action. There is no intermediate output. On return, `--json` prints one
object with these fields; errors instead print `{"error": "…"}`. Ctrl-C is silent.
Human output summarizes the run, optional details and approval guidance; a human
timeout notice goes to stderr.

| Field | Type | Meaning |
|-------|------|---------|
| `run_id` | string or null | Followed run; null while waiting for a new run |
| `task_id` | string | Full task ID |
| `status` | string | Run status, or task status (`queued`) without a new run |
| `pending` | string or null | `plan`, `scope` or `merge` while awaiting approval, otherwise null; absent approval records default to `merge` |
| `verdict` | string or null | Review verdict |
| `cost_usd` | number or null | Run cost rounded to four decimal places |
| `failure_reason` | string or null | Failure reason, including `rejected` |
| `branch_name` | string or null | Run branch |
| `pr_url` | string or null | Pull request URL |
| `stage` | string or null | Run stage |
| `project` | string or null | Project path |
| `started_at` | ISO-8601 string (UTC) or null | Run start time |
| `ended_at` | ISO-8601 string (UTC) or null | Run end time |
| `timed_out` | boolean | Deadline elapsed before the condition was met |
| `scope_request` | object, optional | Pending scope request `{paths, reason}`, when available |

A task timing out without an eligible new run keeps `task_id`, `status: "queued"`
and `timed_out: true`; all other required fields are null. `scope_request` is omitted.

`wait` has different exit codes from other commands:

| Code | Meaning |
|------|---------|
| 0 | Condition met without failure: awaiting approval or `landed` |
| 1 | Run ended `failed` or `aborted`; rejection is `failed` with reason `rejected` |
| 2 | Unknown/ambiguous ref, nonqueued task without a run, unreadable/incompatible ledger, or invalid option |
| 124 | Timeout elapsed first |
| 130 | Interrupted (Ctrl-C), silently |

### Showing one run

```bash
helmraiser show <ref>          # run id, task id or unique run prefix
helmraiser show <ref> --json
```

References resolve in order: exact run ID, task ID (short ID or full
`<project-key>:<id>`), then unique run prefix. A task reference selects its most recent
run by start time, breaking ties by run ID, even when the task is queued again. A task
with no runs fails immediately. Unknown or ambiguous references fail with exit 1 and
name the accepted forms; `--json` returns `{"error": "…"}`.

`show` reads the ledger and the run's own task snapshot (`task.md` in its run directory,
else the ledger's copy: never the task file, which may have changed) and writes nothing.
Human output begins with `pr: <url>` when available; JSON keeps the unchanged `pr_url`
field. PR approval also ends its human output with the bare URL, so
`helmraiser approve <run> | tail -1` returns it (approval JSON is unchanged).
The text is one `label: value` line per field (null fields and a zero `rework_rounds` are
left out; `worktree_path` ends with `(exists)` or `(missing)`), then the limits:

```
iterations: 8 of 8 (rework_allowance)
cost_usd: 1.5000 of 7.2222
minutes: 12.0 of 53.3
```

`--json` prints one flat object. `worktree_exists` is true when `worktree_path` is a
directory now. `pending_approval` is `plan`, `scope` or `merge` only while an approval is
pending, otherwise null. `rework_rounds` counts the owner's rework requests.
`landed_outside` is `{commit, note}` for a run recorded with `record-landed`, otherwise null.
`warnings` lists why a limit is unknown: an unreadable snapshot leaves each `limit` null.

| `limits` key | `used` | `limit` |
|--------------|--------|---------|
| `iterations` | Iterations recorded for the run | The ceiling the runner enforces on the run's latest segment, or on its next one after a hand-back, with `limit_source`: `rework_allowance` is a rework's `iterations_before` plus its allowance, from its hand-back to the end of the segment that took it up; anything else, a budget retry's cumulative count included, is `max_iterations` |
| `cost_usd` | The run's cost, to four decimal places | `max_cost_usd` plus every rework's extra cost |
| `minutes` | Completed active minutes, plus the current segment while the run is `running` | `max_minutes` plus every rework's extra minutes |

The remaining fields are `run_id`, `task_id`, `project`, `status`, `stage`,
`failure_reason`, `verdict`, `policy`, `delivery`, `base_branch`, `base_commit`,
`head_commit`, `branch_name`, `pr_url`, `worktree_path`, `started_at` and `ended_at`
(ISO-8601 UTC).

### Models table

`helmraiser report --since 7d --models` appends a `# Models` section: one row per
implement model among the runs that started in the window, sorted by run count
(`--json` adds the rows as `"models"`).

`--by setup` groups by the run's whole model setup instead; a setup is the plan
model (`none` when the run had no plan stage), the implement model, the fallback
model (the configured fallback, whether or not the run escalated; `none` when the
task had none) and the review model. Each row gets a `setup` object in `--json`
(`plan`, `implement`, `fallback`, `review`; `null` for none) next to the
unchanged columns, and the markdown table shows the setup as a label such as
`plan=none impl=<model> fb=<model> rev=<model>` in place of the `model` column.
`--by` needs `--models`.

Columns: `runs`, `planned` (runs with a plan
stage), `landed`, `awaiting`,
`failed` (failed + aborted, excluding deliberate rejections), `rejected`
(runs left `failed` by `helmraiser reject`), `first_pass` (runs whose first iteration
verified), `escalated` plus `escalation_reasons` counts, `review_rounds` (runs with review
cycles), `severe_first_review` (first review had a severe finding),
`avg_iterations` and `avg_minutes` (ended runs), `cost_usd` (whole-run cost),
`plan_cost_usd`, `cost_per_run`,
`cost_per_landed`, `tokens_per_run` (in + out), and
`opencode_go_share_per_landed` — the fraction of the opencode-go monthly
allowance the model's iteration costs used, per landed run.

### Comparing runs

`helmraiser compare <run-a> <run-b>` asks the `judge` profile to compare two runs of
one task blind. It costs one pi call, with no tools, context files or repository
access, in a temporary session without a sandbox wrapper. The project's `[models]
judge` overrides the config profile. Nothing is stored in the ledger or data
directory; the judge cost is printed, not recorded, and a failed call is not retried.

Run references accept full IDs or unique prefixes. A and B follow argument order.
Both runs must be in the same project and be `landed`, `failed`, `aborted` or
`awaiting_approval`. `--variant-of <task>` instead finds exactly two tasks declaring
that `variant_of` in snapshot frontmatter, sorts their task IDs ascending (A first),
and selects each task's latest run by `(started_at, run_id)`, even if unfinished.
If variants exist in several projects, pass `--project P` to select one repository.

The prompt contains A's snapshot body (not frontmatter), each run's outcome,
iteration count, whether it escalated, automated review verdicts and diff (cut at
80,000 characters). Model names and setup metadata are omitted. Differing task
briefs produce a warning and A's brief is used. After judging, the output reveals
each run's setup alongside the verdict, confidence, scores, defects and merge advice.
Position bias is not corrected; swap the arguments to check it manually.

`--json` returns `judge` (`model`, `cost_usd`), `runs` (A/B facts with setups, without
brief, reviews or diff), `verdict`, `winner_run_id` (`null` for a tie), and `warnings`;
`variant_of` is included only in variant mode. Exit codes: 0 on success, 1 for an
unknown/ambiguous run or a failed judge session/invalid answer, 2 for invalid
arguments or unusable selections.

### Review replay

`helmraiser eval review <cases.json> --variant <name>…` reruns only the review stage
of past runs under reviewer variants and scores how well each finds known defects. Keep
the case file and `--out` (default: `out/` beside the case file) under `~/.helmraiser`,
which the reviewer's sandbox cannot read: they hold labels and other variants' results.

The case file is `{"name": …, "cases": [...]}`. Each case has `id`, `run_id`, `review`
(which review of the run, from 1), `bucket` (`miss`, `found`, `undercall`, `seeded` or
`clean`), `axis` (`spec`, `standards` or null), a `label` (`summary`, `file`,
`severity_expected`, `rule`: `source`, `text`, `written`; none for `clean`), `seed.patch`
(an absolute path, `seeded` only) and an optional `meta`. Unknown buckets are refused.

For each case the replay takes the stored `prompt-review-1[-<n>].md` of review N (the
ledger's review stage starts and review retries give the order), the stored
`directives-review.md`, and copies of the run's `evidence/` and `logs/` files. Any path
ending in `/runs/<run_id>` (also under an older data dir) is pointed at
the replay's run dir, and the run's worktree path, or any path ending in a
`worktree-…-<run_id>` directory, at the replay worktree. A later step (a rework's evidence)
rewrites those files, so a file last written (by mtime) after review N's `review_verdict`
is withheld; the prompt still names it, as stored, and the reviewer finds it missing.
Each result line and dry-run pair lists `evidence_files_copied`, `evidence_files_withheld`
and `unresolved_paths`: paths the prompt or directives name under the replay run dir or
worktree that do not exist there. It looks for the reviewed commit among the run's
iteration commits and head, and keeps the one whose `git diff <base>..<commit>` is the
diff in the stored prompt (above 200 KB, only the size is checked). `meta.reviewed_commit`
and `meta.review_prompt` are tried first; a case fails when they disagree with the
ledger. The reviewer works in a detached worktree at that commit, removed afterwards; a
seeded case applies its patch there and its diff replaces the stored one. The model is
the project's current `review` profile, not the run's. `--review-model
<provider/id[:thinking]>` replaces it in every reviewer session (Spec and Standards
included; the judge keeps its profile) and makes the variant `<name>@<model>` in results
and report columns, so it never shares resume state with the profile model's results.

`--skill <dir>` (repeatable) names a skill directory, one that holds a `SKILL.md`, for the
skill variants (`v2-skills`, `v4-twoaxis-skills`). Their reviewer sessions get each one
through pi's `--skill`, before the project's own discovered skills, and the sandbox can
read it. `~` is expanded and the path is stored absolute. A chosen skill variant without
`--skill`, or a `--skill` directory without `SKILL.md`, is refused before anything runs.
Other variants ignore `--skill`.

| Variant | Reviewer sessions |
| --- | --- |
| `v0-baseline` | The live review argv without a `review` [stage skill folder](#stage-skill-folders): review tools, `--no-context-files`, pi's skill discovery on |
| `v1-noskills` | v0 with `--no-skills` |
| `v2-skills` | `--no-skills` and `--skill` for the `--skill` directories and the project's `.pi/skills` and `.agents/skills` skills |
| `v3-twoaxis` | A Spec session (the live directives plus three spec questions), then a Standards session (a draft directive in `loop/eval_review.py`: cited breaches of the project's documented standards, plus a code-smell baseline that is always `minor`), both `--no-skills` |
| `v4-twoaxis-skills` | v3 with v2's skills |
| `v5-procedure` | v1 with a four-step review procedure (`REVIEW_PROCEDURE` in `loop/eval_review.py`) appended to the directives |
| `v6-twoaxis-procedure` | v3 with the same procedure appended to the Spec directives only |

Sessions run one at a time under the scout's read-only process isolation, with
`allowRead` carve-outs for skill paths that the other-project denial hides. A two-axis
verdict is the worse of the two (`uncertain` > `concerns` > `pass`), and each finding
keeps its `axis`. Evidence checks are not forced as the live loop forces them. For a
positive case with findings, a blind `judge` session (no tools, as `compare`) gets the
label and the findings and says whether one names the defect; severity and axis come from
the matched finding. A clean case records its finding and severe-finding counts.

`--repeat N` (default 1) runs each (case, variant) N times as reps 1..N. Each run appends
one line to `<out>/results.jsonl` with its `rep` (verdict, findings, sessions with cost,
tokens, seconds, `files_read` (distinct paths through `read`) and `tool_calls` by tool,
summed over a two-axis pair, judge answer and cost), and the pair's run dir (prompt,
directives, review texts, pi sessions, sandbox settings) goes to
`<out>/pairs/<case>/<variant>/<pair>/`. A rerun skips (case, variant, rep) whose latest
line is `ok` and retries the others; a line without `rep` is rep 1. `--max-cost` stops before a pair whose estimated cost (the mean of
that variant's finished pairs, else of all) would take this invocation's spend past it.
A quota or contract error stops the evaluation. `--dry-run` builds each worktree, prompt
and argv, prints them and calls no model; it writes no results.

`helmraiser eval report <out>` writes `<out>/report.md` from the latest line of each pair:
per variant, recall per positive bucket (`found` is a regression check), severity given
on `undercall` and `miss` cases, `standards` cases split by written and unwritten rule,
findings and severe findings on clean cases, total and median cost and seconds, a
Reading table (median distinct files read, tool calls and input tokens, positive and clean
cases apart; rows from before these were recorded are left out), then the number of
evidence files withheld per case (`evidence_withheld`). With several reps, recall is the
mean of each rep's recall with its min..max, severity counts are summed, clean-case counts
are per rep, and a Hits per case table shows each case found in k of N reps. It reads only
`<out>`, so a held-out case file with its own `--out` gets its own report.

Exit codes: 0 on success (also when `--max-cost` stops, reported as `stopped`), 1 when a
pair failed or the sandbox is unavailable, 2 for an invalid case file, variant, case id,
unknown run, missing models, or a skill variant without a valid `--skill`.

### Omarchy widget

[`omarchy/zsolt.helmraiser/`](../omarchy/zsolt.helmraiser/README.md) is an Omarchy 4 bar
widget. It shows running and awaiting-approval counts from `status.json` and turns
urgent when the queue is paused or a run awaits approval. It keeps the file fresh by
running `helmraiser status --json` on a timer. Its panel lists runs and failures and
has Approve and Reject buttons. See its README for installation, settings and IPC.

### Stats

```sh
helmraiser stats [--since 24h|7d|30d|90d|all] [--project PATH] [--json] [--data-dir PATH]
```

The JSON is the contract for dashboards; dashboards need not read SQLite. Default
window: `7d`. Unknown windows exit 2. Stats opens an existing current-schema ledger
read-only (missing or old ledgers exit 1), never refreshes status files, and never
reads run directories. Human output is a short plain-text summary. Project paths
are expanded and resolved and filter every section. SQLite may create WAL reader
sidecars; no ledger rows are changed.

All sections are always present. Rates are fractions rounded to three decimal
places, or `null` for a zero denominator. Timestamps are UTC ISO strings.

- `window`: `since` is the selected window; `since_seconds` is its duration, or
  `null` for `all`; `generated_at` is the captured computation time; `project` is
  the resolved filter or `null`. Run and scout cohorts use their `started_at`.
- `runs`: `overall` and sorted `by_project` rows (each with `project`) contain
  `landed`, `failed`, `aborted`, `awaiting_approval`, and `active` counts. Active
  includes every other nonterminal status.
- `quality`:
  - `first_time_right`: `{count, rate}` among landed runs. A qualifying run has
    exactly one iteration, no merge `rework` decision, no scope approval row
    (including pending) or `scope_requested` event, and no earlier run of the same
    task, including outside the window. Equal starts are ordered by run ID.
  - `rework_rate`: runs with a merge `rework` decision divided by runs with a
    nonpending merge decision.
  - `scope_request_rate`: runs with a scope approval row or `scope_requested`
    event divided by all runs.
  - `retried_tasks`: task identities with more than one run in the window.
  - `review_verdicts`: counts of `pass`, `concerns`, `uncertain`, and `none`
    (no recorded verdict).
- `failures`: failed and aborted runs grouped by class. The class is the trimmed
  prefix before the first colon in `failure_reason`, or the trimmed whole reason
  without a colon; empty reasons or prefixes become `unknown`. Each class contains
  `count` and up to three newest `run_ids`, ordered by start time then run ID.
- `cost`: `total_usd` sums run costs; `plan_usd` sums plan costs;
  `implement_usd` sums iteration costs; `other_usd` is total minus plan and
  implement, floored at zero. Other covers review and anything not recorded
  separately, **not** exclusively review. `per_landed_usd` is total cost divided
  by landed count, or `null`.
- `latency`: `active_minutes` and `approval_wait_minutes.{plan,scope,merge}` each
  contain `median` and `p90`, or null values without samples. Active samples sum
  numeric `run_end.active_minutes` per landed run, not wall-clock time; missing
  recordings are not zero. Approval waits pair each awaiting-approval `run_end`
  with its next unconsumed nonpending approval before the next wait episode;
  the approval row determines the kind. Unanswered waits have no sample.
  Percentiles linearly interpolate at `(n - 1) × p`.
- `scouts`: grid rows for every lens and every configured focus project or project
  with historical scouts; an explicit project filter returns only that project's
  complete lens grid. Each row contains:
  - `project`, `lens`, `in_focus`; `auto` and `cooldown_hours` from lens settings.
  - Window scout counts `scouts`, `succeeded`, `failed`, `running`: `running`
    counts scouts whose outcome is neither succeeded nor failed, so
    `scouts = succeeded + failed + running`. `drafts_written` counts the cohort's
    `scout_drafts` rows, including drafts written by scouts still running (before
    their final written-ID list is saved). `cost_usd` sums scout costs.
  - Current draft outcomes for that scout cohort: `pending` (not ever-queued under
    the definition below),
    `queued` (queued, running or awaiting approval), `landed` (any landed run),
    `dropped`, and `failed_drafts` (latest run failed or aborted, no landed run).
    `failed` counts scouts, so the draft failure outcome uses `failed_drafts`.
  - `dropped_by_class`: counts for `wrong`, `unwanted`, `duplicate`, `unusable`,
    and `unclassified`, using the same cohort and dropped outcome (landed takes
    precedence). Missing or unrecognized stored classes count as `unclassified`.
    All five keys are present, even when zero.
  - `queued_unchanged`, `queued_changed`, `queued_unknown` classify **all
    ever-queued drafts**, including landed ones. Unknown means no baseline hash.
    SHA-256 covers only verify, evidence, paths, budget and body of the final
    scout-prepared snapshot (initial rendering if preparation never succeeds).
    Canonical compact JSON sorts mapping keys, preserves list ordering and
    values, uses empty lists for absent/null verify/evidence/paths and an empty
    mapping for absent/null budget. Body line endings become LF and outer
    whitespace is stripped; internal whitespace is preserved. Exact paths in
    `intake.related_tests_added` are removed; status, origin, intake stamps and
    other fields are ignored. Comparison uses the ledger's current task snapshot;
    immutable queue history and later re-intake edits cannot be reconstructed.
    For these counters and draft outcomes, **ever-queued** means the task is
    currently `queued` or has at least one run. A draft queued and then dequeued
    before any run starts counts as `pending`, not in any queued hash counter.
    No queue-history storage is maintained.
  - `last_scout_ended_at`, `last_scout_ok` (succeeded), `last_scout_error` (or
    null), `last_scout_drafts` count describe the latest scout regardless of the
    window. All four are null with no scout.
  - `next_eligible_at` comes from scout cooldown calculation; null means eligible
    now when due time is minus infinity. Finite elapsed times remain timestamps.
    This is cooldown only, not other start gates. An unavailable repository has
    null next eligibility and a short `scheduling_error`; healthy rows have null
    `scheduling_error`. Other projects' rows still return.
- `weekly`: oldest-first UTC Monday ISO-week buckets, including zero weeks,
  capped at the newest 26 overlapping weeks. `week` is `YYYY-Www`; `landed`,
  `failed`, and `first_time_right_rate` use run-start cohorts and the definitions
  above. `drafts_written` and `drafts_landed` use originating scout-start cohorts;
  landed is the current outcome, not the date of landing. `all` starts at the
  earliest selected run/scout (current week on an empty ledger).

## Status contract

`helmraiser status` reports the ledger state and refreshes `status.json`. The file is
written atomically to `$XDG_RUNTIME_DIR/helmraiser/status.json` when
`XDG_RUNTIME_DIR` names an existing directory, or to
`$HELMRAISER_DATA_DIR/status.json` otherwise. The work loop also refreshes it at each
stage change.

The `status.json` object has these fields:

| Field | Type | Meaning |
|-------|------|---------|
| `updated_at` | ISO-8601 string (UTC) | When the file was written |
| `queued_count` | integer | Tasks with status `queued` |
| `drafts_counted_kinds` | array | `["scout", "manual"]`: both kinds count toward `drafts` and safety caps |
| `scouts.lenses` | object | Per-lens `auto`, `cooldown_hours`, and `source.auto` / `source.cooldown_hours` (`ledger`, `config`, `default`), as in drafts settings |
| `drafts` | object | Sorted mapping of project paths to counts of tasks with status `draft`; dropped tasks are excluded |
| `queue_next` | list | The first five queued tasks in dispatch order: `task_id`, `id`, `title`, `priority`, `project`, and `held_by`: `null`, or a hold payload. Project-run holds contain `{run_id, status, pending}` (`pending` is `plan`, `merge` or `scope` for a run awaiting approval, else `null`). Dependency holds contain `{reason: "depends_on", unmet: [{id, reason, text}]}`; combined holds retain both sets of fields |
| `held` | list | Every held queued task: `task_id`, `id`, `title`, `project`, plus the same hold payload as `queue_next[].held_by`; dependency-only holds have no holder run fields |
| `never_startable` | list | With caps enforced, every queued task whose projected usage alone reaches `stop_at`, so `work` never starts it: `task_id`, `id`, `title`, `project`, `reason` (`never_startable:<provider>:<window>`), `model` (whose monthly limit is used), `projected` (fraction of the window), `projection_usd`, `stop_at`, and `text` (what to change). Empty with caps off |
| `running` | list | One object per run that is neither terminal, awaiting approval nor resuming: `run_id`, `task`, `project`, `id`, `title`, `stage`, `iteration` (count so far), `cost_usd`, `minutes` since start. `id` and `title` are the task's short id and title from the ledger, or `null` when the task row is missing |
| `resuming` | list | One object per run handed back to the worker (see [Resuming runs](#resuming-runs)): `run_id`, `task`, `project`, `id`, `title`, and `stage`, where the worker resumes it (`start`, `implement` or `verify`). `held_by` is `null` when no project-admission hold prevents resuming, otherwise the hold preventing this run from resuming now, using the same shape as `queue_next[].held_by` |
| `reworks` | list | Reworked active/awaiting runs and terminal runs from the last 24h: `run_id`, `task`, `status`, `rework_rounds`, and ordered `reworks` requests with full notes. Reworked `running`/`resuming` rows also include `rework_rounds` and `reworks`; unrelated rows keep their shape |
| `awaiting_count` | integer | Count of runs waiting for `helmraiser approve`, `reject`, or merge `rework`; rows are in the CLI's `awaiting` list |
| `failed_24h_count` | integer | Count of runs that ended as `failed` in the last 24 hours; rows are in the CLI's `failed` list |
| `spend` | object | Provider → rolling `5h`, `7d` and `30d` ledger spend in USD |
| `usage` | object | Per subscription and window: `fraction`, `spend_usd`, `resets_at`, `warn_at`, `stop_at` |
| `routing` | object | `headroom` maps every configured or reported subscription to `has_headroom` and its available `5h`, `week` and `month` windows. Each window has `used`, `elapsed`, `target` and `headroom`; unavailable numeric values are `null`. `has_headroom` is true only when at least one window is present and every window's headroom is strictly above the configured margin |
| `scouts` | object | Scout routing and activity: `focus_projects`, `paused_projects`, `pending`, `running`, and `recent` as described below |
| `caps_enforced` | boolean | Whether `stop_at` enforcement is active: the `helmraiser caps on` / `off` switch, otherwise on |
| `paused` | `null` or `{reason, until}` | Queue pause state; `until` is a Unix timestamp or `null` |
| `held_providers` | object | Provider → `{until, detail}` for active provider holds; expired holds are omitted |
| `worker` | object | The background worker: its `worker.json` record (`pid`, `started_at`, `argv`, `log`, `projects`, `exclude_projects`, `max_parallel_runs`) with `state` `running` or `exited`, or `{"state": "none"}` without a record. A running worker also has `children`: one `{pid, project, run_id, kind, started_at}` per child in flight (`kind` is `run` or `scout`; scout children also have `scout_id`; `run_id` is available for fresh runs after registration via their task lock and child PID, and for selected resumptions), and `errors`: one `{project, stage, error, since, retry_at}` per project whose work currently fails (`stage` is `reconcile`, `task`, `start`, `run` or `scout`; `since` and `retry_at` are Unix timestamps). A running worker also has `skips: [{task_id, reason}]` for all current scheduler skip reasons (invalid tasks, quota, caps and optional [Leeboard holds](#leeboard-optional)), not skip history. The record also carries `code` (`{commit, snapshot}`: the commit of the code snapshot the worker runs, and its directory), `restarted` (`{at, from, to}`, the last restart on new code, commit hashes) after one, and `exit` (`{at, reason, error}`) once the worker has ended: `reason` is `stopped`, `max_runs`, or `error` with `error` saying why |

The `scouts` object also includes `lenses` as described above; its activity fields have this shape:

```json
{
  "focus_projects": ["/abs/repo"],
  "paused_projects": ["/abs/repo"],
  "pending": [{"project": "/abs/repo", "requested_at": "<iso>", "wait_reason": "queue paused"}],
  "running": [{"project": "/abs/repo", "scout_id": "<id>", "started_at": "<iso>"}],
  "recent": [{"project": "/abs/repo", "scout_id": "<id>", "ended_at": "<iso>", "ok": true, "drafts": 2, "report": "/abs/path/scout.md", "error": null}]
}
```

`focus_projects` includes configured projects even when they are also in
`paused_projects`; consumers subtract the latter when offering new requests. Pending
requests are oldest first. `wait_reason` is `queue paused`, `project busy`, `draft cap`,
`no headroom`, `slots busy`, or `null` when the request can start on the next worker
tick. `running` includes both ledger scouts and a newly spawned child before it creates
its ledger row. `recent` contains requested and automatic scouts that ended during the
last 24 hours, newest first. Human status prints one `Scout pending:` or `Scout running:`
line per active scout and does not print recent scouts. `recent[].drafts` counts drafts
written by that scout, not currently pending drafts.

```bash
helmraiser status --json | jq '{queued_count, awaiting_count, running: (.running | length), paused}'
```

`helmraiser status --json` also returns these CLI-only lists (not persisted in
`status.json`), plus the `queue` counts and `status_path`:

| Field | Meaning | Corresponding count |
|-------|---------|---------------------|
| `awaiting` | List of runs awaiting approval | `awaiting_count` |
| `failed` | List of runs ending as `failed` in the last 24 hours | `failed_24h_count` |
| `landed` | List of runs ending as `landed` in the last 24 hours | No corresponding landed count key |

Each row carries the same identity keys — `run_id`, `task`, `project`, `id` and `title` —
plus its own fields. An `awaiting` row also names where to review the run: `worktree`,
`branch` and `base_commit`.

The Omarchy widget reads the count keys `queued_count`, `awaiting_count` and
`failed_24h_count`.

Every task or run row in `queue_next`, `held`, `never_startable`, `running`,
`resuming`, `awaiting`, `failed`, and `landed` includes `origin`, either
`{"kind": "claude-code" | "fo", "session": "…"}` or `null`. Queued-task rows read
it from the immutable ledger task snapshot; run rows read the run's `task.md` snapshot.
Human status shows it only in the Awaiting your approval table, shortened to
`(origin: <kind>:<first 8 session characters>)`.

## Running unattended

`work` drains the queue in the foreground until the queue is empty or paused (with
`--watch` it waits for more instead; see [Running the queue](#running-the-queue)). Before
each queued task it resumes a run handed back to it, if any (see below). It runs
one run per project at a time by default: a queued task whose project has a run that is
not terminal, including one awaiting plan or merge approval or one waiting to be resumed,
is **held**, and `work` takes the next task of another project instead. Raising
`max_runs_per_project` allows siblings only below that limit, when the candidate and
**every** nonterminal sibling declare `paths`, their scopes are conservatively disjoint,
and neither depends directly or transitively on the other via `depends_on`. Wildcards
are compared by literal segment prefixes: patterns that could overlap are held, even
`src/file*.py` versus `src/file*.txt`. Running, resuming and parked approval runs all
consume project capacity. Missing or invalid declarations fail closed. A task's
ledger snapshot is frozen while any of its runs is nonterminal: `add`, re-intake
and direct snapshot updates cannot change its scheduling declarations until the
run settles. Identical snapshot updates are no-ops and cannot requeue an active task.
Higher-limit holds retain the holder fields and add `reason` (`project_busy` for
capacity or missing paths, `paths_overlap`, or `depends_on`), `other_task_id`, and
`detail` when declarations are missing/invalid. Concurrent dependency holds also
include `unmet` explanations naming the sibling. When only held tasks are left, `work` exits as
for an empty queue and names them: `no eligible task` and one `held: <task> by run
<run8> (awaiting merge approval)` line each, or `"held"` rows (`task_id`, `run_id`,
`status`, `pending`) in the `--json` object and in the batch summary. `queue next` does
the same. `approve` and `reject`, once they settle a run, list that project's queued
tasks that are now eligible (`"eligible": {"task_ids", "worker", "worker_serves_project",
"project"}` with `--json`) and say whether a running background worker serves that
project; they do not start one. Provider quota
holds skip tasks whose implement or review model uses that provider while tasks on other
providers continue. After running at least one task, skipping one, or honouring a
`worker stop` request, `work` prints a
`Batch:` summary with outcome counts, total cost and time, each run, skipped tasks,
provider holds, and any pause. It also sends that one-line summary through
`notify-send` when available. Set `"notify": false` in `~/.helmraiser/config.json` to turn
desktop notifications off, or pass `--no-notify` for one invocation.

### Dependency holds

`depends_on` holds only the dependent task, unlike the default-limit active run's
project-wide hold. `queue next`, foreground `work` and the background worker skip it and consider
later tasks of the **same project**, as well as other projects. Held tasks remain
`queued` and still count toward queue depth.

A dependency is satisfied by a `landed` run whose recorded delivered `head_commit`
is an ancestor of the dependent's **local** base branch: task `base`, applicable
project default, or the primary checkout's current branch when unset. A detached
checkout without an explicit/default base cannot satisfy dependencies. PR delivery
requires a merge commit containing that head **and local base catch-up**; a merge
on GitHub alone does not release the task. **Squash merges never satisfy the
recorded-head ancestry check**, even with identical content.

For queued PR tasks held only because delivered heads are `not_in_base`, the worker
attempts local base catch-up before selection, at most once per
`base_sync_interval_seconds` per (project, base). Foreground `work` checks once before
selection. Thus even an all-held PR queue can catch up automatically after an
upstream merge. Dependency evaluation itself remains read-only; other unmet reasons
and merge-delivery tasks do not trigger synchronization.

Unknown same-project IDs, dependencies without landed runs, and latest runs that
failed or aborted hold the task with explanations. A latest failed/aborted run
overrides an earlier landed run (latest means `(started_at, run_id)` descending).
When the work of such a run reached the base outside Helmraiser, [`helmraiser record-landed`](#recording-work-merged-outside-helmraiser) ends the hold; editing the task files does not, since dependencies come from snapshots.
Cycles hold their members and tasks whose dependency path reaches the cycle;
the reason includes the cycle path. Missing heads, unavailable bases, malformed
snapshots and git errors also hold rather than silently dispatching.

Dependencies come from **ledger task snapshots**, not edits to live task files.
Eligibility is reevaluated on every selection/status call and worker polling
cycle; adding a previously unknown task or advancing the local base can release
a hold without restarting the worker. Evaluation never fetches, pulls, queries
GitHub, merges, or changes task status.

For example, human status prints:

```text
Held: <project-slug>:compose-app depends_on: compose-network: not landed (queued); compose-services: unknown dependency id
```

The corresponding hold payload in `queue_next[].held_by` (and alongside the task
identity in `held`) is:

```json
{
  "reason": "depends_on",
  "unmet": [
    {"id": "compose-network", "reason": "not_landed", "text": "compose-network: not landed (queued)"},
    {"id": "compose-services", "reason": "unknown", "text": "compose-services: unknown dependency id"}
  ]
}
```

### Resuming runs

`approve` on a pending plan or scope request (including `--decline`), `rework` on a pending merge
or an open PR-delivered run, and `retry` on a failed run do not run pi themselves.
They mark the run `resuming`, with the stage to resume at in the run's `stage`
(`implement` after a plan or scope approval, decline, rework, or `retry --implement`, `verify`
after an ordinary retry, or `start` for a retry without committed work),
and return: the text
output says the worker resumes the run and whether a background worker that serves the
run's project is running (a worker whose `--project` values do not include it gets
`helmraiser work --project <project>`, as does one whose config excludes the project; no
worker gets `no worker is running: helmraiser worker start (or helmraiser work)`). A running
worker picks the run up within `worker_poll_seconds`. `--json` prints `{"approved": "plan", "run_id",
"task_id", "project", "status": "resuming", "stage", "worker", "worker_serves_project"}`
(`retry`: `"retried"`, the previous failure reason, in place of `"approved"`, plus
`"restarted_without_commits": true` only for a restart at `start`;
`rework`: `"reworked": "merge"`, plus `"rebase": true` only for a rebase request;
a scope approval adds `"approved": "scope"` and the approved `paths`; a decline instead
adds `"declined": "scope"` and `"paths": [<requested paths>]`, with no `approved` key), with
`worker` `running`, `exited` or `none`. `work` (with `--once`, `--max`, `--watch`
and `--project` too) resumes such runs before it picks a queued task, oldest first.
At the default limit a resuming run waits while another run of its project is running
or awaiting approval, and holds that project's queued tasks meanwhile. At higher
limits resumption reuses its existing slot and requires safe paths and dependency
independence from every sibling; a resuming run handed back later never holds it, so
when more runs are resuming than the limit allows, the oldest resume first. Lowering
the limit does not terminate existing runs. The resumed run is
the same run and worktree: after a plan approval it continues exactly as a run does
after its plan; after a retry see below. `work --dry-run` does not resume runs. `reject`
also accepts a resuming run that the worker has not picked up yet.

### Cleaning up failed runs’ worktrees

Only `failed` and `aborted` runs qualify: either a later run of the same task
landed (`superseded`), or the run ended more than seven days ago (`age`, by default).
Dirty worktrees, including untracked files, are kept. Removal never uses force;
branches and commits remain. Each removal records a `worktree_cleaned` ledger event.

```bash
helmraiser clean --dry-run --json
helmraiser clean --project /path/to/repo --dry-run
helmraiser clean --project /path/to/repo --json
```

Watch workers also sweep at most hourly, only while the queue is unpaused and only
for projects in their scope with no running child (including scouts). Manual
`clean` works even while paused. JSON returns `{"dry_run": false, "runs": [...]}`;
text lists each result and final removed/would-remove/kept counts.

Set `cleanup.failed_after_days` to a positive integer in `~/.helmraiser/config.json`,
or disable age cleanup with:

```json
{"cleanup": {"failed_after_days": null}}
```

`null` disables **only age cleanup**; superseded runs still qualify. Once a run's
worktree is cleaned, it cannot be retried: queue the task again instead. Ledger
history and session transcripts remain.

### Retrying a failed run

`helmraiser retry <run> [--json]` is for a run that ended `failed` with `blocked: …`,
`verify_exhausted` or `verify_failed: …` because the task's own verify (or evidence)
step was wrong, while the implementation may be fine. It also takes a `budget:…` failure
(see [Retrying after a budget failure](#retrying-after-a-budget-failure)) and a
`services: …` failure, which resumes at `verify` where the services start again when the
run has commits. A `setup: …` failure is also
retryable, but only when nothing was committed (see [Restarting without commits](#restarting-without-commits)).
Fix the task file first; `retry` then:

- refuses a run that is not `failed`, one that failed for another reason, one whose task
  has been queued or run again since (only the task's latest run, while the task is
  `failed`), or one whose worktree, branch or worktree metadata is gone;
- re-reads the task from the file it was registered from, validates it with the same
  loader and rules as a queued task (`policy: auto` is refused when the project sets
  `require_approval`), and replaces the ledger's task snapshot and the run's `task.md`
  with it, so a corrected `verify` or `evidence` block takes effect;
- hands the run back to the worker at stage `verify`, except for a restart at `start`
  when there are no commits and the worktree is clean (or the failure was in setup or
  starting the services).

For an ordinary retry, the worker then commits any uncommitted changes in the worktree
(`helmraiser: uncommitted work of <id> (retry)`, with the `chore(helmraiser):` prefix
when the history is Conventional Commits), runs verify, and on a pass runs
evidence, the review and the policy decision as usual, so the run usually ends
`awaiting_approval` and `approve`, `reject`, `status` and `report` treat it like any
other run. There is no implement session and no review-feedback round; the retry's
verify result replaces the last iteration's in the ledger. When verify fails again the
run ends `failed` with `verify_failed: <step names>`, which can be retried again.

`helmraiser retry <run> --implement [--json]` is for a run whose branch needs more work
rather than a re-check: on 2026-09-30 a run's tests were wrong because the implementer
wrote them while its database was unavailable. It has the same eligibility checks and
re-reads the task file the same way. Except for a restart without commits or a budget
failure, it also refuses
a run that has no failed verify result in the ledger (one blocked with uncommitted edits
before its first verify, say), and hands the run back
at stage `implement` (`"stage": "implement"` in the JSON). The worker then continues the
implement loop on the same branch and worktree, with earlier commits and any
uncommitted changes kept:

- the first prompt, in the run's first implement pi session (`<run-id>-implement`, so the
  implementer keeps its conversation), says the owner handed the run back after it
  failed (with the reason), asks it to re-read the task file, and carries the last
  verify results as a normal failed iteration's feedback: each failed step's excerpt
  and the path of its full log;
- iterations get a fresh budget: the implement model has the task's `max_iterations`
  again (the ledger's iteration numbers carry on), and the time budget restarts, as
  for any resumed run, which counts active time only. The cost cap does not restart:
  the run's cost so far counts against `max_cost_usd`. A run that has had a budget retry
  gets none of this: only what remains;
- the escalation ladder starts again: the implement model first, then the fallback
  with half the iterations, even when the failed run had already escalated;
- evidence, the review, the review-feedback round and the merge decision follow as
  for any run. The run event `run_resume` records `"from": "retry"` and the stage.

After rebasing the run worktree onto its recorded base branch outside the run, use
`helmraiser retry <run> --adopt-base` (also accepts `--implement` and `--json`). This
records that branch's current tip in the ledger and worktree metadata and adds a
`rebased` event; it does not perform a rebase. It refuses an unfinished rebase, a base
tip that is not an ancestor of the worktree's `HEAD`, or an unchanged recorded base
("nothing to adopt"). Ordinary retry does not auto-detect a host rebase.

#### Retrying after a budget failure

A `budget:cost`, `budget:time` or `budget:iterations` failure can be retried once the
task file leaves room on all three caps, so that the retry cannot fail straight away on
another one. A budget is cumulative across a budget retry, never refreshed:
`max_iterations: 12` means 12 in all, not 12 more. `retry` compares the file with what
the run has used and, without room, exits 2 before writing anything, naming what was
used, the limit and the file to edit:

- `max_iterations` must exceed the run's iterations so far;
- `max_cost_usd`, plus the extra cost of its reworks, must exceed the run's cost so far;
- `max_minutes`, plus the extra minutes of its reworks, must exceed its cumulative active
  minutes.

The run then has what remains, from that retry on, in every later resume of the run
(another retry, a plan or scope approval): its `run_resume` event records
`"budget_retry": true`, and the implementer's directives state the remaining minutes and
cost. Only a later rework has its own iteration allowance. The approval policy does not
change. The plain form verifies, collects evidence and reviews the branch without an
implement session. Work the stopped session left uncommitted is committed first, in both
forms (`… uncommitted work of <id> (retry)`), so the branch is what gets verified and
reviewed. `--implement` needs no failed verify result: without one, the prompt asks the
implementer to compare the branch with the task and finish what is missing, and if that
first session changes nothing, Helmraiser verifies the branch as it stands. A run with no
commits and a clean worktree restarts, with the budgets still cumulative.

#### Restarting without commits

When `HEAD` still equals the run's base commit and the worktree is clean, `retry`
restarts the run's work instead of trying to verify an empty branch. A `setup: …` or
`services: …` failure also qualifies with uncommitted setup debris; a `setup: …` failure
with commits remains refused.
`--implement` means the same thing here and does not require a failed verify result.
A dirty worktree with any other failure still follows ordinary retry behavior:
verify by default, or `--implement` only when a failed verify result exists (or the
failure was a budget).
Other failure reasons (including isolation, auth, worktree and
`error:` failures) do not become retryable.

After the task file is re-read, the worker resumes the same run id, branch, worktree
and ledger history at `start`, in this order:

1. Run setup unless the latest recorded attempt passed with the same effective command.
   Missing, failed, timed-out, unfinished or changed setup runs again; a skipped setup
   records `setup_skipped` with `{"reason": "passed earlier"}`. Another setup failure
   ends with the usual `setup: …` reason and can be retried again.
2. Reuse an existing plan and its approval state; otherwise plan only when `plan: true`
   or `approve-plan-first` requires it. The latter waits for plan approval as usual.
3. Implement, verify, collect evidence and review as a normal first iteration, including
   review feedback and the policy decision. The first prompt explains the failed reason,
   says nothing was committed, and asks the implementer to read the possibly changed
   task file and start from the beginning. It carries no old verify results.

The worker does not repeat preflight, worktree creation, materialisation or base sync.
The implement conversation stays in `<run-id>-implement`. Allowances are identical to
`retry --implement`: fresh `max_iterations` on the implement model, continued ledger
iteration numbers, restarted active time, cumulative cost against `max_cost_usd`, and
a fresh escalation ladder even if the failed run had escalated. After a budget failure,
iterations and active time stay cumulative too.
The `handed_back` and `run_resume` events include `"stage": "start"` and
`"restarted_without_commits": true`; `run_resume` keeps `"from": "retry"`. Retry JSON
includes that extra key only for this case. Text output also explains that the worker
restarts the run from its start.

### Recording work merged outside Helmraiser

`helmraiser record-landed <run> --commit <rev> --note "<why>" [--json]` is for a `failed` or
`aborted` run whose branch reached its base branch without Helmraiser: an agent finished it
by hand and the branch was fast-forwarded. The run stays `failed`, so a task that
`depends_on` it stays held (`latest run <id> failed`), and editing task files does not
help, since dependencies come from ledger snapshots. This command records the fact. It
moves no git ref and does not touch the base branch.

It reads the task from the ledger snapshot, not the task file, and refuses, writing
nothing, when:

- `--commit` or `--note` is missing, or the note is blank (exit 2);
- the run is not `failed` or `aborted`: a running, awaiting-approval, resuming or
  landed run goes through its normal path (exit 2);
- the run is not the task's latest run, or the task no longer has the run's status (exit 2);
- `<rev>` is not a commit in the run's project (exit 2; the ledger stores the full SHA);
- the commit is not an ancestor of the tip of the run's `base_branch`
  (`refs/heads/<base_branch>`, exit 2). A dependency hold compares a landed run's
  `head_commit` with the dependent's base, so a commit outside it would release nothing;
- the run has no recorded base branch, the base branch no longer exists, the task
  snapshot cannot be read or git fails (exit 1).

Then it:

- makes the run and its task `landed` and sets the run's `head_commit` to the full
  commit. `failure_reason` is cleared, as for every landed run; `ended_at` is kept, the
  end of the run's work, so durations and spend windows do not move;
- appends a `landed_outside` event with `commit`, `note`, `previous_status`,
  `failure_reason`, `branch` and `previous_head_commit`, so the original failure stays
  recorded; `helmraiser log` shows it on one line;
- adds a first bullet to the run's `summary.md` saying the landing was outside Helmraiser
  and not verified or reviewed by it. The sections below it still describe the run as it
  ended, and `report` marks the entry (`landed_outside` with `commit` and `note` in
  `--json`);
- removes the run's worktree when it is clean, with a `worktree_cleaned` event whose
  `reason` is `landed_outside`, because the cleanup sweep only looks at `failed` and
  `aborted` runs. A dirty worktree, untracked files included, is kept and reported
  (`dirty: N paths`), and the sweep will not take it later. The branch is always kept;
- lists the queued tasks that can now start, as `approve` does.

It writes no approval row: nothing in Helmraiser approved the merge. Earlier failed or
aborted runs of the same task count as `superseded` for `helmraiser clean`. A later
`retry` of the run is refused, as it is no longer `failed`. Helmraiser does not check that
the commit contains the run's work.

With `--json` the result is `recorded: "landed_outside"`, `run_id`, `task_id`,
`status`, `previous_status`, `failure_reason`, `commit`, `base_branch`, `branch_name`,
`note`, `worktree` (`action` `removed`, `kept` or `absent`, plus `path` and `detail`) and,
when there are any, `eligible` as for `approve`. A refusal is `{"error": "…"}`.

### Reworking a run awaiting merge approval

`helmraiser rework <run-or-unique-prefix> [--note "<feedback>"] [--rebase] [--json]` accepts
`awaiting_approval` with a pending **merge** approval, not a pending plan. It also accepts
`landed` runs with `delivery: pr`, a recorded `pr_url`, and a PR that
`gh pr view <url> --json state` reports as `OPEN`. Closed or merged PRs and non-PR
landed runs are refused (exit 2); gh failures or invalid responses are reported (exit 1).
One of
`--note` and `--rebase` is required. Supplied empty or whitespace-only notes and
ineligible states are refused (exit 2); missing
worktree, branch or metadata is an operational error (exit 1). Valid notes are
preserved verbatim. The request closes that approval as `rework` and atomically records
`rework_requested` with the note, last review's findings, round and budget extension.
It hands the same run back at `resuming` / `implement`, without spawning pi.

For an open PR, reopening retains the run ID, branch and PR URL without changing historical
approvals. It fetches the published branch from `origin` and restores a removed worktree
at that head; local-ahead or divergent branches and dirty trees are refused, never reset.
A recreated worktree runs setup again before implementation. The ledger records
`rework_reopened` and clears the delivered head. Dependents stay held during rework and
until the newly delivered head is in their local base. The same notes, governed gates and
bounded allowances below apply. Subsequent `approve` updates the **original PR branch**
with a non-force fast-forward push, without opening another PR; `land` waits for CI as usual.

`--rebase` (alone or with `--note`) is for a run whose base branch moved, including
one parked by `finalize_refused`. It is refused (exit 2) when the local base branch
is already contained in the run branch; use plain `rework --note` then. The target
is the local base branch for both `merge` and `pr` delivery; base integration does not
fetch that base (reopening does fetch the PR branch).
For an unpublished branch, the first prompt starts with a rebase request: `git rebase <base branch>`, resolve
every conflict keeping both sides' intent, `git add` and `git rebase --continue`.
Never leave a rebase in progress: if resolution is impossible, abort it and answer
`BLOCKED: <reason>`. Until the branch contains the base branch tip and no rebase
is in progress, a failing `rebase` verify result goes back to the implementer.
Then the tip becomes the run's base commit, and verify, evidence and review run
again, ending at merge approval as for any rework. With `--rebase` alone the
recorded note is `Rebase the run branch onto the current base branch.` and no old
findings are carried. For unpublished branches, `approve` still rebases automatically when it can.

For an already-published PR, `--rebase` instead asks the implementer to
`git merge --no-edit <base branch>`, resolve conflicts preserving both sides' intent,
and complete the merge. Published commits must not be rewritten or force-pushed.
Unfinished merges or rebases fail the integration gate; abort and report `BLOCKED:` if
resolution is impossible. Approval never rebases an existing PR: if its base moved again,
it refuses with a hint to use `rework --rebase` and repeat the governed gates.

Like `retry`, rework first re-reads the task file and makes it the run's snapshot, so
edits made since reach the run; an invalid file refuses the rework (exit 2). The worker
keeps the branch, earlier commits, worktree and delivery mode (`merge` or `pr`). The
first prompt contains the approver's note (after any rebase request), then reviewer findings (minor findings are
required feedback), then task context and acceptance commands—not a stale verify
failure. Notes do not change declared paths or acceptance commands; the task file does.
The reviewer gets every rework note of the run, oldest first, under `## Owner's rework
requests`, as amending the task brief and acceptance block. Approved plans remain available. The implement model starts a
fresh escalation ladder, followed by fallback only within the remaining allowance.
Normal verify → evidence → review and review-findings feedback run again. Success
returns to merge approval, **even under the original `auto` policy**; only a subsequent
`approve` performs delivery. Failure follows the usual failure path.

For original task iteration cap `N`, each accepted request grants `A = min(2, N)` new
iterations beyond those already used. This is **one shared cap** across primary,
fallback and automatic severe-finding repairs. Unused iterations from earlier rework
requests do not carry forward. Each request adds `max_cost_usd × A/N` dollars and
`max_minutes × A/N` minutes (fractional minutes are not rounded up). After `R` requests,
cumulative cost/minute caps equal the original task caps multiplied by `1 + R × A/N`.
Prior cost and completed active-run minutes count; approval and worker-queue waits do
not. The reviewer retains its timeout floor and exemption from implement-budget
termination, while its usage still accumulates. Later resumes retain these recorded
extensions without granting another one.

`summary.md`, `report` and `status` show ordered rework rounds and full notes. Report
JSON adds `rework_rounds` and `reworks` only for reworked entries; status JSON also
exposes history for awaiting and recent terminal runs independently of summary files.

### Running the queue

`helmraiser work` runs the queue in the foreground, in a terminal, one run at a time,
and exits when nothing is eligible. `--project <repo>` (repeatable) limits it to those
repositories; without it, `work` serves every project but those in
`worker_exclude_projects`. A `work` loop reconciles orphaned runs of the projects it
serves only.

`helmraiser work --watch` keeps running instead: it is the long-running worker. It runs
each run in its own child process (`python -m helmraiser work --once --json --project
<repo>` with the same data directory and caps flag), up to
`max_parallel_runs` children at once globally. Independently, `max_runs_per_project`
limits each project's nonterminal runs (default 1); higher limits permit only opted-in,
disjoint, dependency-independent tasks. A fresh child temporarily blocks further
starts of its project until its run is registered through its task lock and PID. Each time a slot is
free it starts projects with a run handed back to the worker first (a plan approval,
`rework`, or `retry`), then queued tasks. Only after runnable work is exhausted, it fills spare slots
with eligible focus-project/lens scouts using effective per-lens automation. A scout is a supervised `python -m helmraiser scout
<repo> --json` child, occupies one parallel slot without incrementing `--max N`, never
overlaps another child for its project, and inherits the worker's committed-code snapshot
environment. Foreground `--max-parallel` drains do not scout. The worker starts nothing
while the queue is paused, for a task that `work` would skip (invalid, provider out of
quota, over the spend caps, or under an optional [Leeboard hold](#leeboard-optional)), or for a project outside its scope. When nothing is eligible
it waits and re-checks every `worker_poll_seconds`, and at once when a child ends; it exits only on `worker
stop`, after `--max N` runs, or on an error outside any one project (the ledger, the pi
version check at startup). A child that ends without a run (an error such as
unavailable isolation, or another worker took the run first) keeps its project out for
5 minutes. `--max-parallel N` without `--watch` runs the same way but exits once
nothing is running and nothing is eligible. `--watch` and `--max-parallel` cannot be
combined with `--once` or `--dry-run`.

Dependency-held PR base checks run on polling ticks even when unrelated projects
occupy all slots. They respect project scope and never run while the queue is paused,
while that project has any nonterminal run (including awaiting approval or resumable
runs), while it has a child, or while it is backing off. Merge delivery is excluded.
Sync failures are logged without stopping the worker and retried after the per-base
interval; successful synchronization lets normal selection re-evaluate the hold.
Stop requests prevent further checks.

An error in one project's work does not stop the worker: reconciling that project's
orphaned runs (`stage` `reconcile`), loading and checking its next task, which reads the
project's `.helmraiser/project.toml` (`task`; a key the running code does not know fails
here), starting its child (`start`), or a child that exits non-zero without a run
(`run`), or spawning/running a scout (`scout`). The worker prints a `project_error`
event, lists the error under `worker.errors` in `status --json` (and in `status` and
`worker status`) until that step works again, and leaves the project out for 5 minutes
while it serves the others. Existing siblings continue; a sibling started before a
run infrastructure error cannot clear it merely by finishing successfully. Orphans
and restored children are tracked by run identity, not collapsed by project.
A failed scout does not stop the worker. Once
per distinct error it also writes the traceback, when there is one, to its log and
sends a desktop notification.

`work --watch` runs committed Helmraiser code only, so uncommitted edits in the checkout
never reach its runs. Started from the checkout (the editable install), it exports
`src/helmraiser` of HEAD with `git archive` into `<data-dir>/code/<commit>/` (through a
temporary directory renamed into place; an existing snapshot is reused), checks that
the snapshot imports, and re-execs itself with the same argv and pid, with
`PYTHONPATH=<snapshot>/src` in front and `HELMRAISER_CODE_SNAPSHOT=<commit>` and
`HELMRAISER_SOURCE_REPO=<checkout>` set. Its run and scout children inherit that
environment, so they import the same snapshot. On its snapshot it records the commit as `code` in
`worker.json` (`worker status` shows `code: 30f784a (snapshot)`, `status` shows it on
the `Worker:` line), prints `{"code_snapshot": {commit, path}}`, and deletes older
snapshots except those still used by running children and the two most recent other
snapshots. When HEAD does not import it does not start
(`cannot serve from a code snapshot of …`), and when the markers are set but `helmraiser`
does not load from the snapshot it refuses to serve. Setup, verify and evidence steps,
and the run's own git commands (so the project's git hooks), get the environment
without the snapshot entry and markers, so they test the worktree's code. A Helmraiser installed outside a git checkout runs as it is. Interactive commands
(`work --once`, `status`, `approve`, …) always run from the checkout.

`work --watch` restarts itself on new Helmraiser code. Every `worker_poll_seconds` it
reads, with `git rev-parse`, the tree of `src/helmraiser` at HEAD of the checkout (the
committed state only). When a commit changes that tree (a merge into the checkout; not
a docs-only commit, and not an uncommitted edit), it exports a snapshot of the new
commit, checks that it imports, and immediately re-execs onto it without waiting for
running runs or scouts. Children keep running on their original snapshots. Before
re-exec it saves a private `restart_children` handoff in `worker.json`; the new image
adopts those children before scheduling or orphan reconciliation. They still occupy
parallel slots and exclude their projects from new starts; their output and completion
notices are handled normally when they finish. Missing or reused child pids release
their slots, and unfinished runs are reconciled.

The argv and pid, and so `worker.json`, stay the same; the record gains `restarted`.
It notifies `worker restarted on new code (<old> → <new>)`. `worker stop` still starts
nothing new and waits for all children, adopted ones included; a pending stop takes
precedence over restart. When the new snapshot does not import or re-exec fails, it
stays on its old snapshot with its children, keeps serving, notifies why, and tries
again only after the next change. It does not restart under `--max`, outside a
git checkout, or with `"worker_restart_on_code_change": false`.

`helmraiser queue pause` stops new runs and scouts; `queue resume` permits starts again.
Use the global queue pause to stop all starts, including scouts; see also [Leeboard holds](#leeboard-optional).

The background worker never ends silently: when it exits for any reason but `worker
stop` (an error, SIGTERM, `--max N` done), it records `exit` in `worker.json`, which
`helmraiser status` shows as `Worker: exited (error: …)`, and sends a desktop
notification (`worker exited (error: …)`). A SIGKILL cannot be recorded.

With `--json` the worker prints one line per event: `{"started": {pid, project,
run_id, kind, started_at}}`, `{"finished": {…, "exit_code", "runs": [outcome…], "output":
[the child's other lines]}}`, `{"scout_started": {…}}`, `{"scout_finished": {…,
"exit_code", "scout_id", "outcome", "drafts", "report", "output"}}`,
`{"skipped": task, …}` (once per task and reason; see [Leeboard events](#leeboard-optional)),
`{"base_sync": {project, status, reason, base_branch, remote, upstream,
from_commit, to_commit, upstream_commit, …}}` for no-run base attempts (with `detail`
on failures; foreground `work --json` emits this shape too),
`{"paused": …}`, `{"reconciled": […]}`, `{"project_error": {project, stage, error,
since, retry_at}}`, `{"code_snapshot": {commit, path}}`, `{"code_changed": {from, to,
running}}`, `{"restarting": {from,
to}}`, `{"restart_failed": {to, error}}`, `{"stopping": true, "running": N}`, and the
usual final `{"runs", "paused", "held_providers", "summary"}` object. It sends one
desktop notification per finished run (`<task>: landed — $0.42, 12.5 min`) instead of
a batch notification, plus `N drafts for <project> — helmraiser drafts` when a scout
writes drafts and the error/restart notifications above; zero-draft scouts send no draft
notice. Current skips are also available under `worker.skips` for the recorded
background worker. `--no-notify` or `"notify": false` turn notifications off.

To keep the worker running after the terminal closes, start it in the background:

```bash
helmraiser worker start              # work --watch in the background
helmraiser worker start --project ~/Work/a --project ~/Work/b --max-parallel 3
helmraiser worker status             # running/exited/none, its runs and errors, log tail
helmraiser worker stop               # lets running runs finish, then stops
```

`worker start` spawns `python -m helmraiser work --watch --json` detached (it survives the
terminal), appends its output to `<data-dir>/worker.log`, and records the pid, start
time, argv, log path, project scope and parallel limit in `<data-dir>/worker.json`; a
second start is refused while a worker is running. The running worker lists its runs in
flight and its project errors in `<data-dir>/worker.active.json`, which `worker status`
and `status --json` show. `worker status` prints the last five log lines as one short
line per event (a finished run as `finished <project> (pid, exit 0): landed <task>`),
cut to 120 characters; `worker status --json` keeps them raw in `tail`. `worker stop` only asks: the worker starts nothing new, lets its running runs
finish and prints the usual batch summary (`stopped: on request`), so no run is killed
mid-flight. Use `helmraiser abort <run-id>` for an immediate stop of one run. The worker
lowers its own soft `RLIMIT_CORE` to 0 at startup, so it and every child process it
spawns write no core dumps.

Use `helmraiser work --dry-run` first to print the first spawn's argv, directives and
prompt without running anything. If another worker already holds a queued task's lock,
`work` skips it without failing the task and continues to the next queued task.

## Leeboard (optional)

Leeboard (`lb`) optionally adds holds that pause fresh runs or landing, a brief in
implementer directives, and run-authored entries in reports. Without `lb` on PATH,
the integration is not in use: scheduling and landing proceed silently, directives
and reports omit its context, and doctor labels it optional. An installed but failing
executable remains fail-soft with the diagnostics described below.

### Leeboard holds

`helmraiser land` checks `push` and then `merge` holds before approving or changing refs;
an active hold exits 3. Missing `lb` silently continues; broken `lb` warns and continues,
as below.

An active Leeboard (`lb`) hold whose `blocks` contains **`helmraiser-run`** skips fresh
queued tasks in its scope. The task stays `queued`: no run row, worktree, pi session,
or failure is created. A watching worker starts it on the first scheduling pass after
release or expiry, without re-intake. Foreground `work` exits when all tasks are skipped;
watch mode keeps polling. Running and resumed runs are unaffected. `--no-caps` does not
bypass a Leeboard hold; release the hold instead.

Helmraiser checks the task's own project directory. Global and machine holds apply to
all projects; project-scoped holds require the project to be registered in Leeboard
with `lb project add`. Other projects may still start. The console (or background
`worker.log`) names the reason:

```text
skipped <task>: leeboard hold: [hold-id] Tunnel open (release: close tunnel)
```

The supervisor announces each task once while the reason is unchanged, and again when
the hold set changes. The recorded background worker publishes **all** current skips,
including quota, caps and invalid tasks, as `worker.skips: [{task_id, reason}]` in
`status --json` and `status.json`; human status prints `Skipped: <task>: <reason>`.
Foreground `work --watch` has console output only. These are live reasons, not durable
ledger history (D8); skips have no run to attach events to. No hold-specific desktop
notification is sent.

With `--json`, worker `{"skipped": task, …}` events (once per task and reason) for
Leeboard carry `"leeboard_hold": [{id, title, release, until}]`, keys only when set,
with a capped raw `"text"` fallback when holds cannot be parsed. The worker emits
`{"warning": text}` when an installed Leeboard check is unavailable (also emitted by
foreground `work --json`). Unlike the global queue pause, a hold tagged `helmraiser-run`
skips fresh queued tasks in its scope, not scouts.

There is at most one `lb check` per project per scheduling pass (normally every
`worker_poll_seconds`, default 30 seconds; child completion may trigger another pass).
Projects already at their run limit and tasks skipped for an earlier reason cost no
check. Each supervised child checks again: if a hold appears between parent and child,
the child exits 0 without a run and the existing five-minute no-run backoff applies.
Checks time out after five seconds per project. Missing `lb` silently schedules normally.
An installed but broken `lb` **warns and schedules normally**, once per unchanged failure
streak; a changed failure or failure after recovery warns again. `helmraiser doctor`
reports informational executable and store path/existence diagnostics without affecting
readiness; an absent executable says `not installed (optional)`, so an owner can notice
an unavailable gate. Only exit 3 holds tasks, even with malformed output; no Leeboard
store is created or changed by Helmraiser's checks.

### Leeboard brief

Helmraiser generates a Leeboard brief in its host process with `lb brief --cwd <worktree>`,
falling back to the task's project if there is no worktree yet. Leeboard resolves a
worktree to its primary checkout's registered project. The source is owner- and
agent-authored holds, grants, rules, references, inbox and repo state in `$LEEBOARD_HOME`
(or `~/.leeboard`), with `LEEBOARD_HARNESS=helmraiser` and `LEEBOARD_SESSION=<run id>`.

The brief appears under `## Leeboard` after project notes in implementation directives
only, not plan, review or scout directives. It is advisory context: the task file and
directives win on conflict. The implementer is told to skip other brief or recall steps
and not run `lb brief` again. This context is distinct from project notes and from the
scheduling Leeboard hold gate, which can skip a fresh task before a run starts.

Scout inputs include the project's Leeboard hold, grant and rule titles from `lb brief`
when `lb` is available.

With an available brief, implementers get guidance for rare writes: entries must change
an agent's actions, be unavailable from the repo or tools when needed, and have an author
and end condition. Progress, code facts and unfixed defects belong in the final message.
Workers may hand off context beyond the diff and final message only to a validated Claude
Code origin session, ask unresolved questions via
requests while continuing useful work, or record lasting rules learned the hard way.
Coordination lifecycle changes belong to the owner and interactive sessions; workers must
not retry or bypass exit 6 (not permitted), or put credentials in titles or bodies.
Entries created by a run appear in `helmraiser report`.

Fetching has a 15-second timeout. Text is capped at 8000 characters, including the
`… brief truncated` marker when Helmraiser must cut at a line boundary. Missing `lb`
produces no Leeboard context or writing guidance. An installed but failing `lb` is
fail-soft: directives instead say `Leeboard is unavailable (<reason>): skip brief
and recall steps.` Successful empty output still enables writing guidance. Each active run instance caches
its first result across iterations, including empty or unavailable results; a resumed
instance fetches on its first directive write. Dry-run previews never fetch a brief,
because `lb brief` can move the session's Leeboard cursor.

### Leeboard entries in reports

For each listed landed, awaiting-approval or failed run, `helmraiser report` shows
its current Leeboard entries in a `### Leeboard entries` section next to the run's
verdict. Each item includes its id, kind, current status, scope and title; released
or superseded entries remain attributed to their authoring run. Titles are
single-lined and clipped to 200 characters in text output.

Reports query the CLI, not store files: one
`lb list --session <run id> --harness helmraiser --json` per included run,
sequentially, on every report. This costs about 80 ms per run, so a large
`--since 30d` window can add seconds. Each run shows at most 20 entries in Leeboard's
newest-first order, with a count of any remainder (`leeboard_more` in JSON).
`--json` includes a `leeboard_entries` list on every run, empty when none.

Lookup is fail-soft: missing `lb`, errors or invalid replies show no entries,
indistinguishable from a run that wrote none. After a missing executable or a
10-second timeout, remaining runs are not queried; other failures affect only
that run. No Leeboard section is added to `summary.md`.

### Leeboard access

The host-generated brief reaches the implementer through its directives; this delivery
does not change sandbox read permissions.

Setup, verify and evidence commands strip `LEEBOARD_HARNESS` and `LEEBOARD_SESSION`
(the worker's interactive identity) from the worker's environment. pi's allowlist
includes `LEEBOARD_HOME` when set (otherwise `lb` uses `HOME/.leeboard`).
pi sessions also receive `LEEBOARD_HARNESS=helmraiser` and
`LEEBOARD_SESSION=<run_id>` (scouts use their supplied scout ID), not the worker's
inherited Leeboard identity. Setup, verify and evidence do not receive this injected
identity.

Ordinary pi sessions also reopen the resolved `lb` install's nearest package root
(containing `package.json`) read-only when other-project filtering hides it. This
includes its `dist/`, `node_modules/` and other package files, not the rest of the
sibling project. Credential, configured and data-directory denials still win.

Ordinary pi sessions can write the whole existing effective store
(`LEEBOARD_HOME`, else `~/.leeboard`), including entry directories, `log.jsonl` and
session cursors. An existing store `.git` is denied writes to prevent planting hooks
for the owner's later unconfined commits. Helmraiser neither creates nor grants a
missing store. These additional store-write and install-read grants do not apply to
readonly (intake) sessions, setup, verify or evidence.

Whole-store writes and environment attribution are not a security boundary against
direct entry-file edits or deliberate `LEEBOARD_HARNESS` overrides. Explicit task/project
`env` overrides still win over the injected identity and inherited store location.

## Troubleshooting

- **Verify fails in the worktree but passes in your checkout.** A run's worktree
  (`<worktree_root>/<repo-name>-<hash>/worktree-<id>-<run>` by default) is a fresh checkout without
  `.venv`, and your editable install points at the primary `src`. Give the task or
  `project.toml` a `setup:` that builds a venv in the worktree, and call `.venv/bin/...`
  in verify.
- **Coverage threshold fails a verify step that runs a test subset.** `pyproject.toml`
  enforces `--cov-fail-under`, so run subsets with `--no-cov` (for example
  `pytest -q --no-cov tests/cli`).
- **The queue stopped with `paused`.** This is a global manual or timed queue pause;
  inspect `helmraiser status` and use `helmraiser queue resume` to clear it. Provider quota
  errors instead hold only that provider for 30 minutes. An implement-stage quota
  immediately escalates to an unheld fallback on another provider when available;
  otherwise the run fails with `quota: …`. Tasks using other providers continue.
- **`finalize_refused: …`.** Landing is a local `git merge --ff-only` in the primary
  checkout. It is refused when that checkout is dirty (outside `.helmraiser/`), detached,
  on another branch, or has moved since the run started. The run stays
  `awaiting_approval`. Clean up or reset, then `helmraiser approve <run>`. If only the
  base branch moved, `approve` automatically rebases the run branch onto it and merges;
  a conflicting rebase is aborted and refused. `helmraiser rework <run> --rebase`
  hands the run to the implementer to rebase and resolve; verify, evidence and review
  run again before the next `approve`. Nothing is re-verified after the rebase in
  the automatic `approve` path.
- **Headless Chrome aborts in verify or evidence with `process_singleton_posix.cc …
  socket() failed: Operation not permitted`.** Google Chrome and Chromium, including
  `--headless=new`, create the profile's singleton Unix socket at start and abort when
  srt's seccomp filter refuses it; no flag turns that off. Use `chrome-headless-shell`
  (Chrome for Testing's headless build), which has no singleton and runs under the
  verify and evidence policy unchanged. Install it outside srt (its download host is not
  in `allowed_domains`), from `setup` or once by hand into a directory that is readable
  in srt, for example `~/.cache/puppeteer`. With puppeteer-core launch it with
  `executablePath` set to that binary, `headless: 'shell'`, `--no-sandbox` and a
  `userDataDir` under the worktree or `/tmp`. A page served on `127.0.0.1` from inside
  the step loads, over the DevTools WebSocket or `pipe: true`.
- **`skipped …: held by run …`.** Only one run per task at a time. `work` leaves the
  task queued and tries the next task; abort the holder or wait for it to finish.
- **`auth: …` or a pi version error.** Log in with pi for the profile's provider, or
  make sure `pi --version` (the `pi_bin` setting) prints a version.
- **`pi_contract: …` and `Queue paused: pi <version> broke the JSON usage contract`.**
  The installed pi stopped reporting readable usage on assistant messages, so budget caps
  cannot be enforced. In any stage (plan, implement, fallback, review fix, review), the
  run fails at once with no retry, next iteration or escalation. Its branch and worktree
  are kept and the usage recorded so far stays. The queue pauses for every project, so
  the worker starts nothing else. Check pi's changelog against `pi/adapter.py` and
  `pi/events.py`, fix Helmraiser, then run `helmraiser queue resume`. A `pi_contract`
  failure is not retryable: re-intake the task to queue a new run if needed. `intake` reports the same failure as
  `intake session failed (contract): …`.
- **`protocol` errors after a pi update.** pi's JSON output changed in some other way
  (for example no `agent_settled`). An implement session is retried once after 30 s,
  then counts as a failed iteration. Check pi's changelog against `pi/adapter.py` and
  `pi/events.py`.

## Development

```bash
make install-dev   # pip install -e ".[dev]"
ruff check src tests && black --check src tests && mypy src/helmraiser
python -m pytest -q -p no:cacheprovider      # coverage floor 70 %
```

The pre-commit hooks run black, `ruff --fix` and mypy. The test suite never runs a real
model. pi is replaced by `tests/mocks/fake_pi.py`, a scripted `pi` on `PATH`, or by
`tests/mocks/scripted_pi_adapter.py`. Do not use paid agents (Claude) in automated
pipelines; Helmraiser is pi-only. See [AGENTS.md](../AGENTS.md) and
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Documentation

- [Execution model](architecture/execution-model.md): the governed loop, ledger vs project state, abort, run directory
- [GLOSSARY.md](../GLOSSARY.md): the words Helmraiser uses for its concepts
- [CURRENT_STATE.md](../CURRENT_STATE.md) and [TASKS.md](../TASKS.md): where things stand and what comes next
- [CHANGELOG.md](../CHANGELOG.md): release history

## License

MIT (see [LICENSE](../LICENSE)).
