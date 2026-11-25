"""A pair after its fork: the state of its two arms, when it can be picked, the tie
rule, the automatic pick when one arm fails, and the experiment's end by decided pairs.

Output built here names labels (X, Y) only. ``reveal`` is the exception: it says which
arm each label was, and is for a pair that has a pick.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from helmraiser.ledger.db import TERMINAL_RUN_STATUSES, Ledger

LABELS = ("X", "Y")
# How a run ends without delivering anything.
FAILED_RUN_STATUSES = ("failed", "aborted")
# The judge recorded for a pick that Helmraiser made itself.
AUTO_JUDGE = "helmraiser"


class PickRefused(ValueError):
    """The pair cannot be picked as asked; the message says why."""


@dataclass(frozen=True)
class Arm:
    """One run of a pair, under its blind label."""

    label: str
    run: dict[str, Any]
    # The kind of the run's pending approval (plan, scope or merge), if it has one.
    pending: str | None
    # The run has reached its merge decision (a ``merge_decision`` event), whatever
    # became of it since.
    reached_merge: bool

    @property
    def run_id(self) -> str:
        return str(self.run["run_id"])

    @property
    def awaiting_merge(self) -> bool:
        """Deliverable: parked for its merge approval."""
        return self.run["status"] == "awaiting_approval" and self.pending == "merge"

    @property
    def failed(self) -> bool:
        return self.run["status"] in FAILED_RUN_STATUSES

    @property
    def settled(self) -> bool:
        """Decided enough to judge: awaiting its merge approval, or over."""
        return self.awaiting_merge or self.run["status"] in TERMINAL_RUN_STATUSES

    def describe(self) -> str:
        status = str(self.run["status"])
        if status == "awaiting_approval":
            return f"awaiting {self.pending or 'an'} approval"
        reason = self.run["failure_reason"]
        return f"{status} ({reason})" if reason and self.failed else status


def other(label: str) -> str:
    return "Y" if label == "X" else "X"


def label_of(pair: dict[str, Any], run_id: str) -> str:
    """The blind label of ``run_id``, one of the pair's two runs."""
    return "X" if pair["run_x"] == run_id else "Y"


def arms(ledger: Ledger, pair: dict[str, Any]) -> dict[str, Arm] | None:
    """The pair's two arms by label; None for a pair that lacks a run."""
    result: dict[str, Arm] = {}
    for label in LABELS:
        run_id = pair[f"run_{label.lower()}"]
        run = ledger.get_run(str(run_id)) if run_id else None
        if run is None:
            return None
        pending = ledger.pending_approval(str(run_id))
        result[label] = Arm(
            label,
            run,
            str(pending["kind"]) if pending else None,
            bool(ledger.events(str(run_id), types=("merge_decision",))),
        )
    return result


def task_pair(ledger: Ledger, ref: str) -> dict[str, Any] | None:
    """The pair of the one task that ``ref`` names, by its ``<project-key>:<id>`` or by
    a short id that only one task has. None when ``ref`` is a run ID, names no single
    task, or that task has no pair."""
    if ledger.get_run(ref) is not None:
        return None
    tasks = [
        task
        for task in ledger.list_tasks()
        if (task["task_id"] == ref if ":" in ref else task["id"] == ref)
    ]
    return ledger.pair_for_task(str(tasks[0]["task_id"])) if len(tasks) == 1 else None


def delivered_label(ledger: Ledger, states: dict[str, Arm], pick: str) -> str:
    """The label that ``pick`` (X, Y or tie) delivers.

    Both arms must be settled, and the arm to deliver must await its merge approval. A
    tie is a judgement between two deliverable arms: it delivers the cheaper one, then
    the one with fewer iterations, then X.
    """
    for arm in states.values():
        if not arm.settled:
            raise PickRefused(
                f"arm {arm.label} is {arm.describe()}; a pick needs both arms awaiting "
                "merge approval or over"
            )
    if pick == "tie":
        for arm in states.values():
            if not arm.awaiting_merge:
                raise PickRefused(
                    f"arm {arm.label} is {arm.describe()}; a tie needs both arms awaiting "
                    "merge approval"
                )
        return min(
            LABELS,
            key=lambda label: (
                float(states[label].run["cost_usd"] or 0.0),
                len(ledger.iterations(states[label].run_id)),
                label,
            ),
        )
    if not states[pick].awaiting_merge:
        raise PickRefused(
            f"arm {pick} cannot be delivered: it is {states[pick].describe()}, "
            "not awaiting merge approval"
        )
    return pick


