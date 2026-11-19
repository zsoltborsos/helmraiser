# Helmraiser Documentation

## Start here

- [README](../README.md): what Helmraiser does, safety boundaries, installation and
  a complete first task in a disposable repository.
- [CLI and configuration reference](reference.md): task files, model setup, policies,
  isolation, services, monitoring, workers, recovery and troubleshooting.
- [Contributing](../CONTRIBUTING.md): development setup, tests and pull requests.

## Design and operational guidance

- [Execution model](architecture/execution-model.md): the governed loop, failure
  handling, ledger vs project state, abort, and the run directory layout.
- [Model selection](model-selection.md): choosing roles, what reported cost means,
  and explicitly historical observations—not shipped defaults.
- [Expected performance](performance.md): host load by concurrent runs and per-run
  minutes, iterations and cost from one measured day on a laptop.

- [Sandboxed verify design](architecture/sandboxed-verify.md): historical design
  discussion; the reference describes current behavior.
- [Glossary](../GLOSSARY.md), [current state](../CURRENT_STATE.md) and
  [planned work](../TASKS.md).
