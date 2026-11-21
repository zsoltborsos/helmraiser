"""Loader tests: round-trip, quality_gates sugar, legacy status mapping, profiles."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from helmraiser.config import DEFAULT_CONFIG
from helmraiser.task.loader import (
    TaskLoadError,
    load_task,
    parse_task,
    render_task,
    write_task,
)
from helmraiser.task.model import Budget, ModelSpec
from helmraiser.utils.markdown import parse_frontmatter, serialize_frontmatter

FULL_TASK = """---
id: rm-heartbeat-injection
title: Remove heartbeat prompt injection
project: {project}
priority: high
status: queued
policy: review
plan: true
base: programme/x
isolation: none
models:
  implement: openai-codex/gpt-6.1-sol:medium
  fallback: openai-codex/gpt-6-astra:high
  review: openai-codex/gpt-6-astra:high
budget:
  max_cost_usd: 3.0
  max_minutes: 60
  max_iterations: 4
verify:
  - name: tests
    run: .venv/bin/python -m pytest -q -p no:cacheprovider
    timeout: 600
  - name: lint
    run: .venv/bin/ruff check src tests && .venv/bin/mypy src/helmraiser
evidence:
  - name: help-screens
    run: .venv/bin/helmraiser --help > "$HELMRAISER_EVIDENCE_DIR/help.txt"
    artifacts: ["help.txt"]
    criteria: >
      help.txt lists no `agents` command group and no heartbeat flags.
tags: [cleanup, v2]
intake:
  model: openai-codex/gpt-6-astra
  approved_at: 2026-09-23T18:04:00Z
future_key: keep me
---
## Goal

Delete the heartbeat.

---

