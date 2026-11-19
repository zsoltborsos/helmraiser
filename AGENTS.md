# AGENTS.md: working on the Helmraiser repository

This guide is for AI coding agents that change **this** repository. How to *use*
Helmraiser is in [README.md](README.md). The design is in
[docs/architecture/execution-model.md](docs/architecture/execution-model.md).
Name Helmraiser concepts in code, docs, directives and output with the words in
[GLOSSARY.md](GLOSSARY.md).

Current state and next moves: [CURRENT_STATE.md](CURRENT_STATE.md) and [TASKS.md](TASKS.md).
Update them in the commit that lands a slice.

## Layout (`src/helmraiser/`)

| Path | Role |
|------|------|
| `cli/app.py` | Root Typer app, plus `abort` and `version` |
| `cli/commands.py` | `add`, `intake`, `queue`, `work`, `approve`, `reject`, `retry`, `record-landed`, `rework`; shared merge approval |
| `cli/monitor.py` | `doctor`, `status`, `wait`, `report`, `log`, `show`, `attach` |
| `cli/clean_commands.py` | Manual `clean` command: dry-run, project filtering and JSON results |
| `cli/land_commands.py`, `loop/landing.py` | `land`: Logbook holds, shared PR approval, CI, non-force base push, best-effort local fast-forward and merge confirmation |
| `cli/compare_commands.py` | Blind comparison CLI and one-shot judge session |
| `cli/scout_commands.py`, `loop/scout.py` | `scout` (one read-only scout session that writes drafts) and `drafts list\|request` |
| `cli/caps_commands.py`, `loop/spend.py`, `loop/live_usage.py`, `loop/routing.py` | `caps on\|off\|reset`; ledger-derived subscription usage, live Omarchy / OpenCode Go readings, headroom and model routing |
| `cli/base_sync.py` | Host-side base refresh for PR tasks held by delivered-head ancestry |
| `cli/worker_commands.py`, `cli/supervisor.py` | `worker start\|stop\|status`; the long-running `work` loop (`--watch`, `--max-parallel`): one `work --once` child process per run, up to `max_parallel_runs` globally and `max_runs_per_project` per project (default 1; siblings require disjoint declared paths) |
| `config.py` | `~/.helmraiser/config.json`: defaults, subscription templates, load and save |
| `project_notes.py` | Owner-maintained project notes: loading and rendering |
| `logbook.py` | Read-only Logbook CLI hold checks and per-scheduling-pass caching |
| `code_snapshot.py` | The background worker's snapshots of committed code (`<data_dir>/code/<commit>`): export, `PYTHONPATH` and markers, the import check, re-exec, pruning |
| `cli/preflight_commands.py`, `loop/preflight.py` | `preflight`: commands in a throwaway worktree of the primary checkout's HEAD (a linked worktree is refused) under the verify sandbox settings, with sandbox hints |
| `task/` | Task file model, loader/writer, validity rules, `project.toml` defaults, the intake structure-check heuristic (`structural.py`) |
| `ledger/` | Central SQLite ledger (`~/.helmraiser/helmraiser.db`), run directories, `EventLogger` |
| `loop/runner.py` | `GovernedRun`, the outer loop |
| `loop/intake.py` | Intake prompt, proposal parsing, acceptance merge and validation |
| `loop/compare.py` | Run selection, blind prompt, verdict parsing and comparison rendering |
| `loop/{verify,evidence,review,merge,delivery,budget,prompts,status,report,shell,excerpt}.py` | Loop pieces |
| `loop/reconcile.py`, `loop/model_report.py` | Closing ledger runs whose worker died; `report --models` efficiency view |
| `loop/cleanup.py` | Failed/aborted worktree eligibility, safe removal and cleanup events; shared by `clean` and the worker sweep. Also removes a clean worktree when `record-landed` lands a run |
| `loop/abort.py` | `AbortCoordinator`, `AbortResult`, and `abort_run`: exact-run abort through the project's lease |
| `loop/hints.py` | Hints for known sandbox and environment failure signatures, for preflight and for failed verify / evidence steps |
| `loop/services.py` | Project services (`[[services]]`): `docker run` on the host, readiness, cleanup by the `helmraiser.run` label |
| `pi/` | `pi --mode json` adapter, event parsing, stage directives |
| `pipeline/worktree.py` | Run worktrees, `orphaned_metadata`, `finalize_local` (ff-only merge), and `fast_forward_base` (safe primary base checkout update) |
| `pipeline/run_identity.py` | Canonical UUID4 run IDs |
| `runner/` | `ExecutionEngine` (subprocess plus leases), process control |
| `storage/state.py` | Per-project `.helmraiser/state.db`: run rows, task locks, process leases, abort requests |
| `isolation/` | `none` / `process` (`srt` over bubblewrap) / `vm` (reserved); `environment.py`: what runs inherit from the worker; `relay.py`: the in-sandbox socat relays to project services |

