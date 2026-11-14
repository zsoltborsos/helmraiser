"""Evidence collection (spec §4.5.1).

Each step runs in the worktree with ``HELMRAISER_EVIDENCE_DIR`` pointing at the
run's ``evidence/`` directory; its ``artifacts`` globs are expanded there. A
step is ok iff it exited 0 and every glob matched at least one file. Under process
isolation a step runs inside srt with the command sandbox settings (see ``loop/verify.py``).
A step that is not ok reports its output like a failed verify step (``FailureReport``),
with hints for known environment signatures in it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
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
from helmraiser.task.model import EvidenceStep


@dataclass(frozen=True)
class EvidenceResult:
    name: str
    ok: bool
    exit_code: int | None
    seconds: float
    artifacts: list[str]  # matched files, relative to the evidence dir
    missing: list[str]  # globs that matched nothing
    tail: str
    criteria: str
    sandbox: str = SANDBOX_NONE  # srt, none or host (loop/shell.py)
    log: str | None = None  # full output of a step that was not ok, when written
    hints: list[str] = field(default_factory=list)  # environment hints of a step not ok

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def expand_artifacts(patterns: tuple[str, ...], evidence_dir: Path) -> tuple[list[str], list[str]]:
    matched: list[str] = []
    missing: list[str] = []
    for pattern in patterns:
        files = sorted(p for p in evidence_dir.glob(pattern) if p.is_file())
        if not files:
            missing.append(pattern)
        matched += [str(p.relative_to(evidence_dir)) for p in files]
    return matched, missing


def run_evidence_step(
    step: EvidenceStep,
    cwd: Path,
    run_id: str,
    evidence_dir: Path,
    tail: int,
    env: dict[str, str] | None = None,
    wrap: Wrap | None = None,
    failure: FailureReport | None = None,
    index: int = 1,
) -> EvidenceResult:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    full_env = command_environment(
        {"HELMRAISER_RUN_ID": run_id, "HELMRAISER_EVIDENCE_DIR": str(evidence_dir), **(env or {})}
    )
    sandbox = step_sandbox(step.sandbox, wrap)
    res = run_shell(step.run, cwd, step.timeout, full_env, wrap if sandbox == SANDBOX_SRT else None)
    output = res.output
    if res.timed_out:
        output += f"\n[helmraiser] evidence step '{step.name}' timed out after {step.timeout}s"
    matched, missing = expand_artifacts(step.artifacts, evidence_dir)
    ok = res.exit_code == 0 and not missing
    log = None
    hints: list[str] = []
    if ok or failure is None:
        excerpt = tail_lines(output, tail)
    else:
        excerpt = failure_excerpt(output, tail, failure.max_lines, failure.max_chars)
        log = failure.write_log(index, step.name, output)
        hints = sandbox_hints(output, failure.provided_ports, sandboxed=sandbox == SANDBOX_SRT)
    return EvidenceResult(
        name=step.name,
        ok=ok,
        exit_code=res.exit_code,
        seconds=res.seconds,
        artifacts=matched,
        missing=missing,
        tail=excerpt,
        criteria=step.criteria,
        sandbox=sandbox,
        log=log,
        hints=hints,
    )


def run_evidence(
    steps: tuple[EvidenceStep, ...] | list[EvidenceStep],
    cwd: Path,
    run_id: str,
    evidence_dir: Path,
    tail: int = 120,
    env: dict[str, str] | None = None,
    wrap: Wrap | None = None,
    failure: FailureReport | None = None,
) -> list[EvidenceResult]:
    return [
        run_evidence_step(step, cwd, run_id, evidence_dir, tail, env, wrap, failure, index)
        for index, step in enumerate(steps, start=1)
    ]


def all_ok(results: list[EvidenceResult]) -> bool:
    return all(r.ok for r in results)
