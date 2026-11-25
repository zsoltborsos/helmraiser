"""Live experiment ledger counts and CLI lifecycle."""

import copy
import json
import re

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, experiment_commands
from helmraiser.ledger.db import Ledger
from tests.ledger.test_db import make_spec, make_v11_ledger, run_row


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


@pytest.fixture
def report_data(tmp_path, config):
    assert invoke(tmp_path, "start").exit_code == 0
    reasons = []
    with Ledger(tmp_path / "helmraiser.db") as ledger:
        record = ledger.active_experiment()
        for i, (pick, decision) in enumerate(
            [("X", "judged"), ("Y", "failure"), ("tie", "tie"), (None, None)]
        ):
            spec = make_spec(tmp_path, f"report-{i}")
            ledger.upsert_task(spec, "snapshot")
            pair_id = ledger.create_pair(record["id"], spec.task_id, "B", "A")
            for label, cost, minutes, iterations, verdict in [
                ("x", 2, 2, 1, "approve"),
                ("y", 6, 4, 3, "revise"),
            ]:
                run_id = f"run-{i}-{label}"
                ledger.create_run(
                    {
                        **run_row(run_id, spec.task_id, str(tmp_path)),
                        "started_at": 60,
                        "ended_at": 60 + minutes * 60,
                        "cost_usd": cost,
                        "verdict": verdict,
                    }
                )
                for n in range(1, iterations + 1):
                    ledger.start_iteration(run_id, n, f"session-{i}-{label}-{n}", "a/impl")
                    ledger.finish_iteration(
                        run_id,
                        n,
                        agent_ok=True,
                        commit_sha=None,
                        verify_passed=True,
                        verify_results=[],
                    )
                with ledger._write() as conn:
                    conn.execute(
                        f"UPDATE pairs SET run_{label} = ? WHERE id = ?", (run_id, pair_id)
                    )
            if pick is not None:
                reason = f"reason-{i}"
                judge = "test/judge" if decision == "judged" else None
                ledger.set_pair_pick(pair_id, pick, decided_by=decision, reason=reason, judge=judge)
                reasons.append(
                    {
                        "task_id": spec.task_id,
                        "winner": {"X": "B", "Y": "A", "tie": "tie"}[pick],
                        "decided_by": decision,
                        "reason": reason,
                        "judge": judge,
                    }
                )
                if pick != "tie":
                    pair = ledger.get_pair(record["id"], spec.task_id)
                    for _ in range(i + 1):
                        event_id = ledger.append_event(
                            f"run-{i}-{pick.lower()}",
                            "helmraiser",
                            "rework_requested",
                            {"round": 99},
                        )
                        with ledger._write() as conn:
                            conn.execute(
                                "UPDATE run_events SET ts = ? WHERE id = ?",
                                (pair["picked_at"] + 1, event_id),
                            )
    return {
        "experiment": record,
        "pairs": {"total": 4, "picked": 3, "unpicked": 1},
        "arms": {
            "A": {
                "judged_wins": 0,
                "failure_wins": 1,
                "ties": 1,
                "median_cost_usd": 6,
                "median_minutes": 4,
                "median_iterations": 3,
                "review_verdicts": {"revise": 3},
                "post_pick_rework_rounds": 2,
            },
            "B": {
                "judged_wins": 1,
                "failure_wins": 0,
                "ties": 1,
                "median_cost_usd": 2,
                "median_minutes": 2,
                "median_iterations": 1,
                "review_verdicts": {"approve": 3},
                "post_pick_rework_rounds": 1,
            },
        },
        "sign_test": {"n": 1, "wins": {"A": 0, "B": 1}, "p_value": 1.0},
        "reasons": reasons,
    }


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


@pytest.mark.parametrize("command", ["start", "stop", "status", "report"])
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
    for command in [[], ["start"], ["stop"], ["status"], ["report"]]:
        result = CliRunner().invoke(app, ["experiment", *command, "--help"], env={"COLUMNS": "120"})
        output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert result.exit_code == 0
        for word in (
            ["--json", "--data-dir"] if command else ["start", "stop", "status", "report"]
        ):
            assert word in output
        if command == ["stop"]:
            assert "--reason" in output
        if command == ["report"]:
            assert "[name]" in output
    root_help = CliRunner().invoke(app, ["--help"], env={"COLUMNS": "120"})
    assert root_help.exit_code == 0
    assert "experiment" in re.sub(r"\x1b\[[0-9;]*m", "", root_help.output)


