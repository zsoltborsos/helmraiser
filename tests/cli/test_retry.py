"""``helmraiser retry``: a failed run whose work is complete, re-verified against its
corrected task file by the worker (verify → evidence → review, no implement), and
``retry --implement``: the same run handed back at implement with its last verify
failure as the first prompt."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, commands
from helmraiser.ledger.db import Ledger
from helmraiser.loop import services as loop_services
from tests.conftest import FakePi, configured_test_config

runner = CliRunner()

TASK = """---
id: fix-widget
title: Fix the widget
project: {project}
status: draft
policy: review
isolation: none
budget:
  max_cost_usd: 2.0
  max_minutes: 30
  max_iterations: 3
verify:
{verify}
---
## Goal

Make the widget work.
"""

WRONG_VERIFY = "  - name: marker\n    run: test -f wrong.txt\n"
FIXED_VERIFY = "  - name: marker\n    run: test -f done.txt\n"
NOT_RETRYABLE = "retry is only for a run that was blocked, failed verify, ran out of budget"
REVIEW_PASS = '```json\n{"verdict": "pass", "findings": [], "evidence_checks": []}\n```'


@pytest.fixture(autouse=True)
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = configured_test_config()
    monkeypatch.setattr(commands, "load_config", lambda: config)
    monkeypatch.setattr(commands, "get_intake_adapter", lambda config: None)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def invoke(*args: str, data_dir: Path):
    return runner.invoke(app, [*args, "--data-dir", str(data_dir)])


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def task_path(project: Path) -> Path:
    return project.parent / "tasks" / "fix-widget.md"


def write_task(project: Path, verify: str) -> Path:
    path = task_path(project)
    path.parent.mkdir(exist_ok=True)
    path.write_text(TASK.format(project=project, verify=verify), encoding="utf-8")
    return path


def sessions(fake_pi: FakePi) -> list[str]:
    return [c["session_id"].rsplit("-", 1)[1] for c in fake_pi.calls() if c["session_id"]]


def blocked_run(project: Path, data_dir: Path, fake_pi: FakePi, *, clean: bool = False) -> dict:
    """BLOCKED on the wrong task file, with completed/dirty work by default,
    or before writing anything when ``clean`` is set."""
    path = write_task(project, WRONG_VERIFY)
    assert invoke("intake", str(path), "--yes", "--json", data_dir=data_dir).exit_code == 0
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": (
                    [{"text": "BLOCKED: the verify step checks wrong.txt"}]
                    if clean
                    else [
                        {"text": "Done.", "cost": 0.05, "files": [["done.txt", "ok\n"]]},
                        {
                            "text": "BLOCKED: the verify step checks wrong.txt",
                            "cost": 0.01,
                            "files": [["notes.txt", "why\n"]],
                        },
                    ]
                ),
                "*-review": {"text": REVIEW_PASS, "cost": 0.02},
            }
        }
    )
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 1, result.output
    outcome = json.loads(result.output.splitlines()[0])
    assert outcome["status"] == "failed", outcome
    assert outcome["failure_reason"] == "blocked: the verify step checks wrong.txt"
    assert sessions(fake_pi) == (["implement"] if clean else ["implement", "implement"])
    return outcome


def _prepare_adopt_base_run(
    project: Path, data_dir: Path, fake_pi: FakePi, *, rebase: bool = True
) -> tuple[dict, str, str, str]:
    outcome = blocked_run(project, data_dir, fake_pi)
    row = Ledger(data_dir / "helmraiser.db").get_run(outcome["run_id"])
    branch, previous = row["base_branch"], row["base_commit"]
    worktree = Path(outcome["worktree_path"])
    _git(worktree, "add", "notes.txt")
    _git(worktree, "commit", "-m", "Keep run notes")
    path = write_task(project, FIXED_VERIFY)
    path.write_text(
        path.read_text().replace("status: draft", "status: draft\npaths: [done.txt, notes.txt]")
    )
    (project / "upstream.txt").write_text("outside the run's scope\n")
    _git(project, "add", "upstream.txt")
    _git(project, "commit", "-m", "Advance base")
    tip = _git(project, "rev-parse", f"refs/heads/{branch}")
    if rebase:
        _git(worktree, "rebase", branch)
    return outcome, branch, previous, tip


def test_retry_without_adopt_base_counts_upstream_files_as_task_changes(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome, _branch, previous, _tip = _prepare_adopt_base_run(tmp_project, data_dir, fake_pi)
    run_id = outcome["run_id"]
    result = invoke("retry", run_id, "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert "adopted_base" not in json.loads(result.output)

    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 1, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["failure_reason"] == "verify_failed: task-paths"
    ledger = Ledger(data_dir / "helmraiser.db")
    assert "upstream.txt" in ledger.iterations(run_id)[-1]["verify_results"][0]["tail"]
    assert ledger.get_run(run_id)["base_commit"] == previous
    metadata = Path(outcome["worktree_path"]).parent / f"{run_id}.json"
    assert json.loads(metadata.read_text())["base_commit"] == previous
    assert not ledger.events(run_id, types=("rebased",))


def _adopt_base_snapshot(outcome: dict, data_dir: Path) -> tuple:
    run_id = outcome["run_id"]
    worktree = Path(outcome["worktree_path"])
    ledger = Ledger(data_dir / "helmraiser.db")
    return (
        ledger.get_run(run_id),
        ledger.get_task(outcome["task_id"]),
        ledger.events(run_id),
        (worktree.parent / f"{run_id}.json").read_bytes(),
        (data_dir / "runs" / run_id / "task.md").read_bytes(),
        _git(worktree, "rev-parse", "HEAD") if worktree.is_dir() else None,
        _git(worktree, "status", "--porcelain") if worktree.is_dir() else None,
    )


def test_retry_adopt_base_records_host_rebase_and_verifies_only_run_files(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome, branch, previous, tip = _prepare_adopt_base_run(tmp_project, data_dir, fake_pi)
    run_id = outcome["run_id"]
    expected = {"base_branch": branch, "base_commit": tip, "previous": previous}
    result = invoke("retry", run_id, "--adopt-base", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["adopted_base"] == expected
    assert payload["stage"] == "verify"
    assert sessions(fake_pi) == ["implement", "implement"]
    ledger = Ledger(data_dir / "helmraiser.db")
    assert ledger.get_run(run_id)["base_commit"] == tip
    worktree = Path(outcome["worktree_path"])
    metadata = worktree.parent / f"{run_id}.json"
    assert json.loads(metadata.read_text())["base_commit"] == tip
    events = ledger.events(run_id)
    rebased = [e for e in events if e["type"] == "rebased"]
    assert len(rebased) == 1
    assert rebased[0]["source"] == "helmraiser"
    assert rebased[0]["stage"] == "implement"
    assert rebased[0]["payload"] == {**expected, "by": "retry --adopt-base"}
    types = [e["type"] for e in events]
    assert types.index("rebased") < types.index("handed_back")

    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["status"] == "awaiting_approval"
    assert resumed["pending_approval"] == "merge"
    assert resumed["verdict"]["verdict"] == "pass"
    assert [(r["name"], r["passed"]) for r in resumed["verify_results"]] == [("marker", True)]
    assert not ledger.events(run_id, types=("path_scope_failed",))
    assert _git(worktree, "diff", "--name-only", tip, "HEAD").splitlines() == [
        "done.txt",
        "notes.txt",
    ]
    assert (worktree / "upstream.txt").is_file()
    assert sessions(fake_pi) == ["implement", "implement", "review"]


def test_retry_adopt_base_refuses_unrebased_worktree(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome, branch, _previous, tip = _prepare_adopt_base_run(
        tmp_project, data_dir, fake_pi, rebase=False
    )
    before = _adopt_base_snapshot(outcome, data_dir)
    result = invoke("retry", outcome["run_id"], "--adopt-base", "--json", data_dir=data_dir)
    assert result.exit_code == 2, result.output
    assert branch in result.output and tip in result.output
    assert "rebase the worktree onto it first" in result.output
    assert _adopt_base_snapshot(outcome, data_dir) == before


def test_retry_adopt_base_refuses_unchanged_tip(tmp_project: Path, data_dir: Path, fake_pi: FakePi):
    outcome = blocked_run(tmp_project, data_dir, fake_pi)
    before = _adopt_base_snapshot(outcome, data_dir)
    result = invoke("retry", outcome["run_id"], "--adopt-base", "--json", data_dir=data_dir)
    assert result.exit_code == 2, result.output
    assert "nothing to adopt" in result.output
    assert _adopt_base_snapshot(outcome, data_dir) == before


@pytest.mark.parametrize("backend", ["merge", "apply"])
def test_retry_adopt_base_refuses_rebase_in_progress(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, backend: str
):
    outcome, branch, _previous, _tip = _prepare_adopt_base_run(
        tmp_project, data_dir, fake_pi, rebase=False
    )
    worktree = Path(outcome["worktree_path"])
    (worktree / "README.md").write_text("run's conflicting edit\n")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "Run edit")
    (tmp_project / "README.md").write_text("base's conflicting edit\n")
    _git(tmp_project, "add", "README.md")
    _git(tmp_project, "commit", "-m", "Base edit")
    rebase = subprocess.run(
        ["git", "rebase", f"--{backend}", branch],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rebase.returncode != 0
    path = Path(_git(worktree, "rev-parse", "--git-path", f"rebase-{backend}"))
    if not path.is_absolute():
        path = worktree / path
    assert path.is_dir()
    before = _adopt_base_snapshot(outcome, data_dir)
    result = invoke("retry", outcome["run_id"], "--adopt-base", "--json", data_dir=data_dir)
    assert result.exit_code == 2, result.output
    assert "rebase is in progress and unfinished" in result.output
    assert path.is_dir()
    assert _adopt_base_snapshot(outcome, data_dir) == before


@pytest.mark.parametrize("flags", [(), ("--implement",)])
def test_retry_adopt_base_without_remaining_commits_restarts(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, flags: tuple[str, ...]
):
    outcome = blocked_run(tmp_project, data_dir, fake_pi, clean=True)
    run_id = outcome["run_id"]
    ledger = Ledger(data_dir / "helmraiser.db")
    row = ledger.get_run(run_id)
    (tmp_project / "upstream.txt").write_text("upstream\n")
    _git(tmp_project, "add", "upstream.txt")
    _git(tmp_project, "commit", "-m", "Advance base")
    tip = _git(tmp_project, "rev-parse", f"refs/heads/{row['base_branch']}")
    worktree = Path(outcome["worktree_path"])
    _git(worktree, "rebase", row["base_branch"])
    assert _git(worktree, "rev-parse", "HEAD") == tip
    assert _git(worktree, "status", "--porcelain") == ""
    write_task(tmp_project, FIXED_VERIFY)

    result = invoke("retry", run_id, "--adopt-base", *flags, "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["stage"] == "start"
    assert payload["restarted_without_commits"] is True
    assert payload["adopted_base"] == {
        "base_branch": row["base_branch"],
        "base_commit": tip,
        "previous": row["base_commit"],
    }
    assert ledger.get_run(run_id)["base_commit"] == tip
    metadata = worktree.parent / f"{run_id}.json"
    assert json.loads(metadata.read_text())["base_commit"] == tip
    assert sessions(fake_pi) == ["implement"]


def test_retry_adopt_base_with_implement_hands_back_at_implement(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome, branch, previous, tip = _prepare_adopt_base_run(tmp_project, data_dir, fake_pi)
    result = invoke(
        "retry", outcome["run_id"], "--adopt-base", "--implement", "--json", data_dir=data_dir
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["stage"] == "implement"
    assert payload["adopted_base"] == {
        "base_branch": branch,
        "base_commit": tip,
        "previous": previous,
    }
    assert "restarted_without_commits" not in payload
    assert sessions(fake_pi) == ["implement", "implement"]


def test_retry_adopt_base_human_output(tmp_project: Path, data_dir: Path, fake_pi: FakePi):
    outcome, branch, previous, tip = _prepare_adopt_base_run(tmp_project, data_dir, fake_pi)
    result = invoke("retry", outcome["run_id"], "--adopt-base", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert (
        f"adopted base {branch} at {tip[:12]} (was {previous[:12]})" in result.output.splitlines()
    )
    assert f"  the worker resumes run {outcome['run_id']} at verify" in result.output.splitlines()


@pytest.mark.parametrize(
    ("guard", "code", "message"),
    [
        ("reason", 2, NOT_RETRYABLE),
        ("status", 2, "is awaiting_approval, not failed"),
        ("queued", 2, "has moved on"),
        ("missing", 1, "cannot be retried"),
        ("implement", 2, "no failed verify result"),
        ("setup", 2, NOT_RETRYABLE),
        ("invalid_task", 2, "invalid task"),
    ],
)
def test_retry_adopt_base_preserves_existing_eligibility_checks(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, guard: str, code: int, message: str
):
    outcome, _branch, _previous, _tip = _prepare_adopt_base_run(tmp_project, data_dir, fake_pi)
    run_id = outcome["run_id"]
    ledger = Ledger(data_dir / "helmraiser.db")
    flags = ()
    if guard == "reason":
        ledger.update_run(run_id, failure_reason="error: broken")
    elif guard == "status":
        ledger.update_run(run_id, status="awaiting_approval", failure_reason=None)
    elif guard == "queued":
        assert (
            invoke("intake", str(task_path(tmp_project)), "--yes", data_dir=data_dir).exit_code == 0
        )
    elif guard == "missing":
        _git(tmp_project, "worktree", "remove", "--force", outcome["worktree_path"])
    elif guard == "implement":
        for iteration in ledger.iterations(run_id):
            ledger.record_verify(
                run_id,
                iteration["n"],
                commit_sha=iteration["commit_sha"],
                verify_passed=True,
                verify_results=[],
            )
        flags = ("--implement",)
    elif guard == "setup":
        ledger.update_run(run_id, failure_reason="setup: broken")
    elif guard == "invalid_task":
        write_task(tmp_project, "  []\n")
    before = _adopt_base_snapshot(outcome, data_dir)

    result = invoke("retry", run_id, "--adopt-base", *flags, "--json", data_dir=data_dir)
    assert result.exit_code == code, result.output
    assert message in json.loads(result.output)["error"]
    assert _adopt_base_snapshot(outcome, data_dir) == before
    assert not ledger.events(run_id, types=("rebased",))


@pytest.mark.parametrize("legacy", [False, True])
def test_retry_resumes_at_verify_with_the_corrected_task_file(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, tmp_path: Path, legacy: bool
):
    project_file = tmp_project / ".helmraiser" / "project.toml"
    if legacy:
        project_file.parent.mkdir(exist_ok=True)
        project_file.write_text(
            f'[defaults]\nworktree_dir = "{project_file.parent / "worktrees"}"\n'
        )
    outcome = blocked_run(tmp_project, data_dir, fake_pi)
    run_id = outcome["run_id"]
    metadata_path = Path(outcome["worktree_path"]).parent / f"{run_id}.json"
    if legacy:
        project_file.unlink()
    write_task(tmp_project, FIXED_VERIFY)

    result = invoke("retry", run_id[:8], "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out == {
        "retried": "blocked: the verify step checks wrong.txt",
        "run_id": run_id,
        "task_id": outcome["task_id"],
        "status": "resuming",
        "stage": "verify",
        "project": out["project"],
        "worker": "none",
        "worker_serves_project": False,
    }
    ledger = Ledger(data_dir / "helmraiser.db")
    assert "run: test -f done.txt" in ledger.get_task(outcome["task_id"])["snapshot"]
    assert "test -f done.txt" in (data_dir / "runs" / run_id / "task.md").read_text()
    row = ledger.get_run(run_id)
    assert (row["status"], row["stage"]) == ("resuming", "verify")
    status = json.loads((data_dir.parent / "runtime" / "helmraiser" / "status.json").read_text())
    assert [(r["run_id"], r["stage"]) for r in status["resuming"]] == [(run_id, "verify")]
    assert len(fake_pi.calls()) == 2  # retry itself runs nothing

    result = invoke("work", "--once", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["run_id"] == run_id
    assert resumed["status"] == "awaiting_approval" and resumed["pending_approval"] == "merge"
    assert resumed["failure_reason"] is None and resumed["verdict"]["verdict"] == "pass"
    assert [(r["name"], r["passed"]) for r in resumed["verify_results"]] == [("marker", True)]
    # Verify, then review: no new implement session or iteration.
    assert sessions(fake_pi) == ["implement", "implement", "review"]
    assert resumed["iterations"] == 2
    _first, last = ledger.iterations(run_id)
    assert last["verify_passed"] == 1 and last["commit_sha"] == resumed["head_commit"]
    worktree = Path(outcome["worktree_path"])
    assert _git(worktree, "log", "-1", "--format=%s") == (
        "helmraiser: uncommitted work of fix-widget (retry)"
    )
    assert _git(worktree, "status", "--porcelain") == ""
    types = [e["type"] for e in ledger.events(run_id)]
    assert types.index("handed_back") < types.index("run_resume")

    assert resumed["worktree_path"] == outcome["worktree_path"]
    assert list(tmp_path.rglob(f"{run_id}.json")) == [metadata_path]
    # From here it is an ordinary run awaiting approval.
    merged = invoke("approve", run_id, "--json", data_dir=data_dir)
    assert merged.exit_code == 0, merged.output
    assert (tmp_project / "done.txt").read_text() == "ok\n"
    assert ledger.get_run(run_id)["status"] == "landed"
    assert not metadata_path.exists()


def test_a_retry_that_fails_verify_again_ends_failed_with_the_new_reason(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome = blocked_run(tmp_project, data_dir, fake_pi)
    run_id = outcome["run_id"]
    write_task(tmp_project, "  - name: other\n    run: test -f other.txt\n")
    assert invoke("retry", run_id, data_dir=data_dir).exit_code == 0

    result = invoke("work", "--once", "--json", data_dir=data_dir)

    assert result.exit_code == 1, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["status"] == "failed" and resumed["failure_reason"] == "verify_failed: other"
    assert sessions(fake_pi) == ["implement", "implement"]  # no review either
    ledger = Ledger(data_dir / "helmraiser.db")
    assert ledger.get_run(run_id)["failure_reason"] == "verify_failed: other"
    assert ledger.get_task(outcome["task_id"])["status"] == "failed"
    # A verify failure is itself retryable once the task file is right.
    write_task(tmp_project, FIXED_VERIFY)
    again = invoke("retry", run_id, "--json", data_dir=data_dir)
    assert again.exit_code == 0, again.output


def test_retry_cannot_bypass_task_paths(tmp_project, data_dir, fake_pi, monkeypatch):
    from helmraiser.loop import runner as loop_runner

    outcome = blocked_run(tmp_project, data_dir, fake_pi)
    run_id = outcome["run_id"]
    path = write_task(tmp_project, FIXED_VERIFY)
    path.write_text(path.read_text().replace("status: draft", "status: draft\npaths: [done.txt]"))
    assert invoke("retry", run_id, data_dir=data_dir).exit_code == 0

    def forbidden_verify(*args, **kwargs):
        raise AssertionError("Acceptance must be skipped for scope failure")

    monkeypatch.setattr(loop_runner, "run_verify", forbidden_verify)
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 1, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["failure_reason"] == "verify_failed: task-paths"
    assert sessions(fake_pi) == ["implement", "implement"]
    ledger = Ledger(data_dir / "helmraiser.db")
    last = ledger.iterations(run_id)[-1]
    assert last["verify_passed"] == 0
    assert "notes.txt" in last["verify_results"][0]["tail"]
    event = next(e for e in ledger.events(run_id) if e["type"] == "path_scope_failed")
    assert event["stage"] == "verify"
    assert event["payload"]["paths"] == ["done.txt"]
    assert "notes.txt" in event["payload"]["diagnostic"]


def test_retry_hint_names_the_worker(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, monkeypatch: pytest.MonkeyPatch
):
    outcome = blocked_run(tmp_project, data_dir, fake_pi)
    write_task(tmp_project, FIXED_VERIFY)
    monkeypatch.setattr(commands, "worker_state", lambda data_dir: "running")

    result = invoke("retry", outcome["run_id"], data_dir=data_dir)

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith(f"retrying {outcome['task_id']} run {outcome['run_id']}")
    assert f"  task re-read from {task_path(tmp_project)}" in lines
    assert f"  the worker resumes run {outcome['run_id']} at verify" in lines
    assert "  the running worker picks it up" in lines


@pytest.mark.parametrize("flags", [(), ("--implement",)])
def test_retry_refuses_when_the_worktree_is_gone(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, flags: tuple[str, ...]
):
    outcome = blocked_run(tmp_project, data_dir, fake_pi)
    write_task(tmp_project, FIXED_VERIFY)
    _git(tmp_project, "worktree", "remove", "--force", outcome["worktree_path"])

    result = invoke("retry", outcome["run_id"], *flags, "--json", data_dir=data_dir)

    assert result.exit_code == 1, result.output
    assert "cannot be retried" in json.loads(result.output)["error"]
    ledger = Ledger(data_dir / "helmraiser.db")
    assert ledger.get_run(outcome["run_id"])["status"] == "failed"
    assert "wrong.txt" in ledger.get_task(outcome["task_id"])["snapshot"]


@pytest.mark.parametrize("clean", [False, True])
def test_retry_refuses_an_invalid_task_file(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, clean
):
    outcome = blocked_run(tmp_project, data_dir, fake_pi, clean=clean)
    write_task(tmp_project, "  []\n")

    result = invoke("retry", outcome["run_id"], "--json", data_dir=data_dir)

    assert result.exit_code == 2, result.output
    ledger = Ledger(data_dir / "helmraiser.db")
    assert ledger.get_run(outcome["run_id"])["status"] == "failed"
    assert "wrong.txt" in ledger.get_task(outcome["task_id"])["snapshot"]


@pytest.mark.parametrize("flags", [(), ("--implement",)])
@pytest.mark.parametrize("clean", [False, True])
def test_retry_refuses_a_task_that_was_queued_again(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, flags: tuple[str, ...], clean
):
    outcome = blocked_run(tmp_project, data_dir, fake_pi, clean=clean)
    path = write_task(tmp_project, FIXED_VERIFY)
    assert invoke("intake", str(path), "--yes", data_dir=data_dir).exit_code == 0

    result = invoke("retry", outcome["run_id"], *flags, "--json", data_dir=data_dir)

    assert result.exit_code == 2, result.output
    assert "has moved on" in json.loads(result.output)["error"]
    ledger = Ledger(data_dir / "helmraiser.db")
    assert ledger.get_task(outcome["task_id"])["status"] == "queued"


@pytest.mark.parametrize(
    ("reason", "error"),
    [
        (None, "is awaiting_approval, not failed"),
        *[
            (reason, NOT_RETRYABLE)
            for reason in (
                "error: broken",
                "isolation: broken",
                "auth: missing",
                "worktree: broken",
            )
        ],
    ],
)
@pytest.mark.parametrize("flags", [(), ("--implement",)])
def test_retry_refuses_runs_that_are_not_blocked_or_verify_failures(
    tmp_project: Path,
    data_dir: Path,
    fake_pi: FakePi,
    reason: str | None,
    error: str,
    flags: tuple[str, ...],
):
    outcome = blocked_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    if reason is None:
        ledger.update_run(outcome["run_id"], status="awaiting_approval", failure_reason=None)
    else:
        ledger.update_run(outcome["run_id"], failure_reason=reason)

    result = invoke("retry", outcome["run_id"], *flags, "--json", data_dir=data_dir)

    assert result.exit_code == 2, result.output
    assert error in json.loads(result.output)["error"]


# ── retry --implement ─────────────────────────────────────────────────


def prompt_of(call: dict) -> str:
    """The prompt a fake pi invocation got (inline, or ``@file`` when spilled)."""
    argv = call["argv"]
    prompt = argv[argv.index("--") + 1]
    return Path(prompt[1:]).read_text() if prompt.startswith("@") else prompt


def implement_calls(fake_pi: FakePi) -> list[dict]:
    return [c for c in fake_pi.calls() if c["session_id"] and "-implement" in c["session_id"]]


FIRST_TWO = [
    {"text": "Done.", "cost": 0.05, "files": [["done.txt", "ok\n"]]},
    {
        "text": "BLOCKED: the verify step checks wrong.txt",
        "cost": 0.01,
        "files": [["notes.txt", "why\n"]],
    },
]


@pytest.mark.parametrize("legacy", [False, True])
def test_retry_implement_resumes_at_implement_with_the_verify_failure_as_the_prompt(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, tmp_path: Path, legacy: bool
):
    project_file = tmp_project / ".helmraiser" / "project.toml"
    if legacy:
        project_file.parent.mkdir(exist_ok=True)
        project_file.write_text(
            f'[defaults]\nworktree_dir = "{project_file.parent / "worktrees"}"\n'
        )
    outcome = blocked_run(tmp_project, data_dir, fake_pi)
    run_id = outcome["run_id"]
    metadata_path = Path(outcome["worktree_path"]).parent / f"{run_id}.json"
    if legacy:
        project_file.unlink()
    ledger = Ledger(data_dir / "helmraiser.db")
    first_commit = ledger.iterations(run_id)[0]["commit_sha"]

    result = invoke("retry", run_id[:8], "--implement", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["retried"] == "blocked: the verify step checks wrong.txt"
    assert (out["status"], out["stage"]) == ("resuming", "implement")
    assert "restarted_without_commits" not in out
    row = ledger.get_run(run_id)
    assert (row["status"], row["stage"]) == ("resuming", "implement")
    assert len(fake_pi.calls()) == 2  # retry itself runs nothing

    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": [
                    *FIRST_TWO,
                    {"text": "Fixed.", "cost": 0.03, "files": [["wrong.txt", "now\n"]]},
                ],
                "*-review": {"text": REVIEW_PASS, "cost": 0.02},
            }
        }
    )
    result = invoke("work", "--once", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["run_id"] == run_id
    assert resumed["status"] == "awaiting_approval" and resumed["pending_approval"] == "merge"
    assert resumed["verdict"]["verdict"] == "pass"
    assert [(r["name"], r["passed"]) for r in resumed["verify_results"]] == [("marker", True)]
    # One new implement turn in the same pi session, then verify and review.
    assert sessions(fake_pi) == ["implement", "implement", "implement", "review"]
    retry_call = implement_calls(fake_pi)[-1]
    assert retry_call["session_id"] == f"{run_id}-implement"
    prompt = prompt_of(retry_call)
    assert prompt.startswith(
        "The owner handed this run back to you after it ended failed "
        "(blocked: the verify step checks wrong.txt)."
    )
    assert "Verify failed. Helmraiser ran the acceptance commands" in prompt
    assert "### marker (exit 1)" in prompt
    log = data_dir / "runs" / run_id / "logs" / "verify-1-1-marker.log"
    assert f"The full output of this step is in `{log}`" in prompt
    # The iteration count and the branch carry on: earlier commits stay.
    assert resumed["iterations"] == 3
    third = ledger.iterations(run_id)[-1]
    assert third["n"] == 3 and third["verify_passed"] == 1
    worktree = Path(outcome["worktree_path"])
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", first_commit, "HEAD"], cwd=worktree, check=False
    )
    assert ancestor.returncode == 0
    assert (worktree / "done.txt").read_text() == "ok\n"
    assert _git(worktree, "status", "--porcelain") == ""
    resume = next(e for e in ledger.events(run_id) if e["type"] == "run_resume")
    assert resume["payload"]["from"] == "retry" and resume["payload"]["stage"] == "implement"
    assert ledger.get_run(run_id)["failure_reason"] is None
    assert resumed["worktree_path"] == outcome["worktree_path"]
    assert list(tmp_path.rglob(f"{run_id}.json")) == [metadata_path]


def test_retry_implement_gets_a_fresh_escalation_ladder(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome = blocked_run(tmp_project, data_dir, fake_pi)
    run_id = outcome["run_id"]
    ledger = Ledger(data_dir / "helmraiser.db")
    ledger.update_run(run_id, escalated=1)  # the failed run had used its fallback
    assert invoke("retry", run_id, "--implement", data_dir=data_dir).exit_code == 0

    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": [
                    *FIRST_TWO,
                    {"text": "Tried.", "cost": 0.01, "files": [["try.txt", "1\n"]]},
                ],
                "*-implement-2": {
                    "text": "Fixed.",
                    "cost": 0.03,
                    "files": [["wrong.txt", "now\n"]],
                },
                "*-review": {"text": REVIEW_PASS, "cost": 0.02},
            }
        }
    )
    result = invoke("work", "--once", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["status"] == "awaiting_approval" and resumed["escalated"] is True
    # The task's 3 iterations on the implement model, then the fallback.
    assert sessions(fake_pi) == ["implement"] * 5 + ["2", "review"]
    escalated = [e for e in ledger.events(run_id) if e["type"] == "escalated"]
    assert len(escalated) == 1 and escalated[0]["payload"]["max_iterations"] == 2


def test_retry_implement_refuses_a_run_without_a_failed_verify(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    path = write_task(tmp_project, WRONG_VERIFY)
    assert invoke("intake", str(path), "--yes", "--json", data_dir=data_dir).exit_code == 0
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {
                    "text": "BLOCKED: no database",
                    "files": [["draft.txt", "wip\n"]],
                }
            }
        }
    )
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    outcome = json.loads(result.output.splitlines()[0])
    assert outcome["failure_reason"] == "blocked: no database"

    result = invoke("retry", outcome["run_id"], "--implement", "--json", data_dir=data_dir)

    assert result.exit_code == 2, result.output
    assert "no failed verify result" in json.loads(result.output)["error"]
    ledger = Ledger(data_dir / "helmraiser.db")
    assert ledger.get_run(outcome["run_id"])["status"] == "failed"
    # Without --implement it is still an ordinary retry.
    result = invoke("retry", outcome["run_id"], "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["stage"] == "verify" and "restarted_without_commits" not in out


def check_commitless_restart(tmp_project, data_dir, fake_pi, flags):
    outcome = blocked_run(tmp_project, data_dir, fake_pi, clean=True)
    run_id = outcome["run_id"]
    worktree = Path(outcome["worktree_path"])
    assert _git(worktree, "status", "--porcelain") == ""
    assert _git(worktree, "rev-parse", "HEAD") == outcome["base_commit"]
    write_task(tmp_project, FIXED_VERIFY)
    result = invoke("retry", run_id, *flags, "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "retried": outcome["failure_reason"],
        "run_id": run_id,
        "task_id": outcome["task_id"],
        "project": str(tmp_project),
        "status": "resuming",
        "stage": "start",
        "worker": "none",
        "worker_serves_project": False,
        "restarted_without_commits": True,
    }
    ledger = Ledger(data_dir / "helmraiser.db")
    assert "test -f done.txt" in ledger.get_task(outcome["task_id"])["snapshot"]
    assert "test -f done.txt" in (data_dir / "runs" / run_id / "task.md").read_text()
    assert [e["payload"] for e in ledger.events(run_id, types=("handed_back",))] == [
        {"stage": "start", "restarted_without_commits": True}
    ]
    row = ledger.get_run(run_id)
    assert (row["status"], row["stage"]) == ("resuming", "start")
    status = json.loads((data_dir.parent / "runtime" / "helmraiser" / "status.json").read_text())
    assert [(r["run_id"], r["stage"]) for r in status["resuming"]] == [(run_id, "start")]
    assert sessions(fake_pi) == ["implement"]
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": [
                    {"text": "BLOCKED: the verify step checks wrong.txt"},
                    {"text": "Done.", "cost": 0.03, "files": [["done.txt", "ok\n"]]},
                ],
                "*-review": {"text": REVIEW_PASS, "cost": 0.02},
            }
        }
    )
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["status"] == "awaiting_approval" and resumed["pending_approval"] == "merge"
    assert resumed["verdict"]["verdict"] == "pass" and resumed["verify_results"][0]["passed"]
    for key in ("run_id", "worktree_path", "branch_name", "base_commit"):
        assert resumed[key] == outcome[key]
    assert sessions(fake_pi) == ["implement", "implement", "review"]
    assert [i["n"] for i in ledger.iterations(run_id)] == [1, 2]
    call = implement_calls(fake_pi)[-1]
    assert call["session_id"] == f"{run_id}-implement"
    prompt = prompt_of(call)
    assert "Nothing is committed on the branch." in prompt and "Make the widget work." in prompt
    assert "Verify failed" not in prompt
    resume = ledger.events(run_id, types=("run_resume",))[0]["payload"]
    assert resume == {
        "task_id": outcome["task_id"],
        "from": "retry",
        "stage": "start",
        "restarted_without_commits": True,
    }


def test_retry_without_commits_restarts_the_run_at_start(tmp_project, data_dir, fake_pi):
    check_commitless_restart(tmp_project, data_dir, fake_pi, ())


def test_retry_implement_without_commits_restarts_the_run_at_start(tmp_project, data_dir, fake_pi):
    check_commitless_restart(tmp_project, data_dir, fake_pi, ("--implement",))


def setup_failed_run(project, data_dir, fake_pi):
    project_file = project / ".helmraiser" / "project.toml"
    project_file.parent.mkdir(exist_ok=True)
    project_file.write_text(
        '[defaults]\nsetup = "if test -f setup-marker; then true; else touch setup-marker; exit 1; fi"\n'
    )
    path = write_task(project, FIXED_VERIFY)
    assert invoke("intake", str(path), "--yes", data_dir=data_dir).exit_code == 0
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 1, result.output
    outcome = json.loads(result.output.splitlines()[0])
    assert outcome["failure_reason"].startswith("setup:") and fake_pi.calls() == []
    worktree = Path(outcome["worktree_path"])
    assert _git(worktree, "rev-parse", "HEAD") == outcome["base_commit"]
    assert "setup-marker" in _git(worktree, "status", "--porcelain")
    return outcome


def test_retry_restarts_a_run_that_failed_at_setup(tmp_project, data_dir, fake_pi):
    outcome = setup_failed_run(tmp_project, data_dir, fake_pi)
    result = invoke("retry", outcome["run_id"], "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["stage"] == "start" and out["restarted_without_commits"] is True
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {"text": "Done", "files": [["done.txt", "done"]]},
                "*-review": {"text": REVIEW_PASS},
            }
        }
    )
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["status"] == "awaiting_approval" and resumed["pending_approval"] == "merge"
    assert (
        resumed["run_id"] == outcome["run_id"]
        and resumed["worktree_path"] == outcome["worktree_path"]
    )
    ledger = Ledger(data_dir / "helmraiser.db")
    assert [
        e["payload"]["ok"] for e in ledger.events(outcome["run_id"], types=("setup_result",))
    ] == [False, True]


@pytest.mark.parametrize("flags", [(), ("--implement",)])
def test_retry_refuses_a_setup_failure_that_has_commits(tmp_project, data_dir, fake_pi, flags):
    outcome = setup_failed_run(tmp_project, data_dir, fake_pi)
    worktree = Path(outcome["worktree_path"])
    _git(worktree, "add", "setup-marker")
    _git(worktree, "commit", "-m", "Partial setup")
    result = invoke("retry", outcome["run_id"], *flags, "--json", data_dir=data_dir)
    assert result.exit_code == 2, result.output
    assert NOT_RETRYABLE in json.loads(result.output)["error"]
    ledger = Ledger(data_dir / "helmraiser.db")
    assert ledger.get_run(outcome["run_id"])["status"] == "failed"
    assert ledger.events(outcome["run_id"], types=("handed_back",)) == []


# ── budget failures ───────────────────────────────────────────────────


def iterations_used_up(project: Path, data_dir: Path, fake_pi: FakePi) -> tuple[dict, Path]:
    """All 3 of 3 iterations used with verify failing, recorded as a budget stop."""
    path = write_task(project, FIXED_VERIFY)
    assert invoke("intake", str(path), "--yes", "--json", data_dir=data_dir).exit_code == 0
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": [
                    {"text": "Try", "cost": 0.01, "files": [[f"try{n}.txt", "x\n"]]}
                    for n in range(1, 4)
                ]
            }
        }
    )
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    outcome = json.loads(result.output.splitlines()[0])
    assert outcome["status"] == "failed"
    ledger = Ledger(data_dir / "helmraiser.db")
    assert len(ledger.iterations(outcome["run_id"])) >= 3
    ledger.update_run(outcome["run_id"], failure_reason="budget:iterations")
    return outcome, path


@pytest.mark.parametrize("flags", [(), ("--implement",)])
def test_retry_refuses_a_budget_failure_when_the_task_file_allows_no_more(
    tmp_project, data_dir, fake_pi, flags
):
    outcome, path = iterations_used_up(tmp_project, data_dir, fake_pi)
    run_id = outcome["run_id"]
    ledger = Ledger(data_dir / "helmraiser.db")
    used = len(ledger.iterations(run_id))
    snapshot = ledger.get_task(outcome["task_id"])["snapshot"]
    task_md = (data_dir / "runs" / run_id / "task.md").read_text()
    path.write_text(path.read_text().replace("max_iterations: 3", f"max_iterations: {used}"))

    result = invoke("retry", run_id, *flags, "--json", data_dir=data_dir)

    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert list(payload) == ["error"]
    assert f"iterations: {used} used, limit {used} (max_iterations)" in payload["error"]
    assert str(path) in payload["error"]
    assert ledger.get_task(outcome["task_id"])["snapshot"] == snapshot
    assert (data_dir / "runs" / run_id / "task.md").read_text() == task_md
    row = ledger.get_run(run_id)
    assert (row["status"], row["failure_reason"]) == ("failed", "budget:iterations")
    assert ledger.events(run_id, types=("handed_back",)) == []

    result = invoke("retry", run_id, *flags, data_dir=data_dir)
    assert result.exit_code == 2, result.output
    assert "no budget left to retry" in " ".join(result.output.split())


@pytest.mark.parametrize(("flags", "stage"), [((), "verify"), (("--implement",), "implement")])
def test_retry_hands_back_a_budget_failure_once_the_task_file_allows_more(
    tmp_project, data_dir, fake_pi, flags, stage
):
    outcome, path = iterations_used_up(tmp_project, data_dir, fake_pi)
    run_id = outcome["run_id"]
    ledger = Ledger(data_dir / "helmraiser.db")
    used = len(ledger.iterations(run_id))
    raised = path.read_text().replace("max_iterations: 3", f"max_iterations: {used + 1}")
    path.write_text(raised)

    result = invoke("retry", run_id, *flags, "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "retried": "budget:iterations",
        "run_id": run_id,
        "task_id": outcome["task_id"],
        "project": str(tmp_project),
        "status": "resuming",
        "stage": stage,
        "worker": "none",
        "worker_serves_project": False,
    }
    assert ledger.get_task(outcome["task_id"])["snapshot"] == raised
    assert ledger.get_run(run_id)["failure_reason"] == "budget:iterations"


def test_retry_implement_of_a_budget_failure_needs_no_failed_verify_result(
    tmp_project, data_dir, fake_pi
):
    """Cut off in its first session, before any verify ran."""
    path = write_task(tmp_project, FIXED_VERIFY)
    path.write_text(path.read_text().replace("max_cost_usd: 2.0", "max_cost_usd: 0.1"))
    assert invoke("intake", str(path), "--yes", "--json", data_dir=data_dir).exit_code == 0
    fake_pi.write_script(
        {"sessions": {"*-implement": {"text": "Partly", "cost": 0.5, "files": [["done.txt", "x"]]}}}
    )
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    outcome = json.loads(result.output.splitlines()[0])
    assert outcome["failure_reason"] == "budget:cost"
    run_id = outcome["run_id"]
    assert not any(
        i["verify_results"] for i in Ledger(data_dir / "helmraiser.db").iterations(run_id)
    )
    path.write_text(path.read_text().replace("max_cost_usd: 0.1", "max_cost_usd: 2.0"))

    result = invoke("retry", run_id, "--implement", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["stage"] == "implement"
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {"text": "Done", "cost": 0.1, "files": [["more.txt", "y"]]},
                "*-review": {"text": REVIEW_PASS},
            }
        }
    )
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["status"] == "awaiting_approval"
    assert resumed["cost_usd"] == pytest.approx(0.6)
    prompt = prompt_of(implement_calls(fake_pi)[-1])
    assert "budget:cost" in prompt and "Verify failed" not in prompt
    assert "done.txt" in _git(Path(outcome["worktree_path"]), "show", "--stat", "HEAD~1")


# ── services failures ─────────────────────────────────────────────────


SERVICES = """[[services]]
name = "postgres"
image = "postgres:16"
port = 5432
ready_timeout = 1
"""


def services_project(project: Path, setup: str | None = None) -> None:
    project_file = project / ".helmraiser" / "project.toml"
    project_file.parent.mkdir(exist_ok=True)
    defaults = f'[defaults]\nsetup = "{setup}"\n\n' if setup else ""
    project_file.write_text(defaults + SERVICES)


@pytest.fixture
def port_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loop_services, "port_ready", lambda port: True)


def services_failed_run(project, data_dir, fake_docker, setup=None) -> dict:
    services_project(project, setup)
    path = write_task(project, FIXED_VERIFY)
    assert invoke("intake", str(path), "--yes", data_dir=data_dir).exit_code == 0
    fake_docker.write_script({"run_fail": {"postgres": "docker: no such image"}})
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 1, result.output
    outcome = json.loads(result.output.splitlines()[0])
    assert outcome["failure_reason"] == (
        "services: postgres: docker run failed: docker: no such image"
    )
    return outcome


@pytest.mark.parametrize("flags", [(), ("--implement",)])
def test_retry_restarts_a_services_failure_without_commits_despite_setup_debris(
    tmp_project, data_dir, fake_pi, fake_docker, port_ready, flags
):
    outcome = services_failed_run(tmp_project, data_dir, fake_docker, setup="touch setup-debris")
    worktree = Path(outcome["worktree_path"])
    assert "setup-debris" in _git(worktree, "status", "--porcelain")
    assert fake_pi.calls() == []

    result = invoke("retry", outcome["run_id"], *flags, "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["stage"] == "start" and out["restarted_without_commits"] is True
    fake_docker.write_script({})
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {"text": "Done", "files": [["done.txt", "done"]]},
                "*-review": {"text": REVIEW_PASS},
            }
        }
    )
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["status"] == "awaiting_approval"
    assert [s["name"] for s in resumed["services"]] == ["postgres"]
    ledger = Ledger(data_dir / "helmraiser.db")
    assert ledger.events(outcome["run_id"], types=("setup_skipped",))
    assert ledger.events(outcome["run_id"], types=("service_ready",))
    assert fake_docker.containers() == {}


def test_retry_resumes_a_services_failure_with_commits_at_verify(
    tmp_project, data_dir, fake_pi, fake_docker, port_ready
):
    """The services start again: verify is what they serve."""
    outcome = services_failed_run(tmp_project, data_dir, fake_docker)
    worktree = Path(outcome["worktree_path"])
    (worktree / "done.txt").write_text("done\n")
    _git(worktree, "add", "done.txt")
    _git(worktree, "commit", "-m", "Finish")

    result = invoke("retry", outcome["run_id"], "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["stage"] == "verify" and "restarted_without_commits" not in out
    fake_docker.write_script({})
    fake_pi.write_script({"sessions": {"*-review": {"text": REVIEW_PASS}}})
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["status"] == "awaiting_approval"
    assert [s["name"] for s in resumed["services"]] == ["postgres"]
    assert sessions(fake_pi) == ["review"]


def test_retry_implement_of_a_services_failure_still_needs_a_failed_verify_with_commits(
    tmp_project, data_dir, fake_pi, fake_docker
):
    outcome = services_failed_run(tmp_project, data_dir, fake_docker)
    worktree = Path(outcome["worktree_path"])
    (worktree / "done.txt").write_text("done\n")
    _git(worktree, "add", "done.txt")
    _git(worktree, "commit", "-m", "Finish")

    result = invoke("retry", outcome["run_id"], "--implement", "--json", data_dir=data_dir)

    assert result.exit_code == 2, result.output
    assert "no failed verify result" in json.loads(result.output)["error"]


def test_services_failing_on_a_verify_resume_keep_uncommitted_work_on_the_branch(
    tmp_project, data_dir, fake_pi, fake_docker, port_ready
):
    """A retry of a run blocked with edits commits them first, so another retry after the
    services fail resumes at verify: it must not restart real work from the beginning."""
    outcome = blocked_run(tmp_project, data_dir, fake_pi)
    worktree = Path(outcome["worktree_path"])
    assert "notes.txt" in _git(worktree, "status", "--porcelain")
    services_project(tmp_project)
    write_task(tmp_project, FIXED_VERIFY)
    assert invoke("retry", outcome["run_id"], "--json", data_dir=data_dir).exit_code == 0
    fake_docker.write_script({"run_fail": {"postgres": "docker: no such image"}})

    result = invoke("work", "--once", "--json", data_dir=data_dir)

    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["failure_reason"].startswith("services: postgres")
    assert _git(worktree, "status", "--porcelain") == ""
    result = invoke("retry", outcome["run_id"], "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["stage"] == "verify"


def test_retry_restart_text_names_the_resume_point(tmp_project, data_dir, fake_pi):
    outcome = blocked_run(tmp_project, data_dir, fake_pi, clean=True)
    result = invoke("retry", outcome["run_id"], data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert (
        "  no commits beyond the base: the worker restarts the run from its start"
        in result.output.splitlines()
    )


@pytest.mark.parametrize("missing", ["metadata", "branch"])
def test_retry_restart_refuses_missing_worktree_metadata_or_branch(
    tmp_project, data_dir, fake_pi, missing
):
    outcome = blocked_run(tmp_project, data_dir, fake_pi, clean=True)
    worktree = Path(outcome["worktree_path"])
    if missing == "metadata":
        (worktree.parent / f"{outcome['run_id']}.json").unlink()
    else:
        _git(worktree, "checkout", "--detach")
        _git(tmp_project, "branch", "-D", outcome["branch_name"])
    result = invoke("retry", outcome["run_id"], "--json", data_dir=data_dir)
    assert result.exit_code == 1, result.output
    assert "cannot be retried" in json.loads(result.output)["error"]


def test_retry_restart_respects_project_require_approval(tmp_project, data_dir, fake_pi):
    outcome = blocked_run(tmp_project, data_dir, fake_pi, clean=True)
    path = write_task(tmp_project, FIXED_VERIFY)
    path.write_text(path.read_text().replace("policy: review", "policy: auto"))
    project_file = tmp_project / ".helmraiser" / "project.toml"
    project_file.parent.mkdir(exist_ok=True)
    project_file.write_text("[defaults]\nrequire_approval = true\n")
    result = invoke("retry", outcome["run_id"], "--json", data_dir=data_dir)
    assert result.exit_code == 2, result.output
    out = json.loads(result.output)
    assert out["error"] == "invalid task"
    assert any("requires approval" in error for error in out["errors"])
    ledger = Ledger(data_dir / "helmraiser.db")
    assert ledger.get_run(outcome["run_id"])["status"] == "failed"
    assert ledger.events(outcome["run_id"], types=("handed_back",)) == []