Not a frontmatter delimiter.
"""


@pytest.fixture
def config() -> dict:
    return {
        "profiles": {
            "intake": {"model": "p/intake", "thinking": "high"},
            "plan": {"model": "p/planner", "thinking": "high"},
            "implement": {"model": "p/impl", "thinking": "medium"},
            "fallback": {"model": "p/fallback", "thinking": "high"},
            "review": {"model": "p/review", "thinking": "high"},
            "scout": {"model": "p/scout", "thinking": "low"},
            "judge": {"model": "p/judge", "thinking": "high"},
        },
        "budget": {"max_cost_usd": 7.0, "max_minutes": 45, "max_iterations": 3},
    }


def write(tmp_path: Path, content: str, name: str = "task.md") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


class TestLoad:
    def test_full_file(self, tmp_path: Path, config: dict):
        path = write(tmp_path, FULL_TASK.format(project=tmp_path))
        spec = load_task(path, config)

        assert spec.id == "rm-heartbeat-injection"
        assert spec.project == tmp_path
        assert spec.priority == "high"
        assert spec.status == "queued"
        assert spec.policy == "review"
        assert spec.plan is True
        assert spec.base == "programme/x"
        assert spec.models["plan"] == ModelSpec("p/planner", "high")
        assert spec.models["implement"] == ModelSpec("openai-codex/gpt-6.1-sol", "medium")
        assert spec.models["review"] == ModelSpec("openai-codex/gpt-6-astra", "high")
        assert spec.models["intake"] == ModelSpec("p/intake", "high")  # from config
        assert spec.models["scout"] == ModelSpec("p/scout", "low")
        assert spec.budget_explicit is True
        assert (spec.budget.max_cost_usd, spec.budget.max_minutes) == (3.0, 60)
        assert [v.name for v in spec.verify] == ["tests", "lint"]
        assert spec.verify[0].timeout == 600
        assert spec.verify[1].timeout == 300
        assert spec.evidence[0].artifacts == ("help.txt",)
        assert spec.evidence[0].criteria.startswith("help.txt lists")
        assert spec.evidence[0].timeout == 600
        assert spec.tags == ("cleanup", "v2")
        assert spec.extra["intake"]["model"] == "openai-codex/gpt-6-astra"
        assert spec.extra["future_key"] == "keep me"
        assert "Not a frontmatter delimiter." in spec.body
        assert spec.body.startswith("## Goal")
        assert spec.source_path == path.resolve()

    def test_profile_resolution_without_overrides(self, tmp_path: Path, config: dict):
        path = write(tmp_path, f"---\nid: t\ntitle: T\nproject: {tmp_path}\n---\nbody\n")
        spec = load_task(path, config)
        assert spec.budget_explicit is False
        assert spec.models["implement"] == ModelSpec("p/impl", "medium")
        assert spec.models["fallback"] == ModelSpec("p/fallback", "high")
        assert spec.models["review"] == ModelSpec("p/review", "high")
        assert (spec.budget.max_cost_usd, spec.budget.max_minutes, spec.budget.max_iterations) == (
            7.0,
            45,
            3,
        )
        assert spec.status == "draft"
        assert spec.priority == "normal"
        assert spec.isolation == "process"
        assert spec.policy is None
        assert spec.plan is False

    @pytest.mark.parametrize(
        ("budget_fm", "explicit", "expected"),
        [
            ({}, False, Budget(7.0, 45, 3)),
            ({"budget": {}}, True, Budget(7.0, 45, 3)),
            ({"budget": {"max_minutes": 10}}, True, Budget(7.0, 10, 3)),
            (
                {"budget": {"max_cost_usd": 2.0, "max_minutes": 20, "max_iterations": 2}},
                True,
                Budget(2.0, 20, 2),
            ),
            ({"budget": None}, False, Budget(7.0, 45, 3)),
            ({"budget": "invalid"}, False, Budget(7.0, 45, 3)),
        ],
    )
    def test_budget_provenance(self, tmp_path: Path, config: dict, budget_fm, explicit, expected):
        spec = parse_task(
            serialize_frontmatter({"id": "t", **budget_fm}, "body"), tmp_path / "t.md", config
        )
        assert spec.budget_explicit is explicit
        assert spec.budget == expected

    def test_judge_is_a_profile(self, tmp_path: Path, config: dict):
        spec = load_task(write(tmp_path, "---\nid: t\n---\nbody\n"), config)
        assert spec.models["judge"] == ModelSpec("p/judge", "high")
        assert "judge" not in spec.explicit_models
        assert "judge" not in render_task(spec)

    def test_partial_overrides_keep_other_profiles(self, tmp_path: Path, config: dict):
        content = (
            f"---\nid: t\ntitle: T\nproject: {tmp_path}\n"
            "models:\n  review: other/reviewer\n"
            "budget:\n  max_minutes: 10\n---\n"
        )
        spec = load_task(write(tmp_path, content), config)
        assert spec.models["review"] == ModelSpec("other/reviewer", None)
        assert spec.models["implement"] == ModelSpec("p/impl", "medium")
        assert spec.budget.max_minutes == 10
        assert spec.budget.max_cost_usd == 7.0

    def test_empty_config_resolves_no_models(self, tmp_path: Path):
        spec = load_task(write(tmp_path, f"---\nid: t\ntitle: T\nproject: {tmp_path}\n---\n"), {})
        assert spec.models == {}
        assert spec.budget_explicit is False
        assert spec.budget == Budget(**DEFAULT_CONFIG["budget"])

    def test_quality_gates_sugar(self, tmp_path: Path, config: dict):
        content = (
            f"---\nid: old\ntitle: Old topic\nproject: {tmp_path}\n"
            "quality_gates:\n  - pytest -q\n  - ruff check src\n---\nbody\n"
        )
        spec = load_task(write(tmp_path, content), config)
        assert [(v.name, v.run) for v in spec.verify] == [
            ("gate-1", "pytest -q"),
            ("gate-2", "ruff check src"),
        ]
        assert "quality_gates" not in spec.extra

    @pytest.mark.parametrize(
        ("legacy", "mapped"),
        [
            ("todo", "draft"),
            ("in_progress", "running"),
            ("review", "awaiting_approval"),
            ("done", "landed"),
            ("queued", "queued"),
        ],
    )
    def test_legacy_status_mapping(self, tmp_path: Path, config: dict, legacy: str, mapped: str):
        content = f"---\nid: t\ntitle: T\nproject: {tmp_path}\nstatus: {legacy}\n---\n"
        assert load_task(write(tmp_path, content), config).status == mapped

    def test_legacy_medium_priority(self, tmp_path: Path, config: dict):
        content = f"---\nid: t\ntitle: T\nproject: {tmp_path}\npriority: medium\n---\n"
        assert load_task(write(tmp_path, content), config).priority == "normal"

    def test_missing_file(self, tmp_path: Path, config: dict):
        with pytest.raises(TaskLoadError, match="not found"):
            load_task(tmp_path / "nope.md", config)

    def test_no_frontmatter(self, tmp_path: Path, config: dict):
        with pytest.raises(TaskLoadError, match="no frontmatter"):
            load_task(write(tmp_path, "# just a heading\n"), config)

    def test_invalid_yaml_frontmatter_reports_file_and_line(self, tmp_path: Path, config: dict):
        with pytest.raises(TaskLoadError, match=r"bad\.md: invalid YAML frontmatter.*at line 3"):
            parse_task(
                "---\nid: bad\ntitle: a: b\n---\nbody\n",
                tmp_path / "bad.md",
                config,
            )

    def test_evidence_criteria_list_is_joined_into_one_string(self, tmp_path: Path, config: dict):
        content = (
            f"---\nid: t\ntitle: T\nproject: {tmp_path}\nevidence:\n"
            "  - name: e\n    run: x\n    criteria:\n      - Shows A.\n      - Shows B.\n"
            "  - name: f\n    run: y\n    criteria: '  One sentence.  '\n---\n"
        )
        spec = load_task(write(tmp_path, content), config)
        assert [e.criteria for e in spec.evidence] == ["Shows A. Shows B.", "One sentence."]

    def test_verify_entry_must_be_mapping(self, tmp_path: Path, config: dict):
        with pytest.raises(TaskLoadError, match="verify\\[1\\]"):
            parse_task(
                f"---\nid: t\ntitle: T\nproject: {tmp_path}\nverify:\n  - 3\n---\n",
                tmp_path / "x.md",
                config,
            )


class TestRoundTrip:
    @pytest.mark.parametrize(
        "models",
        [
            {},
            {"review": "other/reviewer"},
            {"implement": "p/impl:medium"},
            {"intake": "other/intake", "scout": "other/scout:high"},
        ],
    )
    def test_only_explicit_models_are_written(self, tmp_path: Path, config: dict, models: dict):
        content = serialize_frontmatter({"id": "t", "models": models}, "body")
        spec = parse_task(content, tmp_path / "t.md", config)
        assert spec.explicit_models == frozenset(models)
        assert set(spec.models) == set(config["profiles"])
        fm = parse_frontmatter(render_task(spec))["frontmatter"]
        if models:
            assert fm["models"] == models
        else:
            assert "models" not in fm
            config["profiles"]["implement"] = "new/implement:high"
            again = parse_task(render_task(spec), spec.source_path, config)
            assert again.models["implement"] == ModelSpec("new/implement", "high")

    @pytest.mark.parametrize("plan", [True, False, None])
    def test_plan_presence_round_trips(self, tmp_path: Path, config: dict, plan: bool | None):
        fm = {"id": "t", **({"plan": plan} if plan is not None else {})}
        spec = parse_task(serialize_frontmatter(fm, "body"), tmp_path / "t.md", config)
        assert spec.plan_explicit is (plan is not None)
        rendered = parse_frontmatter(render_task(spec))["frontmatter"]
        assert ("plan" in rendered) is (plan is not None)
        if plan is not None:
            assert rendered["plan"] is plan
        assert parse_task(render_task(spec), spec.source_path, config) == replace(
            spec, budget_explicit=True
        )

    def test_implicit_budget_round_trips_as_explicit(self, tmp_path: Path, config: dict):
        spec = parse_task("---\nid: t\n---\nbody\n", tmp_path / "t.md", config)
        assert spec.budget_explicit is False
        text = render_task(spec)
        fm = parse_frontmatter(text)["frontmatter"]
        assert fm["budget"] == config["budget"]
        assert "budget_explicit" not in fm
        assert parse_task(text, spec.source_path, config) == replace(spec, budget_explicit=True)

    def test_direct_construction_model_serialization_compatibility(
        self, tmp_path: Path, config: dict
    ):
        spec = parse_task("---\nid: t\n---\n", tmp_path / "t.md", config)
        spec = replace(spec, explicit_models=None)
        fm = parse_frontmatter(render_task(spec))["frontmatter"]
        assert fm["models"] == {
            role: spec.models[role].render() for role in ("plan", "implement", "fallback", "review")
        }

    def test_write_then_load_is_identical(self, tmp_path: Path, config: dict):
        path = write(tmp_path, FULL_TASK.format(project=tmp_path))
        spec = load_task(path, config)
        out = tmp_path / "out" / "task.md"
        write_task(spec, out)
        again = load_task(out, config)
        assert again == replace(spec, source_path=out.resolve())

    def test_written_file_has_intake_block_and_body(self, tmp_path: Path, config: dict):
        spec = load_task(write(tmp_path, FULL_TASK.format(project=tmp_path)), config)
        text = render_task(spec)
        assert text.startswith("---\n")
        assert "intake:" in text
        assert "approved_at:" in text
        assert "future_key: keep me" in text
        assert text.rstrip().endswith("Not a frontmatter delimiter.")

    def test_step_sandbox_host_round_trips_and_is_omitted_when_unset(
        self, tmp_path: Path, config: dict
    ):
        content = FULL_TASK.format(project=tmp_path).replace(
            "    timeout: 600\n", "    timeout: 600\n    sandbox: host\n"
        )
        content = content.replace(
            '    artifacts: ["help.txt"]\n', '    artifacts: ["help.txt"]\n    sandbox: host\n'
        )
        spec = load_task(write(tmp_path, content), config)
        assert [s.sandbox for s in spec.verify] == ["host", None]
        assert spec.evidence[0].sandbox == "host"
        text = render_task(spec)
        assert text.count("sandbox: host") == 2
        again = load_task(write(tmp_path, text, "rt.md"), config)
        assert again == replace(spec, source_path=(tmp_path / "rt.md").resolve())
        plain = render_task(load_task(write(tmp_path, FULL_TASK.format(project=tmp_path)), config))
        assert "sandbox:" not in plain

    def test_unset_policy_and_empty_lists_are_omitted(self, tmp_path: Path, config: dict):
        spec = load_task(
            write(tmp_path, f"---\nid: t\ntitle: T\nproject: {tmp_path}\n---\n"), config
        )
        text = render_task(spec)
        assert "policy:" not in text
        assert "\nplan: true\n" not in text
        assert "evidence:" not in text
        assert "tags:" not in text
        assert load_task(write(tmp_path, text, "rt.md"), config).policy is None

    @pytest.mark.parametrize(
        ("isolation", "expected"),
        [("none", "none"), ("process", "process"), ("vm", "vm")],
    )
    def test_explicit_isolation_is_kept(
        self, tmp_path: Path, config: dict, isolation: str, expected: str
    ) -> None:
        content = f"---\nid: t\ntitle: T\nproject: {tmp_path}\nisolation: {isolation}\n---\n"
        assert load_task(write(tmp_path, content), config).isolation == expected


@pytest.mark.parametrize(
    "value", ["upstream", {}, None, [1], [True], ["Bad"], ["has space"], ["a\n"]]
)
def test_depends_on_invalid(tmp_path, value):
    content = serialize_frontmatter(
        {"id": "t", "title": "T", "project": str(tmp_path), "depends_on": value}, "Body"
    )
    with pytest.raises(TaskLoadError, match="depends_on"):
        parse_task(content, tmp_path / "task.md", {})


@pytest.mark.parametrize("dependencies", [None, [], ["first", "second-task"]])
def test_depends_on_roundtrip(tmp_path, config, dependencies):
    content = FULL_TASK.format(project=tmp_path)
    if dependencies is not None:
        parsed = parse_frontmatter(content)
        parsed["frontmatter"]["depends_on"] = dependencies
        content = serialize_frontmatter(parsed["frontmatter"], parsed["body"])
    spec = parse_task(content, tmp_path / "task.md", config)
    assert spec.depends_on == tuple(dependencies or [])
    assert "depends_on" not in spec.extra
    again = parse_task(render_task(spec), spec.source_path, config)
    assert again == spec
    if not dependencies:
        assert "depends_on" not in parse_frontmatter(render_task(spec))["frontmatter"]
