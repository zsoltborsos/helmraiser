---
id: status-json-docs
title: Document the status.json contract and add a --json example to README
project: ~/projects/helmraiser
priority: normal
status: draft
policy: review
isolation: none
setup: python3 -m venv .venv && .venv/bin/pip install -q -e ".[dev]"
verify:
  - name: tests
    run: .venv/bin/python -m pytest -q -p no:cacheprovider tests/cli
  - name: lint
    run: .venv/bin/ruff check src tests
evidence:
  - name: status-output
    run: .venv/bin/helmraiser status --json > "$HELMRAISER_EVIDENCE_DIR/status.json" && .venv/bin/helmraiser --help > "$HELMRAISER_EVIDENCE_DIR/help.txt"
    artifacts: ["status.json", "help.txt"]
    criteria: status.json parses and has keys queued_count, running, awaiting_count, spend; help.txt lists the commands.
tags: [v2, docs]
---
## Goal
Add a "Status contract" section to README.md documenting the `status.json` fields
and a `helmraiser status --json` example.
