"""Manual cleanup, worker sweeps and cleanup-aware retry contracts."""

import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, commands, supervisor
from helmraiser.ledger.db import Ledger, ProjectScope
from helmraiser.pipeline.worktree import WorktreeManager
from tests.loop.test_cleanup import git, seed_run

runner = CliRunner()


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "data" / "helmraiser.db")


def invoke(ledger, *args):
    return runner.invoke(app, ["clean", "--data-dir", str(ledger.db_path.parent), *args])


def test_empty_json(ledger):
    result = invoke(ledger, "--dry-run", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"dry_run": True, "runs": []}


@pytest.mark.parametrize("dry_run", [True, False])
def test_json_cleanup(ledger, tmp_project, dry_run):
    row = seed_run(ledger, tmp_project)
    result = invoke(ledger, "--json", *(["--dry-run"] if dry_run else []))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is dry_run
    assert payload["runs"][0]["action"] == ("would_remove" if dry_run else "removed")
    assert Path(row["worktree_path"]).exists() is dry_run
    assert git(tmp_project, "rev-parse", row["branch_name"])
    assert bool(ledger.events(row["run_id"])) is not dry_run


def test_dirty_exit_zero(ledger, tmp_project):
    row = seed_run(ledger, tmp_project)
    (Path(row["worktree_path"]) / "untracked").write_text("keep")
    result = invoke(ledger, "--json")
    assert result.exit_code == 0
    assert json.loads(result.output)["runs"][0]["detail"] == "dirty: 1 paths"


def test_text_counts(ledger, tmp_project):
    row = seed_run(ledger, tmp_project)
    result = invoke(ledger, "--dry-run")
    assert result.exit_code == 0
    for value in (row["run_id"], row["project"], row["worktree_path"], "age", "would_remove"):
        assert value in result.output
    assert result.output.endswith("0 removed, 1 would remove, 0 kept\n")


def test_empty_text(ledger):
    result = invoke(ledger)
    assert result.exit_code == 0
    assert result.output == "0 removed, 0 would remove, 0 kept\n"


def test_project_filter(ledger, tmp_project, tmp_path, git_init):
    other = tmp_path / "other"
    other.mkdir()
    git_init(other)
    row = seed_run(ledger, tmp_project)
    untouched = seed_run(ledger, other)
    result = invoke(ledger, "--project", str(tmp_project), "--json")
    assert result.exit_code == 0, result.output
    assert [r["run_id"] for r in json.loads(result.output)["runs"]] == [row["run_id"]]
    assert Path(untouched["worktree_path"]).exists()


def test_help():
    result = runner.invoke(app, ["clean", "--help"])
    assert result.exit_code == 0
    text = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    for option in ("--project", "--dry-run", "--json", "--data-dir"):
        assert option in text


@pytest.fixture
def maintenance(monkeypatch):
    operation = Mock()
    monkeypatch.setattr(
        WorktreeManager, "run_maintenance", lambda manager: operation(manager.codebase_dir)
    )
    return operation


@pytest.fixture
def worker(ledger, tmp_path, monkeypatch, maintenance):
    ticks = [0.0]
    instance = supervisor.Supervisor(
        ledger=ledger,
        data_dir=ledger.db_path.parent,
        scope=ProjectScope.of(),
        max_parallel=1,
        watch=True,
        max_runs=None,
        poll_s=30,
        stop_file=tmp_path / "stop",
        spawn=Mock(),
        skip=lambda row: None,
        notify=lambda payload: None,
        as_json=True,
        clock=lambda: ticks[0],
        wall_clock=lambda: 20 * 86400,
        run_config={},
        begin_pass=Mock(),
    )
    monkeypatch.setattr(instance, "_identify_children", lambda: None)
    monkeypatch.setattr(instance, "_reconcile", lambda scope: None)
    monkeypatch.setattr(instance, "_may_start", lambda: False)
    monkeypatch.setattr(instance.base_sync, "check", lambda *args, **kwargs: [])
    return instance, ticks


