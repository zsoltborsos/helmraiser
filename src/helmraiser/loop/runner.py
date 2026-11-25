"""GovernedRun — one governed attempt at a task (spec §4.1–4.7).

Sequence: preflight → lock → worktree → materialise → setup → [plan → wait for
approval] → services (``[[services]]``) → implement (iterate: pi session →
proof-of-work → commit → verify → feedback into the same session; escalate once to
the fallback model with a fresh session and half the iterations) → evidence →
review → merge per policy → close (removes the services on every path).

``approve-plan-first`` stops after the plan stage with a pending ``plan``
approval; ``GovernedRun.resume(run_id, …)`` picks the run up again in the same
worktree and continues from the implement stage with the plan in the prompt. A run
handed back by ``helmraiser retry`` resumes at the verify stage instead: verify →
evidence → review → merge, with no implement session. ``helmraiser retry --implement``
hands a failed run back at the implement stage: the implement loop starts again on the
run's branch with the last verify failure as its first prompt (``retry_implement_prompt``),
a fresh iteration and time budget and the full escalation ladder. Without commits,
retry can instead resume at ``start``: setup unless its latest attempt passed with the
same command, a missing plan if needed, then the ordinary implement flow. The run identity and
history are kept, with the same retry-implement allowances and cumulative cost cap.
"""

from __future__ import annotations

import asyncio
import copy
import math
import os
import re
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, NoReturn

from helmraiser.code_snapshot import without_snapshot
from helmraiser.isolation import Isolation, ProcessIsolation, for_level
from helmraiser.isolation.environment import command_environment
from helmraiser.isolation.placeholders import PlaceholderCleanupError, remove_placeholders
from helmraiser.isolation.process import tracked_denial_warnings, tracked_denials
from helmraiser.isolation.setup import prepare_setup
from helmraiser.ledger.db import LIVE_RUN_STATUSES, NEW_RUN_STAGE, Ledger
from helmraiser.ledger.event_logger import EventLogger
from helmraiser.ledger.rundir import RunDir
from helmraiser.leeboard import BriefResult, fetch_brief
from helmraiser.loop.budget import (
    PAUSE_SECONDS,
    BudgetGuard,
    completed_active_minutes,
    rework_iteration_ceiling,
    unconsumed_rework,
)
from helmraiser.loop.delivery import open_pull_request, pr_body
from helmraiser.loop.evidence import EvidenceResult, run_evidence
from helmraiser.loop.excerpt import DEFAULT_EXCERPT_CHARS, DEFAULT_EXCERPT_LINES, FailureReport
from helmraiser.loop.experiments import RunArm, RunPair, apply_arm
from helmraiser.loop.hints import SETUP_READONLY_HINT, setup_readonly_line
from helmraiser.loop.merge import MergeDecision, decide
from helmraiser.loop.pairs import auto_pick
from helmraiser.loop.prompts import (
    NO_CHANGES_PROMPT,
    escalation_prompt,
    feedback_prompt,
    initial_prompt,
    plan_prompt,
    retry_implement_prompt,
    retry_prompt,
    retry_restart_prompt,
    review_feedback_prompt,
    rework_prompt,
    scope_approved_prompt,
    scope_declined_prompt,
)
from helmraiser.loop.report import (
    APPROVAL_SUBJECTS,
    render_path_scope_failures,
    render_reworks,
    render_scope_request,
)
from helmraiser.loop.review import ScopeDecision, Verdict, build_review_prompt, parse_verdict
from helmraiser.loop.routing import format_dispatch_decision
from helmraiser.loop.services import RunningService, ServiceError, start_service, stop_services
from helmraiser.loop.shell import SANDBOX_HOST, UNCONFINED_NOTE, Wrap, run_shell, tail_lines
from helmraiser.loop.spend import provider
from helmraiser.loop.status import write_status
from helmraiser.loop.verify import VerifyResult, all_passed, last_verify_results, run_verify
from helmraiser.pi.adapter import check_auth, session_id_for
from helmraiser.pi.directives import (
    render_implement_directives,
    render_plan_directives,
    render_review_directives,
)
from helmraiser.pi.events import is_assistant_message_end, ledger_payload, usage_of
from helmraiser.pi.protocol import PiAdapter, PiEvent, PiRequest, PiSessionResult, Usage
from helmraiser.pi.skills import stage_skill_args
from helmraiser.pipeline.diff import changed_paths
from helmraiser.pipeline.run_identity import resolve_run_id
from helmraiser.project_notes import project_notes_for_body
from helmraiser.runner.process_control import (
    PosixProcessController,
    ProcessController,
    ProcessIdentity,
)
from helmraiser.storage.state import StateManager
from helmraiser.task.loader import parse_task, render_task, resolve_models
from helmraiser.task.model import ModelSpec, TaskSpec
from helmraiser.task.paths import matches_paths, parse_paths
from helmraiser.task.project import (
    ServiceSpec,
    effective_env,
    effective_setup,
    expand_env,
    load_project_defaults,
    worktree_manager,
)

RETRY_DELAY_SECONDS = 30.0
# A pair's second run waits this long for its first run to record a base commit.
PIN_POLL_SECONDS = 1.0
PIN_WAIT_POLLS = 600
_CONVENTIONAL_SUBJECT = re.compile(r"^[a-z]+(\([^)]+\))?!?: \S")
DEFAULT_SETUP_TIMEOUT = 600
BLOCKED_REASON_CHARS = 500
SETUP_REASON_CHARS = 500
SETUP_REASON_COMMAND_CHARS = 200
SETUP_REASON_DETAIL_CHARS = 250
SETUP_RECORD_ENV = "HELMRAISER_SETUP_RECORD"
_SETUP_ERROR = re.compile(r"error|fatal|ERR!|ERR_", re.IGNORECASE)
REVIEW_TOOLS: tuple[str, ...] = ("read", "grep", "find", "ls")
PLAN_TOOLS: tuple[str, ...] = REVIEW_TOOLS

AdapterFactory = Callable[[StateManager, Isolation], PiAdapter]
AuthCheck = Callable[[ModelSpec], str | None]
Sleep = Callable[[float], Awaitable[None]]

# Ledger status → per-project state.db status (the lease/abort anchor). A run that
# awaits approval has no process left to abort, so it is terminal there.
STATE_STATUS = {
    "landed": "succeeded",
    "failed": "failed",
    "aborted": "aborted",
    "awaiting_approval": "succeeded",
}


@dataclass
class RunOutcome:
    run_id: str
    status: str
    failure_reason: str | None = None
    task_id: str | None = None
    iterations: int = 0
    escalated: bool = False
    review_rounds: int = 0
    cost_usd: float = 0.0
    minutes: float = 0.0
    branch_name: str | None = None
    worktree_path: str | None = None
    base_commit: str | None = None
    head_commit: str | None = None
    paused: dict[str, Any] | None = None
    verify_results: list[dict[str, Any]] = field(default_factory=list)
    evidence_results: list[dict[str, Any]] = field(default_factory=list)
    verdict: dict[str, Any] | None = None
    pending_approval: str | None = None  # plan | merge | scope
    pr_url: str | None = None
    warnings: list[str] = field(default_factory=list)
    services: list[dict[str, Any]] = field(default_factory=list)
    setup_sandbox: str | None = None
    scope_request: dict[str, Any] | None = None  # {paths, reason} while scope is pending

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── worktree environment, shared with ``helmraiser preflight`` (loop/preflight.py) ──


def helmraiser_env(
    run_id: str, worktree: Path, evidence_dir: Path, base_commit: str
) -> dict[str, str]:
    """The ``HELMRAISER_*`` variables every setup, verify and evidence command sees."""
    return {
        "HELMRAISER_RUN_ID": run_id,
        "HELMRAISER_WORKTREE": str(worktree),
        "HELMRAISER_EVIDENCE_DIR": str(evidence_dir),
        "HELMRAISER_BASE_SHA": base_commit,
    }


def command_env(helmraiser: dict[str, str], env: dict[str, str]) -> dict[str, str]:
    """Environment for setup / verify / evidence: the worker's, without secrets, plus
    ``helmraiser`` and the task's and project's ``env`` expanded over them."""
    base = {**os.environ, **helmraiser}
    return command_environment({**helmraiser, **expand_env(env, base)})


def step_env(command: dict[str, str], services: list[RunningService]) -> dict[str, str]:
    """Environment for verify / evidence: ``command`` plus what the services provide
    (their values win over the task's and project's ``env``)."""
    provided = {key: value for s in services for key, value in s.provides.items()}
    return {**command, **provided}


def command_wrap(
    isolation: Isolation, worktree: Path, run_dir: Path, kind: str, services: list[RunningService]
) -> Wrap | None:
    """Under process isolation verify and evidence steps run inside srt with the
    command sandbox settings, its settings in ``<run_dir>/srt-settings-<kind>.json``, behind
    one relay per service."""
    if not isinstance(isolation, ProcessIsolation):
        return None
    ports = tuple(service.host_port for service in services)
    return lambda argv: isolation.wrap_command(argv, worktree, run_dir, kind, ports)


def start_services(
    run_id: str,
    specs: tuple[ServiceSpec, ...],
    helmraiser: dict[str, str],
    on_event: Callable[[str, dict[str, Any]], None],
    started: list[RunningService],
) -> None:
    """Start each service in order, appending it to ``started``; raises ServiceError.
    ``stop_services(run_id)`` removes them all, by label."""
    base = {**os.environ, **helmraiser}
    for spec in specs:
        started.append(start_service(run_id, spec, expand_env(spec.env, base), on_event))


def failure_hints(outcome: RunOutcome) -> list[str]:
    """The environment hints of the outcome's failed verify and evidence steps (only a
    failed step has any), once each."""
    results = [*outcome.verify_results, *outcome.evidence_results]
    return list(dict.fromkeys(hint for r in results for hint in r.get("hints") or []))


