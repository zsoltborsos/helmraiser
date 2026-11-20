import json

from typer.testing import CliRunner

from helmraiser.cli.app import app


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
