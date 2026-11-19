# Choosing models

Helmraiser runs models through your installed pi and ships **no model defaults**.
Configure IDs available to your authenticated pi providers, rather than relying on
pi's interactive default model. See [model setup](reference.md#models-setup) for
required roles and [profiles and budgets](reference.md#model-profiles-and-budgets)
for configuration and routing.

## Choose roles, then measure

- **Implement:** start with a model you trust for the task's language and complexity.
  A cheaper model can suit narrowly specified work with strong behavioral checks;
  cross-cutting changes may justify a more capable model from the start.
- **Fallback:** a different model can take over after failed iterations or severe
  review findings. A different fallback reserves a share of the run's time. Using
  the same model with more reasoning is not a different-model escalation.
- **Review:** must be a different model from the implementer. Choose one capable of
  checking the diff and evidence; Helmraiser does not enforce a model-strength ranking.
  Review is not a substitute for good acceptance commands or owner inspection.
- **Plan:** useful when the task needs an implementation plan. `plan: true` proceeds
  without plan approval; `approve-plan-first` waits for the owner.
- **Intake, scout and judge:** configure these when using model-proposed acceptance,
  task discovery or blind comparisons. The `scout` profile for implementation surveys
  is distinct from `routing.scout.model` for task discovery.

Profiles can specify a pi-supported thinking level. Built-in profile settings use
`high`, except fallback's `xhigh`; adapt these to your models' supported levels.
Task `models:` overrides project `[models]`, which overrides global `profiles`.
The [example config](../examples/config.json) records one personal setup, not
recommended defaults or a guarantee that those IDs are available to you.

Use `helmraiser report --since 7d --models` to compare iterations, escalation,
review findings, active time and cost. `--by setup` groups by the full model setup.
For a deliberate comparison, use [blind comparison](reference.md#comparing-runs)
and independent checks of the result. A passing model review alone is weak evidence.

## What the dollars mean

The ledger records pi's reported cost, which may be an equivalent API-price estimate
rather than money billed per run on a subscription. It measures Helmraiser's own
sessions; nested survey calls and other tools' usage are not fully represented there.
Per-run dollar budgets therefore bound reported usage, not your total provider bill.

Subscription metering is opt-in. Fresh configurations contain `subscriptions: {}`.
Known OpenCode Go and OpenAI Codex templates are applied only when explicitly named;
provider limits and pricing can change. Configure limits for your own subscription,
not another user's billing date or allowance.

Live readings are used where available; ledger estimates remain useful for attribution.
OpenCode Go can fall back to its usage API. Codex live usage currently depends on
Omarchy's usage record/collector, so that integration is not portable to every Linux
installation. Unknown headroom prevents headroom-based routing or scouting; it does
not make Omarchy a requirement for ordinary task execution.

Quota/rate errors create a provider hold rather than pausing the entire queue. An
implement session may escalate to an available fallback on another provider;
otherwise the run can fail. Tasks on unaffected providers can continue. A pi usage
contract failure, by contrast, pauses the whole queue. See
[spend and caps](reference.md#spend-and-caps) and
[troubleshooting](reference.md#troubleshooting).

## Historical observations—not model recommendations

These personal trials predate the public-release preparation. Model IDs, availability,
prices and the owner's configuration have since changed. They illustrate why to
measure behavior rather than treating low cost or a passing review as correctness.

### Small controlled task (2026-09-24)

Six models implemented a duration parser/formatter with a CLI, tests and a README
section. Every branch passed a separate hidden specification suite, but manual checks
of review findings found additional defects. Each took one iteration. One task is
not enough evidence to rank models on larger work.

| Model | Reported cost | Minutes | Defects confirmed on the branch |
|---|---|---|---|
| `glm-5.3-flash` | $0.049 | 2.1 | none beyond ambiguous `1 h`, accepted by all six |
| `qwen3.8-flash` | $0.056 | 2.8 | `1ſ` raises `KeyError` instead of `ValueError` |
| `deepseek-v4.1-flash` | $0.043 | 1.5 | `1 2h` parses as 12 h |
| `longcat-2.0` | $0.072 | 5.5 | `1 2h` (the reviewer passed it) |
| `mimo-v2.6-flash` | $0.036 | 3.6 | `1 2h`, and `1h30` accepted |
| `minimax-m3` | $0.105 | 3.4 | `1 2h`, and `1h30` accepted |

### Real-work trial (2026-09-30)

Nine tasks from one project, three per model, mixed tests with behavior changes.
The same fallback and reviewer were used across all nine. The tasks were not identical,
so the rows are observations, not a controlled ranking. Minutes exclude approval waits.

| Implement model | Task | Iterations | Escalated | Review | Reported cost | Minutes |
|---|---|---|---|---|---|---|
| `glm-5.3-flash` | list-endpoint tests | 1 | no | pass | $0.18 | 9.5 |
| `glm-5.3-flash` | identity upsert | 3 | yes | race, fixed by hand | $1.01 | 34.6 |
| `glm-5.3-flash` | token lifecycle tests | 5 | no | pass | $0.90 | 55.0 |
| `qwen3.8-flash` | inbound-queue route tests | 1 | no | pass | $0.12 | 8.3 |
| `qwen3.8-flash` | auth error mapping tests | 5 | yes | pass | $0.70 | 19.9 |
| `qwen3.8-flash` | retry policy | 1 | no | thin tests, tightened by hand | $0.15 | 7.1 |
| `deepseek-v4.1-flash` | migration failure | 1 | no | pass | $0.09 | 7.1 |
| `deepseek-v4.1-flash` | sync route and scanner tests | 1 | no | pass | $0.16 | 8.6 |
| `deepseek-v4.1-flash` | output escaping tests | 1 | no | pass | $0.06 | 2.6 |

A failed attempt caused by a missing database was excluded from this trial. The
rerun appears in the table. More recent host-load and run-duration observations are
in [Expected performance](performance.md), with their own sample limitations.
