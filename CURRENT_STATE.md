# Helmraiser — Current State

- Scout drafts omit delivery and inherit the project default (fallback: merge); intake prompts show effective setup, environment variable names only, and explicit-tool-path guidance.

- Scout lenses have CLI/JSON settings with ledger overrides, per-project/lens automatic cadence, and scout-only draft listings by default (`--all` includes manual drafts).

_Updated 2026-11-14._ What is built and true now. Planned and in-flight work is in
[TASKS.md](TASKS.md).

## What Helmraiser is

Helmraiser is a governed, pi-only loop for unattended, eval-backed tasks:
`add → intake → queue → work → approve | reject | rework`. Deterministic `verify`
commands decide done; failures return to the same implementer session, with bounded
iterations and escalation to a fallback model. Evidence and an independent,
read-only advisory review precede policy-controlled delivery. The central ledger
(`~/.helmraiser/helmraiser.db`) records the run state, usage and outcomes.
Independent review receives all approved/declined scope decisions, their paths and owner notes, oldest first.
`approve` (including `--decline`), `reject`, `rework` and `record-landed` accept `--note -` to read a non-empty note from stdin, removing one trailing newline.
Parsed, non-uncertain reviews with severe or minor findings receive bounded in-run feedback; review-driven fallback escalation remains severe-only.

Rework resumes the same worktree with feedback and additional bounded allowances,
then verifies and reviews again. `rework --rebase` lets the implementer resolve a
moved base before those gates. See [the execution model](docs/architecture/execution-model.md).
Open PR-delivered runs can be reworked and approved back onto the same PR, with serialized per-run reopening, history-preserving base merges and dependent holds.

A fresh install has no models and refuses model work until the required roles are configured.
`intake --check` preflights verify/evidence on committed HEAD without ledger writes; `--check --yes` queues only after green/expected-red results.
Intake adds an advisory note (`notes` in `--json`) when `budget.max_cost_usd` is at least $20, suggesting smaller tasks linked with `depends_on`.
New `intake --yes` queueing adds detected related tests to `paths:` (all up to 20, none above); check-only previews them, and recorded additions are marked as permitted test scope.

Without `lb` on PATH, Logbook is treated as not in use: runs and landing proceed silently, directives and reports omit its context, and doctor labels it optional.

## Public-release documentation

The README is the introduction and disposable-repository first-run guide; detailed
CLI/configuration behavior lives in `docs/reference.md`. The slug CLI example uses
process isolation, owner approval and prewritten behavioral tests. Its acceptance
was exercised under real srt: missing target → expected red, temporary reference
implementation → green, incorrect output → rejected, without model calls or queue writes.
Model-selection guidance distinguishes configuration from historical personal trials.
Remaining public-release checks are in `TASKS.md`.

## Known gaps

- **Failed reopened PR runs.** A reopened run that fails (including setup failure) cannot be retried, reworked or landed while its PR stays open; recovery is a hand commit on the PR branch or closing the PR.
- **Git maintenance needs idle time.** A project that always has a live run never
  receives sweep maintenance. External Git commands in a repository without
  `maintenance.auto=false` can still pack refs under a live session.
- **A recorded landing's dirty worktree stays.** `helmraiser record-landed` removes the
  run's worktree only when it is clean. The cleanup sweep looks only at failed and
  aborted runs, so a dirty one is kept until the owner removes it by hand.
- **Downstream draft display.** Consumers other than the Omarchy widget do not yet
  show or manage scout drafts.
- **No live steering.** Implement sessions can run over `pi --mode rpc` when
  `pi_rpc_implement` is on (default off; one real run, 2026-10-26). Helmraiser sends no
  steer or follow-up commands, and there is no in-run approval dialog.

## Watch out for

