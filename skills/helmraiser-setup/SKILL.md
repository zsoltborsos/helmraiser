---
name: helmraiser-setup
description: Configure Helmraiser's models for its user. Use when setting up or reconfiguring Helmraiser's models, handling a models not configured error, or resolving doctor reporting a role not configured.
---

# Helmraiser setup

Default to configuration only: discovery, authentication checks and doctor spend no
model tokens. Offer a paid end-to-end check separately, after readiness.

## Discover

Locate the Helmraiser clone by resolving this skill directory's symlink; the clone
root is two directories above it. Read its README **Models setup** section and show
`examples/config.json` as the owner's tried-and-working reference, not universal defaults.
Resolve these paths against the clone, not the current working directory.

Read `$HELMRAISER_CONFIG_DIR/config.json` when that variable is set, otherwise
`~/.helmraiser/config.json`, if it exists. Inspect it before invoking Helmraiser commands:
Helmraiser can replace malformed JSON with defaults. Preserve malformed content and
agree a repair with the user first.

Run `pi --list-models` and check proposed IDs with
`pi auth check --model <provider/model>`. If models or authentication are insufficient,
explain what is missing; propose only models actually available to the user.

## Propose

Explain the roles and give one available model per setting, with a one-line reason:

| Setting | Purpose |
| --- | --- |
| `profiles.intake.model` | Propose missing acceptance blocks |
| `profiles.plan.model` | Plan implementation |
| `profiles.implement.model` | Perform the task |
| `profiles.fallback.model` | Take over when implement cannot finish |
| `profiles.review.model` | Independently review the result |
| `profiles.scout.model` | Survey code for implementation |
| `profiles.judge.model` | Evaluate blind comparisons |
| `routing.scout.model` | Discover draft tasks |

`work` (all forms) and `worker start` require plan, implement, fallback and review.
`intake` needs its model only to propose acceptance; valid acceptance needs no model.
`scout` needs `routing.scout.model`; `compare` needs judge. Models may serve multiple
roles, but review and implement must have different model IDs; changing only thinking
level does not count.

If the user has the matching subscriptions, offer
`"subscriptions": {"opencode-go": {}, "openai-codex": {}}` (only the names they use).
Named entries opt into Helmraiser's known templates. Preserve existing subscription
settings; the example's custom thresholds are not defaults to copy automatically.

## Apply agreed changes

Present the proposed config changes and wait for explicit agreement before writing.
Back up an existing file to a uniquely named sibling and report that location.
Merge agreed model/subscription changes into existing nested dictionaries, preserving
unrelated keys and existing role settings unless their changes were agreed. Create a
new file only if none exists; validate the resulting JSON. Keep credentials out of
proposals and reports.

## Check readiness

Run `helmraiser doctor` and resolve model configuration/authentication failures until
all profile rows are ready, obtaining agreement for further config changes as needed.
Also auth-check `routing.scout.model` explicitly: doctor checks profiles, not that
separate setting. Report unrelated doctor failures as separate blockers rather than
changing unrelated system settings. Finish with configured models, backup location
(or no prior file), and readiness results; say clearly if readiness remains blocked.

## Optional paid end-to-end check

Offer this check; execute only on the user's request and explicit consent to spend.
Explain first: one small run on their implement and review models consumes subscription
allowance or billed usage, with possible retries/fallback. Give an estimate and agree a
small budget using available pricing; disclose unknown pricing rather than inventing
an exact charge. Keep the user's configured models.

Use an agent-created scratch git repository with an initial commit and a tiny task,
such as creating a one-line text file. Read the clone's task-format documentation to
supply a complete deterministic acceptance block (verify the exact file content),
explicit `plan: false`, `policy: review`, `delivery: merge`, narrowly declared paths,
and the agreed small budget. Valid acceptance avoids an intake model call.

Register with `helmraiser add`, queue with `helmraiser intake --yes`, then execute with
`helmraiser work --once`. Use an isolated `--data-dir <scratch-data>` consistently on
all test commands and `--project <scratch-repo>` on work so production queue tasks
cannot be consumed. Confirm that only the scratch task is queued there before work.

Report actual task/run status, verification and review outcomes, and recorded cost
(distinguishing it from a subscription charge). Awaiting merge approval is not landed.
Once execution has stopped, clean up only the scratch repository and its owned run,
worktree and data artifacts using appropriate Helmraiser/git cleanup. Preserve active
worktrees and unrelated data; report any scratch cleanup left pending.
