"""Ledger-backed live experiment outcomes and exact sign test."""

from __future__ import annotations

from math import comb
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
        for pair in pairs:
            winner = "tie" if pair["pick"] == "tie" else pair[f"arm_{pair['pick'].lower()}"]
            reasons.append(
                {
                    "task_id": pair["task_id"],
                    "winner": winner,
                    "decided_by": pair["decided_by"],
                    "reason": pair["reason"],
                    "judge": pair["judge"],
                }
            )
        return {
            "experiment": record,
            "pairs": {key: counts[key] for key in ("total", "picked", "unpicked")},
            "arms": arms,
            "sign_test": _sign_test(arms["A"]["judged_wins"], arms["B"]["judged_wins"]),
            "reasons": reasons,
        }