def test_sweep_throttled(worker, ledger, tmp_project, monkeypatch, maintenance):
    seed_run(ledger, tmp_project)
    instance, ticks = worker
    cleanup = Mock(return_value=[])
    monkeypatch.setattr(supervisor, "clean_worktrees", cleanup)
    instance._fill()
    assert cleanup.call_count == 1
    maintenance.assert_called_once_with(tmp_project)
    ticks[0] = 3599.99
    instance._fill()
    assert cleanup.call_count == 1
    assert maintenance.call_count == 1
    ticks[0] = 3600
    instance._fill()
    assert cleanup.call_count == 2
    assert maintenance.call_count == 2
    assert cleanup.call_args.kwargs["now"] == 20 * 86400


def test_sweep_paused(worker, ledger, tmp_project, monkeypatch, maintenance):
    seed_run(ledger, tmp_project)
    instance, ticks = worker
    cleanup = Mock(return_value=[])
    monkeypatch.setattr(supervisor, "clean_worktrees", cleanup)
    ledger.pause_queue("test")
    instance._fill()
    assert instance.last_worktree_sweep is None
    cleanup.assert_not_called()
    maintenance.assert_not_called()
    ledger.resume_queue()
    instance._fill()
    cleanup.assert_called_once()
    maintenance.assert_called_once_with(tmp_project)


def test_non_watch_never_sweeps(worker, ledger, tmp_project, monkeypatch, maintenance):
    seed_run(ledger, tmp_project)
    instance, _ = worker
    instance.watch = False
    cleanup = Mock()
    monkeypatch.setattr(supervisor, "clean_worktrees", cleanup)
    instance._fill()
    cleanup.assert_not_called()
    maintenance.assert_not_called()


@pytest.mark.parametrize("kind", ["run", "scout"])
def test_sweep_scope_and_busy_projects(
    worker, ledger, tmp_project, tmp_path, git_init, monkeypatch, kind, maintenance
):
    projects = [tmp_project]
    for name in ("excluded", "outside", "busy"):
        path = tmp_path / name
        path.mkdir()
        git_init(path)
        projects.append(path)
    for project in projects:
        seed_run(ledger, project)
    instance, _ = worker
    instance.scope = ProjectScope.of([projects[0], projects[1], projects[3]], [projects[1]])
    instance.children = [SimpleNamespace(project=str(projects[3]), kind=kind)]
    cleanup = Mock(return_value=[])
    monkeypatch.setattr(supervisor, "clean_worktrees", cleanup)
    instance._sweep_worktrees()
    cleanup.assert_called_once()
    assert cleanup.call_args.kwargs["project"] == tmp_project
    maintenance.assert_called_once_with(tmp_project)


def test_sweep_removes_real_tree_and_reports(worker, ledger, tmp_project, capsys):
    row = seed_run(ledger, tmp_project)
    instance, _ = worker
    instance._fill()
    payloads = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert payloads[0]["cleaned"][0]["run_id"] == row["run_id"]
    assert not Path(row["worktree_path"]).exists()
    assert ledger.events(row["run_id"])[0]["type"] == "worktree_cleaned"


def test_sweep_error_is_logged_throttled_and_isolated(
    worker, ledger, tmp_project, tmp_path, git_init, monkeypatch, capsys, maintenance
):
    other = tmp_path / "a-broken"
    other.mkdir()
    git_init(other)
    seed_run(ledger, other)
    good = seed_run(ledger, tmp_project)
    original = supervisor.clean_worktrees

    def cleanup(*args, **kwargs):
        if kwargs["project"] == other:
            raise RuntimeError("broken cleanup")
        return original(*args, **kwargs)

    cleanup_mock = Mock(side_effect=cleanup)
    monkeypatch.setattr(supervisor, "clean_worktrees", cleanup_mock)
    instance, _ = worker
    instance._fill()
    instance._fill()
    assert cleanup_mock.call_count == 2
    assert instance.begin_pass.call_count == 2
    assert instance.errors == {}
    assert not Path(good["worktree_path"]).exists()
    output = capsys.readouterr().out
    assert '"cleanup_error": "broken cleanup"' in output
    assert '"cleaned"' in output
    assert {call.args[0] for call in maintenance.call_args_list} == {other, tmp_project}


@pytest.mark.parametrize(
    "status", ["starting", "running", "resuming", "awaiting_approval", "failed", "landed"]
)
def test_sweep_maintenance_live_run_guard(
    worker, ledger, tmp_project, monkeypatch, maintenance, status
):
    seed_run(ledger, tmp_project, status="failed")
    seed_run(ledger, tmp_project, status=status, started=2)
    monkeypatch.setattr(supervisor, "clean_worktrees", Mock(return_value=[]))
    instance, _ = worker
    instance._sweep_worktrees()
    if status in {"starting", "running"}:
        maintenance.assert_not_called()
    else:
        maintenance.assert_called_once_with(tmp_project)


