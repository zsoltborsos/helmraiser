"""Verify runner (spec §4.4): every step runs, in order, in the worktree.

Under process isolation the runner passes a ``wrap``, and each step runs inside srt
with the command sandbox settings unless it says ``sandbox: host``. With a ``failure`` report a
failing step's ``tail`` is the failure excerpt (``loop/excerpt.py``), its full output
goes to ``log``, and ``hints`` name known sandbox or environment signatures in that
output (``loop/hints.py``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from helmraiser.isolation.environment import command_environment
from helmraiser.loop.excerpt import FailureReport, failure_excerpt
from helmraiser.loop.hints import sandbox_hints
from helmraiser.loop.shell import (
    SANDBOX_NONE,
    SANDBOX_SRT,
    Wrap,
    run_shell,
    step_sandbox,
    tail_lines,
)
from helmraiser.task.model import VerifyStep

__all__ = [
    "VerifyResult",
    "all_passed",
    "last_verify_results",
    "run_step",
    "run_verify",
    "tail_lines",
]


@dataclass(frozen=True)
class VerifyResult:
    name: str
    passed: bool
    exit_code: int | None
    seconds: float
    tail: str
    sandbox: str = SANDBOX_NONE  # srt, none or host (loop/shell.py)
    log: str | None = None  # full output of a failed step, when written
    hints: list[str] = field(default_factory=list)  # environment hints of a failed step

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VerifyResult:
        """From ``as_dict`` output as the ledger stores it (unknown keys ignored)."""
        names = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in names})


def run_step(
    step: VerifyStep,
    cwd: Path,
    run_id: str,
    tail: int,
    env: dict[str, str] | None = None,
    wrap: Wrap | None = None,
    failure: FailureReport | None = None,
    index: int = 1,
) -> VerifyResult:
    """``/bin/bash -o pipefail -c <run>`` in ``cwd`` with the step's timeout. ``index``
    (1-based) names the step's log file."""
    full_env = command_environment({"HELMRAISER_RUN_ID": run_id, **(env or {})})
    sandbox = step_sandbox(step.sandbox, wrap)
    res = run_shell(step.run, cwd, step.timeout, full_env, wrap if sandbox == SANDBOX_SRT else None)
    output = res.output
    exit_code = res.exit_code
    if res.timed_out:
        output += f"\n[helmraiser] verify step '{step.name}' timed out after {step.timeout}s"
    elif exit_code is None:
        exit_code = -1
    passed = exit_code == 0
    log = None
    hints: list[str] = []
    if passed or failure is None:
        excerpt = tail_lines(output, tail)
    else:
        excerpt = failure_excerpt(output, tail, failure.max_lines, failure.max_chars)
        log = failure.write_log(index, step.name, output)
        hints = sandbox_hints(output, failure.provided_ports, sandboxed=sandbox == SANDBOX_SRT)
    return VerifyResult(
        name=step.name,
        passed=passed,
        exit_code=exit_code,
        seconds=res.seconds,
        tail=excerpt,
        sandbox=sandbox,
        log=log,
        hints=hints,
    )


def run_verify(
    steps: tuple[VerifyStep, ...] | list[VerifyStep],
    cwd: Path,
    run_id: str,
    tail: int = 120,
    env: dict[str, str] | None = None,
    wrap: Wrap | None = None,
    failure: FailureReport | None = None,
) -> list[VerifyResult]:
    """Run all steps even after a failure so feedback is complete."""
    return [
        run_step(step, cwd, run_id, tail, env, wrap, failure, index)
        for index, step in enumerate(steps, start=1)
    ]


def all_passed(results: list[VerifyResult]) -> bool:
    return all(r.passed for r in results)


def last_verify_results(iterations: list[dict[str, Any]]) -> list[VerifyResult]:
    """The verify results of the last ledger iteration that has any (``Ledger.iterations``)."""
    for iteration in reversed(iterations):
        if iteration.get("verify_results"):
            return [VerifyResult.from_dict(r) for r in iteration["verify_results"]]
    return []
