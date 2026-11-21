"""Manual scout candidate contracts and draft workflow."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, scout_commands
from helmraiser.ledger.db import Ledger
from helmraiser.loop.scout import (
    ScoutError,
    _logbook_brief,
    _recent_log,
    _state_documents,
    build_scout_prompt,
    classify_preflight_step,
    collect_scout_inputs,
    normalize_scout_preflight,
    parse_candidates,
    render_draft,
    render_scout_report,
)
from helmraiser.pi.directives import render_scout_directives
from helmraiser.task.loader import parse_task
from helmraiser.task.model import project_slug
from helmraiser.task.project import load_project_defaults
from helmraiser.utils.markdown import parse_frontmatter
from tests.conftest import FakeLb, FakePi
from tests.loop.test_preflight import srt_calls
from tests.loop.test_preflight import srt_log as _srt_log_fixture  # noqa: F401

runner = CliRunner()


@pytest.fixture
def scout_srt_log(request: pytest.FixtureRequest) -> Path:
    return request.getfixturevalue("_srt_log_fixture")


def candidate(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "tighten-parser",
        "title": "Tighten parser validation",
        "why": "The parser accepts an invalid value.",
        "evidence": ["src/parser.py:12"],
        "files": ["src/parser.py", "tests/test_parser.py"],
        "change": "Reject invalid values and add a regression test.",
        "verify_hints": ["pytest tests/test_parser.py"],
        "size": "S",
        "needs_owner_ruling": False,
        "ruling_question": None,
    }
    value.update(overrides)
    return value


def intake_proposal(*, name: str = "tests", timeout: int = 120) -> str:
    return f"""```yaml
verify:
  - name: {name}
    run: pytest tests/new_test.py
    timeout: {timeout}
evidence: []
policy: review
plan: false
isolation: process
budget:
  max_cost_usd: 1
  max_minutes: 30
  max_iterations: 2
```"""


def project_file(repo: Path, text: str) -> None:
    path = repo / ".helmraiser" / "project.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestScoutInputs:
    def test_project_exclusions_are_normalized_and_validated(self, tmp_project: Path):
        project_file(tmp_project, '[scout]\nexclude = ["docs/private/", "projects\\\\paused"]\n')
        assert load_project_defaults(tmp_project).scout_exclude == (
            "docs/private",
            "projects/paused",
        )

    @pytest.mark.parametrize("value", ['"/etc"', '"../other"', '"a/../b"', '"C:\\\\secret"', '""'])
    def test_project_exclusions_reject_unsafe_paths(self, tmp_project: Path, value: str):
        project_file(tmp_project, f"[scout]\nexclude = [{value}]\n")
        with pytest.raises(ValueError, match="safe relative paths"):
            load_project_defaults(tmp_project)

    def test_state_documents_are_root_and_one_project_level_only(self, tmp_project: Path):
        (tmp_project / "STATUS.md").write_text("root")
        child = tmp_project / "projects" / "one"
        child.mkdir(parents=True)
        (child / "TASKS.md").write_text("child")
        nested = child / "nested"
        nested.mkdir()
        (nested / "STATUS.md").write_text("ignored")

        assert _state_documents(tmp_project) == [
            {"path": "STATUS.md", "content": "root"},
            {"path": "projects/one/TASKS.md", "content": "child"},
        ]

    def test_state_documents_never_follow_symlinks_out_of_the_repository(
        self, tmp_project: Path, tmp_path: Path
    ):
        outside = tmp_path / "outside"
        (outside / "secret").mkdir(parents=True)
        (outside / "STATUS.md").write_text("outside root")
        (outside / "secret" / "TASKS.md").write_text("outside project")
        (tmp_project / "STATUS.md").symlink_to(outside / "STATUS.md")
        projects = tmp_project / "projects"
        projects.mkdir()
        (projects / "linked").symlink_to(outside / "secret")
        real = projects / "real"
        real.mkdir()
        (real / "TASKS.md").write_text("inside")

        assert _state_documents(tmp_project) == [
            {"path": "projects/real/TASKS.md", "content": "inside"},
        ]

    def test_a_symlinked_projects_directory_is_not_read(self, tmp_project: Path, tmp_path: Path):
        outside = tmp_path / "elsewhere"
        (outside / "one").mkdir(parents=True)
        (outside / "one" / "STATUS.md").write_text("outside")
        (tmp_project / "projects").symlink_to(outside)

        assert _state_documents(tmp_project) == []

    def test_recent_log_is_limited_to_40(self, tmp_project: Path):
        for number in range(45):
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", f"commit {number}"],
                cwd=tmp_project,
                check=True,
                capture_output=True,
            )
        log = _recent_log(tmp_project)
        assert len(log) == 40
        assert "commit 44" in log[0]

    def test_prompt_contains_tasks_drop_reason_and_exclusions(
        self, tmp_project: Path, tmp_path: Path
    ):
        ledger = Ledger(tmp_path / "ledger.db")
        snapshot = "---\nid: old\ndrop_reason: Already fixed\n---\n"
        from tests.ledger.test_db import make_spec

        ledger.upsert_task(
            make_spec(tmp_project, "old", status="dropped", title="Old title"), snapshot
        )
        inputs = collect_scout_inputs(tmp_project, ledger, ("private",))
        prompt = build_scout_prompt(inputs, 3)
        assert "Old title" in prompt and "Already fixed" in prompt and "private" in prompt

    def test_project_notes_are_in_scout_prompt(self, tmp_project: Path, tmp_path: Path):
        ledger = Ledger(tmp_path / "ledger.db")
        notes = "Never invoke the socket-opening test CLI."

        inputs = collect_scout_inputs(tmp_project, ledger, (), notes)
        prompt = build_scout_prompt(inputs, 3)

        assert inputs["project_notes"] == notes
        assert notes in prompt
        assert "constraints on every candidate" in prompt
        assert "verify_hints" in prompt

    def test_logbook_is_filtered_and_reaches_scout_prompt(
        self,
        tmp_project: Path,
        tmp_path: Path,
        fake_lb: FakeLb,
        monkeypatch: pytest.MonkeyPatch,
    ):
        fake_lb.write_script(
            {
                "brief": {
                    "stdout": json.dumps(
                        {
                            "project": "demo",
                            "holds": [
                                {"title": "No cloud", "secret": "hidden"},
                                {"title": "Wait for owner"},
                                {"title": ""},
                                {"title": "  "},
                                {"title": 42},
                                {},
                                "not an entry",
                                None,
                            ],
                            "grants": [{"title": "Owner merges"}],
                            "rules": [{"title": "Use pipefail"}],
                            "references": [{"title": "hidden"}],
                            "inbox": [{"title": "hidden"}],
                            "repoState": ["hidden"],
                            "secret": "hidden",
                        }
                    )
                }
            }
        )
        monkeypatch.setenv("LOGBOOK_HARNESS", "pi")
        monkeypatch.setenv("LOGBOOK_SESSION", "parent-session")
        ledger = Ledger(tmp_path / "ledger.db")
        inputs = collect_scout_inputs(tmp_project, ledger, ())
        assert inputs["logbook"] == {
            "holds": ["No cloud", "Wait for owner"],
            "grants": ["Owner merges"],
            "rules": ["Use pipefail"],
        }
        prompt = build_scout_prompt(inputs, 3)
        prompt_inputs = json.loads(prompt.split("Scout inputs:\n", 1)[1])
        assert prompt_inputs["logbook"] == inputs["logbook"]
        assert "secret" not in prompt and "hidden" not in prompt
        (call,) = fake_lb.calls()
        assert call["argv"] == ["brief", "--cwd", str(tmp_project), "--json"]
        assert call["env"]["LOGBOOK_HARNESS"] == "helmraiser"
        assert call["env"]["LOGBOOK_SESSION"] is None

    @pytest.mark.parametrize(
        "response",
        [
            {"exit": 1, "stderr": "lb: unavailable"},
            {"exit": 3, "stdout": "{}"},
            {"stdout": "not json"},
            {"stdout": "[]"},
            {"stdout": "null"},
        ],
    )
    def test_logbook_failures_are_silent(
        self, tmp_project: Path, fake_lb: FakeLb, response: dict, capsys
    ):
        fake_lb.write_script({"brief": response})
        assert _logbook_brief(tmp_project) is None
        assert capsys.readouterr() == ("", "")

    def test_logbook_missing_is_silent(
        self, tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        monkeypatch.setenv("PATH", str(tmp_path))
        assert _logbook_brief(tmp_project) is None
        assert capsys.readouterr() == ("", "")

    @pytest.mark.parametrize("timeout", [False, True])
    def test_logbook_subprocess_errors_are_silent(
        self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch, timeout: bool, capsys
    ):
        def fail(argv, **kwargs):
            assert kwargs["timeout"] == 30
            assert kwargs["shell"] is False
            if timeout:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            raise OSError("cannot start lb")

        monkeypatch.setattr("helmraiser.loop.scout.subprocess.run", fail)
        assert _logbook_brief(tmp_project) is None
        assert capsys.readouterr() == ("", "")

    def test_logbook_missing_or_malformed_lists_are_empty(self, tmp_project: Path, fake_lb: FakeLb):
        fake_lb.write_script(
            {"brief": {"stdout": json.dumps({"holds": {"title": "ignored"}, "rules": None})}}
        )
        assert _logbook_brief(tmp_project) == {"holds": [], "grants": [], "rules": []}


class TestScoutPreflightClassification:
    @pytest.mark.parametrize(
        ("output", "files", "expected"),
        [
            ("Could not find 'src/config.test.ts'", ("src/config.test.ts",), "expected red"),
            (
                "ERROR: file or directory not found: tests/new_test.py",
                ("tests/new_test.py",),
                "expected red",
            ),
            (
                "python: can't open file 'tools/new.py': [Errno 2] No such file or directory",
                ("tools/new.py",),
                "expected red",
            ),
            (
                "Error: Cannot find module 'src/new-module.ts'",
                ("src/new-module.ts",),
                "expected red",
            ),
            (
                "AssertionError: tests/new_test.py should return true",
                ("tests/new_test.py",),
                "fail",
            ),
            (
                "AssertionError: Could not find 'tests/new_test.py' in the response",
                ("tests/new_test.py",),
                "fail",
            ),
            ("Could not find 'src/unrelated.ts'", ("src/config.test.ts",), "fail"),
        ],
    )
    def test_missing_target_classification(
        self, tmp_project: Path, output: str, files: tuple[str, ...], expected: str
    ):
        result = {"passed": False, "exit_code": 1, "output": output, "hints": []}
        assert classify_preflight_step(tmp_project, result, files) == expected

    def test_existing_head_path_is_not_expected_red(self, tmp_project: Path):
        result = {
            "passed": False,
            "exit_code": 1,
            "output": "Could not find 'README.md'",
            "hints": [],
        }
        # The same diagnostic for a path that is missing at HEAD is expected red, so
        # only the HEAD existence check makes this one a plain failure.
        assert classify_preflight_step(tmp_project, result, ("README.md",)) == "fail"
        missing = {**result, "output": "Could not find 'NEW.md'"}
        assert classify_preflight_step(tmp_project, missing, ("NEW.md",)) == "expected red"

    def test_sandbox_precedes_coincidental_missing_path(self, tmp_project: Path):
        result = {
            "passed": False,
            "exit_code": 1,
            "output": "listen EPERM /tmp/tsx.pipe; Could not find 'src/config.test.ts'",
            "hints": ["Unix sockets are blocked by the sandbox"],
        }
        assert classify_preflight_step(tmp_project, result, ("src/config.test.ts",)) == "sandbox"

    def test_report_includes_top_level_preflight_error(self):
        report = render_scout_report(
            scout_id="scout",
            candidates=[],
            drafted=["project:task"],
            skipped=[],
            usage={},
            preparations=[
                {
                    "task_id": "project:task",
                    "intake_status": "proposed",
                    "proposal": {},
                    "preflight": {
                        "summary": "fail(1)",
                        "error": "services: redis failed to start",
                        "steps": [],
                    },
                }
            ],
        )
        assert "Preflight error: services: redis failed to start" in report

    @pytest.mark.parametrize(
        ("classes", "error", "summary"),
        [
            (("green", "green"), None, "green"),
            (("green", "expected red"), None, "expected red"),
            (("expected red", "sandbox"), None, "sandbox(1)"),
            (("sandbox", "fail", "fail"), None, "fail(2)"),
            ((), "setup: exit 1", "fail(1)"),
        ],
    )
    def test_summary_precedence(
        self,
        tmp_project: Path,
        classes: tuple[str, ...],
        error: str | None,
        summary: str,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import helmraiser.loop.preflight as preflight_module

        monkeypatch.setattr(
            preflight_module,
            "classify_preflight_step",
            lambda repo, result, files: result["scripted_class"],
        )
        commands = [
            {
                "name": f"step-{n}",
                "exit_code": 0 if value == "green" else 1,
                "passed": value == "green",
                "seconds": 0.1,
                "tail": value,
                "output": value,
                "hints": ["hint"] if value == "sandbox" else [],
                "scripted_class": value,
            }
            for n, value in enumerate(classes)
        ]
        normalized = normalize_scout_preflight(
            tmp_project, {"commands": commands, "error": error}, ("new.py",)
        )
        assert normalized["summary"] == summary
        assert all("output" not in step for step in normalized["steps"])


class TestScoutContract:
    def test_directive_is_read_only_and_requires_exact_json(self, tmp_project: Path):
        text = render_scout_directives(
            scout_id="s", project=tmp_project, worktree="/tree", exclusions=("private",)
        )
        assert "never modify, create, or delete" in text
        assert "host, cloud, provider" in text
        assert "Do not reopen recorded owner" in text
        assert "only one strict JSON list" in text
        assert "private" in text

    def test_valid_candidates_parse(self):
        (parsed,) = parse_candidates(json.dumps([candidate()]), ())
        assert parsed.id == "tighten-parser"
        assert parsed.evidence == ("src/parser.py:12",)
        assert parsed.verify_hints == ("pytest tests/test_parser.py",)

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            ("not json", "malformed scout JSON"),
            (json.dumps({}), "must be a JSON list"),
            (json.dumps([candidate(extra=True)]), "invalid keys"),
            (json.dumps([candidate(id="../escape")]), "safe task id"),
            (json.dumps([candidate(title="")]), "title must be"),
            (json.dumps([candidate(evidence=[])]), "evidence must be"),
            (json.dumps([candidate(evidence=["src/a.py"])]), "invalid evidence path"),
            (json.dumps([candidate(files=[])]), "files must be"),
            (json.dumps([candidate(size="XL")]), "size must be"),
            (json.dumps([candidate(needs_owner_ruling="yes")]), "must be a boolean"),
            (
                json.dumps([candidate(needs_owner_ruling=True, ruling_question="")]),
                "non-empty ruling_question",
            ),
        ],
    )
    def test_invalid_candidates_fail_as_a_whole(self, payload: str, message: str):
        with pytest.raises(ScoutError, match=message):
            parse_candidates(payload, ())

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("files", ["../secret"]),
            ("files", ["/etc/passwd"]),
            ("files", ["private/a.py"]),
            ("evidence", ["private/a.py:1"]),
        ],
    )
    def test_unsafe_and_excluded_paths_fail(self, field: str, value: list[str]):
        with pytest.raises(ScoutError, match="invalid .* path"):
            parse_candidates(json.dumps([candidate(**{field: value})]), ("private",))

    @pytest.mark.parametrize("delivery", ["pr", None])
    def test_generated_draft_is_deterministic_and_loadable(
        self, tmp_project: Path, delivery: str | None
    ):
        if delivery is not None:
            project_file(tmp_project, f'[defaults]\ndelivery = "{delivery}"\n')
        (parsed,) = parse_candidates(json.dumps([candidate()]), ())
        text = render_draft(parsed, tmp_project, "00000000-0000-4000-8000-000000000000")
        assert text == render_draft(parsed, tmp_project, "00000000-0000-4000-8000-000000000000")
        frontmatter = parse_frontmatter(text)["frontmatter"]
        assert "delivery" not in frontmatter
        assert frontmatter == {
            "id": "tighten-parser",
            "title": "Tighten parser validation",
            "project": str(tmp_project.resolve()),
            "policy": "review",
            "isolation": "process",
            "status": "draft",
            "source": "scout 00000000-0000-4000-8000-000000000000",
            "size": "S",
            "tags": ["scout"],
        }
        spec = parse_task(text, tmp_project / ".helmraiser/tasks/tighten-parser.md", {})

        assert spec.id == "tighten-parser" and spec.status == "draft"
        assert spec.policy == "review"
        assert spec.delivery == (delivery or "merge")
        assert spec.isolation == "process" and spec.tags == ("scout",)
        assert spec.extra["size"] == "S"
        assert spec.extra["source"] == "scout 00000000-0000-4000-8000-000000000000"
        assert "## Goal" in spec.body and "`src/parser.py:12`" in spec.body
        assert "## Change" in spec.body and "`tests/test_parser.py`" in spec.body
        assert "## Verify hints" in spec.body and "pytest tests/test_parser.py" in spec.body

    def test_project_notes_are_appended_once_to_drafts(self, tmp_project: Path):
        notes = "Use node --import tsx, never the tsx CLI."
        (parsed,) = parse_candidates(json.dumps([candidate()]), ())
        text = render_draft(parsed, tmp_project, "00000000-0000-4000-8000-000000000000", notes)
        assert text.count("## Project notes (Helmraiser)") == 1
        assert text.count(notes) == 1

        (already_carried,) = parse_candidates(
            json.dumps([candidate(change=f"Make the change. {notes}")]), ()
        )
        text = render_draft(
            already_carried,
            tmp_project,
            "00000000-0000-4000-8000-000000000000",
            notes,
        )
        assert text.count(notes) == 1
        assert "## Project notes (Helmraiser)" not in text


@pytest.fixture
def cli_config(monkeypatch: pytest.MonkeyPatch, configured_config: dict) -> dict:
    config = configured_config
    monkeypatch.setattr(scout_commands, "load_config", lambda: config)
    return config


def run_scout(repo: Path, data_dir: Path, *args: str):
    return runner.invoke(
        app,
        ["scout", str(repo), "--data-dir", str(data_dir), "--json", *args],
    )


def seed_draft(ledger: Ledger, repo: Path, **overrides: object) -> str:
    (item,) = parse_candidates(json.dumps([candidate(**overrides)]), ())
    path = repo / ".helmraiser" / "tasks" / f"{item.id}.md"
    text = render_draft(item, repo, "00000000-0000-4000-8000-000000000000")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    spec = parse_task(text, path.resolve(), {})
    return ledger.upsert_task(spec, text)


@pytest.mark.parametrize("args", [[], ["--force"], ["--force", "--scout-id", "test"]])
def test_missing_scout_model(cli_config, tmp_project, tmp_path, fake_pi, args):
    cli_config["routing"]["scout"]["model"] = None
    base = tmp_path / "data"
    result = run_scout(tmp_project, base, *args)
    assert result.exit_code == 2, result.output
    assert json.loads(result.output) == {
        "error": "models_not_configured",
        "missing": ["routing.scout.model"],
    }
    assert not base.exists()
    assert not fake_pi.calls()


class TestScoutCommand:
    @pytest.mark.parametrize("declared", [True, False])
    def test_body_path_notes_are_saved_in_scout_drafts(
        self,
        tmp_project: Path,
        tmp_path: Path,
        fake_pi: FakePi,
        scout_srt_log: Path,
        cli_config: dict,
        monkeypatch: pytest.MonkeyPatch,
        declared: bool,
    ):
        if declared:
            # Current scout drafts omit paths. Supply a scoped draft to exercise
            # note propagation without changing the production draft renderer.
            def scoped_draft(*args, **kwargs):
                return render_draft(*args, **kwargs).replace(
                    "status: draft\n",
                    "status: draft\npaths: [src/parser.py, tests/test_parser.py]\n",
                )

            monkeypatch.setattr("helmraiser.loop.scout.render_draft", scoped_draft)
        (tmp_project / "docs").mkdir()
        (tmp_project / "docs/context.md").write_text("Context\n")
        subprocess.run(["git", "-C", str(tmp_project), "add", "docs/context.md"], check=True)
        fake_pi.write_script(
            {
                "sessions": {
                    "*-scout": {"text": json.dumps([candidate(change="Update docs/context.md.")])},
                    "*-intake": {"text": intake_proposal()},
                }
            }
        )
        monkeypatch.setattr(
            "helmraiser.loop.scout.run_preflight_steps",
            lambda *args, **kwargs: {"commands": [], "error": None},
        )
        data_dir = tmp_path / "data"

        result = run_scout(tmp_project, data_dir, "--force")

        assert result.exit_code == 0, result.output
        note = (
            "body mentions files outside paths: docs/context.md"
            " — add them to paths if the task edits them"
        )
        ledger = Ledger(data_dir / "helmraiser.db")
        (row,) = ledger.list_tasks(status="draft")
        intake = ledger.get_scout_draft(row["task_id"])
        assert intake["intake_notes"].count(note) == int(declared)
        report = Path(json.loads(result.output)["report_path"]).read_text()
        assert (note in report) == declared
        drafts = runner.invoke(app, ["drafts", "--data-dir", str(data_dir), "--json"])
        assert drafts.exit_code == 0, drafts.output
        assert (note in json.loads(drafts.output)[0]["intake_notes"]) == declared
        path = tmp_project / ".helmraiser/tasks/tighten-parser.md"
        for text in (path.read_text(), row["snapshot"]):
            spec = parse_task(text, path, {})
            assert spec.status == "draft"
            assert spec.paths == (("src/parser.py", "tests/test_parser.py") if declared else None)

    def test_shell_syntax_notes_are_saved_before_preflight(
        self,
        tmp_project: Path,
        tmp_path: Path,
        fake_pi: FakePi,
        scout_srt_log: Path,
        cli_config: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        proposal = intake_proposal().replace(
            "    run: pytest tests/new_test.py",
            "    run: |\n      cat <<'END'\n      ```json",
        )
        fake_pi.write_script(
            {
                "sessions": {
                    "*-scout": {"text": json.dumps([candidate()])},
                    "*-intake": {"text": proposal},
                }
            }
        )
        data_dir = tmp_path / "data"
        seen = []

        def preflight(repo, steps, **kwargs):
            ledger = Ledger(data_dir / "helmraiser.db")
            (row,) = ledger.list_tasks(status="draft")
            intake = ledger.get_scout_draft(row["task_id"])
            assert intake["intake_status"] == "proposed"
            syntax_notes = [n for n in intake["intake_notes"] if "shell syntax" in n]
            assert len(syntax_notes) == 1
            note = syntax_notes[0]
            assert "verify step tests" in note and "here-document" in note
            assert steps[0].run == "cat <<'END'\n```json\n"
            assert intake["proposal"]["verify"][0]["run"] == steps[0].run
            seen.append(note)
            return {"commands": [], "error": None}

        monkeypatch.setattr("helmraiser.loop.scout.run_preflight_steps", preflight)

        result = run_scout(tmp_project, data_dir, "--force")

        assert result.exit_code == 0, result.output
        assert len(seen) == 1
        payload = json.loads(result.output)
        report = Path(payload["report_path"]).read_text()
        assert seen[0] in report
        drafts = runner.invoke(app, ["drafts", "--data-dir", str(data_dir), "--json"])
        assert drafts.exit_code == 0, drafts.output
        assert seen[0] in json.loads(drafts.output)[0]["intake_notes"]
        human = runner.invoke(app, ["drafts", "--data-dir", str(data_dir)])
        assert human.exit_code == 0 and seen[0] in human.output

    def test_project_notes_reach_fake_pi_prompt_and_generated_draft(
        self,
        tmp_project: Path,
        tmp_path: Path,
        fake_pi: FakePi,
        scout_srt_log: Path,
        cli_config: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        notes = "Use node --import tsx; the tsx CLI cannot open sockets."
        notes_path = tmp_project / ".helmraiser" / "notes.md"
        notes_path.parent.mkdir(exist_ok=True)
        notes_path.write_text(notes, encoding="utf-8")
        fake_pi.write_script(
            {
                "sessions": {
                    "*-scout": {"text": json.dumps([candidate(id="noted", title="Noted")])},
                    "*-intake": {"text": intake_proposal()},
                }
            }
        )
        monkeypatch.setattr(
            "helmraiser.loop.scout.run_preflight_steps",
            lambda *args, **kwargs: {"commands": [], "error": None},
        )

        result = run_scout(tmp_project, tmp_path / "data", "--force")

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        run_dir = Path(payload["report_path"]).parent
        assert notes in (run_dir / "prompt-scout-1.md").read_text(encoding="utf-8")
        draft = (tmp_project / ".helmraiser" / "tasks" / "noted.md").read_text(encoding="utf-8")
        assert draft.count("## Project notes (Helmraiser)") == 1
        assert draft.count(notes) == 1

    def test_happy_path_writes_two_drafts_and_reports_ruling(
        self,
        tmp_project: Path,
        tmp_path: Path,
        fake_pi: FakePi,
        scout_srt_log: Path,
        cli_config: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        candidates = [
            candidate(id="one", title="One", files=["tests/new_test.py"]),
            candidate(
                id="ruling",
                title="Needs choice",
                needs_owner_ruling=True,
                ruling_question="A or B?",
            ),
            candidate(id="two", title="Two", size="M", files=["tests/new_test.py"]),
        ]
        fake_pi.write_script(
            {
                "sessions": {
                    "*-scout": {
                        "text": json.dumps(candidates),
                        "cost": 0.12,
                        "tokens_in": 11,
                        "tokens_out": 22,
                    },
                    "*-intake": {"text": intake_proposal()},
                }
            }
        )
        data_dir = tmp_path / "data"
        monkeypatch.setattr(
            "helmraiser.loop.scout.run_preflight_steps",
            lambda *args, **kwargs: {
                "commands": [
                    {
                        "name": "tests",
                        "exit_code": 1,
                        "passed": False,
                        "seconds": 0.2,
                        "output": "ERROR: file or directory not found: tests/new_test.py",
                        "tail": "ERROR: file or directory not found: tests/new_test.py",
                        "hints": [],
                    }
                ],
                "error": None,
            },
        )

        result = run_scout(tmp_project, data_dir, "--force")

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["outcome"] == "succeeded"
        assert len(payload["written_task_ids"]) == 2
        assert sorted(path.name for path in (tmp_project / ".helmraiser/tasks").glob("*.md")) == [
            "one.md",
            "two.md",
        ]
        ledger = Ledger(data_dir / "helmraiser.db")
        task_rows = ledger.list_tasks(status="draft")
        assert [row["id"] for row in task_rows] == ["one", "two"]
        for task_row in task_rows:
            spec = parse_task(task_row["snapshot"], Path(task_row["source_path"]), cli_config)
            assert spec.status == "draft" and "intake" not in spec.extra
            assert spec.verify[0].name == "tests" and spec.verify[0].timeout == 120
            assert Path(task_row["source_path"]).read_text() == task_row["snapshot"]
            prepared = ledger.get_scout_draft(task_row["task_id"])
            assert prepared is not None and prepared["proposal"]["verify"][0]["name"] == "tests"
            assert prepared["preflight"]["summary"] == "expected red"
        row = ledger.get_scout(payload["scout_id"])
        assert row is not None and row["outcome"] == "succeeded"
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_project,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        assert row["head_commit"] == head
        assert (row["cost_usd"], row["tokens_in"], row["tokens_out"]) == (0.12, 11, 22)
        report = Path(payload["report_path"]).read_text()
        assert "Needs choice" in report and "A or B?" in report
        assert '"verify"' in report and "Preflight: expected red" in report
        assert not (tmp_project / ".helmraiser/tasks/ruling.md").exists()
        calls = fake_pi.calls()
        scout_calls = [call for call in calls if call["session_id"].endswith("-scout")]
        intake_calls = [call for call in calls if call["session_id"].endswith("-intake")]
        assert len(scout_calls) == 1 and len(intake_calls) == 2
        call = scout_calls[0]
        assert (
            call["argv"][call["argv"].index("--model") + 1]
            == cli_config["routing"]["scout"]["model"]
        )
        assert call["argv"][call["argv"].index("--tools") + 1] == "read,grep,find,ls"
        assert call["argv"][-1].startswith("@")
        assert not Path(call["cwd"]).exists()
        sandbox_calls = srt_calls(scout_srt_log)
        assert len(sandbox_calls) == 3
        sandbox_call = sandbox_calls[0]
        writes = sandbox_call["settings"]["filesystem"]["allowWrite"]
        assert call["cwd"] not in writes and "/tmp" not in writes

    def test_scout_intake_failure_continues_to_next_draft(
        self,
        tmp_project: Path,
        tmp_path: Path,
        fake_pi: FakePi,
        scout_srt_log: Path,
        cli_config: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        fake_pi.write_script(
            {
                "sessions": {
                    "*-scout": {
                        "text": json.dumps(
                            [candidate(id="bad", title="Bad"), candidate(id="good", title="Good")]
                        )
                    },
                    "*-intake": [{"text": "not yaml"}, {"text": intake_proposal()}],
                }
            }
        )
        calls: list[str] = []

        def preflight(repo, steps, **kwargs):
            calls.append(steps[0].name)
            return {"commands": [], "error": None}

        monkeypatch.setattr("helmraiser.loop.scout.run_preflight_steps", preflight)
        data_dir = tmp_path / "data"

        result = run_scout(tmp_project, data_dir, "--force")

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["outcome"] == "succeeded" and len(payload["written_task_ids"]) == 2
        ledger = Ledger(data_dir / "helmraiser.db")
        failed = ledger.get_scout_draft(payload["written_task_ids"][0])
        prepared = ledger.get_scout_draft(payload["written_task_ids"][1])
        assert failed is not None and failed["intake_status"] == "intake_failed"
        assert "fenced yaml" in failed["intake_error"] and failed["preflight"] is None
        assert prepared is not None and prepared["intake_status"] == "proposed"
        assert prepared["preflight"]["summary"] == "green"
        assert calls == ["tests"]

    def test_scout_preflights_drafts_serially_with_step_timeouts(
        self,
        tmp_project: Path,
        tmp_path: Path,
        fake_pi: FakePi,
        scout_srt_log: Path,
        cli_config: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        cli_config["routing"]["scout"]["max_drafts_per_run"] = 4
        candidates = [
            candidate(id=f"task-{n}", title=f"Task {n}", files=["tests/new_test.py"])
            for n in range(4)
        ]
        fake_pi.write_script(
            {
                "sessions": {
                    "*-scout": {"text": json.dumps(candidates)},
                    "*-intake": [
                        {"text": intake_proposal(name=f"step-{n}", timeout=10 + n)}
                        for n in range(4)
                    ],
                }
            }
        )
        seen: list[tuple[str, int]] = []
        active = False
        outputs = [
            "",
            "Could not find 'tests/new_test.py'",
            "listen EPERM /tmp/x.pipe",
            "assert 1 == 2",
        ]

        def preflight(repo, steps, **kwargs):
            nonlocal active
            assert not active
            active = True
            step = steps[0]
            seen.append((step.name, step.timeout))
            index = len(seen) - 1
            hints = ["sandbox hint"] if index == 2 else []
            payload = {
                "commands": [
                    {
                        "name": step.name,
                        "exit_code": 0 if index == 0 else 1,
                        "passed": index == 0,
                        "seconds": 0.1,
                        "output": outputs[index],
                        "tail": outputs[index],
                        "hints": hints,
                    }
                ],
                "error": None,
            }
            active = False
            return payload

        monkeypatch.setattr("helmraiser.loop.scout.run_preflight_steps", preflight)
        data_dir = tmp_path / "data"

        result = run_scout(tmp_project, data_dir, "--force", "--max", "4")

        assert result.exit_code == 0, result.output
        assert seen == [(f"step-{n}", 10 + n) for n in range(4)]
        ledger = Ledger(data_dir / "helmraiser.db")
        preflights = [
            ledger.get_scout_draft(task_id)["preflight"]
            for task_id in json.loads(result.output)["written_task_ids"]
        ]
        assert [preflight["summary"] for preflight in preflights] == [
            "green",
            "expected red",
            "sandbox(1)",
            "fail(1)",
        ]
        for n, preflight in enumerate(preflights):
            assert preflight["steps"] == [
                {
                    "name": f"step-{n}",
                    "exit_code": 0 if n == 0 else 1,
                    "seconds": 0.1,
                    "class": ("green", "expected red", "sandbox", "fail")[n],
                    "hint": "sandbox hint" if n == 2 else None,
                    "tail": outputs[n],
                }
            ]

    def test_malformed_json_fails_without_drafts(
        self,
        tmp_project: Path,
        tmp_path: Path,
        fake_pi: FakePi,
        scout_srt_log: Path,
        cli_config: dict,
    ):
        fake_pi.write_script({"sessions": {"*-scout": {"text": "not json\nlast line"}}})
        data_dir = tmp_path / "data"

        result = run_scout(tmp_project, data_dir, "--force")

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["outcome"] == "failed" and payload["written_task_ids"] == []
        assert "not json\nlast line" in Path(payload["report_path"]).read_text()
        assert Ledger(data_dir / "helmraiser.db").list_tasks() == []

    @pytest.mark.parametrize("symlink_kind", ["task-file", "tasks-directory"])
    def test_symlinked_draft_destinations_cannot_escape_repository(
        self,
        symlink_kind: str,
        tmp_project: Path,
        tmp_path: Path,
        fake_pi: FakePi,
        scout_srt_log: Path,
        cli_config: dict,
    ):
        outside = tmp_path / "outside"
        outside.mkdir()
        helmraiser_dir = tmp_project / ".helmraiser"
        helmraiser_dir.mkdir(exist_ok=True)
        task_dir = helmraiser_dir / "tasks"
        if symlink_kind == "tasks-directory":
            task_dir.symlink_to(outside, target_is_directory=True)
        else:
            task_dir.mkdir()
            (task_dir / "one.md").symlink_to(outside / "one.md")
        fake_pi.write_script(
            {"sessions": {"*-scout": {"text": json.dumps([candidate(id="one", title="One")])}}}
        )

        result = run_scout(tmp_project, tmp_path / "data", "--force")

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["outcome"] == "failed" and payload["written_task_ids"] == []
        assert "symlink" in payload["error"]
        assert not (outside / "one.md").exists()
        assert Ledger(tmp_path / "data" / "helmraiser.db").list_tasks() == []

    def test_invalid_project_config_uses_json_error_response(
        self,
        tmp_project: Path,
        tmp_path: Path,
        fake_pi: FakePi,
        scout_srt_log: Path,
        cli_config: dict,
    ):
        project_file(tmp_project, '[scout]\nexclude = "not-a-list"\n')

        result = run_scout(tmp_project, tmp_path / "data", "--force")

        assert result.exit_code == 1
        assert "scout.exclude must be a list of strings" in json.loads(result.output)["error"]
        assert fake_pi.calls() == []

    def test_duplicate_candidates_are_skipped_and_later_candidate_fills_cap(
        self,
        tmp_project: Path,
        tmp_path: Path,
        fake_pi: FakePi,
        scout_srt_log: Path,
        cli_config: dict,
    ):
        data_dir = tmp_path / "data"
        ledger = Ledger(data_dir / "helmraiser.db")
        from tests.ledger.test_db import make_spec

        ledger.upsert_task(
            make_spec(tmp_project, "old", status="failed", title="Existing title"), ""
        )
        fake_pi.write_script(
            {
                "sessions": {
                    "*-scout": {
                        "text": json.dumps(
                            [
                                candidate(id="old", title="Different"),
                                candidate(id="new-title", title="  EXISTING   TITLE "),
                                candidate(id="fresh", title="Fresh"),
                            ]
                        )
                    }
                }
            }
        )

        result = run_scout(tmp_project, data_dir, "--force", "--max", "1")

        payload = json.loads(result.output)
        assert result.exit_code == 0 and payload["written_task_ids"] == [
            f"{str(tmp_project).replace('/', '-')}:fresh"
        ]
        assert len(payload["skipped_duplicates"]) == 2

    def test_focus_and_pause_gates_refuse_without_starting(
        self,
        tmp_project: Path,
        tmp_path: Path,
        fake_pi: FakePi,
        scout_srt_log: Path,
        cli_config: dict,
    ):
        data_dir = tmp_path / "data"
        result = run_scout(tmp_project, data_dir)
        assert result.exit_code == 1 and "not in routing.scout.focus_projects" in result.output
        assert Ledger(data_dir / "helmraiser.db").list_scouts() == []
        assert fake_pi.calls() == []

        cli_config["routing"]["scout"]["focus_projects"] = [str(tmp_project)]
        cli_config["routing"]["scout"]["paused_projects"] = [str(tmp_project)]
        result = run_scout(tmp_project, data_dir)
        assert result.exit_code == 1 and "paused" in result.output
        assert Ledger(data_dir / "helmraiser.db").list_scouts() == []
        assert fake_pi.calls() == []

    def test_max_must_be_positive_and_is_capped(
        self,
        tmp_project: Path,
        tmp_path: Path,
        fake_pi: FakePi,
        scout_srt_log: Path,
        cli_config: dict,
    ):
        invalid = run_scout(tmp_project, tmp_path / "invalid", "--force", "--max", "0")
        assert invalid.exit_code == 1 and "positive" in invalid.output
        fake_pi.write_script(
            {
                "sessions": {
                    "*-scout": {
                        "text": json.dumps(
                            [candidate(id=str(n), title=f"Task {n}") for n in range(1, 6)]
                        )
                    }
                }
            }
        )
        capped = run_scout(tmp_project, tmp_path / "capped", "--force", "--max", "20")
        assert capped.exit_code == 0
        assert len(json.loads(capped.output)["written_task_ids"]) == 3


class TestDraftCommands:
    def test_lists_human_and_json_with_project_filter(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        ledger = Ledger(data_dir / "helmraiser.db")
        first, second = tmp_path / "p1", tmp_path / "p2"
        first.mkdir()
        second.mkdir()
        seed_draft(ledger, first, id="one", title="First", size="S")
        seed_draft(ledger, second, id="two", title="Second", size="L")

        result = runner.invoke(app, ["drafts", "--data-dir", str(data_dir), "--json"])
        rows = json.loads(result.output)
        assert result.exit_code == 0 and [row["id"] for row in rows] == ["one", "two"]
        assert all(isinstance(row["age"], int) and row["age"] >= 0 for row in rows)
        assert set(rows[0]) == {
            "lens",
            "task_id",
            "project",
            "path",
            "origin",
            "id",
            "title",
            "size",
            "source",
            "kind",
            "age",
            "intake_status",
            "intake_error",
            "intake_notes",
            "proposal",
            "preflight",
        }
        assert rows[0]["intake_status"] == "not_run" and rows[0]["preflight"] is None
        assert Path(rows[0]["path"]).is_absolute()
        assert rows[0]["origin"] is None
        assert rows[0]["task_id"] == f"{project_slug(first)}:one"

        human = runner.invoke(
            app,
            ["drafts", "--data-dir", str(data_dir), "--project", str(second)],
        )
        assert human.exit_code == 0 and "Second" in human.output and "First" not in human.output
        assert (
            "size=L" in human.output and "source=scout" in human.output and "age=" in human.output
        )
        assert "preflight: not run" in human.output

    def test_lists_proposals_preflight_summaries_and_intake_failures(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        repo = tmp_path / "repo"
        repo.mkdir()
        ledger = Ledger(data_dir / "helmraiser.db")
        ledger.create_scout(
            {"id": "scout", "project": str(repo), "model": "p/m", "report_path": "/r"}
        )
        summaries = ["green", "expected red", "sandbox(2)", "fail(1)"]
        task_ids = []
        for n, summary in enumerate(summaries):
            task_id = seed_draft(ledger, repo, id=f"draft-{n}", title=f"Draft {n}")
            task_ids.append(task_id)
            ledger.create_scout_draft(task_id, "scout", candidate(id=f"draft-{n}"))
            proposal = {"verify": [{"name": "tests", "run": "pytest", "timeout": 30}]}
            ledger.set_scout_draft_intake(task_id, status="proposed", proposal=proposal, error=None)
            ledger.set_scout_draft_preflight(
                task_id,
                {
                    "summary": summary,
                    "error": None,
                    "steps": [
                        {
                            "name": "tests",
                            "exit_code": 0,
                            "seconds": 0.1,
                            "class": "green",
                            "hint": None,
                            "tail": "ok",
                        }
                    ],
                },
            )
        failed_id = seed_draft(ledger, repo, id="failed", title="Failed intake")
        ledger.create_scout_draft(failed_id, "scout", candidate(id="failed"))
        ledger.set_scout_draft_intake(
            failed_id, status="intake_failed", proposal=None, error="invalid acceptance"
        )

        json_result = runner.invoke(app, ["drafts", "--data-dir", str(data_dir), "--json"])
        rows = {row["id"]: row for row in json.loads(json_result.output)}
        assert rows["draft-0"]["proposal"]["verify"][0]["timeout"] == 30
        assert rows["draft-0"]["preflight"]["steps"][0]["tail"] == "ok"
        assert "output" not in rows["draft-0"]["preflight"]["steps"][0]
        assert rows["failed"]["intake_error"] == "invalid acceptance"

        human = runner.invoke(app, ["drafts", "--data-dir", str(data_dir)])
        for summary in summaries:
            assert f"preflight: {summary}" in human.output
        assert "intake: intake_failed (invalid acceptance) preflight: not run" in human.output

    def test_drop_by_full_and_bare_id_preserves_file_and_body(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        repo = tmp_path / "repo"
        repo.mkdir()
        ledger = Ledger(data_dir / "helmraiser.db")
        task_id = seed_draft(ledger, repo, id="drop-me", title="Drop me", size="M")
        path = repo / ".helmraiser/tasks/drop-me.md"
        before_body = parse_task(path.read_text(), path, {}).body

        result = runner.invoke(
            app,
            [
                "drafts",
                "drop",
                task_id,
                "--reason",
                "Already handled elsewhere",
                "--json",
                "--data-dir",
                str(data_dir),
            ],
        )

        assert result.exit_code == 0, result.output
        assert path.exists()
        updated = parse_task(path.read_text(), path, {})
        assert updated.status == "dropped" and updated.body == before_body
        assert updated.extra["source"].startswith("scout ") and updated.extra["size"] == "M"
        row = ledger.get_task(task_id)
        assert row is not None and row["status"] == "dropped"
        assert "Already handled elsewhere" in row["snapshot"]

        other = seed_draft(ledger, repo, id="bare", title="Bare")
        bare = runner.invoke(
            app,
            [
                "drafts",
                "drop",
                "bare",
                "--reason",
                "Not useful",
                "--data-dir",
                str(data_dir),
            ],
        )
        assert bare.exit_code == 0 and ledger.get_task(other)["status"] == "dropped"

    def test_drop_rejects_blank_unknown_ambiguous_and_non_draft(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        ledger = Ledger(data_dir / "helmraiser.db")
        p1, p2 = tmp_path / "p1", tmp_path / "p2"
        p1.mkdir()
        p2.mkdir()
        full = seed_draft(ledger, p1, id="same", title="One")
        seed_draft(ledger, p2, id="same", title="Two")
        seed_draft(ledger, p1, id="landed", title="Landed")
        ledger.set_task_status(f"{str(p1).replace('/', '-')}:landed", "landed")

        def drop(task: str, reason: str = "reason"):
            return runner.invoke(
                app,
                [
                    "drafts",
                    "drop",
                    task,
                    "--reason",
                    reason,
                    "--data-dir",
                    str(data_dir),
                ],
            )

        assert "ambiguous" in drop("same").output
        assert "unknown" in drop("missing").output
        assert "not a draft" in drop("landed").output
        assert "must not be blank" in drop(full, "   ").output

    def test_drop_reason_is_in_next_prompt_and_duplicate_cannot_return(
        self,
        tmp_project: Path,
        tmp_path: Path,
        fake_pi: FakePi,
        scout_srt_log: Path,
        cli_config: dict,
    ):
        data_dir = tmp_path / "data"
        original = candidate(id="remember", title="Remember this")
        fake_pi.write_script({"sessions": {"*-scout": {"text": json.dumps([original])}}})
        first = run_scout(tmp_project, data_dir, "--force")
        first_payload = json.loads(first.output)
        task_id = first_payload["written_task_ids"][0]
        dropped = runner.invoke(
            app,
            [
                "drafts",
                "drop",
                task_id,
                "--reason",
                "Already handled elsewhere",
                "--data-dir",
                str(data_dir),
            ],
        )
        assert dropped.exit_code == 0
        fake_pi.write_script(
            {
                "sessions": {
                    "*-scout": {
                        "text": json.dumps(
                            [
                                original,
                                candidate(id="renamed", title="  REMEMBER   THIS "),
                                candidate(id="fresh", title="Fresh task"),
                            ]
                        )
                    }
                }
            }
        )

        second = run_scout(tmp_project, data_dir, "--force")

        second_payload = json.loads(second.output)
        assert second_payload["written_task_ids"] == [f"{str(tmp_project).replace('/', '-')}:fresh"]
        scout_call = [call for call in fake_pi.calls() if call["session_id"].endswith("-scout")][-1]
        prompt_path = Path(scout_call["argv"][-1].removeprefix("@"))
        prompt = prompt_path.read_text()
        assert "Remember this" in prompt and "Already handled elsewhere" in prompt


class _FakeLedger:
    def scout_lens_override(self, lens):
        return {}

    def __init__(
        self,
        drafts: int,
        *,
        queued: int = 0,
        paused: bool = False,
        runs: list[dict[str, object]] | None = None,
        scouts: list[dict[str, object]] | None = None,
    ) -> None:
        self.drafts = drafts
        self.queued = queued
        self.paused = paused
        self.runs = runs or []
        self.scouts = scouts or []

    def is_paused(self) -> dict[str, object] | None:
        return {"reason": "maintenance"} if self.paused else None

    def list_tasks(self, status: str | None = None, project: str | None = None) -> list[object]:
        count = self.drafts if status == "draft" else self.queued if status == "queued" else 0
        return [object()] * count

    def list_runs(self, project: str | None = None) -> list[dict[str, object]]:
        return self.runs

    def list_scouts(self, project: str | None = None) -> list[dict[str, object]]:
        return self.scouts


def _gate_config(repo: Path) -> dict[str, object]:
    return {
        "routing": {
            "scout": {
                "enabled": True,
                "focus_projects": [str(repo)],
                "paused_projects": [],
                "model": "openai-codex/gpt-6.1-sol:high",
                "max_pending_drafts": 3,
            }
        },
        "subscriptions": {"openai-codex": {}},
    }


class TestScoutSupervisorGateHeadroomAndDrafts:
    @pytest.fixture(autouse=True)
    def _no_live_usage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from helmraiser.loop import spend

        monkeypatch.setattr(spend, "live_for", lambda *a, **k: {})
        monkeypatch.setattr(spend, "usage", lambda *a, **k: {})

    def _headroom(self, monkeypatch: pytest.MonkeyPatch, has: bool) -> None:
        import helmraiser.loop.scout as scout_module

        monkeypatch.setattr(
            scout_module,
            "calculate_headroom",
            lambda usage, config, now: {"openai-codex": {"has_headroom": has}},
        )

    def test_disabled_paused_nonfocus_and_queue_pause_refuse(
        self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from helmraiser.loop.scout import ScoutError, scout_gate

        self._headroom(monkeypatch, True)
        config = _gate_config(tmp_project)
        config["routing"]["scout"]["enabled"] = False
        with pytest.raises(ScoutError, match="enabled is false"):
            scout_gate(tmp_project, _FakeLedger(0), config, 0.0)
        config["routing"]["scout"]["enabled"] = True
        config["routing"]["scout"]["paused_projects"] = [str(tmp_project)]
        with pytest.raises(ScoutError, match="paused for scouting"):
            scout_gate(tmp_project, _FakeLedger(0), config, 0.0)
        config["routing"]["scout"]["paused_projects"] = []
        config["routing"]["scout"]["focus_projects"] = []
        with pytest.raises(ScoutError, match="not in routing.scout.focus_projects"):
            scout_gate(tmp_project, _FakeLedger(0), config, 0.0)
        config["routing"]["scout"]["focus_projects"] = [str(tmp_project)]
        with pytest.raises(ScoutError, match="queue is paused"):
            scout_gate(tmp_project, _FakeLedger(0, paused=True), config, 0.0)

    def test_queued_and_every_nonterminal_run_refuse(
        self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from helmraiser.loop.scout import ScoutError, scout_gate

        self._headroom(monkeypatch, True)
        config = _gate_config(tmp_project)
        with pytest.raises(ScoutError, match="queued work"):
            scout_gate(tmp_project, _FakeLedger(0, queued=1), config, 0.0)
        for status in ("running", "verifying", "reviewing", "resuming", "awaiting_approval"):
            with pytest.raises(ScoutError, match=f"non-terminal run: {status}"):
                scout_gate(
                    tmp_project,
                    _FakeLedger(0, runs=[{"status": status}]),
                    config,
                    0.0,
                )

    def test_no_headroom_refuses(self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch):
        from helmraiser.loop.scout import ScoutError, scout_gate

        self._headroom(monkeypatch, False)
        with pytest.raises(ScoutError, match="no headroom: openai-codex"):
            scout_gate(tmp_project, _FakeLedger(0), _gate_config(tmp_project), 0.0)

    def test_pending_drafts_at_the_cap_refuse(
        self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from helmraiser.loop.scout import ScoutError, scout_gate

        self._headroom(monkeypatch, True)
        with pytest.raises(ScoutError, match="3 pending drafts"):
            scout_gate(tmp_project, _FakeLedger(3), _gate_config(tmp_project), 0.0)

    def test_headroom_and_room_for_drafts_pass(
        self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from helmraiser.loop.scout import scout_gate

        self._headroom(monkeypatch, True)
        scout_gate(tmp_project, _FakeLedger(2), _gate_config(tmp_project), 0.0)

    def test_successful_scout_head_or_cooldown_gate(
        self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from helmraiser.loop.scout import ScoutError, scout_gate

        self._headroom(monkeypatch, True)
        config = _gate_config(tmp_project)
        config["routing"]["scout"]["cooldown_hours"] = 1
        now = 10_000.0
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_project,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        recent = {
            "outcome": "succeeded",
            "head_commit": head,
            "started_at": now - 20,
            "ended_at": now - 10,
        }
        with pytest.raises(ScoutError, match="HEAD is unchanged"):
            scout_gate(tmp_project, _FakeLedger(0, scouts=[recent]), config, now)
        scout_gate(
            tmp_project,
            _FakeLedger(0, scouts=[{**recent, "head_commit": "different"}]),
            config,
            now,
        )
        scout_gate(
            tmp_project,
            _FakeLedger(0, scouts=[{**recent, "ended_at": now - 3600}]),
            config,
            now,
        )
        scout_gate(
            tmp_project,
            _FakeLedger(0, scouts=[{**recent, "outcome": "failed"}]),
            config,
            now,
        )
        # Out of order: the newest completion is the one compared, whatever its start.
        older_start_newer_end = {**recent, "started_at": now - 7200, "ended_at": now - 5}
        newer_start_older_end = {
            **recent,
            "head_commit": "different",
            "started_at": now - 30,
            "ended_at": now - 7000,
        }
        with pytest.raises(ScoutError, match="HEAD is unchanged"):
            scout_gate(
                tmp_project,
                _FakeLedger(0, scouts=[newer_start_older_end, older_start_newer_end]),
                config,
                now,
            )


def test_per_lens_due_and_automatic_gates(tmp_project, monkeypatch):
    from helmraiser.loop import scout as module

    config = _gate_config(tmp_project)
    config["routing"]["scout"]["enabled"] = False
    ledger = _FakeLedger(
        0,
        scouts=[
            {
                "outcome": "succeeded",
                "lens": "docs",
                "started_at": 1,
                "ended_at": 100,
                "head_commit": "same",
            }
        ],
    )
    monkeypatch.setattr(
        ledger, "scout_lens_override", lambda lens: {"auto": True} if lens == "tests" else {}
    )
    monkeypatch.setattr(module, "head_commit", lambda repo: "same")
    monkeypatch.setattr(module.spend, "live_for", lambda *a, **k: {})
    monkeypatch.setattr(module.spend, "usage", lambda *a, **k: {})
    monkeypatch.setattr(
        module, "calculate_headroom", lambda *a: {"openai-codex": {"has_headroom": True}}
    )
    assert module.scout_due_at(tmp_project, ledger, config, lens="tests") == float("-inf")
    assert module.scout_due_at(tmp_project, ledger, config, lens="docs") == 86500
    module.scout_gate(tmp_project, ledger, config, 101, lens="tests", automatic=True)
    with pytest.raises(module.ScoutError):
        module.scout_gate(tmp_project, ledger, config, 101, lens="docs", automatic=True)
    config["routing"]["scout"]["enabled"] = True
    module.scout_gate(tmp_project, ledger, config, 86500, lens="docs")
    with pytest.raises(module.ScoutError, match="cooldown"):
        module.scout_gate(tmp_project, ledger, config, 86499, lens="docs")


def test_manual_drafts_are_only_listed_with_all(tmp_path):
    base = tmp_path / "data"
    ledger = Ledger(base / "helmraiser.db")
    repo = tmp_path / "repo"
    seed_draft(ledger, repo, id="scouted")
    key = seed_draft(ledger, repo, id="manual")
    row = ledger.get_task(key)
    text = row["snapshot"].replace("source: scout ", "source: owner ")
    path = Path(row["source_path"])
    path.write_text(text)
    ledger.upsert_task(parse_task(text, path, {}), text)
    for command in (["drafts"], ["drafts", "list"]):
        result = runner.invoke(app, [*command, "--json", "--data-dir", str(base)])
        assert result.exit_code == 0, result.output
        assert [(r["id"], r["kind"]) for r in json.loads(result.output)] == [("scouted", "scout")]
        result = runner.invoke(app, [*command, "--all", "--json", "--data-dir", str(base)])
        assert {r["kind"] for r in json.loads(result.output)} == {"scout", "manual"}
