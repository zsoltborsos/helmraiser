"""Leeboard diagnostics are informational, not a readiness gate."""

import os

import pytest

from helmraiser import leeboard
from helmraiser.ledger.db import Ledger
from tests.cli.test_doctor import invoke, ready_checks  # noqa: F401


@pytest.mark.parametrize("missing_binary", [False, True])
@pytest.mark.parametrize("missing_home", [False, True])
def test_leeboard_doctor_json(tmp_path, fake_lb, monkeypatch, missing_binary, missing_home):
    import json

    data = tmp_path / "data"
    Ledger(data / "helmraiser.db")
    home = leeboard.leeboard_home()
    if missing_home:
        home = tmp_path / "absent"
        monkeypatch.setenv("LEEBOARD_HOME", str(home))
    if missing_binary:
        monkeypatch.setattr(leeboard, "find_lb", lambda: None)
    result = invoke(data, "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["leeboard"] == {
        "lb": None if missing_binary else str(fake_lb.bin),
        "home": str(home),
        "exists": not missing_home,
    }
    assert home.exists() is not missing_home
    assert fake_lb.calls() == []


def test_leeboard_doctor_missing_binary_is_optional(no_lb, tmp_path, fake_lb, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    data = tmp_path / "data"
    Ledger(data / "helmraiser.db")
    result = invoke(data)
    assert result.exit_code == 0, result.output
    assert "leeboard" in result.output and "info" in result.output
    assert "not installed (optional)" in " ".join(result.output.split())
    assert fake_lb.calls() == []


def test_leeboard_doctor_human_and_failure(tmp_path, fake_lb, monkeypatch):
    from helmraiser.cli import monitor

    data = tmp_path / "data"
    Ledger(data / "helmraiser.db")
    result = invoke(data)
    assert result.exit_code == 0, result.output
    assert "leeboard" in result.output and "info" in result.output
    monkeypatch.setattr(leeboard, "find_lb", lambda: None)
    monkeypatch.setenv("LEEBOARD_HOME", str(tmp_path / "absent"))
    monkeypatch.setattr(monitor, "check_auth", lambda *args: "not authenticated")
    result = invoke(data, "--json")
    assert result.exit_code == 1, result.output
    assert fake_lb.calls() == []
    assert not os.path.exists(os.environ["LEEBOARD_HOME"])
