"""Live experiment ledger counts and CLI lifecycle."""

import pytest

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