def record_pick(
    ledger: Ledger,
    pair: dict[str, Any],
    pick: str,
    *,
    decided_by: str,
    reason: str | None,
    judge: str | None,
) -> bool:
    """Record the pair's pick, once: False when it already has one. Each of its runs
    gets a ``pair_pick`` event, and the experiment ends when this pick was its last
    (``end_if_decided``)."""
    if not ledger.set_pair_pick(
        int(pair["id"]), pick, decided_by=decided_by, reason=reason, judge=judge
    ):
        return False
    for label in LABELS:
        ledger.append_event(
            str(pair[f"run_{label.lower()}"]),
            "helmraiser",
            "pair_pick",
            {
                "label": label,
                "pick": pick,
                "decided_by": decided_by,
                "reason": reason,
                "judge": judge,
            },
        )
    end_if_decided(ledger, int(pair["experiment_id"]))
    return True


def end_if_decided(ledger: Ledger, experiment_id: int) -> bool:
    """End the experiment, with reason ``end_pairs``, when it is active and its decided
    pairs (judged, tie and by failure) have reached its ``end_pairs``."""
    experiment = ledger.experiment_by_id(experiment_id)
    if experiment is None or experiment["status"] != "active" or experiment["end_pairs"] is None:
        return False
    if ledger.experiment_pair_counts(experiment_id)["picked"] < int(experiment["end_pairs"]):
        return False
    return ledger.end_experiment(experiment_id, "end_pairs")


def auto_pick(ledger: Ledger, run_id: str) -> str | None:
    """Pick by failure, where the end of run ``run_id`` has just been recorded.

    When one arm of the run's unpicked pair ended as failed or aborted without ever
    reaching a merge approval, and the other awaits its merge approval, that other arm
    is picked (``decided_by`` ``failure``). It is not delivered: it stays awaiting its
    merge approval as an ordinary run. Returns the picked label, or None: an unpaired
    run, a pair with a pick, both arms failed, or an arm still under way.
    """
    pair = ledger.pair_for_run(run_id)
    if pair is None or pair["pick"] is not None:
        return None
    states = arms(ledger, pair)
    if states is None:
        return None
    for label in LABELS:
        failed = states[other(label)]
        if states[label].awaiting_merge and failed.failed and not failed.reached_merge:
            picked = record_pick(
                ledger,
                pair,
                label,
                decided_by="failure",
                reason=f"other arm failed: {failed.run['failure_reason']}",
                judge=AUTO_JUDGE,
            )
            return label if picked else None
    return None


def reveal(ledger: Ledger, pair: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Which arm each label was, with that arm's overrides. Only for a picked pair."""
    experiment = ledger.experiment_by_id(int(pair["experiment_id"]))
    definitions = experiment["arms"] if experiment is not None else {}
    return {
        label: {
            "arm": pair[f"arm_{label.lower()}"],
            "overrides": definitions.get(pair[f"arm_{label.lower()}"], {}),
        }
        for label in LABELS
    }


def awaiting_pick(ledger: Ledger, pair: dict[str, Any]) -> bool:
    """The pair has no pick, both arms are settled and one can be delivered."""
    if pair["pick"] is not None:
        return False
    states = arms(ledger, pair)
    return (
        states is not None
        and all(arm.settled for arm in states.values())
        and any(arm.awaiting_merge for arm in states.values())
    )


def run_pair(ledger: Ledger, run_id: str) -> dict[str, Any] | None:
    """A paired run's ``pair`` object in the status output: the label, never the arm.
    None for an unpaired run."""
    pair = ledger.pair_for_run(run_id)
    if pair is None:
        return None
    return {
        "task_id": pair["task_id"],
        "label": label_of(pair, run_id),
        "picked": pair["pick"] is not None,
        "awaiting_pick": awaiting_pick(ledger, pair),
    }


def awaiting_pick_count(ledger: Ledger) -> int:
    return sum(awaiting_pick(ledger, pair) for pair in ledger.unpicked_pairs())
