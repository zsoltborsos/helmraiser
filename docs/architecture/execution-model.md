# Execution model

Helmraiser has exactly one execution path: the governed loop in
`src/helmraiser/loop/runner.py` (`GovernedRun`). Each queued task gets one run, and a
run is one governed attempt with a canonical UUID4 run ID.

```text
helmraiser work
  ├─ reconcile non-terminal ledger runs whose worker lock is missing or dead
  └─ until empty or paused:
       first, a run handed back to the worker (status resuming), oldest first, one
       run per project by default: GovernedRun.resume(run).run() from the run's stage
       otherwise the next queued task (priority, then queue time):
       subscription stop_at ok? (else skip it)  isolation available? (else refuse)
       project capacity: nonterminal runs (awaiting_approval included) use slots;
       default 1; higher limits require disjoint paths and independent dependencies
       GovernedRun(task snapshot).run()
         claim       task lock (project state.db), before recording the run
         record      run rows in the central ledger and project state.db
         preflight   isolation check, pi auth for the implement model
         worktree    <worktree_root>/<repo-name>-<hash>/worktree-<id>-<run>, branch feature/<id>/<run>,
                     created from task `base`, project default `base`, or the checkout branch
         run dir     ~/.helmraiser/runs/<run>/ (task.md snapshot, sessions/, evidence/)
         setup       task / project.toml `setup:` once in the worktree; under process
                     isolation, its own srt settings for cache and registry (or approved host escape)
         [plan]      `plan: true`: plan session → implement immediately
                     approve-plan-first: plan session → awaiting_approval (plan)
         implement   pi session ─┐
                     proof of work (HEAD or status moved) → commit
                     verify commands → failures trimmed into the SAME session
                     ×max_iterations, then one escalation to `fallback`
                     (fresh session, failure history, half the iterations)
                     a different fallback reserves its share of run time; timeout partials commit
                     `SCOPE: <paths> — <reason>` → awaiting_approval (scope)
         evidence    evidence commands → $HELMRAISER_EVIDENCE_DIR, diff.patch
         review      different model, read-only tools, no context files,
                     at least `review_min_minutes` session time
                     → pass | concerns | uncertain (review.md)
         [feedback]  severe findings → current implementer fixes + verify → evidence + review
                     → if still severe, one fresh fallback round → evidence + review
         merge       auto + pass → configured delivery:
                       merge: finalize_local (ff-only merge, worktree removed)
                       pr: push branch, gh pr create (worktree removed, branch kept)
                     otherwise → awaiting_approval (branch + worktree kept)
         close       ledger, summary.md, status.json
```

