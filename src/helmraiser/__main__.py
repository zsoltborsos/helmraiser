"""``python -m helmraiser`` runs the CLI; the worker spawns it detached."""

from helmraiser.cli.app import app

app()
