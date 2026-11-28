# Helmraiser — Tasks

Only current, evidence-backed work. Delete an item in the commit that finishes it;
[CURRENT_STATE.md](CURRENT_STATE.md) and git record what landed. Runnable work goes
through the Helmraiser queue (`.helmraiser/tasks/`, not tracked); this file says why it exists
and what comes next.

## Release

1. **Set the GitHub social preview** (Settings; there is no API) to
   `docs/brand/helmraiser-social.png`.
2. **Optional: a paid end-to-end run of the README quick start.** A clean-machine
   install passes it up to the paid run, without models configured.

## Live experiments (agreed 2026-11-25, building)

While an experiment is active, every queued task runs as a **pair**: two runs of the same
task, one per **arm**, each with the whole pipeline. A coordinator's blind judge picks the
better one, which lands; the other is rejected. Every task gets a best-of-two, and the
picks measure the setups. Arm A is the **control** (today's setup). Owner decisions from
the grilling session:

- **Definition.** One active experiment, written in `~/.helmraiser/config.json`: name, two
  arms, end condition (N decided pairs, default 30, and/or an end date), quota brake
  (default 85% codex weekly usage), optional `exclude_projects`. `experiment start` copies
  it into the ledger (arms frozen); `experiment stop` ends it. Ending reverts to arm A and
  never adopts a winner by itself.
- **Arms** override a closed list only: `models` (per stage, with thinking), `skills`
  (`default` = `~/.local/share/helmraiser/skills/`, a named set in
  `~/.local/share/helmraiser/skill-sets/<name>/<stage>/`, `none` = project skills only,
  `discover` = pi discovery) and `pi_rpc_implement`. The arm wins over the task file for
  the factor under test; the task's override is recorded. `experiment start` refuses arms
  that break the closed list or the reviewer-must-differ rule. Review, verify and evidence
  stay identical unless the review profile is the factor.
- **Forking.** At queue time, unless the task sets `experiment: false`, is plan-approval
  first, or its project is excluded. Above the quota brake, tasks run single-arm on arm A
  (**unpaired**, outside the comparison). Both arms start together, count as one
  `max_runs_per_project` slot (each counts against `max_parallel_runs`), get separate
  worktrees, and ignore each other in path-overlap checks. Each arm has the task's full
  budget.
- **Blind.** X/Y assigned at random per task; models, skills, arm names hidden in `show`,
  `status`, `log` and the widget until the pick; branch, worktree and session names carry
  no arm. Diffs, verify/evidence, Helmraiser's review, cost, time and iterations are
  shown. `--reveal` exists for debugging and marks the pair unblinded.
- **The task is the unit.** `wait <task>` returns when the pair is ready; `show <task>`
  shows X and Y side by side; `diff <task> X|Y`; abort, drop and retry act on both arms.
  `approve` is refused for pairs.
- **Picking.** `helmraiser pick-packet <task>` writes one file: the brief, both diffs,
  verify/evidence results, review verdicts and findings, cost, time, iterations — no
  models, skills, arms, run IDs or paths. The coordinator gives only that file to a fresh
  judge sub-agent (`oracle`, fixed prompt in the helmraiser-tasks skill), then runs
  `pick <task> X|Y|tie --reason "…" --judge "<harness/agent (model, effort)>"`. Pick
  approves the winner (normal merge or PR delivery), rejects the loser keeping its
  branch, and reveals the setups. Judge on the delivered result; rework only the winner
  afterwards (counted against it). Ties land the cheaper arm, then fewer iterations.
  Picks stay a coordinator action: Helmraiser never asks Claude to judge, and any pi judge
  is advisory, logged only for agreement.
- **Failure.** Only one arm reaches approval: it is picked automatically, a decided win
  **by failure**. Both fail: `retry` retries both.
- **Ledger.** Schema 12: `experiments` (name, frozen arms, end condition, brake, status,
  times) and `pairs` (task, X and Y runs and arms, pick, reason, judge, picked at, how
  decided: judged / by failure / tie), with one automatic 11→12 upgrade after a backup.
- **Report.** `experiment report [<name>] [--json]`: judged win/tie/loss, wins by
  failure, median cost/time/iterations, review verdicts and post-pick rework per arm, a
  sign-test p-value on judged pairs, the reasons.
- **First experiment: running.** `hub-skills-vs-none`, started 2026-11-25 22:26 UTC: A
  `default` (hub skills) against B `none`, all projects, 30 decided pairs or 2026-12-09,
  brake 95%. Check it with `helmraiser experiment status|report`. Leeboard rule `a21b` tells
  coordinators to pick pairs through a blind judge.

Slices, in order (1, 3 and 4 interactive; 2, 5 and 6 may run as Helmraiser tasks):

1. Done (b72bc5c, 98b1779): ledger schema 12; the live ledger is upgraded.
2. Done (PR #37, #38): `experiment start|stop|status` with arm validation; an arm
   applied to a run (`GovernedRun(arm=RunArm)`, the `experiment_arm` event).
3. Done (163454b): a task forks into a pair when a worker claims it (one task row, two
   runs; Y from X's base commit), pair slot, sibling exemption, brake, exclusions,
   `experiment: false`. Known gaps are in CURRENT_STATE.md.
4. Done (4abdcb3, a0f1299): pick, auto-pick, retry/abort on pairs, end conditions; blind
   `show`/`log`/`wait`/`status`/`attach` with `--reveal`, `wait`/`show`/`diff` by task,
   `pick-packet`, `rework` refused before the pick.
5. Done (PR #39): `experiment report [<name>] [--json]` with the sign test. Its docs go
   in slice 6.
6. Done (611b2ae, a0b97f1, this commit): GLOSSARY, the helmraiser-tasks "Picking a pair"
   section with the judge prompt, the widget's "pair awaiting pick" label, AGENTS.md.

## Scout follow-ups

- Lenses `verification` (optional generic web research for current best practices),
  and `architecture` (with an import-graph summary as scout input).
- A scheduled package-health check outside the scout.

## Next

- **A run whose child dies leaves its waiter hanging until reconcile** (KosmoSenti report,
  2026-11-28). On 2026-11-28 an `omarchy-update` slowed fsyncs on the btrfs home volume.
  It locked the ledger 15:53–15:58 UTC, and pair arm 700d82f0 then died on an event
  write. The worker died on the same lock, and the run showed `running` for 90 minutes,
  until the next `worker start` reconciled it as orphaned. The worker now survives a
  busy ledger and commits without fsync (see CURRENT_STATE). Still open:
  (a) `wait` polls the run row only, so it does not notice that the run's process and the
  worker are gone; (b) `retry` refuses an orphaned run (`failed`, reason `orphaned`) even
  when it made no commits, so a pair arm lost this way cannot be re-armed.

- **The reviewer passes style and consistency slips** (Leeboard retro, 2026-11-25). Runs
  fcc907f1 and 2b29c691 passed with no findings. A coordinator spec/standards review then
  found four real issues that needed rework: a misaligned `--help` line, help text missing
  a new `--status lapsed` value, top-level fake-timer hooks leaking into unrelated tests,
  and output built by regex-patching serialized text. Ideas: give the review prompt the
  repository's conventions file (CONTRIBUTING.md, AGENTS.md), and have it check new lines
  against their neighbours and help/usage text against new flags and values. Add these
  diffs to the replay's held-out written-rule Standards cases (item 4) and measure there
  before changing the directive.
- **Intake's related-test detection over-adds** (same retro). For a change to
  `src/commands/read.ts`, `intake --yes` added 10–15 test files that only share imports of
  widely used modules (`src/entry.ts`, `test/helpers.ts`), so sibling tasks overlap and
  cannot run in parallel. Count only tests that import a declared source file directly;
  do not follow shared helpers.
- **A worker restart that cannot leave no worker.** `worker stop` only writes the stop
  file and returns; a `worker start` right after it fails "already running" while the old
  worker still lives, and that worker then exits on the stop file. On 2026-11-25 this left
  no worker for 46 minutes (11:37–12:23 UTC). Add `worker restart` (or `stop --wait`)
  that waits for the exit, then starts; note in the docs that a code change on main
  already restarts the worker.
- **Decide whether an unclosed final verdict fence should parse.** Run 472d345c's
  reviewer ended its review with a valid `json` block but no closing fence, so
  `parse_verdict` (`loop/review.py`) recorded `uncertain` (`parsed=false`) for a pass.
  `test_only_line_delimited_fences_complete_a_block` requires that strictness today.
  Accepting a final unclosed block when its JSON parses would avoid such false
  uncertains.
- **A flaky Leeboard timeout test.** `test_leeboard_supervisor_unavailable_warn_streak[timeout]`
  (`tests/cli/test_leeboard_hold_skip.py`) gives a fake `lb` a 0.2 s timeout against a 1 s
  sleep and expects one warning. In run a07bc610's 22-minute full suite it saw none; it
  passes alone. Find what makes the timeout path skip the warning under load.
1. **Decide how to roll out RPC implement sessions.** `pi_rpc_implement` is off by
   default. One dogfood run matched json mode in events, per-message usage, iteration cost
   and `agent_settled`; RPC mode emits no `session` header event, which nothing reads.
   Not yet seen: EOF mid-run, other providers. The flag is global, so either turn it on
   for all projects or add a per-project setting first.
2. **`steer-live-runs`.** `helmraiser steer <run> "<text>"` writes a ledger row; the
   run's own `work --once` child polls it and sends pi a `steer` command. Owner messages
   are advisory, never widen `paths:`, and count against the normal cost/time budget.
   Design decisions:
   - Use a ledger inbox rather than a socket.
   - Refuse runs whose session predates RPC.
   - Abort stays lease-based SIGTERM.
   - Under srt, pi exits 0 on SIGTERM, so the `state.db` abort rescue still applies.
3. **Exercise the widget actions on real runs.** Reject, plan approval, Drop and the
   report terminal have not been used from the Omarchy panel on real runs.
4. **Evaluation pack.** A fixed set of replayed tasks with hidden checks, rerun against
   one variant at a time and stored for comparison. The review replay exists
   (`helmraiser eval review|report`): it reruns past reviews under reviewer variants.
   The first replay moved the review profile to `openai-codex/gpt-6.2-astra:high`: it
   found 13 of 33 held-out misses, where every variant on the previous model found none.
   Measured 2026-11-28 on that model (`~/.helmraiser/eval/review-replay-1`: `run4-strong`,
   `heldout/out-strong`, `heldout/out-strong-noskills`, `heldout/out-strong-v7`). A blind
   audit judged 18 of 21 severe findings on "clean" cases real, so six clean cases were
   relabelled as eight misses; clean-case counts below are after that. Held-out: 18 misses
   (4 of them Leeboard-retro cases) and 4 clean, 3 reps; derivation: 17 misses, 6 clean,
   1 rep (`NOTES.md`, "Clean-case severe findings audit" and "Relabelled").
   - Held-out misses, on the 11 cases neither written from nor relabelled by a variant's
     own findings (3 reps): `v1-noskills` 11/33, `v5-procedure` 13/33,
     `v7-conventions` (v1 plus a project-conventions block) 15/33, `v8-reading` (v1 plus
     "read changed files, callers and tests in full") 10/33. Counting all non-retro
     misses: 12, 19, 18 and 16 of 42, but v5 leads there because two relabelled cases
     came from v5's own findings (941995f3, afd919b5). Derivation misses: 9, 10, 11, 10
     of 17. Held-out cost: $25, $36, $42, $60.
   - Standards cases stay the weak spot. Held-out non-retro (one written rule, one
     unwritten): 0 hits for every variant. The retro four, which `v7` was written from:
     `v7` 6/12 (miss-fcc907f1 and miss-2b29c691-timers 3/3 each, called minor), others 0.
     Derivation written-rule 6/7 for `v7`, 5/7 for the others.
   - Clean cases, held-out findings/severe: v1 22/0, v5 30/0, v7 48/2 (clean-e2923a21,
     tests derive the expected value from the production constant; not audited), v8 24/0.
   - Reading alone is not the lever: `v8` reads as much as v5 and v7 (median 16 files
     against 7) at the highest cost and catches the fewest. The differences between
     variants are two to four hits on 11 cases, too few to pick one. Keep the live
     directive as is; the next gain needs more held-out cases (Standards ones above all),
     not more variants.
   Remaining after that: the implement-stage pass, first `pi_rpc_implement` on against
   off (a candidate for the next live experiment), which informs item 1.
5. **Decide on redacting verify output sent to models.** Verify and evidence tails reach
   the implementer (`feedback_prompt` in `loop/prompts.py`, the iteration history) and the
   reviewer (`loop/review.py`) verbatim. Service-provided variables such as database URLs
   reach those models only through these tails. `loop/redact.py` already redacts the
   scout's intake retry feedback and could be applied there too.

## Waiting

- **In-run approval dialogs** (`ask-owner-dialogs`). Only if steered runs show
  approvals being missed. It would need a pi `tool_call` extension, which reverses
  today's `--no-extensions`.
- **Draft display in other consumers.** Belongs to those projects; nothing to do here.