- **Run references.** `helmraiser show` accepts a run ID, unique run prefix, or short/full
  task ID; task references select the latest run, including queued tasks' historical runs.
  Human `show` leads with the PR URL when available; PR approval ends with the bare
  URL, so `helmraiser approve … | tail -1` returns it. The status payload's count keys are
  `queued_count`, `awaiting_count` and `failed_24h_count`.

- **Live subscription usage.** No subscriptions are enabled by default. Known
  templates fill explicitly named blocks only; existing named subscriptions retain
  their settings. OpenCode Go and Codex share Omarchy record reading, refreshing
  through their collectors after 15 minutes or when `limitsStale` is true.
  Stale records remain usable if collection fails; OpenCode Go falls back to its usage
  API without usable record windows. Helmraiser no longer publishes a duplicate record.

- **Implement-session Git writes are limited.** In the primary `.git`, sessions can
  write only their own branch's ref directory (plus objects and their worktree's
  admin directory); `git stash` fails there. So do Git steps that must delete a ref and
  take `packed-refs.lock`, such as dropping a commit during `rework --rebase`
  (`rebase --skip`). The implementer reports `BLOCKED:`. Drop the commit on the host in
  the run worktree. Helmraiser still records the old base, and verify's path-scope check
  then counts the base's own commits as the run's changes. Use
  `helmraiser retry <run> --adopt-base` to record the new base and retry.

- **Failed/aborted worktrees have limited retention.** Clean trees are removed after
  seven days or once superseded by a later landed run, by `helmraiser clean` or the
  hourly watch-worker sweep. Retry requires the worktree to remain; branches stay.

- **Pause the global queue while a production tunnel is open.** Use `helmraiser queue
  pause` to stop new runs and scouts, then `helmraiser queue resume` when safe. Do not
  bypass that pause with manual `scout --force`.

- **Handbacks need a worker serving the project.** Plan approval, scope approval,
  rework and retry hand work back rather than running it inline. Check worker scope
  and exclusions; without a suitable worker, use `helmraiser work --project <repo>`.

- **Parallel limits.** The global default is `max_parallel_runs = 2`; the project
  default is `max_runs_per_project = 1`. Higher project limits require disjoint
  declared `paths:` and dependency independence. Approval waits and resumptions
  consume capacity. Keep independently launched work loops' project scopes disjoint.
  Running-worker status takes its parallel limit from the matching active payload,
  including after self-restart, and otherwise retains the recorded value.

- **Workers use committed snapshots.** Uncommitted edits never reach run children.
  On code changes the worker restarts immediately and adopts existing children;
  those children retain their original snapshots across the restart.

- **Widget reloads.** Use `omarchy restart shell` when QML edits are not picked up.
  Disabling and re-enabling can reset `helmraiserBin`; restore it with
  `omarchy bar set zsolt.helmraiser helmraiserBin <repo>/.venv/bin/helmraiser` if needed.

- **External worktrees.** Defaults live outside the repository under
  `$HELMRAISER_DATA_DIR/worktrees/<repo-name>-<path-hash>`. Nested overrides can upset
  scanners or a project whose tests reject a nested `.git`. Legacy worktrees and
  metadata are recovered in place, not moved.

- **pnpm's store must share the worktree's filesystem.** Configure `worktree_root`
  or project `worktree_dir` accordingly, and put a store such as `/data/.pnpm-store`
  in `[defaults.env]` and `[setup].write`. `helmraiser doctor` warns when the store is on
  another filesystem than the worktrees, is missing from `[setup].write` for sandboxed
  setup, or is not named while setup runs pnpm.

- **`BLOCKED:` normally fails the run, keeping its branch and worktree until cleanup.** Fix a
  bad verify/evidence block and use `helmraiser retry <run>` to re-verify and review
  without implementation. If code needs more work, fix the environment and use
  `helmraiser retry <run> --implement`: iteration and active-time allowances refresh,
  but cost remains cumulative under the original cap. With no commits and a clean
  worktree, either retry form restarts the run's work, keeping its history: setup only
  when needed, plan only when needed, then implementation and the normal gates.
  Commitless `setup:` and `services:` failures can also be retried, even with setup
  debris; a `services:` failure with commits resumes at verify. Re-intake starts from
  scratch.
  Exception: a clean new commit in a run with declared services can proceed to
  verify despite `BLOCKED:`, since only verify/evidence can access those services.

