# Contributing to Helmraiser

Thank you for your interest in contributing to Helmraiser!

## Development Setup

```bash
# Clone the repo
git clone https://github.com/zsoltborsos/helmraiser.git
cd helmraiser

# Python 3.12+; isolate dependencies from the system Python
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
export PATH="$PWD/.venv/bin:$PATH"
```

## Running Tests

```bash
# All tests
pytest

# With coverage report
pytest --cov=helmraiser --cov-report=term-missing

# A specific file
python -m pytest --no-cov tests/cli/test_show.py -v
```

The suite uses fake pi adapters and does not call real models or require provider
credentials. Docker calls are guarded; real sandbox/container tests are optional.
Test subsets need `--no-cov` because the coverage floor applies to the full suite.
See [AGENTS.md](AGENTS.md) for test isolation rules and the complete gates.

## Code Style

Helmraiser uses **black** for formatting, **ruff** for linting, and **mypy** for type checking.

```bash
# Format code
black src/ tests/

# Lint
ruff check src/ tests/

# Type check
mypy src/helmraiser
```

Or use `make` (see `Makefile`):

```bash
make format   # auto-format with black
make lint     # ruff + black --check
make typecheck
make test
```

## Pre-commit Hooks

To auto-run formatting/linting on every commit:

```bash
pip install pre-commit
pre-commit install
```

## Branch Naming

- `feature/<short-description>` — new features
- `fix/<short-description>` — bug fixes
- `docs/<short-description>` — documentation only
- `chore/<short-description>` — maintenance, dependency bumps

## Pull Request Conventions

1. Keep PRs focused — one concern per PR.
2. Add or update tests for any code you change.
3. Ensure all CI checks pass before requesting review.
4. Fill out the PR template (`.github/PULL_REQUEST_TEMPLATE.md`).

## Where to Ask Questions

- Open an [Issue](https://github.com/zsoltborsos/helmraiser/issues) for questions, bugs or feature requests.
- Report vulnerabilities privately using [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
