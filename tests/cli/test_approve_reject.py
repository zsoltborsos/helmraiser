"""``helmraiser approve`` / ``helmraiser reject`` (spec §5, §4.5.3, §4.6)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, commands
from helmraiser.ledger.db import Ledger
from helmraiser.ledger.rundir import RunDir
from helmraiser.loop.review import EVIDENCE_INLINE_LIMIT, OWNER_NOTE_INLINE_LIMIT
from helmraiser.loop.runner import RunOutcome
from helmraiser.pipeline.worktree import WorktreeManager
from helmraiser.storage.state import StateManager
from helmraiser.task.project import worktree_manager
from tests.conftest import FakePi, configured_test_config
from tests.pipeline.test_base_sync import setup_remote

runner = CliRunner()

TASK = """---
id: {task_id}
title: Fix the widget
project: {project}
status: draft
policy: {policy}
delivery: {delivery}
isolation: none
budget:
  max_cost_usd: 2.0
  max_minutes: 30
  max_iterations: 3
verify:
  - name: marker
    run: test -f {marker}
---
## Goal

Make the widget work.
"""

REVIEW_PASS = '```json\n{"verdict": "pass", "findings": [], "evidence_checks": []}\n```'


@pytest.fixture(autouse=True)
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    config = configured_test_config()
    monkeypatch.setattr(commands, "load_config", lambda: config)
    monkeypatch.setattr(commands, "get_intake_adapter", lambda config: None)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    return config


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def invoke(*args: str, data_dir: Path):
    return runner.invoke(app, [*args, "--data-dir", str(data_dir)])


def queue_and_run(
    tmp_project: Path,
    data_dir: Path,
    fake_pi: FakePi,
    policy: str = "review",
    delivery: str = "merge",
    task_id: str = "fix-widget",
    marker: str = "done.txt",
    content: str = "ok\n",
    paths: tuple[str, ...] | None = None,
) -> dict:
    path = tmp_project.parent / "tasks" / f"{task_id}.md"  # keep the primary clean
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        TASK.format(
            project=tmp_project,
            policy=policy,
            delivery=delivery,
            task_id=task_id,
            marker=marker,
        ),
        encoding="utf-8",
    )
    if paths is not None:
        path.write_text(
            path.read_text().replace(
                "isolation: none\n", "isolation: none\npaths: " + json.dumps(paths) + "\n"
            )
        )
    result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    fake_pi.write_script(
        {
            "sessions": {
                "*-plan": {"text": "# Plan\n\n1. touch done.txt", "cost": 0.01},
                "*-implement": {"text": "Done.", "cost": 0.05, "files": [[marker, content]]},
                "*-review": {"text": REVIEW_PASS, "cost": 0.02},
            }
        }
    )
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    outcome = json.loads(result.output.splitlines()[0])
    assert outcome["status"] == "awaiting_approval", outcome
    return outcome


def test_outcome_prints_explicit_base(data_dir: Path, capsys):
    outcome = RunOutcome("run-1", "failed", branch_name="feature/task/run-1")
    task = type("Task", (), {"task_id": "task-1", "base": "programme/x"})()

    commands._print_outcome(outcome, task, data_dir)

    assert "branch: feature/task/run-1\n  base: programme/x" in capsys.readouterr().out


def test_awaiting_plan_outcome_prints_plan_path(data_dir: Path, capsys):
    outcome = RunOutcome("run-1", "awaiting_approval", pending_approval="plan")
    task = type("Task", (), {"task_id": "task-1"})()

    commands._print_outcome(outcome, task, data_dir)

    assert f"plan: {data_dir / 'runs' / 'run-1' / 'plan.md'}" in capsys.readouterr().out


def test_plan_approval_without_configured_models(tmp_project, data_dir, fake_pi, cli_env):
    outcome = queue_and_run(tmp_project, data_dir, fake_pi, policy="approve-plan-first")
    cli_env["profiles"] = {}
    before = fake_pi.calls()
    for command in ("show", "log"):
        result = invoke(command, outcome["run_id"], "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert "models_not_configured" not in result.output
    result = invoke("approve", outcome["run_id"], "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert "models_not_configured" not in result.output
    assert fake_pi.calls() == before
    assert Ledger(data_dir / "helmraiser.db").get_run(outcome["run_id"])["status"] == "resuming"


class TestApprove:
    def test_second_sibling_rebases_after_first_lands(
        self, tmp_project, data_dir, fake_pi, cli_env
    ):
        cli_env["max_runs_per_project"] = 2
        first = queue_and_run(
            tmp_project,
            data_dir,
            fake_pi,
            task_id="first",
            marker="first.txt",
            paths=("first.txt",),
        )
        second = queue_and_run(
            tmp_project,
            data_dir,
            fake_pi,
            task_id="second",
            marker="second.txt",
            paths=("second.txt",),
        )
        assert first["base_commit"] == second["base_commit"]
        manager = worktree_manager(tmp_project)
        assert all(
            Path(o["worktree_path"]).exists() and manager._metadata_path(o["run_id"]).exists()
            for o in (first, second)
        )
        result = invoke("approve", first["run_id"], "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        first_head = json.loads(result.output)["head"]
        assert not Path(first["worktree_path"]).exists()
        assert not manager._metadata_path(first["run_id"]).exists()
        assert Path(second["worktree_path"]).exists()
        assert manager._metadata_path(second["run_id"]).exists()
        result = invoke("approve", second["run_id"], "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        second_head = json.loads(result.output)["head"]
        assert second_head != second["head_commit"]
        assert _git(tmp_project, "rev-parse", "HEAD") == second_head
        assert _git(tmp_project, "rev-parse", "HEAD^") == first_head
        assert (tmp_project / "first.txt").read_text() == "ok\n"
        assert (tmp_project / "second.txt").read_text() == "ok\n"
        with Ledger(data_dir / "helmraiser.db") as ledger:
            for outcome in (first, second):
                row = ledger.get_run(outcome["run_id"])
                assert row["status"] == ledger.get_task(row["task_id"])["status"] == "landed"
                assert not Path(outcome["worktree_path"]).exists()
                assert not manager._metadata_path(outcome["run_id"]).exists()
            assert ledger.get_run(second["run_id"])["head_commit"] == second_head

    @pytest.mark.parametrize("legacy", [False, True])
    def test_merge_lands_and_removes_worktree(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi, legacy: bool, monkeypatch
    ):
        def forbidden(*args, **kwargs):
            pytest.fail("merge delivery must not require a current upstream base")

        monkeypatch.setattr(WorktreeManager, "require_current_base", forbidden)
        project_file = tmp_project / ".helmraiser" / "project.toml"
        if legacy:
            project_file.parent.mkdir(exist_ok=True)
            project_file.write_text(
                f'[defaults]\nworktree_dir = "{project_file.parent / "worktrees"}"\n'
            )
        outcome = queue_and_run(tmp_project, data_dir, fake_pi)
        run_id = outcome["run_id"]
        metadata_path = Path(outcome["worktree_path"]).parent / f"{run_id}.json"
        if legacy:
            project_file.unlink()
        before = _git(tmp_project, "rev-parse", "HEAD")

        summary = RunDir(run_id, data_dir).summary_md
        heading, body = summary.read_text().split("\n", 1)
        assert heading == "## Fix the widget — awaiting_approval"
        result = invoke("approve", run_id, "--note", "lgtm", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        assert summary.read_text() == "## Fix the widget — landed\n" + body
        out = json.loads(result.output)
        assert out == {
            "approved": "merge",
            "run_id": run_id,
            "task_id": outcome["run_id"] and out["task_id"],
            "status": "landed",
            "branch_name": outcome["branch_name"],
            "head": outcome["head_commit"],
        }
        assert _git(tmp_project, "rev-parse", "HEAD") == outcome["head_commit"] != before
        assert (tmp_project / "done.txt").read_text() == "ok\n"
        assert not Path(outcome["worktree_path"]).exists()
        assert not metadata_path.exists()
        assert outcome["branch_name"] not in _git(tmp_project, "branch", "--list")
        ledger = Ledger(data_dir / "helmraiser.db")
        row = ledger.get_run(run_id)
        assert row["status"] == "landed" and row["failure_reason"] is None and row["ended_at"]
        assert ledger.get_task(out["task_id"])["status"] == "landed"
        (approval,) = ledger.approvals(run_id)
        assert (approval["kind"], approval["decision"], approval["note"]) == (
            "merge",
            "approved",
            "lgtm",
        )
        status = json.loads(
            (Path(fake_pi.bin).parents[1] / "runtime" / "helmraiser" / "status.json").read_text()
        )
        assert status["awaiting_count"] == 0

    def test_unique_prefix_and_human_output(self, tmp_project, data_dir, fake_pi):
        outcome = queue_and_run(tmp_project, data_dir, fake_pi)
        result = invoke("approve", outcome["run_id"][:8], data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert "landed" in result.output and outcome["head_commit"][:12] in result.output

    def test_finalize_refused_keeps_run_awaiting(self, tmp_project, data_dir, fake_pi):
        outcome = queue_and_run(tmp_project, data_dir, fake_pi)
        (tmp_project / "README.md").write_text("dirty\n")
        summary = RunDir(outcome["run_id"], data_dir).summary_md
        before = summary.read_bytes()
        result = invoke("approve", outcome["run_id"], "--json", data_dir=data_dir)
        assert result.exit_code == 1, result.output
        assert summary.read_bytes() == before
        error = json.loads(result.output)["error"]
        assert "finalize_refused: primary checkout is dirty" in error
        assert "rework" not in error
        ledger = Ledger(data_dir / "helmraiser.db")
        row = ledger.get_run(outcome["run_id"])
        assert row["status"] == "awaiting_approval"
        assert row["failure_reason"].startswith("finalize_refused")
        assert ledger.pending_approval(outcome["run_id"])["kind"] == "merge"
        assert len(ledger.approvals(outcome["run_id"])) == 1
        assert Path(outcome["worktree_path"]).is_dir()

    def test_approve_rebases_parked_run_onto_moved_base(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi
    ):
        run = queue_and_run(tmp_project, data_dir, fake_pi, marker="second.txt")
        (tmp_project / "first.txt").write_text("ok\n")
        _git(tmp_project, "add", "first.txt")
        _git(tmp_project, "commit", "-q", "-m", "landed meanwhile")
        moved = _git(tmp_project, "rev-parse", "HEAD")
        assert moved != run["base_commit"]

        result = invoke("approve", run["run_id"], "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        assert _git(tmp_project, "rev-parse", "HEAD^") == moved
        assert (tmp_project / "first.txt").read_text() == "ok\n"
        assert (tmp_project / "second.txt").read_text() == "ok\n"

    def test_approve_refuses_conflicting_rebase(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi
    ):
        run = queue_and_run(
            tmp_project, data_dir, fake_pi, marker="README.md", content="second run\n"
        )
        (tmp_project / "README.md").write_text("first run\n")
        _git(tmp_project, "commit", "-q", "-am", "landed meanwhile")

        result = invoke("approve", run["run_id"], "--json", data_dir=data_dir)

        assert result.exit_code == 1, result.output
        error = json.loads(result.output)["error"]
        assert "finalize_refused: rebase onto moved HEAD failed" in error
        assert f"helmraiser rework {run['run_id']} --rebase" in error
        row = Ledger(data_dir / "helmraiser.db").get_run(run["run_id"])
        assert row["status"] == "awaiting_approval"
        assert row["failure_reason"].startswith("finalize_refused: rebase onto moved HEAD failed")
        assert "rework" not in row["failure_reason"]
        assert Path(run["worktree_path"]).is_dir()
        assert _git(Path(run["worktree_path"]), "status", "--porcelain") == ""

    @pytest.mark.parametrize("as_json", [False, True])
    @pytest.mark.parametrize("with_hint", [False, True])
    def test_pr_delivery_opens_url_and_rebases(
        self,
        tmp_project: Path,
        data_dir: Path,
        fake_pi: FakePi,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        as_json: bool,
        with_hint: bool,
    ):
        hint = (
            {
                "task_ids": ["P:later"],
                "worker": "none",
                "worker_serves_project": False,
                "project": str(tmp_project),
            }
            if with_hint
            else None
        )
        monkeypatch.setattr(commands, "_eligible_hint", lambda *args: hint)
        remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        _git(tmp_project, "remote", "add", "origin", str(remote))
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        calls = tmp_path / "gh-calls"
        gh = bin_dir / "gh"
        gh.write_text(
            f'#!/bin/sh\necho "$*" >> {calls}\necho https://example.test/pr/1\n',
            encoding="utf-8",
        )
        gh.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
        base_branch = _git(tmp_project, "branch", "--show-current")
        outcome = queue_and_run(tmp_project, data_dir, fake_pi, delivery="pr")
        summary = RunDir(outcome["run_id"], data_dir).summary_md
        heading, body = summary.read_text().split("\n", 1)
        assert heading == "## Fix the widget — awaiting_approval"
        assert "- pr:" not in body
        (tmp_project / "base-moved.txt").write_text("moved\n", encoding="utf-8")
        _git(tmp_project, "add", ".")
        _git(tmp_project, "commit", "-m", "move base")

        result = invoke(
            "approve", outcome["run_id"], *(["--json"] if as_json else []), data_dir=data_dir
        )

        assert result.exit_code == 0, result.output
        if as_json:
            payload = json.loads(result.stdout)
            expected = {
                "approved": "merge",
                "run_id": outcome["run_id"],
                "task_id": outcome["task_id"],
                "status": "landed",
                "branch_name": outcome["branch_name"],
                "pr_url": "https://example.test/pr/1",
            }
            if hint:
                expected["eligible"] = hint
            assert payload == expected
        else:
            assert "opened https://example.test/pr/1" in result.stdout
            lines = result.stdout.splitlines()
            assert lines[-1] == "https://example.test/pr/1"
            if with_hint:
                assert "  now eligible: P:later" in lines[:-1]
                assert "  no worker is running: helmraiser worker start" in lines[:-1]
            else:
                assert "now eligible:" not in result.stdout
        heading, updated_body = summary.read_text().split("\n", 1)
        assert heading == "## Fix the widget — landed"
        lines = updated_body.splitlines(keepends=True)
        pr_line = "- pr: https://example.test/pr/1\n"
        assert lines.count(pr_line) == 1
        pr_index = lines.index(pr_line)
        anchor = "- base:" if any(line.startswith("- base:") for line in lines) else "- branch:"
        assert lines[pr_index - 1].startswith(anchor)
        assert "".join(line for line in lines if line != pr_line) == body
        assert f"--base {base_branch} --head {outcome['branch_name']}" in calls.read_text(
            encoding="utf-8"
        )
        row = Ledger(data_dir / "helmraiser.db").get_run(outcome["run_id"])
        assert row["status"] == "landed" and row["pr_url"] == "https://example.test/pr/1"
        assert outcome["branch_name"] in _git(tmp_project, "branch", "--list")

    @pytest.mark.parametrize("as_json", [False, True])
    def test_pr_stale_other_worktree_refuses(
        self, tmp_project, tmp_path, data_dir, fake_pi, monkeypatch, as_json
    ):
        outcome = queue_and_run(tmp_project, data_dir, fake_pi, delivery="pr")
        run_id = outcome["run_id"]
        origin, _, old, _ = setup_remote(tmp_project, tmp_path)
        _git(tmp_project, "checkout", "-b", "scratch")
        side = tmp_path / "side"
        _git(tmp_project, "worktree", "add", str(side), "main")
        calls = []

        def open_pr(*args, **kwargs):
            calls.append((args, kwargs))
            return "https://example.test/pr/1"

        monkeypatch.setattr(commands, "open_pull_request", open_pr)
        manager = worktree_manager(tmp_project)
        metadata = manager._metadata_path(run_id).read_bytes()
        summary = RunDir(run_id, data_dir).summary_md.read_bytes()
        with StateManager(tmp_project / ".helmraiser" / "state.db") as state:
            project_run = state.get_run(run_id)
        with Ledger(data_dir / "helmraiser.db") as ledger:
            before = ledger.get_run(run_id)
            approvals = ledger.approvals(run_id)
            events = ledger.events(run_id)
            result = invoke("approve", run_id, *(["--json"] if as_json else []), data_dir=data_dir)
            assert result.exit_code == 1, result.output
            error = json.loads(result.output)["error"] if as_json else result.output
            assert all(word in error for word in ("finalize_refused", "main", "origin", "fetch"))
            row = ledger.get_run(run_id)
            assert row["status"] == ledger.get_task(row["task_id"])["status"] == "awaiting_approval"
            assert row["ended_at"] == before["ended_at"] and row["pr_url"] is None
            assert {k: v for k, v in row.items() if k != "failure_reason"} == {
                k: v for k, v in before.items() if k != "failure_reason"
            }
            assert ledger.approvals(run_id) == approvals
            assert ledger.pending_approval(run_id)["decision"] == "pending"
            assert ledger.events(run_id) == events
        with StateManager(tmp_project / ".helmraiser" / "state.db") as state:
            assert state.get_run(run_id) == project_run
        assert calls == []
        assert _git(origin, "for-each-ref", f"refs/heads/{outcome['branch_name']}") == ""
        assert _git(tmp_project, "rev-parse", outcome["branch_name"]) == outcome["head_commit"]
        assert _git(tmp_project, "rev-parse", "main") == old
        assert _git(tmp_project, "rev-parse", "HEAD") == old
        assert _git(side, "rev-parse", "HEAD") == old
        for checkout in (tmp_project, side):
            assert (checkout / "README.md").read_text() == "# Test Project\n"
            assert _git(checkout, "status", "--porcelain", "--untracked-files=no") == ""
        assert Path(outcome["worktree_path"]).is_dir()
        assert _git(Path(outcome["worktree_path"]), "rev-parse", "HEAD") == outcome["head_commit"]
        assert manager._metadata_path(run_id).read_bytes() == metadata
        assert RunDir(run_id, data_dir).summary_md.read_bytes() == summary

    def test_pr_stale_unchecked_base_synchronizes(
        self, tmp_project, tmp_path, data_dir, fake_pi, monkeypatch
    ):
        outcome = queue_and_run(tmp_project, data_dir, fake_pi, delivery="pr")
        run_id = outcome["run_id"]
        origin, _, old, new = setup_remote(tmp_project, tmp_path)
        _git(tmp_project, "checkout", "-b", "scratch")
        calls = []

        def open_pr(*args, **kwargs):
            calls.append((args, kwargs))
            return "https://example.test/pr/1"

        monkeypatch.setattr(commands, "open_pull_request", open_pr)
        with Ledger(data_dir / "helmraiser.db") as ledger:
            since = ledger.events(run_id)[-1]["id"]
            result = invoke("approve", run_id, "--json", data_dir=data_dir)
            assert result.exit_code == 0, result.output
            row = ledger.get_run(run_id)
            assert row["status"] == "landed"
            head = row["head_commit"]
            events = ledger.events(run_id, since_id=since)
            (sync,) = [e for e in events if e["type"] == "base_sync"]
            (delivered,) = [e for e in events if e["type"] == "delivered"]
            assert sync["id"] < delivered["id"]
            assert sync["payload"] == {
                "status": "synced",
                "reason": "fast_forward",
                "base_branch": "main",
                "remote": "origin",
                "upstream": "refs/heads/main",
                "from_commit": old,
                "to_commit": new,
                "upstream_commit": new,
            }
        assert len(calls) == 1
        assert calls[0][1]["base"] == "main"
        assert _git(tmp_project, "rev-parse", "main") == new
        assert _git(tmp_project, "rev-parse", f"{head}^") == new
        assert _git(origin, "rev-parse", outcome["branch_name"]) == head
        assert _git(tmp_project, "symbolic-ref", "--short", "HEAD") == "scratch"
        assert _git(tmp_project, "rev-parse", "HEAD") == old
        assert (tmp_project / "README.md").read_text() == "# Test Project\n"
        assert _git(tmp_project, "status", "--porcelain", "--untracked-files=no") == ""

    def test_pr_base_sync_recorded_before_finalize_failure(
        self, tmp_project, tmp_path, data_dir, fake_pi, monkeypatch
    ):
        outcome = queue_and_run(tmp_project, data_dir, fake_pi, delivery="pr")
        _, _, _, new = setup_remote(tmp_project, tmp_path)

        def refuse(*args, **kwargs):
            raise RuntimeError("later finalization failed")

        monkeypatch.setattr(WorktreeManager, "finalize_remote", refuse)
        with Ledger(data_dir / "helmraiser.db") as ledger:
            since = ledger.events(outcome["run_id"])[-1]["id"]
            result = invoke("approve", outcome["run_id"], "--json", data_dir=data_dir)
            assert result.exit_code == 1, result.output
            assert "later finalization failed" in json.loads(result.output)["error"]
            assert ledger.get_run(outcome["run_id"])["status"] == "awaiting_approval"
            events = ledger.events(outcome["run_id"], since_id=since)
            assert [e["type"] for e in events] == ["base_sync"]
            assert events[0]["payload"]["upstream_commit"] == new

    def test_not_awaiting(self, tmp_project, data_dir, fake_pi):
        outcome = queue_and_run(tmp_project, data_dir, fake_pi)
        assert invoke("approve", outcome["run_id"], data_dir=data_dir).exit_code == 0
        again = invoke("approve", outcome["run_id"], "--json", data_dir=data_dir)
        assert again.exit_code == 2
        assert "is landed, not awaiting approval" in json.loads(again.output)["error"]

    def test_unknown_run(self, data_dir, fake_pi):
        result = invoke("approve", "nope", "--json", data_dir=data_dir)
        assert result.exit_code == 1 and "unknown run" in json.loads(result.output)["error"]

    def test_plan_approval_hands_the_run_back_without_running_it(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi
    ):
        outcome = queue_and_run(tmp_project, data_dir, fake_pi, policy="approve-plan-first")
        run_id = outcome["run_id"]
        assert outcome["pending_approval"] == "plan" and outcome["iterations"] == 0
        plan_path = data_dir / "runs" / run_id / "plan.md"
        assert plan_path.read_text() == "# Plan\n\n1. touch done.txt\n"

        original_plan = "# Plan\n\n1. touch done.txt\n" + "Long plan line.\n" * 700
        plan_path.write_text(original_plan, encoding="utf-8")
        note = "  Owner's decision: café\nKeep this contract instead.  \n"
        result = invoke("approve", run_id, "--note", note, "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out == {
            "approved": "plan",
            "run_id": run_id,
            "task_id": out["task_id"],
            "status": "resuming",
            "stage": "implement",
            "project": out["project"],
            "worker": "none",
            "worker_serves_project": False,
        }
        # Nothing ran: the plan session is still the only pi call.
        assert [c["session_id"].rsplit("-", 1)[1] for c in fake_pi.calls()] == ["plan"]
        assert plan_path.read_text() == original_plan + f"\n## Owner's note on approval\n\n{note}\n"
        ledger = Ledger(data_dir / "helmraiser.db")
        row = ledger.get_run(run_id)
        assert (row["status"], row["stage"]) == ("resuming", "implement")
        assert ledger.get_task(out["task_id"])["status"] == "resuming"
        assert [(a["kind"], a["decision"], a["note"]) for a in ledger.approvals(run_id)] == [
            ("plan", "approved", note)
        ]
        assert ledger.events(run_id)[-1]["type"] == "handed_back"
        status = json.loads(
            (Path(fake_pi.bin).parents[1] / "runtime" / "helmraiser" / "status.json").read_text()
        )
        assert [(r["run_id"], r["stage"]) for r in status["resuming"]] == [(run_id, "implement")]
        assert status["running"] == [] and status["awaiting_count"] == 0
        # A resuming run is not awaiting approval any more.
        again = invoke("approve", run_id, "--json", data_dir=data_dir)
        assert again.exit_code == 2
        assert "is resuming, not awaiting approval" in json.loads(again.output)["error"]

    def test_plan_approval_without_note_preserves_plan(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi
    ):
        outcome = queue_and_run(tmp_project, data_dir, fake_pi, policy="approve-plan-first")
        run_id = outcome["run_id"]
        plan_path = data_dir / "runs" / run_id / "plan.md"
        original_plan = plan_path.read_bytes()

        result = invoke("approve", run_id, "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        assert plan_path.read_bytes() == original_plan
        ledger = Ledger(data_dir / "helmraiser.db")
        (approval,) = ledger.approvals(run_id)
        assert (approval["kind"], approval["decision"], approval["note"]) == (
            "plan",
            "approved",
            None,
        )
        row = ledger.get_run(run_id)
        assert (row["status"], row["stage"]) == ("resuming", "implement")
        assert [c["session_id"].rsplit("-", 1)[1] for c in fake_pi.calls()] == ["plan"]

    @pytest.mark.parametrize(
        "note",
        [None, "  Owner decision: café\n" + "Revised contract.\n" * 600 + "UNIQUE NOTE END  \n"],
        ids=["no-note", "long-note"],
    )
    def test_work_resumes_the_approved_plan_at_implement(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi, note: str | None
    ):
        outcome = queue_and_run(tmp_project, data_dir, fake_pi, policy="approve-plan-first")
        run_id = outcome["run_id"]
        run_dir = data_dir / "runs" / run_id
        original_plan = "# Plan\n\n1. touch done.txt\n" + "Detailed plan line.\n" * 700
        assert len(original_plan.encode()) > EVIDENCE_INLINE_LIMIT
        (run_dir / "plan.md").write_text(original_plan, encoding="utf-8")
        note_args = [] if note is None else ["--note", note]
        if note is not None:
            assert EVIDENCE_INLINE_LIMIT < len(note.encode()) < OWNER_NOTE_INLINE_LIMIT
        assert invoke("approve", run_id, *note_args, data_dir=data_dir).exit_code == 0

        result = invoke("work", "--once", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        out = json.loads(result.output.splitlines()[0])
        assert out["run_id"] == run_id
        assert out["status"] == "awaiting_approval" and out["pending_approval"] == "merge"
        assert out["iterations"] == 1 and out["verdict"]["verdict"] == "pass"
        assert round(out["cost_usd"], 2) == 0.08
        calls = [c for c in fake_pi.calls() if c["session_id"]]
        assert [c["session_id"].rsplit("-", 1)[1] for c in calls] == ["plan", "implement", "review"]
        assert all(c["cwd"] == outcome["worktree_path"] for c in calls)
        # The prompt travels by file: implement resumes after the plan, review follows.
        implement = Path(calls[1]["argv"][-1][1:]).read_text(encoding="utf-8")
        review = Path(calls[2]["argv"][-1][1:]).read_text(encoding="utf-8")
        directives = (run_dir / "directives.md").read_text(encoding="utf-8")
        sentence = (
            "- The owner's note on approval in the approved plan overrides the task brief "
            "and acceptance block.\n"
        )
        assert original_plan in implement
        assert "## Approved plan" in implement and "## Approved plan" in review
        assert "1. touch done.txt" in review and "[Plan cut after 8192 bytes.]" in review
        expected_plan = original_plan
        if note is not None:
            expected_plan += f"\n## Owner's note on approval\n\n{note}\n"
            assert "## Owner's note on approval" in implement and note.strip() in implement
            assert "## Owner's decisions on approval" in review
            assert note in review.split("## Task", 1)[0]
            assert sentence in directives
        else:
            assert "## Owner's note on approval" not in implement
            assert "## Owner's decisions on approval" not in implement + review
            assert sentence not in directives
        assert (run_dir / "plan.md").read_text(encoding="utf-8") == expected_plan
        ledger = Ledger(data_dir / "helmraiser.db")
        approval, merge = ledger.approvals(run_id)
        assert approval["note"] == note
        assert (merge["kind"], merge["decision"]) == ("merge", "pending")
        assert ledger.get_run(run_id)["status"] == "awaiting_approval"
        assert ledger.next_resumable() is None

        # Second approve merges as usual.
        merged = invoke("approve", run_id, "--json", data_dir=data_dir)
        assert merged.exit_code == 0, merged.output
        assert json.loads(merged.output)["approved"] == "merge"
        assert (tmp_project / "done.txt").exists()
        assert [(a["kind"], a["decision"]) for a in ledger.approvals(run_id)] == [
            ("plan", "approved"),
            ("merge", "approved"),
        ]

    @pytest.mark.parametrize(
        ("worker", "record", "line"),
        [
            ("running", {}, "  the running worker picks it up"),
            (
                "running",
                {"projects": ["/elsewhere", "SAME"], "exclude_projects": []},
                "  the running worker picks it up",
            ),
            (
                "running",
                {"projects": ["/elsewhere", "/other"], "exclude_projects": []},
                "  the running worker does not serve this project: helmraiser work --project PROJECT",
            ),
            (
                "running",
                {"projects": None, "exclude_projects": ["/elsewhere", "SAME"]},
                "  the running worker does not serve this project: helmraiser work --project PROJECT",
            ),
            # A record from before the scope fields: read from its argv.
            (
                "running",
                {"argv": ["work", "--project", "/elsewhere", "--project", "SAME"]},
                "  the running worker picks it up",
            ),
            (
                "running",
                {"argv": ["work", "--project", "/elsewhere"]},
                "  the running worker does not serve this project: helmraiser work --project PROJECT",
            ),
            ("none", {}, "  no worker is running: helmraiser worker start (or helmraiser work)"),
        ],
    )
    def test_plan_approval_names_the_worker(
        self,
        tmp_project: Path,
        data_dir: Path,
        fake_pi: FakePi,
        monkeypatch: pytest.MonkeyPatch,
        worker: str,
        record: dict,
        line: str,
    ):
        outcome = queue_and_run(tmp_project, data_dir, fake_pi, policy="approve-plan-first")
        monkeypatch.setattr(commands, "worker_state", lambda data_dir: worker)
        project = str(tmp_project.resolve())

        def same(value: object) -> object:
            if isinstance(value, list):
                return [same(item) for item in value]
            return project if value == "SAME" else value

        saved = {"pid": 1, "argv": [], **{key: same(value) for key, value in record.items()}}
        (data_dir / "worker.json").write_text(json.dumps(saved), encoding="utf-8")
        line = line.replace("PROJECT", project)

        result = invoke("approve", outcome["run_id"], data_dir=data_dir)

        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert f"  the worker resumes run {outcome['run_id']} at implement" in lines
        assert line in lines

    def test_work_resumes_a_handed_back_run_before_queued_tasks(
        self, tmp_project: Path, tmp_path: Path, data_dir: Path, fake_pi: FakePi, git_init
    ):
        outcome = queue_and_run(tmp_project, data_dir, fake_pi, policy="approve-plan-first")
        other = tmp_path / "other"
        other.mkdir()
        git_init(other)
        elsewhere = queue_only(other, data_dir, "elsewhere")
        assert invoke("approve", outcome["run_id"], data_dir=data_dir).exit_code == 0

        result = invoke("work", "--once", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        assert json.loads(result.output.splitlines()[0])["run_id"] == outcome["run_id"]
        ledger = Ledger(data_dir / "helmraiser.db")
        assert ledger.get_task(elsewhere)["status"] == "queued"

    def test_handed_back_run_keeps_one_run_per_project(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi
    ):
        outcome = queue_and_run(tmp_project, data_dir, fake_pi, policy="approve-plan-first")
        assert invoke("approve", outcome["run_id"], data_dir=data_dir).exit_code == 0
        later = queue_only(tmp_project, data_dir, "later-widget")
        ledger = Ledger(data_dir / "helmraiser.db")
        assert ledger.next_queued() is None
        assert ledger.held_tasks()[later]["status"] == "resuming"

        # Draining resumes the run, which parks for merge and still holds the project.
        result = invoke("work", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        final = json.loads(result.output.splitlines()[-1])
        assert [run["run_id"] for run in final["runs"]] == [outcome["run_id"]]
        assert final["summary"]["held"] == [
            {
                "task_id": later,
                "run_id": outcome["run_id"],
                "status": "awaiting_approval",
                "pending": "merge",
            }
        ]
        assert ledger.get_task(later)["status"] == "queued"


@pytest.mark.parametrize(("command", "status"), [("approve", "landed"), ("reject", "failed")])
def test_settle_ignores_summary_oserror(
    tmp_project, data_dir, fake_pi, monkeypatch, command, status
):
    outcome = queue_and_run(tmp_project, data_dir, fake_pi)
    summary = RunDir(outcome["run_id"], data_dir).summary_md
    before = summary.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("summary unavailable")

    monkeypatch.setattr(commands, "restamp_summary", fail)
    result = invoke(command, outcome["run_id"], "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    ledger = Ledger(data_dir / "helmraiser.db")
    row = ledger.get_run(outcome["run_id"])
    assert row["status"] == ledger.get_task(row["task_id"])["status"] == status
    assert summary.read_bytes() == before
    runtime = Path(fake_pi.bin).parents[1] / "runtime" / "helmraiser" / "status.json"
    assert json.loads(runtime.read_text())["awaiting_count"] == 0


class TestReject:
    @pytest.mark.parametrize("legacy", [False, True])
    def test_reject_removes_worktree_keeps_branch(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi, legacy: bool
    ):
        project_file = tmp_project / ".helmraiser" / "project.toml"
        if legacy:
            project_file.parent.mkdir(exist_ok=True)
            project_file.write_text(
                f'[defaults]\nworktree_dir = "{project_file.parent / "worktrees"}"\n'
            )
        outcome = queue_and_run(tmp_project, data_dir, fake_pi)
        run_id = outcome["run_id"]
        metadata_path = Path(outcome["worktree_path"]).parent / f"{run_id}.json"
        if legacy:
            project_file.unlink()
        before = _git(tmp_project, "rev-parse", "HEAD")

        summary = RunDir(run_id, data_dir).summary_md
        heading, body = summary.read_text().split("\n", 1)
        assert heading == "## Fix the widget — awaiting_approval"
        result = invoke("reject", run_id, "--note", "wrong approach", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        assert summary.read_text() == "## Fix the widget — failed\n" + body
        out = json.loads(result.output)
        assert out["rejected"] == "merge" and out["status"] == "failed"
        assert out["worktree_removed"] is True and out["worktree_error"] is None
        assert not Path(outcome["worktree_path"]).exists()
        manager = worktree_manager(tmp_project)
        assert not manager._metadata_path(run_id).exists()
        assert not metadata_path.exists()
        assert manager.orphaned_metadata() == []
        assert outcome["branch_name"] in _git(tmp_project, "branch", "--list")
        assert _git(tmp_project, "rev-parse", "HEAD") == before
        ledger = Ledger(data_dir / "helmraiser.db")
        row = ledger.get_run(run_id)
        assert row["status"] == "failed" and row["failure_reason"] == "rejected"
        assert ledger.get_task(out["task_id"])["status"] == "failed"
        (approval,) = ledger.approvals(run_id)
        assert (approval["kind"], approval["decision"], approval["note"]) == (
            "merge",
            "rejected",
            "wrong approach",
        )

    def test_reject_pending_plan(self, tmp_project, data_dir, fake_pi):
        outcome = queue_and_run(tmp_project, data_dir, fake_pi, policy="approve-plan-first")
        result = invoke("reject", outcome["run_id"], data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert "rejected" in result.output and "(plan)" in result.output
        ledger = Ledger(data_dir / "helmraiser.db")
        (approval,) = ledger.approvals(outcome["run_id"])
        assert (approval["kind"], approval["decision"]) == ("plan", "rejected")
        assert ledger.pending_approval(outcome["run_id"]) is None
        assert not Path(outcome["worktree_path"]).exists()
        # Approving afterwards is refused: the run is no longer awaiting.
        assert invoke("approve", outcome["run_id"], data_dir=data_dir).exit_code == 2

    def test_reject_not_awaiting(self, tmp_project, data_dir, fake_pi):
        outcome = queue_and_run(tmp_project, data_dir, fake_pi)
        assert invoke("reject", outcome["run_id"], data_dir=data_dir).exit_code == 0
        assert invoke("reject", outcome["run_id"], data_dir=data_dir).exit_code == 2

    def test_reject_a_handed_back_run_before_the_worker_resumes_it(
        self, tmp_project, data_dir, fake_pi
    ):
        outcome = queue_and_run(tmp_project, data_dir, fake_pi, policy="approve-plan-first")
        run_id = outcome["run_id"]
        assert invoke("approve", run_id, data_dir=data_dir).exit_code == 0

        result = invoke("reject", run_id, "--note", "changed my mind", "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "failed" and out["worktree_removed"] is True
        ledger = Ledger(data_dir / "helmraiser.db")
        row = ledger.get_run(run_id)
        assert row["status"] == "failed" and row["failure_reason"] == "rejected"
        assert ledger.next_resumable() is None
        assert not Path(outcome["worktree_path"]).exists()
        # The worker finds nothing to resume.
        assert invoke("work", "--once", "--json", data_dir=data_dir).exit_code == 0
        assert [c["session_id"].rsplit("-", 1)[1] for c in fake_pi.calls()] == ["plan"]


def queue_only(project: Path, data_dir: Path, task_id: str) -> str:
    path = project.parent / "tasks" / f"{task_id}.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        TASK.format(
            project=project, policy="review", delivery="merge", task_id=task_id, marker="x.txt"
        ),
        encoding="utf-8",
    )
    result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    return str(json.loads(result.output)["task_id"])


class TestEligibleHint:
    @pytest.mark.parametrize(
        ("action", "worker"), [("approve", "none"), ("reject", "running"), ("approve", "exited")]
    )
    def test_settling_a_run_names_the_projects_eligible_tasks(
        self,
        tmp_project: Path,
        data_dir: Path,
        fake_pi: FakePi,
        monkeypatch: pytest.MonkeyPatch,
        action: str,
        worker: str,
    ):
        outcome = queue_and_run(tmp_project, data_dir, fake_pi)
        later = queue_only(tmp_project, data_dir, "later-widget")
        ledger = Ledger(data_dir / "helmraiser.db")
        assert ledger.next_queued() is None
        monkeypatch.setattr(commands, "worker_state", lambda data_dir: worker)

        result = invoke(action, outcome["run_id"], "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["eligible"] == {
            "task_ids": [later],
            "worker": worker,
            "worker_serves_project": worker == "running",
            "project": str(tmp_project),
        }
        assert ledger.next_queued()["task_id"] == later

    @pytest.mark.parametrize(
        ("worker", "exclude", "line"),
        [
            ("running", False, "  the running worker picks them up"),
            (
                "running",
                True,
                "  the running worker does not serve this project: helmraiser work --project P",
            ),
            ("none", False, "  no worker is running: helmraiser worker start"),
        ],
    )
    def test_human_hint(
        self,
        tmp_project: Path,
        data_dir: Path,
        fake_pi: FakePi,
        monkeypatch: pytest.MonkeyPatch,
        worker: str,
        exclude: bool,
        line: str,
    ):
        outcome = queue_and_run(tmp_project, data_dir, fake_pi)
        later = queue_only(tmp_project, data_dir, "later-widget")
        monkeypatch.setattr(commands, "worker_state", lambda data_dir: worker)
        record = {"pid": 1, "argv": [], "projects": None, "exclude_projects": []}
        if exclude:
            record["exclude_projects"] = [str(tmp_project)]
        (data_dir / "worker.json").write_text(json.dumps(record), encoding="utf-8")
        line = line.replace("project P", f"project {tmp_project}")

        result = invoke("reject", outcome["run_id"], data_dir=data_dir)

        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert f"  now eligible: {later}" in lines
        assert line in lines

    def test_no_hint_without_queued_tasks_of_the_project(
        self, tmp_project: Path, tmp_path: Path, data_dir: Path, fake_pi: FakePi, git_init
    ):
        other = tmp_path / "other"
        other.mkdir()
        git_init(other)
        first = queue_and_run(tmp_project, data_dir, fake_pi)
        queue_only(other, data_dir, "elsewhere")

        as_json = invoke("approve", first["run_id"], "--json", data_dir=data_dir)

        assert as_json.exit_code == 0, as_json.output
        assert "eligible" not in json.loads(as_json.output)

    def test_no_hint_while_the_plan_run_is_handed_back(
        self, tmp_project: Path, data_dir: Path, fake_pi: FakePi
    ):
        outcome = queue_and_run(tmp_project, data_dir, fake_pi, policy="approve-plan-first")
        queue_only(tmp_project, data_dir, "later-widget")

        result = invoke("approve", outcome["run_id"], "--json", data_dir=data_dir)

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["approved"] == "plan" and payload["status"] == "resuming"
        assert "eligible" not in payload


@pytest.mark.parametrize("delivery", ["merge", "pr"])
def test_depends_on_approval_records_rebased_head(
    tmp_project, data_dir, fake_pi, tmp_path, monkeypatch, delivery
):
    from dataclasses import replace

    from helmraiser.task.loader import render_task, write_task
    from tests.ledger.test_db import make_spec

    if delivery == "pr":
        remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        _git(tmp_project, "remote", "add", "origin", str(remote))
        bin_dir = tmp_path / "gh-bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text("#!/bin/sh\necho https://example.test/pr/1\n")
        gh.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    outcome = queue_and_run(tmp_project, data_dir, fake_pi, delivery=delivery)
    ledger = Ledger(data_dir / "helmraiser.db")
    dependent = replace(
        make_spec(tmp_project, "dependent", depends_on=("fix-widget",)),
        source_path=tmp_path / "dependent.md",
    )
    write_task(dependent, dependent.source_path)
    ledger.upsert_task(dependent, render_task(dependent))
    assert ledger.next_queued() is None
    (tmp_project / "base-moved.txt").write_text("moved\n")
    _git(tmp_project, "add", "base-moved.txt")
    _git(tmp_project, "commit", "-m", "base moved")
    result = invoke("approve", outcome["run_id"], "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    row = ledger.get_run(outcome["run_id"])
    ref = outcome["branch_name"] if delivery == "pr" else row["base_branch"]
    delivered = _git(tmp_project, "rev-parse", f"refs/heads/{ref}")
    assert row["head_commit"] == delivered != outcome["head_commit"]
    if delivery == "merge":
        assert payload["head"] == delivered
        assert payload["eligible"]["task_ids"] == [dependent.task_id]
    else:
        assert "eligible" not in payload
        assert ledger.next_queued() is None
        assert _git(remote, "rev-parse", f"refs/heads/{ref}") == delivered
        _git(tmp_project, "merge", "--no-ff", ref, "-m", "merge PR")
    assert ledger.next_queued()["task_id"] == dependent.task_id


def test_depends_on_rejection_does_not_advertise_dependent(tmp_project, data_dir, fake_pi):
    from helmraiser.task.loader import render_task
    from tests.ledger.test_db import make_spec

    outcome = queue_and_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    dependent = make_spec(tmp_project, "dependent", depends_on=("fix-widget",))
    ledger.upsert_task(dependent, render_task(dependent))
    result = invoke("reject", outcome["run_id"], "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert "eligible" not in json.loads(result.output)
    assert ledger.next_queued() is None
    assert ledger.held_tasks()[dependent.task_id]["unmet"][0]["reason"] == "failed"
