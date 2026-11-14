.PHONY: install-dev test lint typecheck format all

install-dev:
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check src/ tests/
	black --check src/ tests/

typecheck:
	mypy src/helmraiser

format:
	black src/ tests/
	ruff check --fix src/ tests/

all: format lint typecheck test
