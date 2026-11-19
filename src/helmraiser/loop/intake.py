"""Intake prompts, proposal parsing, acceptance merging, and validation."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from helmraiser.config import CONFIG_FILE
from helmraiser.isolation import ProcessIsolation
from helmraiser.loop.preflight import (
    PreflightError,
    failed_scout_preflight,
    normalize_scout_preflight,
    run_preflight_steps,
)
from helmraiser.pi.adapter import PiJsonAdapter
from helmraiser.pi.protocol import PiAdapter, PiEvent, PiRequest
from helmraiser.project_notes import project_notes_for_body, project_notes_section
from helmraiser.task.loader import TaskLoadError, parse_task, profile_models, spec_to_frontmatter
from helmraiser.task.model import ISOLATIONS, ModelSpec, TaskSpec, VerifyStep
from helmraiser.task.project import load_project_defaults
from helmraiser.task.schema import validate_task
from helmraiser.task.structural import (
    body_outside_paths_warnings,
    outside_paths_warnings,
    related_tests_warnings,
    structural_reason,
)
from helmraiser.utils.markdown import serialize_frontmatter

PROPOSAL_KEYS: tuple[str, ...] = (
    "verify",
    "evidence",
    "policy",
    "plan",
    "isolation",
    "budget",
    "models",
)
INTAKE_TOOLS: tuple[str, ...] = ("read", "grep", "find", "ls")

_FENCE = re.compile(
    r"^[ \t]*```(?:yaml|yml)[ \t]*\r?\n(.*?)^[ \t]*```[ \t]*\r?$",
    re.DOTALL | re.MULTILINE,
)


class IntakeSandboxError(ValueError):
    """The intake session cannot be sandboxed and the config does not allow otherwise."""


def intake_wrapper(config: dict[str, Any]) -> Callable[[list[str], Path, Path], list[str]]:
    """Wrap the intake pi session in process isolation (read rules as for runs, writes
    only to its session dir). Without srt it refuses, unless the config sets
    ``isolation.intake_unsandboxed: true``."""
    isolation = ProcessIsolation(config, readonly=True)
    unsandboxed = (config.get("isolation") or {}).get("intake_unsandboxed") is True

    def wrap(argv: list[str], cwd: Path, run_dir: Path) -> list[str]:
        reason = isolation.check_available()
        if reason is not None:
            if unsandboxed:
                return list(argv)
            raise IntakeSandboxError(
                f"intake runs pi in process isolation, which is unavailable: {reason}. "
                f"Install srt, or set isolation.intake_unsandboxed: true in {CONFIG_FILE}"
            )
        isolation.set_project_deny_read(load_project_defaults(cwd).deny_read, cwd)
        return isolation.wrap(argv, cwd, run_dir)

    return wrap


def get_intake_adapter(config: dict[str, Any]) -> PiAdapter | None:
    """The pi adapter used by ``intake`` (tests monkeypatch this)."""
    return PiJsonAdapter(
        pi_bin=str(config.get("pi_bin", "pi")),
        argv_wrapper=intake_wrapper(config),
    )


def build_intake_prompt(spec: TaskSpec, config: dict[str, Any]) -> str:
    profiles = profile_models(spec.project, config)
    profiles_prompt = "\n".join(
        f"- {role}: {profiles[role].render()}"
        for role in ("implement", "fallback", "review", "plan")
        if role in profiles
    )
    owner_tail = (
        "`origin`, and `policy` when the file sets it"
        if spec.policy is not None
        else "and `origin`"
    )
    current = {
        key: value
        for key, value in spec_to_frontmatter(spec).items()
        if key in PROPOSAL_KEYS and (value or key == "plan")
    }
    current_yaml = yaml.safe_dump(current, sort_keys=False) if current else "(none)\n"
    notes = project_notes_for_body(spec.project, spec.body)
    notes_prompt = (
        "## Owner-maintained project notes\n\n"
        "These are constraints on proposed verify and evidence commands:\n\n"
        f"{project_notes_section(notes)}\n\n"
        if notes is not None
        else ""
    )
    avoid_models = (config.get("intake") or {}).get("avoid_models", [])
    avoid_prompt = (
        f"Never propose these models: {', '.join(avoid_models)}.\n" if avoid_models else ""
    )
    return (
        f'You are preparing the acceptance block for Helmraiser task "{spec.title}" '
        f"(id {spec.id}) in the repository at {spec.project}.\n"
        "Read the repository as needed; never modify files.\n\n"
        + (
            "## Owner path constraints\n\n"
            + "\n".join(f"- `{pattern}`" for pattern in spec.paths)
            + "\n\nPreserve these declared patterns unchanged.\n\n"
            if spec.paths is not None
            else ""
        )
        + "## Task\n\n"
        f"{spec.body.strip()}\n\n"
        "## Current acceptance block\n\n"
        f"```yaml\n{current_yaml}```\n\n"
        f"{notes_prompt}"
        "## Configured model profiles\n\n"
        f"{profiles_prompt}\n\n"
        "## Answer format\n\n"
        "Answer with a fenced ```yaml block as the LAST thing in your message with keys\n"
        "`verify`, `evidence`, `policy`, `plan`, `isolation`, `budget`, `models`.\n"
        "- verify: list of {name, run, timeout}; deterministic commands run from the repo\n"
        "  root that exit 0 only when the task is done.\n"
        "- evidence: list of {name, run, artifacts, criteria}; commands write files into\n"
        "  $HELMRAISER_EVIDENCE_DIR and criteria say what a reviewer must see in them.\n"
        "- verify and evidence run the project's tests and commands and show behaviour.\n"
        "  They never read, grep, parse or assert on source files' text, and never\n"
        "  check that a file or an import exists; reading the code and its\n"
        "  documentation is the reviewer's job. Never judge how the work was\n"
        "  committed: no commit counts or order, no HEAD~N diffs.\n"
        "- A check for skipped or todo tests must match only test lines: TAP's\n"
        "  summary always prints `# skipped 0` and `# todo 0`, so a bare\n"
        "  /#\\s*(SKIP|TODO)/i fails every run.\n"
        "- Never set `sandbox` on a verify or evidence step: they run inside the sandbox.\n"
        "- policy: review or approve-plan-first (never auto).\n"
        "- isolation: process (the default sandbox). Propose vm only when the task needs\n"
        "  it; never propose none. budget: {max_cost_usd, max_minutes, max_iterations}.\n"
        "- models: implement, fallback, review, plan as provider/id[:thinking]; review must\n"
        "  differ from implement and never be weaker. Propose only deliberate\n"
        "  deviations from configured profiles; omit `models:` when no deviation is needed.\n"
        "  do not copy resolved defaults. Preserve existing explicit model choices.\n"
        "- Preserve an existing explicit plan boolean; suggest one only when unspecified.\n"
        "  Set `plan: true` when the task is loosely specified and a cheap implementer\n"
        "  is used; leave it out when the task already names files, steps and tests.\n"
        f"- `base`, `delivery`, `priority`, `depends_on`, `paths`, {owner_tail} are owner-controlled,\n"
        "  not proposal fields. Preserve the file's explicit policy and declared paths.\n"
        f"{avoid_prompt}"
    )


def parse_proposal(text: str) -> dict[str, Any]:
    """The last fenced YAML block of the model's answer, as a dict."""
    blocks = _FENCE.findall(text)
    if not blocks:
        raise ValueError("intake answer contains no fenced yaml block")
    try:
        data = yaml.safe_load(blocks[-1])
    except yaml.YAMLError as exc:
        raise ValueError(f"intake proposal is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("intake proposal must be a YAML mapping")
    return {k: v for k, v in data.items() if k in PROPOSAL_KEYS}


def _strip_step_sandbox(proposal: dict[str, Any]) -> list[str]:
    """Drop ``sandbox`` from every proposed step: only the owner opts a step out of srt."""
    notes: list[str] = []
    for kind in ("verify", "evidence"):
        steps = proposal.get(kind)
        for n, step in enumerate(steps if isinstance(steps, list) else [], start=1):
            if isinstance(step, dict) and "sandbox" in step:
                value = step.pop("sandbox")
                name = step.get("name") or f"{kind}-{n}"
                notes.append(
                    f"proposal asked for sandbox: {value} on {kind} step {name}; removed "
                    "(steps run in the sandbox)"
                )
    return notes


def _flag_structural_steps(proposal: dict[str, Any]) -> list[str]:
    """A note for every proposed step that inspects source text; the step is kept."""
    notes: list[str] = []
    for kind in ("verify", "evidence"):
        steps = proposal.get(kind)
        for n, step in enumerate(steps if isinstance(steps, list) else [], start=1):
            run = step.get("run") if isinstance(step, dict) else step
            reason = structural_reason(run) if isinstance(run, str) else None
            if reason:
                name = (step.get("name") if isinstance(step, dict) else None) or f"{kind}-{n}"
                notes.append(
                    f"{kind} step {name} looks like a structure check ({reason}): verify and "
                    "evidence should run the code, not inspect source files; review it "
                    "before queueing"
                )
    return notes


def _flag_shell_syntax_steps(proposal: dict[str, Any]) -> list[str]:
    """Check proposed scripts without executing them; keep steps and note diagnostics."""
    notes: list[str] = []
    for kind in ("verify", "evidence"):
        steps = proposal.get(kind)
        for n, step in enumerate(steps if isinstance(steps, list) else [], start=1):
            run = step.get("run") if isinstance(step, dict) else step
            if not isinstance(run, str):
                continue
            name = (step.get("name") if isinstance(step, dict) else None) or f"{kind}-{n}"
            try:
                result = subprocess.run(
                    ["bash", "-n"], input=run, capture_output=True, text=True, timeout=5
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                notes.append(f"{kind} step {name} shell syntax check failed: {exc}")
                continue
            # An unterminated heredoc emits a warning but bash -n exits zero.
            diagnostic = result.stderr.strip()
            if result.returncode or diagnostic:
                notes.append(
                    f"{kind} step {name} has a shell syntax issue: "
                    f"{diagnostic or f'bash -n exited {result.returncode}'}; review it before queueing"
                )
    return notes


@dataclass
class IntakeOutcome:
    spec: TaskSpec
    proposal: dict[str, Any] | None
    notes: list[str]
    errors: list[str]


async def _noop_event(_event: PiEvent) -> None:
    return None


def propose_acceptance(
    spec: TaskSpec,
    adapter: PiAdapter,
    model: ModelSpec,
    data_dir: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Run the intake model over the task and return its proposal.

    The session writes to a temporary dir (the sandbox's only write root); its
    session files are kept under ``<data-dir>/intake/<id>/sessions`` afterwards.
    """
    run_id = str(uuid.uuid4())
    with tempfile.TemporaryDirectory(prefix="helmraiser-intake-") as tmp:
        request = PiRequest(
            prompt=build_intake_prompt(spec, config),
            session_id=f"{run_id}-intake",
            model=model,
            cwd=spec.project,
            session_dir=Path(tmp) / "sessions",
            tools=INTAKE_TOOLS,
            context_files=True,
            stage="intake",
        )
        result = asyncio.run(adapter.run(request, _noop_event, run_id))
        if request.session_dir.is_dir():
            shutil.copytree(
                request.session_dir, data_dir / "intake" / run_id / "sessions", dirs_exist_ok=True
            )
    if not result.ok:
        raise ValueError(
            f"intake session failed ({result.error_kind}): {result.error_message or ''}".strip()
        )
    return parse_proposal(result.final_text)


def _intake_model_overrides(
    original: dict[str, Any],
    candidate: dict[str, Any],
    config: dict[str, Any],
    project: Path,
) -> dict[str, str]:
    """Keep original roles and new deviations from the current configured profiles."""
    profiles = profile_models(project, config)
    retained: dict[str, str] = {}
    for role, value in {**original, **candidate}.items():
        if value:
            model = ModelSpec.parse(value)
            if role in original or model != profiles.get(role):
                retained[role] = model.render()
    return retained


def _preflight_candidate_files(spec: TaskSpec) -> tuple[str, ...]:
    """Literal task-mentioned files, never inferred from acceptance commands."""
    files: dict[str, None] = {}
    body_tokens = re.split(r"[\s`\"'\[\]()<> ,;|]+", spec.body)
    for token in [*(spec.paths or ()), *body_tokens]:
        if "://" in token or any(char in token for char in "*?${}[]="):
            continue
        token = token.rstrip(".,:;!?")
        token = token.split("#", 1)[0].split("::", 1)[0]
        token = re.sub(r":\d+(?:-\d+)?$", "", token).removeprefix("./")
        if (
            not token
            or token.startswith(("/", "-", "~", "$"))
            or token.endswith("/")
            or any(segment in ("", "..") for segment in token.split("/"))
            or ("/" not in token and not PurePosixPath(token).suffix)
        ):
            continue
        files[token] = None
    return tuple(files)


def check_acceptance(spec: TaskSpec, *, config: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    """Preflight verify then evidence on HEAD, without pi or ledger access."""
    steps = [
        *spec.verify,
        *(VerifyStep(step.name, step.run, step.timeout, step.sandbox) for step in spec.evidence),
    ]
    try:
        payload = run_preflight_steps(spec.project, steps, config=config, data_dir=data_dir)
    except PreflightError as exc:
        return failed_scout_preflight(str(exc))
    return normalize_scout_preflight(spec.project, payload, _preflight_candidate_files(spec))


def run_intake(
    spec: TaskSpec,
    *,
    config: dict[str, Any],
    adapter: PiAdapter | None,
    model: ModelSpec | None,
    data_dir: Path,
    allow_auto: bool,
    approve: bool = True,
) -> IntakeOutcome:
    """Merge the model's proposal (if any) into ``spec`` and validate it as queued.

    With ``approve=False`` the result is a proposed draft (``status: draft``,
    no ``intake`` stamp) but validation still checks it as queued.
    """
    notes: list[str] = []
    proposal: dict[str, Any] | None = None
    frontmatter = spec_to_frontmatter(spec)
    original_models = frontmatter.get("models", {})

    if adapter is not None and not validate_task(replace(spec, status="queued")):
        notes.append("task already carries a valid acceptance block; intake model not asked")
    elif adapter is not None:
        if model is None:
            raise ValueError("intake model not configured")
        proposal = propose_acceptance(spec, adapter, model, data_dir, config)
        if spec.policy is not None and "policy" in proposal:
            proposal["policy"] = spec.policy
            notes.append(f"intake kept the file's policy: {spec.policy}")
        elif proposal.get("policy") == "auto" and not allow_auto:
            proposal["policy"] = "review"
            notes.append("proposal asked for policy auto; set to review (pass --allow-auto)")
        proposed_isolation = proposal.get("isolation")
        if (
            isinstance(proposed_isolation, str)
            and proposed_isolation in ISOLATIONS
            and ISOLATIONS.index(proposed_isolation) < ISOLATIONS.index(spec.isolation)
        ):
            proposal["isolation"] = spec.isolation
            notes.append(
                f"proposal asked for isolation {proposed_isolation}; kept {spec.isolation}"
            )
        notes += _strip_step_sandbox(proposal)
        notes += _flag_structural_steps(proposal)
        notes += _flag_shell_syntax_steps(proposal)
        if spec.plan_explicit and "plan" in proposal:
            proposal["plan"] = spec.plan
        had_models = "models" in proposal
        proposed_models = proposal.pop("models", None)
        candidates = proposed_models if isinstance(proposed_models, dict) else {}
        models = _intake_model_overrides(
            original_models, {**candidates, **original_models}, config, spec.project
        )
        if models:
            frontmatter["models"] = models
            if had_models:
                proposal["models"] = models
        else:
            frontmatter.pop("models", None)
        frontmatter.update(proposal)
    else:
        notes.append("no intake adapter available; validating the file's own acceptance block")

    if approve:
        frontmatter["status"] = "queued"
        previous_intake = spec.extra.get("intake")
        related = (
            previous_intake.get("related_tests_added")
            if isinstance(previous_intake, dict)
            else None
        )
        frontmatter["intake"] = {
            **({"related_tests_added": related} if isinstance(related, list) else {}),
            **({"model": model.render()} if model is not None else {}),
            "approved_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    else:
        frontmatter["status"] = "draft"
        frontmatter.pop("intake", None)
    try:
        merged = parse_task(serialize_frontmatter(frontmatter, spec.body), spec.source_path, config)
    except TaskLoadError as exc:
        return IntakeOutcome(spec, proposal, notes, [str(exc)])
    notes += outside_paths_warnings(merged)
    notes += body_outside_paths_warnings(merged)
    notes += related_tests_warnings(merged)
    return IntakeOutcome(merged, proposal, notes, validate_task(replace(merged, status="queued")))
