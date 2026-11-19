"""``helmraiser work`` (spec §5): dry-run, refusal on unavailable isolation, --once end-to-end."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, commands, resources
from helmraiser.isolation import process as process_module
from helmraiser.ledger.db import Ledger
from helmraiser.storage.state import StateManager
from helmraiser.task.project import worktree_manager
from tests.cli.test_held_base_sync import seed_pr_dependency_chain
from tests.conftest import FakePi

runner = CliRunner()

REVIEW_PASS = (
    "Looks correct.\n\n```json\n" '{"verdict": "pass", "findings": [], "evidence_checks": []}\n```'
)

TASK = """---
id: {task_id}
title: Fix the widget
project: {project}
status: draft
policy: review
isolation: {isolation}
budget:
  max_cost_usd: 2.0
  max_minutes: 30
  max_iterations: 3
verify:
  - name: marker
    run: test -f done.txt
  - name: content
    run: grep -q ok done.txt
---
## Goal

Make the widget work.
"""


@pytest.fixture(autouse=True)
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configured_config: dict) -> dict:
    config = configured_config
    monkeypatch.setattr(commands, "load_config", lambda: config)
    monkeypatch.setattr(commands, "get_intake_adapter", lambda config: None)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    return config


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def queue_task(
    tmp_project: Path,
    data_dir: Path,
    isolation: str = "none",
    task_id: str = "fix-widget",
) -> str:
    path = tmp_project / "topics" / f"{task_id}.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        TASK.format(project=tmp_project, isolation=isolation, task_id=task_id), encoding="utf-8"
    )
    result = runner.invoke(
        app, ["intake", str(path), "--yes", "--json", "--data-dir", str(data_dir)]
    )
    assert result.exit_code == 0, result.output
    return str(json.loads(result.output)["task_id"])


def work(*args: str, data_dir: Path):
    return runner.invoke(app, ["work", *args, "--data-dir", str(data_dir)])


@pytest.fixture
def notify_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "notify.log"
    bin_dir = tmp_path / "notify-bin"
    bin_dir.mkdir()
    executable = bin_dir / "notify-send"
    executable.write_text(
        '#!/bin/sh\nprintf "%s|%s\\n" "$1" "$2" >> "$NOTIFY_LOG"\nexit "${NOTIFY_EXIT:-0}"\n'
    )
    executable.chmod(0o755)
    monkeypatch.setenv("NOTIFY_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def test_admission_changes_after_selection(tmp_project, data_dir, fake_pi, monkeypatch):
    task_id = queue_task(tmp_project, data_dir)
    original = Ledger.claim_queued_run

    def lose(self, record, **kwargs):
        self.dequeue_task(record["task_id"])
        return original(self, record, **kwargs)

    monkeypatch.setattr(Ledger, "claim_queued_run", lose)
    result = work("--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert '"skipped"' in result.output
    with Ledger(data_dir / "helmraiser.db") as ledger:
        assert not ledger.list_runs()
        assert ledger.get_task(task_id)["status"] == "draft"
    assert (
        StateManager(tmp_project / ".helmraiser/state.db").get_locked_run_id("fix-widget") is None
    )
    assert not fake_pi.calls()


def test_failed_resume_claim_is_not_reselected(tmp_project, data_dir, fake_pi, monkeypatch):
    task_id = queue_task(tmp_project, data_dir)
    with Ledger(data_dir / "helmraiser.db") as ledger:
        ledger.create_run(
            {
                "run_id": "ba2777e0-65e8-4e17-8b3b-59765d94d0b1",
                "pi_version": "0.87.1",
                "task_id": task_id,
                "project": str(tmp_project),
                "status": "resuming",
                "policy": "review",
                "isolation": "none",
                "model_implement": "a/impl:medium",
                "model_review": "a/review:high",
            }
        )
        ledger.set_task_status(task_id, "resuming")
    attempts = []

    def refuse(self, run_id, **kwargs):
        attempts.append(run_id)
        return False

    monkeypatch.setattr(Ledger, "claim_resumable", refuse)
    result = work("--once", "--json", "--supervised", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert attempts == ["ba2777e0-65e8-4e17-8b3b-59765d94d0b1"]
    with Ledger(data_dir / "helmraiser.db") as ledger:
        assert ledger.get_run("ba2777e0-65e8-4e17-8b3b-59765d94d0b1")["status"] == "resuming"
    assert not fake_pi.calls()


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--once"],
        ["--max", "2"],
        ["--dry-run"],
        ["--watch"],
        ["--max-parallel", "2"],
        ["--once", "--supervised"],
    ],
)
@pytest.mark.parametrize("existing", [False, True])
def test_missing_work_models(args, existing, cli_env, data_dir, tmp_project, fake_pi, monkeypatch):
    if existing:
        queue_task(tmp_project, data_dir)
    cli_env["profiles"] = {}
    before = (data_dir / "helmraiser.db").read_bytes() if existing else None
    monkeypatch.setattr(
        commands, "_serve_from_snapshot", lambda *a, **k: pytest.fail("snapshot started")
    )
    result = work(*args, "--json", data_dir=data_dir)
    assert result.exit_code == 2, result.output
    assert json.loads(result.output) == {
        "error": "models_not_configured",
        "missing": ["plan", "implement", "fallback", "review"],
    }
    assert not fake_pi.calls()
    if existing:
        assert (data_dir / "helmraiser.db").read_bytes() == before
        assert not (data_dir / "runs").exists()
    else:
        assert not data_dir.exists()


@pytest.mark.parametrize("project_models", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_task_models_satisfy_work(cli_env, data_dir, tmp_project, fake_pi, project_models, dry_run):
    profiles = cli_env["profiles"]
    cli_env["profiles"] = {}
    cli_env["routing"]["scout"]["model"] = None
    models = {role: profiles[role]["model"] for role in ("plan", "implement", "fallback", "review")}
    if project_models:
        folder = tmp_project / ".helmraiser"
        folder.mkdir(exist_ok=True)
        (folder / "project.toml").write_text(
            "[models]\n" + "\n".join(f'{r} = "{m}"' for r, m in models.items())
        )
    task_id = queue_task(tmp_project, data_dir)
    if not project_models:
        with Ledger(data_dir / "helmraiser.db") as ledger:
            row = ledger.get_task(task_id)
            snapshot = row["snapshot"].replace(
                "---\n", "---\nmodels:\n" + "".join(f"  {r}: {m}\n" for r, m in models.items()), 1
            )
            ledger.set_task_snapshot(task_id, snapshot)
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {"files": [["done.txt", "ok\n"]]},
                "*-review": {"text": REVIEW_PASS},
            }
        }
    )
    result = work("--dry-run" if dry_run else "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert "--model None" not in result.output
    if dry_run:
        assert "pi --print" not in json.loads(result.output)["directives"]
        assert not fake_pi.calls()
    else:
        assert json.loads(result.output.splitlines()[0])["status"] == "awaiting_approval"
        assert [call["session_id"].rsplit("-", 1)[-1] for call in fake_pi.calls()] == [
            "implement",
            "review",
        ]


@pytest.mark.parametrize("args", [["--max", "1"], ["--once"], ["--max", "2"], []])
def test_mixed_queue_checks_only_selected_work(cli_env, data_dir, tmp_project, fake_pi, args):
    first = queue_task(tmp_project, data_dir, task_id="configured")
    second = queue_task(tmp_project, data_dir, task_id="unconfigured")
    models = cli_env["profiles"]
    cli_env["profiles"] = {}
    with Ledger(data_dir / "helmraiser.db") as ledger:
        row = ledger.get_task(first)
        overrides = "models:\n" + "".join(
            f"  {role}: {models[role]['model']}\n"
            for role in ("plan", "implement", "fallback", "review")
        )
        ledger.set_task_snapshot(first, row["snapshot"].replace("---\n", "---\n" + overrides, 1))
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {"files": [["done.txt", "ok\n"]]},
                "*-review": {"text": REVIEW_PASS},
            }
        }
    )
    result = work(*args, "--json", data_dir=data_dir)
    with Ledger(data_dir / "helmraiser.db") as ledger:
        assert ledger.get_task(second)["status"] == "queued"
        if args in (["--max", "1"], ["--once"]):
            assert result.exit_code == 0, result.output
            assert [run["task_id"] for run in ledger.list_runs()] == [first]
            assert ledger.get_task(first)["status"] == "awaiting_approval"
            assert len(fake_pi.calls()) == 2
        else:
            assert result.exit_code == 2, result.output
            assert json.loads(result.output) == {
                "error": "models_not_configured",
                "missing": ["plan", "implement", "fallback", "review"],
            }
            assert not ledger.list_runs()
            assert not fake_pi.calls()


@pytest.mark.parametrize(
    "argv", [["version"], ["status"], ["caps"], ["queue", "list"], ["queue", "next"], ["report"]]
)
def test_model_free_commands(cli_env, data_dir, fake_pi, argv):
    cli_env["profiles"] = {}
    options = [] if argv == ["version"] else ["--json", "--data-dir", str(data_dir)]
    result = runner.invoke(app, [*argv, *options])
    assert result.exit_code == 0, result.output
    assert "models_not_configured" not in result.output
    assert not fake_pi.calls()


def test_partial_work_models_use_queue_snapshot(cli_env, data_dir, tmp_project, fake_pi):
    task_id = queue_task(tmp_project, data_dir)
    cli_env["profiles"] = {"plan": "p/planner"}
    with Ledger(data_dir / "helmraiser.db") as ledger:
        row = ledger.get_task(task_id)
        snapshot = row["snapshot"].replace("---\n", "---\nmodels:\n  review: p/reviewer\n", 1)
        ledger.set_task_snapshot(task_id, snapshot)
        # Reading the source file instead of the queued snapshot would miss review.
        Path(row["source_path"]).unlink()
    result = work("--once", "--json", data_dir=data_dir)
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["missing"] == ["implement", "fallback"]
    assert not fake_pi.calls()


class TestDryRun:
    @pytest.mark.parametrize("as_json", [False, True])
    def test_logbook_dry_run_does_not_fetch_brief(
        self, tmp_project, data_dir, fake_pi, fake_lb, as_json
    ):
        task_id = queue_task(tmp_project, data_dir)
        fake_lb.write_script({"brief": {"stdout": "RECOGNIZABLE LOGBOOK BRIEF"}})
        result = work("--dry-run", *(["--json"] if as_json else []), data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert not [call for call in fake_lb.calls() if call["argv"][0] == "brief"]
        assert "RECOGNIZABLE LOGBOOK BRIEF" not in result.output
        assert "## Logbook" not in result.output
        assert "Logbook is unavailable" not in result.output
        assert fake_pi.calls() == []
        assert Ledger(data_dir / "helmraiser.db").list_runs() == []
        assert Ledger(data_dir / "helmraiser.db").get_task(task_id)["status"] == "queued"
        assert not (tmp_project / ".helmraiser" / "worktrees").exists()
        assert not (data_dir / "runs").exists()

    def test_prints_argv_and_prompts_and_spawns_nothing(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi
    ):
        task_id = queue_task(tmp_project, data_dir)
        result = work("--dry-run", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        plan = json.loads(result.output)
        assert plan["dry_run"] is True and plan["task_id"] == task_id
        argv = plan["argv"]
        assert argv[:3] == ["pi", "--mode", "json"]
        assert argv[argv.index("--session-id") + 1] == f"{plan['run_id']}-implement"
        assert argv[argv.index("--model") + 1] == "openai-codex/gpt-6.1-sol:high"
        assert argv[argv.index("--session-dir") + 1] == f"{plan['run_dir']}/sessions"
        assert argv[argv.index("--append-system-prompt") + 1] == f"{plan['run_dir']}/directives.md"
        assert "--no-extensions" in argv and argv[-2] == "--"
        # Sandbox or not, the CLI always wraps, so the prompt travels by file;
        # the dry run only plans the path and must not create the run dir.
        assert argv[-1] == f"@{plan['run_dir']}/prompt-implement-1.md"
        assert "Make the widget work." in plan["prompt"] and "`test -f done.txt`" in plan["prompt"]
        assert "governed implementation" in plan["directives"]
        assert "3 verify iterations, 30 minutes, $2.00" in plan["directives"]
        assert Path(plan["worktree"]) == worktree_manager(tmp_project).path_for(
            "fix-widget", plan["run_id"]
        )
        assert not Path(plan["worktree"]).parent.exists()
        # Nothing spawned, nothing created.
        assert fake_pi.calls() == []
        assert Ledger(data_dir / "helmraiser.db").list_runs() == []
        assert not (tmp_project / ".helmraiser" / "worktrees").exists()
        assert not Path(plan["run_dir"]).exists()
        assert Ledger(data_dir / "helmraiser.db").get_task(task_id)["status"] == "queued"

    def test_project_notes_are_in_dry_run_directives(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi
    ):
        notes = "Use node --import tsx instead of tsx --test."
        notes_path = tmp_project / ".helmraiser" / "notes.md"
        notes_path.parent.mkdir(exist_ok=True)
        notes_path.write_text(notes, encoding="utf-8")
        task_id = queue_task(tmp_project, data_dir)

        result = work("--dry-run", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        plan = json.loads(result.output)
        assert plan["directives"].count(notes) == 1
        assert plan["directives"].count("## Project notes (Helmraiser)") == 1
        assert fake_pi.calls() == []
        assert Ledger(data_dir / "helmraiser.db").list_runs() == []
        assert not (tmp_project / ".helmraiser" / "worktrees").exists()
        assert not Path(plan["run_dir"]).exists()
        assert Ledger(data_dir / "helmraiser.db").get_task(task_id)["status"] == "queued"

    def test_human_output(self, tmp_project: Path, data_dir: Path, fake_pi: FakePi):
        queue_task(tmp_project, data_dir)
        result = work("--dry-run", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert "argv: pi --mode json" in result.output
        assert "--- directives.md ---" in result.output and "--- prompt ---" in result.output
        assert "Begin." in result.output

    def test_empty_queue(self, data_dir: Path, fake_pi: FakePi):
        result = work("--dry-run", data_dir=data_dir)
        assert result.exit_code == 0 and "no queued task" in result.output


class TestRefusals:
    def test_project_approval_requirement_refuses_queued_auto_task(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi
    ) -> None:
        path = tmp_project / "topics" / "auto-task.md"
        path.parent.mkdir(exist_ok=True)
        text = TASK.format(project=tmp_project, isolation="process", task_id="auto-task")
        text = text.replace(
            "policy: review",
            "policy: auto\nevidence:\n  - name: proof\n    run: 'true'\n    artifacts: [proof.txt]\n    criteria: proof exists",
        )
        path.write_text(text)
        queued = runner.invoke(
            app,
            [
                "intake",
                str(path),
                "--yes",
                "--allow-auto",
                "--json",
                "--data-dir",
                str(data_dir),
            ],
        )
        assert queued.exit_code == 0, queued.output
        project_file = tmp_project / ".helmraiser" / "project.toml"
        project_file.parent.mkdir(exist_ok=True)
        project_file.write_text("[defaults]\nrequire_approval = true\n")

        result = work("--once", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        skipped = json.loads(result.output.splitlines()[0])
        assert skipped["skipped"].endswith(":auto-task")
        assert "project requires approval: policy auto is not allowed" in skipped["errors"]
        assert fake_pi.calls() == []

    def test_unavailable_isolation_is_refused(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi, monkeypatch
    ):
        queue_task(tmp_project, data_dir, isolation="process")
        original_which = process_module.shutil.which
        monkeypatch.setattr(
            process_module.shutil,
            "which",
            lambda name: None if name == "srt" else original_which(name),
        )
        result = work("--once", "--json", data_dir=data_dir)
        assert result.exit_code == 1, result.output
        out = json.loads(result.output)
        assert "isolation process unavailable" in out["error"]
        assert "srt not found on PATH" in out["error"]
        assert fake_pi.calls() == []
        assert Ledger(data_dir / "helmraiser.db").list_runs() == []

    def test_vm_isolation_is_refused(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi, monkeypatch: pytest.MonkeyPatch
    ):
        # Wide enough that a long tmp_path-derived task id never wraps the message.
        monkeypatch.setenv("COLUMNS", "1000")
        queue_task(tmp_project, data_dir, isolation="vm")
        result = work("--once", data_dir=data_dir)
        assert result.exit_code == 1
        assert "vm isolation not implemented" in result.output

    def test_any_pi_version_runs(self, tmp_project: Path, data_dir: Path, fake_pi: FakePi):
        queue_task(tmp_project, data_dir)
        fake_pi.write_script({"version": "0.99.2"})
        assert work("--dry-run", data_dir=data_dir).exit_code == 0

    def test_pi_without_version_fails(self, tmp_project: Path, data_dir: Path, fake_pi: FakePi):
        queue_task(tmp_project, data_dir)
        fake_pi.write_script({"version": ""})
        result = work("--dry-run", data_dir=data_dir)
        assert result.exit_code == 1
        assert "gave no version" in result.output


class TestCoreDumps:
    def test_work_disables_core_dumps_before_running_a_task(
        self,
        tmp_project: Path,
        data_dir: Path,
        fake_pi: FakePi,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        queue_task(tmp_project, data_dir)
        fake_pi.write_script(
            {
                "sessions": {
                    "*-implement": {
                        "text": "Implemented.",
                        "cost": 0.05,
                        "tokens_in": 500,
                        "tokens_out": 40,
                        "tools": ["edit"],
                        "files": [["done.txt", "ok\n"]],
                    },
                    "*-review": {
                        "text": REVIEW_PASS,
                        "cost": 0.02,
                        "tokens_in": 300,
                        "tokens_out": 20,
                    },
                }
            }
        )
        events: list[str] = []
        monkeypatch.setattr(commands, "disable_core_dumps", lambda: events.append("disable"))
        real = commands.GovernedRun

        class RecordingRun(real):
            def __init__(self, *args: object, **kwargs: object) -> None:
                events.append("run")
                super().__init__(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(commands, "GovernedRun", RecordingRun)
        result = work("--once", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert events == ["disable", "run"]

    def test_helper_sets_soft_zero_and_keeps_hard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[object, tuple[int, int]]] = []

        class FakeResource:
            RLIMIT_CORE = 4

            @staticmethod
            def getrlimit(res: int) -> tuple[int, int]:
                assert res == FakeResource.RLIMIT_CORE
                return (4096, 8192)

            @staticmethod
            def setrlimit(res: int, limits: tuple[int, int]) -> None:
                calls.append((res, limits))

        monkeypatch.setattr(resources, "resource", FakeResource)
        assert resources.disable_core_dumps() is True
        assert calls == [(FakeResource.RLIMIT_CORE, (0, 8192))]

    @pytest.mark.parametrize("error", [OSError("denied"), ValueError("bad limit")])
    def test_helper_swallows_setrlimit_errors(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        class FakeResource:
            RLIMIT_CORE = 4

            @staticmethod
            def getrlimit(res: int) -> tuple[int, int]:
                return (4096, 8192)

            @staticmethod
            def setrlimit(res: int, limits: tuple[int, int]) -> None:
                raise error

        monkeypatch.setattr(resources, "resource", FakeResource)
        assert resources.disable_core_dumps() is False

    def test_helper_tolerates_missing_resource_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(resources, "resource", None)
        assert resources.disable_core_dumps() is False


class TestOnce:
    def test_end_to_end_with_fake_pi(self, tmp_project: Path, data_dir: Path, fake_pi: FakePi):
        task_id = queue_task(tmp_project, data_dir)
        fake_pi.write_script(
            {
                "sessions": {
                    "*-implement": {
                        "text": "Implemented.",
                        "cost": 0.05,
                        "tokens_in": 500,
                        "tokens_out": 40,
                        "tools": ["edit"],
                        "files": [["done.txt", "ok\n"]],
                    },
                    "*-review": {
                        "text": REVIEW_PASS,
                        "cost": 0.02,
                        "tokens_in": 300,
                        "tokens_out": 20,
                    },
                }
            }
        )

        result = work("--once", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        lines = [json.loads(line) for line in result.output.strip().splitlines()]
        outcome, summary = lines
        assert outcome["status"] == "awaiting_approval", outcome
        assert outcome["iterations"] == 1 and outcome["cost_usd"] == 0.07
        assert outcome["verdict"]["verdict"] == "pass" and outcome["pending_approval"] == "merge"
        assert outcome["verify_results"] == [
            {**outcome["verify_results"][0], "name": "marker", "passed": True},
            {**outcome["verify_results"][1], "name": "content", "passed": True},
        ]
        assert summary["runs"] == [outcome] and summary["paused"] is None
        assert summary["summary"]["counts"]["awaiting_approval"] == 1
        run_id = outcome["run_id"]

        # The real pi argv reached the fake, from inside the worktree: implement, then review.
        calls = [c for c in fake_pi.calls() if c["session_id"]]
        call, review = calls
        assert call["session_id"] == f"{run_id}-implement"
        assert call["cwd"] == outcome["worktree_path"]
        assert call["env"]["HELMRAISER_RUN_ID"] == run_id
        assert call["env"]["HELMRAISER_WORKTREE"] == outcome["worktree_path"]
        assert call["argv"][:2] == ["--mode", "json"]
        spill = Path(call["argv"][-1][1:])
        assert spill == data_dir / "runs" / run_id / "prompt-implement-1.md"
        assert spill.read_text(encoding="utf-8").startswith("## Goal")
        assert review["session_id"] == f"{run_id}-review" and review["cwd"] == call["cwd"]
        assert (
            review["argv"][review["argv"].index("--model") + 1] == "openai-codex/gpt-6-astra:high"
        )
        assert review["argv"][review["argv"].index("--tools") + 1] == "read,grep,find,ls"
        assert "--no-context-files" in review["argv"]
        assert review["env"]["HELMRAISER_STAGE"] == "review"

        # Ledger state, run dir, worktree, status.json.
        ledger = Ledger(data_dir / "helmraiser.db")
        row = ledger.get_run(run_id)
        assert row["status"] == "awaiting_approval" and row["tokens_in"] == 800
        assert row["verdict"] == "pass"
        assert row["pi_version"] == "0.87.1"
        assert row["worktree_path"] == outcome["worktree_path"]
        assert ledger.get_task(task_id)["status"] == "awaiting_approval"
        (iteration,) = ledger.iterations(run_id)
        assert iteration["verify_passed"] == 1 and iteration["cost_usd"] == 0.05
        pi_types = [e["type"] for e in ledger.events(run_id) if e["source"] == "pi"]
        assert "agent_settled" in pi_types and "tool_execution_start" in pi_types
        assert "message_update" not in pi_types
        run_dir = data_dir / "runs" / run_id
        assert (run_dir / "task.md").exists() and (run_dir / "summary.md").exists()
        assert (run_dir / "events.jsonl").exists()
        assert (run_dir / "review.md").read_text() == REVIEW_PASS
        assert "done.txt" in (run_dir / "diff.patch").read_text()
        assert "### Review: pass" in (run_dir / "summary.md").read_text()
        worktree = Path(outcome["worktree_path"])
        assert (worktree / "done.txt").read_text() == "ok\n"
        branches = subprocess.run(
            ["git", "branch", "--list", outcome["branch_name"]],
            cwd=tmp_project,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert outcome["branch_name"] in branches
        status_file = Path(fake_pi.bin).parents[1] / "runtime" / "helmraiser" / "status.json"
        status = json.loads(status_file.read_text())
        assert status["awaiting_count"] == 1
        assert sum(values["5h"] for values in status["spend"].values()) == 0.07

        # Queue: the task is no longer queued; a second --once has nothing to do.
        again = work("--once", data_dir=data_dir)
        assert again.exit_code == 0 and "no queued task" in again.output

    def test_failed_run_exits_1_and_keeps_worktree(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi
    ):
        queue_task(tmp_project, data_dir)
        fake_pi.write_script({"default": {"text": "BLOCKED: no widget spec found"}})
        result = work("--once", data_dir=data_dir)
        assert result.exit_code == 1, result.output
        assert "blocked: no widget spec found" in result.output
        (row,) = Ledger(data_dir / "helmraiser.db").list_runs()
        assert row["status"] == "failed"

    def test_aborted_run_exits_1(self, tmp_project: Path, data_dir: Path, fake_pi: FakePi):
        queue_task(tmp_project, data_dir)
        fake_pi.write_script({"default": {"stop_reason": "aborted", "files": [["done.txt", ""]]}})
        result = work("--once", "--json", data_dir=data_dir)
        assert result.exit_code == 1, result.output
        outcome = json.loads(result.output.splitlines()[0])
        assert outcome["status"] == "aborted"
        assert Ledger(data_dir / "helmraiser.db").get_run(outcome["run_id"])["status"] == "aborted"

    def test_locked_task_is_skipped_and_next_task_runs(
        self, tmp_project: Path, tmp_path: Path, data_dir: Path, fake_pi: FakePi, git_init
    ):
        first_task_id = queue_task(tmp_project, data_dir)
        other = tmp_path / "other"
        other.mkdir()
        git_init(other)
        second_task_id = queue_task(other, data_dir)
        holder_run_id = "11111111-1111-4111-8111-111111111111"
        state = StateManager(tmp_project / ".helmraiser" / "state.db")
        state.create_run(holder_run_id, "fix-widget")
        assert state.acquire_lock(holder_run_id, "fix-widget", 1)
        fake_pi.write_script({"default": {"files": [["done.txt", "ok\n"]]}})

        result = work("--once", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        skipped, outcome, summary = [
            json.loads(line) for line in result.output.strip().splitlines()
        ]
        assert skipped == {"skipped": first_task_id, "held_by": holder_run_id}
        assert outcome["status"] == "awaiting_approval"
        assert summary["runs"] == [outcome] and summary["paused"] is None
        assert summary["summary"]["skipped_task_ids"] == [first_task_id]
        ledger = Ledger(data_dir / "helmraiser.db")
        (run_row,) = ledger.list_runs()
        assert run_row["task_id"] == second_task_id
        assert ledger.get_task(first_task_id)["status"] == "queued"

    def test_phantom_is_reconciled_before_same_project_dispatch(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi
    ):
        old_path = tmp_project / "topics" / "old-task.md"
        old_path.parent.mkdir(exist_ok=True)
        old_path.write_text(
            TASK.format(project=tmp_project, isolation="none", task_id="old-task").replace(
                "title: Fix the widget", "title: Old task"
            ),
            encoding="utf-8",
        )
        old_task_id = str(json.loads(invoke_intake(old_path, data_dir).output)["task_id"])
        queued_task_id = queue_task(tmp_project, data_dir)
        ledger = Ledger(data_dir / "helmraiser.db")
        ledger.set_task_status(old_task_id, "running")
        old_run_id = "11111111-1111-4111-8111-111111111111"
        ledger.create_run(
            {
                "run_id": old_run_id,
                "task_id": old_task_id,
                "project": str(tmp_project),
                "status": "running",
                "policy": "review",
                "isolation": "none",
                "model_implement": "a/impl:medium",
                "model_review": "a/review:high",
                "pi_version": "0.87.1",
            }
        )
        state = StateManager(tmp_project / ".helmraiser" / "state.db")
        state.create_run(old_run_id, "old-task")
        dead = subprocess.Popen(["true"])
        dead.wait()
        state.acquire_lock(old_run_id, "old-task", dead.pid)
        fake_pi.write_script({"default": {"files": [["done.txt", "ok\n"]]}})

        result = work("--once", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        reconciled, outcome, summary = [
            json.loads(line) for line in result.output.strip().splitlines()
        ]
        assert reconciled["reconciled"][0]["run_id"] == old_run_id
        assert ledger.get_run(old_run_id)["status"] == "failed"
        assert ledger.get_run(outcome["run_id"])["task_id"] == queued_task_id
        assert summary["runs"] == [outcome] and summary["paused"] is None
        assert "summary" in summary

    def test_max_drains_two_tasks(
        self, tmp_project: Path, tmp_path: Path, data_dir: Path, fake_pi: FakePi, git_init
    ):
        other = tmp_path / "other"
        other.mkdir()
        git_init(other)
        queue_task(tmp_project, data_dir)
        queue_task(other, data_dir)
        fake_pi.write_script({"default": {"cost": 0.01, "files": [["done.txt", "ok\n"]]}})
        result = work("--max", "2", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        summary = json.loads(result.output.strip().splitlines()[-1])
        assert [r["status"] for r in summary["runs"]] == ["awaiting_approval"] * 2
        batch = summary["summary"]
        assert batch["counts"] == {
            "landed": 0,
            "awaiting_approval": 2,
            "failed": 0,
            "aborted": 0,
        }
        assert batch["total_cost_usd"] == pytest.approx(
            sum(run["cost_usd"] for run in summary["runs"])
        )
        assert [run["task_id"] for run in batch["runs"]] == [
            run["task_id"] for run in summary["runs"]
        ]

    def test_human_batch_lists_two_drained_runs(
        self, tmp_project: Path, tmp_path: Path, data_dir: Path, fake_pi: FakePi, git_init
    ):
        other = tmp_path / "other"
        other.mkdir()
        git_init(other)
        queue_task(tmp_project, data_dir, task_id="first-widget")
        queue_task(other, data_dir, task_id="second-widget")
        fake_pi.write_script({"default": {"files": [["done.txt", "ok\n"]]}})

        result = work("--max", "2", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        batch = result.output[result.output.index("Batch:") :]
        assert batch.startswith("Batch: 2 runs — 2 awaiting approval — $0.00")
        assert "first-widget $0.00" in batch and "second-widget $0.00" in batch
        assert batch.rstrip().endswith("next: helmraiser report --since 24h")

    def test_human_batch_and_max_one_summary(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi
    ):
        queue_task(tmp_project, data_dir, task_id="first-widget")
        queue_task(tmp_project, data_dir, task_id="second-widget")
        fake_pi.write_script({"default": {"files": [["done.txt", "ok\n"]]}})

        result = work("--max", "1", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        assert "Batch: 1 run — 1 awaiting approval — $0.00" in result.output
        assert "  awaiting " in result.output and "first-widget $0.00" in result.output
        assert "second-widget $0.00" not in result.output
        assert result.output.rstrip().endswith("next: helmraiser report --since 24h")

    @pytest.mark.parametrize(
        ("option", "config_notify", "exit_code", "expected_calls"),
        [
            (None, True, 0, 1),
            ("--no-notify", True, 0, 0),
            (None, False, 0, 0),
            (None, True, 1, 1),
        ],
    )
    def test_batch_notification_controls_and_failure(
        self,
        tmp_project: Path,
        data_dir: Path,
        fake_pi: FakePi,
        notify_stub: Path,
        cli_env: dict,
        monkeypatch: pytest.MonkeyPatch,
        option: str | None,
        config_notify: bool,
        exit_code: int,
        expected_calls: int,
    ):
        queue_task(tmp_project, data_dir)
        fake_pi.write_script({"default": {"files": [["done.txt", "ok\n"]]}})
        cli_env["notify"] = config_notify
        monkeypatch.setenv("NOTIFY_EXIT", str(exit_code))

        result = work(*((option,) if option else ()), data_dir=data_dir)

        assert result.exit_code == 0, result.output
        calls = notify_stub.read_text().splitlines() if notify_stub.exists() else []
        assert len(calls) == expected_calls
        if calls:
            assert calls[0].startswith("Helmraiser|Batch: 1 run — 1 awaiting approval")

    def test_empty_queue_does_not_notify(self, data_dir: Path, fake_pi: FakePi, notify_stub: Path):
        result = work(data_dir=data_dir)
        assert result.exit_code == 0 and result.output.strip() == "no queued task"
        assert not notify_stub.exists()

    def test_empty_queue_json_prints_the_final_object(self, data_dir: Path, fake_pi: FakePi):
        result = work("--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"runs": [], "paused": None}

    def test_parked_run_holds_its_project_while_other_projects_run(
        self, tmp_project: Path, tmp_path: Path, data_dir: Path, fake_pi: FakePi, git_init
    ):
        other = tmp_path / "other"
        other.mkdir()
        git_init(other)
        first = queue_task(tmp_project, data_dir, task_id="first-widget")
        second = queue_task(tmp_project, data_dir, task_id="second-widget")
        third = queue_task(other, data_dir, task_id="third-widget")
        fake_pi.write_script({"default": {"files": [["done.txt", "ok\n"]]}})

        result = work("--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        *outcomes, final = [json.loads(line) for line in result.output.strip().splitlines()]
        assert [o["task_id"] for o in outcomes] == [first, third]
        assert [o["status"] for o in outcomes] == ["awaiting_approval"] * 2
        assert final["summary"]["held"] == [
            {
                "task_id": second,
                "run_id": outcomes[0]["run_id"],
                "status": "awaiting_approval",
                "pending": "merge",
            }
        ]
        assert final["summary"]["skipped_task_ids"] == []
        ledger = Ledger(data_dir / "helmraiser.db")
        assert ledger.get_task(second)["status"] == "queued"
        assert len(ledger.list_runs(project=str(tmp_project))) == 1

    def test_only_held_tasks_left_exits_like_an_empty_queue_and_names_them(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi, notify_stub: Path
    ):
        first = queue_task(tmp_project, data_dir, task_id="first-widget")
        fake_pi.write_script({"default": {"files": [["done.txt", "ok\n"]]}})
        parked = json.loads(work("--once", "--json", data_dir=data_dir).output.splitlines()[0])
        assert parked["task_id"] == first and parked["status"] == "awaiting_approval"
        notify_stub.unlink(missing_ok=True)
        second = queue_task(tmp_project, data_dir, task_id="second-widget")
        calls_before = len(fake_pi.calls())

        as_json = work("--json", data_dir=data_dir)
        text = work(data_dir=data_dir)

        assert as_json.exit_code == 0, as_json.output
        assert json.loads(as_json.output) == {
            "runs": [],
            "paused": None,
            "held": [
                {
                    "task_id": second,
                    "run_id": parked["run_id"],
                    "status": "awaiting_approval",
                    "pending": "merge",
                }
            ],
        }
        assert text.exit_code == 0, text.output
        assert text.output.splitlines() == [
            "no eligible task",
            f"  held: {second} by run {parked['run_id'][:8]} (awaiting merge approval)",
        ]
        assert len(fake_pi.calls()) == calls_before
        assert not notify_stub.exists()

    @pytest.mark.parametrize(
        ("args", "exclude", "served"),
        [
            (("--project", "ONE", "--project", "THREE"), [], ["one", "three"]),
            ((), ["TWO"], ["one", "three"]),
            # An explicit --project wins over the config's exclude list.
            (("--project", "TWO"), ["TWO"], ["two"]),
        ],
    )
    def test_project_scope_from_options_and_config(
        self,
        tmp_path: Path,
        data_dir: Path,
        fake_pi: FakePi,
        git_init,
        cli_env: dict,
        args: tuple[str, ...],
        exclude: list[str],
        served: list[str],
    ):
        projects = {}
        for name in ("one", "two", "three"):
            projects[name.upper()] = tmp_path / name
            projects[name.upper()].mkdir()
            git_init(projects[name.upper()])
            queue_task(projects[name.upper()], data_dir, task_id=f"{name}-widget")
        cli_env["worker_exclude_projects"] = [str(projects[key]) for key in exclude]
        fake_pi.write_script({"default": {"files": [["done.txt", "ok\n"]]}})
        argv = [str(projects.get(arg, arg)) for arg in args]

        result = work(*argv, "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        final = json.loads(result.output.strip().splitlines()[-1])
        assert sorted(run["task_id"].rsplit(":", 1)[1] for run in final["runs"]) == sorted(
            f"{name}-widget" for name in served
        )

    def test_batch_summary_line_names_held_tasks(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi
    ):
        queue_task(tmp_project, data_dir, task_id="first-widget")
        second = queue_task(tmp_project, data_dir, task_id="second-widget")
        fake_pi.write_script({"default": {"files": [["done.txt", "ok\n"]]}})

        result = work(data_dir=data_dir)

        assert result.exit_code == 0, result.output
        assert "Batch: 1 run — 1 awaiting approval — $0.00, 0 min — 1 held" in result.output
        run_id = Ledger(data_dir / "helmraiser.db").list_runs()[0]["run_id"]
        assert f"  held: {second} by run {run_id[:8]} (awaiting merge approval)" in result.output


class TestEvidenceEndToEnd:
    def test_evidence_step_and_review_session_with_fake_pi(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi, cli_env: dict
    ):
        # Isolate evidence collection and one review session from automatic feedback.
        cli_env["review_feedback_rounds"] = 0
        path = tmp_project / "topics" / "fix-widget.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            TASK.format(project=tmp_project, isolation="none", task_id="fix-widget").replace(
                "---\n## Goal",
                "evidence:\n  - name: listing\n"
                '    run: ls > "$HELMRAISER_EVIDENCE_DIR/ls.txt" && printf x > "$HELMRAISER_EVIDENCE_DIR/shot.png"\n'
                '    artifacts: ["ls.txt", "*.png"]\n'
                "    criteria: ls.txt lists done.txt\n"
                "setup: printf 'setup ran in %s\\n' \"$HELMRAISER_WORKTREE\" > setup.log\n"
                "env:\n  WIDGET_HOME: $HELMRAISER_WORKTREE/home\n"
                "---\n## Goal",
            ),
            encoding="utf-8",
        )
        assert invoke_intake(path, data_dir).exit_code == 0
        fake_pi.write_script(
            {
                "log_env": ["WIDGET_HOME"],
                "sessions": {
                    "*-implement": {
                        "text": "Implemented.",
                        "cost": 0.05,
                        "files": [["done.txt", "ok\n"]],
                    },
                    "*-review": {
                        "text": (
                            "Checked ls.txt.\n\n```json\n"
                            '{"verdict": "concerns", "findings": [{"severity": "minor", "file": "done.txt", '
                            '"line": 1, "note": "trailing newline"}], '
                            '"evidence_checks": [{"name": "listing", "meets_criteria": true, "note": "ok"}]}\n```'
                        ),
                        "cost": 0.01,
                    },
                },
            }
        )

        result = work("--once", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        outcome = json.loads(result.output.splitlines()[0])
        assert outcome["status"] == "awaiting_approval"
        (evidence,) = outcome["evidence_results"]
        assert evidence["ok"] and evidence["artifacts"] == ["ls.txt", "shot.png"]
        run_dir = data_dir / "runs" / outcome["run_id"]
        assert "done.txt" in (run_dir / "evidence" / "ls.txt").read_text()
        assert outcome["verdict"]["verdict"] == "concerns"
        assert outcome["verdict"]["evidence_checks"][0]["meets_criteria"] is True
        worktree = Path(outcome["worktree_path"])
        assert (worktree / "setup.log").read_text() == f"setup ran in {worktree}\n"
        calls = [c for c in fake_pi.calls() if c["session_id"]]
        implement, review = calls
        assert implement["env"]["WIDGET_HOME"] == f"{worktree}/home"
        assert review["env"]["WIDGET_HOME"] == f"{worktree}/home"
        prompt = review["argv"][-1]
        if prompt.startswith("@"):
            prompt = Path(prompt[1:]).read_text()
        assert f"@{run_dir / 'evidence' / 'shot.png'}" in prompt
        assert "Criteria: ls.txt lists done.txt" in prompt
        ledger = Ledger(data_dir / "helmraiser.db")
        (row,) = ledger.evidence(outcome["run_id"])
        assert row["name"] == "listing" and row["ok"] == 1
        assert ledger.get_run(outcome["run_id"])["verdict"] == "concerns"
        types = [e["type"] for e in ledger.events(outcome["run_id"]) if e["source"] == "helmraiser"]
        assert "setup_result" in types and "evidence_result" in types and "review_verdict" in types

    def test_setup_failure_fails_the_run_with_tail(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi
    ):
        path = tmp_project / "topics" / "fix-widget.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            TASK.format(project=tmp_project, isolation="none", task_id="fix-widget").replace(
                "---\n## Goal",
                "setup: echo installing; echo 'no such package' >&2; exit 7\n---\n## Goal",
            ),
            encoding="utf-8",
        )
        assert invoke_intake(path, data_dir).exit_code == 0
        result = work("--once", "--json", data_dir=data_dir)
        assert result.exit_code == 1, result.output
        outcome = json.loads(result.output.splitlines()[0])
        assert outcome["status"] == "failed"
        assert outcome["failure_reason"] == "setup: exit 7: no such package"
        assert fake_pi.calls() == [
            c for c in fake_pi.calls() if not c["session_id"]
        ]  # no pi session
        ledger = Ledger(data_dir / "helmraiser.db")
        event = next(e for e in ledger.events(outcome["run_id"]) if e["type"] == "setup_result")
        assert event["payload"]["exit_code"] == 7 and "no such package" in event["payload"]["tail"]

    def test_project_toml_defaults_apply(self, tmp_project: Path, data_dir: Path, fake_pi: FakePi):
        (tmp_project / ".helmraiser").mkdir(exist_ok=True)
        (tmp_project / ".helmraiser" / "project.toml").write_text(
            '[defaults]\nsetup = "echo default-setup > setup.log"\n\n[defaults.env]\nFROM_PROJECT = "yes"\n'
        )
        queue_task(tmp_project, data_dir)
        fake_pi.write_script(
            {"log_env": ["FROM_PROJECT"], "default": {"files": [["done.txt", "ok\n"]]}}
        )
        result = work("--once", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        outcome = json.loads(result.output.splitlines()[0])
        assert (Path(outcome["worktree_path"]) / "setup.log").read_text() == "default-setup\n"
        implement = [c for c in fake_pi.calls() if c["session_id"]][0]
        assert implement["env"]["FROM_PROJECT"] == "yes"

    def test_runs_do_not_inherit_worker_secrets(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("SSH_AUTH_SOCK", "/run/fake-agent.sock")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "fake-session")
        monkeypatch.setenv("GH_TOKEN", "fake-token")
        monkeypatch.setenv("WORKER_ONLY", "plain")
        (tmp_project / ".helmraiser").mkdir(exist_ok=True)
        (tmp_project / ".helmraiser" / "project.toml").write_text(
            '[defaults]\nsetup = "env > setup.env"\n\n[defaults.env]\nGH_TOKEN = "$GH_TOKEN"\n'
        )
        queue_task(tmp_project, data_dir)
        names = ["SSH_AUTH_SOCK", "CLAUDE_CODE_SESSION_ID", "GH_TOKEN", "WORKER_ONLY", "PATH"]
        fake_pi.write_script({"log_env": names, "default": {"files": [["done.txt", "ok\n"]]}})

        result = work("--once", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        outcome = json.loads(result.output.splitlines()[0])
        setup_env = (Path(outcome["worktree_path"]) / "setup.env").read_text().splitlines()
        assert "GH_TOKEN=fake-token" in setup_env and "WORKER_ONLY=plain" in setup_env
        assert not [line for line in setup_env if line.startswith(("SSH_AUTH", "CLAUDE_"))]
        implement = [c for c in fake_pi.calls() if c["session_id"]][0]
        assert set(implement["env"]) == {"GH_TOKEN", "PATH"} | {
            name for name in implement["env"] if name.startswith("HELMRAISER_")
        }
        assert implement["env"]["GH_TOKEN"] == "fake-token"


def test_batch_summary_carries_run_warnings():
    outcome = {
        "run_id": "abcdef0123",
        "task_id": "t",
        "status": "landed",
        "cost_usd": 0.0,
        "minutes": 1.0,
        "warnings": ["deny hides a tracked file"],
    }
    summary = commands._batch_summary([outcome], set(), warnings=["usage high"])
    assert summary["warnings"] == ["usage high", "abcdef01: deny hides a tracked file"]


def invoke_intake(path: Path, data_dir: Path):
    return runner.invoke(app, ["intake", str(path), "--yes", "--json", "--data-dir", str(data_dir)])


def seed_dependency_work(ledger, project, id, dependencies=(), status="queued"):
    from helmraiser.task.loader import render_task
    from helmraiser.task.model import VerifyStep
    from tests.ledger.test_db import make_spec

    spec = make_spec(
        project,
        id,
        depends_on=dependencies,
        status=status,
        verify=(VerifyStep("marker", "test -f done.txt"),),
    )
    ledger.upsert_task(spec, render_task(spec))
    return spec


@pytest.mark.parametrize("as_json", [False, True])
def test_depends_on_work_all_held(tmp_project, data_dir, fake_pi, as_json):
    ledger = Ledger(data_dir / "helmraiser.db")
    dependent = seed_dependency_work(ledger, tmp_project, "dependent", ("missing",))
    result = work("--once", *(["--json"] if as_json else []), data_dir=data_dir)
    assert result.exit_code == 0, result.output
    if as_json:
        payload = json.loads(result.output)
        assert payload["runs"] == []
        assert payload["held"][0]["unmet"][0]["id"] == "missing"
    else:
        assert "no eligible task" in result.output
        assert "missing: unknown dependency id" in result.output
    assert ledger.list_runs() == []
    assert ledger.get_task(dependent.task_id)["status"] == "queued"
    assert not (tmp_project / ".helmraiser/worktrees").exists()
    assert fake_pi.calls() == []


@pytest.mark.parametrize("same_project", [False, True])
def test_depends_on_work_skips_for_eligible(
    tmp_project, tmp_path, data_dir, fake_pi, git_init, same_project
):
    other = tmp_project
    if not same_project:
        other = tmp_path / "other"
        other.mkdir()
        git_init(other)
    ledger = Ledger(data_dir / "helmraiser.db")
    held = seed_dependency_work(ledger, tmp_project, "held", ("missing",))
    free = seed_dependency_work(ledger, other, "free")
    dry = work("--dry-run", "--json", data_dir=data_dir)
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["task_id"] == free.task_id
    assert fake_pi.calls() == []
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {"files": [["done.txt", "ok\n"]]},
                "*-review": {"text": REVIEW_PASS},
            }
        }
    )
    result = work("--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[0])
    assert payload["task_id"] == free.task_id
    assert payload["status"] == "awaiting_approval"
    assert ledger.get_task(held.task_id)["status"] == "queued"
    assert [r["task_id"] for r in ledger.list_runs()] == [free.task_id]
    assert len([call for call in fake_pi.calls() if call["session_id"].endswith("-implement")]) == 1


def test_depends_on_work_local_base_release(tmp_project, data_dir, fake_pi):
    from tests.ledger.test_db import run_row
    from tests.ledger.test_depends_on import feature, git

    _, head = feature(tmp_project)
    ledger = Ledger(data_dir / "helmraiser.db")
    upstream = seed_dependency_work(ledger, tmp_project, "upstream", status="landed")
    dependent = seed_dependency_work(ledger, tmp_project, "dependent", ("upstream",))
    ledger.create_run(
        {**run_row("r", upstream.task_id, str(tmp_project), "landed"), "head_commit": head}
    )
    before = work("--once", "--json", data_dir=data_dir)
    assert before.exit_code == 0, before.output
    assert json.loads(before.output)["held"][0]["unmet"][0]["reason"] == "not_in_base"
    assert fake_pi.calls() == []
    git(tmp_project, "merge", "--no-ff", "feature", "-m", "merge")
    git(tmp_project, "branch", "-d", "feature")  # leave Helmraiser's feature/ namespace free
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {"files": [["done.txt", "ok\n"]]},
                "*-review": {"text": REVIEW_PASS},
            }
        }
    )
    result = work("--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output.splitlines()[0])["task_id"] == dependent.task_id


@pytest.fixture
def recorded_runs(monkeypatch):
    from helmraiser.loop.runner import RunOutcome

    calls = []

    class Recorder:
        def __init__(self, task, *, ledger, **kwargs):
            self.task = task
            self.ledger = ledger
            self.run_id = f"recorded-{task.id}"

        async def run(self):
            assert self.ledger.next_queued()["task_id"] == self.task.task_id
            calls.append(self.task.task_id)
            self.ledger.set_task_status(self.task.task_id, "landed")
            return RunOutcome(self.run_id, "landed", task_id=self.task.task_id)

    monkeypatch.setattr(commands, "GovernedRun", Recorder)
    return calls


@pytest.mark.parametrize("as_json", [False, True])
def test_held_pr_foreground_sync_before_selection(
    tmp_project, data_dir, fake_pi, recorded_runs, as_json
):
    ledger = Ledger(data_dir / "helmraiser.db")
    dependent, _ = seed_pr_dependency_chain(ledger, tmp_project)
    result = work("--once", "--no-notify", *(["--json"] if as_json else []), data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert recorded_runs == [dependent.task_id]
    if as_json:
        payload = json.loads(result.output.splitlines()[0])["base_sync"]
        assert payload["status"] == "synced" and payload["project"] == str(tmp_project)
        assert payload["base_branch"] == "main"
    else:
        assert f"base sync {tmp_project} (main): synced" in result.output
    assert fake_pi.calls() == []


def test_held_pr_foreground_drain_checks_once(
    tmp_project, data_dir, fake_pi, recorded_runs, monkeypatch
):
    from dataclasses import replace

    from helmraiser.task.loader import render_task

    ledger = Ledger(data_dir / "helmraiser.db")
    dependent, _ = seed_pr_dependency_chain(ledger, tmp_project)
    second = replace(dependent, id="second")
    ledger.upsert_task(second, render_task(second))
    calls = []
    real = commands.HeldBaseSync.check

    def check(self, ledger, scope, **kwargs):
        calls.append(scope)
        return real(self, ledger, scope, **kwargs)

    monkeypatch.setattr(commands.HeldBaseSync, "check", check)
    result = work("--json", "--no-notify", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert recorded_runs == [dependent.task_id, second.task_id]
    assert len(calls) == 1


@pytest.mark.parametrize("option", ["--dry-run", "--supervised"])
def test_held_pr_foreground_skips_new_check(tmp_project, data_dir, fake_pi, monkeypatch, option):
    from helmraiser.cli.base_sync import HeldBaseSync

    ledger = Ledger(data_dir / "helmraiser.db")
    seed_pr_dependency_chain(ledger, tmp_project)

    def check(*args):
        pytest.fail("dry runs and supervised children must not synchronize")

    monkeypatch.setattr(HeldBaseSync, "check", check)
    result = work("--once", option, "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert "base_sync" not in result.output
    if option == "--supervised":
        assert "not_in_base" in result.output
    else:
        assert result.output == "no queued task\n"


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("failure", ["paused", "fetch"])
def test_held_pr_foreground_failure_preserves_hold(
    tmp_project, data_dir, fake_pi, recorded_runs, as_json, failure
):
    from tests.ledger.test_depends_on import git

    ledger = Ledger(data_dir / "helmraiser.db")
    _, origin = seed_pr_dependency_chain(ledger, tmp_project)
    if failure == "paused":
        ledger.pause_queue("test")
    else:
        git(tmp_project, "remote", "set-url", "origin", str(origin / "missing"))
    result = work("--once", *(["--json"] if as_json else []), data_dir=data_dir)
    assert result.exit_code == (3 if failure == "paused" else 0), result.output
    assert recorded_runs == []
    if failure == "paused":
        assert "base_sync" not in result.output and "paused" in result.output
    elif as_json:
        sync, summary = [json.loads(line) for line in result.output.splitlines()]
        assert sync["base_sync"]["status"] == "failed"
        assert sync["base_sync"]["detail"]
        assert summary["held"][0]["unmet"][0]["reason"] == "not_in_base"
    else:
        assert "base sync" in result.output and "fetch_failed" in result.output
        assert "no eligible task" in result.output
