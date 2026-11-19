---
id: slug-cli
title: Implement a Python slug CLI against the existing acceptance tests
project: ~/code/helmraiser-demo
status: draft
policy: review
delivery: merge
isolation: process
paths: [slug.py]
verify:
  - name: slug-behavior
    run: python3 test_slug.py
budget:
  max_cost_usd: 2.0
  max_minutes: 20
  max_iterations: 2
---
## Goal

Create `slug.py`, a Python 3 command-line program that accepts one text argument and
prints its slug followed by a newline. Lowercase ASCII letters, retain digits,
replace each run of characters outside `a-z` and `0-9` with a single hyphen, and
strip leading and trailing hyphens. Empty or punctuation-only input prints an
empty line. Use only the Python standard library.

The repository's existing `test_slug.py` exercises the CLI as a subprocess. Read
and run it; leave those acceptance tests unchanged. Only `slug.py` may change.

This deliberately small task demonstrates Helmraiser's workflow in a disposable
repository. Real tasks should include checks that cover their own requirements.