With process isolation, reads are broadly allowed except built-in credential/data
location denials, configured `isolation.deny_read`, project `[sandbox] deny_read`
entries and optional other-project filtering. See the complete
[isolation reference](../reference.md#isolation) for boundaries and pi exceptions. Relative
project entries are denied beneath both the primary checkout and the run worktree;
absolute and `~` entries are denied as specified. The effective policy is recorded in
`srt-settings.json` in the run directory. Setup instead uses
`srt-settings-setup.json`: writes to the worktree, `/tmp` and `[setup].write`, network
to `[setup].allowed_domains`, and the same read denials. `[setup] sandbox = "host"` is
unconfined, is valid only with `[defaults] require_approval = true`, and is marked in
events, the outcome and summary. `isolation: none` is unchanged. `require_approval`
also makes `policy: auto` invalid at intake and work dispatch.

`helmraiser approve <run>` either runs `finalize_local` for merge delivery, pushes and
opens a pull request for PR delivery, or approves a pending plan and hands the run
back to the worker (`resuming`, stage `implement`); the next `work` loop resumes the
same run and worktree at the implement stage (`GovernedRun.resume`). On a pending scope
request it first adds the approved paths (all requested, or `--paths`) to the task
file's `paths:` and re-reads the task, then hands the run back the same way; the
resumed implement session is told which paths the owner approved. `helmraiser retry
<run>` hands a blocked or verify-failed run back at the verify stage, after re-reading
its task file: verify → evidence → review → merge decision, with no implement session.
With no commits and a clean worktree (or a `setup:` failure even with setup debris),
either retry form resumes at `start` in the same worktree: setup unless its latest attempt
passed with the same command, a missing plan only when needed, then normal implementation
and gates.
For unpublished work, merge and PR approvals automatically rebase the run branch
when its captured base has moved; a conflicting rebase is refused without landing.
This automatic approval-time rebase does not re-run verification. Use `rework --rebase`
to integrate a moved base and run the governed gates again. Existing published PRs
are never rebased by approval: `rework --rebase` instead merges the base into the
published history, then verifies and reviews before a non-force update of the same PR.
Parked runs should therefore be approved in dispatch order. `helmraiser reject <run>` removes the worktree and its creation metadata, keeps the
branch, and marks the run failed.

## Failure handling inside a run

| Condition | Result |
|-----------|--------|
| verify still failing after all iterations (and escalation) | `failed`, `verify_exhausted` |
| cost / time / iteration cap (checked before each spawn and at each streamed `message_end`) | `failed`, `budget:<kind>` |
| provider or protocol error in an implement session | retried as the next iteration |
| pi reports no readable usage on an assistant message (`contract`), in any stage | `failed`, `pi_contract: …` at once (no retry, iteration or escalation), and the queue pauses until `helmraiser queue resume` |
| provider quota / rate error in implement | hold that provider for 30 min and immediately escalate to an unheld fallback on another provider; without one, run `failed` (`quota: …`) |
| provider quota / rate error in an approval-gated plan or in review | hold that provider for 30 min and run `failed` (`quota: …`) |
| error or empty result in a `plan: true` plan | record `plan_failed` and continue to implementation without a plan; `BLOCKED: …` still fails the run |
| queued task whose implement or review provider is held | task skipped for this `work` invocation as `quota:<provider>`, with the hold expiry; tasks on other providers run |
| provider window usage plus the task's projected usage would reach `stop_at` | task skipped for this `work` invocation as `usage:<provider>:<window>`, with the reset time; the next task runs |
| first implement-model timeout, or its time share expires, with a different fallback | commit uncommitted partial work and escalate with reason `timeout` |
| any other implement session timeout | commit uncommitted partial work, then `failed`, `timeout` |
| agent answers `BLOCKED: …` | `failed`, `blocked: …` |
| agent answers `BLOCKED: …` in a run with `[[services]]`, leaving a new commit and a clean tree | record `blocked_to_verify` and run verify; if it fails, `failed`, `blocked: …` |
| agent answers `SCOPE: <path>[, <path>…] — <reason>` in an implement iteration with no out-of-scope changes | commit uncommitted work, record `scope_requested`, and park: `awaiting_approval` with a pending `scope` approval, worktree and branch kept; no verify |
| agent answers `SCOPE: …` and the iteration changed out-of-scope files | path scope check fails: `failed`, `verify_failed: task-paths` |
| agent answers `SCOPE: …` in an implement iteration with no valid new relative path, or the task declares no `paths` | `failed`, `blocked: …` |
| planner of a `plan: true` task that declares no `paths` starts its answer with `SCOPE: …` | record `scope_line_ignored`, drop the line and keep the plan after it; with no plan after it, `failed`, `plan: the planning session produced no plan` |
| no changes in an iteration | the agent is told so, and the loop continues |
| review session failure, malformed verdict, failed evidence, missing/unassessed evidence check | verdict forced to `uncertain`; each review gets at least `review_min_minutes` even if the run has less time left |
| severe review findings | fix round on the producing model; if still severe, escalate once to a different fallback model |
| review fix/fallback round cannot verify or exceeds budget | reset to the reviewed commit and decide policy using that review; run failure reason remains empty |
| evidence check not met on a `pass` verdict | verdict downgraded to `concerns` |
| local finalization refused (dirty / moved primary checkout) | `awaiting_approval`, `finalize_refused: …` |
| PR delivery push or `gh pr create` failed | `awaiting_approval`, `finalize_refused: …` |
| unexpected exception | `failed`, `error: <type>: …` (never left dangling) |
| worker died (SIGKILL, crash) | `failed`, `orphaned: worker <pid> is gone`, on the next `work` |

## State: ledger vs project state

| | Central ledger | Per-project state |
|---|---|---|
| Location | `$HELMRAISER_DATA_DIR/helmraiser.db` (default `~/.helmraiser`) | `<repo>/.helmraiser/state.db` |
| Owner | `ledger/db.py` (`Ledger`) | `storage/state.py` (`StateManager`) |
| Holds | tasks (with enqueue snapshot), runs, run events, iterations, evidence, approvals, queue pause, provider holds and cap switch | run status mirror, per-task locks, attempt-scoped process leases, abort requests |
| Statuses | `starting, running, verifying, reviewing, awaiting_approval, resuming, landed, failed, aborting, aborted` | `queued, starting, running, succeeded, failed, aborting, aborted` |
| Read by | every CLI command, `status.json` | `abort`, `ExecutionEngine` lease publishing |

The ledger is the source of truth. A fresh run first claims its task in project state;
if another worker holds the lock, `work` skips that task without creating a run row or
changing task status. Project state also allows a recorded run to be aborted from the
repository without the ledger. Worktree metadata (`<run>.json`) sits next to the
worktrees under `<worktree_root>/<repo-name>-<hash>/`. The suffix uses the resolved
repository's name and the first 12 hexadecimal SHA-256 characters of its resolved
path. Global `config.json`'s `worktree_root` defaults to `null`, selecting
`default_data_dir()/worktrees` (`$HELMRAISER_DATA_DIR/worktrees`, normally
`~/.helmraiser/worktrees`). A configured root expands environment variables and `~`;
relative roots resolve from the current working directory. This lets package stores
and worktrees share a filesystem (for example a separate data disk for pnpm).

An explicit project `worktree_dir` bypasses this root and suffix; its environment
variables expand and relative paths resolve from the repository. Per-project
`.helmraiser/state.db`, tasks and `finalize.lock` remain in the repository.

## Abort

`helmraiser abort <run-id> --project-dir <repo>` goes through `loop/abort.py`, which builds
an `AbortCoordinator` over the project's `state.db`. The coordinator marks the run
`aborting` and signals only the leased pi process group. `PosixProcessController` validates the session leader, PID/PGID and
Linux `/proc` start token before sending SIGTERM, then SIGKILL after a grace period.
Finally the coordinator marks project state `aborted`. If the task-lock worker is no
longer alive, `abort` also closes the ledger run and task as `aborted`; otherwise the
running loop sees the pi session end as `aborted`, or has its next spawn refused
(`run is aborting`), and closes the run as `aborted` in the ledger. A session cut short while the run is marked
`aborting` also counts as aborted (under `isolation: process`, srt exits 0 on SIGTERM).
The loop checks `state.db` once more before the merge decision, so an abort that lands
after the review returned closes the run as `aborted` without merging.

## Run directory (`$HELMRAISER_DATA_DIR/runs/<run-id>/`)

| Path | Content |
|------|---------|
| `task.md` | Task snapshot given to the agent |
| `plan.md` | Implementation plan for `plan: true` and `approve-plan-first` runs |
| `directives.md`, `directives-plan.md`, `directives-review.md` | Stage directives appended to pi's system prompt (`pi/directives.py`) |
| `sessions/` | pi `--session-dir` for every stage (`helmraiser attach` opens these) |
| `events.jsonl` | JSONL mirror of every ledger event |
| `evidence/` | `$HELMRAISER_EVIDENCE_DIR` for evidence commands |
| `logs/` | Full output of each failed verify step (`verify-<iteration>-<step#>-<name>.log`) and each evidence step that was not ok (`evidence-<step#>-<name>.log`) |
| `diff.patch` | `git diff base..HEAD` at evidence/review time |
| `review.md` | Final reviewer answer, verbatim |
| `review-1.md`, `review-2.md` | Earlier reviewer answers when feedback rounds run |
| `summary.md` | Morning-report fragment |

## pi invocation

`pi/adapter.py` runs `pi --mode json --session-id … --session-dir … --model …` with
`--append-system-prompt <directives>`, `--no-extensions --no-prompt-templates
--no-themes`, and (for review) a read-only `--tools` list and `--no-context-files`.
In this default JSON mode, up to `LARGE_PROMPT_BYTES` (60 KiB) the prompt is passed
inline; above that it is
written to `prompt-<stage>-<attempt>[-<n>].md` in the run directory and passed as
`@<path>`. When the session runs through an isolation wrapper (the CLI always sets
one, even for `isolation: none`) the prompt always goes through that file, even when
short, because the sandbox wraps the whole command as one shell-quoted
argument and quoting can push it past srt's argv limit; the file is written when
the session is spawned, so a dry run builds the argv without creating the run
directory. The engine uses a 16 MiB stream line limit, because `turn_end` records
exceed asyncio's 64 KiB default. Under process isolation, pi can write to the worktree, the run's
`sessions/` directory, `/tmp`, restricted parts of the primary checkout's Git common
directory (its branch ref directory, objects and worktree administration; not hooks or
config), and the constrained `~/.pi/agent` root. The rest of the run
directory is not writable from the sandbox.

With the global `pi_rpc_implement` flag enabled (default `false`), only implement-stage
sessions instead use `--mode rpc`, retaining the other flags and isolation wrapper.
The adapter sends one LF-terminated `prompt` command on stdin, with no positional
prompt or spill file. RPC emits the same session events without a session header;
the adapter consumes command responses rather than forwarding them. Stdin stays
open until `agent_settled` is forwarded (or a command is rejected), then closes for
orderly exit; the engine also closes it on every exit path. Usage accounting,
streaming budget checks, timeouts and lease-based SIGTERM aborts are unchanged.
Steering is not yet available.

`pi/events.py` classifies the session end (`quota`, `provider`,
`protocol`, `contract`, `timeout`, `aborted`) and accumulates usage and cost. A session that would
otherwise succeed fails as `contract` when any assistant message lacks readable usage
(numeric `input`, `output` and `cost.total`), so budget caps never count unreadable usage
as $0. Helmraiser runs whatever pi is installed, with no supported range; the pi version is
stored on every run.
