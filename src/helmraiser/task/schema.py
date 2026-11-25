"""Task validity rules (spec §1.2)."""

from __future__ import annotations

import subprocess

from helmraiser.task.model import (
    DELIVERIES,
    ENV_KEY_PATTERN,
    ID_PATTERN,
    ISOLATIONS,
    POLICIES,
    PRIORITIES,
    STATUSES,
    STEP_SANDBOXES,
    TaskSpec,
    parse_depends_on,
)
from helmraiser.task.paths import parse_paths
from helmraiser.task.project import load_project_defaults


def validate_task(task: TaskSpec) -> list[str]:
    """Return validation errors; an empty list means the task is valid.

    ``draft`` needs only ``id``, ``title`` and ``project``. ``queued`` (runnable)
    additionally needs a policy, non-empty verify steps, an existing git repo and
    a reviewer that differs from the implementer. ``policy: auto`` needs process or
    vm isolation and at least one evidence step. A step with ``sandbox: host`` needs
    the project's ``require_approval = true``.
    """
    errors: list[str] = []
    if task.paths is not None:
        try:
            parse_paths(list(task.paths))
        except ValueError as exc:
            errors.append(str(exc))

    if not task.id or not ID_PATTERN.match(task.id):
        errors.append("id is required and must match [a-z0-9][a-z0-9._-]*")
    try:
        parse_depends_on(list(task.depends_on))
    except ValueError as exc:
        errors.append(str(exc))
    if task.id in task.depends_on:
        errors.append("depends_on must not contain a self-dependency")
    if not task.title.strip():
        errors.append("title is required")
    if not str(task.project).strip() or not task.project.is_absolute():
        errors.append("project is required and must be an absolute path")
    if task.priority not in PRIORITIES:
        errors.append(f"priority must be one of {', '.join(PRIORITIES)}")
    if task.status not in STATUSES:
        errors.append(f"status must be one of {', '.join(STATUSES)}")
    if task.policy is not None and task.policy not in POLICIES:
        errors.append(f"policy must be one of {', '.join(POLICIES)}")
    if not isinstance(task.plan, bool):
        errors.append("plan must be a boolean")
    if not isinstance(task.experiment, bool):
        errors.append("experiment must be a boolean")
    if task.delivery not in DELIVERIES:
        errors.append(f"delivery must be one of {', '.join(DELIVERIES)}")
    if task.base is not None:
        valid_base = (
            bool(task.base)
            and subprocess.run(
                ["git", "check-ref-format", "--branch", task.base],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        if not valid_base:
            errors.append("base must be a valid git branch name")
    if task.isolation not in ISOLATIONS:
        errors.append(f"isolation must be one of {', '.join(ISOLATIONS)}")

    for step in task.verify:
        if not isinstance(step.red_on_base, bool):
            errors.append(f"verify step '{step.name}': red_on_base must be a boolean")
        if not step.run.strip():
            errors.append(f"verify step '{step.name}' has an empty run command")
        if step.name == "task-paths":
            errors.append("verify step name 'task-paths' is reserved for the path scope check")
    for evidence in task.evidence:
        if not isinstance(evidence.red_on_base, bool):
            errors.append(f"evidence step '{evidence.name}': red_on_base must be a boolean")
        if not evidence.run.strip():
            errors.append(f"evidence step '{evidence.name}' has an empty run command")
    for kind, acceptance_steps in (("verify", task.verify), ("evidence", task.evidence)):
        for acceptance_step in acceptance_steps:
            if acceptance_step.red_output is not None:
                if (
                    not isinstance(acceptance_step.red_output, str)
                    or not acceptance_step.red_output.strip()
                ):
                    errors.append(
                        f"{kind} step '{acceptance_step.name}': red_output must be a non-empty string"
                    )
                if acceptance_step.red_on_base is not True:
                    errors.append(
                        f"{kind} step '{acceptance_step.name}': red_output requires red_on_base: true"
                    )
    steps = [("verify", s.name, s.sandbox) for s in task.verify] + [
        ("evidence", e.name, e.sandbox) for e in task.evidence
    ]
    host_steps: list[str] = []
    for kind, name, sandbox in steps:
        if sandbox is None:
            continue
        if sandbox not in STEP_SANDBOXES:
            errors.append(f"{kind} step '{name}': sandbox must be {', '.join(STEP_SANDBOXES)}")
        else:
            host_steps.append(f"{kind} step '{name}'")
    if (
        host_steps
        and task.project.is_absolute()
        and not load_project_defaults(task.project).require_approval
    ):
        errors.append(
            f"sandbox: host ({', '.join(host_steps)}) needs require_approval = true "
            "in the project's .helmraiser/project.toml"
        )
    for key in task.env:
        if not ENV_KEY_PATTERN.match(key):
            errors.append(f"env key {key!r} is not a valid environment variable name")

    if task.status == "queued":
        if task.policy is None:
            errors.append("policy is required for a queued task")
        if not task.verify:
            errors.append("at least one verify step is required for a queued task")
        if not (task.project / ".git").exists():
            errors.append(f"project is not a git repository: {task.project}")
        implement = task.models.get("implement")
        review = task.models.get("review")
        if implement is not None and review is not None and implement.model == review.model:
            errors.append(f"reviewer must differ from implementer (both {implement.model})")

    if task.policy == "auto":
        if task.project.is_absolute() and load_project_defaults(task.project).require_approval:
            errors.append("project requires approval: policy auto is not allowed")
        if task.isolation not in ("process", "vm"):
            errors.append("policy auto requires isolation process or vm")
        if not task.evidence:
            errors.append("policy auto requires at least one evidence step")

    return errors
