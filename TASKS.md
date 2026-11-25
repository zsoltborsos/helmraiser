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
6. **Per-stage worker skills** (owner approved 2026-11-25, started). Every pi session
   except the eval replay loads all discovered skills (`pi/adapter.py` never passes
   `--no-skills`). Design (Leeboard thread `request-say-whether-worker-pi-sessions-should-be-86ef`):
   - A stage whose folder `~/.local/share/helmraiser/skills/<stage>/` exists runs with
     `--no-skills`, one `--skill` per skill directory in it, then the project's own
     `.pi/skills` and `.agents/skills`. An empty folder means project skills only.
   - Stages are intake, scout, plan, implement (fallback too) and review. No folder
     means pi's discovery, as today.
   - Under isolation, allowRead for each folder and each resolved symlink target.
   - Open source ships no worker skills; the docs name a suggested set.

## Waiting

- **In-run approval dialogs** (`ask-owner-dialogs`). Only if steered runs show
  approvals being missed. It would need a pi `tool_call` extension, which reverses
  today's `--no-extensions`.
- **Draft display in other consumers.** Belongs to those projects; nothing to do here.
