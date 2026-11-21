"""CLI tests for ``helmraiser add``, ``intake`` and ``queue`` (spec §5, step 1)."""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from helmraiser.cli import app, commands
from helmraiser.config import DEFAULT_CONFIG
from helmraiser.isolation import process as process_module
from helmraiser.isolation.process import ProcessIsolation
from helmraiser.ledger.db import Ledger
from helmraiser.loop import intake as intake_logic
from helmraiser.loop import preflight as preflight_logic
from helmraiser.loop.intake import _intake_model_overrides, build_intake_prompt
from helmraiser.loop.preflight import PreflightError
from helmraiser.task.loader import parse_task, render_task, resolve_models
from helmraiser.task.model import Budget, TaskSpec
from helmraiser.utils.markdown import parse_frontmatter, serialize_frontmatter
from tests.loop.test_preflight import assert_cleaned_up, project_toml, srt_calls
from tests.loop.test_preflight import srt_log as _srt_log_fixture  # noqa: F401
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

runner = CliRunner()

DRAFT = """---
id: fix-widget
title: Fix the widget
project: {project}
status: draft
---
## Goal

Make the widget work.
"""

COMPLETE = """---
id: fix-widget
title: Fix the widget
project: {project}
status: draft
policy: review
verify:
  - name: tests
    run: pytest -q
---
## Goal

Make the widget work.
"""

PROPOSAL = """Here is my proposal.

```yaml
verify:
  - name: tests
    run: pytest -q
    timeout: 120
  - name: lint
    run: ruff check src
evidence: []
policy: {policy}
isolation: {isolation}
budget:
  max_cost_usd: 2.5
  max_minutes: 30
  max_iterations: 3
models:
  implement: openai-codex/gpt-6.1-sol:high
  fallback: openai-codex/gpt-6.1-sol:xhigh
  review: openai-codex/gpt-6-astra:high
```
"""


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, configured_config: dict) -> dict:
    config = configured_config
    monkeypatch.setattr(commands, "load_config", lambda: config)
    return config


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def intake_srt_log(request: pytest.FixtureRequest) -> Path:
    return request.getfixturevalue("_srt_log_fixture")


@pytest.fixture
def ledger(data_dir: Path) -> Ledger:
    return Ledger(data_dir / "helmraiser.db")


def write_task(project: Path, template: str) -> Path:
    path = project / "topics" / "fix-widget.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(template.format(project=project), encoding="utf-8")
    return path


def invoke(*args: str, data_dir: Path) -> object:
    return runner.invoke(app, [*args, "--data-dir", str(data_dir)])


def use_adapter(monkeypatch: pytest.MonkeyPatch, adapter: ScriptedPiAdapter | None) -> None:
    monkeypatch.setattr(commands, "get_intake_adapter", lambda config: adapter)


@pytest.mark.parametrize("as_json", [False, True])
def test_missing_intake_refuses_before_side_effects(
    tmp_project, data_dir, isolated_config, monkeypatch, as_json
):
    isolated_config["profiles"] = {}
    path = write_task(tmp_project, DRAFT)
    original = path.read_text()
    monkeypatch.setattr(commands, "get_intake_adapter", lambda _: pytest.fail("adapter started"))
    result = invoke(
        "intake", str(path), "--yes", *(["--json"] if as_json else []), data_dir=data_dir
    )
    assert result.exit_code == 2, result.output
    if as_json:
        assert json.loads(result.output) == {
            "error": "models_not_configured",
            "missing": ["intake"],
        }
    else:
        assert "models not configured: intake" in result.output
        assert "helmraiser-setup skill" in result.output
    assert path.read_text() == original
    assert not data_dir.exists()


