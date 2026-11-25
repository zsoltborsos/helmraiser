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
- **First experiment.** A `default` (hub skills) against B `none`, all projects, 30
  decided pairs or 14 days, brake 95%, starting as soon as the slices land.

Slices, in order (1, 3 and 4 interactive; 2, 5 and 6 may run as Helmraiser tasks):

1. Done (b72bc5c, 98b1779): ledger schema 12; the live ledger is upgraded.
2. Done (PR #37, #38): `experiment start|stop|status` with arm validation; an arm
   applied to a run (`GovernedRun(arm=RunArm)`, the `experiment_arm` event).
3. Done (163454b): a task forks into a pair when a worker claims it (one task row, two
   runs; Y from X's base commit), pair slot, sibling exemption, brake, exclusions,
   `experiment: false`. Known gaps are in CURRENT_STATE.md.
4. 4a done (4abdcb3): `pick <task> X|Y|tie`, auto-pick by failure, `approve`/`land`
   refused for an unpicked pair's merge, retry and abort on both arms, end conditions,
   the dependency fix, `pair` and `awaiting_pick_count` in status. 4b next: blind
   `wait`/`show`/`diff` by task, `pick-packet`, setups hidden in show/status/log/widget
   until the pick, `--reveal`, `rework` refused on an unpicked arm, and `pick` reporting
   a PR winner as PR opened rather than `landed`.
5. Done (PR #39): `experiment report [<name>] [--json]` with the sign test. Its docs go
   in slice 6.
6. Docs: reference, GLOSSARY (experiment, arm, control, pair, pick, decided pair, unpaired
   run), the helmraiser-tasks "Picking a pair" section with the judge prompt, the widget's
   "pair awaiting pick" label.

## Scout follow-ups

- Lenses `verification` (optional generic web research for current best practices),
  and `architecture` (with an import-graph summary as scout input).
- A scheduled package-health check outside the scout.

## Next

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
0. **Make the suite independent of the working directory.** Run from the primary
   checkout, 16 doctor and no-Leeboard tests fail: doctor reads the untracked
   `.helmraiser/project.toml` there (`delivery = "pr"`), adds the `gh` check, and the
   suite's fake `gh` reports not ready. From a clean worktree the suite passes. Point
   those tests at a temporary project instead of the current directory.
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
   After the codex weekly reset (2026-11-26 12:17 UTC), measure:
   - the stronger model without the review procedure (`v1-noskills`), on the held-out set;
   - the stronger model on the derivation set, as a regression check;
   - more held-out Standards cases with a written rule (only one so far), before any
     two-reviewer slice.
   Remaining after that: the implement-stage pass, first `pi_rpc_implement` on against
   off, which informs item 1.
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
