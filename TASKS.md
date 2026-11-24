# Helmraiser — Tasks

Only current, evidence-backed work. Delete an item in the commit that finishes it;
[CURRENT_STATE.md](CURRENT_STATE.md) and git record what landed. Runnable work goes
through the Helmraiser queue (`.helmraiser/tasks/`, not tracked); this file says why it exists
and what comes next.

## Release

1. **Finish release contact/settings checks, then go public.** Supply a real private
   enforcement contact for `CODE_OF_CONDUCT.md` (still a placeholder); confirm GitHub
   private vulnerability reporting works for the route in `SECURITY.md` (API returned
   404 while the repo was private). Review the alpha README on GitHub and try a clean-machine
   installation before release. The first-task acceptance has passed real sandbox
   preflight locally without models; a paid end-to-end example run remains opt-in.
   Set the GitHub social preview (Settings) to `docs/brand/helmraiser-social.png`;
   repository visibility stays the owner's decision.

## Scout follow-ups

- Lenses `verification` (optional generic web research for current best practices),
  and `architecture` (with an import-graph summary as scout input).
- A scheduled package-health check outside the scout.

## Next

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
   Remaining: the implement-stage pass, first `pi_rpc_implement` on against off, which
   informs item 1.

## Waiting

- **In-run approval dialogs** (`ask-owner-dialogs`). Only if steered runs show
  approvals being missed. It would need a pi `tool_call` extension, which reverses
  today's `--no-extensions`.
- **Draft display in other consumers.** Belongs to those projects; nothing to do here.
