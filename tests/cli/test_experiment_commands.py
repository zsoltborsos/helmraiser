"""Live experiment ledger counts and CLI lifecycle."""

import copy
import json
import re

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, experiment_commands
from helmraiser.ledger.db import Ledger
from tests.ledger.test_db import make_spec, make_v11_ledger


def seed_pairs(ledger, experiment_id, tmp_path):
    for i, (pick, decision) in enumerate(
        [
            ("X", "judged"),
            ("Y", "judged"),
            ("X", "failure"),
            ("Y", "failure"),
            ("tie", "tie"),
            (None, None),
        ]
    ):
        spec = make_spec(tmp_path, f"t{i}")
        ledger.upsert_task(spec, "snapshot")
        pair = ledger.create_pair(experiment_id, spec.task_id, "B", "A")
        if pick:
            ledger.set_pair_pick(pair, pick, decided_by=decision)


COUNTS = {
    "total": 6,
    "picked": 5,
    "judged_wins": {"A": 1, "B": 1},
    "failure_wins": {"A": 1, "B": 1},
    "ties": 1,
    "unpicked": 1,
}
ZERO = {
    "total": 0,
    "picked": 0,
    "judged_wins": {"A": 0, "B": 0},
    "failure_wins": {"A": 0, "B": 0},
    "ties": 0,
    "unpicked": 0,
}


def test_pair_counts(tmp_path):
    with Ledger(tmp_path / "ledger.db") as ledger:
        first = ledger.start_experiment("first", {}, quota_brake=0.85)
        assert ledger.experiment_pair_counts(first) == ZERO
        seed_pairs(ledger, first, tmp_path)
        assert ledger.experiment_pair_counts(first) == COUNTS
        ledger.end_experiment(first, "done")
        second = ledger.start_experiment("second", {}, quota_brake=0.85)
        assert ledger.experiment_pair_counts(second) == ZERO
        assert ledger.experiment_pair_counts(first) == COUNTS
        # Asymmetric counts prove X is mapped to B, rather than hardcoded A.
        spec = make_spec(tmp_path, "extra")
        ledger.upsert_task(spec, "snapshot")
        pair = ledger.create_pair(second, spec.task_id, "B", "A")
        ledger.set_pair_pick(pair, "X", decided_by="judged")
        assert ledger.experiment_pair_counts(second)["judged_wins"] == {"A": 0, "B": 1}


def test_counts_pending_upgrade(tmp_path):
    path = tmp_path / "ledger.db"
    make_v11_ledger(path, run_status="running")
    with Ledger(path) as ledger:
        with pytest.raises(ValueError) as error:
            ledger.experiment_pair_counts(1)
        assert str(error.value) == ledger.upgrade_pending


@pytest.fixture
def config(monkeypatch):
    value = {
        "profiles": {"implement": "p/impl", "review": "p/review"},
        "experiment": {
            "name": "trial",
            "arms": {"A": {"skills": "default"}, "B": {"skills": "none"}},
        },
    }
    monkeypatch.setattr(experiment_commands, "load_config", lambda: value)
    return value


def invoke(tmp_path, *args):
    return CliRunner().invoke(app, ["experiment", *args, "--data-dir", str(tmp_path)])


@pytest.mark.parametrize("as_json", [False, True])
def test_lifecycle(tmp_path, config, as_json):
    flags = ["--json"] if as_json else []
    original = copy.deepcopy(config["experiment"]["arms"])
    start = invoke(tmp_path, "start", *flags)
    assert start.exit_code == 0, start.output
    if as_json:
        stored = json.loads(start.output)["experiment"]
        assert stored["arms"] == original
        assert stored["end_pairs"] == 30
        assert stored["quota_brake"] == 0.85
    else:
        assert "trial" in start.output and '"skills": "none"' in start.output
    with Ledger(tmp_path / "helmraiser.db") as ledger:
        seed_pairs(ledger, ledger.active_experiment()["id"], tmp_path)
    config["experiment"] = "invalid now"
    status = invoke(tmp_path, "status", *flags)
    assert status.exit_code == 0, status.output
    if as_json:
        payload = json.loads(status.output)
        assert payload["pairs"] == COUNTS
        assert payload["experiment"]["arms"] == original
    else:
        for field in [
            "A:",
            "B:",
            "started_at (UTC)",
            "end_pairs: 30",
            "end_at (UTC): unset",
            "quota_brake: 0.85",
            "exclude_projects: []",
            "total: 6",
            "picked: 5",
            "unpicked: 1",
            "judged_wins:",
            "failure_wins:",
            "ties: 1",
        ]:
            assert field in status.output
    stop = invoke(tmp_path, "stop", "--reason", "finished", *flags)
    assert stop.exit_code == 0 and "finished" in stop.output
    inactive = invoke(tmp_path, "status", *flags)
    assert inactive.exit_code == 0
    if as_json:
        assert json.loads(inactive.output) == {"experiment": None, "pairs": ZERO}
    else:
        assert inactive.output.strip() == "none"
    assert invoke(tmp_path, "stop", *flags).exit_code == 1


def test_start_refusals(tmp_path, config):
    for value in [None, {}, {"name": "trial", "arms": {}}]:
        config["experiment"] = value
        result = invoke(tmp_path, "start", "--json")
        assert result.exit_code == 1
        assert "error" in json.loads(result.output)
        with Ledger(tmp_path / "helmraiser.db") as ledger:
            assert ledger.active_experiment() is None


@pytest.mark.parametrize("as_json", [False, True])
def test_start_refuses_unrenderable_end_at_without_writing(tmp_path, config, as_json):
    config["experiment"]["end_at"] = "9999-12-31T23:59:59.999999Z"
    result = invoke(tmp_path, "start", *(["--json"] if as_json else []))
    assert result.exit_code == 1
    message = json.loads(result.output)["error"] if as_json else result.output
    assert "end_at" in message
    with Ledger(tmp_path / "helmraiser.db") as ledger:
        assert ledger.active_experiment() is None
        assert ledger.get_experiment("trial") is None


def test_unique_name_and_active(tmp_path, config):
    assert invoke(tmp_path, "start").exit_code == 0
    assert "already active" in invoke(tmp_path, "start").output
    assert invoke(tmp_path, "stop", "--json").exit_code == 0
    with Ledger(tmp_path / "helmraiser.db") as ledger:
        assert ledger.get_experiment("trial")["end_reason"] == "stopped"
    assert "already exists" in invoke(tmp_path, "start").output


@pytest.mark.parametrize("command", ["start", "stop", "status"])
@pytest.mark.parametrize("as_json", [False, True])
def test_pending_upgrade(tmp_path, config, command, as_json):
    path = tmp_path / "helmraiser.db"
    make_v11_ledger(path, run_status="running")
    with Ledger(path) as ledger:
        explanation = ledger.upgrade_pending
    result = invoke(tmp_path, command, *(["--json"] if as_json else []))
    assert result.exit_code == 1
    assert (json.loads(result.output)["error"] if as_json else result.output.strip()) == explanation


def test_help():
    for command in [[], ["start"], ["stop"], ["status"]]:
        result = CliRunner().invoke(app, ["experiment", *command, "--help"], env={"COLUMNS": "120"})
        output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert result.exit_code == 0
        for word in (["--json", "--data-dir"] if command else ["start", "stop", "status"]):
            assert word in output
        if command == ["stop"]:
            assert "--reason" in output
