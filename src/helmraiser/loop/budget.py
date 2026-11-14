"""Budget guard (spec §4.2): cost, wall time and iteration caps."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from helmraiser.task.model import Budget

PAUSE_SECONDS = 30 * 60


class BudgetGuard:
    """Tracks one run's spend; ``exceeded()`` names the first cap that is over.

    ``cost_usd`` is the settled cost of finished sessions plus the streamed
    cost of the session in flight (``set_streaming``), so a runaway session is
    caught at the next ``message_end``.
    """

    def __init__(
        self,
        budget: Budget,
        started_at: float,
        clock: Callable[[], float] = time.time,
        *,
        prior_minutes: float = 0.0,
        extra_cost_usd: float = 0.0,
        extra_minutes: float = 0.0,
    ) -> None:
        self.budget = budget
        self.started_at = started_at
        self.clock = clock
        self.prior_minutes = prior_minutes
        self.max_cost_usd = budget.max_cost_usd + extra_cost_usd
        self.max_minutes = budget.max_minutes + extra_minutes
        self.settled_cost = 0.0
        self.streaming_cost = 0.0
        self.iterations = 0

    @property
    def cost_usd(self) -> float:
        return self.settled_cost + self.streaming_cost

    @property
    def minutes(self) -> float:
        return self.prior_minutes + max(0.0, self.clock() - self.started_at) / 60.0

    def set_streaming(self, cost_usd: float) -> None:
        self.streaming_cost = cost_usd

    def settle(self, cost_usd: float) -> None:
        self.settled_cost += cost_usd
        self.streaming_cost = 0.0

    def exceeded(self) -> str | None:
        """``cost`` | ``time`` | ``iterations`` | None."""
        if self.cost_usd > self.max_cost_usd:
            return "cost"
        if self.minutes > self.max_minutes:
            return "time"
        if self.iterations > self.budget.max_iterations:
            return "iterations"
        return None

    def remaining_seconds(self) -> int:
        return max(0, int((self.max_minutes - self.minutes) * 60))


def completed_active_minutes(
    events: list[dict[str, Any]], *, started_at: float, ended_at: float | None
) -> float:
    """Completed active segments only; approval and queue waits never count."""
    start: float | None = None
    total = 0.0
    completed = False
    for event in events:
        if event["type"] in ("run_start", "run_resume"):
            start = float(event["ts"])
        elif event["type"] == "run_end":
            payload = event["payload"]
            if "active_minutes" in payload:
                total += max(0.0, float(payload["active_minutes"]))
                completed = True
            elif start is not None:
                total += max(0.0, float(event["ts"]) - start) / 60
                completed = True
            start = None
    if not completed and ended_at is not None:
        return max(0.0, ended_at - started_at) / 60
    return total


def unconsumed_rework(
    reworks: list[dict[str, Any]], events: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The latest rework request that no ``run_resume`` has taken up yet."""
    if not reworks:
        return None
    consumed = {
        e["payload"].get("rework_round")
        for e in events
        if e["type"] == "run_resume" and e["payload"].get("from") == "rework"
    }
    latest = reworks[-1]
    return None if latest["round"] in consumed else latest


def rework_iteration_ceiling(request: dict[str, Any]) -> int:
    """The iteration count a rework's allowance ends at: those done before it, plus the allowance."""
    return int(request["iterations_before"]) + int(request["iteration_allowance"])


def iteration_limit(
    max_iterations: int, reworks: list[dict[str, Any]], events: list[dict[str, Any]]
) -> tuple[int, str]:
    """The iteration limit and its source (``rework_allowance`` or ``max_iterations``) that
    the runner enforces on a run's latest segment, or on the next one if the run is handed
    back. ``events`` hold the run's ``run_resume`` and ``handed_back`` events.

    A rework's ceiling applies from its hand-back to the end of the segment that took it up;
    every other segment, a budget retry's included (cumulative from the run's start), is
    bounded by ``max_iterations``."""
    pending = unconsumed_rework(reworks, events)
    if pending is not None:
        return rework_iteration_ceiling(pending), "rework_allowance"
    segments = [e for e in events if e["type"] in ("run_resume", "handed_back")]
    if segments and segments[-1]["type"] == "run_resume":
        payload = segments[-1]["payload"]
        if payload.get("from") == "rework":
            request = next((r for r in reworks if r["round"] == payload.get("rework_round")), None)
            if request is not None:
                return rework_iteration_ceiling(request), "rework_allowance"
    return max_iterations, "max_iterations"
