# CLAUDE.md

Read [AGENTS.md](AGENTS.md) for the layout, gates and test discipline of this repository.

## Cost rule

Helmraiser is **pi-only**. Never use Claude (or any other paid per-token agent) inside
Helmraiser pipelines or automated runs: implement, fallback, review, intake and scout
profiles all go through `pi`. Claude is for interactive work on this repository only.

## Tips for this repository

- Use the venv tools: `export PATH="$PWD/.venv/bin:$PATH"`.
- Run the full suite with coverage, and pytest subsets with `--no-cov`.
- The suite never talks to a real model. Extend `tests/mocks/fake_pi.py` or
  `ScriptedPiAdapter` instead.
- Implementer, plan and review directives (what pi is told) are in
  `src/helmraiser/pi/directives.py`.
- Document only what the code does. Current state and known gaps go in `CURRENT_STATE.md`;
  planned work goes in `TASKS.md`. Update them in the commit that lands a slice.
