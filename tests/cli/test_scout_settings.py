import json

import pytest
from typer.testing import CliRunner

from helmraiser.cli import scout_commands
from helmraiser.cli.app import app
from helmraiser.ledger.db import Ledger


@pytest.mark.parametrize("command", [["set", "docs", "--auto", "off"], ["reset", "docs"]])
@pytest.mark.parametrize("invalid_lens", ["docs", "tests"])
def test_failed_settings_leave_overrides_unchanged(tmp_path, monkeypatch, command, invalid_lens):
    config = {"routing": {"scout": {"lenses": {invalid_lens: {"cooldown_hours": 0}}}}}
    monkeypatch.setattr(scout_commands, "load_config", lambda: config)
    with Ledger(tmp_path / "helmraiser.db") as ledger:
        ledger.set_scout_lens_override("docs", auto=True)
        before = ledger.scout_lens_override("docs")
    result = CliRunner().invoke(
        app, ["drafts", "settings", *command, "--json", "--data-dir", str(tmp_path)]
    )
    assert result.exit_code == 2, result.output
    assert "cooldown_hours" in json.loads(result.output)["error"]
    with Ledger(tmp_path / "helmraiser.db") as ledger:
        assert ledger.scout_lens_override("docs") == before
        assert ledger.scout_lens_override("tests") == {}


def test_reset_validates_uncovered_config_before_deleting(tmp_path, monkeypatch):
    config = {"routing": {"scout": {"lenses": {"docs": {"auto": "invalid"}}}}}
    monkeypatch.setattr(scout_commands, "load_config", lambda: config)
    with Ledger(tmp_path / "helmraiser.db") as ledger:
        ledger.set_scout_lens_override("docs", auto=True, cooldown_hours=12)
        before = ledger.scout_lens_override("docs")
    runner = CliRunner()
    args = ["--json", "--data-dir", str(tmp_path)]
    assert runner.invoke(app, ["drafts", "settings", *args]).exit_code == 0
    result = runner.invoke(app, ["drafts", "settings", "reset", "docs", *args])
    assert result.exit_code == 2, result.output
    with Ledger(tmp_path / "helmraiser.db") as ledger:
        assert ledger.scout_lens_override("docs") == before


def test_set_can_override_invalid_config(tmp_path, monkeypatch):
    config = {"routing": {"scout": {"lenses": {"docs": {"auto": "invalid"}}}}}
    monkeypatch.setattr(scout_commands, "load_config", lambda: config)
    result = CliRunner().invoke(
        app,
        [
            "drafts",
            "settings",
            "set",
            "docs",
            "--auto",
            "on",
            "--json",
            "--data-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["lenses"]["docs"]["auto"] is True
    with Ledger(tmp_path / "helmraiser.db") as ledger:
        assert ledger.scout_lens_override("docs") == {"auto": True}


def test_settings(tmp_path):
    runner = CliRunner()
    args = ["--json", "--data-dir", str(tmp_path)]
    result = runner.invoke(app, ["drafts", "settings", *args])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["lenses"]["docs"]["auto"] is False
    result = runner.invoke(app, ["drafts", "settings", "set", "docs", "--auto", "on", *args])
    assert result.exit_code == 0, result.output
    row = json.loads(result.output)["lenses"]["docs"]
    assert row["auto"] is True
    assert row["source"]["auto"] == "ledger"
    result = runner.invoke(app, ["drafts", "settings", "reset", "docs", *args])
    assert result.exit_code == 0
    assert json.loads(result.output)["lenses"]["docs"]["source"]["auto"] == "default"
    for options in (
        ["bad"],
        ["docs"],
        ["docs", "--cooldown-hours", "0"],
        ["docs", "--auto", "bad"],
    ):
        result = runner.invoke(app, ["drafts", "settings", "set", *options, *args])
        assert result.exit_code == 2, result.output
