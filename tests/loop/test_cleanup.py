"""Cleanup safety exercised against real Git worktrees and a real ledger."""

import subprocess
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from helmraiser.ledger.db import Ledger
from helmraiser.loop.cleanup import clean_worktrees
from helmraiser.pipeline.worktree import WorktreeManager
from helmraiser.task.project import worktree_manager
from tests.ledger.test_db import make_spec, run_row

NOW = 20 * 86400.0


def git(project: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=project, text=True, capture_output=True, check=True
    ).stdout.strip()


def seed_run(ledger, project, *, status="failed", started=1, ended=2, topic="t1"):
    spec = make_spec(project, topic)
    task_id = ledger.upsert_task(spec, "snapshot")
    run_id = str(uuid4())
    manager = worktree_manager(project)
    worktree = manager.create(topic, run_id)
    metadata = manager.metadata(run_id)
    ledger.create_run(
        {
            **run_row(run_id, task_id, str(project), status),
            "started_at": started,
            "ended_at": ended,
            "worktree_path": str(worktree),
            "branch_name": metadata["branch_name"],
            "base_commit": metadata["base_commit"],
            "failure_reason": "verify: tests failed",
        }
    )
    ledger.set_task_status(task_id, status)
    return ledger.get_run(run_id)


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "data" / "helmraiser.db")


def sweep(ledger, **kwargs):
    return clean_worktrees(ledger, config=kwargs.pop("config", {}), now=NOW, **kwargs)


def test_superseded(ledger, tmp_project):
    old = seed_run(ledger, tmp_project, ended=NOW)
    seed_run(ledger, tmp_project, status="landed", started=2)
    result = sweep(ledger)
    assert [(r["run_id"], r["reason"], r["action"]) for r in result] == [
        (old["run_id"], "superseded", "removed")
    ]


def test_old_aborted(ledger, tmp_project):
    seed_run(ledger, tmp_project, status="aborted")
    assert sweep(ledger)[0]["reason"] == "age"


def test_superseded_precedes_age(ledger, tmp_project):
    seed_run(ledger, tmp_project)
    seed_run(ledger, tmp_project, status="landed", started=2)
    assert sweep(ledger)[0]["reason"] == "superseded"


@pytest.mark.parametrize("ended", [NOW, NOW - 7 * 86400, None])
def test_not_old(ledger, tmp_project, ended):
    seed_run(ledger, tmp_project, ended=ended)
    assert sweep(ledger) == []


@pytest.mark.parametrize("started,topic", [(0, "t1"), (1, "t1"), (2, "other")])
def test_not_superseded(ledger, tmp_project, started, topic):
    seed_run(ledger, tmp_project, ended=NOW)
    seed_run(ledger, tmp_project, status="landed", started=started, topic=topic)
    assert sweep(ledger) == []


def test_null_disables_only_age(ledger, tmp_project):
    row = seed_run(ledger, tmp_project)
    config = {"cleanup": {"failed_after_days": None}}
    assert sweep(ledger, config=config) == []
    seed_run(ledger, tmp_project, status="landed", started=2)
    assert sweep(ledger, config=config)[0]["run_id"] == row["run_id"]


@pytest.mark.parametrize(
    "status", ["landed", "awaiting_approval", "running", "resuming", "aborting"]
)
def test_other_statuses_untouched(ledger, tmp_project, status):
    row = seed_run(ledger, tmp_project, status=status)
    assert sweep(ledger) == []
    assert Path(row["worktree_path"]).exists()


