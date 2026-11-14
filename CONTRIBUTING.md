# Contributing to Helmraiser

Thank you for your interest in contributing to Helmraiser!

## Development Setup

```bash
# Clone the repo
git clone https://github.com/zsoltborsos/helmraiser.git
cd helmraiser

# Install in editable mode with dev dependencies
pip install -e ".[dev]"
```

## Running Tests

```bash
# All tests
pytest

# With coverage report
pytest --cov=helmraiser --cov-report=term-missing

# A specific file
pytest --no-cov tests/test_cli.py -v
```

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

- Open a [GitHub Discussion](https://github.com/zsoltborsos/helmraiser/discussions) for general questions.
- Open an [Issue](https://github.com/zsoltborsos/helmraiser/issues) for bugs or feature requests.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
