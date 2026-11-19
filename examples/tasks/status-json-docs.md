---
id: status-json-docs
title: Document the status.json contract and add a --json example to README
project: ~/projects/helmraiser
priority: normal
status: draft
policy: review
isolation: process
setup: python3 -m venv .venv && .venv/bin/pip install -q -e ".[dev]"
verify:
  - name: tests
    run: .venv/bin/python -m pytest -q --no-cov -p no:cacheprovider tests/cli
  - name: lint
    run: .venv/bin/ruff check src tests
evidence:
  - name: status-output
    run: .venv/bin/helmraiser status --json > "$HELMRAISER_EVIDENCE_DIR/status.json" && .venv/bin/helmraiser --help > "$HELMRAISER_EVIDENCE_DIR/help.txt"
    artifacts: ["status.json", "help.txt"]
    criteria: status.json parses and has keys queued_count, running, awaiting_count, spend; help.txt lists the commands.
tags: [docs]
---
## Historical example

This task's work is already implemented, now in `docs/reference.md`. It is retained
as an example of setup, verify and evidence syntax, not a task to queue unchanged.
Use `examples/tasks/slug-cli.md` and the README's disposable demo for a first run.

## Original goal
Add a "Status contract" section to README.md documenting the `status.json` fields
and a `helmraiser status --json` example.
