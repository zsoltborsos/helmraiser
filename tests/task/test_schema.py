"""Validity matrix for v2 tasks (spec §1.2)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from helmraiser.task.loader import parse_task
from helmraiser.task.model import Budget, EvidenceStep, ModelSpec, TaskSpec, VerifyStep
from helmraiser.task.schema import validate_task


def make_spec(project: Path, **overrides) -> TaskSpec:
    base = TaskSpec(
        id="demo-task",
        title="Demo task",
        project=project,
        priority="normal",
        status="draft",
        policy=None,
        isolation="none",
        models={
            "implement": ModelSpec("openai-codex/gpt-6.1-sol", "medium"),
            "fallback": ModelSpec("openai-codex/gpt-6-astra", "high"),
            "review": ModelSpec("openai-codex/gpt-6-astra", "high"),
        },
        budget=Budget(5.0, 90, 4),
        verify=(VerifyStep("tests", "pytest -q"),),
        evidence=(),
        tags=(),
        body="## Goal\n\nDo the thing.",
        source_path=project / "topics" / "demo-task.md",
    )
    return replace(base, **overrides)


def queued(project: Path, **overrides) -> TaskSpec:
    overrides.setdefault("status", "queued")
    overrides.setdefault("policy", "review")
    return make_spec(project, **overrides)


@pytest.mark.parametrize("kind", ["verify", "evidence"])
@pytest.mark.parametrize("value", [None, "specific [failure].*", "", "  ", 1, True, [], {}])
@pytest.mark.parametrize("marked", [False, True])
def test_red_output_validation(tmp_path, kind, value, marked):
    base = (
        VerifyStep("check", "false")
        if kind == "verify"
        else EvidenceStep("check", "false", (), "proof")
    )
    step = replace(base, red_on_base=marked, red_output=value)
    errors = validate_task(make_spec(tmp_path, **{kind: (step,)}))
    valid = value is None or (isinstance(value, str) and bool(value.strip()) and marked)
    assert (not errors) is valid
    if errors:
        assert any(f"{kind} step 'check': red_output" in error for error in errors)


class TestDraft:
    def test_minimal_draft_is_valid(self, tmp_path: Path):
        spec = make_spec(tmp_path, verify=(), models={})
        assert validate_task(spec) == []

    @pytest.mark.parametrize("bad_id", ["", "Bad", "-lead", "has space", "ünïcode"])
    def test_id_pattern(self, tmp_path: Path, bad_id: str):
        errors = validate_task(make_spec(tmp_path, id=bad_id))
        assert any("id" in e for e in errors)

    def test_title_required(self, tmp_path: Path):
        assert any("title" in e for e in validate_task(make_spec(tmp_path, title="  ")))

    def test_project_must_be_absolute(self, tmp_path: Path):
        errors = validate_task(make_spec(Path("relative/dir")))
        assert any("project" in e for e in errors)

    def test_enum_fields(self, tmp_path: Path):
        errors = validate_task(
            make_spec(tmp_path, priority="urgent", status="todo", policy="yolo", isolation="jail")
        )
        joined = "\n".join(errors)
        for word in ("priority", "status", "policy", "isolation"):
            assert word in joined

    @pytest.mark.parametrize("value", ["true", 1, None])
    def test_plan_must_be_boolean(self, tmp_path: Path, value: object):
        errors = validate_task(make_spec(tmp_path, plan=value))  # type: ignore[arg-type]
        assert "plan must be a boolean" in errors

    def test_empty_verify_run_rejected_even_in_draft(self, tmp_path: Path):
        spec = make_spec(tmp_path, verify=(VerifyStep("tests", "   "),))
        assert any("empty run" in e for e in validate_task(spec))

    def test_delivery_values(self, tmp_path: Path):
        assert validate_task(make_spec(tmp_path, delivery="pr")) == []
        errors = validate_task(make_spec(tmp_path, delivery="email"))
        assert errors == ["delivery must be one of merge, pr"]

    @pytest.mark.parametrize("base", ["", "bad name", "-invalid", "bad..name"])
    def test_invalid_base_is_rejected(self, tmp_path: Path, base: str):
        errors = validate_task(make_spec(tmp_path, base=base))
        assert errors == ["base must be a valid git branch name"]

    def test_project_delivery_default_applies_when_task_omits_it(self, tmp_path: Path):
        (tmp_path / ".helmraiser").mkdir()
        (tmp_path / ".helmraiser" / "project.toml").write_text(
            '[defaults]\ndelivery = "pr"\n', encoding="utf-8"
        )
        omitted = parse_task(
            f"---\nid: demo\ntitle: Demo\nproject: {tmp_path}\n---\nBody\n",
            tmp_path / "task.md",
            {},
        )
        explicit = parse_task(
            f"---\nid: demo\ntitle: Demo\nproject: {tmp_path}\ndelivery: merge\n---\nBody\n",
            tmp_path / "task.md",
            {},
        )
        assert omitted.delivery == "pr"
        assert explicit.delivery == "merge"

    def test_project_base_default_applies_when_task_omits_it(self, tmp_path: Path):
        (tmp_path / ".helmraiser").mkdir()
        (tmp_path / ".helmraiser" / "project.toml").write_text(
            '[defaults]\nbase = "programme/default"\n', encoding="utf-8"
        )
        omitted = parse_task(
            f"---\nid: demo\ntitle: Demo\nproject: {tmp_path}\n---\nBody\n",
            tmp_path / "task.md",
            {},
        )
        explicit = parse_task(
            f"---\nid: demo\ntitle: Demo\nproject: {tmp_path}\nbase: programme/task\n---\nBody\n",
            tmp_path / "task.md",
            {},
        )
        assert omitted.base == "programme/default"
        assert explicit.base == "programme/task"


class TestQueued:
    def test_valid_queued_task(self, tmp_project: Path):
        assert validate_task(queued(tmp_project)) == []

    def test_policy_required(self, tmp_project: Path):
        errors = validate_task(queued(tmp_project, policy=None))
        assert "policy is required for a queued task" in errors

    def test_verify_required(self, tmp_project: Path):
        errors = validate_task(queued(tmp_project, verify=()))
        assert any("verify step is required" in e for e in errors)

    def test_project_must_be_git_repo(self, tmp_path: Path):
        errors = validate_task(queued(tmp_path))
        assert any("not a git repository" in e for e in errors)

    def test_reviewer_must_differ_from_implementer(self, tmp_project: Path):
        same = ModelSpec("openai-codex/gpt-6-astra", "high")
        spec = queued(
            tmp_project,
            models={"implement": same, "review": ModelSpec("openai-codex/gpt-6-astra", "low")},
        )
        errors = validate_task(spec)
        assert any("reviewer must differ" in e for e in errors)

    def test_reviewer_thinking_alone_does_not_count_as_different(self, tmp_project: Path):
        spec = queued(
            tmp_project,
            models={
                "implement": ModelSpec("a/x", "low"),
                "review": ModelSpec("a/x", "high"),
            },
        )
        assert any("reviewer must differ" in e for e in validate_task(spec))

    @pytest.mark.parametrize("models", [{}, {"implement": ModelSpec("p/m")}])
    def test_model_free_task_management(self, tmp_project: Path, models):
        assert validate_task(queued(tmp_project, models=models)) == []

    def test_draft_does_not_need_git_repo(self, tmp_path: Path):
        assert validate_task(make_spec(tmp_path)) == []


class TestAutoPolicy:
    evidence = (EvidenceStep("shots", "capture", ("*.png",), "shows the dialog"),)

    def test_auto_needs_isolation_and_evidence(self, tmp_project: Path):
        errors = validate_task(queued(tmp_project, policy="auto"))
        assert "policy auto requires isolation process or vm" in errors
        assert "policy auto requires at least one evidence step" in errors

    def test_auto_with_isolation_but_no_evidence(self, tmp_project: Path):
        errors = validate_task(queued(tmp_project, policy="auto", isolation="process"))
        assert errors == ["policy auto requires at least one evidence step"]

    def test_auto_with_evidence_but_no_isolation(self, tmp_project: Path):
        errors = validate_task(queued(tmp_project, policy="auto", evidence=self.evidence))
        assert errors == ["policy auto requires isolation process or vm"]

    @pytest.mark.parametrize("isolation", ["process", "vm"])
    def test_auto_satisfied(self, tmp_project: Path, isolation: str):
        spec = queued(tmp_project, policy="auto", isolation=isolation, evidence=self.evidence)
        assert validate_task(spec) == []

    def test_auto_rule_applies_to_drafts_too(self, tmp_path: Path):
        """Violations are errors, never silently downgraded."""
        errors = validate_task(make_spec(tmp_path, policy="auto"))
        assert any("policy auto requires" in e for e in errors)

    def test_project_can_forbid_auto(self, tmp_project: Path):
        project_file = tmp_project / ".helmraiser" / "project.toml"
        project_file.parent.mkdir(exist_ok=True)
        project_file.write_text("[defaults]\nrequire_approval = true\n")
        auto = queued(tmp_project, policy="auto", isolation="process", evidence=self.evidence)

        assert validate_task(auto) == ["project requires approval: policy auto is not allowed"]
        assert validate_task(queued(tmp_project, policy="review")) == []
        assert validate_task(queued(tmp_project, policy="approve-plan-first")) == []

    def test_approve_plan_first_needs_nothing_extra(self, tmp_project: Path):
        assert validate_task(queued(tmp_project, policy="approve-plan-first")) == []


class TestStepSandbox:
    """``sandbox: host`` runs a step outside srt; only a require_approval project may."""

    host_verify = (VerifyStep("tests", "pytest -q", sandbox="host"),)
    host_evidence = (EvidenceStep("shots", "capture", ("*.png",), "c", sandbox="host"),)

    def require_approval(self, project: Path) -> None:
        project_file = project / ".helmraiser" / "project.toml"
        project_file.parent.mkdir(exist_ok=True)
        project_file.write_text("[defaults]\nrequire_approval = true\n")

    def test_host_step_needs_require_approval(self, tmp_project: Path):
        spec = queued(tmp_project, verify=self.host_verify, evidence=self.host_evidence)
        assert validate_task(spec) == [
            "sandbox: host (verify step 'tests', evidence step 'shots') needs "
            "require_approval = true in the project's .helmraiser/project.toml"
        ]
        assert any(
            "needs require_approval" in e for e in validate_task(replace(spec, status="draft"))
        )

    def test_host_step_is_valid_with_require_approval(self, tmp_project: Path):
        self.require_approval(tmp_project)
        spec = queued(tmp_project, verify=self.host_verify, evidence=self.host_evidence)
        assert validate_task(spec) == []

    def test_other_sandbox_values_are_rejected(self, tmp_project: Path):
        self.require_approval(tmp_project)
        spec = queued(tmp_project, verify=(VerifyStep("tests", "pytest -q", sandbox="none"),))
        assert validate_task(spec) == ["verify step 'tests': sandbox must be host"]


@pytest.mark.parametrize("status", ["draft", "queued"])
def test_depends_on_self(tmp_project, status):
    spec = make_spec(tmp_project, status=status, policy="review", depends_on=("demo-task",))
    assert any("self" in error for error in validate_task(spec))


@pytest.mark.parametrize("dependencies", [("Bad",), (1,), (True,)])
def test_depends_on_constructed_invalid(tmp_path, dependencies):
    assert any(
        "depends_on" in error
        for error in validate_task(make_spec(tmp_path, depends_on=dependencies))
    )


def test_depends_on_unknown_valid(tmp_path):
    assert validate_task(make_spec(tmp_path, depends_on=("unknown-task",))) == []