The implementer, plan and review directives are the system-prompt text that pi receives.
They live in `pi/directives.py`. Loop prompts live in `loop/prompts.py`.

## Gates

```bash
export PATH="$PWD/.venv/bin:$PATH"
ruff check src tests && black --check src tests && mypy src/helmraiser
python -m pytest -q -p no:cacheprovider        # full suite, coverage floor 70 %
python -m pytest -q --no-cov tests/loop        # subsets need --no-cov
git diff --check
```

The pre-commit hooks run black, `ruff --fix` and mypy on commit. Stage explicit paths.

## Test discipline

- **Never call a real model from the suite.** Use `tests/mocks/fake_pi.py`, a scripted
  `pi` executable put on `PATH` by the `fake_pi` fixture, for subprocess-level tests.
  Use `tests/mocks/scripted_pi_adapter.py` (`ScriptedPiAdapter`) for loop-level tests.
- Real-model runs are opt-in manual dogfood: use the README's disposable repository
  and `examples/tasks/slug-cli.md`, with explicit consent to spend. The spec reserves
  `HELMRAISER_DOGFOOD=1` to gate such a test, but no test uses it yet. Keep it out of
  the default suite if you add one.
- Tests must not signal host processes. `tests/conftest.py` makes `os.killpg` raise, and
  abort tests use a fake `ProcessController`.
- `tests/conftest.py` points `HELMRAISER_CONFIG_DIR` at `~/.helmraiser-test` unless it is
  already set. It never uses the real `~/.helmraiser`. Give ledger tests a `tmp_path` data
  dir (`--data-dir` or the `data_dir` fixture in `tests/loop/conftest.py`).
- Tests that start the real `srt` (`needs_real_srt` in
  `tests/isolation/test_process_settings.py`) resolve it at import, before the guard stub
  in `tests/conftest.py` shadows it, and skip inside a sandbox (`SANDBOX_RUNTIME` set),
  where Helmraiser runs its own suite as a verify step.
- The suite never needs Docker. `tests/conftest.py` puts a guard `docker` on `PATH` that
  fails every call; tests of services use the `fake_docker` fixture
  (`tests/mocks/fake_docker.py`). `tests/isolation/test_services_real.py` starts real
  containers only with `HELMRAISER_DOCKER_TESTS=1`, when `docker info` works, `postgres:16`, `redis:7` and
  `qdrant/qdrant:latest` are already pulled and the real srt is usable; it removes
  them afterwards.
- For a bug fix, add a regression test that fails without the fix.

## Rules

- Helmraiser is pi-only. Never run paid agents (Claude) in automated pipelines.
- The ledger is the source of truth. Never drive abort from anything but the run's
  own validated lease.
- Helmraiser runs whatever pi is installed. It has no supported range and records the pi
  version per run (the ledger does this already). An assistant message without readable
  usage (budget caps rely on it) is a `contract` error: the run fails at once with
  `pi_contract: …` in any stage and the whole queue pauses until `helmraiser queue resume`.
  Other breaks in pi's JSON output are `protocol` errors. When pi updates, check its
  changelog against `pi/adapter.py` and `pi/events.py`.
