"""Tests for Helmraiser CLI commands."""

import re
from unittest.mock import patch

from typer.testing import CliRunner

from helmraiser.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "Helmraiser" in result.output


def test_abort_dispatches_exact_run_id(tmp_path):
    (tmp_path / ".git").mkdir()
    run_id = "2e6fd1a2-5f9f-4bc4-b09c-2aafe87e87b0"
    with patch("helmraiser.loop.abort.abort_run", return_value=True) as abort_run:
        result = runner.invoke(app, ["abort", run_id, "--project-dir", str(tmp_path)])

    assert result.exit_code == 0
    abort_run.assert_called_once_with(tmp_path, run_id)
    assert f"run_id={run_id} status=aborted" in result.output


def test_abort_rejects_unknown_run(tmp_path):
    (tmp_path / ".git").mkdir()
    run_id = "2e6fd1a2-5f9f-4bc4-b09c-2aafe87e87b0"
    with patch("helmraiser.loop.abort.abort_run", return_value=False):
        result = runner.invoke(app, ["abort", run_id, "--project-dir", str(tmp_path)])

    assert result.exit_code != 0
    assert "unknown run" in result.output


def test_abort_rejects_non_repository(tmp_path):
    result = runner.invoke(
        app,
        [
            "abort",
            "2e6fd1a2-5f9f-4bc4-b09c-2aafe87e87b0",
            "--project-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    assert "not a Git repository" in result.output


def test_abort_rejects_non_canonical_run_id(tmp_path):
    (tmp_path / ".git").mkdir()
    result = runner.invoke(app, ["abort", "not-a-uuid", "--project-dir", str(tmp_path)])
    assert result.exit_code != 0
    assert "UUID" in result.output


def test_land_lookup_json(tmp_path):
    import json

    result = runner.invoke(app, ["land", "missing", "--json", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "landed": False,
        "error": "unknown run: missing",
        "steps": [],
    }


def test_land_invalid_duration(tmp_path):
    import json

    for flag, value in [("--interval", "-1"), ("--timeout", "nan"), ("--timeout", "inf")]:
        result = runner.invoke(
            app, ["land", "missing", "--json", flag, value, "--data-dir", str(tmp_path)]
        )
        assert result.exit_code == 2
        assert "finite and nonnegative" in json.loads(result.stdout)["error"]


def test_approve_help_lists_decline():
    result = runner.invoke(app, ["approve", "--help"])
    assert result.exit_code == 0
    output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "--decline" in output


def test_land_help():
    result = runner.invoke(app, ["land", "--help"])
    assert result.exit_code == 0
    # CI forces colour; rich's escape codes split option names in the raw output.
    output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    for option in ("--interval", "--timeout", "--no-ci", "--json", "--data-dir"):
        assert option in output


def test_root_app_exposes_only_the_governed_loop_commands():
    import typer

    commands = set(typer.main.get_command(app).commands)  # type: ignore[attr-defined]
    assert commands == {
        "add",
        "intake",
        "queue",
        "caps",
        "worker",
        "work",
        "status",
        "wait",
        "report",
        "stats",
        "log",
        "show",
        "diff",
        "attach",
        "doctor",
        "preflight",
        "compare",
        "eval",
        "experiment",
        "scout",
        "drafts",
        "approve",
        "land",
        "clean",
        "reject",
        "pick",
        "pick-packet",
        "retry",
        "record-landed",
        "rework",
        "abort",
        "version",
    }
