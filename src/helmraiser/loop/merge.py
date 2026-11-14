"""Merge decision per policy (spec §4.5.3)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from helmraiser.task.model import POLICIES


@dataclass(frozen=True)
class MergeDecision:
    action: str  # keep | finalize | finalize_refused
    status: str  # awaiting_approval | landed
    failure_reason: str | None = None


def decide(policy: str | None, verdict: str | None, finalize: Callable[[], None]) -> MergeDecision:
    """``auto`` + ``pass`` finalizes (ff-merge, worktree removed); everything else
    keeps branch and worktree and waits for the owner. A refused finalization is
    reported, never retried here (``helmraiser approve`` re-runs it)."""
    if policy not in POLICIES:
        raise ValueError(f"unknown policy: {policy}")
    if policy == "auto" and verdict == "pass":
        try:
            finalize()
        except Exception as exc:  # dirty primary checkout, HEAD moved, …
            return MergeDecision(
                "finalize_refused", "awaiting_approval", f"finalize_refused: {exc}"
            )
        return MergeDecision("finalize", "landed")
    return MergeDecision("keep", "awaiting_approval")
