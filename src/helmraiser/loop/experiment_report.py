"""Ledger-backed live experiment outcomes and exact sign test."""

from __future__ import annotations

from collections import Counter
from math import comb
from statistics import median
from typing import Any

from helmraiser.ledger.db import Ledger


def _select_experiment(ledger: Ledger, name: str | None) -> dict[str, Any]:
    if name is not None:
        record = ledger.get_experiment(name)
        if record is None:
            raise ValueError(f"no such experiment: {name}")
        return record
    record = ledger.active_experiment()
    if record is None:
        latest = (
            ledger._get_conn()
            .execute("SELECT name FROM experiments ORDER BY started_at DESC, id DESC LIMIT 1")
            .fetchone()
        )
        if latest is not None:
            record = ledger.get_experiment(latest["name"])
    if record is None:
        raise ValueError("no experiment has been started")
    return record


def _sign_test(wins_a: int, wins_b: int) -> dict[str, Any]:
    n = wins_a + wins_b
    p_value = (
        min(1.0, 2 * sum(comb(n, k) for k in range(min(wins_a, wins_b) + 1)) / 2**n) if n else None
    )
    return {"n": n, "wins": {"A": wins_a, "B": wins_b}, "p_value": p_value}


def _run_metrics(ledger: Ledger, runs: list[dict[str, Any]]) -> dict[str, Any]:
    costs = [run["cost_usd"] for run in runs]
    minutes = [
        (run["ended_at"] - run["started_at"]) / 60 for run in runs if run["ended_at"] is not None
    ]
    iterations = [len(ledger.iterations(run["run_id"])) for run in runs]
    verdicts = Counter(run["verdict"] or "none" for run in runs)
    return {
        "median_cost_usd": median(costs) if costs else None,
        "median_minutes": median(minutes) if minutes else None,
        "median_iterations": median(iterations) if iterations else None,
        "review_verdicts": dict(sorted(verdicts.items())),
    }


def build_experiment_report(ledger: Ledger, name: str | None = None) -> dict[str, Any]:
    """Report the frozen experiment and its picked pairs in one read snapshot."""
    if not ledger.experiments_available:
        raise ValueError(ledger.upgrade_pending)
    with ledger.read_snapshot():
        record = _select_experiment(ledger, name)
        counts = ledger.experiment_pair_counts(record["id"])
        arms = {
            arm: {
                "judged_wins": counts["judged_wins"][arm],
                "failure_wins": counts["failure_wins"][arm],
                "ties": counts["ties"],
                "post_pick_rework_rounds": 0,
            }
            for arm in ("A", "B")
        }
        pairs = (
            ledger._get_conn()
            .execute(
                "SELECT * FROM pairs WHERE experiment_id = ? AND pick IS NOT NULL ORDER BY id",
                (record["id"],),
            )
            .fetchall()
        )
        reasons = []
        runs: dict[str, list[dict[str, Any]]] = {"A": [], "B": []}
        for pair in pairs:
            for label in ("x", "y"):
                run_id = pair[f"run_{label}"]
                run = ledger.get_run(run_id) if run_id is not None else None
                if run is not None:
                    runs[pair[f"arm_{label}"]].append(run)
            winner = "tie" if pair["pick"] == "tie" else pair[f"arm_{pair['pick'].lower()}"]
            if winner != "tie":
                winning_run = pair[f"run_{pair['pick'].lower()}"]
                if winning_run is not None:
                    arms[winner]["post_pick_rework_rounds"] += sum(
                        event["ts"] > pair["picked_at"]
                        for event in ledger.events(winning_run, types=("rework_requested",))
                    )
            reasons.append(
                {
                    "task_id": pair["task_id"],
                    "winner": winner,
                    "decided_by": pair["decided_by"],
                    "reason": pair["reason"],
                    "judge": pair["judge"],
                }
            )
        for arm in ("A", "B"):
            arms[arm].update(_run_metrics(ledger, runs[arm]))
        return {
            "experiment": record,
            "pairs": {key: counts[key] for key in ("total", "picked", "unpicked")},
            "arms": arms,
            "sign_test": _sign_test(arms["A"]["judged_wins"], arms["B"]["judged_wins"]),
            "reasons": reasons,
        }
