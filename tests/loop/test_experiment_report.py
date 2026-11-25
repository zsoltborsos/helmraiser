"""Live experiment reports use picked pairs, not blind label assumptions."""

from itertools import count

import pytest

from helmraiser.ledger.db import Ledger
from helmraiser.loop.experiment_report import build_experiment_report
from tests.ledger.test_db import make_spec, make_v11_ledger

ARMS = {"A": {"skills": "default"}, "B": {"skills": "none"}}


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / "helmraiser.db") as value:
        yield value


@pytest.fixture
def experiment(ledger):
    return ledger.start_experiment("trial", ARMS, quota_brake=0.85)


@pytest.fixture
def pair_factory(ledger, tmp_path):
    ids = count()

    def create(experiment_id, *, arm_x="A", pick="X", decision="judged", reason=None, judge=None):
        spec = make_spec(tmp_path, f"task-{next(ids)}")
        ledger.upsert_task(spec, "snapshot")
        pair_id = ledger.create_pair(
            experiment_id, spec.task_id, arm_x, "B" if arm_x == "A" else "A"
        )
        if pick is not None:
            assert ledger.set_pair_pick(
                pair_id, pick, decided_by=decision, reason=reason, judge=judge
            )
        return ledger.get_pair(experiment_id, spec.task_id)

    return create


def test_outcomes_and_reasons(ledger, experiment, pair_factory):
    first = pair_factory(experiment, arm_x="B", pick="Y", reason="cleaner", judge="judge/one")
    second = pair_factory(experiment, arm_x="B", pick="X")
    third = pair_factory(experiment, arm_x="A", pick="X")
    failure = pair_factory(experiment, arm_x="B", pick="X", decision="failure")
    tie = pair_factory(experiment, pick="tie", decision="tie", reason="equal")
    pair_factory(experiment, pick=None)
    ledger.end_experiment(experiment, "done")
    other = ledger.start_experiment("other", {}, quota_brake=0.85)
    pair_factory(other)

    report = build_experiment_report(ledger, "trial")
    assert report["experiment"] == ledger.get_experiment("trial")
    assert report["pairs"] == {"total": 6, "picked": 5, "unpicked": 1}
    assert report["arms"]["A"]["judged_wins"] == 2
    assert report["arms"]["B"]["judged_wins"] == 1
    assert report["arms"]["A"]["failure_wins"] == 0
    assert report["arms"]["B"]["failure_wins"] == 1
    assert report["arms"]["A"]["ties"] == report["arms"]["B"]["ties"] == 1
    assert report["sign_test"] == {"n": 3, "wins": {"A": 2, "B": 1}, "p_value": 1.0}
    assert report["reasons"] == [
        {
            "task_id": pair["task_id"],
            "winner": winner,
            "decided_by": pair["decided_by"],
            "reason": pair["reason"],
            "judge": pair["judge"],
        }
        for pair, winner in [
            (first, "A"),
            (second, "B"),
            (third, "A"),
            (failure, "B"),
            (tie, "tie"),
        ]
    ]


@pytest.mark.parametrize(
    "wins_a,wins_b,p_value",
    [(8, 2, 0.109375), (2, 8, 0.109375), (10, 0, 0.001953125), (0, 0, None), (5, 5, 1.0)],
)
def test_exact_sign_test(ledger, experiment, pair_factory, wins_a, wins_b, p_value):
    for arm, wins in [("A", wins_a), ("B", wins_b)]:
        for i in range(wins):
            # Each arm wins through both labels.
            pair_factory(
                experiment,
                arm_x=arm if i % 2 else ("B" if arm == "A" else "A"),
                pick="X" if i % 2 else "Y",
            )
    pair_factory(experiment, pick="tie", decision="tie")
    pair_factory(experiment, decision="failure")
    assert build_experiment_report(ledger)["sign_test"] == {
        "n": wins_a + wins_b,
        "wins": {"A": wins_a, "B": wins_b},
        "p_value": p_value,
    }


def test_selection(ledger, experiment):
    ledger.end_experiment(experiment, "done")
    second = ledger.start_experiment("second", ARMS, quota_brake=0.85)
    # The active experiment wins even if a historical start time is later.
    with ledger._write() as conn:
        conn.execute("UPDATE experiments SET started_at = ? WHERE id = ?", (100, experiment))
        conn.execute("UPDATE experiments SET started_at = ? WHERE id = ?", (50, second))
    assert build_experiment_report(ledger)["experiment"]["name"] == "second"
    assert build_experiment_report(ledger, "trial")["experiment"]["name"] == "trial"
    with pytest.raises(ValueError, match="no such experiment: missing"):
        build_experiment_report(ledger, "missing")
    ledger.end_experiment(second, "done too")
    assert build_experiment_report(ledger)["experiment"]["name"] == "trial"
    with ledger._write() as conn:
        conn.execute("UPDATE experiments SET started_at = ?", (100,))
    assert build_experiment_report(ledger)["experiment"]["name"] == "second"


def test_empty_ledger(ledger):
    with pytest.raises(ValueError, match="no experiment has been started"):
        build_experiment_report(ledger)
    with pytest.raises(ValueError, match="no such experiment: missing"):
        build_experiment_report(ledger, "missing")


def test_pending_upgrade(tmp_path):
    path = tmp_path / "old.db"
    make_v11_ledger(path, run_status="running")
    with Ledger(path) as ledger:
        with pytest.raises(ValueError) as error:
            build_experiment_report(ledger)
        assert str(error.value) == ledger.upgrade_pending