def test_sweep_maintenance_runs_after_cleanup(
    worker, ledger, tmp_project, monkeypatch, maintenance, capsys
):
    seed_run(ledger, tmp_project)
    calls = []
    monkeypatch.setattr(
        supervisor, "clean_worktrees", lambda *args, **kwargs: calls.append("clean") or []
    )
    maintenance.side_effect = lambda project: calls.append("maintenance")
    instance, _ = worker
    instance._sweep_worktrees()
    assert calls == ["clean", "maintenance"]
    assert capsys.readouterr().out == ""


def test_sweep_maintenance_error_is_logged_and_isolated(
    worker, ledger, tmp_project, tmp_path, git_init, maintenance, capsys
):
    other = tmp_path / "a-broken"
    other.mkdir()
    git_init(other)
    for project in (other, tmp_project):
        seed_run(ledger, project)

    def maintain(project):
        if project == other:
            raise RuntimeError("broken maintenance")

    maintenance.side_effect = maintain
    instance, _ = worker
    instance._sweep_worktrees()
    assert {call.args[0] for call in maintenance.call_args_list} == {other, tmp_project}
    payloads = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {"maintenance_error": "broken maintenance", "project": str(other)} in payloads
    assert instance.errors == {}


def test_sweep_maintenance_skips_deleted_project(
    worker, ledger, tmp_project, tmp_path, git_init, monkeypatch, maintenance, capsys
):
    gone = tmp_path / "gone"
    gone.mkdir()
    git_init(gone)
    for project in (gone, tmp_project):
        seed_run(ledger, project)
    shutil.rmtree(gone)
    monkeypatch.setattr(supervisor, "clean_worktrees", Mock(return_value=[]))
    instance, _ = worker
    instance._sweep_worktrees()
    maintenance.assert_called_once_with(tmp_project)
    assert capsys.readouterr().out == ""


def test_sweep_discovery_error_swallowed(worker, monkeypatch, capsys):
    instance, _ = worker
    monkeypatch.setattr(instance.ledger, "list_runs", Mock(side_effect=RuntimeError("discovery")))
    instance._fill()
    assert instance.begin_pass.call_count == 1
    assert '"cleanup_error": "discovery"' in capsys.readouterr().out


@pytest.mark.parametrize("reason", ["age", "superseded"])
@pytest.mark.parametrize("as_json", [True, False])
@pytest.mark.parametrize("implement", [True, False])
def test_retry_after_cleaning(ledger, tmp_project, reason, as_json, implement):
    row = seed_run(ledger, tmp_project)
    if reason == "superseded":
        seed_run(ledger, tmp_project, status="landed", started=2)
    task_status = ledger.get_task(row["task_id"])["status"]
    result = invoke(ledger, "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["runs"][0]["reason"] == reason
    event = ledger.events(row["run_id"], types=("worktree_cleaned",))[-1]
    args = ["retry", row["run_id"], "--data-dir", str(ledger.db_path.parent)]
    if as_json:
        args.append("--json")
    if implement:
        args.append("--implement")
    result = runner.invoke(app, args)
    assert result.exit_code == commands.EXIT_ERROR, result.output
    message = json.loads(result.output)["error"] if as_json else " ".join(result.output.split())
    assert "worktree was cleaned" in message
    assert f"({reason})" in message
    assert commands._iso(event["ts"]) in message
    assert f"branch {row['branch_name']} was kept" in message
    assert "queue the task again instead" in message
    assert ledger.get_run(row["run_id"])["status"] == "failed"
    assert ledger.get_task(row["task_id"])["status"] == task_status


def test_invalid_config(ledger, monkeypatch):
    def invalid():
        raise ValueError("cleanup.failed_after_days must be a positive integer or null")

    monkeypatch.setattr(commands, "load_config", invalid)
    result = invoke(ledger, "--json")
    assert result.exit_code == commands.EXIT_VALIDATION
    assert "cleanup.failed_after_days" in json.loads(result.output)["error"]
