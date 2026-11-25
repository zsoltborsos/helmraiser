"""Live experiment reports use picked pairs, not blind label assumptions."""

from itertools import count

import pytest

from helmraiser.ledger.db import Ledger
from helmraiser.loop.experiment_report import build_experiment_report
from tests.ledger.test_db import make_spec, make_v11_ledger, run_row

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


@pytest.fixture
def run_factory(ledger, tmp_path):
    ids = count()

    def create(pair, label, *, cost=1, minutes=1, iterations=(), verdict=None, link=True):
        run_id = f"run-{next(ids)}"
        ledger.create_run(
            {
                **run_row(run_id, pair["task_id"], str(tmp_path)),
                "cost_usd": cost,
                "started_at": 0,
                "ended_at": minutes * 60 if minutes is not None else None,
                "verdict": verdict,
            }
        )
        for n in iterations:
            ledger.start_iteration(run_id, n, f"session-{n}", "a/impl")
            ledger.finish_iteration(
                run_id,
                n,
                agent_ok=True,
                commit_sha=None,
                verify_passed=True,
                verify_results=[],
                cost_usd=999,
            )
        if link:
            with ledger._write() as conn:
                conn.execute(f"UPDATE pairs SET run_{label} = ? WHERE id = ?", (run_id, pair["id"]))
        return run_id

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
            "unblinded": False,
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


@pytest.mark.parametrize("sample_count", [3, 4])
def test_run_metrics(ledger, experiment, pair_factory, run_factory, sample_count):
    samples = [
        # cost, minutes, sparse iteration numbers, current review verdict
        ((1, 1, (), "approve"), (2, 2, (5,), "reject")),
        ((5, 3, (7,), "approve"), (4, 4, (1, 9), None)),
        ((9, 9, (2, 8, 20), None), (8, 8, (1, 2, 3, 4), "approve")),
        ((20, None, (1, 2, 3, 4), "reject"), (10, 10, (1, 2, 3, 4, 5, 6), "approve")),
    ]
    for i, (a, b) in enumerate(samples[:sample_count]):
        pair = pair_factory(
            experiment,
            arm_x="B" if i % 2 else "A",
            pick="tie" if i == 2 else "X",
            decision="tie" if i == 2 else ("failure" if i == 1 else "judged"),
        )
        for arm, (cost, minutes, iterations, verdict) in [("A", a), ("B", b)]:
            label = "x" if pair["arm_x"] == arm else "y"
            run_factory(
                pair, label, cost=cost, minutes=minutes, iterations=iterations, verdict=verdict
            )
        # Same task but not one of this pair's linked runs: excluded.
        run_factory(pair, "x", cost=10000, minutes=10000, iterations=range(10), link=False)
    unpicked = pair_factory(experiment, pick=None)
    for label in ("x", "y"):
        run_factory(unpicked, label, cost=10000, minutes=10000, iterations=range(10))
    unpaired_task = ledger.upsert_task(make_spec(ledger.db_path.parent, "unpaired"), "snapshot")
    run_factory({"task_id": unpaired_task}, "x", cost=10000, link=False)

    arms = build_experiment_report(ledger)["arms"]
    assert arms["A"]["median_cost_usd"] == (5 if sample_count == 3 else 7)
    assert arms["B"]["median_cost_usd"] == (4 if sample_count == 3 else 6)
    assert arms["A"]["median_minutes"] == 3
    assert arms["B"]["median_minutes"] == (4 if sample_count == 3 else 6)
    assert arms["A"]["median_iterations"] == (1 if sample_count == 3 else 2)
    assert arms["B"]["median_iterations"] == (2 if sample_count == 3 else 3)
    expected_a = {"approve": 2, "none": 1}
    expected_b = {"approve": sample_count - 2, "none": 1, "reject": 1}
    if sample_count == 4:
        expected_a["reject"] = 1
    assert arms["A"]["review_verdicts"] == expected_a
    assert arms["B"]["review_verdicts"] == expected_b
    assert list(arms["A"]["review_verdicts"]) == sorted(expected_a)


def test_missing_run_links(ledger, experiment, pair_factory, run_factory):
    pair = pair_factory(experiment)
    # Null run links are ignored without dropping the pair.
    report = build_experiment_report(ledger)
    assert report["pairs"] == {"total": 1, "picked": 1, "unpicked": 0}
    for arm in report["arms"].values():
        assert arm["median_cost_usd"] is None
        assert arm["median_minutes"] is None
        assert arm["median_iterations"] is None
        assert arm["review_verdicts"] == {}
    run_factory(pair, "y", minutes=None)
    arm = build_experiment_report(ledger)["arms"]["B"]
    assert arm["median_cost_usd"] == 1
    assert arm["median_minutes"] is None
    assert arm["median_iterations"] == 0
    assert arm["review_verdicts"] == {"none": 1}


def test_post_pick_rework(ledger, experiment, pair_factory, run_factory):
    def event(run_id, at, event_type="rework_requested"):
        event_id = ledger.append_event(run_id, "helmraiser", event_type, {"round": 99, "ts": 0})
        with ledger._write() as conn:
            conn.execute("UPDATE run_events SET ts = ? WHERE id = ?", (at, event_id))

    # B wins via Y, then A wins via Y by failure: both reverse the naive X=A mapping.
    for arm_x, decision, count_after in [("A", "judged", 2), ("B", "failure", 3)]:
        pair = pair_factory(experiment, arm_x=arm_x, pick="Y", decision=decision)
        loser = run_factory(pair, "x")
        winner = run_factory(pair, "y")
        picked_at = pair["picked_at"]
        event(winner, picked_at - 1)
        event(winner, picked_at)
        for offset in range(1, count_after + 1):
            event(winner, picked_at + offset)
        event(loser, picked_at + 10)
        event(winner, picked_at + 10, "review_feedback")
        event(winner, picked_at + 10, "rework_reopened")
    # Nor do tied or not-yet-picked runs have winners whose rework can count.
    for pick, decision in [("tie", "tie"), (None, None)]:
        pair = pair_factory(experiment, pick=pick, decision=decision)
        for label in ("x", "y"):
            run_id = run_factory(pair, label)
            event(run_id, (pair["picked_at"] or 0) + 100)
    arms = build_experiment_report(ledger)["arms"]
    assert arms["A"]["post_pick_rework_rounds"] == 3
    assert arms["B"]["post_pick_rework_rounds"] == 2


def test_empty_experiment(ledger, experiment):
    report = build_experiment_report(ledger)
    assert report["experiment"]["arms"] == ARMS
    assert report["pairs"] == {"total": 0, "picked": 0, "unpicked": 0}
    assert report["sign_test"] == {"n": 0, "wins": {"A": 0, "B": 0}, "p_value": None}
    assert report["reasons"] == []
    for arm in ("A", "B"):
        assert report["arms"][arm] == {
            "judged_wins": 0,
            "failure_wins": 0,
            "ties": 0,
            "median_cost_usd": None,
            "median_minutes": None,
            "median_iterations": None,
            "review_verdicts": {},
            "post_pick_rework_rounds": 0,
        }
