---
name: helmraiser-tasks
description: Use when writing or editing a Helmraiser task file, queueing with intake, reviewing a run, approving, landing, reworking, or retrying.
---

# Helmraiser tasks

Use these checks at the relevant stage. When installed by symlink, resolve README
links from this skill's source directory in the checkout.

## 1. Test assumptions before writing the brief

Prove each claim about a library, tool, service or sandbox with a one-line
`helmraiser preflight <repo> --run '<probe>'` before putting it in the brief. For
example, check a service container's reported address or a template engine's
sandbox behaviour. Plausible assumptions can be false in the actual sandbox.
See [Trying commands in the sandbox](../../docs/reference.md#trying-commands-in-the-sandbox).

## 2. Prove acceptance before queueing

Queue with `helmraiser intake <file> --check --yes`: it runs every `verify` and
`evidence` command on the unchanged base and queues only when each step is `green`
or `expected red` (fails only because a file the task creates is missing). Use
`--check` alone to look first. For a single command, use `helmraiser preflight`.
Expect only failures the task explicitly intends to fix; investigate any others. Intake proposals can choose an environment variable a
tool ignores or a type-check gate that already fails on the base. Broken
acceptance wastes implementation runs.

Preflight takes the **primary checkout**, tests its committed HEAD and uses
project setup/environment, not task overrides. Account explicitly for task-only
`setup` or `env` differences before treating the result as proof. Select the
project interpreter explicitly, such as `.venv/bin/python`, where applicable.
See [Trying commands in the sandbox](../../docs/reference.md#trying-commands-in-the-sandbox)
and [Task files](../../docs/reference.md#task-files).

## 3. Declare the complete scope

Put every file the change needs in `paths:`, including tests elsewhere that
assert the changed behaviour. For new queueing, `intake --yes` adds related tests
it can find (all when there are at most 20; none above that cap); `--check` alone
previews additions. Still grep for inventory, golden and fixture tests the detector
cannot find. Added tests are permitted scope, not required edits: update only what
the change forces and keep assertions at least as strict. Shared added tests can
serialize siblings. Before approving a scope request, widen any independent path
guards in verify commands too: neither intake nor scope approval rewrites them,
and omitted tests can stop an otherwise valid change.
See [Task files](../../docs/reference.md#task-files) and [Policies](../../docs/reference.md#policies).

## 4. Keep the brief safe to share

Briefs go to hosted models and into PR bodies. Describe weaknesses factually,
never as attack recipes, and omit private infrastructure details: the brief
leaves the local interactive session.

## 5. Queue through intake

`helmraiser add <file>` registers a draft;
`helmraiser intake <file> --check --yes --origin <kind>:<session>` queues it. A valid
hand-written acceptance block needs no intake-model call. Use the supported
origin kinds in [Workflow commands](../../docs/reference.md#quick-start): the queueing session
owns review, and registration alone does not start work.

Diff guards must use `$HELMRAISER_BASE_SHA`, so scope checks use the captured base
rather than a moving branch. Keep fetch/rebase commands out of task bodies;
leave base synchronization to Helmraiser's host workflow.
See [Task files](../../docs/reference.md#task-files) and [Policies](../../docs/reference.md#policies).

## 6. Schedule independent siblings

Same-project concurrency needs configured capacity, declared disjoint `paths:`
on every sibling and no direct or transitive `depends_on` relationship.
Awaiting-approval runs consume project capacity: review promptly to free slots.
These checks keep parallel edits from conflicting.

`depends_on` waits until the dependency's delivered head is in the local base:
merge and fast-forward that base. PR creation or remote merge alone is not enough;
dependents must start with the delivered code. Squash merging does not satisfy
the recorded-head ancestry check.
See [Running unattended](../../docs/reference.md#running-unattended) and
[Dependency holds](../../docs/reference.md#dependency-holds).

## 7. Check sandbox readability

The home directory is broadly readable except for built-in denials, including
credential stores such as `~/.ssh`, cloud CLI/browser data and `~/.secrets`, plus
configured `isolation.deny_read`. Project-level denials and other configured
restrictions also apply; home is not the complete read boundary. Check a path:

```bash
helmraiser preflight <repo> --run 'ls -A <path> | wc -l'
```

Credentials interactive agents use but Helmraiser runs must not read belong in
`~/.secrets/<project>/`. The sandbox is not a blanket home-directory privacy
boundary. See [Isolation](../../docs/reference.md#isolation) for read rules and exceptions.

## 8. Review before final approval

Inspect the diff, verify/evidence results and independent review before approving
**delivery**. For `delivery: pr`, approve opens the PR and only then starts its PR
CI. Every repository CI check applicable to touched files must already be a verify
step: approval is not a trial CI run.

While awaiting **merge** approval, fix with
`helmraiser rework <run> --note "<feedback>"`, never by editing the run branch by
hand. Treat delivery approval as final: PR delivery removes the worktree and
records delivery before the PR merges. Plan/scope approvals instead resume work.
Use `helmraiser land <run>` for governed PR landing.
See [Policies](../../docs/reference.md#policies) and
[Reworking a run awaiting merge approval](../../docs/reference.md#reworking-a-run-awaiting-merge-approval).

## 9. Recognize the cleanup/startup race

Helmraiser keeps preflight's temporary run directories outside `<data-dir>/runs`.
If a denied path vanishes before bubblewrap starts, Helmraiser rebuilds the sandbox
settings and retries startup once automatically, recording a `sandbox_retry`
event; commands that have started are not retried.

If a run still fails with a bubblewrap "Read-only file system" error naming
another run, check `helmraiser log <run>` for `sandbox_retry`, then use
`helmraiser retry <run>`. This can be a sandbox startup race, not an application
defect.
See [Isolation](../../docs/reference.md#isolation),
[Trying commands in the sandbox](../../docs/reference.md#trying-commands-in-the-sandbox),
[Monitoring](../../docs/reference.md#monitoring) and
[Retrying a failed run](../../docs/reference.md#retrying-a-failed-run).

## 10. Check the small CLI/environment traps

- Task `setup:` **replaces**, rather than extends, project setup. Preserve required
  environment preparation or later gates lose their dependencies.
  See [`project.toml`](../../docs/reference.md#projecttoml).
- `helmraiser show <ref> [--json]` and `helmraiser wait <ref>` both take a run ID, task ID
  or unique run prefix; for a task ID, `show` picks the task's latest run. `wait` waits
  for attention by default; use `--until end` for terminal completion.
  See [Showing one run](../../docs/reference.md#showing-one-run) and
  [Waiting for a run](../../docs/reference.md#waiting-for-a-run).
- Read the PR URL from `helmraiser show <run> --json`, field `pr_url`, rather than
  scraping human output: JSON is the machine interface.
- Before consuming `status --json`, read [Status contract](../../docs/reference.md#status-contract).
  Count keys end in `_count`: `awaiting_count` is a count, `awaiting`
  is a list. Treating one as the other breaks monitoring.

## 11. Keep tasks small

If a task needs a budget of $20 or more, split it into smaller tasks linked with
`depends_on`, each with its own acceptance block. Intake’s large-budget note is
advisory, not a queueing error.
