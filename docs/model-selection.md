# Model selection for Helmraiser tasks

Rules for whoever fills the `models:` block of a task file: a person in an interactive
session, or the intake model proposing it. Helmraiser passes `--model provider/id:thinking`
to pi on every session, so nothing here comes from pi's default model or pi-crew's roles.

## What the budget really is

- `openai-codex/*` runs on the ChatGPT subscription (about $100 a month). It has a usage
  allowance per 5-hour and weekly window that OpenAI does not publish in numbers. It is
  the main pool: most implement, fallback and review sessions go here.
- `opencode-go/*` runs on the OpenCode Go subscription ($10 a month), the smaller pool.
  Each model has a published **monthly dollar limit** — $60, $30 or $15 (see
  `subscriptions.opencode-go.monthly_limit_usd` in the config), of which 20% is
  available per 5 hours and 50% per week
  ([OpenCode Go docs](https://opencode.ai/docs/go/)). Each model's spend counts against
  its own limit, and the dashboard shows one usage percentage per window for the whole
  subscription: `sum(cost / (monthly_limit_of_model * window_share))`. The weekly
  window resets Monday 00:00 UTC, the monthly one renews on the 17th at 17:00 UTC.
  Helmraiser mirrors exactly this in `subscriptions`, so the choice of model decides
  whether a task costs a sliver of the allowance or all of it.
- Claude is never used by Helmraiser (see `CLAUDE.md`).
- The `$` in the ledger and in `helmraiser report` is pi's estimate of the equivalent API
  price. It matched OpenCode's billing on four models, except DeepSeek peak hours where
  OpenCode charges double. Helmraiser sees only its own spend: nested scout calls and use
  outside Helmraiser are not in the ledger, so the estimate can read low; and its 5-hour
  window slides, while OpenCode's is a block from the first request, so it can also read
  high (24% against a real 11% on 2026-09-24). Helmraiser therefore uses the OpenCode Go usage API
  and Omarchy's Codex usage record as the effective subscription percentages whenever
  they are available, retaining the ledger estimate as a fallback and for per-model
  attribution.

Both pools pause the queue on quota or rate errors instead of failing tasks, so running
out is recoverable but stalls the queue.

## opencode-go allowance per model (2026-09-24)

Approximate requests per 5-hour window from the OpenCode Go docs. A Helmraiser run made
20 to 70 model requests on the last eight real runs (implement and review together).

| Allowance | Models | Requests per 5 h | Use |
|-----------|--------|------------------|-----|
| plenty | `mimo-v2.6-flash`, `mimo-v2.5`, `deepseek-v4-flash`, `longcat-2.0`, `deepseek-v4.1-flash`, `glm-5.3-flash`, `qwen3.8-flash`, `qwen3.7-plus`, `hy3`, `minimax-m3`, `minimax-m2.7`, `mimo-v2.6-pro`, `qwen3.6-plus` | 3,200 to 30,100 | candidates for routine work |
| moderate | `kimi-k2.7-code`, `kimi-k2.6`, `deepseek-v4-pro`, `glm-5.2`, `glm-5.1`, `gpt-5.6-luna` | 880 to 2,050 | occasional, one task at a time |
| scarce | `glm-5.3`, `kimi-k3`, `qwen3.8-max`, `qwen3.7-max`, `grok-4.6`, `grok-4.7` | 110 to 220 | do not use: two to four runs empty the window |

`deepseek-v4.1-flash` had a 4x allowance promotion until 2026-09-27; the scout default
moved to `glm-5.3-flash` when it ended.

## Comparison (2026-09-24)

Six "plenty" models ran the same small task on the lab repository: a duration parser and
formatter with a CLI, their own `unittest` tests and a README section. `policy: review`,
reviewer `gpt-6-sol:high`. Every run took one iteration and about 15 to 20 requests, and
every branch passed a hidden spec suite kept outside the repository. The reviewer's
findings, each checked by running the edge case on the branch, are what separated them.

| Model | Cost | Minutes | Defects confirmed on the branch |
|-------|------|---------|---------------------------------|
| `glm-5.3-flash` | $0.049 | 2.1 | none beyond the ambiguous `"1 h"` all six accept |
| `qwen3.8-flash` | $0.056 | 2.8 | `"1ſ"` raises `KeyError` instead of `ValueError` |
| `deepseek-v4.1-flash` | $0.043 | 1.5 | `"1 2h"` parses as 12 h |
| `longcat-2.0` | $0.072 | 5.5 | `"1 2h"` (the reviewer passed it) |
| `mimo-v2.6-flash` | $0.036 | 3.6 | `"1 2h"`, and `"1h30"` accepted |
| `minimax-m3` | $0.105 | 3.4 | `"1 2h"`, and `"1h30"` accepted |

One task is thin evidence. It says the six can all do a well-specified small task; it
does not rank them on larger work.

## Real-work trial (2026-09-30)

Nine tasks from one work project, three per cheap model, with a similar mix of test-only work and one
behaviour change each. All used the same fallback (`gpt-5.6-sol:medium`) and reviewer
(`gpt-6-sol:high`). Minutes are run time, not time spent awaiting approval.

| Model | Task | Iterations | Escalated | Review | Cost | Minutes |
|-------|------|------------|-----------|--------|------|---------|
| `glm-5.3-flash` | list-endpoint tests | 1 | no | pass | $0.18 | 9.5 |
| `glm-5.3-flash` | identity upsert (behaviour) | 3 | yes | concerns: a race, fixed by hand | $1.01 | 34.6 |
| `glm-5.3-flash` | token lifecycle tests | 5 | no | pass | $0.90 | 55.0 |
| `qwen3.8-flash` | inbound-queue route tests | 1 | no | pass | $0.12 | 8.3 |
| `qwen3.8-flash` | auth error mapping tests | 5 | yes | pass | $0.70 | 19.9 |
| `qwen3.8-flash` | retry policy (behaviour) | 1 | no | concerns: thin tests, tightened by hand | $0.15 | 7.1 |
| `deepseek-v4.1-flash` | migration failure (seam + tests) | 1 | no | pass | $0.09 | 7.1 |
| `deepseek-v4.1-flash` | sync route and scanner tests | 1 | no | pass | $0.16 | 8.6 |
| `deepseek-v4.1-flash` | output escaping tests | 1 | no | pass | $0.06 | 2.6 |

The OpenCode Go share of the weekly allowance per landed run, from
`report --models --since 24h`, was about 0.17% for `deepseek-v4.1-flash` and for
`qwen3.8-flash`, and 0.23% for `glm-5.3-flash`. That glm figure includes the day's other
glm runs. deepseek's 4x weight per dollar was offset by its low token use.

Two things distorted the first attempts:
- The quota projection kept qwen and deepseek tasks from starting until `projection_usd`
  (2f2d3f0).
- A required database was missing on the first token-lifecycle attempt. That run is
  excluded; the table shows the rerun.

Three tasks per model is still thin. deepseek did best here (all first pass, cheapest),
but its tasks were among the more mechanical ones. On 2026-10-01 the owner made
`deepseek-v4.1-flash` the default implementer anyway; scout stays on `glm-5.3-flash`.
Keep comparing with `report --models` as real runs accumulate.

On 2026-10-02 the owner moved every Sol role to `gpt-6.1-sol` (pi 0.99.1 made it the
default OpenAI Codex model) and asked that spare codex allowance be used before a reset.
Since a reviewer must differ from the implementer and never be weaker, review moved to
`gpt-6-astra:high`. On 2026-10-18 the fallback moved from `gpt-6-sol:high` to
`gpt-6.1-sol:xhigh`: the latest Sol with more reasoning, since most escalations follow
review findings:

| Profile | Model |
|---------|-------|
| implement | `openai-codex/gpt-6.1-sol:high` |
| plan | `openai-codex/gpt-6.1-sol:high` |
| fallback | `openai-codex/gpt-6.1-sol:xhigh` |
| review | `openai-codex/gpt-6-astra:high` |
| intake | `openai-codex/gpt-6-astra:high` |
| scout (routing) | `openai-codex/gpt-6.1-sol:high` |
| judge | `openai-codex/gpt-6-astra:high` |

## Tiers (2026-09-24, updated 2026-10-02)

| Tier | Models | Use for |
|------|--------|---------|
| frontier | `openai-codex/gpt-6-astra` | intake proposals and review (since 2026-10-02) |
| strong | `openai-codex/gpt-6.1-sol` | implement and plan (`:high`, since 2026-10-02); fallback (`:xhigh`, since 2026-10-18) |
| workhorse | `openai-codex/gpt-5.6-luna`, `gpt-5.6-terra` | not used by Helmraiser since 2026-10-02 |
| fast codex | `openai-codex/gpt-5.3-codex-spark`, `gpt-5.5` | not used by Helmraiser yet |
| cheap pool | `opencode-go/glm-5.3-flash`, `qwen3.8-flash`, `deepseek-v4.1-flash` | implement (default), scout |

Thinking levels: `medium` is the default everywhere except review. `high` is for the
reviewer and for tasks that say so. `low` for scouting and one-file mechanical edits.

## Rules

1. **Start from the profiles** in `~/.helmraiser/config.json`; omit `models:` when the
   defaults fit. Defaults since 2026-10-02 are in the table above; before that, implement
   `opencode-go/deepseek-v4.1-flash:medium` (2026-10-01; `glm-5.3-flash` from 2026-09-24),
   fallback `openai-codex/gpt-5.6-sol:medium`, review `openai-codex/gpt-6-sol:high`.
2. **Cheap first, escalate on quality.** A well-specified task goes to the cheap
   implementer. When the fallback is a different model, Helmraiser reserves part of the
   run time so the fallback always gets a turn: the implementer's sessions stop after
   `first_model_time_share` (0.6 by default), preserve partial work, and escalate.
   Helmraiser also escalates when verify iterations run out or when severe review findings
   remain after one review-feedback round on the implementer. The quality bar is the
   reviewer's and the approver's, not the implementer's.
3. **The spec carries the cheap model.** Name the files, functions and line numbers, the
   exact change, what not to touch, and which tests to run while working. A vague task on
   a cheap model wastes the run: on 2026-09-24 the same `qwen3.8-flash` timed out after
   91 turns on "close the leaked connections" and passed first time, in one iteration,
   when the three leaking classes and the change were named. Bound the work too: a cheap
   model that meets an open-ended side problem (on 2026-09-24, rich table truncation in a
   test) can spend the whole run on it. Say what not to change, ask for a commit after each
   step, and name the one test file to run while working.
4. **Start on codex Sol** (`implement: openai-codex/gpt-6.1-sol:medium`) when
   the task leaves design choices open, is a cross-cutting refactor, or changes
   concurrency, the run loop, git handling or the sandbox: there the cheap model's likely
   failure is code that passes verify with a subtle flaw.
5. **The reviewer is never weaker than the implementer** and never the same model. Do
   not lower it to save allowance.
6. **`gpt-6-astra`** is for intake and planning; as an implementer or fallback only when
   a task names it (a large refactor across a large codebase), and `:high` only then.
7. **Avoid the scarce opencode-go models** (`glm-5.3`, `kimi-k3`, `qwen*-max`, `grok`):
   two to four runs use up a 5-hour window.
8. **Budget.** Cheap implementer: `max_cost_usd: 3` (most of it is the codex review and
   any fallback). Codex implementer: `4` to `6`.
9. **Evidence of the choice.** When a task deviates from the defaults, one line under
   `## Goal` says which rule applied.
10. **Plan loose tasks for cheap implementers.** Set `plan: true` when a task is loosely
    specified and uses a cheap implementer. Leave it out when the task already names the
    files, small steps and tests; that task already carries the plan the model needs.

## Observed costs (ledger estimate, one iteration each)

To check a model choice against real runs, read `helmraiser report --since 7d
--models`: it reports landing, escalation, review findings, cost and
tokens per implement model straight from the ledger. The table below is a
one-iteration estimate.

| Shape | Example | Cost |
|-------|---------|------|
| test-only, small | parser tests | $0.32 |
| docs-only | docs-only change | $0.44 |
| test flake removal | test flake removal | $0.63 |
| feature, 3 to 5 files with tests | Helmraiser doctor orphans, worktree_dir | $0.53 to $1.10 |
| config split with test moves | config split with test moves | $0.80 |
