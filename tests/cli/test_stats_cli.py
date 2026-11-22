"""CLI stats opens only an existing, current ledger and never mutates it."""

import json
import re
import sqlite3

from typer.testing import CliRunner

from helmraiser.cli import app
from helmraiser.ledger.db import Ledger

runner = CliRunner()


def test_json_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr("helmraiser.loop.stats.load_config", lambda: {})
    db = tmp_path / "helmraiser.db"
    with Ledger(db):
        pass
    before = db.read_bytes()
    files = set(tmp_path.rglob("*"))
    with sqlite3.connect(db) as conn:
        rows = list(conn.iterdump())
    for args in [[], ["--since", "all"], ["--project", str(tmp_path / "missing")]]:
        result = runner.invoke(app, ["stats", "--data-dir", str(tmp_path), "--json", *args])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert set(data) == {
            "window",
            "runs",
            "quality",
            "failures",
            "cost",
            "latency",
            "scouts",
            "weekly",
        }
        assert data["runs"]["overall"]["landed"] == 0
        assert data["window"]["since"] == ("all" if "all" in args else "7d")
        if "--project" in args:
            assert data["window"]["project"] == str((tmp_path / "missing").resolve())
            assert all(r["scheduling_error"] for r in data["scouts"])
    with sqlite3.connect(db) as conn:
        assert rows == list(conn.iterdump())
    assert db.read_bytes() == before
    # SQLite read-only WAL readers may create shared-memory sidecars, not data.
    assert (
        set(tmp_path.rglob("*")) - {tmp_path / "helmraiser.db-wal", tmp_path / "helmraiser.db-shm"}
        == files
    )
    human = runner.invoke(app, ["stats", "--data-dir", str(tmp_path)])
    assert human.exit_code == 0
    assert "landed=0" in human.output and "$0.00" in human.output


def test_invalid_and_missing(tmp_path):
    bad = runner.invoke(app, ["stats", "--since", "2d", "--data-dir", str(tmp_path)])
    assert bad.exit_code == 2
    assert not (tmp_path / "helmraiser.db").exists()
    missing = runner.invoke(app, ["stats", "--data-dir", str(tmp_path), "--json"])
    assert missing.exit_code == 1
    assert "cannot read ledger" in missing.output
    assert not (tmp_path / "helmraiser.db").exists()


def test_old_ledger(tmp_path):
    with Ledger(tmp_path / "helmraiser.db"):
        pass
    with sqlite3.connect(tmp_path / "helmraiser.db") as conn:
        conn.execute("PRAGMA user_version=10")
    result = runner.invoke(app, ["stats", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    with sqlite3.connect(tmp_path / "helmraiser.db") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10


def test_help():
    result = runner.invoke(app, ["stats", "--help"], env={"COLUMNS": "120"})
    assert result.exit_code == 0
    text = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    for option in ["--since", "--project", "--json", "--data-dir", "90d", "all"]:
        assert option in text