@pytest.mark.parametrize("filename", ["README.md", "untracked"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_dirty_kept(ledger, tmp_project, filename, dry_run):
    row = seed_run(ledger, tmp_project)
    (Path(row["worktree_path"]) / filename).write_text("changed")
    result = sweep(ledger, dry_run=dry_run)
    assert result[0]["action"] == "kept"
    assert result[0]["detail"] == "dirty: 1 paths"
    assert Path(row["worktree_path"]).exists()
    assert ledger.events(row["run_id"]) == []


def test_dry_run(ledger, tmp_project):
    row = seed_run(ledger, tmp_project)
    assert sweep(ledger, dry_run=True)[0]["action"] == "would_remove"
    assert Path(row["worktree_path"]).exists()
    assert worktree_manager(tmp_project).metadata(row["run_id"])
    assert git(tmp_project, "rev-parse", row["branch_name"])
    assert ledger.events(row["run_id"]) == []


def test_keeps_commits_deletes_metadata_records_event(ledger, tmp_project):
    row = seed_run(ledger, tmp_project)
    tree = Path(row["worktree_path"])
    (tree / "committed").write_text("valuable")
    git(tree, "add", ".")
    git(tree, "commit", "-m", "run-only commit")
    head = git(tree, "rev-parse", "HEAD")
    result = sweep(ledger)
    assert result[0]["action"] == "removed"
    assert not tree.exists()
    assert git(tmp_project, "rev-parse", row["branch_name"]) == head
    manager = worktree_manager(tmp_project)
    assert manager.get_info("t1", row["run_id"]) is None
    with pytest.raises(RuntimeError, match="no recoverable"):
        manager.metadata(row["run_id"])
    event = ledger.events(row["run_id"])[0]
    assert event["type"] == "worktree_cleaned"
    assert event["payload"] == {
        "reason": "age",
        "worktree_path": str(tree),
        "branch_name": row["branch_name"],
    }
    assert ledger.get_run(row["run_id"])["status"] == "failed"
    assert sweep(ledger) == []
    assert len(ledger.events(row["run_id"])) == 1


def test_status_race(ledger, tmp_project, monkeypatch):
    row = seed_run(ledger, tmp_project)
    original = WorktreeManager._finalization_lock

    @contextmanager
    def changed(manager):
        with original(manager):
            ledger.update_run(row["run_id"], status="resuming")
            yield

    monkeypatch.setattr(WorktreeManager, "_finalization_lock", changed)
    result = sweep(ledger)
    assert result[0]["detail"] == "status changed: resuming"
    assert Path(row["worktree_path"]).exists()


def test_unregistered_skipped(ledger, tmp_project):
    row = seed_run(ledger, tmp_project)
    worktree_manager(tmp_project).remove("t1", row["run_id"])
    assert sweep(ledger) == []


def test_missing_project_or_path_skipped(ledger, tmp_project, tmp_path):
    row = seed_run(ledger, tmp_project)
    ledger.update_run(row["run_id"], project=str(tmp_path / "missing"))
    assert sweep(ledger) == []
    ledger.update_run(row["run_id"], project=str(tmp_project), worktree_path=None)
    assert sweep(ledger) == []


def test_filter_and_order(ledger, tmp_project, tmp_path, git_init):
    other = tmp_path / "another"
    other.mkdir()
    git_init(other)
    rows = [seed_run(ledger, p) for p in (tmp_project, other, tmp_project)]
    filtered = sweep(ledger, project=tmp_project, dry_run=True)
    assert len(filtered) == 2
    result = sweep(ledger, dry_run=True)
    assert [(r["project"], r["run_id"]) for r in result] == sorted(
        (r["project"], r["run_id"]) for r in rows
    )


@pytest.mark.parametrize("ledger_uses_symlink", [True, False])
def test_project_filter_matches_symlink_and_real_path(
    ledger, tmp_project, tmp_path, ledger_uses_symlink
):
    alias = tmp_path / "project-link"
    alias.symlink_to(tmp_project, target_is_directory=True)
    recorded = alias if ledger_uses_symlink else tmp_project
    selected = tmp_project if ledger_uses_symlink else alias
    row = seed_run(ledger, recorded)

    result = sweep(ledger, project=selected)

    assert [(entry["run_id"], entry["action"]) for entry in result] == [(row["run_id"], "removed")]
    assert result[0]["project"] == str(recorded)
    assert not Path(row["worktree_path"]).exists()


def test_broken_project_does_not_stop_others(ledger, tmp_project, tmp_path, git_init):
    bad = tmp_path / "bad"
    bad.mkdir()
    git_init(bad)
    seed_run(ledger, bad)
    seed_run(ledger, tmp_project)
    (bad / ".helmraiser").mkdir(exist_ok=True)
    (bad / ".helmraiser" / "project.toml").write_text("not valid toml !")
    result = sweep(ledger)
    assert {r["project"]: r["action"] for r in result} == {
        str(bad): "kept",
        str(tmp_project): "removed",
    }
    assert next(r["detail"] for r in result if r["project"] == str(bad))


@pytest.mark.parametrize(
    "error", [RuntimeError("refused"), subprocess.CalledProcessError(1, "git")]
)
def test_remove_error_kept(ledger, tmp_project, monkeypatch, error):
    first = seed_run(ledger, tmp_project)
    second = seed_run(ledger, tmp_project)
    original = WorktreeManager.discard

    def discard(manager, topic_id, run_id):
        if run_id == first["run_id"]:
            raise error
        return original(manager, topic_id, run_id)

    monkeypatch.setattr(WorktreeManager, "discard", discard)
    result = {r["run_id"]: r for r in sweep(ledger)}
    assert result[first["run_id"]]["action"] == "kept"
    assert result[first["run_id"]]["detail"] == str(error)
    assert result[second["run_id"]]["action"] == "removed"