@pytest.mark.parametrize("override", ["option", "task"])
def test_explicit_intake_model_without_profiles(
    tmp_project, data_dir, isolated_config, monkeypatch, override
):
    isolated_config["profiles"] = {}
    path = write_task(tmp_project, DRAFT)
    options = ["--model", "p/intake:high"] if override == "option" else []
    if override == "task":
        path.write_text(
            path.read_text().replace("---\n", "---\nmodels:\n  intake: p/intake:high\n", 1)
        )
    adapter = ScriptedPiAdapter(
        {"intake": PiScript(final_text=PROPOSAL.format(policy="review", isolation="process"))}
    )
    use_adapter(monkeypatch, adapter)
    result = invoke("intake", str(path), "--yes", "--json", *options, data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert len(adapter.calls) == 1
    assert adapter.calls[0].model.render() == "p/intake:high"


def test_complete_intake_needs_no_models(tmp_project, data_dir, isolated_config, monkeypatch):
    isolated_config["profiles"] = {}
    use_adapter(monkeypatch, ScriptedPiAdapter({}))
    path = write_task(tmp_project, COMPLETE)
    result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    stamp = parse_frontmatter(path.read_text())["frontmatter"]["intake"]
    assert "approved_at" in stamp and "model" not in stamp


class TestParseProposal:
    @pytest.mark.parametrize("language", ["yaml", "yml"])
    @pytest.mark.parametrize("closing", ["```", " \t``` \t", "```\n"])
    def test_heredoc_backticks_are_preserved_and_last_block_wins(self, language, closing):
        run = 'python - <<\'PY\'\nprint("```json")\nprint("inline ``` fence")\nPY\n'
        block = "verify:\n  - name: heredoc\n    run: |\n" + "".join(
            f"      {line}\n" for line in run.splitlines()
        )
        text = f"```yaml\nverify: []\n```\nThen:\n```{language}\n{block}{closing}"

        proposal = intake_logic.parse_proposal(text)

        assert proposal["verify"] == [{"name": "heredoc", "run": run}]

    @pytest.mark.parametrize(
        "text",
        [
            "```yaml\nverify: [] ```",
            "```yaml\nverify: []\n``` trailing text",
            "prefix ```yaml\nverify: []\n```",
            "```yaml extra\nverify: []\n```",
            "```yaml\n\nverify: []\n",  # no closing line
        ],
    )
    def test_only_line_delimited_fences_complete_a_block(self, text):
        with pytest.raises(ValueError, match="no fenced yaml block"):
            intake_logic.parse_proposal(text)


class TestShellSyntaxSteps:
    @pytest.mark.parametrize("kind", ["verify", "evidence"])
    @pytest.mark.parametrize(
        ("run", "diagnostic", "returncode"),
        [
            ('echo "unterminated\n', "unexpected EOF", 2),
            ("cat <<'END'\nmissing terminator\n", "here-document", 0),
        ],
    )
    def test_invalid_shell_is_kept_with_bash_diagnostic(
        self, tmp_project, data_dir, monkeypatch, kind, run, diagnostic, returncode
    ):
        checked = subprocess.run(["bash", "-n"], input=run, text=True, capture_output=True)
        assert checked.returncode == returncode and diagnostic in checked.stderr
        step = {"name": "broken-script", "run": run}
        proposal = {"policy": "review", "verify": [{"run": "true"}], kind: [step]}
        path = write_task(tmp_project, DRAFT)
        use_adapter(
            monkeypatch,
            ScriptedPiAdapter(
                {"intake": PiScript(final_text=f"```yaml\n{yaml.safe_dump(proposal)}```")}
            ),
        )

        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        notes = json.loads(result.output)["notes"]
        assert any(f"{kind} step broken-script" in n and checked.stderr.strip() in n for n in notes)
        fm = parse_frontmatter(path.read_text())["frontmatter"]
        assert len(fm[kind]) == 1
        assert fm[kind][0]["name"] == step["name"]
        assert fm[kind][0]["run"] == run

    def test_valid_multiline_steps_are_not_executed_or_flagged(
        self, tmp_project, data_dir, monkeypatch
    ):
        marker = tmp_project / "must-not-exist"
        run = f"touch '{marker}'\ncat <<'END'\n```json\nEND\n"
        proposal = {
            "policy": "review",
            "verify": [{"name": "valid-verify", "run": run}],
            "evidence": [{"name": "valid-evidence", "run": run}],
        }
        path = write_task(tmp_project, DRAFT)
        use_adapter(
            monkeypatch,
            ScriptedPiAdapter(
                {"intake": PiScript(final_text=f"```yaml\n{yaml.safe_dump(proposal)}```")}
            ),
        )
        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["notes"] == []
        assert not marker.exists()

    def test_syntax_checks_use_stdin_and_a_short_timeout(self, monkeypatch):
        calls = []

        def check(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(intake_logic.subprocess, "run", check)
        proposal = {"verify": [{"run": "echo verify"}], "evidence": ["echo evidence"]}

        assert intake_logic._flag_shell_syntax_steps(proposal) == []
        assert len(calls) == 2
        for (argv, kwargs), run in zip(calls, ["echo verify", "echo evidence"], strict=True):
            assert argv == ["bash", "-n"]
            assert kwargs["input"] == run
            assert kwargs["text"] and kwargs["capture_output"]
            assert 0 < kwargs["timeout"] <= 5

    @pytest.mark.parametrize(
        "error", [subprocess.TimeoutExpired(["bash", "-n"], 5), OSError("no bash")]
    )
    def test_unavailable_check_is_a_note_not_a_crash(self, monkeypatch, error):
        def check(*args, **kwargs):
            raise error

        monkeypatch.setattr(intake_logic.subprocess, "run", check)
        notes = intake_logic._flag_shell_syntax_steps({"verify": ["true"]})
        assert len(notes) == 1 and "verify step verify-1" in notes[0]
        assert str(error) in notes[0]


# ── add ───────────────────────────────────────────────────────────────


class TestAdd:
    def test_add_from_file(self, tmp_project: Path, data_dir: Path, ledger: Ledger):
        path = write_task(tmp_project, DRAFT)
        result = invoke("add", str(path), "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "draft"
        assert out["task_id"].endswith(":fix-widget")
        row = ledger.get_task(out["task_id"])
        assert row is not None and row["status"] == "draft"
        assert row["snapshot"] == path.read_text()
        assert row["queued_at"] is None

    def test_add_registers_as_draft_even_if_file_says_queued(
        self, tmp_project: Path, data_dir: Path, ledger: Ledger
    ):
        path = write_task(tmp_project, COMPLETE.replace("status: draft", "status: queued"))
        result = invoke("add", str(path), data_dir=data_dir)
        assert result.exit_code == 0, result.output
        row = ledger.get_task(result.output.strip())
        assert row is not None and row["status"] == "draft"
        assert "status: queued" in path.read_text()  # file untouched

    def test_add_from_text(self, tmp_project: Path, data_dir: Path, ledger: Ledger):
        result = invoke(
            "add",
            "--project",
            str(tmp_project),
            "--title",
            "Fix the Widget!",
            "Make it work.",
            "--json",
            data_dir=data_dir,
        )
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        path = tmp_project / ".helmraiser" / "tasks" / "fix-the-widget.md"
        assert Path(out["path"]) == path
        parsed = parse_frontmatter(path.read_text())
        assert parsed["frontmatter"]["id"] == "fix-the-widget"
        assert parsed["frontmatter"]["status"] == "draft"
        assert parsed["frontmatter"]["project"] == str(tmp_project)
        assert parsed["body"] == "Make it work."
        assert ledger.get_task(out["task_id"]) is not None

    def test_add_from_text_refuses_to_overwrite(self, tmp_project: Path, data_dir: Path):
        existing = tmp_project / ".helmraiser" / "tasks" / "fix-widget.md"
        existing.parent.mkdir(parents=True)
        existing.write_text(DRAFT)
        result = invoke(
            "add", "--project", str(tmp_project), "--title", "Fix widget", data_dir=data_dir
        )
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_add_title_requires_project(self, tmp_path: Path, data_dir: Path):
        result = invoke("add", "--title", "x", "--json", data_dir=data_dir)
        assert result.exit_code == 2
        assert "project" in json.loads(result.output)["error"]

    def test_add_missing_file(self, tmp_path: Path, data_dir: Path):
        result = invoke("add", str(tmp_path / "nope.md"), data_dir=data_dir)
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_add_invalid_task_exits_2(self, tmp_project: Path, data_dir: Path):
        path = write_task(tmp_project, DRAFT.replace("id: fix-widget", "id: Bad ID"))
        result = invoke("add", str(path), "--json", data_dir=data_dir)
        assert result.exit_code == 2
        assert any("id" in e for e in json.loads(result.output)["errors"])

    def test_add_no_arguments(self, data_dir: Path):
        result = invoke("add", data_dir=data_dir)
        assert result.exit_code == 2


# ── intake ────────────────────────────────────────────────────────────


class TestIntakeProtection:
    @pytest.mark.parametrize("mode", ["yes", "propose", "editor"])
    @pytest.mark.parametrize("explicit_policy", [True, False])
    def test_policy_preserves_owner_choice_or_accepts_proposal(
        self,
        tmp_project: Path,
        data_dir: Path,
        ledger: Ledger,
        monkeypatch,
        mode: str,
        explicit_policy: bool,
    ):
        template = (
            DRAFT.replace("status: draft", "status: draft\npolicy: review")
            if explicit_policy
            else DRAFT
        )
        path = write_task(tmp_project, template)
        expected = "review" if explicit_policy else "approve-plan-first"
        adapter = ScriptedPiAdapter(
            {
                "intake": PiScript(
                    final_text="```yaml\npolicy: approve-plan-first\nverify:\n- run: pytest -q\n```"
                )
            }
        )
        use_adapter(monkeypatch, adapter)
        if mode == "editor":
            monkeypatch.setattr(commands, "_is_tty", lambda: True)

            def edit(text: str, extension: str) -> str:
                assert parse_frontmatter(text)["frontmatter"]["policy"] == expected
                return text

            monkeypatch.setattr(commands.click, "edit", edit)
            result = invoke("intake", str(path), data_dir=data_dir)
        else:
            result = invoke("intake", str(path), f"--{mode}", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        written = parse_frontmatter(path.read_text())["frontmatter"]
        assert written["policy"] == expected
        assert written["status"] == ("draft" if mode == "propose" else "queued")
        note = "intake kept the file's policy: review"
        if mode == "editor":
            assert (note in result.output) is explicit_policy
        else:
            out = json.loads(result.output)
            assert out["proposal"]["policy"] == expected
            assert (note in out["notes"]) is explicit_policy
        (row,) = ledger.list_tasks()
        assert row["snapshot"] == path.read_text()
        assert len(adapter.calls) == 1

    @pytest.mark.parametrize("mode", ["yes", "propose", "editor"])
    @pytest.mark.parametrize("plan", [True, False])
    def test_explicit_plan_and_owner_fields_survive(
        self,
        tmp_project: Path,
        data_dir: Path,
        ledger: Ledger,
        monkeypatch,
        mode: str,
        plan: bool,
    ):
        protected = {
            "plan": plan,
            "base": "owner/base",
            "delivery": "pr",
            "priority": "high",
            "origin": {"kind": "fo", "session": "owner-session"},
        }
        fm = parse_frontmatter(DRAFT.format(project=tmp_project))["frontmatter"]
        fm.update(protected)
        path = write_task(tmp_project, serialize_frontmatter(fm, "Make the widget work."))
        proposal = {
            "verify": [{"name": "tests", "run": "pytest -q"}],
            "policy": "review",
            "plan": not plan,
            "base": "proposal/base",
            "delivery": "merge",
            "priority": "low",
            "origin": {"kind": "claude-code", "session": "proposal-session"},
        }
        adapter = ScriptedPiAdapter(
            {"intake": PiScript(final_text=f"```yaml\n{yaml.safe_dump(proposal)}```")}
        )
        use_adapter(monkeypatch, adapter)
        if mode == "editor":
            monkeypatch.setattr(commands, "_is_tty", lambda: True)
            monkeypatch.setattr(commands.click, "edit", lambda text, extension: text)
            result = invoke("intake", str(path), data_dir=data_dir)
        else:
            result = invoke("intake", str(path), f"--{mode}", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        written = parse_frontmatter(path.read_text())["frontmatter"]
        for key, value in protected.items():
            assert written[key] == value
        assert written["plan"] is plan
        assert written["status"] == ("draft" if mode == "propose" else "queued")
        assert ("intake" in written) is (mode != "propose")
        if mode != "editor":
            assert json.loads(result.output)["proposal"]["plan"] is plan
        (row,) = ledger.list_tasks()
        assert row["snapshot"] == path.read_text()
        assert len(adapter.calls) == 1

    def test_unspecified_plan_can_be_proposed(self, tmp_project: Path, data_dir: Path, monkeypatch):
        path = write_task(tmp_project, DRAFT)
        proposal = "```yaml\npolicy: review\nplan: true\nverify:\n- run: pytest -q\n```"
        use_adapter(monkeypatch, ScriptedPiAdapter({"intake": PiScript(final_text=proposal)}))
        result = invoke("intake", str(path), "--yes", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert parse_frontmatter(path.read_text())["frontmatter"]["plan"] is True

    def test_prompt_shows_explicit_false_and_override_guidance(self, tmp_project: Path):
        spec = parse_task(
            DRAFT.format(project=tmp_project).replace(
                "status: draft", "status: draft\nplan: false"
            ),
            tmp_project / "t.md",
            {},
        )
        prompt = build_intake_prompt(spec, {})
        assert "plan: false" in prompt
        assert "deviations from configured profiles" in prompt
        assert "Preserve existing explicit model choices" in prompt
        assert "Preserve an existing explicit plan boolean" in prompt
        assert (
            "`base`, `delivery`, `priority`, `depends_on`, `paths`, and `origin` are owner-controlled"
            in prompt
        )
        assert "default implementer is a cheap" not in prompt
        explicit_prompt = build_intake_prompt(replace(spec, policy="review"), {})
        assert (
            "`base`, `delivery`, `priority`, `depends_on`, `paths`, `origin`, and `policy` when the file sets it "
            "are owner-controlled"
        ) in explicit_prompt
        assert "Preserve the file's explicit policy" in explicit_prompt


class TestIntakeProportionateAcceptance:
    @staticmethod
    def current_block(prompt: str) -> dict:
        section = prompt.split("## Current acceptance block\n\n", 1)[1]
        return yaml.safe_load(section.split("```yaml\n", 1)[1].split("```", 1)[0])

    @pytest.mark.parametrize(
        ("budget_fm", "explicit", "expected"),
        [
            ({}, False, Budget(5.0, 90, 4)),
            ({"budget": {}}, True, Budget(5.0, 90, 4)),
            ({"budget": {"max_minutes": 30}}, True, Budget(5.0, 30, 4)),
            (
                {"budget": {"max_cost_usd": 2.5, "max_minutes": 30, "max_iterations": 3}},
                True,
                Budget(2.5, 30, 3),
            ),
        ],
    )
    def test_current_budget_visibility(self, tmp_project: Path, budget_fm, explicit, expected):
        config = {"budget": {"max_cost_usd": 5.0, "max_minutes": 90, "max_iterations": 4}}
        parsed = parse_frontmatter(DRAFT.format(project=tmp_project))
        spec = parse_task(
            serialize_frontmatter({**parsed["frontmatter"], **budget_fm}, parsed["body"]),
            tmp_project / "t.md",
            config,
        )
        assert spec.budget_explicit is explicit
        assert spec.budget == expected
        current = self.current_block(build_intake_prompt(spec, config))
        if explicit:
            assert current["budget"] == {
                "max_cost_usd": expected.max_cost_usd,
                "max_minutes": expected.max_minutes,
                "max_iterations": expected.max_iterations,
            }
        else:
            assert "budget" not in current
        assert spec.budget == expected

    def test_merged_budget_stays_explicit(self, tmp_project: Path, data_dir: Path, isolated_config):
        spec = parse_task(DRAFT.format(project=tmp_project), tmp_project / "t.md", isolated_config)
        assert spec.budget_explicit is False
        adapter = ScriptedPiAdapter(
            {"intake": PiScript(final_text=PROPOSAL.format(policy="review", isolation="process"))}
        )
        outcome = intake_logic.run_intake(
            spec,
            config=isolated_config,
            adapter=adapter,
            model=spec.models["intake"],
            data_dir=data_dir,
            allow_auto=False,
        )
        assert len(adapter.calls) == 1
        assert not outcome.errors
        assert outcome.spec.budget == Budget(2.5, 30, 3)
        assert outcome.spec.budget_explicit is True
        current = self.current_block(build_intake_prompt(outcome.spec, isolated_config))
        text = render_task(outcome.spec)
        fm = parse_frontmatter(text)["frontmatter"]
        assert (
            fm["budget"]
            == current["budget"]
            == {
                "max_cost_usd": 2.5,
                "max_minutes": 30,
                "max_iterations": 3,
            }
        )
        assert "budget_explicit" not in fm
        assert parse_task(text, spec.source_path, isolated_config) == outcome.spec

    @pytest.mark.parametrize("feedback", [None, "Use project notes for step durations."])
    def test_prompt_consistency_rules(self, tmp_project: Path, feedback):
        spec = parse_task(DRAFT.format(project=tmp_project), tmp_project / "t.md", {})
        prompt = build_intake_prompt(spec, {}, feedback=feedback)
        rules = (
            "Refer to the base commit as `$HELMRAISER_BASE_SHA`, never as a literal commit id.",
            "Give every step a timeout that fits it: the defaults are 300 seconds for verify and "
            "600 for evidence, and a full test suite usually needs more; follow the project notes "
            "when they state a duration.",
            "Run the project's own configured lint and type commands; never add lint rules the "
            "project does not configure.",
            "Together, the steps must check every part of the change the task describes.",
        )
        answer_format = prompt.split("## Answer format\n\n", 1)[1]
        for rule in rules:
            assert answer_format.count(f"- {rule}\n") == 1

    def test_prompt_describes_check_placement_and_budget(self, tmp_project: Path):
        spec = parse_task(DRAFT.format(project=tmp_project), tmp_project / "t.md", {})
        prompt = build_intake_prompt(spec, {})
        checks = (
            "Verify runs after every implementation attempt and must stay fast: the tests "
            "that cover the changed code, plus the project's lint or type checks when the "
            "task changes code. Put the project's full test suite (and its coverage gate) "
            "in evidence, which runs once after verify passes, never in verify. "
            "Do not repeat a check in both."
        )
        budget = (
            "budget: {max_cost_usd, max_minutes, max_iterations}, sized to the task: "
            "for size S propose max_cost_usd 3, max_minutes 60 and "
            "max_iterations 3; for M propose 6, 120 and 4; for L or unknown size, "
            "size it to the work. Preserve an existing budget."
        )
        for guidance in (checks, budget):
            assert guidance in prompt
            assert prompt.index(guidance) > prompt.index("## Answer format")

    @pytest.mark.parametrize("size", ["S", "M", "L"])
    def test_prompt_shows_task_size_before_body(self, tmp_project: Path, size: str):
        spec = parse_task(
            DRAFT.format(project=tmp_project).replace(
                "status: draft", f"status: draft\nsize: {size}"
            ),
            tmp_project / "t.md",
            {},
        )
        assert spec.extra["size"] == size
        prompt = build_intake_prompt(spec, {})
        section = prompt.split("## Task\n\n")[1].split("## Current acceptance block")[0]
        assert section == f"Task size: {size}\n\n{spec.body.strip()}\n\n"

    def test_prompt_omits_task_size_when_unspecified(self, tmp_project: Path):
        spec = parse_task(DRAFT.format(project=tmp_project), tmp_project / "t.md", {})
        prompt = build_intake_prompt(spec, {})
        assert "Task size:" not in prompt
        section = prompt.split("## Task\n\n")[1].split("## Current acceptance block")[0]
        assert section == f"{spec.body.strip()}\n\n"


class TestIntakeProjectEnvironment:
    @pytest.mark.parametrize(
        ("project_setup", "task_setup"),
        [
            ("python3 -m venv .venv", None),
            ("project-setup-sentinel", "task-setup-sentinel"),
            (None, "task-only-setup-sentinel"),
            (None, None),
        ],
    )
    def test_setup(self, tmp_project: Path, project_setup: str | None, task_setup: str | None):
        if project_setup is not None:
            project_toml(tmp_project, f'[defaults]\nsetup = "{project_setup}"\n')
        spec = parse_task(DRAFT.format(project=tmp_project), tmp_project / "t.md", {})
        prompt = build_intake_prompt(replace(spec, setup=task_setup), {})
        section = prompt.split("## Project environment\n\n")[1].split(
            "## Configured model profiles"
        )[0]
        assert prompt.index("## Project environment") < prompt.index("## Configured model profiles")
        setup = task_setup or project_setup
        if setup is not None:
            assert f"Effective setup command:\n\n```sh\n{setup}\n```" in section
            assert "The project has no setup command." not in section
        else:
            assert "The project has no setup command." in section
            assert "```" not in section
        if task_setup and project_setup:
            assert project_setup not in prompt
        assert "Environment variables applied to every step:" not in prompt
        assert (
            "Verify and evidence steps run from the repository root after setup, "
            "with no virtual environment activated. Call tools that setup installs by "
            "their path in the worktree (for example `.venv/bin/python` or "
            "`node_modules/.bin/vitest`) or through the project's own runner "
            "(for example `npm test`)."
        ) in prompt

    def test_environment_names_without_values(self, tmp_project: Path):
        project_toml(
            tmp_project,
            '[defaults.env]\nZETA = "project-zeta-secret"\nSHARED = "project-shared-secret"\n',
        )
        spec = parse_task(DRAFT.format(project=tmp_project), tmp_project / "t.md", {})
        prompt = build_intake_prompt(
            replace(spec, env={"ALPHA": "task-alpha-secret", "SHARED": "task-shared-secret"}),
            {},
        )
        assert ("Environment variables applied to every step: `ALPHA`, `SHARED`, `ZETA`.") in prompt
        assert prompt.count("`SHARED`") == 1
        for value in (
            "project-zeta-secret",
            "project-shared-secret",
            "task-alpha-secret",
            "task-shared-secret",
        ):
            assert value not in prompt

    def test_services_show_names_without_values(self, tmp_project: Path):
        spec = parse_task(DRAFT.format(project=tmp_project), tmp_project / "t.md", {})
        baseline = build_intake_prompt(spec, {})
        project_toml(
            tmp_project,
            """
[[services]]
name = "postgres"
image = "postgres:16"
port = 5432
env = { POSTGRES_PASSWORD = "service-postgres-secret" }
[services.provides]
ZETA_DATABASE_URL = "postgresql://user:zeta-secret@{host}:{port}/db"
DATABASE_URL = "postgresql://user:provided-password@{host}:{port}/db"

[[services]]
name = "redis"
image = "redis:7"
port = 6379
env = { REDIS_PASSWORD = "service-redis-secret" }
provides = { REDIS_URL = "redis://:redis-provided-secret@{host}:{port}/0" }
""",
        )
        prompt = build_intake_prompt(spec, {})
        section = prompt.split("## Project environment\n\n")[1].split(
            "## Configured model profiles"
        )[0]
        block = (
            "Declared services:\n\n"
            "- `postgres`: `DATABASE_URL`, `ZETA_DATABASE_URL`\n"
            "- `redis`: `REDIS_URL`\n\n"
            "Helmraiser starts these services before verify and evidence and sets the listed "
            "variables for every step. Use those variables; never start containers or services "
            "from a step.\n\n"
        )
        assert prompt.count(block) == 1
        assert block in section
        assert section.index(block) < section.index("Verify and evidence steps run")
        for hidden in (
            "postgres:16",
            "redis:7",
            "POSTGRES_PASSWORD",
            "REDIS_PASSWORD",
            "service-postgres-secret",
            "service-redis-secret",
            "postgresql://user:zeta-secret@{host}:{port}/db",
            "postgresql://user:provided-password@{host}:{port}/db",
            "redis://:redis-provided-secret@{host}:{port}/0",
            "zeta-secret",
            "provided-password",
            "redis-provided-secret",
        ):
            assert hidden not in prompt
        for port in ("5432", "6379"):
            assert port not in section
        assert prompt.replace(block, "", 1) == baseline

    @pytest.mark.parametrize("configuration", [None, "services = []\n"])
    def test_no_services_omits_service_context(self, tmp_project: Path, configuration: str | None):
        if configuration is not None:
            project_toml(tmp_project, configuration)
        spec = parse_task(DRAFT.format(project=tmp_project), tmp_project / "t.md", {})
        prompt = build_intake_prompt(spec, {})
        assert "Declared services:" not in prompt
        assert (
            "Helmraiser starts these services before verify and evidence and sets the listed "
            "variables for every step. Use those variables; never start containers or services "
            "from a step."
        ) not in prompt


class TestIntakeModels:
    @pytest.mark.parametrize("config", [{}, {"intake": {}}, {"intake": {"avoid_models": []}}])
    def test_default_prompt_has_no_avoidance(self, tmp_project: Path, config: dict):
        spec = parse_task(DRAFT.format(project=tmp_project), tmp_project / "t.md", config)
        prompt = build_intake_prompt(spec, config)
        assert "Never propose these models" not in prompt
        assert "opencode-go subscription" not in prompt

    def test_configured_avoidance_ends_prompt(self, tmp_project: Path):
        config = {"intake": {"avoid_models": ["foo/*", "bar/baz"]}}
        spec = parse_task(DRAFT.format(project=tmp_project), tmp_project / "t.md", config)
        prompt = build_intake_prompt(spec, config)
        assert prompt.rstrip().endswith("Never propose these models: foo/*, bar/baz.")
        assert prompt.count("Never propose these models") == 1

    def test_configured_avoidance_reaches_intake(
        self, tmp_project: Path, data_dir: Path, monkeypatch, isolated_config: dict
    ):
        isolated_config["intake"]["avoid_models"] = ["foo/*", "bar/baz"]
        path = write_task(
            tmp_project, DRAFT.replace("status: draft", "status: draft\npolicy: review")
        )
        adapter = ScriptedPiAdapter(
            {"intake": PiScript(final_text="```yaml\nverify:\n- run: pytest -q\n```")}
        )
        use_adapter(monkeypatch, adapter)
        result = invoke("intake", str(path), "--yes", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert (
            adapter.calls[0].prompt.rstrip().endswith("Never propose these models: foo/*, bar/baz.")
        )

    def test_runtime_prompt_describes_configured_profiles(
        self, tmp_project: Path, data_dir: Path, monkeypatch, isolated_config: dict
    ):
        profiles = {
            "implement": "custom/implement:high",
            "fallback": {"model": "custom/fallback", "thinking": "medium"},
            "review": "custom/review:xhigh",
            "plan": {"model": "custom/plan", "thinking": "low"},
        }
        isolated_config["profiles"].update(profiles)
        template = DRAFT.replace(
            "status: draft",
            "status: draft\npolicy: review\nmodels:\n  implement: owner/implement:high",
        )
        path = write_task(tmp_project, template)
        adapter = ScriptedPiAdapter(
            {"intake": PiScript(final_text="```yaml\nverify:\n- run: pytest -q\n```")}
        )
        use_adapter(monkeypatch, adapter)
        result = invoke("intake", str(path), "--yes", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        prompt = adapter.calls[0].prompt
        for role, model in resolve_models(None, isolated_config).items():
            if role in profiles:
                assert f"- {role}: {model.render()}" in prompt
        assert "owner/implement:high" in prompt
        assert "openai-codex/" not in prompt
        assert "Propose only deliberate\n  deviations from configured profiles" in prompt
        assert "omit `models:` when no deviation is needed" in prompt
        assert "Never propose these models" not in prompt
        assert "they exhaust the opencode-go subscription" not in prompt

    @pytest.mark.parametrize("mode", ["yes", "propose", "editor"])
    @pytest.mark.parametrize("custom_profiles", [False, True])
    @pytest.mark.parametrize(
        "scenario",
        [
            "omitted",
            "defaults",
            "deviation",
            "thinking",
            "original-deviation",
            "original-equal",
            "original-omitted",
            "empty",
            "non-mapping",
            "conflicting",
        ],
    )
    def test_sparse_model_choices(
        self,
        tmp_project: Path,
        data_dir: Path,
        ledger: Ledger,
        monkeypatch,
        isolated_config: dict,
        custom_profiles: bool,
        scenario: str,
        mode: str,
    ):
        if custom_profiles:
            isolated_config["profiles"] = {
                role: {"model": f"custom/{role}", "thinking": "high"}
                for role in isolated_config["profiles"]
            }
        profiles = {
            role: model.render() for role, model in resolve_models(None, isolated_config).items()
        }
        original = {}
        proposed = None
        expected = {}
        if scenario == "defaults":
            proposed = profiles
        elif scenario in ("deviation", "thinking"):
            value = (
                "other/implement:high"
                if scenario == "deviation"
                else profiles["implement"].split(":")[0] + ":low"
            )
            proposed = {"implement": value}
            expected = proposed
        elif scenario in (
            "original-deviation",
            "original-equal",
            "original-omitted",
            "empty",
            "non-mapping",
            "conflicting",
        ):
            original = {
                "implement": (
                    profiles["implement"]
                    if scenario == "original-equal"
                    else "owner/implement:high"
                )
            }
            expected = original
            proposed = {"review": profiles["review"]}
            if scenario == "original-omitted":
                proposed = None
            elif scenario == "empty":
                proposed = {}
            elif scenario == "non-mapping":
                proposed = "not a mapping"
            elif scenario == "conflicting":
                proposed = {"implement": "proposal/implement"}
        fm = parse_frontmatter(DRAFT.format(project=tmp_project))["frontmatter"]
        if original:
            fm["models"] = original
        path = write_task(tmp_project, serialize_frontmatter(fm, "Make the widget work."))
        proposal = "verify:\n- name: tests\n  run: pytest -q\npolicy: review\n"
        if proposed is not None:
            proposal += yaml.safe_dump({"models": proposed})
        adapter = ScriptedPiAdapter({"intake": PiScript(final_text=f"```yaml\n{proposal}```")})
        use_adapter(monkeypatch, adapter)
        if mode == "editor":
            monkeypatch.setattr(commands, "_is_tty", lambda: True)
            monkeypatch.setattr(commands.click, "edit", lambda text, extension: text)
            result = invoke("intake", str(path), data_dir=data_dir)
        else:
            result = invoke("intake", str(path), f"--{mode}", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        written = parse_frontmatter(path.read_text())["frontmatter"]
        assert written.get("models", {}) == expected
        assert ("models" in written) is bool(expected)
        (row,) = ledger.list_tasks()
        assert row["snapshot"] == path.read_text()
        assert len(adapter.calls) == 1

    def test_prompt_describes_project_profiles(self, tmp_project: Path):
        project_file = tmp_project / ".helmraiser" / "project.toml"
        project_file.parent.mkdir(parents=True, exist_ok=True)
        project_file.write_text('[models]\nimplement = "x/proj:medium"\n', encoding="utf-8")
        spec = parse_task(DRAFT.format(project=tmp_project), tmp_project / "t.md", {})

        prompt = build_intake_prompt(spec, {})

        assert "- implement: x/proj:medium" in prompt

    def test_project_profile_is_not_an_intake_override(self, tmp_project: Path):
        project_file = tmp_project / ".helmraiser" / "project.toml"
        project_file.parent.mkdir(parents=True, exist_ok=True)
        project_file.write_text('[models]\nimplement = "x/proj:medium"\n', encoding="utf-8")

        overrides = _intake_model_overrides(
            {},
            {"implement": "x/proj:medium", "review": "x/other:high"},
            {},
            tmp_project,
        )

        assert overrides == {"review": "x/other:high"}


class TestIntakeCheck:
    @pytest.mark.parametrize("kind", ["verify", "evidence"])
    @pytest.mark.parametrize(
        "code,hints,classification",
        [(1, [], "expected red"), (0, [], "fail"), (1, ["sandbox hint"], "sandbox")],
    )
    @pytest.mark.parametrize("as_json", [False, True])
    def test_marked_check_queue(
        self, tmp_project, data_dir, ledger, monkeypatch, kind, code, hints, classification, as_json
    ):
        def run(repo, steps, **kwargs):
            return {
                "commands": [
                    {
                        "name": step.name,
                        "red_on_base": step.red_on_base,
                        "exit_code": code if step.red_on_base else 0,
                        "hints": hints if step.red_on_base else [],
                        "tail": "existing README.md check",
                    }
                    for step in steps
                ]
            }

        monkeypatch.setattr(intake_logic, "run_preflight_steps", run)
        use_adapter(monkeypatch, ScriptedPiAdapter({}))
        content = COMPLETE.replace(
            "policy: review\n",
            "policy: review\nevidence:\n  - name: evidence\n    run: echo evidence\n    artifacts: [out.txt]\n    criteria: works\n",
        )
        fm = parse_frontmatter(content.format(project=tmp_project))["frontmatter"]
        fm[kind][0]["red_on_base"] = True
        path = write_task(tmp_project, serialize_frontmatter(fm, "Check existing README.md."))
        original = path.read_bytes()
        if classification != "expected red":
            self.no_ledger(monkeypatch)
        result = invoke(
            "intake",
            str(path),
            "--check",
            "--yes",
            *(["--json"] if as_json else []),
            data_dir=data_dir,
        )
        assert result.exit_code == (0 if classification == "expected red" else 2), result.output
        if as_json:
            out = json.loads(result.output)
            step = next(
                s
                for s in out["check"]["steps"]
                if s["name"] == ("tests" if kind == "verify" else "evidence")
            )
            assert step["class"] == classification
            text = step["tail"]
        else:
            assert classification in result.output
            text = result.output
        if code == 0:
            assert "declared red on base but passed on the unchanged code" in text
        if classification == "expected red":
            saved = parse_frontmatter(path.read_text())["frontmatter"]
            assert saved[kind][0]["red_on_base"] is True
            assert saved["status"] == "queued"
            assert saved["intake"]["approved_at"]
        else:
            assert path.read_bytes() == original

    @staticmethod
    def preflight(monkeypatch, *classes):
        payload = {
            "commands": [
                {
                    "name": "tests" if n == 0 else "evidence",
                    "exit_code": 0 if value == "green" else 1,
                    "seconds": 0.25,
                    "output": "ERROR: file or directory not found: tests/new.py",
                    "tail": "[literal] diagnostic",
                    "hints": ["sandbox hint"] if value == "sandbox" else [],
                }
                for n, value in enumerate(classes)
            ]
        }
        monkeypatch.setattr(intake_logic, "run_preflight_steps", lambda *a, **kw: payload)
        return payload

    @staticmethod
    def no_ledger(monkeypatch):
        monkeypatch.setattr(commands, "_ledger", lambda *a, **kw: pytest.fail("ledger accessed"))

    @pytest.mark.parametrize("classification", ["green", "expected red"])
    def test_pass_queues_and_stamps(
        self, tmp_project, data_dir, ledger, monkeypatch, classification
    ):
        self.preflight(monkeypatch, classification)
        path = write_task(tmp_project, COMPLETE + "\nCreate `tests/new.py`.\n")
        adapter = ScriptedPiAdapter({})
        use_adapter(monkeypatch, adapter)
        result = invoke("intake", str(path), "--check", "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["check"]["summary"] == classification
        assert out["check"]["steps"][0]["class"] == classification
        assert ledger.get_task(out["task_id"])["status"] == "queued"
        fm = parse_frontmatter(path.read_text())["frontmatter"]
        assert fm["status"] == "queued"
        assert fm["intake"]["approved_at"].endswith("Z")
        assert adapter.calls == []

    @pytest.mark.parametrize("kind", ["verify", "evidence"])
    @pytest.mark.parametrize("classification", ["fail", "sandbox"])
    @pytest.mark.parametrize("as_json", [False, True])
    def test_failure_blocks_queue(
        self, tmp_project, data_dir, monkeypatch, kind, classification, as_json
    ):
        self.preflight(
            monkeypatch,
            *([classification, "green"] if kind == "verify" else ["green", classification]),
        )
        self.no_ledger(monkeypatch)
        use_adapter(monkeypatch, ScriptedPiAdapter({}))
        path = write_task(
            tmp_project,
            COMPLETE.replace(
                "policy: review\n",
                "policy: review\nevidence:\n  - name: evidence\n    run: echo evidence\n"
                "    artifacts: [out.txt]\n    criteria: works\n",
            ),
        )
        original = path.read_bytes()
        result = invoke(
            "intake",
            str(path),
            "--check",
            "--yes",
            *(["--json"] if as_json else []),
            data_dir=data_dir,
        )
        assert result.exit_code == 2, result.output
        if as_json:
            out = json.loads(result.output)
            assert out["error"] == "acceptance check failed"
            assert out["check"]["summary"] == f"{classification}(1)"
            assert [step["class"] for step in out["check"]["steps"]] == (
                [classification, "green"] if kind == "verify" else ["green", classification]
            )
            for step in out["check"]["steps"]:
                assert set(step) == {"name", "exit_code", "seconds", "class", "hint", "tail"}
                assert isinstance(step["seconds"], (float, int))
        else:
            name = "tests" if kind == "verify" else "evidence"
            assert f"{name}: {classification}" in result.output
            assert ("evidence: green" if kind == "verify" else "tests: green") in result.output
            assert "[literal] diagnostic" in result.output
            if classification == "sandbox":
                assert "sandbox hint" in result.output
        assert path.read_bytes() == original
        assert not data_dir.exists()

    @pytest.mark.parametrize("classification", ["fail", "sandbox"])
    def test_proposed_check_failure_does_not_retry(
        self, tmp_project, data_dir, monkeypatch, classification
    ):
        self.preflight(monkeypatch, classification, "green")
        self.no_ledger(monkeypatch)
        adapter = ScriptedPiAdapter(
            {
                "intake": [
                    PiScript(final_text=PROPOSAL.format(policy="review", isolation="process")),
                    PiScript(final_text="must not be asked"),
                ]
            }
        )
        use_adapter(monkeypatch, adapter)
        path = write_task(tmp_project, DRAFT)
        original = path.read_bytes()
        result = invoke("intake", str(path), "--check", "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 2, result.output
        out = json.loads(result.output)
        assert out["error"] == "acceptance check failed"
        assert out["check"]["summary"] == f"{classification}(1)"
        assert len(adapter.calls) == 1
        assert "## Failed acceptance check" not in adapter.calls[0].prompt
        assert path.read_bytes() == original
        assert not data_dir.exists()

    @pytest.mark.parametrize("as_json", [False, True])
    def test_check_only_is_ledger_and_editor_free(
        self, tmp_project, data_dir, monkeypatch, as_json
    ):
        self.preflight(monkeypatch, "green")
        self.no_ledger(monkeypatch)
        monkeypatch.setattr(commands, "_is_tty", lambda: False)
        monkeypatch.setattr(
            commands, "_edit_proposal", lambda *a, **kw: pytest.fail("editor opened")
        )
        adapter = ScriptedPiAdapter({})
        use_adapter(monkeypatch, adapter)
        path = write_task(tmp_project, COMPLETE)
        original = path.read_bytes()
        result = invoke(
            "intake", str(path), "--check", *(["--json"] if as_json else []), data_dir=data_dir
        )
        assert result.exit_code == 0, result.output
        assert path.read_bytes() == original
        assert not data_dir.exists()
        assert adapter.calls == []
        if as_json:
            out = json.loads(result.output)
            assert out["task_id"].endswith(":fix-widget")
            assert out["proposal"] is None
            assert out["status"] == "draft"
            assert out["path"] == str(path)
            assert out["task"]
            assert out["notes"]
            assert out["check"] == {
                "summary": "green",
                "error": None,
                "steps": [
                    {
                        "name": "tests",
                        "exit_code": 0,
                        "seconds": 0.25,
                        "class": "green",
                        "hint": None,
                        "tail": "[literal] diagnostic",
                    }
                ],
            }
        else:
            assert "tests: green" in result.output

    @pytest.mark.parametrize("red_on_base", [False, True])
    @pytest.mark.parametrize("failure", [False, True])
    @pytest.mark.parametrize("propose", [False, True])
    def test_check_only_model_proposal_saves_unstamped_draft(
        self, tmp_project, data_dir, monkeypatch, failure, propose, red_on_base
    ):
        self.preflight(monkeypatch, "fail" if failure else "green")
        self.no_ledger(monkeypatch)
        path = write_task(tmp_project, DRAFT)
        proposal = PROPOSAL.format(policy="review", isolation="process")
        if red_on_base:
            proposal = proposal.replace(
                "    timeout: 120", "    timeout: 120\n    red_on_base: true"
            )
        adapter = ScriptedPiAdapter({"intake": PiScript(final_text=proposal)})
        use_adapter(monkeypatch, adapter)
        result = invoke(
            "intake",
            str(path),
            "--check",
            "--json",
            *(["--propose"] if propose else []),
            data_dir=data_dir,
        )
        assert result.exit_code == (2 if failure else 0), result.output
        out = json.loads(result.output)
        assert out["proposal"]["verify"]
        assert out["check"]["summary"] == ("fail(1)" if failure else "green")
        assert len(adapter.calls) == 1
        assert adapter.calls[0].stage == "intake"
        fm = parse_frontmatter(path.read_text())["frontmatter"]
        assert fm["status"] == "draft"
        assert "intake" not in fm
        assert [step["name"] for step in fm["verify"]] == ["tests", "lint"]
        assert fm["verify"][0].get("red_on_base", False) is red_on_base
        assert not (data_dir / "helmraiser.db").exists()

    def test_proposal_failed_queue_check_leaves_original(self, tmp_project, data_dir, monkeypatch):
        self.preflight(monkeypatch, "fail")
        self.no_ledger(monkeypatch)
        path = write_task(tmp_project, DRAFT)
        original = path.read_bytes()
        adapter = ScriptedPiAdapter(
            {"intake": PiScript(final_text=PROPOSAL.format(policy="review", isolation="process"))}
        )
        use_adapter(monkeypatch, adapter)
        result = invoke("intake", str(path), "--check", "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 2, result.output
        assert json.loads(result.output)["check"]["summary"] == "fail(1)"
        assert len(adapter.calls) == 1
        assert path.read_bytes() == original
        assert not (data_dir / "helmraiser.db").exists()

    @pytest.mark.parametrize(
        "error", ["setup: exit 1", "process isolation is unavailable: missing srt"]
    )
    @pytest.mark.parametrize("as_json", [False, True])
    def test_preflight_start_failure(self, tmp_project, data_dir, monkeypatch, error, as_json):
        self.no_ledger(monkeypatch)
        use_adapter(monkeypatch, ScriptedPiAdapter({}))
        path = write_task(tmp_project, COMPLETE)
        original = path.read_bytes()

        def run(*args, **kwargs):
            if error.startswith("setup"):
                return {"error": error, "commands": []}
            raise PreflightError(error)

        monkeypatch.setattr(intake_logic, "run_preflight_steps", run)
        result = invoke(
            "intake", str(path), "--check", *(["--json"] if as_json else []), data_dir=data_dir
        )
        assert result.exit_code == 2, result.output
        if as_json:
            out = json.loads(result.output)
            assert out["error"] == "acceptance check failed"
            assert out["check"] == {"summary": "fail(1)", "error": error, "steps": []}
        else:
            assert error in result.output
        assert path.read_bytes() == original
        assert not data_dir.exists()

    def test_propose_yes_stays_invalid(self, tmp_project, data_dir, monkeypatch):
        self.no_ledger(monkeypatch)
        path = write_task(tmp_project, COMPLETE)
        result = invoke("intake", str(path), "--check", "--propose", "--yes", data_dir=data_dir)
        assert result.exit_code == 2
        assert "--propose and --yes are exclusive" in result.output

    def test_without_flag_never_executes_check(self, tmp_project, data_dir, monkeypatch):
        monkeypatch.setattr(commands, "check_acceptance", lambda *a, **kw: pytest.fail("checked"))
        use_adapter(monkeypatch, ScriptedPiAdapter({}))
        path = write_task(tmp_project, COMPLETE)
        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert "check" not in json.loads(result.output)

    @pytest.mark.parametrize("refusal", ["caps", "active"])
    def test_downstream_refusal_preserves_check(self, tmp_project, data_dir, monkeypatch, refusal):
        self.preflight(monkeypatch, "green")
        use_adapter(monkeypatch, ScriptedPiAdapter({}))
        path = write_task(tmp_project, COMPLETE)
        if refusal == "caps":
            monkeypatch.setattr(commands, "never_startable", lambda *a: {"reason": "over cap"})
            monkeypatch.setattr(commands, "never_startable_text", lambda *a: "will never start")
            monkeypatch.setattr(commands, "caps_enforcement", lambda *a: (True, "test"))
        else:

            def active(*args, **kwargs):
                raise commands.TaskActive("already active")

            monkeypatch.setattr(Ledger, "upsert_task", active)
        result = invoke("intake", str(path), "--check", "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 2, result.output
        out = json.loads(result.output)
        assert out["check"]["summary"] == "green"
        assert (
            "refusing to queue" in out["error"] if refusal == "caps" else "active" in out["error"]
        )

    @pytest.mark.parametrize("kind", ["verify", "evidence"])
    @pytest.mark.parametrize("queue", [False, True])
    def test_marked_existing_file_executes_on_base(
        self, tmp_project, data_dir, isolated_config, intake_srt_log, monkeypatch, kind, queue
    ):
        use_adapter(monkeypatch, ScriptedPiAdapter({}))
        if not queue:
            self.no_ledger(monkeypatch)
        step = "  - name: change-check\n    run: test ! -f README.md\n    red_on_base: true\n"
        content = COMPLETE.replace("    run: pytest -q", "    run: 'true'")
        if kind == "verify":
            content = content.replace("  - name: tests\n    run: 'true'\n", step)
        else:
            content = content.replace(
                "policy: review\n",
                "policy: review\nevidence:\n"
                + step
                + "    artifacts: [out.txt]\n    criteria: works\n",
            )
        path = write_task(tmp_project, content)
        original = path.read_bytes()
        result = invoke(
            "intake",
            str(path),
            "--check",
            "--json",
            *(["--yes"] if queue else []),
            data_dir=data_dir,
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["check"]["summary"] == "expected red"
        assert srt_calls(intake_srt_log)
        if not queue:
            assert path.read_bytes() == original

    def test_cli_executes_on_head_with_setup_and_cleanup(
        self, tmp_project, tmp_path, data_dir, isolated_config, intake_srt_log, monkeypatch
    ):
        self.no_ledger(monkeypatch)
        adapter = ScriptedPiAdapter({})
        use_adapter(monkeypatch, adapter)
        temp_root = tmp_path / "temporary"
        temp_root.mkdir()
        isolated_config.setdefault("isolation", {})["tmpdir"] = str(temp_root)
        project_toml(tmp_project, '[defaults]\nsetup = "echo setup > setup.txt"\n')
        (tmp_project / "README.md").write_text("dirty primary content\n")
        path = write_task(
            tmp_project,
            COMPLETE.replace(
                "    run: pytest -q",
                '    run: test -f setup.txt && test "$(cat README.md)" = "# Test Project" && echo verify > verify.txt\n'
                "    timeout: 7\nevidence:\n  - name: proof\n"
                '    run: test -f verify.txt && echo proof > "$HELMRAISER_EVIDENCE_DIR/proof.txt"\n'
                "    timeout: 19\n    artifacts: [proof.txt]\n    criteria: works",
            ),
        )
        original = path.read_bytes()
        seen = []
        run_step = preflight_logic.run_step

        def recording(step, *args, **kwargs):
            seen.append((step.name, step.timeout))
            return run_step(step, *args, **kwargs)

        monkeypatch.setattr(preflight_logic, "run_step", recording)
        result = invoke("intake", str(path), "--check", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["check"]["summary"] == "green"
        assert seen == [("tests", 7), ("proof", 19)]
        assert [step["class"] for step in out["check"]["steps"]] == ["green", "green"]
        assert all(isinstance(step["seconds"], (int, float)) for step in out["check"]["steps"])
        calls = srt_calls(intake_srt_log)
        assert [Path(call["argv"][1]).name for call in calls] == [
            "srt-settings-setup.json",
            "srt-settings-verify.json",
            "srt-settings-verify.json",
        ]
        worktree = Path(calls[0]["cwd"])
        assert worktree != tmp_project
        assert {call["cwd"] for call in calls} == {str(worktree)}
        assert (tmp_project / "README.md").read_text() == "dirty primary content\n"
        assert not (tmp_project / "setup.txt").exists()
        assert not (tmp_project / "verify.txt").exists()
        assert path.read_bytes() == original
        assert not Path(calls[0]["argv"][1]).parent.exists()
        assert list(temp_root.iterdir()) == []
        assert adapter.calls == []
        assert_cleaned_up(tmp_project, data_dir, worktree)


class TestIntakeYes:
    def test_proposal_is_merged_written_and_queued(
        self, tmp_project: Path, data_dir: Path, ledger: Ledger, monkeypatch
    ):
        path = write_task(tmp_project, DRAFT)
        adapter = ScriptedPiAdapter(
            {"intake": PiScript(final_text=PROPOSAL.format(policy="review", isolation="none"))}
        )
        use_adapter(monkeypatch, adapter)

        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "queued"
        assert out["proposal"]["policy"] == "review"

        fm = parse_frontmatter(path.read_text())["frontmatter"]
        assert fm["status"] == "queued"
        assert fm["policy"] == "review"
        assert [v["name"] for v in fm["verify"]] == ["tests", "lint"]
        assert fm["verify"][0]["timeout"] == 120
        assert fm["budget"] == {"max_cost_usd": 2.5, "max_minutes": 30, "max_iterations": 3}
        assert "models" not in fm
        assert fm["intake"]["model"] == "openai-codex/gpt-6-astra:high"
        assert fm["intake"]["approved_at"].endswith("Z")
        assert "Make the widget work." in path.read_text()

        row = ledger.get_task(out["task_id"])
        assert row is not None
        assert row["status"] == "queued" and row["queued_at"] is not None
        assert row["snapshot"] == path.read_text()
        assert row["policy"] == "review"

        (req,) = adapter.calls
        assert req.stage == "intake"
        assert req.cwd == tmp_project
        assert req.tools == ("read", "grep", "find", "ls")
        assert req.model.render() == "openai-codex/gpt-6-astra:high"
        assert req.session_id.endswith("-intake")
        # The session writes to a throwaway dir: the intake sandbox's only write root.
        assert req.session_dir.parent.name.startswith("helmraiser-intake-")
        assert not req.session_dir.parent.exists()
        assert "Make the widget work." in req.prompt
        assert "fenced ```yaml block" in req.prompt

    def test_proposed_criteria_list_is_saved_as_one_string(
        self, tmp_project: Path, data_dir: Path, monkeypatch
    ):
        path = write_task(tmp_project, DRAFT)
        proposal = PROPOSAL.format(policy="review", isolation="process").replace(
            "evidence: []\n",
            "evidence:\n  - name: help\n    run: app --help\n    artifacts: [help.txt]\n"
            "    criteria: ['Lists the new flag.', 'Exits 0.']\n",
        )
        use_adapter(monkeypatch, ScriptedPiAdapter({"intake": PiScript(final_text=proposal)}))

        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        fm = parse_frontmatter(path.read_text())["frontmatter"]
        assert fm["evidence"][0]["criteria"] == "Lists the new flag. Exits 0."

    def test_model_option_overrides_profile(self, tmp_project: Path, data_dir: Path, monkeypatch):
        path = write_task(tmp_project, DRAFT)
        adapter = ScriptedPiAdapter(
            {"intake": PiScript(final_text=PROPOSAL.format(policy="review", isolation="none"))}
        )
        use_adapter(monkeypatch, adapter)
        result = invoke("intake", str(path), "--yes", "--model", "x/y:low", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert adapter.calls[0].model.render() == "x/y:low"
        assert parse_frontmatter(path.read_text())["frontmatter"]["intake"]["model"] == "x/y:low"
        assert result.output.strip().endswith("queued")

    def test_auto_proposal_is_downgraded_without_allow_auto(
        self, tmp_project: Path, data_dir: Path, monkeypatch
    ):
        path = write_task(tmp_project, DRAFT)
        use_adapter(
            monkeypatch,
            ScriptedPiAdapter(
                {"intake": PiScript(final_text=PROPOSAL.format(policy="auto", isolation="process"))}
            ),
        )
        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["proposal"]["policy"] == "review"
        assert any("--allow-auto" in n for n in out["notes"])
        assert parse_frontmatter(path.read_text())["frontmatter"]["policy"] == "review"

    def test_allow_auto_keeps_auto_but_validates_it(
        self, tmp_project: Path, data_dir: Path, monkeypatch
    ):
        path = write_task(tmp_project, DRAFT)
        # auto with process isolation but no evidence → validation error, nothing written
        use_adapter(
            monkeypatch,
            ScriptedPiAdapter(
                {"intake": PiScript(final_text=PROPOSAL.format(policy="auto", isolation="process"))}
            ),
        )
        before = path.read_text()
        result = invoke("intake", str(path), "--yes", "--allow-auto", "--json", data_dir=data_dir)
        assert result.exit_code == 2, result.output
        assert "evidence" in " ".join(json.loads(result.output)["errors"])
        assert path.read_text() == before

    def test_intake_refuses_auto_task_on_require_approval_project(
        self, tmp_project: Path, data_dir: Path, monkeypatch
    ) -> None:
        project_file = tmp_project / ".helmraiser" / "project.toml"
        project_file.parent.mkdir(exist_ok=True)
        project_file.write_text("[defaults]\nrequire_approval = true\n")
        path = write_task(
            tmp_project, DRAFT.replace("status: draft", "status: draft\npolicy: auto")
        )
        adapter = ScriptedPiAdapter(
            {"intake": PiScript(final_text=PROPOSAL.format(policy="auto", isolation="process"))}
        )
        use_adapter(monkeypatch, adapter)
        before = path.read_text()

        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)

        assert result.exit_code == 2, result.output
        assert "project requires approval: policy auto is not allowed" in result.output
        assert adapter.calls == []
        assert path.read_text() == before

    def test_intake_ignores_allow_auto_on_require_approval_project(
        self, tmp_project: Path, data_dir: Path, ledger: Ledger, monkeypatch
    ) -> None:
        project_file = tmp_project / ".helmraiser" / "project.toml"
        project_file.parent.mkdir(exist_ok=True)
        project_file.write_text("[defaults]\nrequire_approval = true\n")
        path = write_task(tmp_project, DRAFT)
        use_adapter(
            monkeypatch,
            ScriptedPiAdapter(
                {"intake": PiScript(final_text=PROPOSAL.format(policy="review", isolation="none"))}
            ),
        )

        result = invoke("intake", str(path), "--yes", "--allow-auto", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert "project requires approval; --allow-auto ignored" in out["notes"]
        assert ledger.get_task(out["task_id"])["policy"] == "review"

    def test_invalid_proposal_exits_2_and_leaves_file(
        self, tmp_project: Path, data_dir: Path, ledger: Ledger, monkeypatch
    ):
        path = write_task(tmp_project, DRAFT)
        bad = PROPOSAL.format(policy="review", isolation="none").replace(
            "review: openai-codex/gpt-6-astra:high", "review: openai-codex/gpt-6.1-sol:high"
        )
        use_adapter(monkeypatch, ScriptedPiAdapter({"intake": PiScript(final_text=bad)}))
        before = path.read_text()
        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 2
        assert any("reviewer must differ" in e for e in json.loads(result.output)["errors"])
        assert path.read_text() == before
        assert ledger.list_tasks() == []

    def test_adapter_failure_exits_1(self, tmp_project: Path, data_dir: Path, monkeypatch):
        path = write_task(tmp_project, DRAFT)
        use_adapter(
            monkeypatch,
            ScriptedPiAdapter(
                {"intake": PiScript(ok=False, error_kind="quota", error_message="429 slow down")}
            ),
        )
        result = invoke("intake", str(path), "--yes", data_dir=data_dir)
        assert result.exit_code == 1
        assert "quota" in result.output and "429" in result.output

    def test_broken_usage_contract_says_contract(
        self, tmp_project: Path, data_dir: Path, fake_pi, monkeypatch
    ):
        from helmraiser.pi.adapter import PiJsonAdapter

        path = write_task(tmp_project, DRAFT)
        assistant = {"role": "assistant", "content": [], "stopReason": "stop"}
        fake_pi.write_script(
            {
                "default": {
                    "events": [
                        {"type": "agent_start"},
                        {"type": "message_end", "message": assistant},
                        {"type": "agent_settled"},
                    ]
                }
            }
        )
        use_adapter(monkeypatch, PiJsonAdapter())
        result = invoke("intake", str(path), "--yes", data_dir=data_dir)
        assert result.exit_code == 1
        output = " ".join(result.output.split())
        assert "intake session failed (contract): pi reported no readable usage" in output
        assert "protocol" not in output

    def test_answer_without_yaml_block_exits_1(
        self, tmp_project: Path, data_dir: Path, monkeypatch
    ):
        path = write_task(tmp_project, DRAFT)
        use_adapter(monkeypatch, ScriptedPiAdapter({"intake": PiScript(final_text="no yaml")}))
        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 1
        assert "fenced yaml" in json.loads(result.output)["error"]

    def test_complete_file_does_not_ask_the_model(
        self, tmp_project: Path, data_dir: Path, ledger: Ledger, monkeypatch
    ):
        adapter = ScriptedPiAdapter({"intake": PiScript(final_text="never used")})
        use_adapter(monkeypatch, adapter)
        path = write_task(tmp_project, COMPLETE)
        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert adapter.calls == []
        assert out["proposal"] is None
        assert any("already carries a valid acceptance block" in n for n in out["notes"])
        assert ledger.get_task(out["task_id"])["status"] == "queued"

    def test_real_adapter_is_wired_from_config(self, isolated_config: dict):
        from helmraiser.pi.adapter import PiJsonAdapter

        isolated_config["pi_bin"] = "/opt/pi/bin/pi"
        adapter = intake_logic.get_intake_adapter(isolated_config)
        assert isinstance(adapter, PiJsonAdapter)
        assert adapter.pi_bin == "/opt/pi/bin/pi"
        assert adapter.argv_wrapper is not None

    def test_without_adapter_complete_file_is_queued(
        self, tmp_project: Path, data_dir: Path, ledger: Ledger, monkeypatch
    ):
        use_adapter(monkeypatch, None)
        path = write_task(tmp_project, COMPLETE)
        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["proposal"] is None
        assert any("no intake adapter" in n for n in out["notes"])
        fm = parse_frontmatter(path.read_text())["frontmatter"]
        assert fm["status"] == "queued"
        assert "approved_at" in fm["intake"]
        row = ledger.get_task(out["task_id"])
        assert row is not None and row["status"] == "queued"
        assert ledger.next_queued()["task_id"] == out["task_id"]

    def test_without_adapter_incomplete_file_exits_2(
        self, tmp_project: Path, data_dir: Path, monkeypatch
    ):
        use_adapter(monkeypatch, None)
        path = write_task(tmp_project, DRAFT)
        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 2
        errors = json.loads(result.output)["errors"]
        assert "policy is required for a queued task" in errors
        assert any("verify" in e for e in errors)

    def test_without_yes_prints_proposal_and_exits_1(
        self, tmp_project: Path, data_dir: Path, ledger: Ledger, monkeypatch
    ):
        path = write_task(tmp_project, DRAFT)
        use_adapter(
            monkeypatch,
            ScriptedPiAdapter(
                {"intake": PiScript(final_text=PROPOSAL.format(policy="review", isolation="none"))}
            ),
        )
        before = path.read_text()
        result = invoke("intake", str(path), data_dir=data_dir)
        assert result.exit_code == 1
        assert "--yes" in result.output
        assert "name: lint" in result.output
        assert path.read_text() == before
        assert ledger.list_tasks() == []

    def test_missing_file(self, tmp_path: Path, data_dir: Path):
        result = invoke("intake", str(tmp_path / "nope.md"), "--yes", data_dir=data_dir)
        assert result.exit_code == 1

    def test_propose_and_yes_are_exclusive(self, tmp_project: Path, data_dir: Path):
        path = write_task(tmp_project, DRAFT)
        result = invoke("intake", str(path), "--propose", "--yes", data_dir=data_dir)
        assert result.exit_code == 2
        assert "--propose and --yes are exclusive" in result.output

    def test_propose_saves_draft_and_queues_from_proposed_file(
        self, tmp_project: Path, data_dir: Path, ledger: Ledger, monkeypatch
    ):
        path = write_task(tmp_project, DRAFT)
        adapter = ScriptedPiAdapter(
            {"intake": PiScript(final_text=PROPOSAL.format(policy="review", isolation="none"))}
        )
        use_adapter(monkeypatch, adapter)

        result = invoke("intake", str(path), "--propose", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["id"] == "fix-widget"
        assert out["status"] == "draft"
        assert out["path"] == str(path)
        assert out["proposal"]["policy"] == "review"
        assert "Make the widget work." in out["task"]
        assert out["task_id"].endswith(":fix-widget")
        assert isinstance(out["notes"], list)
        assert len(adapter.calls) == 1
        assert adapter.calls[0].stage == "intake"

        fm = parse_frontmatter(path.read_text())["frontmatter"]
        assert fm["status"] == "draft"
        assert "intake" not in fm
        assert [v["name"] for v in fm["verify"]] == ["tests", "lint"]
        assert fm["verify"][0]["timeout"] == 120
        assert fm["budget"] == {"max_cost_usd": 2.5, "max_minutes": 30, "max_iterations": 3}
        assert fm["policy"] == "review"
        proposed = fm

        row = ledger.get_task(out["task_id"])
        assert row is not None
        assert row["status"] == "draft" and row["queued_at"] is None
        assert row["snapshot"] == path.read_text()
        assert ledger.next_queued() is None

        # --yes on the proposed file queues it exactly as written, without the adapter
        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "queued"
        assert out["proposal"] is None
        assert len(adapter.calls) == 1, "intake model must not be asked again"
        assert any("already carries a valid acceptance block" in n for n in out["notes"])

        fm = parse_frontmatter(path.read_text())["frontmatter"]
        assert fm["status"] == "queued"
        assert fm["intake"]["approved_at"].endswith("Z")
        # Everything but the approval itself is exactly what was proposed.
        queued = {k: v for k, v in fm.items() if k not in ("status", "intake")}
        assert queued == {k: v for k, v in proposed.items() if k != "status"}

        row = ledger.get_task(out["task_id"])
        assert row is not None
        assert row["status"] == "queued" and row["queued_at"] is not None
        assert row["snapshot"] == path.read_text()

    def test_propose_invalid_file_is_untouched(
        self, tmp_project: Path, data_dir: Path, ledger: Ledger, monkeypatch
    ):
        path = write_task(tmp_project, DRAFT)
        bad = PROPOSAL.format(policy="review", isolation="none").replace(
            "review: openai-codex/gpt-6-astra:high", "review: openai-codex/gpt-6.1-sol:high"
        )
        use_adapter(monkeypatch, ScriptedPiAdapter({"intake": PiScript(final_text=bad)}))
        before = path.read_text()
        result = invoke("intake", str(path), "--propose", "--json", data_dir=data_dir)
        assert result.exit_code == 2
        assert any("reviewer must differ" in e for e in json.loads(result.output)["errors"])
        assert path.read_text() == before
        assert ledger.list_tasks() == []


class TestIsolationFloor:
    """Intake keeps the file's isolation when the proposal is weaker (loader default: process)."""

    UNPOLICYED = """---
id: fix-widget
title: Fix the widget
project: {project}
status: draft
verify:
  - name: tests
    run: pytest -q
---
## Goal

Make the widget work.
"""

    def propose(
        self,
        tmp_project: Path,
        data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        task_template: str,
        proposal_isolation: str,
    ) -> object:
        path = write_task(tmp_project, task_template)
        use_adapter(
            monkeypatch,
            ScriptedPiAdapter(
                {
                    "intake": PiScript(
                        final_text=PROPOSAL.format(policy="review", isolation=proposal_isolation)
                    )
                }
            ),
        )
        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        fm = parse_frontmatter(path.read_text())["frontmatter"]
        return out, fm

    def test_proposal_none_with_missing_key_keeps_default_process(
        self, tmp_project: Path, data_dir: Path, monkeypatch
    ):
        out, fm = self.propose(tmp_project, data_dir, monkeypatch, DRAFT, "none")
        assert fm["isolation"] == "process"
        assert "proposal asked for isolation none; kept process" in out["notes"]

    def test_proposal_none_for_explicit_process_keeps_process(
        self, tmp_project: Path, data_dir: Path, monkeypatch
    ):
        task = self.UNPOLICYED.replace("status: draft", "status: draft\nisolation: process")
        out, fm = self.propose(tmp_project, data_dir, monkeypatch, task, "none")
        assert fm["isolation"] == "process"
        assert any("proposal asked for isolation none; kept process" in n for n in out["notes"])

    def test_proposal_vm_for_process_is_kept(self, tmp_project: Path, data_dir: Path, monkeypatch):
        task = self.UNPOLICYED.replace("status: draft", "status: draft\nisolation: process")
        out, fm = self.propose(tmp_project, data_dir, monkeypatch, task, "vm")
        assert fm["isolation"] == "vm"
        assert not any("isolation" in n for n in out["notes"])

    def test_proposal_none_for_explicit_none_keeps_none_without_note(
        self, tmp_project: Path, data_dir: Path, monkeypatch
    ):
        task = self.UNPOLICYED.replace("status: draft", "status: draft\nisolation: none")
        out, fm = self.propose(tmp_project, data_dir, monkeypatch, task, "none")
        assert fm["isolation"] == "none"
        assert not any("isolation" in n for n in out["notes"])

    def test_intake_prompt_says_process_is_the_default(self) -> None:
        spec = TaskSpec(
            id="t",
            title="T",
            project=Path("."),
            priority="normal",
            status="draft",
            policy=None,
            isolation="process",
            models={},
            budget=Budget(0, 0, 0),
            verify=(),
            evidence=(),
            tags=(),
            body="body",
            source_path=Path("t.md"),
        )
        prompt = build_intake_prompt(spec, {})
        assert "process (the default sandbox)" in prompt
        assert "none or process" not in prompt
        assert "no commit counts or order" in prompt
        assert "Never set `sandbox` on a verify or evidence step" in prompt

    def test_project_notes_constrain_intake_prompt_without_duplication(
        self, tmp_project: Path
    ) -> None:
        notes = "Use node --import tsx, never tsx --test."
        spec = TaskSpec(
            id="t",
            title="T",
            project=tmp_project,
            priority="normal",
            status="draft",
            policy=None,
            isolation="process",
            models={},
            budget=Budget(0, 0, 0),
            verify=(),
            evidence=(),
            tags=(),
            body="body",
            source_path=tmp_project / "t.md",
        )

        notes_introduction = (
            "These are the project's own rules for verify and evidence steps, budgets "
            "and timeouts. Where they differ from the general rules below, follow these:"
        )
        old_introduction = "These are constraints on proposed verify and evidence commands:"
        budget_guidance = "Preserve an existing budget. Project notes override this guidance."
        prompt = build_intake_prompt(spec, {})
        assert notes_introduction not in prompt
        assert "## Owner-maintained project notes" not in prompt
        assert old_introduction not in prompt
        assert budget_guidance in prompt

        notes_path = tmp_project / ".helmraiser" / "notes.md"
        notes_path.parent.mkdir(exist_ok=True)
        notes_path.write_text(notes, encoding="utf-8")
        prompt = build_intake_prompt(spec, {})
        assert "owner-maintained project notes" in prompt.lower()
        assert notes_introduction in prompt
        assert budget_guidance in prompt
        assert old_introduction not in prompt
        assert prompt.count(notes) == 1
        assert "## Project notes (Helmraiser)" in prompt

        prompt = build_intake_prompt(replace(spec, body=f"body\n\n{notes}"), {})
        assert prompt.count(notes) == 1
        assert "## Owner-maintained project notes" not in prompt


class TestStepSandboxFloor:
    """Intake never proposes ``sandbox: host``: only the owner opts a step out of srt."""

    HOST_PROPOSAL = (
        PROPOSAL.format(policy="review", isolation="process")
        .replace("    run: ruff check src\n", "    run: ruff check src\n    sandbox: host\n")
        .replace(
            "evidence: []\n",
            "evidence:\n  - name: shots\n    run: capture\n    artifacts: [a.png]\n"
            "    criteria: shows it\n    sandbox: host\n",
        )
    )

    @pytest.mark.parametrize("require_approval", [False, True])
    def test_proposed_sandbox_host_is_removed_with_a_note(
        self, tmp_project: Path, data_dir: Path, monkeypatch, require_approval: bool
    ):
        if require_approval:  # even where the owner could set it by hand
            (tmp_project / ".helmraiser").mkdir(exist_ok=True)
            (tmp_project / ".helmraiser" / "project.toml").write_text(
                "[defaults]\nrequire_approval = true\n"
            )
        path = write_task(tmp_project, DRAFT)
        use_adapter(
            monkeypatch,
            ScriptedPiAdapter({"intake": PiScript(final_text=self.HOST_PROPOSAL)}),
        )
        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        notes = json.loads(result.output)["notes"]
        assert (
            "proposal asked for sandbox: host on verify step lint; removed "
            "(steps run in the sandbox)"
        ) in notes
        assert any("sandbox: host on evidence step shots; removed" in n for n in notes)
        fm = parse_frontmatter(path.read_text())["frontmatter"]
        assert [step["name"] for step in fm["verify"]] == ["tests", "lint"]
        assert not any("sandbox" in step for step in [*fm["verify"], *fm["evidence"]])

    def test_the_owners_own_host_step_is_kept_when_the_model_is_not_asked(
        self, tmp_project: Path, data_dir: Path, monkeypatch
    ):
        (tmp_project / ".helmraiser").mkdir(exist_ok=True)
        (tmp_project / ".helmraiser" / "project.toml").write_text(
            "[defaults]\nrequire_approval = true\n"
        )
        path = write_task(
            tmp_project,
            COMPLETE.replace("    run: pytest -q\n", "    run: pytest -q\n    sandbox: host\n"),
        )
        adapter = ScriptedPiAdapter({"intake": PiScript(final_text=self.HOST_PROPOSAL)})
        use_adapter(monkeypatch, adapter)
        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        assert adapter.calls_for("intake") == []
        fm = parse_frontmatter(path.read_text())["frontmatter"]
        assert fm["verify"][0]["sandbox"] == "host"

    def test_a_host_step_without_require_approval_is_refused(
        self, tmp_project: Path, data_dir: Path, monkeypatch
    ):
        path = write_task(
            tmp_project,
            COMPLETE.replace("    run: pytest -q\n", "    run: pytest -q\n    sandbox: host\n"),
        )
        use_adapter(monkeypatch, None)
        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)

        assert result.exit_code == 2, result.output
        errors = json.loads(result.output)["errors"]
        assert any("needs require_approval = true" in e for e in errors)


class TestLargeBudgetWarnings:
    @pytest.mark.parametrize("cost", [19.99, 20.0, 25.0])
    @pytest.mark.parametrize("mode", [["--check"], ["--yes"], ["--check", "--yes"]])
    @pytest.mark.parametrize("as_json", [False, True])
    def test_note_on_check_and_queue(self, tmp_project, data_dir, monkeypatch, cost, mode, as_json):
        monkeypatch.setenv("COLUMNS", "120")
        path = write_task(
            tmp_project,
            COMPLETE.replace(
                "policy: review\n", f"policy: review\nbudget:\n  max_cost_usd: {cost}\n"
            ),
        )
        original = path.read_bytes()
        adapter = ScriptedPiAdapter({})
        use_adapter(monkeypatch, adapter)
        if "--check" in mode:
            TestIntakeCheck.preflight(monkeypatch, "green")
        check_only = "--yes" not in mode
        if check_only:
            TestIntakeCheck.no_ledger(monkeypatch)
        result = invoke(
            "intake", str(path), *mode, *(["--json"] if as_json else []), data_dir=data_dir
        )
        assert result.exit_code == 0, result.output
        expected = (
            f"budget.max_cost_usd is ${cost:g}; tasks budgeted at $20 or more often fail: "
            "consider splitting this task into smaller tasks linked with depends_on"
        )
        if as_json:
            payload = json.loads(result.output)
            notes = [n for n in payload["notes"] if n.startswith("budget.max_cost_usd is ")]
            assert notes == ([] if cost < 20 else [expected])
            assert payload["status"] == ("draft" if check_only else "queued")
        else:
            output = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", result.output).split())
            if cost < 20:
                assert "budget.max_cost_usd is " not in output
            else:
                assert output.count(expected) == 1
        saved = parse_task(path.read_text(), path, {})
        assert saved.budget.max_cost_usd == cost
        if check_only:
            assert path.read_bytes() == original
            assert not data_dir.exists()
        else:
            assert saved.status == "queued"
            ledger = Ledger(data_dir / "helmraiser.db")
            assert ledger.get_task(saved.task_id)["status"] == "queued"
        assert adapter.calls == []

    def test_proposed_budget_is_used(self, tmp_project, data_dir, ledger, monkeypatch):
        path = write_task(tmp_project, DRAFT)
        proposal = PROPOSAL.format(policy="review", isolation="process").replace(
            "max_cost_usd: 2.5", "max_cost_usd: 25"
        )
        adapter = ScriptedPiAdapter({"intake": PiScript(final_text=proposal)})
        use_adapter(monkeypatch, adapter)
        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert [n for n in payload["notes"] if n.startswith("budget.max_cost_usd is ")] == [
            "budget.max_cost_usd is $25; tasks budgeted at $20 or more often fail: "
            "consider splitting this task into smaller tasks linked with depends_on"
        ]
        assert payload["status"] == "queued"
        saved = parse_task(path.read_text(), path, {})
        assert saved.status == "queued" and saved.budget.max_cost_usd == 25
        assert ledger.get_task(saved.task_id)["status"] == "queued"
        assert len(adapter.calls) == 1
        assert adapter.calls[0].stage == "intake"

    @pytest.mark.parametrize("collect_scope_warnings", [False, True])
    def test_run_intake_collection_flag(
        self, tmp_project, data_dir, isolated_config, collect_scope_warnings
    ):
        path = write_task(
            tmp_project,
            COMPLETE.replace("policy: review\n", "policy: review\nbudget:\n  max_cost_usd: 25\n"),
        )
        spec = parse_task(path.read_text(), path, isolated_config)
        outcome = intake_logic.run_intake(
            spec,
            config=isolated_config,
            adapter=None,
            model=None,
            data_dir=data_dir,
            allow_auto=False,
            collect_scope_warnings=collect_scope_warnings,
        )
        assert outcome.errors == []
        notes = [n for n in outcome.notes if n.startswith("budget.max_cost_usd is ")]
        assert notes == (
            [
                "budget.max_cost_usd is $25; tasks budgeted at $20 or more often fail: "
                "consider splitting this task into smaller tasks linked with depends_on"
            ]
            if collect_scope_warnings
            else []
        )


class TestBodyOutsidePaths:
    NOTE = (
        "body mentions files outside paths: tests/test_old.py"
        " — add them to paths if the task edits them"
    )

    @pytest.fixture
    def task(self, tmp_project):
        (tmp_project / "tests").mkdir()
        (tmp_project / "tests/test_old.py").write_text("def test_x(): pass\n")
        subprocess.run(["git", "-C", str(tmp_project), "add", "tests/test_old.py"], check=True)
        return write_task(
            tmp_project,
            COMPLETE.replace("policy: review\n", "policy: review\npaths: [src/a.py]\n")
            + "\nEdit tests/test_old.py.\n",
        )

    @pytest.mark.parametrize("mode", ["yes", "propose", "editor"])
    def test_note_surfaces_without_changing_declared_paths(
        self, task, data_dir, ledger, monkeypatch, mode
    ):
        use_adapter(monkeypatch, None)
        printed = []
        if mode == "editor":
            monkeypatch.setattr(commands, "_is_tty", lambda: True)
            real_print = commands.console.print

            def record(*args, **kwargs):
                printed.append(" ".join(str(a) for a in args))
                real_print(*args, **kwargs)

            monkeypatch.setattr(commands.console, "print", record)

            def fake_edit(text, extension):
                assert any(self.NOTE in line for line in printed)
                return text

            monkeypatch.setattr(commands.click, "edit", fake_edit)
            options = []
        else:
            options = [f"--{mode}", "--json"]

        result = invoke("intake", str(task), *options, data_dir=data_dir)

        assert result.exit_code == 0, result.output
        status = "draft" if mode == "propose" else "queued"
        if mode == "editor":
            assert sum(self.NOTE in line for line in printed) == 1
        else:
            payload = json.loads(result.output)
            assert payload["notes"].count(self.NOTE) == 1
            assert payload["status"] == status
        saved = parse_task(task.read_text(), task, {})
        assert saved.status == status and saved.paths == ("src/a.py",)
        (row,) = ledger.list_tasks()
        assert parse_task(row["snapshot"], task, {}).paths == ("src/a.py",)

    @pytest.mark.parametrize(
        "body,expected",
        [
            ("Evidence:\n- tests/test_old.py\n", False),
            ("## Project notes (Owner)\ntests/test_old.py\n", False),
            (
                "## Change\nEdit tests/test_old.py.\n\nEvidence:\n- tests/test_old.py\n\n## Project notes (Owner)\ntests/test_old.py",
                True,
            ),
        ],
    )
    def test_excluded_body_sections(self, task, data_dir, ledger, monkeypatch, body, expected):
        use_adapter(monkeypatch, None)
        task.write_text(task.read_text().replace("Edit tests/test_old.py.", body))
        result = invoke("intake", str(task), "--propose", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["notes"].count(self.NOTE) == int(expected)
        assert payload["status"] == "draft"
        (row,) = ledger.list_tasks()
        for text in (task.read_text(), row["snapshot"]):
            spec = parse_task(text, task, {})
            assert spec.paths == ("src/a.py",) and spec.status == "draft"

    def test_add_does_not_run_body_warning_checks(self, task, data_dir, ledger, monkeypatch):
        def unexpected(*args, **kwargs):
            pytest.fail("add must not scan body paths")

        monkeypatch.setattr("helmraiser.task.structural.body_paths_outside_scope", unexpected)

        result = invoke("add", str(task), "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        (row,) = ledger.list_tasks()
        saved = parse_task(row["snapshot"], task, {})
        assert saved.status == "draft" and saved.paths == ("src/a.py",)
        assert parse_task(task.read_text(), task, {}).paths == ("src/a.py",)


def test_related_test_metadata_and_prompt(tmp_project, isolated_config, data_dir):
    from helmraiser.loop.prompts import initial_prompt, paths_block
    from helmraiser.task.loader import render_task

    path = tmp_project / "task.md"
    spec = parse_task(
        COMPLETE.format(project=tmp_project).replace(
            "status: draft\n",
            "status: draft\npaths: [src/a.py, tests/test_a.py]\n"
            "intake:\n  related_tests_added: [tests/test_a.py, tests/stale.py]\n",
        ),
        path,
        isolated_config,
    )
    outcome = intake_logic.run_intake(
        spec,
        config=isolated_config,
        adapter=None,
        model=None,
        data_dir=data_dir,
        allow_auto=False,
    )
    roundtrip = parse_task(render_task(outcome.spec), path, isolated_config)
    assert roundtrip.extra["intake"]["related_tests_added"] == [
        "tests/test_a.py",
        "tests/stale.py",
    ]
    prompt = initial_prompt(roundtrip)
    assert "- `tests/test_a.py` (related test, added by intake)" in prompt
    assert "- `src/a.py`\n" in prompt
    assert "tests/stale.py" not in prompt
    assert "permitted scope, not required edits" in prompt
    assert "keeping each assertion at least as strict" in prompt
    ordinary = replace(spec, extra={})
    assert paths_block(ordinary) == (
        "## Allowed paths\n\n- `src/a.py`\n- `tests/test_a.py`\n\n"
        "Changes outside these globs fail before verify. Restore/remove forbidden "
        "changes; do not broaden the declaration.\n\n"
    )


class TestRelatedTestsOutsidePaths:
    SOURCE = "src/widgets/icon-symbol.tsx"
    NOTE = (
        "tests outside paths exercise in-scope files: tests/other.test.tsx"
        " — add them to paths if the change may need to update them"
    )

    @pytest.fixture
    def task(self, tmp_project):
        for name, text in {
            self.SOURCE: "export const IconSymbol = 1;\n",
            "tests/other.test.tsx": "import { IconSymbol } from '../src/widgets/icon-symbol';\n",
        }.items():
            path = tmp_project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        subprocess.run(["git", "-C", str(tmp_project), "add", self.SOURCE, "tests/"], check=True)
        return write_task(
            tmp_project,
            COMPLETE.replace("policy: review\n", f"policy: review\npaths: [{self.SOURCE}]\n"),
        )

    @pytest.mark.parametrize("mode", ["propose", "editor"])
    def test_note_surfaces_without_changing_declared_paths(
        self, task, data_dir, ledger, monkeypatch, mode
    ):
        use_adapter(monkeypatch, None)
        printed = []
        if mode == "editor":
            monkeypatch.setattr(commands, "_is_tty", lambda: True)
            real_print = commands.console.print

            def record(*args, **kwargs):
                printed.append(" ".join(str(a) for a in args))
                real_print(*args, **kwargs)

            monkeypatch.setattr(commands.console, "print", record)

            def fake_edit(text, extension):
                assert any(self.NOTE in line for line in printed)
                return text

            monkeypatch.setattr(commands.click, "edit", fake_edit)
            options = []
        else:
            options = [f"--{mode}", "--json"]

        result = invoke("intake", str(task), *options, data_dir=data_dir)

        assert result.exit_code == 0, result.output
        status = "draft" if mode == "propose" else "queued"
        if mode == "editor":
            assert sum(self.NOTE in line for line in printed) == 1
        else:
            payload = json.loads(result.output)
            assert payload["notes"].count(self.NOTE) == 1
            assert payload["status"] == status
        saved = parse_task(task.read_text(), task, {})
        assert saved.status == status and saved.paths == (self.SOURCE,)
        (row,) = ledger.list_tasks()
        assert parse_task(row["snapshot"], task, {}).paths == (self.SOURCE,)

    @pytest.mark.parametrize("check", [False, True])
    @pytest.mark.parametrize("as_json", [False, True])
    def test_yes_adds_and_renders_task(self, task, data_dir, ledger, monkeypatch, check, as_json):
        from helmraiser.task.loader import render_task

        use_adapter(monkeypatch, None)
        TestIntakeCheck.preflight(monkeypatch, "green")
        original = parse_task(task.read_text(), task, {})
        result = invoke(
            "intake",
            str(task),
            "--yes",
            *(["--check"] if check else []),
            *(["--json"] if as_json else []),
            data_dir=data_dir,
        )
        assert result.exit_code == 0, result.output
        document = task.read_bytes().decode()
        fm = parse_frontmatter(document)["frontmatter"]
        saved = parse_task(document, task, {})
        assert saved == replace(
            original,
            status="queued",
            paths=(self.SOURCE, "tests/other.test.tsx"),
            extra={"intake": fm["intake"]},
            budget_explicit=True,
        )
        assert document == render_task(saved)
        assert fm["intake"]["related_tests_added"] == ["tests/other.test.tsx"]
        (row,) = ledger.list_tasks()
        assert row["snapshot"] == document
        if as_json:
            payload = json.loads(result.output)
            assert payload["related_tests_added"] == ["tests/other.test.tsx"]
            assert payload["related_tests_would_add"] == []
            assert payload["related_tests_count"] == 1
            assert self.NOTE not in payload["notes"]
        else:
            assert "Added related tests to paths: tests/other.test.tsx" in result.output
            assert "add them to paths if" not in result.output
        # A repeat keeps the metadata and cannot append a second copy.
        repeat = invoke("intake", str(task), "--yes", "--json", data_dir=data_dir)
        assert repeat.exit_code == 0, repeat.output
        assert json.loads(repeat.output)["related_tests_added"] == []
        saved = parse_task(task.read_text(), task, {})
        assert saved.paths.count("tests/other.test.tsx") == 1
        assert saved.extra["intake"]["related_tests_added"] == ["tests/other.test.tsx"]

    @pytest.mark.parametrize("as_json", [False, True])
    @pytest.mark.parametrize("count", [1, 20, 21])
    def test_check_only_preview(self, task, data_dir, monkeypatch, as_json, count):
        use_adapter(monkeypatch, None)
        TestIntakeCheck.preflight(monkeypatch, "green")
        TestIntakeCheck.no_ledger(monkeypatch)
        names = [f"tests/other{n}.test.tsx" for n in range(count)]
        monkeypatch.setattr(commands, "related_tests_outside_scope", lambda *a: names)
        original = task.read_bytes()
        result = invoke(
            "intake",
            str(task),
            "--check",
            *(["--json"] if as_json else []),
            data_dir=data_dir,
        )
        assert result.exit_code == 0, result.output
        assert task.read_bytes() == original
        assert not data_dir.exists()
        if as_json:
            payload = json.loads(result.output)
            assert payload["related_tests_added"] == []
            assert payload["related_tests_would_add"] == (names if count <= 20 else [])
            assert payload["related_tests_count"] == count
            notes = " ".join(payload["notes"])
        else:
            notes = result.output
        if count <= 20:
            assert "Would add related tests to paths:" in notes
        else:
            assert "21 total; none automatically added: limit is 20" in notes
            assert "add them to paths if" in notes

    @pytest.mark.parametrize("as_json", [False, True])
    @pytest.mark.parametrize("ledger_status", ["queued", "running"])
    def test_check_preview_is_conditional_on_unchecked_ledger_state(
        self, task, data_dir, ledger, monkeypatch, as_json, ledger_status
    ):
        from tests.ledger.test_db import run_row

        use_adapter(monkeypatch, None)
        TestIntakeCheck.preflight(monkeypatch, "green")
        original = task.read_bytes()
        spec = parse_task(original.decode(), task, {})
        ledger.upsert_task(replace(spec, status=ledger_status), original.decode())
        if ledger_status == "running":
            ledger.create_run(run_row("holder", spec.task_id, str(spec.project)))
        snapshot = ledger.get_task(spec.task_id)
        with monkeypatch.context() as check_only:
            TestIntakeCheck.no_ledger(check_only)
            result = invoke(
                "intake",
                str(task),
                "--check",
                *(["--json"] if as_json else []),
                data_dir=data_dir,
            )
        assert result.exit_code == 0, result.output
        assert task.read_bytes() == original
        assert ledger.get_task(spec.task_id) == snapshot
        if as_json:
            payload = json.loads(result.output)
            assert payload["related_tests_would_add"] == ["tests/other.test.tsx"]
            assert payload["related_tests_added"] == []
            notes = " ".join(payload["notes"])
        else:
            notes = " ".join(result.output.split())
        assert "Would add related tests to paths:" in notes
        assert "additions require a task that is neither queued nor active in the ledger" in notes
        assert "check-only does not inspect ledger state" in notes
        actual = invoke("intake", str(task), "--yes", "--json", data_dir=data_dir)
        assert actual.exit_code == (0 if ledger_status == "queued" else 2), actual.output
        assert json.loads(actual.output).get("related_tests_added", []) == []
        assert parse_task(task.read_text(), task, {}).paths == (self.SOURCE,)
        assert parse_task(ledger.get_task(spec.task_id)["snapshot"], task, {}).paths == (
            self.SOURCE,
        )

    def test_final_scope_warnings_and_metadata_merge(self, task, data_dir, monkeypatch):
        use_adapter(monkeypatch, None)
        task.write_text(
            task.read_text()
            .replace("    run: pytest -q", "    run: pytest tests/other.test.tsx")
            .replace(
                "status: draft\n",
                "status: draft\nintake:\n  related_tests_added: [tests/old.py, tests/old.py]\n",
            )
            + "\nUpdate tests/other.test.tsx only if necessary.\n"
        )
        result = invoke("intake", str(task), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert not any("outside paths" in note for note in payload["notes"])
        assert parse_task(task.read_text(), task, {}).extra["intake"]["related_tests_added"] == [
            "tests/old.py",
            "tests/other.test.tsx",
        ]

    @pytest.mark.parametrize("count", [20, 21])
    def test_all_or_none_cap(self, task, data_dir, ledger, monkeypatch, count):
        use_adapter(monkeypatch, None)
        repo = task.parent.parent
        # Use the real detector, including its sorting and tracked-file rules.
        for n in range(count - 1):
            (repo / f"tests/extra{n}.test.tsx").write_text(
                "import { IconSymbol } from '../src/widgets/icon-symbol';\n"
            )
        subprocess.run(["git", "-C", str(repo), "add", "tests/"], check=True)
        result = invoke("intake", str(task), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["related_tests_count"] == count
        saved = parse_task(task.read_text(), task, {})
        if count == 20:
            assert len(payload["related_tests_added"]) == 20
            assert saved.paths == (self.SOURCE, *payload["related_tests_added"])
        else:
            assert payload["related_tests_added"] == []
            assert saved.paths == (self.SOURCE,)
            assert "21 total; none automatically added: limit is 20" in " ".join(payload["notes"])
            assert "related_tests_added" not in saved.extra["intake"]
        assert ledger.list_tasks()[0]["snapshot"] == task.read_text()

    @pytest.mark.parametrize("scope", ["absent", "unmatched", "covered"])
    def test_no_additions(self, task, data_dir, monkeypatch, scope):
        use_adapter(monkeypatch, None)
        paths = {
            "absent": "",
            "unmatched": "paths: [missing.ts]\n",
            "covered": f"paths: [{self.SOURCE}, tests/]\n",
        }[scope]
        task.write_text(task.read_text().replace(f"paths: [{self.SOURCE}]\n", paths))
        result = invoke("intake", str(task), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["related_tests_added"] == []
        assert payload["related_tests_count"] == 0
        fm = parse_frontmatter(task.read_text())["frontmatter"]
        assert "related_tests_added" not in fm["intake"]
        if scope == "absent":
            assert "paths" not in fm

    @pytest.mark.parametrize("queued_in", ["file", "ledger", "both"])
    def test_existing_queued_scope_unchanged(self, task, data_dir, ledger, monkeypatch, queued_in):
        use_adapter(monkeypatch, None)
        if queued_in != "file":
            spec = parse_task(task.read_text(), task, {})
            ledger.upsert_task(replace(spec, status="queued"), task.read_text())
        if queued_in != "ledger":
            task.write_text(task.read_text().replace("status: draft", "status: queued"))
        result = invoke("intake", str(task), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["related_tests_added"] == []
        assert parse_task(task.read_text(), task, {}).paths == (self.SOURCE,)
        assert parse_task(ledger.list_tasks()[0]["snapshot"], task, {}).paths == (self.SOURCE,)

    @pytest.mark.parametrize("refusal", ["check", "active", "registration", "caps"])
    def test_refusal_leaves_original_scope(self, task, data_dir, ledger, monkeypatch, refusal):
        from tests.ledger.test_db import run_row

        use_adapter(monkeypatch, None)
        original = task.read_bytes()
        spec = parse_task(task.read_text(), task, {})
        ledger.upsert_task(spec, original.decode())
        snapshot = ledger.get_task(spec.task_id)
        if refusal == "check":
            TestIntakeCheck.preflight(monkeypatch, "fail")
            TestIntakeCheck.no_ledger(monkeypatch)
        elif refusal == "active":
            ledger.create_run(run_row("holder", spec.task_id, str(spec.project)))
        elif refusal == "caps":
            monkeypatch.setattr(commands, "never_startable", lambda *a: {"reason": "over cap"})
            monkeypatch.setattr(commands, "never_startable_text", lambda *a: "will never start")
            monkeypatch.setattr(commands, "caps_enforcement", lambda *a: (True, "test"))
        else:

            def refuse(*args, **kwargs):
                raise commands.TaskActive("raced with registration")

            monkeypatch.setattr(Ledger, "upsert_task", refuse)
        result = invoke(
            "intake",
            str(task),
            "--yes",
            "--json",
            *(["--check"] if refusal == "check" else []),
            data_dir=data_dir,
        )
        assert result.exit_code == 2, result.output
        assert task.read_bytes() == original
        assert ledger.get_task(spec.task_id) == snapshot
        assert json.loads(result.output).get("related_tests_added", []) == []

    def test_added_test_serializes_sibling(
        self, task, data_dir, ledger, monkeypatch, isolated_config
    ):
        from helmraiser.task.loader import render_task
        from tests.ledger.test_db import make_spec, run_row

        use_adapter(monkeypatch, None)
        isolated_config["max_runs_per_project"] = 2
        sibling = make_spec(
            task.parent.parent, "sibling", paths=("tests/other.test.tsx",), status="running"
        )
        ledger.upsert_task(sibling, render_task(sibling))
        ledger.create_run(run_row("holder", sibling.task_id, str(sibling.project)))
        result = invoke("intake", str(task), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["related_tests_added"] == ["tests/other.test.tsx"]
        result = invoke("queue", "next", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["task"] is None
        assert payload["held"][0]["reason"] == "paths_overlap"
        assert payload["held"][0]["other_task_id"] == "sibling"

    def test_add_does_not_scan_related_tests(self, task, data_dir, ledger, monkeypatch):
        def unexpected(*args, **kwargs):
            pytest.fail("add must not scan related tests")

        monkeypatch.setattr("helmraiser.task.structural.related_tests_outside_scope", unexpected)
        result = invoke("add", str(task), "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        (row,) = ledger.list_tasks()
        assert parse_task(row["snapshot"], task, {}).paths == (self.SOURCE,)
        assert parse_task(task.read_text(), task, {}).paths == (self.SOURCE,)

    def test_note_with_scripted_acceptance(self, task, tmp_project, data_dir, ledger, monkeypatch):
        task.write_text(
            DRAFT.format(project=tmp_project).replace(
                "status: draft\n", f"status: draft\npaths: [{self.SOURCE}]\n"
            )
        )
        adapter = ScriptedPiAdapter(
            {"intake": PiScript(final_text=PROPOSAL.format(policy="review", isolation="process"))}
        )
        use_adapter(monkeypatch, adapter)
        result = invoke("intake", str(task), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert self.NOTE not in json.loads(result.output)["notes"]
        assert json.loads(result.output)["related_tests_added"] == ["tests/other.test.tsx"]
        assert parse_task(task.read_text(), task, {}).paths == (self.SOURCE, "tests/other.test.tsx")
        (row,) = ledger.list_tasks()
        assert row["snapshot"] == task.read_text()
        assert parse_task(row["snapshot"], task, {}).paths == (self.SOURCE, "tests/other.test.tsx")
        assert len(adapter.calls) == 1


class TestStructuralSteps:
    """Intake flags, never drops, proposed steps that inspect source text."""

    STRUCTURAL_PROPOSAL = (
        PROPOSAL.format(policy="review", isolation="process")
        .replace(
            "    run: ruff check src\n",
            "    run: ruff check src\n"
            "  - name: exports\n"
            "    run: grep -q 'export function widget' src/widget.ts\n",
        )
        .replace(
            "evidence: []\n",
            "evidence:\n"
            "  - name: shape\n"
            "    run: node -e \"assert.match(readFileSync('src/widget.ts', 'utf8'), /x/)\"\n"
            "    artifacts: []\n"
            "    criteria: exports widget\n"
            "  - name: output\n"
            '    run: pytest -q > "$HELMRAISER_EVIDENCE_DIR/out.txt"\n'
            "    artifacts: [out.txt]\n"
            "    criteria: tests pass\n",
        )
    )
    VERIFY_NOTE = (
        "verify step exports looks like a structure check (greps src/widget.ts): verify "
        "and evidence should run the code, not inspect source files; review it before queueing"
    )

    def test_yes_queues_with_a_note_per_structural_step(
        self, tmp_project: Path, data_dir: Path, ledger: Ledger, monkeypatch
    ):
        path = write_task(tmp_project, DRAFT)
        use_adapter(
            monkeypatch,
            ScriptedPiAdapter({"intake": PiScript(final_text=self.STRUCTURAL_PROPOSAL)}),
        )

        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "queued"
        structural = [n for n in out["notes"] if "structure check" in n]
        assert structural == [
            self.VERIFY_NOTE,
            "evidence step shape looks like a structure check (reads src/widget.ts): verify "
            "and evidence should run the code, not inspect source files; review it before "
            "queueing",
        ]
        fm = parse_frontmatter(path.read_text())["frontmatter"]
        assert [step["name"] for step in fm["verify"]] == ["tests", "lint", "exports"]
        assert [step["name"] for step in fm["evidence"]] == ["shape", "output"]

    def test_propose_prints_the_note(self, tmp_project: Path, data_dir: Path, monkeypatch):
        path = write_task(tmp_project, DRAFT)
        use_adapter(
            monkeypatch,
            ScriptedPiAdapter({"intake": PiScript(final_text=self.STRUCTURAL_PROPOSAL)}),
        )

        result = invoke("intake", str(path), "--propose", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        assert "verify step exports looks like a structure check" in " ".join(result.output.split())

    def test_without_yes_off_a_terminal_the_notes_are_shown(
        self, tmp_project: Path, data_dir: Path, monkeypatch
    ):
        path = write_task(tmp_project, DRAFT)
        use_adapter(
            monkeypatch,
            ScriptedPiAdapter({"intake": PiScript(final_text=self.STRUCTURAL_PROPOSAL)}),
        )

        as_json = invoke("intake", str(path), "--json", data_dir=data_dir)

        assert as_json.exit_code == 1
        assert self.VERIFY_NOTE in json.loads(as_json.output)["notes"]

    def test_the_editor_path_shows_the_note_before_the_editor_opens(
        self, tmp_project: Path, data_dir: Path, monkeypatch
    ):
        path = write_task(tmp_project, DRAFT)
        use_adapter(
            monkeypatch,
            ScriptedPiAdapter({"intake": PiScript(final_text=self.STRUCTURAL_PROPOSAL)}),
        )
        monkeypatch.setattr(commands, "_is_tty", lambda: True)
        printed: list[str] = []
        real_print = commands.console.print

        def record(*args, **kwargs):
            printed.append(" ".join(str(a) for a in args))
            real_print(*args, **kwargs)

        monkeypatch.setattr(commands.console, "print", record)

        def fake_edit(text: str, extension: str) -> str:
            assert any("verify step exports looks like a structure check" in p for p in printed)
            return text

        monkeypatch.setattr(commands.click, "edit", fake_edit)

        result = invoke("intake", str(path), data_dir=data_dir)

        assert result.exit_code == 0, result.output
        assert sum("verify step exports" in p for p in printed) == 1

    def test_intake_prompt_forbids_structure_checks(self) -> None:
        spec = TaskSpec(
            id="t",
            title="T",
            project=Path("."),
            priority="normal",
            status="draft",
            policy=None,
            isolation="process",
            models={},
            budget=Budget(0, 0, 0),
            verify=(),
            evidence=(),
            tags=(),
            body="body",
            source_path=Path("t.md"),
        )
        prompt = build_intake_prompt(spec, {})
        assert "never read, grep, parse or assert on source files" in prompt
        assert "documentation is the reviewer's job" in prompt
        assert (
            "Exception: when the task changes only documentation files (Markdown or plain "
            "text, not help strings or other text inside code), one step may check that the stale "
            "text the task removes no longer appears in that document, for example "
            "`! grep -n 'old wording' README.md`."
        ) in prompt
        instruction = "Set `red_on_base: true` on a step that checks the task's change against a file that already exists, such as the stale-text check above or an assertion on current command output, so it must fail before the change. Leave it unset on every other step."
        assert instruction in prompt
        assert prompt.index(instruction) > prompt.index("Exception:")
        assert "{name, run, timeout, red_on_base (optional)}" in prompt
        assert "{name, run, artifacts, criteria, red_on_base (optional)}" in prompt
        assert "TAP's\n  summary always prints `# skipped 0` and `# todo 0`" in prompt


class TestEffectiveIntakeModels:
    @pytest.mark.parametrize("mode", ["yes", "propose"])
    @pytest.mark.parametrize("collision", ["profiles", "override"])
    def test_effective_model_collision_is_not_written(
        self,
        tmp_project: Path,
        data_dir: Path,
        ledger: Ledger,
        monkeypatch,
        isolated_config: dict,
        mode: str,
        collision: str,
    ):
        path = write_task(tmp_project, DRAFT)
        before = path.read_text()
        proposal = {"policy": "review", "verify": [{"run": "pytest -q"}]}
        if collision == "profiles":
            isolated_config["profiles"]["implement"] = "same/model:low"
            isolated_config["profiles"]["review"] = "same/model:high"
        else:
            review = resolve_models(None, isolated_config)["review"].render()
            proposal["models"] = {"implement": review}
        adapter = ScriptedPiAdapter(
            {"intake": PiScript(final_text=f"```yaml\n{yaml.safe_dump(proposal)}```")}
        )
        use_adapter(monkeypatch, adapter)
        result = invoke("intake", str(path), f"--{mode}", "--json", data_dir=data_dir)
        assert result.exit_code == 2, result.output
        assert "reviewer must differ from implementer" in result.output
        assert path.read_text() == before
        assert ledger.list_tasks() == []
        assert len(adapter.calls) == 1


class TestIntakeEditor:
    """``intake`` without ``--yes`` on a terminal opens the merged task in ``$EDITOR``."""

    @pytest.fixture(autouse=True)
    def on_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(commands, "_is_tty", lambda: True)

    @pytest.fixture
    def draft(self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        use_adapter(
            monkeypatch,
            ScriptedPiAdapter(
                {"intake": PiScript(final_text=PROPOSAL.format(policy="review", isolation="none"))}
            ),
        )
        return write_task(tmp_project, DRAFT)

    def edit_with(self, monkeypatch: pytest.MonkeyPatch, edit) -> list[str]:
        seen: list[str] = []

        def fake_edit(text: str, extension: str) -> str | None:
            assert extension == ".md"
            seen.append(text)
            return edit(text)

        monkeypatch.setattr(commands.click, "edit", fake_edit)
        return seen

    @pytest.mark.parametrize(
        "scenario", ["default", "deviation", "delete-original", "change-original"]
    )
    def test_editor_normalizes_sparse_models(
        self,
        tmp_project: Path,
        data_dir: Path,
        ledger: Ledger,
        monkeypatch,
        isolated_config: dict,
        scenario: str,
    ):
        original = {"implement": "owner/implement:high"} if "original" in scenario else {}
        fm = parse_frontmatter(DRAFT.format(project=tmp_project))["frontmatter"]
        if original:
            fm["models"] = original
        path = write_task(tmp_project, serialize_frontmatter(fm, "Make the widget work."))
        use_adapter(
            monkeypatch,
            ScriptedPiAdapter(
                {
                    "intake": PiScript(
                        final_text=PROPOSAL.format(policy="review", isolation="process")
                    )
                }
            ),
        )
        default = resolve_models(None, isolated_config)["implement"].render()
        edited_models = {
            "implement": "editor/implement:high" if scenario == "deviation" else default
        }
        if scenario == "delete-original":
            edited_models = {}
        expected = original if scenario == "delete-original" else edited_models
        if scenario == "default":
            expected = {}

        def edit(text: str) -> str:
            parsed = parse_frontmatter(text)
            fm = parsed["frontmatter"]
            fm["models"] = edited_models
            return serialize_frontmatter(fm, parsed["body"])

        self.edit_with(monkeypatch, edit)
        result = invoke("intake", str(path), data_dir=data_dir)
        assert result.exit_code == 0, result.output
        written = parse_frontmatter(path.read_text())["frontmatter"]
        assert written.get("models", {}) == expected
        assert ("models" in written) is bool(expected)
        (row,) = ledger.list_tasks()
        assert row["snapshot"] == path.read_text()

    def test_editor_effective_model_collision_is_not_written(
        self, draft: Path, data_dir: Path, ledger: Ledger, monkeypatch, isolated_config: dict
    ):
        before = draft.read_text()
        review = resolve_models(None, isolated_config)["review"].render()

        def edit(text: str) -> str:
            parsed = parse_frontmatter(text)
            parsed["frontmatter"]["models"] = {"implement": review}
            return serialize_frontmatter(parsed["frontmatter"], parsed["body"])

        self.edit_with(monkeypatch, edit)
        result = invoke("intake", str(draft), data_dir=data_dir)
        assert result.exit_code == 2, result.output
        assert "reviewer must differ from implementer" in result.output
        assert draft.read_text() == before
        assert ledger.list_tasks() == []

    def test_saved_edit_is_written_and_queued(
        self, draft: Path, data_dir: Path, ledger: Ledger, monkeypatch
    ):
        seen = self.edit_with(monkeypatch, lambda text: text.replace("ruff check src", "ruff ."))
        result = invoke("intake", str(draft), data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert "queued" in result.output
        assert "name: lint" in seen[0]
        assert seen[0].startswith("---\n")
        fm = parse_frontmatter(draft.read_text())["frontmatter"]
        assert fm["status"] == "queued"
        assert [s["run"] for s in fm["verify"]] == ["pytest -q", "ruff ."]
        assert "approved_at" in fm["intake"]
        (task,) = ledger.list_tasks()
        assert task["status"] == "queued"

    def test_unedited_text_matches_what_yes_writes(self, draft: Path, data_dir: Path, monkeypatch):
        seen = self.edit_with(monkeypatch, lambda text: text)
        result = invoke("intake", str(draft), data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert draft.read_text() == seen[0]

    def test_closed_without_saving_cancels(
        self, draft: Path, data_dir: Path, ledger: Ledger, monkeypatch
    ):
        before = draft.read_text()
        self.edit_with(monkeypatch, lambda text: None)
        result = invoke("intake", str(draft), data_dir=data_dir)
        assert result.exit_code == 1
        assert "intake cancelled" in result.output
        assert draft.read_text() == before
        assert ledger.list_tasks() == []

    def test_invalid_yaml_is_not_written(
        self, draft: Path, data_dir: Path, ledger: Ledger, monkeypatch
    ):
        before = draft.read_text()
        self.edit_with(monkeypatch, lambda text: text.replace("verify:", "verify: [", 1))
        result = invoke("intake", str(draft), data_dir=data_dir)
        assert result.exit_code == 2
        assert draft.read_text() == before
        assert ledger.list_tasks() == []

    def test_invalid_task_prints_errors_and_is_not_written(
        self, draft: Path, data_dir: Path, ledger: Ledger, monkeypatch
    ):
        before = draft.read_text()
        self.edit_with(monkeypatch, lambda text: text.replace("policy: review\n", ""))
        result = invoke("intake", str(draft), data_dir=data_dir)
        assert result.exit_code == 2
        assert "policy is required for a queued task" in result.output
        assert draft.read_text() == before
        assert ledger.list_tasks() == []

    def test_auto_set_in_editor_is_downgraded_without_allow_auto(
        self, draft: Path, data_dir: Path, monkeypatch
    ):
        self.edit_with(monkeypatch, lambda text: text.replace("policy: review", "policy: auto"))
        result = invoke("intake", str(draft), data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert "--allow-auto" in result.output
        assert parse_frontmatter(draft.read_text())["frontmatter"]["policy"] == "review"

    def test_auto_set_in_editor_is_kept_with_allow_auto(
        self, draft: Path, data_dir: Path, monkeypatch
    ):
        self.edit_with(
            monkeypatch,
            lambda text: text.replace(
                "policy: review", "policy: auto\nevidence:\n- name: smoke\n  run: echo ok"
            ).replace("isolation: none", "isolation: process"),
        )
        result = invoke("intake", str(draft), "--allow-auto", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert parse_frontmatter(draft.read_text())["frontmatter"]["policy"] == "auto"

    def test_json_never_opens_the_editor(self, draft: Path, data_dir: Path, monkeypatch):
        seen = self.edit_with(monkeypatch, lambda text: text)
        result = invoke("intake", str(draft), "--json", data_dir=data_dir)
        assert result.exit_code == 1
        assert seen == []
        assert "re-run with --yes" in json.loads(result.output)["error"]


# ── queue ─────────────────────────────────────────────────────────────


def queue_one(tmp_project: Path, data_dir: Path, monkeypatch) -> str:
    use_adapter(monkeypatch, None)
    path = write_task(tmp_project, COMPLETE)
    result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    return str(json.loads(result.output)["task_id"])


class TestQueue:
    def test_list_json_and_table(self, tmp_project: Path, data_dir: Path, monkeypatch):
        key = queue_one(tmp_project, data_dir, monkeypatch)
        result = invoke("queue", "list", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["paused"] is None
        (task,) = out["tasks"]
        assert task["task_id"] == key
        assert task["status"] == "queued"
        assert task["priority"] == "normal"
        assert task["project"] == str(tmp_project)
        assert task["queued_at"].endswith("+00:00")
        assert "snapshot" not in task

        table = invoke("queue", "list", data_dir=data_dir)
        assert table.exit_code == 0
        assert "Task queue" in table.output and "queued" in table.output

    def test_bare_queue_lists(self, tmp_project: Path, data_dir: Path, monkeypatch):
        queue_one(tmp_project, data_dir, monkeypatch)
        result = invoke("queue", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert len(json.loads(result.output)["tasks"]) == 1

    def test_list_status_filter(self, tmp_project: Path, data_dir: Path, monkeypatch):
        queue_one(tmp_project, data_dir, monkeypatch)
        result = invoke("queue", "list", "--status", "draft", "--json", data_dir=data_dir)
        assert json.loads(result.output)["tasks"] == []

    def test_next(self, tmp_project: Path, data_dir: Path, monkeypatch):
        empty = invoke("queue", "next", "--json", data_dir=data_dir)
        assert empty.exit_code == 0 and json.loads(empty.output)["task"] is None
        assert "no queued task" in invoke("queue", "next", data_dir=data_dir).output

        key = queue_one(tmp_project, data_dir, monkeypatch)
        result = invoke("queue", "next", data_dir=data_dir)
        assert result.exit_code == 0 and result.output.strip() == key
        as_json = invoke("queue", "next", "--json", data_dir=data_dir)
        assert json.loads(as_json.output)["task"]["task_id"] == key
        other = invoke("queue", "next", "--project", "/elsewhere", "--json", data_dir=data_dir)
        assert json.loads(other.output)["task"] is None

    def test_next_names_tasks_held_by_a_parked_run(
        self, tmp_project: Path, data_dir: Path, ledger: Ledger, monkeypatch
    ):
        earlier = queue_one(tmp_project, data_dir, monkeypatch)
        ledger.set_task_status(earlier, "awaiting_approval")
        later = tmp_project / "topics" / "later.md"
        later.write_text(
            COMPLETE.replace("id: fix-widget", "id: later").format(project=tmp_project)
        )
        queued = invoke("intake", str(later), "--yes", "--json", data_dir=data_dir)
        key = json.loads(queued.output)["task_id"]
        run_id = "22222222-2222-4222-8222-222222222222"
        ledger.create_run(
            {
                "run_id": run_id,
                "task_id": earlier,
                "project": str(tmp_project),
                "status": "awaiting_approval",
                "policy": "approve-plan-first",
                "isolation": "none",
                "model_implement": "a/impl",
                "model_review": "a/review",
                "pi_version": "0.87.1",
            }
        )
        ledger.record_approval(run_id, "plan", "pending")

        as_json = invoke("queue", "next", "--json", data_dir=data_dir)
        text = invoke("queue", "next", data_dir=data_dir)

        assert json.loads(as_json.output) == {
            "task": None,
            "paused": None,
            "held": [
                {
                    "task_id": key,
                    "run_id": run_id,
                    "status": "awaiting_approval",
                    "pending": "plan",
                }
            ],
        }
        assert text.output.splitlines() == [
            "no eligible task",
            f"  held: {key} by run 22222222 (awaiting plan approval)",
        ]

    def test_pause_resume_and_exit_3(self, tmp_project: Path, data_dir: Path, monkeypatch):
        queue_one(tmp_project, data_dir, monkeypatch)
        paused = invoke("queue", "pause", "--reason", "night", "--json", data_dir=data_dir)
        assert paused.exit_code == 0, paused.output
        assert json.loads(paused.output)["paused"] == {"reason": "night", "until": None}

        nxt = invoke("queue", "next", "--json", data_dir=data_dir)
        assert nxt.exit_code == 3
        assert json.loads(nxt.output)["paused"]["reason"] == "night"
        assert invoke("queue", "next", data_dir=data_dir).exit_code == 3

        listed = invoke("queue", "list", data_dir=data_dir)
        assert "paused" in listed.output.lower() and "night" in listed.output

        timed = invoke("queue", "pause", "--minutes", "30", "--json", data_dir=data_dir)
        assert json.loads(timed.output)["paused"]["until"] is not None

        ledger = Ledger(data_dir / "helmraiser.db")
        ledger.hold_provider("opencode-go", datetime.now(UTC).timestamp() + 60, "quota")
        resumed = invoke("queue", "resume", "--json", data_dir=data_dir)
        resumed_payload = json.loads(resumed.output)
        assert resumed.exit_code == 0 and resumed_payload["paused"] is None
        assert resumed_payload["held_providers"] == {}
        assert ledger.provider_holds(datetime.now(UTC).timestamp()) == {}
        assert invoke("queue", "next", data_dir=data_dir).exit_code == 0

    def test_remove_by_full_id_and_unique_bare_id(
        self, tmp_project: Path, data_dir: Path, ledger: Ledger, monkeypatch
    ):
        key = queue_one(tmp_project, data_dir, monkeypatch)
        path = tmp_project / "topics" / "fix-widget.md"

        removed = invoke("queue", "remove", "fix-widget", data_dir=data_dir)
        assert removed.exit_code == 0, removed.output
        assert removed.output.strip() == f"{key} removed from the queue (now draft)"
        (task,) = json.loads(invoke("queue", "list", "--json", data_dir=data_dir).output)["tasks"]
        assert task["status"] == "draft"
        assert "status: queued" in path.read_text()  # the file on disk is untouched

        again = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
        assert again.exit_code == 0, again.output
        assert invoke("queue", "next", data_dir=data_dir).output.strip() == key

        by_id = invoke("queue", "remove", key, "--json", data_dir=data_dir)
        assert by_id.exit_code == 0, by_id.output
        assert json.loads(by_id.output) == {"task_id": key, "status": "draft"}
        assert ledger.get_task(key)["status"] == "draft"

    def test_remove_refuses_non_queued_tasks(
        self, tmp_project: Path, data_dir: Path, ledger: Ledger, monkeypatch
    ):
        key = queue_one(tmp_project, data_dir, monkeypatch)
        for status in ("running", "awaiting_approval"):
            assert ledger.set_task_status(key, status)
            result = invoke("queue", "remove", "fix-widget", data_dir=data_dir)
            assert result.exit_code == 2, result.output
            assert status in result.output
        assert ledger.get_task(key)["status"] == "awaiting_approval"

    def test_remove_does_not_overwrite_a_task_that_started_meanwhile(
        self, tmp_project: Path, data_dir: Path, ledger: Ledger, monkeypatch
    ):
        key = queue_one(tmp_project, data_dir, monkeypatch)
        original = Ledger.list_tasks

        def list_then_start(self: Ledger, *args, **kwargs):
            rows = original(self, *args, **kwargs)
            self.set_task_status(key, "running")  # work picks it up after the lookup
            return rows

        monkeypatch.setattr(Ledger, "list_tasks", list_then_start)
        result = invoke("queue", "remove", "fix-widget", data_dir=data_dir)
        monkeypatch.setattr(Ledger, "list_tasks", original)

        assert result.exit_code == 2, result.output
        assert "running" in result.output
        assert ledger.get_task(key)["status"] == "running"

    def test_requeued_task_goes_to_the_back_of_the_queue(
        self, tmp_project: Path, data_dir: Path, ledger: Ledger, monkeypatch
    ):
        key = queue_one(tmp_project, data_dir, monkeypatch)
        first_queued_at = ledger.get_task(key)["queued_at"]
        assert invoke("queue", "remove", key, data_dir=data_dir).exit_code == 0
        assert ledger.get_task(key)["queued_at"] is None
        path = tmp_project / "topics" / "fix-widget.md"
        assert invoke("intake", str(path), "--yes", data_dir=data_dir).exit_code == 0
        assert ledger.get_task(key)["queued_at"] > first_queued_at

    def test_remove_ambiguous_bare_id(
        self, tmp_project: Path, tmp_path: Path, git_init, data_dir: Path, monkeypatch
    ):
        queue_one(tmp_project, data_dir, monkeypatch)
        other = tmp_path / "other"
        other.mkdir()
        git_init(other)
        use_adapter(monkeypatch, None)
        path = other / "topics" / "fix-widget.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(COMPLETE.format(project=other), encoding="utf-8")
        queued = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
        assert queued.exit_code == 0, queued.output
        other_key = json.loads(queued.output)["task_id"]

        result = invoke("queue", "remove", "fix-widget", data_dir=data_dir)

        assert result.exit_code == 1, result.output
        flat = result.output.replace("\n", "")
        assert "ambiguous task id: fix-widget" in flat
        assert other_key in flat
        by_full = invoke("queue", "remove", other_key, data_dir=data_dir)
        assert by_full.exit_code == 0, by_full.output

    def test_remove_unknown_task(self, data_dir: Path):
        result = invoke("queue", "remove", "nope", data_dir=data_dir)
        assert result.exit_code == 1
        assert "unknown task: nope" in result.output

    def test_data_dir_env_fallback(self, tmp_project: Path, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HELMRAISER_DATA_DIR", str(tmp_path / "env-data"))
        use_adapter(monkeypatch, None)
        path = write_task(tmp_project, COMPLETE)
        result = runner.invoke(app, ["intake", str(path), "--yes"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "env-data" / "helmraiser.db").exists()
        assert runner.invoke(app, ["queue", "next"]).output.strip().endswith(":fix-widget")


# ── intake sandbox (S3) ──────────────────────────────────────────────


class _SessionWritingAdapter(ScriptedPiAdapter):
    """Writes a session file where pi would, then answers like the scripted adapter."""

    async def run(self, req, on_event, run_id):  # type: ignore[no-untyped-def]
        req.session_dir.mkdir(parents=True, exist_ok=True)
        (req.session_dir / "s.jsonl").write_text("{}\n")
        return await super().run(req, on_event, run_id)


class TestIntakeSandbox:
    @pytest.fixture
    def home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        return home

    def test_refuses_without_srt(self, tmp_project: Path, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(ProcessIsolation, "check_available", lambda self: "srt not found")
        wrap = intake_logic.intake_wrapper(copy.deepcopy(DEFAULT_CONFIG))
        with pytest.raises(intake_logic.IntakeSandboxError, match="intake_unsandboxed: true"):
            wrap(["pi", "--mode", "json"], tmp_project, tmp_path / "run")

    def test_unsandboxed_only_when_the_config_says_so(
        self, tmp_project: Path, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr(ProcessIsolation, "check_available", lambda self: "srt not found")
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["isolation"] = {"intake_unsandboxed": True}
        argv = ["pi", "--mode", "json"]
        assert intake_logic.intake_wrapper(config)(argv, tmp_project, tmp_path / "run") == argv

    def test_wraps_in_srt_with_the_run_read_rules_and_no_project_writes(
        self, tmp_project: Path, tmp_path: Path, home: Path, monkeypatch
    ):
        monkeypatch.setattr(ProcessIsolation, "check_available", lambda self: None)
        (tmp_project / ".helmraiser").mkdir(exist_ok=True)
        (tmp_project / ".helmraiser" / "project.toml").write_text(
            '[sandbox]\ndeny_read = ["secrets"]\n'
        )
        (tmp_path / "sibling").mkdir()
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["isolation"] = {"projects_root": str(tmp_path)}
        run_dir = tmp_path / "helmraiser-intake-x"

        argv = intake_logic.intake_wrapper(config)(["pi", "--mode", "json"], tmp_project, run_dir)

        index = argv.index("--settings")
        assert argv[:index] == [
            shutil.which("env"),
            f"CLAUDE_CODE_TMPDIR={process_module.DEFAULT_SRT_TMPDIR}",
            shutil.which("srt"),
        ]
        assert argv[index:] == [
            "--settings",
            str(run_dir / "srt-settings.json"),
            "--",
            "pi",
            "--mode",
            "json",
        ]
        fs = json.loads((run_dir / "srt-settings.json").read_text())["filesystem"]
        assert fs["allowWrite"] == [
            str(run_dir / "sessions"),
            str(process_module.DEFAULT_SRT_TMPDIR),
        ]
        assert str(tmp_project / "secrets") in fs["denyRead"]
        assert (
            str(home / ".claude") in fs["denyRead"] and str(home / ".helmraiser") in fs["denyRead"]
        )
        assert str(tmp_path / "sibling") in fs["denyRead"]
        assert not [p for p in fs["denyRead"] if p.startswith(str(tmp_project) + "/.git")]

    def test_cli_refuses_with_a_clear_message(self, tmp_project: Path, data_dir: Path, monkeypatch):
        monkeypatch.setattr(ProcessIsolation, "check_available", lambda self: "srt not found")
        path = write_task(tmp_project, DRAFT)
        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code != 0
        assert "process isolation, which is unavailable: srt not found" in result.output
        assert parse_frontmatter(path.read_text())["frontmatter"]["status"] == "draft"

    def test_a_complete_file_needs_no_sandbox(self, tmp_project: Path, data_dir: Path, monkeypatch):
        monkeypatch.setattr(ProcessIsolation, "check_available", lambda self: "srt not found")
        path = write_task(tmp_project, COMPLETE)
        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output

    def test_session_files_are_kept_in_the_data_dir(
        self, tmp_project: Path, data_dir: Path, monkeypatch
    ):
        adapter = _SessionWritingAdapter(
            {"intake": PiScript(final_text=PROPOSAL.format(policy="review", isolation="process"))}
        )
        use_adapter(monkeypatch, adapter)
        path = write_task(tmp_project, DRAFT)
        result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        (req,) = adapter.calls
        run_id = req.session_id.removesuffix("-intake")
        kept = data_dir / "intake" / run_id / "sessions" / "s.jsonl"
        assert kept.read_text() == "{}\n" and not req.session_dir.exists()


@pytest.mark.parametrize(
    "command,value",
    [("add", ["Bad"]), ("add", ["fix-widget"]), ("intake", ["fix-widget"]), ("intake", [True])],
)
def test_depends_on_registration_rejected(
    tmp_project, data_dir, ledger, monkeypatch, command, value
):
    use_adapter(monkeypatch, None)
    parsed = parse_frontmatter(COMPLETE.format(project=tmp_project))
    parsed["frontmatter"]["depends_on"] = value
    path = write_task(tmp_project, serialize_frontmatter(parsed["frontmatter"], parsed["body"]))
    args = [command, str(path), "--json"]
    if command == "intake":
        args.append("--yes")
    result = invoke(*args, data_dir=data_dir)
    assert result.exit_code == 2, result.output
    assert "depends_on" in result.output
    assert ledger.list_tasks() == []


@pytest.mark.parametrize("scripted", [False, True])
def test_depends_on_intake_owner_preserved(tmp_project, data_dir, ledger, monkeypatch, scripted):
    parsed = parse_frontmatter(COMPLETE.format(project=tmp_project))
    parsed["frontmatter"]["depends_on"] = ["unknown-task"]
    path = write_task(tmp_project, serialize_frontmatter(parsed["frontmatter"], parsed["body"]))
    use_adapter(
        monkeypatch,
        (
            ScriptedPiAdapter(
                {
                    "intake": PiScript(
                        final_text='```yaml\nverify: [{name: test, run: "true"}]\npolicy: review\ndepends_on: [replacement]\n```'
                    )
                }
            )
            if scripted
            else None
        ),
    )
    prompt = build_intake_prompt(parse_task(path.read_text(), path, {}), DEFAULT_CONFIG)
    assert "`depends_on`" in next(
        line for line in prompt.splitlines() if "owner-controlled" in line
    )
    result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    row = ledger.list_tasks()[0]
    assert parse_frontmatter(row["snapshot"])["frontmatter"]["depends_on"] == ["unknown-task"]
    assert parse_frontmatter(path.read_text())["frontmatter"]["depends_on"] == ["unknown-task"]
    assert ledger.held_tasks()[row["task_id"]]["unmet"][0]["reason"] == "unknown"


@pytest.mark.parametrize("command", ["add", "intake"])
@pytest.mark.parametrize("value", [None, [], "src/*", [1], [""], ["  "], ["/src"], ["a/../b"]])
def test_paths_registration_rejected(tmp_project, data_dir, ledger, monkeypatch, command, value):
    use_adapter(monkeypatch, None)
    parsed = parse_frontmatter(COMPLETE.format(project=tmp_project))
    parsed["frontmatter"]["paths"] = value
    path = write_task(tmp_project, serialize_frontmatter(parsed["frontmatter"], parsed["body"]))
    args = [command, str(path), "--json"]
    if command == "intake":
        args.append("--yes")
    result = invoke(*args, data_dir=data_dir)
    assert result.exit_code == 2, result.output
    assert "paths" in result.output
    assert ledger.list_tasks() == []


@pytest.mark.parametrize("scripted,declared", [(False, True), (True, True), (True, False)])
def test_paths_intake_owner_preserved(
    tmp_project, data_dir, ledger, monkeypatch, scripted, declared
):
    parsed = parse_frontmatter((DRAFT if scripted else COMPLETE).format(project=tmp_project))
    patterns = ["./src//**/*.py", "docs/", "docs/"]
    if declared:
        parsed["frontmatter"]["paths"] = patterns
    path = write_task(tmp_project, serialize_frontmatter(parsed["frontmatter"], parsed["body"]))
    adapter = (
        ScriptedPiAdapter(
            {
                "intake": PiScript(
                    final_text='```yaml\nverify: [{name: test, run: "true"}]\npolicy: review\npaths: [replacement]\n```'
                )
            }
        )
        if scripted
        else None
    )
    use_adapter(monkeypatch, adapter)
    prompt = build_intake_prompt(parse_task(path.read_text(), path, {}), DEFAULT_CONFIG)
    if declared:
        assert "Preserve these declared patterns unchanged" in prompt
        assert all(pattern in prompt for pattern in patterns)
    result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    if adapter is not None:
        assert len(adapter.calls) == 1
    for content in (path.read_text(), ledger.list_tasks()[0]["snapshot"]):
        fm = parse_frontmatter(content)["frontmatter"]
        if declared:
            assert fm["paths"] == patterns
        else:
            assert "paths" not in fm


@pytest.mark.parametrize("command", [("intake", "--yes"), ("intake", "--propose"), ("add",)])
def test_reintake_cannot_redeclare_active_run_scope(
    tmp_project, data_dir, ledger, fake_pi, isolated_config, monkeypatch, command
):
    from helmraiser.task.loader import render_task
    from tests.cli.test_approve_reject import queue_and_run
    from tests.ledger.test_db import make_spec

    isolated_config["max_runs_per_project"] = 2
    runtime = data_dir / "runtime"
    runtime.mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    use_adapter(monkeypatch, None)
    outcome = queue_and_run(
        tmp_project, data_dir, fake_pi, task_id="a", marker="a/done.txt", paths=("a/**",)
    )
    holder = ledger.get_task(outcome["task_id"])
    spec = parse_task(holder["snapshot"], Path(holder["source_path"]), isolated_config)
    spec.source_path.write_text(render_task(replace(spec, paths=("b/**",))))
    name, *options = command
    result = invoke(name, str(spec.source_path), *options, "--json", data_dir=data_dir)
    assert result.exit_code == 2, result.output
    error = json.loads(result.output)["error"]
    assert outcome["run_id"] in error and "snapshot cannot change" in error
    assert ledger.get_task(spec.task_id) == holder
    candidate = make_spec(tmp_project, "c", paths=("a/**",))
    ledger.upsert_task(candidate, render_task(candidate))
    result = invoke("queue", "next", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["task"] is None
    assert payload["held"][0]["reason"] == "paths_overlap"
    assert payload["held"][0]["other_task_id"] == "a"


@pytest.mark.parametrize("overlap", [False, True])
def test_sibling_queue_next(tmp_project, data_dir, ledger, monkeypatch, overlap):
    from helmraiser.task.loader import render_task
    from tests.ledger.test_db import make_spec, run_row

    monkeypatch.setattr(commands, "load_config", lambda: {"max_runs_per_project": 2})
    a = make_spec(tmp_project, "a", paths=("a/**",))
    b = make_spec(tmp_project, "b", paths=(("a/**" if overlap else "b/**"),), status="running")
    for spec in (a, b):
        ledger.upsert_task(spec, render_task(spec))
    ledger.create_run(run_row("holder", b.task_id, str(tmp_project)))
    result = invoke("queue", "next", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    if overlap:
        assert payload["task"] is None
        assert payload["held"][0]["reason"] == "paths_overlap"
        assert payload["held"][0]["other_task_id"] == "b"
        text = invoke("queue", "next", data_dir=data_dir)
        assert "paths_overlap with b" in text.output
    else:
        assert payload["task"]["task_id"] == a.task_id
    (tmp_project / ".helmraiser").mkdir(exist_ok=True)
    (tmp_project / ".helmraiser/project.toml").write_text("max_runs_per_project = 1\n")
    result = invoke("queue", "next", "--json", data_dir=data_dir)
    assert json.loads(result.output)["task"] is None


def test_depends_on_queue_next(tmp_project, data_dir, ledger):
    from helmraiser.task.loader import render_task
    from tests.ledger.test_db import make_spec

    held = make_spec(
        tmp_project, "held", depends_on=("missing-one", "missing-two"), priority="high"
    )
    free = make_spec(tmp_project, "free")
    for spec in (held, free):
        ledger.upsert_task(spec, render_task(spec))
    result = invoke("queue", "next", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["task"]["task_id"] == free.task_id
    assert [item["id"] for item in payload["held"][0]["unmet"]] == ["missing-one", "missing-two"]
    ledger.dequeue_task(free.task_id)
    result = invoke("queue", "next", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["task"] is None
    text = invoke("queue", "next", data_dir=data_dir)
    assert text.exit_code == 0, text.output
    assert "no eligible task" in text.output
    assert "missing-one: unknown" in text.output and "missing-two: unknown" in text.output


def test_failed_check_feedback_prompt_and_forwarding(tmp_project, data_dir):
    path = write_task(tmp_project, DRAFT)
    spec = parse_task(path.read_text(), path, DEFAULT_CONFIG)
    feedback = "step: tests\ncommand: false\nbase diagnostic"
    opening = (
        "Your previous proposal was run on the unchanged code. These steps failed for "
        "reasons unrelated to the task. Propose an acceptance block that avoids each failure: use "
        "only commands and tools the project environment provides, and do not lint or test files "
        "that already fail on the unchanged code unless the task fixes them."
    )
    original = build_intake_prompt(spec, DEFAULT_CONFIG)
    assert original == build_intake_prompt(spec, DEFAULT_CONFIG, None)
    assert "## Failed acceptance check" not in original and opening not in original
    prompt = build_intake_prompt(spec, DEFAULT_CONFIG, feedback)
    section = f"## Failed acceptance check\n\n{opening}\n\n{feedback}\n\n"
    assert prompt.count(section) == 1
    assert prompt.replace(section, "") == original
    assert prompt.index("## Current acceptance block") < prompt.index(section)
    assert prompt.index(section) < prompt.index("## Project environment")
    adapter = ScriptedPiAdapter(
        {"intake": PiScript(final_text=PROPOSAL.format(policy="review", isolation="process"))}
    )
    outcome = intake_logic.run_intake(
        spec,
        config=DEFAULT_CONFIG,
        adapter=adapter,
        model=intake_logic.ModelSpec.parse("test/model"),
        data_dir=data_dir,
        allow_auto=False,
        approve=False,
        feedback=feedback,
    )
    assert not outcome.errors
    assert len(adapter.calls) == 1
    assert section in adapter.calls[0].prompt