- **A `budget:*` failure retries only with room on all three caps, and stays cumulative.**
  Raise the task file first: `max_iterations: 12` means 12 in all. Later retries and
  approvals of that run keep counting from its start. `budget:iterations` often fires
  below `max_iterations`, because a rework has its own two-iteration allowance:
  `helmraiser log` shows `limit_source`.

- **Node projects.** A project with git hooks may need `HUSKY=0 npm ci` in setup and
  `HUSKY=0` in env. Put the required Node and package-manager binaries on `PATH` via
  `[defaults.env]`. Tests resolving built package output need a setup build.

- **Worktree-local tools.** Fresh worktrees have no `.venv`; the primary editable
  install points at the primary `src`. Build a venv in setup and use explicit
  `.venv/bin/...` commands so verify tests the worktree, not the primary checkout.

- **Pytest subsets need `--no-cov`.** Otherwise the full-suite coverage floor can
  fail a valid subset.

- **Local landing needs a suitable primary checkout.** Dirty, detached or
  wrong-branch checkouts block finalize. A moved base may require approval's
  automatic rebase, or `rework --rebase` when conflicts need implementation work.
  Shared PR approval first synchronizes the captured local base with its configured
  upstream, fast-forwarding safely or refusing before delivery if it cannot prove
  the base contains that upstream.

- **pi usage is a hard contract.** Helmraiser records the installed pi version but
  has no supported version range. Unreadable assistant usage fails the run with
  `pi_contract: …` and pauses the queue. Inspect `pi/adapter.py` and `pi/events.py`
  compatibility before `helmraiser queue resume`; other JSON breaks are protocol errors.

- **Install srt dependencies.** `npm install -g @anthropic-ai/sandbox-runtime`
  also needs `bwrap`, `socat` and `rg` on `PATH`. With version-managed Node, ensure
  its bin directory is on the worker's `PATH`.

- **srt settings and pi locks.** The supported settings shape includes
  `network.deniedDomains` and `filesystem.denyWrite`. pi needs `~/.pi/agent` writable
  for lock directories even when reading authentication. A `.git` write root needs
  explicit hook/config protection; srt's built-in nested-path denials do not cover it.

- **srt write/read gotchas.** Existing entries in `~/.pi/agent` are write-denied,
  but pi can still create new entries. srt runs one `bash -c` argument, capped at
  128 KiB on Linux. A read carve-out inside a denied directory can override writable
  paths beneath it with a read-only bind, so the policy denies around the run dir.
  Pi sessions and setup, verify and evidence launches rebuild settings and retry once
  when a denied path vanishes before bubblewrap starts the payload, recording
  `sandbox_retry` in the ledger and local events. An in-sandbox start marker prevents
  replaying started commands; an exhausted pi startup retry is terminal, not a
  provider/protocol retry or model fallback.

- **Sandbox temporary files.** Every sandboxed session and step gets
  `isolation.tmpdir` (default `/tmp/claude`) as `TMPDIR`. On a small `/tmp` tmpfs it
  can fill and fail a whole verify run; point it at a directory on disk. Helmraiser
  never cleans that shared storage. Preflight uses a removable
  `helmraiser-preflight-<id>` child under explicitly configured `isolation.tmpdir`, or
  the system temporary directory when unset, rather than the persistent runs directory.

- **Abort under srt.** srt can exit 0 after abort's SIGTERM. The loop checks project
  `state.db` and closes the session as `aborted` without retrying.

- **Manual JSON-mode smoke runs.** An open stdin pipe can make `pi --mode json`
  hang silently; close stdin with `</dev/null`, as the loop does.