@pytest.mark.parametrize("as_json", [False, True])
def test_report_output(tmp_path, config, report_data, as_json):
    # Reports use the frozen definition, never the current (now invalid) config.
    config["experiment"] = "invalid now"
    with Ledger(tmp_path / "helmraiser.db") as ledger:
        before = list(ledger._get_conn().iterdump())
    result = invoke(tmp_path, "report", *(["--json"] if as_json else []))
    assert result.exit_code == 0, result.output
    if as_json:
        assert json.loads(result.output) == report_data
    else:
        for field in [
            "experiment: trial (active)",
            'A: {"skills": "default"}',
            'B: {"skills": "none"}',
            "started_at (UTC):",
            "ended_at (UTC): unset",
            "end_reason: n/a",
            "end_pairs: 30; end_at (UTC): unset",
            "quota_brake: 0.85",
            "exclude_projects: []",
            "pairs: total: 4; picked: 3; unpicked: 1",
            "A: judged_wins: 0; failure_wins: 1; ties: 1",
            "B: judged_wins: 1; failure_wins: 0; ties: 1",
            "median_cost_usd: 6.0; median_minutes: 4.0; median_iterations: 3",
            "median_cost_usd: 2.0; median_minutes: 2.0; median_iterations: 1",
            'review_verdicts: {"revise": 3}',
            'review_verdicts: {"approve": 3}',
            "post_pick_rework_rounds: 2",
            "post_pick_rework_rounds: 1",
            "sign_test: n: 1; A: 0; B: 1; p_value: 1.0",
            "reasons:",
        ]:
            assert field in result.output
        for reason in report_data["reasons"]:
            assert (
                f"{reason['task_id']}: {reason['winner']} ({reason['decided_by']}); "
                f"reason: {reason['reason']}; judge: {reason['judge']}"
            ) in result.output
        assert "+00:00" in result.output
    with Ledger(tmp_path / "helmraiser.db") as ledger:
        assert list(ledger._get_conn().iterdump()) == before


@pytest.mark.parametrize("as_json", [False, True])
def test_report_selection(tmp_path, config, as_json):
    assert invoke(tmp_path, "start").exit_code == 0
    assert invoke(tmp_path, "stop", "--reason", "first complete").exit_code == 0
    config["experiment"]["name"] = "second"
    assert invoke(tmp_path, "start").exit_code == 0
    flags = ["--json"] if as_json else []
    named = invoke(tmp_path, "report", "trial", *flags)
    active = invoke(tmp_path, "report", *flags)
    assert named.exit_code == active.exit_code == 0
    if as_json:
        record = json.loads(named.output)["experiment"]
        assert record["name"] == "trial" and record["status"] == "ended"
        assert record["end_reason"] == "first complete"
        assert record["ended_at"] is not None
        assert json.loads(active.output)["experiment"]["name"] == "second"
    else:
        assert "experiment: trial (ended)" in named.output
        assert "end_reason: first complete" in named.output
        assert "ended_at (UTC): unset" not in named.output
        assert "experiment: second (active)" in active.output
    assert invoke(tmp_path, "stop").exit_code == 0
    latest = invoke(tmp_path, "report", *flags)
    assert latest.exit_code == 0
    if as_json:
        assert json.loads(latest.output)["experiment"]["name"] == "second"
    else:
        assert "experiment: second (ended)" in latest.output


@pytest.mark.parametrize("as_json", [False, True])
def test_report_refusals(tmp_path, config, as_json):
    flags = ["--json"] if as_json else []
    empty = invoke(tmp_path, "report", *flags)
    assert empty.exit_code == 1
    message = json.loads(empty.output)["error"] if as_json else empty.output
    assert "no experiment has been started" in message
    assert invoke(tmp_path, "start").exit_code == 0
    missing = invoke(tmp_path, "report", "missing", *flags)
    assert missing.exit_code == 1
    message = json.loads(missing.output)["error"] if as_json else missing.output
    assert "no such experiment: missing" in message


@pytest.mark.parametrize("as_json", [False, True])
def test_report_empty_experiment(tmp_path, config, as_json):
    assert invoke(tmp_path, "start").exit_code == 0
    result = invoke(tmp_path, "report", *(["--json"] if as_json else []))
    assert result.exit_code == 0, result.output
    if as_json:
        report = json.loads(result.output)
        assert report["pairs"] == {"total": 0, "picked": 0, "unpicked": 0}
        assert report["reasons"] == []
        assert report["sign_test"] == {"n": 0, "wins": {"A": 0, "B": 0}, "p_value": None}
        for stats in report["arms"].values():
            assert stats["median_cost_usd"] is None
            assert stats["median_minutes"] is None
            assert stats["median_iterations"] is None
    else:
        assert "total: 0; picked: 0; unpicked: 0" in result.output
        assert "median_cost_usd: n/a; median_minutes: n/a; median_iterations: n/a" in result.output
        assert "sign_test: n: 0; A: 0; B: 0; p_value: n/a" in result.output
