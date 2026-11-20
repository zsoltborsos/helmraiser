# Helmraiser

[![CI](https://github.com/zsoltborsos/helmraiser/actions/workflows/ci.yml/badge.svg)](https://github.com/zsoltborsos/helmraiser/actions/workflows/ci.yml)

**Run coding tasks unattended across your Git repositories, with checks and approval you control.**

Helmraiser uses [pi](https://github.com/earendil-works/pi) to implement tasks in separate
Git worktrees. You define the work and the commands that check its result. Helmraiser
runs implementation and verification, feeds failures back for bounded retries, and
collects evidence for a separate model review. Results wait for your approval unless
you explicitly enable automatic delivery. A central ledger tracks progress, usage and
outcomes across projects.

**Status: public-release preparation, alpha.** Used for personal unattended work;
interfaces and behavior may change. Linux only. Start with a non-sensitive test repository.

## How it works

```text
write a task → intake → queue → worktree → implement → verify → evidence → review
                                            ↑           │                   │
                                            └─ failures ┘                   ↓
                                                               approval → delivery
```

- **Checks you define.** Each task has an acceptance block: verify commands and
  optional evidence commands. Intake validates it and can help propose it with a model.
  A complete hand-written block needs no intake-model call.
- **Bounded implementation.** Failed checks go back to the implementer; a fallback
  model can take over. Cost, active time and iterations have configurable budgets.
- **Owner-controlled delivery.** `review` waits for approval; `approve-plan-first`
  also waits before implementation. Opt-in `auto` requires process isolation,
  evidence and a passing review. A reviewer cannot grant merge authority by itself.
- **Work across repositories.** A background worker serves the queue with concurrency
  limits, dependencies and per-project settings. Optional scouting proposes drafts;
  it does not queue them automatically.
- **Inspect and recover.** Read the diff, command output, review and usage; approve,
  reject, rework or retry eligible runs. The ledger retains the history.

Helmraiser owns the outer loop; pi owns each coding session. It is intended for
well-specified work with executable checks, not open-ended autonomous development.
Small interactive edits usually do not need it. Passing checks means the configured
checks passed—not that the implementation is correct or secure.

## Safety and limitations

Helmraiser executes model-generated code. **Process isolation is not a VM or a
blanket privacy boundary.** The default `process` mode uses sandbox-runtime (`srt`)
over bubblewrap for model sessions, setup, verify and evidence.

- Reads are broadly allowed except built-in and configured denials. Other projects
  remain readable unless you configure `isolation.projects_root` or read denials.
  Pi sessions can read pi's authentication file for provider access.
- `isolation: none` and explicitly approved `sandbox: host` steps run unconfined.
- Task content, code and evidence supplied to models may leave your machine. Use
  providers and repositories you trust, and keep production credentials out of reach.
- Review is a separate model's assessment, not a correctness guarantee. Choose a
  capable reviewer; Helmraiser enforces a different model, not a strength ranking.
- Approval can automatically rebase unpublished work onto a moved base **without
  re-running verification**. Use `rework --rebase` to run the governed gates again.
- `delivery: pr` records a run as `landed` when the PR is delivered, not merged.
  `helmraiser land` waits for CI and directly pushes the base branch; branch protection
  may refuse it. It is not a `gh pr merge` wrapper.

Read [isolation and permissions](docs/reference.md#isolation) before using sensitive
repositories, and [delivery policies](docs/reference.md#policies) before enabling `auto`.
Known operational gaps are in [CURRENT_STATE.md](CURRENT_STATE.md).

## Install

Requirements:

- Linux, Python 3.12+, Git, and Node.js/npm for the CLIs below.
- [pi](https://github.com/earendil-works/pi), configured and authenticated with your
  chosen providers. Helmraiser needs at least two distinct models for implementation
  and review; they may use the same provider.
- For default isolation: `srt`, `bwrap`, `socat` and `rg` on `PATH`. Install bubblewrap,
  socat and ripgrep with your distribution's package manager.
- Optional: authenticated `gh` for PR delivery; Docker for declared project services.
  Neither is needed for the first task below. Omarchy and Logbook are optional.

```bash
npm install -g @earendil-works/pi-coding-agent @anthropic-ai/sandbox-runtime

git clone https://github.com/zsoltborsos/helmraiser.git
cd helmraiser
python3 -m venv .venv
.venv/bin/pip install -e .
export PATH="$PWD/.venv/bin:$PATH"
export HELMRAISER_SOURCE="$PWD"  # used by the first-task commands below
helmraiser version
```

Keep the checkout: this is an editable source install. Add its `.venv/bin` to your
shell's PATH for later terminals, and ensure pi and srt are on the worker's PATH too.
For development, use [CONTRIBUTING.md](CONTRIBUTING.md).

### Configure models

Helmraiser ships **no model defaults**. After authenticating through pi, create or
merge these roles into `~/.helmraiser/config.json`, replacing the placeholders with
model IDs your pi installation supports:

```json
{
  "profiles": {
    "intake": {"model": "<provider>/<implement-model>"},
    "plan": {"model": "<provider>/<implement-model>"},
    "implement": {"model": "<provider>/<implement-model>"},
    "fallback": {"model": "<provider>/<implement-model>"},
    "review": {"model": "<provider>/<different-review-model>"},
    "scout": {"model": "<provider>/<implement-model>"},
    "judge": {"model": "<provider>/<different-review-model>"}
  }
}
```

`work` requires plan, implement, fallback and review, even without a plan stage.
The example also configures intake, implementation surveys (`scout`) and comparisons
(`judge`), because `doctor` checks all profiles. Using the same implement and fallback
model does not provide a different-model escalation. Task discovery additionally
needs `routing.scout.model` when you enable it.

Run `helmraiser doctor` and resolve readiness failures before continuing. Missing
profiles make doctor fail even if the command you intend to use does not need them. Helmraiser records the installed pi
version but has no supported version range; incompatible usage reporting fails a run
and pauses the queue.

See [model setup](docs/reference.md#models-setup), the optional
[setup skill](skills/helmraiser-setup/SKILL.md), and a
[personal example configuration](examples/config.json). Model-generated work consumes
provider usage; ledger dollars are pi's reported cost estimates, not necessarily your
subscription bill.

## Quick start

This small exercise demonstrates the workflow without installing project dependencies.
It creates a separate demo repository and asks pi to implement a slug CLI against
prewritten behavioral tests. Use a **new** `~/code/helmraiser-demo` directory and run
these commands in the shell used for installation. Git must have your author identity
configured so the initial commit succeeds.

### 1. Prepare the repository and task

```bash
mkdir -p ~/code/helmraiser-demo
cd ~/code/helmraiser-demo
git init -b main
printf '.helmraiser/\n__pycache__/\n' > .gitignore
cp "$HELMRAISER_SOURCE/examples/quickstart/test_slug.py" .
git add .gitignore test_slug.py
git commit -m "Add acceptance tests for the slug CLI"

mkdir -p .helmraiser/tasks
cp "$HELMRAISER_SOURCE/examples/tasks/slug-cli.md" .helmraiser/tasks/
```

Read [the task](examples/tasks/slug-cli.md) and [its tests](examples/quickstart/test_slug.py).
The task uses `policy: review`, `isolation: process`, local merge delivery and declared
paths limited to `slug.py`. Its `project` points to `~/code/helmraiser-demo`; change
that field if you chose another location. Task files need not be committed.

### 2. Check acceptance and queue

```bash
helmraiser intake .helmraiser/tasks/slug-cli.md --check
helmraiser intake .helmraiser/tasks/slug-cli.md --check --yes
helmraiser queue list
```

The check runs in a throwaway worktree of committed HEAD. Here it should report
**expected red** because `slug.py` does not exist yet. A sandbox error or unrelated
failure must be fixed before queueing. The task already has valid acceptance, so
these commands do not ask a model to propose it. Intake's notes about `test_slug.py`
being outside declared paths are intentional: it is a prewritten check, not a file
the implementer should edit. Intake registers the task too; a separate `add` is optional.

### 3. Run and inspect

The next command **spends model usage**. It runs one eligible task for this project:

```bash
helmraiser work --once --project ~/code/helmraiser-demo
helmraiser report
helmraiser show <run-id>
helmraiser log <run-id>
```

Replace `<run-id>` with the ID printed by `work` (a unique prefix also works).
A successful run waits for merge approval. Inspect its worktree diff, verify results
and review in the run directory under `~/.helmraiser/runs/<run-id>/` before deciding.

```bash
helmraiser approve <run-id>                         # deliver by local merge
# Or: helmraiser rework <run-id> --note "what to fix"
# Or: helmraiser reject <run-id> --note "why"
```

Rework hands the run back to the worker; run `work --once --project
~/code/helmraiser-demo` again to process it. After approval, run `python3 test_slug.py`
in the demo's primary checkout to check the delivered result yourself.

## Next steps

- **Real tasks:** [task files](docs/reference.md#task-files),
  [project setup](docs/reference.md#projecttoml) and
  [sandbox preflight](docs/reference.md#trying-commands-in-the-sandbox).
- **Unattended operation:** `helmraiser worker start`, then `helmraiser status` and
  `helmraiser report`. [Worker reference](docs/reference.md#running-the-queue).
  `worker stop` lets active runs finish; `queue pause` stops new starts, not active work.
- **Recovery:** [rework](docs/reference.md#reworking-a-run-awaiting-merge-approval),
  [retry](docs/reference.md#retrying-a-failed-run) and
  [troubleshooting](docs/reference.md#troubleshooting).
- **Agent-assisted operation:** install the [tasks skill](skills/helmraiser-tasks/SKILL.md).
  [Installation commands and workflow](docs/reference.md#quick-start).
- **Automation:** CLI commands support `--json`;
  [status contract](docs/reference.md#status-contract) and
  [monitoring commands](docs/reference.md#monitoring).

## Documentation and contributing

- [Documentation index](docs/README.md) and [complete CLI/configuration reference](docs/reference.md)
- [Execution model](docs/architecture/execution-model.md) and [glossary](GLOSSARY.md)
- [Current state](CURRENT_STATE.md), [planned work](TASKS.md) and [changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md), [security reporting](SECURITY.md) and [code of conduct](CODE_OF_CONDUCT.md)

Bug reports and questions are welcome in [GitHub Issues](https://github.com/zsoltborsos/helmraiser/issues).
Please report security vulnerabilities privately as described in the security policy.

## History

Development started on 25 February 2026 under the working name Squadron. This repository's
history begins at the rename to Helmraiser on 14 November 2026.

## License

[MIT](LICENSE).