def conventional_history(repo: Path, rev: str, sample: int = 20) -> bool:
    """True when most of the last ``sample`` non-merge subjects at ``rev`` are
    Conventional Commits (``type(scope)!: subject``)."""
    log = subprocess.run(
        ["git", "log", "--no-merges", f"-{sample}", "--format=%s", rev],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    subjects = log.stdout.splitlines() if log.returncode == 0 else []
    matching = sum(1 for s in subjects if _CONVENTIONAL_SUBJECT.match(s))
    return matching * 2 > len(subjects)


def _with_detail(reason: str, detail: str | None) -> str:
    detail = (detail or "").strip()
    return f"{reason}: {detail}" if detail else reason


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def blocked_detail(text: str) -> str:
    """The first paragraph of a BLOCKED answer, normalized and bounded."""
    text = text.strip().removeprefix("BLOCKED:").strip()
    lines = []
    for line in text.splitlines():
        if not line.strip():
            break
        lines.append(line)
    return _cap(_one_line("\n".join(lines)), BLOCKED_REASON_CHARS)


def setup_failure_detail(tail: str) -> str:
    """The first error and final nonblank line of setup output, without repetition."""
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    if not lines:
        return ""
    last = lines[-1]
    error = next(
        (line for line in lines if not line.startswith("+") and _SETUP_ERROR.search(line)), None
    )
    return f"{error} … {last}" if error is not None and error != last else last


def _setup_script(command: str) -> str:
    """``command`` after an ERR trap that records the last failed simple command.

    Only builtins run in the trap (no external tools, nothing that depends on the
    setup's PATH), its own stderr and ``set -x`` trace go to ``/dev/null``, and
    ``|| :`` keeps a failed write from mattering. The record is overwritten, so the
    last failure wins; it says nothing about whether that command ended the script,
    hence "last failed command" in the reason. The prefix is on its own line so a
    syntax error reports only the user's command.
    """
    return (
        'trap \'{ printf "%s\\t%s" "$?" "$BASH_COMMAND" '
        f'> "${SETUP_RECORD_ENV}"; }} 2>/dev/null || :\' ERR\n{command}'
    )


def _read_setup_record(path: Path) -> str | None:
    """The last failed command the trap recorded, if any."""
    try:
        code, _, command = path.read_text(encoding="utf-8", errors="replace").partition("\t")
    except OSError:
        return None
    return command if code.strip().isdigit() and code.strip() != "0" and command else None


class TaskLocked(Exception):
    def __init__(self, holder_run_id: str | None) -> None:
        super().__init__(f"task is held by run {holder_run_id}")
        self.holder_run_id = holder_run_id


class _RunFailed(Exception):
    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class _ScopeRequested(Exception):
    """The implementer needs files outside ``paths``: the run parks for the owner."""


_SCOPE_SEPARATOR = re.compile(r"\s+[—–-]\s+|\s*—\s*")


def parse_scope_request(text: str, allowed: tuple[str, ...] | None) -> tuple[list[str], str] | None:
    """``<path>[, <path>…] — <reason>`` (after ``SCOPE:``) as (new paths, reason).

    Paths the declaration already covers are dropped. ``None`` for a malformed request:
    no paths are declared, none is given, one is not a valid relative path, or all are
    covered already.
    """
    if allowed is None:
        return None
    head, *rest = _SCOPE_SEPARATOR.split(text.strip(), maxsplit=1)
    paths = [p.strip().strip("`'\"") for p in head.split(",")]
    try:
        parse_paths(paths)
    except ValueError:
        return None
    new = [p for p in dict.fromkeys(paths) if not matches_paths(p, allowed)]
    return (new, rest[0].strip() if rest else "") if new else None


class _RoundAbandoned(Exception):
    """An optional review round could not finish; the reviewed version stands."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def pair_lock_topic(task_id: str, run_id: str) -> str:
    """The state lock topic of a paired run of the task with frontmatter id ``task_id``."""
    return f"{task_id}/{run_id}"


def run_record(
    task: TaskSpec, run_id: str, *, pi_version: str, started_at: float
) -> dict[str, Any]:
    """A new run's ledger row; ``task`` carries the run's arm, if it has one."""
    return {
        "run_id": run_id,
        "task_id": task.task_id,
        "project": str(task.project),
        "status": "starting",
        "policy": task.policy or "review",
        "isolation": task.isolation,
        "delivery": task.delivery,
        "model_implement": task.models["implement"].render(),
        "model_fallback": task.models["fallback"].render() if "fallback" in task.models else None,
        "model_review": task.models["review"].render(),
        "pi_version": pi_version,
        "started_at": started_at,
    }


def arm_event(arm: RunArm, replaced: dict[str, Any]) -> dict[str, Any]:
    """The payload of a run's ``experiment_arm`` event."""
    return {"name": arm.name, "overrides": arm.overrides(), "replaced": replaced}


class GovernedRun:
    def __init__(
        self,
        task: TaskSpec,
        *,
        config: dict[str, Any],
        ledger: Ledger,
        adapter_factory: AdapterFactory,
        pi_version: str,
        data_dir: Path | None = None,
        snapshot: str | None = None,
        routing_decisions: list[dict[str, Any]] | None = None,
        arm: RunArm | None = None,
        pair: RunPair | None = None,
        run_id: str | None = None,
        auth_check: AuthCheck | None = None,
        isolation: Isolation | None = None,
        process_controller: ProcessController | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if pair is not None and arm is None:
            raise ValueError("a pair needs the arm of the run that claims it")
        self.snapshot = snapshot if snapshot is not None else render_task(task)
        self.arm = arm
        # ``pair``: this run claims its task as a pair. It is run X, with ``arm``; its
        # claim records run Y, with ``pair.arm`` applied to the same task.
        self.pair = pair
        self._unarmed_task = task
        self._arm_replaced: dict[str, Any] = {}
        if arm is not None:
            task, self._arm_replaced = apply_arm(task, arm, config)
        self.task = task
        self.config = config
        self.ledger = ledger
        self.adapter_factory = adapter_factory
        self.pi_version = pi_version
        self.data_dir = data_dir
        self.routing_decisions = copy.deepcopy(routing_decisions or [])
        self.project_notes = project_notes_for_body(task.project, task.body)
        self._leeboard_brief: BriefResult | None = None
        self.run_id = resolve_run_id(run_id)
        # The state lock is unique per topic. An unpaired run locks its task id; a
        # pair's two runs share a task, so each locks a topic of its own.
        self.lock_topic = pair_lock_topic(task.id, self.run_id) if pair is not None else task.id
        # The pair's other run, whose base commit this run starts from (a pair's run Y).
        self._pin_to: str | None = None
        pi_bin = str(config.get("pi_bin", "pi"))
        self.auth_check: AuthCheck = auth_check or (lambda model: check_auth(model, pi_bin))
        defaults = load_project_defaults(task.project)
        self.isolation = isolation or for_level(task.isolation, config)
        if isinstance(self.isolation, ProcessIsolation):
            self.isolation.set_project_deny_read(defaults.deny_read, task.project)
        self.process_controller = process_controller or PosixProcessController()
        self.sleep = sleep
        self.clock = clock
        self.tail_lines = int(config.get("verify_tail_lines", 120))
        self.excerpt_lines = int(config.get("verify_excerpt_lines", DEFAULT_EXCERPT_LINES))
        self.excerpt_chars = int(config.get("verify_excerpt_chars", DEFAULT_EXCERPT_CHARS))
        self.first_model_time_share = float(config.get("first_model_time_share", 0.6))
        self.plan_time_share = float(config.get("plan_time_share", 0.25))
        self.review_min_minutes = int(config.get("review_min_minutes", 10))
        self.setup_timeout = int(config.get("setup_timeout", DEFAULT_SETUP_TIMEOUT))
        self.setup = effective_setup(task, defaults)
        self.setup_config = defaults.setup_config
        self.setup_sandbox = (
            None
            if self.setup is None
            else (
                SANDBOX_HOST
                if self.setup_config.sandbox == SANDBOX_HOST
                else ("srt" if isinstance(self.isolation, ProcessIsolation) else "none")
            )
        )
        self.task_env = effective_env(task, defaults)
        self.service_specs = defaults.services
        self.services: list[RunningService] = []
        self._services_started = False

        self.rundir = RunDir(self.run_id, data_dir)
        self.event_logger: EventLogger = self.rundir.event_logger()
        if isinstance(self.isolation, ProcessIsolation):
            self.isolation.retry_observer = lambda payload: self._event("sandbox_retry", payload)
        self.state = StateManager(task.project / ".helmraiser" / "state.db")
        self.worktrees = worktree_manager(task.project)
        self.adapter: PiAdapter = self.adapter_factory(self.state, self.isolation)

        self.started_at = 0.0
        self.guard = BudgetGuard(task.budget, 0.0, clock)
        self.worktree: Path | None = None
        self.base_commit: str | None = None
        self.conventional_commits: bool | None = None
        self.branch_name: str | None = None
        self.head_commit: str | None = None
        self.iteration = 0
        self.escalated = False
        self.review_rounds = 0
        self.review_count = 0
        self.implement_model = task.models["implement"]
        self.implement_session_id = session_id_for(self.run_id, "implement")
        self.implement_attempt = 1
        self.locked = False
        self.last_results: list[VerifyResult] = []
        self.evidence_results: list[EvidenceResult] = []
        self.verdict: Verdict | None = None
        self.paused: dict[str, Any] | None = None
        self.warnings: list[str] = []
        self.usage = Usage()
        self.resume_row: dict[str, Any] | None = None
        self.resume_stage = "implement"
        # The failure a run handed back by ``retry`` ended with (None after a plan approval).
        self.retried_from: str | None = None
        # True from a retry of a ``budget:*`` failure on, for the run's every later resume:
        # iterations and active time then count from the run's start, never afresh.
        self.cumulative_budgets = False
        # ``retry --implement`` after a budget stop: the first session may find nothing left
        # to do, and the unverified branch still goes to verify.
        self._verify_unchanged_head = False
        self.pending_approval: str | None = None
        self.pr_url: str | None = None
        self._cost_at_start = 0.0
        self._budget_kind: str | None = None
        self._stream_cost = 0.0
        self.plan_ran = False
        self.reworks: list[dict[str, Any]] = []
        self.rework_request: dict[str, Any] | None = None
        self.rebase_onto: str | None = None
        self.published_head: str | None = None
        self.rework_ceiling: int | None = None
        # The implementer's pending scope request ({paths, reason}) and, on a resume after
        # ``helmraiser approve``, the owner's grant ({paths, note}).
        self.scope_request: dict[str, Any] | None = None
        self.scope_grant: dict[str, Any] | None = None

    @classmethod
    def resume(
        cls,
        run_id: str,
        *,
        config: dict[str, Any],
        ledger: Ledger,
        adapter_factory: AdapterFactory,
        pi_version: str,
        data_dir: Path | None = None,
        **overrides: Any,
    ) -> GovernedRun:
        """A run handed back to the worker; ``run()`` continues at the run's ``stage``:
        ``implement`` (an approved plan, the default, or ``retry --implement``) or
        ``verify`` (a retry), or ``start`` (a retry without commits). A retried run
        still carries its failure reason."""
        row = ledger.get_run(run_id)
        if row is None:
            raise ValueError(f"unknown run: {run_id}")
        task_row = ledger.get_task(str(row["task_id"]))
        if task_row is None:
            raise ValueError(f"run {run_id} has no task row")
        task = parse_task(str(task_row["snapshot"]), Path(str(task_row["source_path"])), config)
        task = replace(task, delivery=str(row["delivery"]))
        # Snapshots retain author intent; dispatch choices belong to this run alone.
        for event in ledger.events(run_id, types=("routing",)):
            payload = event["payload"]
            if event["type"] != "routing" or "from" not in payload or "to" not in payload:
                continue
            if payload.get("lever") == "implement" and payload["from"] != payload["to"]:
                task = replace(
                    task,
                    models={
                        **task.models,
                        "implement": ModelSpec.parse(str(row["model_implement"])),
                    },
                )
            elif (
                payload.get("lever") == "plan"
                and payload["from"] is False
                and payload["to"] is True
            ):
                task = replace(task, plan=True)
        if "arm" not in overrides:
            arm_events = ledger.events(run_id, types=("experiment_arm",))
            if arm_events:
                payload = arm_events[0]["payload"]
                overrides["arm"] = RunArm.from_overrides(payload["name"], payload["overrides"])
        run = cls(
            task,
            config=config,
            ledger=ledger,
            adapter_factory=adapter_factory,
            pi_version=pi_version,
            data_dir=data_dir,
            snapshot=str(task_row["snapshot"]),
            run_id=run_id,
            **overrides,
        )
        paired = ledger.pair_for_run(run_id)
        if paired is not None:
            run.lock_topic = pair_lock_topic(task.id, run_id)
            if row["stage"] == NEW_RUN_STAGE:
                run._pin_to = str(paired["run_x"] if paired["run_y"] == run_id else paired["run_y"])
        run.resume_row = row
        run.resume_stage = str(row["stage"] or "implement")
        run.retried_from = str(row["failure_reason"]) if row["failure_reason"] else None
        run.cumulative_budgets = run._budget_retry or any(
            e["payload"].get("budget_retry") for e in ledger.events(run_id, types=("run_resume",))
        )
        reopened = ledger.events(run_id, types=("rework_reopened",))
        if reopened:
            run.published_head = str(reopened[-1]["payload"]["head_commit"])
        run.reworks = ledger.reworks(run_id)
        if run.reworks:
            events = ledger.events(run_id, types=("run_resume", "review_verdict"))
            latest = unconsumed_rework(run.reworks, events)
            if latest is not None:
                run.rework_request = latest
                if latest.get("rebase"):
                    run.rebase_onto = str(row["base_branch"])
            run.review_count = sum(e["type"] == "review_verdict" for e in events)
        # A scope approval hands the run back with the approved paths; resumed once.
        handed = ledger.events(run_id, types=("handed_back", "run_resume"))
        if handed and handed[-1]["type"] == "handed_back" and "scope" in handed[-1]["payload"]:
            run.scope_grant = handed[-1]["payload"]["scope"]
        return run

    @property
    def _budget_retry(self) -> bool:
        """Handed back by ``retry`` after a ``budget:*`` failure."""
        return (self.retried_from or "").startswith("budget:")

    # ── public ──────────────────────────────────────────────────────────

    async def run(self) -> RunOutcome:
        if self.resume_row is not None:
            return await self._run_resumed(self.resume_row)
        task = self.task
        self.started_at = self.clock()
        self.guard = BudgetGuard(task.budget, self.started_at, self.clock)
        if not self.state.acquire_lock(self.run_id, self.lock_topic, os.getpid()):
            raise TaskLocked(self.state.get_locked_run_id(self.lock_topic))
        self.locked = True
        try:
            self.ledger.claim_queued_run(
                run_record(
                    task, self.run_id, pi_version=self.pi_version, started_at=self.started_at
                ),
                snapshot=self.snapshot,
                config=self.config,
                pair=self._pair_claim(),
            )
            self.state.create_run(self.run_id, self.lock_topic)
            self._event("run_start", {"task_id": task.task_id, "pi_version": self.pi_version})
            if self.arm is not None:
                self._event("experiment_arm", arm_event(self.arm, self._arm_replaced))
            for decision in self.routing_decisions:
                self._event("routing", decision)
            write_status(self.ledger, self.data_dir)
        except Exception:  # recording failed: do not leave the task claimed
            self.state.release_lock(self.run_id, self.lock_topic)
            self.locked = False
            raise
        return await self._run_fresh()

    def _pair_claim(self) -> dict[str, Any] | None:
        """What this run's claim records for its pair's other run (``claim_queued_run``):
        that run's row and ``experiment_arm`` event, with its own arm applied to the
        task as dispatched, and copies of this run's routing decisions."""
        if self.pair is None or self.arm is None:
            return None
        arm = self.pair.arm
        task, replaced = apply_arm(self._unarmed_task, arm, self.config)
        return {
            "experiment_id": self.pair.experiment_id,
            "arm_x": self.arm.name,
            "arm_y": arm.name,
            "run": run_record(
                task, resolve_run_id(), pi_version=self.pi_version, started_at=self.started_at
            ),
            "events": [
                ("experiment_arm", arm_event(arm, replaced)),
                *(("routing", decision) for decision in self.routing_decisions),
            ],
        }

    async def _run_new(self) -> RunOutcome:
        """Start a pair's second run, which its first run's claim recorded as
        ``resuming`` at stage ``new``: a fresh pipeline run, not a resume."""
        task = self.task
        self.started_at = self.clock()
        self.guard = BudgetGuard(task.budget, self.started_at, self.clock)
        try:
            self.ledger.update_run(
                self.run_id,
                status="starting",
                stage=None,
                started_at=self.started_at,
                pi_version=self.pi_version,
            )
            self.ledger.set_task_status(task.task_id, "running", run_id=self.run_id)
            self.state.create_run(self.run_id, self.lock_topic)
            self._event("run_start", {"task_id": task.task_id, "pi_version": self.pi_version})
            write_status(self.ledger, self.data_dir)
        except Exception:  # as for a claimed run: reconcile closes it
            self.state.release_lock(self.run_id, self.lock_topic)
            self.locked = False
            raise
        return await self._run_fresh()

    async def _pinned_base(self) -> tuple[str, str] | None:
        """The base branch and commit a pair's second run starts from: those of the
        first run, so that both arms start from identical code. Waits while that run
        is live without a worktree. None for any other run, and when the first run
        ended without a worktree: there is nothing to pin to."""
        if self._pin_to is None:
            return None
        for _ in range(PIN_WAIT_POLLS):
            row = self.ledger.get_run(self._pin_to)
            if row is None:
                return None
            if row["base_commit"]:
                return str(row["base_branch"]), str(row["base_commit"])
            if row["status"] not in LIVE_RUN_STATUSES:
                return None
            await self.sleep(PIN_POLL_SECONDS)
        raise _RunFailed("failed", "pair: the other run recorded no base commit to start from")

    async def _run_fresh(self) -> RunOutcome:
        """The pipeline of a run that has just been recorded as started."""
        task = self.task
        status = "failed"
        reason: str | None = None
        try:
            self._preflight()
            self._check_tracked_denials()
            self._create_worktree(await self._pinned_base())
            self._materialise()
            self._run_setup()
            if task.policy == "approve-plan-first":
                await self._plan(wait_for_approval=True)
                status, reason = "awaiting_approval", None
            else:
                plan = await self._plan(wait_for_approval=False) if task.plan else None
                status, reason = await self._finish_from_implement(plan, plan_approved=False)
        except _ScopeRequested:
            status, reason = "awaiting_approval", None
        except _RunFailed as exc:
            status, reason = exc.status, exc.reason
        except Exception as exc:  # infrastructure error: never leave the run dangling
            status, reason = "failed", f"error: {type(exc).__name__}: {exc}"
        except BaseException:  # cancelled or interrupted: reconcile closes the run later
            self._stop_services()
            raise
        return self._close(status, reason)

    def claim_resume(self) -> bool:
        if not self.state.acquire_lock(self.run_id, self.lock_topic, os.getpid()):
            return False
        self.locked = True
        try:
            if self.ledger.claim_resumable(self.run_id, checked=True, config=self.config):
                return True
        except BaseException:
            self.state.release_lock(self.run_id, self.lock_topic)
            self.locked = False
            raise
        self.state.release_lock(self.run_id, self.lock_topic)
        self.locked = False
        return False

    async def _run_resumed(self, row: dict[str, Any]) -> RunOutcome:
        task = self.task
        if not self.locked:
            if not self.state.acquire_lock(self.run_id, self.lock_topic, os.getpid()):
                raise TaskLocked(self.state.get_locked_run_id(self.lock_topic))
            self.locked = True
        if self.resume_stage == NEW_RUN_STAGE:
            return await self._run_new()
        self.started_at = self.clock()  # the time budget counts active time only
        prior_minutes = (
            completed_active_minutes(
                self.ledger.events(self.run_id, types=("run_start", "run_resume", "run_end")),
                started_at=float(row["started_at"]),
                ended_at=row["ended_at"],
            )
            if self.reworks or self.cumulative_budgets
            else 0.0
        )
        self.guard = BudgetGuard(
            task.budget,
            self.started_at,
            self.clock,
            prior_minutes=prior_minutes,
            extra_cost_usd=sum(r["extra_cost_usd"] for r in self.reworks),
            extra_minutes=sum(r["extra_minutes"] for r in self.reworks),
        )
        self.usage = Usage(
            int(row["tokens_in"] or 0), int(row["tokens_out"] or 0), float(row["cost_usd"] or 0.0)
        )
        self._cost_at_start = self.usage.cost_usd
        self.guard.settle(self.usage.cost_usd)
        self.iteration = len(self.ledger.iterations(self.run_id))
        if self.rework_request is not None:
            self.rework_ceiling = rework_iteration_ceiling(self.rework_request)
        elif self.cumulative_budgets:
            self.rework_ceiling = task.budget.max_iterations
        self.escalated = bool(row["escalated"])
        self.base_commit = row["base_commit"]
        self.branch_name = row["branch_name"]
        self.pr_url = row["pr_url"]
        stage = self.resume_stage
        retry_implement = stage == "implement" and self.retried_from is not None
        restart = stage == "start"
        if restart or retry_implement or self.rework_request is not None:
            # A fresh escalation ladder: the implement model first, then the fallback.
            self.escalated = False
            self.ledger.update_run(self.run_id, escalated=0)
        self.ledger.update_run(
            self.run_id, status="running", stage=stage, ended_at=None, failure_reason=None
        )
        self.ledger.set_task_status(task.task_id, "running", run_id=self.run_id)
        self.state.update_run_status(self.run_id, "running")
        self._event(
            "run_resume",
            {
                "task_id": task.task_id,
                "from": (
                    "rework"
                    if self.rework_request is not None
                    else (
                        "scope"
                        if self.scope_grant is not None
                        else "retry" if restart or stage == "verify" or retry_implement else "plan"
                    )
                ),
                "stage": stage,
                **({"restarted_without_commits": True} if restart else {}),
                **({"budget_retry": True} if self._budget_retry else {}),
                **({"rework_round": self.rework_request["round"]} if self.rework_request else {}),
            },
        )
        write_status(self.ledger, self.data_dir)

        status = "failed"
        reason: str | None = None
        try:
            self._check_arm_models()
            try:
                self.worktree = self.worktrees.create(task.id, self.run_id, resume=True)
            except RuntimeError as exc:
                raise _RunFailed("failed", f"worktree: {exc}") from exc
            self.rundir.create()
            if restart:
                self._run_restart_setup()
            elif self.rework_request and self.rework_request.get("worktree_recreated"):
                self._run_setup()
            plan = self._read_plan()
            if restart:
                if plan is None and task.policy == "approve-plan-first":
                    await self._plan(wait_for_approval=True)
                    status, reason = "awaiting_approval", None
                else:
                    if plan is None and task.plan:
                        plan = await self._plan(wait_for_approval=False)
                    approved = plan is not None and self._plan_approved()
                    status, reason = await self._finish_from_implement(
                        plan,
                        plan_approved=approved,
                        prompt=retry_restart_prompt(task, self.retried_from or "", plan, approved),
                    )
            elif stage == "verify":
                status, reason = await self._finish_from_verify(plan)
            elif self.rework_request is not None:
                prompt = rework_prompt(
                    task,
                    self.rework_request["note"],
                    self.rework_request["findings"],
                    rebase_onto=self.rebase_onto,
                    published_pr=bool(self.pr_url),
                )
                if plan is not None:
                    heading = "Approved plan" if self._plan_approved() else "Plan"
                    prompt += f"\n\n## {heading}\n\n{plan}"
                status, reason = await self._finish_from_implement(
                    plan,
                    plan_approved=self._plan_approved(),
                    prompt=prompt,
                )
            elif self.scope_grant is not None:
                prompt = (
                    scope_declined_prompt(self.scope_grant["declined"], self.scope_grant["note"])
                    if "declined" in self.scope_grant
                    else scope_approved_prompt(
                        self.scope_grant["paths"], self.scope_grant.get("note")
                    )
                )
                if plan is not None:
                    heading = "Approved plan" if self._plan_approved() else "Plan"
                    prompt += f"\n\n## {heading}\n\n{plan}"
                status, reason = await self._finish_from_implement(
                    plan,
                    plan_approved=plan is not None and self._plan_approved(),
                    prompt=prompt,
                )
            elif retry_implement:
                self.last_results = last_verify_results(self.ledger.iterations(self.run_id))
                if self._budget_retry:
                    self._commit_retry_work()
                    self._verify_unchanged_head = True
                prompt = retry_implement_prompt(self.last_results, self.retried_from or "")
                status, reason = await self._finish_from_implement(
                    plan,
                    plan_approved=plan is not None and self._plan_approved(),
                    prompt=prompt,
                )
            else:
                status, reason = await self._finish_from_implement(plan, plan_approved=True)
        except _ScopeRequested:
            status, reason = "awaiting_approval", None
        except _RunFailed as exc:
            status, reason = exc.status, exc.reason
        except Exception as exc:
            status, reason = "failed", f"error: {type(exc).__name__}: {exc}"
        except BaseException:
            self._stop_services()
            raise
        return self._close(status, reason)

    async def _finish_from_implement(
        self, plan: str | None = None, *, plan_approved: bool = True, prompt: str | None = None
    ) -> tuple[str, str | None]:
        # Services start before the first implement session, so one that cannot start
        # fails the run before any model time is spent; verify and evidence use them.
        self._start_services()
        self.head_commit = await self._implement(plan, plan_approved=plan_approved, prompt=prompt)
        self.run_evidence()
        verdict = (
            await self.run_review(plan, plan_approved=plan_approved)
            if plan is not None
            else await self.run_review()
        )
        feedback_limit = max(0, int(self.config.get("review_feedback_rounds", 1)))
        round_abandoned = False

        while (
            feedback_limit > self.review_rounds
            and self.verdict is not None
            and self.verdict.parsed
            and self.verdict.verdict != "uncertain"
            and self.verdict.findings
        ):
            completed = await self._run_review_round(
                review_feedback_prompt(self.verdict.findings),
                plan=plan,
                plan_approved=plan_approved,
            )
            if not completed:
                round_abandoned = True
                break
            verdict = self.verdict.verdict if self.verdict else verdict

        fallback = self.task.models.get("fallback")
        if (
            feedback_limit > 0
            and self.verdict is not None
            and self.verdict.parsed
            and self.verdict.verdict != "uncertain"
            and self.verdict.severe
            and not round_abandoned
            and not self.escalated
            and self._remaining_rework_iterations() > 0
            and fallback is not None
            and fallback.render() != self.implement_model.render()
        ):
            self.escalated = True
            self.ledger.update_run(self.run_id, escalated=1)
            self.implement_model = fallback
            self.implement_attempt = 2
            self.implement_session_id = session_id_for(self.run_id, "implement", 2)
            max_iterations = math.ceil(self.task.budget.max_iterations / 2)
            self._event(
                "escalated",
                {"model": fallback.render(), "max_iterations": max_iterations, "reason": "review"},
                stage="implement",
            )
            prompt = escalation_prompt(
                self.task, self.ledger.iterations(self.run_id), self.base_commit or "HEAD"
            )
            prompt += "\n\n" + review_feedback_prompt(self.verdict.findings)
            completed = await self._run_review_round(
                prompt,
                plan=plan,
                plan_approved=plan_approved,
                max_iterations=max_iterations,
            )
            if completed:
                verdict = self.verdict.verdict if self.verdict else verdict

        decision = self.decide_merge(verdict)
        return decision.status, decision.failure_reason

    async def _finish_from_verify(self, plan: str | None) -> tuple[str, str | None]:
        """A retried run: verify the branch as it stands (uncommitted work is committed
        first), then evidence, review and the merge decision. No implement session and
        no review-feedback round."""
        assert self.worktree is not None
        self._stage("verify")
        # Before the services: a run they fail keeps its work and resumes here, not at start.
        self._commit_retry_work()
        self._start_services()
        self.head_commit = self._git("rev-parse", "HEAD").stdout.strip()
        self.ledger.update_run(self.run_id, status="verifying")
        results = self._run_verify(self.implement_session_id, self.iteration, stage="verify")
        self.ledger.update_run(self.run_id, status="running")
        passed = all_passed(results)
        self.last_results = results
        self._event(
            "verify_result",
            {"n": self.iteration, "passed": passed, "results": [r.as_dict() for r in results]},
            stage="verify",
        )
        if self.iteration:
            self.ledger.record_verify(
                self.run_id,
                self.iteration,
                commit_sha=self.head_commit,
                verify_passed=passed,
                verify_results=[r.as_dict() for r in results],
            )
        if not passed:
            failed = ", ".join(r.name for r in results if not r.passed)
            raise _RunFailed("failed", f"verify_failed: {failed}")
        self.run_evidence()
        if plan is None:
            verdict = await self.run_review()
        else:
            verdict = await self.run_review(plan, plan_approved=self._plan_approved())
        decision = self.decide_merge(verdict)
        return decision.status, decision.failure_reason

    def _plan_approved(self) -> bool:
        return any(
            a["kind"] == "plan" and a["decision"] == "approved"
            for a in self.ledger.approvals(self.run_id)
        )

    def _plan_approval_note(self) -> str | None:
        for approval in reversed(self.ledger.approvals(self.run_id)):
            if approval["kind"] == "plan" and approval["decision"] == "approved":
                return str(approval["note"]) if approval["note"] else None
        return None

    def _scope_decisions(self) -> list[ScopeDecision]:
        approvals = [a for a in self.ledger.approvals(self.run_id) if a["kind"] == "scope"]
        requests: list[dict[str, Any]] = []
        for event in self.ledger.events(self.run_id, types=("scope_requested", "handed_back")):
            payload = event["payload"]
            if event["type"] == "scope_requested":
                requests.append({"paths": payload["paths"]})
            elif requests and "scope" in payload:
                requests[-1]["scope"] = payload["scope"]

        decisions = []
        # Each request creates one approval. Pair before filtering so unresolved or
        # rejected approvals cannot shift later decisions onto the wrong request.
        for index, approval in enumerate(approvals):
            decision = approval["decision"]
            if decision not in ("approved", "declined"):
                continue
            request = requests[index] if index < len(requests) else {}
            scope = request.get("scope", {})
            if decision == "approved":
                # Only the handback records the actual subset granted by --paths.
                paths = scope.get("paths", ())
            else:
                paths = scope.get("declined", request.get("paths", ()))
            note = approval["note"]
            decisions.append(
                ScopeDecision(decision, tuple(paths), str(note) if note is not None else None)
            )
        return decisions

    async def _run_review_round(
        self,
        prompt: str,
        *,
        plan: str | None,
        plan_approved: bool,
        max_iterations: int | None = None,
    ) -> bool:
        """Run review-driven fixes; restore the reviewed state when the round cannot verify."""
        assert self.worktree is not None and self.verdict is not None
        limit = min(
            max_iterations or math.ceil(self.task.budget.max_iterations / 2),
            self._remaining_rework_iterations(),
        )
        if limit == 0:
            return False
        reviewed_commit = self._git("rev-parse", "HEAD").stdout.strip()
        reviewed_verdict = self.verdict
        reviewed_text = self.rundir.review_md.read_text(encoding="utf-8")
        reviewed_results = self.last_results
        reviewed_evidence = self.evidence_results
        self.review_rounds += 1
        self._event(
            "review_feedback",
            {
                "round": self.review_rounds,
                "model": self.implement_model.render(),
                "severe": len(reviewed_verdict.severe),
            },
            stage="implement",
            session_id=self.implement_session_id,
        )
        try:
            commit = await self._iterate_review_fixes(prompt, limit)
        except (_RunFailed, _RoundAbandoned) as exc:
            if isinstance(exc, _RunFailed) and not exc.reason.startswith("budget:"):
                raise
            return self._abandon_review_round(
                exc.reason,
                reviewed_commit,
                reviewed_verdict,
                reviewed_text,
                reviewed_results,
                reviewed_evidence,
            )
        if commit is None:
            return self._abandon_review_round(
                "verify_exhausted",
                reviewed_commit,
                reviewed_verdict,
                reviewed_text,
                reviewed_results,
                reviewed_evidence,
            )
        self.head_commit = commit
        self.run_evidence()
        if plan is not None:
            await self.run_review(plan, plan_approved=plan_approved)
        else:
            await self.run_review()
        return True

    def _abandon_review_round(
        self,
        reason: str,
        commit: str,
        verdict: Verdict,
        review_text: str,
        results: list[VerifyResult],
        evidence: list[EvidenceResult],
    ) -> bool:
        assert self.worktree is not None
        self._git("reset", "--hard", commit)
        self._git("clean", "-fd")
        self.head_commit = commit
        self.verdict = verdict
        self.last_results = results
        self.evidence_results = evidence
        self.rundir.review_md.write_text(review_text, encoding="utf-8")
        self.ledger.update_run(self.run_id, verdict=verdict.verdict, status="running")
        if reason.startswith("budget:"):
            # A streaming budget stop uses the abort machinery to terminate pi, but
            # abandoning this optional round must not turn the governed run into aborted.
            self.state.update_run_status(self.run_id, "running")
        self._event(
            "review_round_abandoned",
            {"round": self.review_rounds, "reason": reason},
            stage="implement",
        )
        return False

    # ── steps 0–4 ───────────────────────────────────────────────────────

    def _check_arm_models(self) -> None:
        if self.arm is not None:
            implement = self.task.models["implement"]
            if implement.model == self.task.models["review"].model:
                raise _RunFailed(
                    "failed",
                    f"models: reviewer must differ from implementer (both {implement.model})",
                )

    def _preflight(self) -> None:
        self._check_arm_models()
        reason = self.isolation.check_available()
        if reason is not None:
            raise _RunFailed("failed", f"isolation: {reason}")
        reason = self.auth_check(self.task.models["implement"])
        if reason is not None:
            raise _RunFailed("failed", f"auth: {reason}")
        self._event("preflight", {"pi_version": self.pi_version, "auth": "ready"})

    def _check_tracked_denials(self) -> None:
        """Warn (never fail) when a relative deny_read entry hides tracked files."""
        if not isinstance(self.isolation, ProcessIsolation):
            return
        found = tracked_denials(self.task.project, self.isolation.configured_deny_read())
        if not found:
            return
        self.warnings += tracked_denial_warnings(found)
        self._event(
            "sandbox_tracked_deny",
            {"entries": {entry: files[:20] for entry, files in found.items()}},
        )

    def _create_worktree(self, pinned: tuple[str, str] | None = None) -> None:
        """``pinned``: the base branch and commit to start from (``_pinned_base``),
        instead of the tip of the task's base."""
        try:
            maintenance = self.worktrees.disable_auto_maintenance()
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            self._event("git_maintenance", {"error": str(exc)})
        else:
            if maintenance["changed"]:
                self._event(
                    "git_maintenance",
                    {"set": "maintenance.auto=false", "previous": maintenance["previous"]},
                )
        if self.task.delivery == "pr" and pinned is None:
            payload = self.worktrees.sync_base(base=self.task.base)
            self._event("base_sync", payload)
        try:
            if pinned is None:
                self.worktree = self.worktrees.create(
                    self.task.id, self.run_id, base=self.task.base
                )
            else:
                self.worktree = self.worktrees.create(
                    self.task.id, self.run_id, base=pinned[0], base_commit=pinned[1]
                )
        except RuntimeError as exc:
            raise _RunFailed("failed", f"worktree: {exc}") from exc
        meta = self.worktrees.metadata(self.run_id)
        self.base_commit = str(meta["base_commit"])
        self.branch_name = str(meta["branch_name"])
        self.ledger.update_run(
            self.run_id,
            worktree_path=str(self.worktree),
            branch_name=self.branch_name,
            base_branch=str(meta["base_branch"]),
            base_commit=self.base_commit,
        )
        self._event(
            "worktree_created",
            {"path": str(self.worktree), "branch": self.branch_name, "base": self.base_commit},
        )

    def _materialise(self) -> None:
        self.rundir.create()
        self.rundir.task_md.write_text(self.snapshot, encoding="utf-8")
        self._write_directives(iteration=1, max_iterations=self.task.budget.max_iterations)

    def _write_directives(self, iteration: int, max_iterations: int) -> None:
        if self._leeboard_brief is None:
            self._leeboard_brief = fetch_brief(self.worktree or self.task.project, self.run_id)
        origin = self.task.origin
        leeboard_handoff_to = (
            f"session:{origin['session']}"
            if origin is not None
            and origin["kind"] == "claude-code"
            and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", origin["session"])
            else None
        )
        scout = self.task.models.get("scout") or resolve_models(None, self.config).get("scout")
        self.rundir.directives_md.write_text(
            render_implement_directives(
                run_id=self.run_id,
                title=self.task.title,
                worktree=self.worktree or "<worktree>",
                task_path=self.rundir.task_md,
                task_id=self.task.id,
                scout_model=scout.render() if scout else None,
                max_iterations=max_iterations,
                max_minutes=(
                    round(self.guard.remaining_seconds() / 60, 1)
                    if self.cumulative_budgets
                    else self.guard.max_minutes if self.reworks else self.task.budget.max_minutes
                ),
                max_cost_usd=(
                    max(0.0, self.guard.max_cost_usd - self.guard.cost_usd)
                    if self.cumulative_budgets
                    else self.guard.max_cost_usd if self.reworks else self.task.budget.max_cost_usd
                ),
                iteration=iteration,
                has_plan_approval_note=bool(self._plan_approval_note()),
                paths=self.task.paths,
                service_vars=tuple(key for spec in self.service_specs for key in spec.provides),
                project_notes=self.project_notes,
                leeboard_brief=self._leeboard_brief,
                leeboard_handoff_to=leeboard_handoff_to,
            ),
            encoding="utf-8",
        )

    # ── worktree environment (task/project ``setup`` and ``env``) ───────

    def _helmraiser_env(self) -> dict[str, str]:
        assert self.worktree is not None and self.base_commit is not None
        return helmraiser_env(self.run_id, self.worktree, self.rundir.evidence, self.base_commit)

    def _session_env(self) -> dict[str, str]:
        """Extra environment for pi sessions (the adapter adds the HELMRAISER_* base)."""
        base = {**os.environ, **self._helmraiser_env()}
        return {
            "HELMRAISER_WORKTREE": base["HELMRAISER_WORKTREE"],
            **expand_env(self.task_env, base),
        }

    def _command_env(self) -> dict[str, str]:
        """Environment for setup / verify / evidence: the worker's, without secrets."""
        return command_env(self._helmraiser_env(), self.task_env)

    def _step_env(self) -> dict[str, str]:
        return step_env(self._command_env(), self.services)

    def _command_wrap(self, kind: str) -> Wrap | None:
        if self.worktree is None:
            return None
        return command_wrap(self.isolation, self.worktree, self.rundir.path, kind, self.services)

    # ── project services (``[[services]]``, loop/services.py) ─────────

    def _start_services(self) -> None:
        if not self.service_specs or self._services_started:
            return
        self._services_started = True
        try:
            start_services(
                self.run_id,
                self.service_specs,
                self._helmraiser_env(),
                lambda kind, payload: self._event(kind, payload),
                self.services,
            )
        except ServiceError as exc:
            raise _RunFailed("failed", str(exc)) from exc

    def _stop_services(self) -> None:
        """Remove the run's containers (by label). Idempotent; never raises."""
        if not self._services_started:
            return
        self._services_started = False
        try:
            removed, error = stop_services(self.run_id)
        except Exception as exc:  # cleanup must not mask the run's own outcome
            removed, error = [], f"{type(exc).__name__}: {exc}"
        self._event("service_stop", {"removed": removed, "error": error})
        if error is not None:
            self.warnings.append(
                f"services: cleanup failed ({error}); `helmraiser work` removes them later"
            )

    def _note_unconfined(
        self, kind: str, results: list[VerifyResult] | list[EvidenceResult], **where: Any
    ) -> None:
        for result in results:
            if result.sandbox == SANDBOX_HOST:
                self._event(
                    "step_unconfined",
                    {"kind": kind, "name": result.name, "note": UNCONFINED_NOTE},
                    stage="implement",
                    **where,
                )

    def _failure_report(self, log_prefix: str) -> FailureReport:
        return FailureReport(
            self.excerpt_lines,
            self.excerpt_chars,
            self.rundir.logs,
            log_prefix,
            tuple(service.host_port for service in self.services),
        )

    def _rebase_in_progress(self) -> bool:
        assert self.worktree is not None
        for name in ("rebase-merge", "rebase-apply"):
            result = self._git("rev-parse", "--git-path", name)
            if result.returncode == 0:
                path = Path(result.stdout.strip())
                if not path.is_absolute():
                    path = self.worktree / path
                if path.is_dir():
                    return True
        return False

    def _merge_in_progress(self) -> bool:
        return self._git("rev-parse", "--verify", "MERGE_HEAD").returncode == 0

    def _rebase_result(self) -> VerifyResult | None:
        published = self.published_head
        if (
            published is not None
            and self._git("merge-base", "--is-ancestor", published, "HEAD").returncode != 0
        ):
            return VerifyResult(
                name="rebase",
                passed=False,
                exit_code=1,
                seconds=0.0,
                tail=(
                    "Published commits must not be rewritten. "
                    f"Restore the published head {published} as an ancestor of HEAD, "
                    "preserving its commits and applying fixes on top. Do not force push."
                ),
            )
        base = self.rebase_onto
        if base is None:
            return None
        if self.pr_url and (self._merge_in_progress() or self._rebase_in_progress()):
            diagnostic = (
                "The base integration is in progress and unfinished. Complete the merge with "
                "`git add` and `git commit --no-edit`, or abort the unfinished merge/rebase "
                f"and use `git merge --no-edit {base}`. Do not rewrite published commits."
            )
        elif self._rebase_in_progress():
            diagnostic = (
                "The rebase is in progress and unfinished. Finish it with `git add` and "
                "`git rebase --continue`, or run `git rebase --abort`."
            )
        else:
            ref = f"refs/heads/{base}"
            resolved = self._git("rev-parse", "--verify", ref)
            tip = resolved.stdout.strip()
            if (
                resolved.returncode == 0
                and tip
                and self._git("merge-base", "--is-ancestor", tip, "HEAD").returncode == 0
            ):
                previous = self.base_commit
                self.base_commit = tip
                self.ledger.update_run(self.run_id, base_commit=tip)
                self.worktrees.adopt_base(self.run_id, tip)
                self._event(
                    "rebased",
                    {"base_branch": base, "base_commit": tip, "previous": previous},
                    stage="implement",
                )
                self.rebase_onto = None
                return None
            diagnostic = f"The branch does not contain {base} at {tip[:12] or 'unresolved tip'}. " + (
                f"Run `git merge --no-edit {base}`, resolve conflicts keeping both sides' "
                "intent, `git add`, and `git commit --no-edit`. Do not rewrite published commits."
                if self.pr_url
                else f"Run `git rebase {base}`, resolve conflicts keeping both sides' intent, "
                "`git add`, and `git rebase --continue`. Do not merge."
            )
        return VerifyResult(name="rebase", passed=False, exit_code=1, seconds=0.0, tail=diagnostic)

    def _path_scope_result(self) -> VerifyResult | None:
        patterns = self.task.paths
        if patterns is None:
            return None
        assert self.worktree is not None and self.base_commit is not None
        try:
            offending = [
                path
                for path in changed_paths(self.worktree, self.base_commit)
                if not matches_paths(path, patterns)
            ]
        except RuntimeError as exc:
            detail = f"Scope could not be established: {exc}"
        else:
            if not offending:
                return None
            detail = "Offending paths:\n" + "\n".join(f"- {path!r}" for path in offending)
        diagnostic = (
            "Task path scope failure.\n"
            + detail
            + "\nAllowed globs:\n"
            + "\n".join(f"- {pattern!r}" for pattern in patterns)
            + "\nAcceptance commands were skipped. Restore/remove forbidden changes; "
            "do not broaden the declaration."
        )
        return VerifyResult(
            name="task-paths", passed=False, exit_code=1, seconds=0.0, tail=diagnostic
        )

    def _run_verify(
        self, session_id: str, n: int, *, stage: str = "implement"
    ) -> list[VerifyResult]:
        assert self.worktree is not None
        rebase_failure = self._rebase_result()
        if rebase_failure is not None:
            self._event(
                "rebase_pending",
                {"n": n, "base_branch": self.rebase_onto, "diagnostic": rebase_failure.tail},
                stage=stage,
                session_id=session_id,
            )
            return [rebase_failure]
        scope_failure = self._path_scope_result()
        if scope_failure is not None:
            self._event(
                "path_scope_failed",
                {"n": n, "paths": list(self.task.paths or ()), "diagnostic": scope_failure.tail},
                stage=stage,
                session_id=session_id,
            )
            return [scope_failure]
        results = run_verify(
            self.task.verify,
            self.worktree,
            self.run_id,
            self.tail_lines,
            self._step_env(),
            self._command_wrap("verify"),
            self._failure_report(f"verify-{n}"),
        )
        self._note_unconfined("verify", results, session_id=session_id)
        return results

    def _setup_wrap(self, environment: dict[str, str]) -> tuple[dict[str, str], Wrap | None]:
        if (
            not isinstance(self.isolation, ProcessIsolation)
            or self.setup_config.sandbox == SANDBOX_HOST
        ):
            return environment, None
        assert self.worktree is not None
        environment, write, cache = prepare_setup(
            self.task.project, self.rundir.path.parent.parent, self.setup_config, environment
        )
        isolation = self.isolation
        worktree = self.worktree
        return environment, lambda argv: isolation.wrap_setup(
            argv,
            worktree,
            self.rundir.path,
            write,
            self.setup_config.allowed_domains,
            setup_cache=cache,
        )

    def _run_restart_setup(self) -> None:
        """Reuse only a completed, successful setup with the effective command."""
        if not self.setup:
            return
        command: str | None = None
        passed = False
        for event in self.ledger.events(self.run_id, types=("setup_start", "setup_result")):
            if event["type"] == "setup_start":
                command = event["payload"].get("run")
                passed = False
            else:
                passed = event["payload"].get("ok") is True
        if passed and command == self.setup:
            self._event("setup_skipped", {"reason": "passed earlier"})
            return
        self._stage("setup")
        self._run_setup()

    def _run_setup(self) -> None:
        if not self.setup:
            return
        assert self.worktree is not None and self.setup_sandbox is not None
        payload = {
            "run": self.setup,
            "timeout": self.setup_timeout,
            "sandbox": self.setup_sandbox,
        }
        self._event("setup_start", payload)
        if self.setup_sandbox == SANDBOX_HOST:
            self._event(
                "step_unconfined",
                {"kind": "setup", "name": "setup", "note": UNCONFINED_NOTE},
                stage="implement",
            )
        record = self.worktree / f".helmraiser-setup-{self.run_id}.rec"
        record.unlink(missing_ok=True)
        env = {**self._command_env(), SETUP_RECORD_ENV: str(record)}
        env, wrap = self._setup_wrap(env)
        try:
            res = run_shell(
                _setup_script(self.setup),
                self.worktree,
                self.setup_timeout,
                env,
                wrap,
            )
            failed_command = _read_setup_record(record)
        finally:
            record.unlink(missing_ok=True)
        output = res.output
        if res.timed_out:
            output += f"\n[helmraiser] setup timed out after {self.setup_timeout}s"
        tail = tail_lines(output, self.tail_lines)
        ok = res.exit_code == 0
        readonly = (
            setup_readonly_line(res.output)
            if not ok and self.setup_sandbox == "srt" and not res.timed_out
            else None
        )
        self._event(
            "setup_result",
            {
                "ok": ok,
                "exit_code": res.exit_code,
                "seconds": res.seconds,
                "tail": tail,
                "sandbox": self.setup_sandbox,
                **({"hints": [SETUP_READONLY_HINT]} if readonly is not None else {}),
            },
        )
        if not ok:
            last = readonly if readonly is not None else setup_failure_detail(tail)
            if failed_command and not res.timed_out:
                command = _cap(_one_line(failed_command), SETUP_REASON_COMMAND_CHARS)
                reason = f"setup: exit {res.exit_code} (last failed command: {command})"
                if last:
                    reason += f" — {_cap(_one_line(last), SETUP_REASON_DETAIL_CHARS)}"
                raise _RunFailed("failed", _cap(reason, SETUP_REASON_CHARS))
            detail = "timed out" if res.timed_out else f"exit {res.exit_code}"
            if readonly is not None:
                last = _cap(_one_line(last), SETUP_REASON_DETAIL_CHARS)
            raise _RunFailed("failed", _with_detail(f"setup: {detail}", last))

    # ── step 5: plan stage (spec §4.6) ──────────────────────────────────

    async def _plan(self, *, wait_for_approval: bool) -> str | None:
        task = self.task
        assert self.worktree is not None
        self.plan_ran = True
        self._stage("plan")
        model = task.models.get("plan", task.models["implement"])
        session_id = session_id_for(self.run_id, "plan")
        self.rundir.plan_directives_md.write_text(
            render_plan_directives(
                run_id=self.run_id,
                title=task.title,
                worktree=self.worktree,
                task_id=task.id,
                wait_for_approval=wait_for_approval,
                project_notes=self.project_notes,
                paths=task.paths,
            ),
            encoding="utf-8",
        )
        res = await self._spawn(
            plan_prompt(task, wait_for_approval=wait_for_approval),
            session_id,
            model,
            1,
            stage="plan",
            tools=PLAN_TOOLS,
            system_prompt_path=self.rundir.plan_directives_md,
            enforce_budget=wait_for_approval,
            timeout_cap_s=int(self.plan_time_share * task.budget.max_minutes * 60),
        )
        self.ledger.update_run(
            self.run_id, model_plan=model.render(), plan_cost_usd=res.usage.cost_usd
        )
        self._fail_on_broken_contract(res)
        text = res.final_text.strip()
        if wait_for_approval:
            self._raise_for_session_error(res, stage="plan", model=model)
        if text.startswith("BLOCKED:"):
            raise _RunFailed("failed", f"blocked: {blocked_detail(text)}")
        if wait_for_approval:
            if not text:
                raise _RunFailed("failed", "plan: the planning session produced no plan")
        elif res.error_kind is not None or not text:
            reason = (
                _with_detail(res.error_kind, res.error_message)
                if res.error_kind is not None
                else "empty plan"
            )
            self._event(
                "plan_failed",
                {"reason": reason},
                stage="plan",
                session_id=session_id,
            )
            return None
        scope: tuple[list[str], str] | None = None
        if not wait_for_approval and text.startswith("SCOPE:"):
            first_line, _, text = text.partition("\n")
            if task.paths is None:
                # Nothing to approve: the planner only invented a limit. Keep its plan.
                self._event(
                    "scope_line_ignored",
                    {"line": first_line.strip()},
                    stage="plan",
                    session_id=session_id,
                )
            else:
                asked = first_line[len("SCOPE:") :]
                scope = parse_scope_request(asked, task.paths)
                if scope is None:
                    raise _RunFailed("failed", f"blocked: {asked.strip()}")
            text = text.strip()
            if not text:
                raise _RunFailed("failed", "plan: the planning session produced no plan")
        plan_path = self._plan_path()
        plan_path.write_text(text + "\n", encoding="utf-8")
        if wait_for_approval:
            self.pending_approval = "plan"
        self._event(
            "plan_written",
            {"path": str(plan_path), "chars": len(text)},
            stage="plan",
            session_id=session_id,
        )
        if scope is not None:
            self._park_for_scope(
                None, *scope, stage="plan", session_id=session_id, cost=res.usage.cost_usd
            )
        return text

    def _plan_path(self) -> Path:
        return self.rundir.plan_md

    def _read_plan(self) -> str | None:
        path = self._plan_path()
        return path.read_text(encoding="utf-8") if path.is_file() else None

    # ── step 6: implement loop (spec §4.3) ──────────────────────────────

    async def _implement(
        self, plan: str | None = None, *, plan_approved: bool = True, prompt: str | None = None
    ) -> str:
        """The implement loop; ``prompt`` replaces the initial prompt (``retry --implement``)."""
        task = self.task
        assert self.worktree is not None and self.base_commit is not None
        implement_start = self.clock()
        self._stage("implement")
        max_iterations = min(task.budget.max_iterations, self._remaining_rework_iterations())
        if max_iterations == 0:
            self._budget_exceeded("iterations")
        attempt = 1
        model = task.models["implement"]
        fallback = task.models.get("fallback")
        if self.scope_grant is not None and self.escalated and fallback is not None:
            # The fallback asked for the scope: continue after either decision in its session.
            attempt, model = 2, fallback
            max_iterations = min(math.ceil(task.budget.max_iterations / 2), max_iterations)
        run_deadline = self.started_at + task.budget.max_minutes * 60
        first_model_start = implement_start if self.plan_ran else self.started_at
        first_model_budget = (
            run_deadline - implement_start if self.plan_ran else task.budget.max_minutes * 60
        )
        if self.rework_ceiling is not None:
            first_model_start = implement_start
            first_model_budget = self.guard.remaining_seconds()
        first_model_deadline = (
            first_model_start + self.first_model_time_share * first_model_budget
            if fallback is not None and fallback.render() != model.render()
            else None
        )
        if prompt is None:
            prompt = initial_prompt(task, plan, approved=plan_approved)

        while True:
            escalation_reason: str | None = None
            session_id = session_id_for(self.run_id, "implement", attempt)
            self.implement_model = model
            self.implement_session_id = session_id
            self.implement_attempt = attempt
            for local_n in range(1, max_iterations + 1):
                if self._remaining_rework_iterations() == 0:
                    self._budget_exceeded("iterations")
                if (
                    attempt == 1
                    and first_model_deadline is not None
                    and self.clock() >= first_model_deadline
                ):
                    if self.iteration > 0:
                        self._commit_timeout_work(self.iteration)
                    escalation_reason = "timeout"
                    break
                self.iteration += 1
                n = self.iteration
                kind = self.guard.exceeded()
                if kind is not None:
                    self._budget_exceeded(kind)
                self._write_directives(local_n, max_iterations)
                self.ledger.start_iteration(self.run_id, n, session_id, model.render())
                self._event(
                    "iteration_start",
                    {"n": n, "attempt": attempt, "model": model.render(), "prompt": prompt},
                    stage="implement",
                    session_id=session_id,
                )
                head_before, status_before = self._git_state()

                timeout_cap_s = (
                    int(first_model_deadline - self.clock())
                    if attempt == 1 and first_model_deadline is not None
                    else None
                )
                res = await self._spawn(
                    prompt, session_id, model, attempt, timeout_cap_s=timeout_cap_s
                )
                iteration_cost = res.usage.cost_usd
                self._fail_on_broken_contract(res, iteration=n, cost=iteration_cost)
                timeout_commit = (
                    self._commit_timeout_work(n) if res.error_kind == "timeout" else None
                )

                # Preserve timeout edits before enforcing a budget crossed during the
                # session. The budget remains the failure reason, but the branch keeps
                # the model's partial work for inspection.
                budget_kind = self._budget_kind or self.guard.exceeded()
                if budget_kind is not None:
                    self._finish_iteration(
                        n, agent_ok=False, commit_sha=timeout_commit, cost=iteration_cost
                    )
                    self._budget_exceeded(budget_kind)
                if res.error_kind == "quota":
                    self._finish_iteration(n, agent_ok=False, cost=iteration_cost)
                    exhausted_provider = self._hold_provider(model, res.error_message)
                    fallback = task.models.get("fallback")
                    active_holds = self.ledger.provider_holds(self.clock())
                    if (
                        not self.escalated
                        and fallback is not None
                        and provider(fallback.render()) != exhausted_provider
                        and provider(fallback.render()) not in active_holds
                    ):
                        escalation_reason = "quota"
                        break
                    raise _RunFailed("failed", f"quota: {res.error_message or ''}".strip())
                if res.error_kind == "sandbox":
                    self._finish_iteration(n, agent_ok=False, cost=iteration_cost)
                    raise _RunFailed("failed", _with_detail("sandbox", res.error_message))
                if res.error_kind in ("provider", "protocol"):
                    self._finish_iteration(n, agent_ok=False, cost=iteration_cost)
                    prompt = retry_prompt(res.error_message)
                    continue
                if res.error_kind == "timeout":
                    self._finish_iteration(
                        n, agent_ok=False, commit_sha=timeout_commit, cost=iteration_cost
                    )
                    if attempt == 1 and first_model_deadline is not None:
                        escalation_reason = "timeout"
                        break
                    raise _RunFailed("failed", _with_detail("timeout", res.error_message))
                if res.error_kind == "aborted":
                    self._finish_iteration(n, agent_ok=False, cost=iteration_cost)
                    raise _RunFailed("aborted", _with_detail("aborted", res.error_message))
                scope: tuple[list[str], str] | None = None
                if res.final_text.lstrip().startswith("SCOPE:"):
                    asked = res.final_text.strip().splitlines()[0][len("SCOPE:") :]
                    scope = parse_scope_request(asked, task.paths)
                    if scope is None:
                        self._finish_iteration(n, agent_ok=True, cost=iteration_cost)
                        raise _RunFailed("failed", f"blocked: {asked.strip()}")
                    if self._path_scope_result() is None:
                        self._park_for_scope(n, *scope, session_id=session_id, cost=iteration_cost)
                    # With out-of-scope edits too, the path scope check below fails the run.
                blocked: str | None = None
                if res.final_text.lstrip().startswith("BLOCKED:"):
                    blocked = blocked_detail(res.final_text)
                    head_after, status_after = self._git_state()
                    if not (self.service_specs and head_after != head_before and not status_after):
                        self._finish_iteration(n, agent_ok=True, cost=iteration_cost)
                        raise _RunFailed("failed", f"blocked: {blocked}")
                    # Verify steps that need the services cannot run in the session, so
                    # implementers block on them: committed work goes to verify once.
                    self._event(
                        "blocked_to_verify",
                        {"n": n, "reason": blocked},
                        stage="implement",
                        session_id=session_id,
                    )

                # Proof of work (ExecuteHandler semantics): HEAD or status must move.
                head_after, status_after = self._git_state()
                verify_unchanged, self._verify_unchanged_head = self._verify_unchanged_head, False
                if (
                    head_before == head_after
                    and status_before == status_after
                    and self._path_scope_result() is None
                    and not verify_unchanged
                ):
                    self._event("no_changes", {"n": n}, stage="implement", session_id=session_id)
                    self._finish_iteration(n, agent_ok=True, cost=iteration_cost)
                    prompt = NO_CHANGES_PROMPT
                    continue
                if status_after and not self._commit_all(
                    self._commit_message(f"iteration {n} of {task.id}")
                ):
                    self._finish_iteration(n, agent_ok=True, cost=iteration_cost)
                    raise _RunFailed("failed", "commit_failed")
                commit_sha = self._git("rev-parse", "HEAD").stdout.strip()

                self.ledger.update_run(self.run_id, status="verifying")
                results = self._run_verify(session_id, n)
                self.ledger.update_run(self.run_id, status="running")
                passed = all_passed(results)
                self.last_results = results
                self._event(
                    "verify_result",
                    {"n": n, "passed": passed, "results": [r.as_dict() for r in results]},
                    stage="implement",
                    session_id=session_id,
                )
                self._finish_iteration(
                    n,
                    agent_ok=True,
                    commit_sha=commit_sha,
                    verify_passed=passed,
                    results=results,
                    cost=iteration_cost,
                )
                if passed:
                    return commit_sha
                if blocked is not None:
                    raise _RunFailed("failed", f"blocked: {blocked}")
                if scope is not None:
                    failed = ", ".join(r.name for r in results if not r.passed)
                    raise _RunFailed("failed", f"verify_failed: {failed}")
                prompt = feedback_prompt(results)
                if (
                    attempt == 1
                    and first_model_deadline is not None
                    and self.clock() >= first_model_deadline
                ):
                    escalation_reason = "timeout"
                    break

            # Exhausted this model's iterations or time slice.
            if self._remaining_rework_iterations() == 0:
                self._budget_exceeded("iterations")
            fallback = task.models.get("fallback")
            if not self.escalated and fallback is not None:
                self.escalated = True
                attempt = 2
                model = fallback
                max_iterations = min(
                    (
                        math.ceil(task.budget.max_iterations / 2)
                        if self.rework_ceiling is not None
                        else math.ceil(max_iterations / 2)
                    ),
                    self._remaining_rework_iterations(),
                )
                self.ledger.update_run(self.run_id, escalated=1)
                escalation_payload: dict[str, Any] = {
                    "model": model.render(),
                    "max_iterations": max_iterations,
                }
                if escalation_reason is not None:
                    escalation_payload["reason"] = escalation_reason
                self._event("escalated", escalation_payload, stage="implement")
                prompt = escalation_prompt(
                    task, self.ledger.iterations(self.run_id), self.base_commit
                )
                if self.rework_request is not None:
                    # The fallback has a separate session: carry the owner's request
                    # explicitly rather than relying on the primary's conversation.
                    prompt = (
                        rework_prompt(
                            task,
                            self.rework_request["note"],
                            self.rework_request["findings"],
                            rebase_onto=self.rebase_onto,
                            published_pr=bool(self.pr_url),
                        )
                        + "\n\n"
                        + prompt
                    )
                continue
            raise _RunFailed("failed", "verify_exhausted")

    def _park_for_scope(
        self,
        n: int | None,
        paths: list[str],
        reason: str,
        *,
        session_id: str,
        cost: float,
        stage: str = "implement",
    ) -> NoReturn:
        """Park a plan or implement request, committing work only for an iteration."""
        if n is not None:
            if self._git_state()[1] and not self._commit_all(
                self._commit_message(f"iteration {n} of {self.task.id}")
            ):
                self._finish_iteration(n, agent_ok=True, cost=cost)
                raise _RunFailed("failed", "commit_failed")
            self.head_commit = self._git("rev-parse", "HEAD").stdout.strip()
            self._finish_iteration(n, agent_ok=True, commit_sha=self.head_commit, cost=cost)
        self.scope_request = {"paths": paths, "reason": reason}
        self.pending_approval = "scope"
        self._event(
            "scope_requested",
            {"n": n, **self.scope_request} if n is not None else self.scope_request,
            stage=stage,
            session_id=session_id,
        )
        raise _ScopeRequested()

    def _remaining_rework_iterations(self) -> int:
        return (
            max(0, self.rework_ceiling - self.iteration)
            if self.rework_ceiling is not None
            else self.task.budget.max_iterations
        )

    async def _iterate_review_fixes(self, prompt: str, max_iterations: int) -> str | None:
        """Iterate the current implement session until review fixes verify."""
        assert self.worktree is not None
        model = self.implement_model
        session_id = self.implement_session_id
        max_iterations = min(max_iterations, self._remaining_rework_iterations())
        for local_n in range(1, max_iterations + 1):
            if self._remaining_rework_iterations() == 0:
                self._budget_exceeded("iterations")
            self.iteration += 1
            n = self.iteration
            kind = self.guard.exceeded()
            if kind is not None:
                self._budget_exceeded(kind)
            self._write_directives(local_n, max_iterations)
            self.ledger.start_iteration(self.run_id, n, session_id, model.render())
            self._event(
                "iteration_start",
                {
                    "n": n,
                    "attempt": self.implement_attempt,
                    "model": model.render(),
                    "prompt": prompt,
                },
                stage="implement",
                session_id=session_id,
            )
            head_before, status_before = self._git_state()
            res = await self._spawn(prompt, session_id, model, self.implement_attempt)
            cost = res.usage.cost_usd
            self._fail_on_broken_contract(res, iteration=n, cost=cost)
            budget_kind = self._budget_kind or self.guard.exceeded()
            if budget_kind is not None:
                self._finish_iteration(n, agent_ok=False, cost=cost)
                self._budget_exceeded(budget_kind)
            if res.error_kind is not None:
                self._finish_iteration(n, agent_ok=False, cost=cost)
                if res.error_kind in ("provider", "protocol"):
                    prompt = retry_prompt(res.error_message)
                    continue
                if res.error_kind == "aborted":
                    raise _RunFailed("aborted", _with_detail("aborted", res.error_message))
                if res.error_kind == "quota":
                    self._hold_provider(model, res.error_message)
                raise _RoundAbandoned(_with_detail(res.error_kind, res.error_message))
            head_after, status_after = self._git_state()
            if (
                head_before == head_after
                and status_before == status_after
                and self._path_scope_result() is None
            ):
                self._finish_iteration(n, agent_ok=True, cost=cost)
                prompt = NO_CHANGES_PROMPT
                continue
            if status_after and not self._commit_all(
                self._commit_message(f"review feedback {self.review_rounds} of {self.task.id}")
            ):
                self._finish_iteration(n, agent_ok=True, cost=cost)
                raise _RunFailed("failed", "commit_failed")
            commit = self._git("rev-parse", "HEAD").stdout.strip()
            self.ledger.update_run(self.run_id, status="verifying")
            results = self._run_verify(session_id, n)
            self.ledger.update_run(self.run_id, status="running")
            passed = all_passed(results)
            self.last_results = results
            self._event(
                "verify_result",
                {"n": n, "passed": passed, "results": [r.as_dict() for r in results]},
                stage="implement",
                session_id=session_id,
            )
            self._finish_iteration(
                n,
                agent_ok=True,
                commit_sha=commit,
                verify_passed=passed,
                results=results,
                cost=cost,
            )
            if passed:
                return commit
            prompt = feedback_prompt(results)
        return None

    async def _spawn(
        self,
        prompt: str,
        session_id: str,
        model: ModelSpec,
        attempt: int,
        *,
        stage: str = "implement",
        tools: tuple[str, ...] | None = None,
        context_files: bool = True,
        system_prompt_path: Path | None = None,
        enforce_budget: bool = True,
        timeout_cap_s: int | None = None,
        timeout_floor_s: int | None = None,
    ) -> PiSessionResult:
        """One pi turn in ``session_id``; provider/protocol errors retry once."""
        assert self.worktree is not None
        spawn_started_at = self.clock()

        def session_timeout() -> int:
            timeout_s = max(60, self.guard.remaining_seconds())
            if timeout_cap_s is not None:
                elapsed = int(self.clock() - spawn_started_at)
                timeout_s = max(60, min(timeout_s, timeout_cap_s - elapsed))
            if timeout_floor_s is not None:
                timeout_s = max(timeout_s, timeout_floor_s)
            return timeout_s

        req = PiRequest(
            prompt=prompt,
            session_id=session_id,
            model=model,
            cwd=self.worktree,
            session_dir=self.rundir.sessions,
            system_prompt_path=system_prompt_path or self.rundir.directives_md,
            tools=tools,
            context_files=context_files,
            timeout_s=session_timeout(),
            env=self._session_env(),
            stage=stage,
            attempt=attempt,
            rpc=stage == "implement"
            and (
                self.arm.pi_rpc_implement
                if self.arm is not None and self.arm.pi_rpc_implement is not None
                else bool(self.config.get("pi_rpc_implement", False))
            ),
            extra_args=stage_skill_args(
                stage,
                self.worktree,
                skills=(
                    self.arm.skills
                    if self.arm is not None and self.arm.skills is not None
                    else "default"
                ),
            ),
        )
        res = await self._run_session(req, enforce_budget)
        if res.error_kind in ("provider", "protocol") and self._budget_kind is None:
            self._event(
                "retry",
                {"error_kind": res.error_kind, "error": res.error_message},
                stage=stage,
                session_id=session_id,
            )
            await self.sleep(RETRY_DELAY_SECONDS)
            first = res.usage
            req = replace(req, timeout_s=session_timeout())
            res = await self._run_session(req, enforce_budget)
            res.usage = Usage(
                first.tokens_in + res.usage.tokens_in,
                first.tokens_out + res.usage.tokens_out,
                first.cost_usd + res.usage.cost_usd,
            )
        return res

    async def _run_session(self, req: PiRequest, enforce_budget: bool = True) -> PiSessionResult:
        self._budget_kind = None
        self._stream_cost = 0.0
        stream_usage = Usage()
        self.guard.set_streaming(0.0)

        async def on_event(event: PiEvent) -> None:
            nonlocal stream_usage
            payload = ledger_payload(event)
            if payload is not None:
                self._event(
                    event.type, payload, stage=req.stage, session_id=req.session_id, source="pi"
                )
            if is_assistant_message_end(event):
                message_usage = usage_of(event.raw["message"].get("usage"))
                stream_usage = Usage(
                    stream_usage.tokens_in + message_usage.tokens_in,
                    stream_usage.tokens_out + message_usage.tokens_out,
                    stream_usage.cost_usd + message_usage.cost_usd,
                )
                self._stream_cost = stream_usage.cost_usd
                self.ledger.update_run(
                    self.run_id,
                    cost_usd=self.usage.cost_usd + stream_usage.cost_usd,
                    tokens_in=self.usage.tokens_in + stream_usage.tokens_in,
                    tokens_out=self.usage.tokens_out + stream_usage.tokens_out,
                )
                self.guard.set_streaming(self._stream_cost)
                kind = self.guard.exceeded()
                if enforce_budget and kind is not None and self._budget_kind is None:
                    self._budget_kind = kind
                    self._event("budget_exceeded", {"kind": kind, "during": "session"})
                    self._terminate_current()

        res = await self.adapter.run(req, on_event, self.run_id)
        if res.error_kind in ("provider", "protocol", "sandbox") and self._abort_requested():
            # srt exits 0 when abort's SIGTERM reaches it, so under process isolation
            # an aborted session looks cut short rather than killed.
            res.error_kind = "aborted"
        self.guard.settle(res.usage.cost_usd)
        self.usage = Usage(
            self.usage.tokens_in + res.usage.tokens_in,
            self.usage.tokens_out + res.usage.tokens_out,
            self.usage.cost_usd + res.usage.cost_usd,
        )
        self.ledger.update_run(
            self.run_id,
            cost_usd=self.usage.cost_usd,
            tokens_in=self.usage.tokens_in,
            tokens_out=self.usage.tokens_out,
        )
        return res

    def _raise_for_session_error(
        self, res: PiSessionResult, *, stage: str, model: ModelSpec
    ) -> None:
        """A quota error puts its provider on hold; other errors fail the run (non-iterating stages)."""
        budget_kind = self._budget_kind or self.guard.exceeded()
        if budget_kind is not None:
            self._budget_exceeded(budget_kind)
        if res.error_kind == "quota":
            self._hold_provider(model, res.error_message)
            raise _RunFailed("failed", f"quota: {res.error_message or ''}".strip())
        if res.error_kind == "aborted":
            raise _RunFailed("aborted", _with_detail("aborted", res.error_message))
        if res.error_kind is not None:
            raise _RunFailed(
                "failed", _with_detail(f"{stage}: {res.error_kind}", res.error_message)
            )

    def _abort_requested(self) -> bool:
        """``helmraiser abort`` (or a budget stop) marked this run in the project state.db."""
        row = self.state.get_run(self.run_id)
        return row is not None and row["status"] in ("aborting", "aborted")

    def _terminate_current(self) -> None:
        """Stop the running pi process through its lease (the abort path)."""
        request = self.state.request_abort(self.run_id)
        if request is None or request.lease is None:
            return
        lease = request.lease
        self.process_controller.terminate(
            ProcessIdentity(lease.leader_pid, lease.pgid, lease.process_start_token)
        )

    def _budget_exceeded(self, kind: str) -> None:
        self._event(
            "budget_exceeded",
            {
                "kind": kind,
                "cost_usd": self.guard.cost_usd,
                "minutes": self.guard.minutes,
                **(self._iteration_limit() if kind == "iterations" else {}),
            },
        )
        raise _RunFailed("failed", f"budget:{kind}")

    def _iteration_limit(self) -> dict[str, Any]:
        """The limit a ``budget:iterations`` stop hit: a rework's allowance is usually below
        ``max_iterations``, which also holds, from the run's start, after a budget retry."""
        return {
            "iterations": self.iteration,
            "limit": (
                self.rework_ceiling
                if self.rework_ceiling is not None
                else self.task.budget.max_iterations
            ),
            "limit_source": (
                "rework_allowance" if self.rework_request is not None else "max_iterations"
            ),
        }

    def _fail_on_broken_contract(
        self, res: PiSessionResult, *, iteration: int | None = None, cost: float = 0.0
    ) -> None:
        """A pi without readable usage defeats budget caps for every run: fail this run
        at once (any stage) and pause the whole queue until Helmraiser is fixed."""
        if res.error_kind != "contract":
            return
        if iteration is not None:
            self._finish_iteration(iteration, agent_ok=False, cost=cost)
        reason = (
            f"pi {self.pi_version} broke the JSON usage contract (run {self.run_id[:8]}); "
            "fix Helmraiser, then run helmraiser queue resume"
        )
        self.ledger.pause_queue(reason)
        self.paused = self.ledger.is_paused()
        self._event("queue_paused", {"reason": reason, "error": res.error_message})
        raise _RunFailed("failed", _with_detail("pi_contract", res.error_message))

    def _hold_provider(self, model: ModelSpec, detail: str | None) -> str:
        name = provider(model.render())
        until = self.clock() + PAUSE_SECONDS
        self.ledger.hold_provider(name, until, detail)
        self._event(
            "provider_held",
            {"provider": name, "until": until, "detail": detail},
        )
        return name

    def _finish_iteration(
        self,
        n: int,
        *,
        agent_ok: bool,
        commit_sha: str | None = None,
        verify_passed: bool | None = None,
        results: list[VerifyResult] | None = None,
        cost: float = 0.0,
    ) -> None:
        self.ledger.finish_iteration(
            self.run_id,
            n,
            agent_ok=agent_ok,
            commit_sha=commit_sha,
            verify_passed=verify_passed,
            verify_results=[r.as_dict() for r in results or []],
            cost_usd=cost,
        )

    # ── step 7: evidence (spec §4.5.1) ──────────────────────────────────

    def run_evidence(self) -> list[EvidenceResult]:
        assert self.worktree is not None and self.base_commit is not None
        diff = self._git("diff", f"{self.base_commit}..HEAD")
        self.rundir.diff_patch.write_text(diff.stdout, encoding="utf-8")
        results = run_evidence(
            self.task.evidence,
            self.worktree,
            self.run_id,
            self.rundir.evidence,
            self.tail_lines,
            self._step_env(),
            self._command_wrap("evidence"),
            self._failure_report("evidence"),
        )
        self._note_unconfined("evidence", results)
        for result in results:
            self.ledger.record_evidence(
                self.run_id,
                result.name,
                ok=result.ok,
                exit_code=result.exit_code,
                artifacts=result.artifacts,
                tail=result.tail,
            )
            self._event("evidence_result", result.as_dict(), stage="implement")
        self.evidence_results = results
        return results

    # ── step 8: review (spec §4.5.2) ────────────────────────────────────

    async def run_review(self, plan: str | None = None, *, plan_approved: bool = True) -> str:
        task = self.task
        assert self.worktree is not None
        self._stage("review")
        self.ledger.update_run(self.run_id, status="reviewing")
        model = task.models["review"]
        self.review_count += 1
        if self.review_count > 1 and self.rundir.review_md.is_file():
            self.rundir.review_round_md(self.review_count - 1).write_text(
                self.rundir.review_md.read_text(encoding="utf-8"), encoding="utf-8"
            )
        session_id = session_id_for(self.run_id, "review", self.review_count)
        self.rundir.review_directives_md.write_text(
            render_review_directives(
                run_id=self.run_id,
                title=task.title,
                worktree=self.worktree,
                project_notes=self.project_notes,
                paths=task.paths,
            ),
            encoding="utf-8",
        )
        diff_text = (
            self.rundir.diff_patch.read_text(encoding="utf-8")
            if self.rundir.diff_patch.exists()
            else ""
        )
        plan_options: dict[str, Any] = {"plan": plan}
        if plan is not None:
            plan_options["plan_approved"] = plan_approved
        prompt = build_review_prompt(
            task,
            diff_path=self.rundir.diff_patch,
            diff_text=diff_text,
            verify_results=self.last_results,
            evidence_results=self.evidence_results,
            evidence_dir=self.rundir.evidence,
            plan_approval_note=self._plan_approval_note(),
            scope_decisions=self._scope_decisions(),
            rework_notes=[str(r["note"]) for r in self.ledger.reworks(self.run_id)],
            **plan_options,
        )
        res = await self._spawn(
            prompt,
            session_id,
            model,
            1,
            stage="review",
            tools=REVIEW_TOOLS,
            context_files=False,
            system_prompt_path=self.rundir.review_directives_md,
            enforce_budget=False,
            timeout_floor_s=self.review_min_minutes * 60,
        )
        self._fail_on_broken_contract(res)
        if res.error_kind == "quota":
            self._hold_provider(model, res.error_message)
            raise _RunFailed("failed", f"quota: {res.error_message or ''}".strip())
        if res.error_kind == "aborted":
            raise _RunFailed("aborted", _with_detail("aborted", res.error_message))

        message = res.final_text
        if res.error_kind is not None:
            message += f"\n\n(review session failed: {res.error_kind}: {res.error_message or ''})"
        verdict = parse_verdict(res.final_text)
        if res.error_kind is not None:
            verdict.verdict, verdict.forced = (
                "uncertain",
                f"review session failed: {res.error_kind}",
            )
        if verdict.parsed and task.evidence:
            unassessed = next(
                (
                    check
                    for check in verdict.evidence_checks
                    if check.get("meets_criteria") is not True
                    and check.get("meets_criteria") is not False
                ),
                None,
            )
            unmet = next(
                (
                    check
                    for check in verdict.evidence_checks
                    if check.get("meets_criteria") is False
                ),
                None,
            )
            if not verdict.evidence_checks and verdict.verdict in ("pass", "concerns"):
                verdict.verdict, verdict.forced = "uncertain", "no evidence checks"
            elif unassessed is not None and verdict.verdict in ("pass", "concerns"):
                name = str(unassessed.get("name") or "unnamed")
                verdict.verdict, verdict.forced = (
                    "uncertain",
                    f"evidence check {name} not assessed",
                )
            elif unmet is not None and verdict.verdict == "pass":
                name = str(unmet.get("name") or "unnamed")
                verdict.verdict, verdict.forced = (
                    "concerns",
                    f"evidence check {name} not met",
                )
        failed_evidence = [r.name for r in self.evidence_results if not r.ok]
        if failed_evidence:
            verdict.forced = f"evidence not ok: {', '.join(failed_evidence)}"
            verdict.verdict = "uncertain"
        self.rundir.review_md.write_text(message, encoding="utf-8")
        self.verdict = verdict
        self.ledger.update_run(self.run_id, verdict=verdict.verdict, status="running")
        self._event("review_verdict", verdict.as_dict(), stage="review", session_id=session_id)
        return verdict.verdict

    # ── step 9: merge (spec §4.5.3) ─────────────────────────────────────

    def decide_merge(self, verdict: str | None) -> MergeDecision:
        # Abort is otherwise noticed only at a pi spawn or session end; one that lands
        # after the review returned must still stop an ``auto`` run from landing.
        if self._abort_requested():
            raise _RunFailed("aborted", "aborted: before the merge decision")
        self._stage("merge")

        def finalize() -> None:
            if self.task.delivery == "merge":
                self.worktrees.finalize_local(self.task.id, self.run_id)
                return
            body = pr_body(
                self.task,
                self.run_id,
                self.verdict,
                self.last_results,
                self.evidence_results,
                self.usage.cost_usd,
            )
            metadata = self.worktrees.finalize_remote(
                self.task.id,
                self.run_id,
                deliver=lambda base, head: open_pull_request(
                    self.task.project, base=base, head=head, title=self.task.title, body=body
                ),
            )
            self.pr_url = metadata["pr_url"]
            self.ledger.update_run(self.run_id, pr_url=self.pr_url)
            self._event("delivered", {"pr_url": self.pr_url}, stage="merge")

        # One arm of a pair never lands by itself, whatever the policy: it waits for
        # the pick.
        parked = bool(self.reworks) or self.ledger.pair_for_run(self.run_id) is not None
        decision = decide("review" if parked else self.task.policy, verdict, finalize)
        if decision.status != "landed":
            self.pending_approval = "merge"
        self._event(
            "merge_decision",
            {
                "action": decision.action,
                "status": decision.status,
                "verdict": verdict,
                "failure_reason": decision.failure_reason,
            },
            stage="merge",
        )
        return decision

    # ── step 10: close ──────────────────────────────────────────────────

    def _close(self, status: str, reason: str | None) -> RunOutcome:
        self._stop_services()
        minutes = (self.clock() - self.started_at) / 60.0
        self.ledger.update_run(self.run_id, head_commit=self.head_commit, stage=None)
        self.ledger.finish_run(self.run_id, status, reason)
        self.ledger.set_task_status(self.task.task_id, status, run_id=self.run_id)
        if (
            status == "awaiting_approval"
            and self.pending_approval in ("plan", "merge", "scope")
            and self.ledger.pending_approval(self.run_id) is None
        ):
            self.ledger.record_approval(self.run_id, self.pending_approval, "pending")
        try:
            self.state.update_run_status(self.run_id, STATE_STATUS.get(status, "failed"))
        finally:
            if self.locked:
                self.state.release_lock(self.run_id, self.lock_topic)
        self._event(
            "run_end",
            {"status": status, "failure_reason": reason, "active_minutes": minutes},
        )
        auto_pick(self.ledger, self.run_id)
        outcome = RunOutcome(
            run_id=self.run_id,
            status=status,
            failure_reason=reason,
            task_id=self.task.task_id,
            iterations=self.iteration,
            escalated=self.escalated,
            review_rounds=self.review_rounds,
            cost_usd=round(self.usage.cost_usd, 6),
            minutes=round(self.guard.minutes, 2),
            branch_name=self.branch_name,
            worktree_path=str(self.worktree) if self.worktree else None,
            base_commit=self.base_commit,
            head_commit=self.head_commit,
            paused=self.paused,
            verify_results=[r.as_dict() for r in self.last_results],
            evidence_results=[r.as_dict() for r in self.evidence_results],
            verdict=self.verdict.as_dict() if self.verdict else None,
            pending_approval=self.pending_approval if status == "awaiting_approval" else None,
            pr_url=self.pr_url,
            warnings=list(self.warnings),
            services=[service.as_dict() for service in self.services],
            setup_sandbox=self.setup_sandbox,
            scope_request=self.scope_request if status == "awaiting_approval" else None,
        )
        self._write_summary(outcome)
        write_status(self.ledger, self.data_dir)
        return outcome

    def _write_summary(self, outcome: RunOutcome) -> None:
        lines = [
            f"## {self.task.title} — {outcome.status}",
            "",
            f"- task: `{self.task.task_id}`",
            f"- run: `{outcome.run_id}`",
            f"- branch: `{outcome.branch_name or '-'}`",
            *([f"- base: `{self.task.base}`"] if self.task.base else []),
            *([f"- pr: {outcome.pr_url}"] if outcome.pr_url else []),
            f"- worktree: `{outcome.worktree_path or '-'}`",
            f"- iterations: {outcome.iterations}"
            + (" (escalated to fallback)" if outcome.escalated else ""),
            *([f"- review rounds: {outcome.review_rounds}"] if outcome.review_rounds else []),
            f"- cost: ${outcome.cost_usd:.4f}, {outcome.minutes:.1f} min",
        ]
        lines += render_reworks(self.ledger.reworks(self.run_id))
        scope_failures: list[dict[str, Any]] = []
        setup_failure: dict[str, Any] | None = None
        for event in self.ledger.events(
            self.run_id, types=("path_scope_failed", "setup_result", "routing")
        ):
            payload = event["payload"]
            if event["type"] == "path_scope_failed":
                scope_failures.append(payload)
            if event["type"] == "setup_result" and not payload.get("ok"):
                setup_failure = payload
            if (
                event["type"] == "routing"
                and payload.get("lever") in ("plan", "implement")
                and "from" in payload
                and "to" in payload
            ):
                lines.append(f"- routing: {format_dispatch_decision(payload)}")
        if outcome.failure_reason:
            lines.append(f"- failure: {outcome.failure_reason}")
        if outcome.setup_sandbox == SANDBOX_HOST:
            lines.append(f"- setup: {UNCONFINED_NOTE}")
        if outcome.status == "failed":
            lines += [f"- environment hint: {hint}" for hint in failure_hints(outcome)]
        lines += [
            f"- service {s['name']}: `{s['image']}` on 127.0.0.1:{s['host_port']}"
            f" (container port {s['port']}; provides {', '.join(s['provides']) or '-'})"
            for s in outcome.services
        ]
        lines += [f"- warning: {warning}" for warning in outcome.warnings]
        if outcome.status == "awaiting_approval":
            what = APPROVAL_SUBJECTS.get(outcome.pending_approval or "merge", "the merge")
            if outcome.scope_request:
                lines += render_scope_request(outcome.run_id, outcome.scope_request)
            lines.append(f"- next: `helmraiser approve {outcome.run_id}` (or reject) — {what}")
            if outcome.pending_approval == "merge":
                lines.append(f'- or: `helmraiser rework {outcome.run_id} --note "<feedback>"`')
        if setup_failure:
            lines += ["", "### Setup failure", "", "```", setup_failure.get("tail", ""), "```"]
            lines += [f"- hint: {hint}" for hint in setup_failure.get("hints", [])]
        if outcome.verify_results:
            lines += ["", "### Last verify", ""]
            for r in outcome.verify_results:
                mark = "pass" if r["passed"] else f"FAIL (exit {r['exit_code']})"
                note = f"; {UNCONFINED_NOTE}" if r.get("sandbox") == SANDBOX_HOST else ""
                lines.append(f"- {r['name']}: {mark} in {r['seconds']}s{note}")
                lines += [f"  - hint: {hint}" for hint in r.get("hints") or []]
        if outcome.evidence_results:
            lines += ["", "### Evidence", ""]
            for e in outcome.evidence_results:
                mark = (
                    "ok" if e["ok"] else f"NOT OK (exit {e['exit_code']}, missing {e['missing']})"
                )
                note = f"; {UNCONFINED_NOTE}" if e.get("sandbox") == SANDBOX_HOST else ""
                lines.append(f"- {e['name']}: {mark}; artifacts {e['artifacts']}{note}")
                lines += [f"  - hint: {hint}" for hint in e.get("hints") or []]
        if outcome.verdict:
            v = outcome.verdict
            lines += ["", f"### Review: {v['verdict']}", ""]
            if v.get("forced"):
                lines.append(f"- forced: {v['forced']}")
            if v.get("error"):
                lines.append(f"- parse: {v['error']}")
            for f in v.get("findings") or []:
                where = f"{f.get('file', '?')}:{f.get('line', 0)}"
                lines.append(f"- [{f.get('severity', '?')}] {where} — {f.get('note', '')}")
            for c in v.get("evidence_checks") or []:
                lines.append(
                    f"- evidence {c.get('name', '?')}: meets_criteria={c.get('meets_criteria')}"
                    + (f" — {c['note']}" if c.get("note") else "")
                )
            if not v.get("findings") and not v.get("evidence_checks"):
                lines.append("- no findings")
        lines += render_path_scope_failures(scope_failures)
        try:
            self.rundir.path.mkdir(parents=True, exist_ok=True)
            self.rundir.summary_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    # ── helpers ─────────────────────────────────────────────────────────

    def _stage(self, name: str) -> None:
        self.ledger.update_run(self.run_id, stage=name, status="running")
        self._event("stage_start", {"stage": name}, stage=name)
        write_status(self.ledger, self.data_dir)

    def _event(
        self,
        type: str,
        payload: dict[str, Any],
        stage: str | None = None,
        session_id: str | None = None,
        source: str = "helmraiser",
    ) -> None:
        self.ledger.append_event(
            self.run_id, source, type, payload, stage=stage, session_id=session_id
        )
        self.event_logger.log(
            type, agent_id=session_id, source=source, stage=stage, payload=payload
        )

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        assert self.worktree is not None
        # Without the worker's code snapshot: the project's git hooks run its own code.
        return subprocess.run(
            ["git", *args],
            cwd=self.worktree,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env=without_snapshot(os.environ),
        )

    def _git_state(self) -> tuple[str, str]:
        head = self._git("rev-parse", "HEAD").stdout.strip()
        status = self._git("status", "--porcelain").stdout.strip()
        return head, status

    def _commit_timeout_work(self, iteration: int) -> str | None:
        """Commit edits left by a timed-out implement session for inspection or fallback."""
        _, status = self._git_state()
        if not status:
            return None
        message = self._commit_message(f"partial work of iteration {iteration} (timed out)")
        if not self._commit_all(message):
            raise _RunFailed("failed", "commit_failed")
        return self._git("rev-parse", "HEAD").stdout.strip()

    def _commit_retry_work(self) -> None:
        """Commit what the failed run left uncommitted: the branch is what gets reviewed."""
        if self._git_state()[1] and not self._commit_all(
            self._commit_message(f"uncommitted work of {self.task.id} (retry)")
        ):
            raise _RunFailed("failed", "commit_failed")

    def _commit_message(self, text: str) -> str:
        """``chore(helmraiser): text`` when the base history is Conventional Commits, else
        ``helmraiser: text``."""
        if self.conventional_commits is None:
            assert self.worktree is not None
            self.conventional_commits = conventional_history(
                self.worktree, self.base_commit or "HEAD"
            )
        return f"chore(helmraiser): {text}" if self.conventional_commits else f"helmraiser: {text}"

    def _commit_all(self, message: str) -> bool:
        if self.rebase_onto is not None and (
            self._rebase_in_progress() or (self.pr_url and self._merge_in_progress())
        ):
            return True
        assert self.worktree is not None
        cleanup_failed = False
        try:
            names = remove_placeholders(self.worktree)
        except PlaceholderCleanupError as exc:
            names = exc.removed_names
            cleanup_failed = True
        except (OSError, subprocess.SubprocessError):
            return False
        if names:
            self._event("sandbox_placeholders_removed", {"cwd": str(self.worktree), "names": names})
        if cleanup_failed:
            return False
        if self._git("add", "-A").returncode != 0:
            return False
        if names:
            staged = self._git("diff", "--cached", "--quiet")
            if staged.returncode == 0:
                return True
            if staged.returncode != 1:
                return False
        return self._git("commit", "-q", "-m", message).returncode == 0
