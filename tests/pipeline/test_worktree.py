"""Integration tests for run-specific worktrees and local finalization."""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from helmraiser.pipeline.worktree import WorktreeManager


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "trunk")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test User")
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")
    (path / "base.txt").write_text("base\nsecond\n", encoding="utf-8")
    git(path, "commit", "-am", "base")
    return path


def test_worktree_dir_property_is_read_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    default = WorktreeManager(repo)
    external = tmp_path / "external"
    configured = WorktreeManager(repo, worktree_dir=external)

    assert default.worktree_dir == repo / ".helmraiser" / "worktrees"
    assert configured.worktree_dir == external
    assert not default.worktree_dir.exists()
    assert not external.exists()
    with pytest.raises(AttributeError):
        configured.worktree_dir = repo  # type: ignore[misc]


def test_bounded_git_execution(repo, monkeypatch):
    manager = WorktreeManager(repo, command_timeout=12)
    real_run = subprocess.run
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    manager._git("rev-parse", "HEAD")
    manager._git("rev-parse", "HEAD", timeout=3)
    assert [c["timeout"] for c in calls] == [12, 3]
    assert all(c["shell"] is False and c["env"]["GIT_TERMINAL_PROMPT"] == "0" for c in calls)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 12)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        manager._git("status")


@pytest.mark.parametrize("previous", [None, "false", "true", ""])
def test_disable_auto_maintenance(repo, monkeypatch, previous):
    if previous is not None:
        git(repo, "config", "--local", "maintenance.auto", previous)
    before = git(repo, "config", "--local", "--list").splitlines()
    manager = WorktreeManager(repo)
    calls = []
    real_git = manager._git

    def observed(*args, **kwargs):
        calls.append(args)
        return real_git(*args, **kwargs)

    monkeypatch.setattr(manager, "_git", observed)
    assert manager.disable_auto_maintenance() == {
        "changed": previous != "false",
        "previous": previous,
    }
    assert len(calls) == (1 if previous == "false" else 2)
    assert git(repo, "config", "--local", "--get", "maintenance.auto") == "false"
    after = git(repo, "config", "--local", "--list").splitlines()
    assert [v for v in before if not v.startswith("maintenance.auto=")] == [
        v for v in after if not v.startswith("maintenance.auto=")
    ]


def test_disable_auto_maintenance_git_failure(tmp_path):
    with pytest.raises(RuntimeError, match="git config.*failed:"):
        WorktreeManager(tmp_path).disable_auto_maintenance()


@pytest.mark.parametrize("configured,expected", [(None, 60), (7, 7)])
def test_disable_auto_maintenance_is_bounded(repo, monkeypatch, configured, expected):
    manager = WorktreeManager(repo, command_timeout=configured)
    real_run = subprocess.run
    calls = []

    def observed(argv, **kwargs):
        calls.append(kwargs)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", observed)
    manager.disable_auto_maintenance()
    assert [c["timeout"] for c in calls] == [expected, expected]
    assert all(c["cwd"] == repo for c in calls)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], expected)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        manager.disable_auto_maintenance()


def test_disabled_auto_maintenance_preserves_loose_refs(repo):
    for i in range(300):
        git(repo, "branch", f"feature/t{i}/run{i}")
    WorktreeManager(repo).disable_auto_maintenance()
    git(repo, "commit", "--allow-empty", "-m", "after disabling maintenance")
    git(repo, "fetch", ".", "trunk:refs/remotes/x/trunk")
    assert not (repo / ".git" / "packed-refs").exists()
    assert (repo / ".git" / "refs/heads/feature/t7/run7").is_file()


def test_run_maintenance_packs_loose_refs(repo):
    manager = WorktreeManager(repo)
    manager.disable_auto_maintenance()
    for i in range(300):
        git(repo, "branch", f"feature/t{i}/run{i}")
    manager.run_maintenance()
    assert "refs/heads/feature/t7/run7" in (repo / ".git/packed-refs").read_text()


def test_run_maintenance_failure(tmp_path):
    with pytest.raises(RuntimeError, match="not a git repository"):
        WorktreeManager(tmp_path).run_maintenance()


@pytest.mark.parametrize("configured,expected", [(None, 60), (7, 7)])
def test_run_maintenance_is_bounded(repo, monkeypatch, configured, expected):
    manager = WorktreeManager(repo, command_timeout=configured)
    real_run = subprocess.run
    calls = []

    def observed(argv, **kwargs):
        calls.append((argv, kwargs))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", observed)
    manager.run_maintenance()
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["git", "maintenance", "run", "--auto"]
    assert kwargs["cwd"] == repo
    assert kwargs["timeout"] == expected

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], expected)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        manager.run_maintenance()


def test_finalize_remote_push_callback(repo):
    manager = WorktreeManager(repo, command_timeout=5)
    run_id = new_id()
    worktree = manager.create("topic", run_id)
    calls = []

    def push(path, branch, remote):
        assert path == worktree and path.exists()
        calls.append((branch, remote))

    manager.finalize_remote("topic", run_id, push=push, deliver=lambda *_: "url")
    assert calls == [(manager._branch("topic", run_id), "origin")]
    assert not worktree.exists()


@pytest.mark.parametrize(
    "condition,reason",
    [
        ("clean", "fast_forward"),
        ("untracked", "fast_forward"),
        ("dirty", "dirty_checkout"),
        ("other_branch", "not_checked_out"),
        ("other_worktree", "other_worktree"),
        ("diverged", "not_fast_forward"),
        ("equal", "up_to_date"),
        ("ahead", "up_to_date"),
    ],
)
def test_fast_forward_base(repo, tmp_path, condition, reason):
    manager = WorktreeManager(repo)
    original = git(repo, "rev-parse", "trunk")
    git(repo, "checkout", "-b", "run")
    (repo / "result.txt").write_text("result")
    git(repo, "add", "result.txt")
    git(repo, "commit", "-m", "result")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "trunk")
    if condition == "dirty":
        (repo / "base.txt").write_text("dirty")
    elif condition == "untracked":
        (repo / "untracked.txt").touch()
    elif condition in ("other_branch", "other_worktree"):
        git(repo, "checkout", "run")
        if condition == "other_worktree":
            git(repo, "worktree", "add", str(tmp_path / "other"), "trunk")
    elif condition == "diverged":
        (repo / "base.txt").write_text("diverged")
        git(repo, "commit", "-am", "diverged")
    elif condition in ("equal", "ahead"):
        git(repo, "merge", "--ff-only", head)
        if condition == "ahead":
            (repo / "base.txt").write_text("ahead")
            git(repo, "commit", "-am", "ahead")
    before = git(repo, "rev-parse", "trunk")
    result = manager.fast_forward_base("trunk", head)
    assert result["reason"] == reason
    assert result["status"] == ("updated" if reason == "fast_forward" else "skipped")
    assert git(repo, "rev-parse", "trunk") == (head if reason == "fast_forward" else before)
    assert manager.command_timeout is None
    if condition == "dirty":
        assert (repo / "base.txt").read_text() == "dirty"
        assert before == original


@pytest.mark.parametrize(
    "error", [RuntimeError("merge failed"), subprocess.TimeoutExpired("git", 60)]
)
def test_fast_forward_base_failure(repo, monkeypatch, error):
    manager = WorktreeManager(repo)
    original = git(repo, "rev-parse", "HEAD")

    def fail(*args, **kwargs):
        assert manager.command_timeout == 60
        raise error

    monkeypatch.setattr(manager, "_git", fail)
    assert manager.fast_forward_base("trunk", original)["status"] == "failed"
    assert git(repo, "rev-parse", "HEAD") == original
    assert manager.command_timeout is None


def new_id() -> str:
    return str(uuid.uuid4())


@pytest.mark.parametrize("finish", ["discard", "local", "remote"])
def test_legacy_run_recovery(repo: Path, tmp_path: Path, finish: str):
    legacy = WorktreeManager(repo)
    run_id = new_id()
    worktree = legacy.create("topic", run_id)
    metadata_path = legacy._metadata_path(run_id)
    manager = WorktreeManager(repo, worktree_dir=tmp_path / "external")

    assert manager.path_for("topic", run_id) == worktree
    assert manager.metadata(run_id) == legacy.metadata(run_id)
    assert manager.create("topic", run_id, resume=True) == worktree
    assert manager.path_for("topic", new_id()).parent == tmp_path / "external"
    manager.adopt_base(run_id, git(repo, "rev-parse", "HEAD"))
    assert metadata_path.exists()
    assert not (tmp_path / "external").exists()

    (worktree / "result.txt").write_text("done\n")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "result")
    if finish == "discard":
        manager.discard("topic", run_id)
        assert git(repo, "rev-parse", f"feature/topic/{run_id}")
    elif finish == "local":
        manager.finalize_local("topic", run_id)
        assert (repo / "result.txt").read_text() == "done\n"
    else:
        remote = tmp_path / "remote.git"
        git(tmp_path, "init", "--bare", str(remote))
        git(repo, "remote", "add", "origin", str(remote))
        manager.finalize_remote("topic", run_id)
        assert git(remote, "rev-parse", f"feature/topic/{run_id}")
    assert not worktree.exists()
    assert not metadata_path.exists()


def test_current_metadata_wins_over_legacy(repo: Path, tmp_path: Path):
    legacy = WorktreeManager(repo)
    run_id = new_id()
    legacy.create("topic", run_id)
    external = tmp_path / "external"
    external.mkdir()
    current = {**legacy.metadata(run_id), "base_commit": "current"}
    (external / f"{run_id}.json").write_text(json.dumps(current))
    manager = WorktreeManager(repo, worktree_dir=external)
    assert manager.metadata(run_id) == current
    assert manager.path_for("topic", run_id).parent == external
    with pytest.raises(RuntimeError, match="metadata does not match"):
        manager.create("topic", run_id, resume=True)


def test_legacy_tampered_path_is_still_refused(repo: Path, tmp_path: Path):
    legacy = WorktreeManager(repo)
    run_id = new_id()
    legacy.create("topic", run_id)
    metadata = {**legacy.metadata(run_id), "worktree_path": str(tmp_path / "wrong")}
    legacy._metadata_path(run_id).write_text(json.dumps(metadata))
    manager = WorktreeManager(repo, worktree_dir=tmp_path / "external")
    with pytest.raises(RuntimeError, match="metadata does not match"):
        manager.create("topic", run_id, resume=True)
    with pytest.raises(RuntimeError, match="outside its expected path"):
        manager.finalize_local("topic", run_id)


def test_create_uses_captured_arbitrary_base_and_run_specific_names(repo: Path):
    run_id = new_id()
    manager = WorktreeManager(repo)

    worktree = manager.create("topic", run_id)

    assert worktree.name == f"worktree-topic-{run_id}"
    assert git(worktree, "branch", "--show-current") == f"feature/topic/{run_id}"
    metadata = manager._read_metadata(run_id)
    assert metadata["base_branch"] == "trunk"
    assert metadata["base_commit"] == git(repo, "rev-parse", "HEAD")


def test_adopt_base_updates_only_the_base_commit(repo: Path):
    manager = WorktreeManager(repo)
    run_id = new_id()
    manager.create("topic", run_id)
    before = manager.metadata(run_id)
    (repo / "base.txt").write_text("moved\n")
    git(repo, "commit", "-am", "move base")
    tip = git(repo, "rev-parse", "HEAD")

    manager.adopt_base(run_id, tip)

    assert manager.metadata(run_id) == {**before, "base_commit": tip}


def test_adopt_base_unknown_run_raises_like_metadata(repo: Path):
    manager = WorktreeManager(repo)
    run_id = new_id()
    with pytest.raises(RuntimeError, match="no recoverable worktree state") as expected:
        manager.metadata(run_id)
    with pytest.raises(RuntimeError) as actual:
        manager.adopt_base(run_id, git(repo, "rev-parse", "HEAD"))
    assert str(actual.value) == str(expected.value)


def test_simultaneous_same_topic_worktrees_are_run_isolated(repo: Path):
    manager = WorktreeManager(repo)
    first, second = new_id(), new_id()
    a, b = manager.create("same-topic", first), manager.create("same-topic", second)
    assert a != b
    assert git(a, "branch", "--show-current") != git(b, "branch", "--show-current")
    assert manager._metadata_path(first) != manager._metadata_path(second)
    assert manager.metadata(first)["base_commit"] == manager.metadata(second)["base_commit"]
    (a / "base.txt").write_text("first only\n")
    git(a, "commit", "-am", "first isolated change")
    assert (b / "base.txt").read_text() == "base\nsecond\n"
    manager.discard("same-topic", first)
    assert not a.exists() and not manager._metadata_path(first).exists()
    assert b.exists() and manager._metadata_path(second).exists()
    assert git(b, "rev-parse", "HEAD") == manager.metadata(second)["base_commit"]
    manager.discard("same-topic", second)


def test_create_uses_explicit_base_branch_tip(repo: Path) -> None:
    git(repo, "branch", "-m", "main")
    git(repo, "checkout", "-b", "programme/x")
    (repo / "programme.txt").write_text("programme\n", encoding="utf-8")
    git(repo, "add", "programme.txt")
    git(repo, "commit", "-m", "programme")
    programme_tip = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    manager = WorktreeManager(repo)
    run_id = new_id()

    worktree = manager.create("topic", run_id, base="programme/x")

    metadata = manager.metadata(run_id)
    assert metadata["base_branch"] == "programme/x"
    assert metadata["base_commit"] == programme_tip
    assert git(worktree, "rev-parse", "HEAD") == programme_tip
    assert (worktree / "programme.txt").read_text(encoding="utf-8") == "programme\n"


def test_create_with_explicit_base_allows_detached_primary(repo: Path) -> None:
    git(repo, "branch", "programme/x")
    git(repo, "checkout", "--detach")

    worktree = WorktreeManager(repo).create("topic", new_id(), base="programme/x")

    assert git(worktree, "merge-base", "--is-ancestor", "programme/x", "HEAD") == ""


def test_create_refuses_missing_explicit_base(repo: Path) -> None:
    with pytest.raises(RuntimeError, match="base branch programme/missing does not exist"):
        WorktreeManager(repo).create("topic", new_id(), base="programme/missing")


def test_discard_removes_worktree_and_metadata(repo: Path) -> None:
    manager = WorktreeManager(repo)
    run_id = new_id()
    worktree = manager.create("topic", run_id)

    manager.discard("topic", run_id)

    assert not worktree.exists()
    assert not manager._metadata_path(run_id).exists()
    assert manager.orphaned_metadata() == []


def test_discard_tolerates_missing_metadata(repo: Path) -> None:
    manager = WorktreeManager(repo)
    run_id = new_id()
    worktree = manager.create("topic", run_id)
    manager._metadata_path(run_id).unlink()

    manager.discard("topic", run_id)

    assert not worktree.exists()


def test_orphaned_metadata_reports_only_removed_worktrees(repo: Path) -> None:
    manager = WorktreeManager(repo)
    run_id = new_id()
    worktree = manager.create("topic", run_id)

    assert manager.orphaned_metadata() == []

    git(repo, "worktree", "remove", str(worktree))

    assert manager.orphaned_metadata() == [
        {
            "run_id": run_id,
            "topic_id": "topic",
            "branch_name": f"feature/topic/{run_id}",
            "worktree_path": str(worktree),
            "metadata_path": str(manager._metadata_path(run_id)),
            "branch_exists": True,
        }
    ]


def test_orphans_scan_both_directories_with_current_precedence(repo: Path, tmp_path: Path):
    legacy = WorktreeManager(repo)
    manager = WorktreeManager(repo, worktree_dir=tmp_path / "external")
    old_id, current_id = new_id(), new_id()
    old = legacy.create("old", old_id)
    current = manager.create("current", current_id)
    git(repo, "worktree", "remove", str(old))
    git(repo, "worktree", "remove", str(current))
    duplicate = manager._worktree_dir / f"{old_id}.json"
    duplicate.write_text(legacy._metadata_path(old_id).read_text())

    orphans = manager.orphaned_metadata()

    assert {item["run_id"] for item in orphans} == {old_id, current_id}
    assert len(orphans) == 2
    assert next(item for item in orphans if item["run_id"] == old_id)["metadata_path"] == str(
        duplicate
    )
    duplicate.unlink()
    assert next(item for item in manager.orphaned_metadata() if item["run_id"] == old_id)[
        "metadata_path"
    ] == str(legacy._metadata_path(old_id))


def test_create_never_reuses_another_runs_worktree_or_branch(repo: Path):
    manager = WorktreeManager(repo)
    first, second = new_id(), new_id()
    first_path = manager.create("topic", first)
    second_path = manager.create("topic", second)

    assert first_path != second_path
    assert manager.exists("topic", first)
    assert manager.exists("topic", second)
    with pytest.raises(RuntimeError, match="use resume"):
        manager.create("topic", first)


def test_create_refuses_detached_primary(repo: Path):
    git(repo, "checkout", "--detach")
    with pytest.raises(RuntimeError, match="detached"):
        WorktreeManager(repo).create("topic", new_id())


def test_external_worktree_dir_round_trip_and_orphaned_metadata(repo: Path, tmp_path: Path) -> None:
    external_dir = tmp_path / "external" / "run-worktrees"
    manager = WorktreeManager(repo, worktree_dir=external_dir)
    run_id = new_id()
    worktree = manager.create("topic", run_id)
    (worktree / "result.txt").write_text("external\n", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "external result")

    manager.finalize_local("topic", run_id)

    assert (repo / "result.txt").read_text(encoding="utf-8") == "external\n"
    assert not worktree.exists()
    assert not manager._metadata_path(run_id).exists()

    orphan_id = new_id()
    orphan = manager.create("orphan", orphan_id)
    git(repo, "worktree", "remove", str(orphan))

    assert manager.orphaned_metadata() == [
        {
            "run_id": orphan_id,
            "topic_id": "orphan",
            "branch_name": f"feature/orphan/{orphan_id}",
            "worktree_path": str(orphan),
            "metadata_path": str(manager._metadata_path(orphan_id)),
            "branch_exists": True,
        }
    ]


def test_finalize_local_fast_forwards_and_cleans_successful_run(repo: Path):
    manager = WorktreeManager(repo)
    run_id = new_id()
    worktree = manager.create("topic", run_id)
    (worktree / "result.txt").write_text("done\n", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "result")

    manager.finalize_local("topic", run_id)

    assert (repo / "result.txt").read_text(encoding="utf-8") == "done\n"
    assert not worktree.exists()
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/feature/topic/{run_id}"],
            cwd=repo,
        ).returncode
        != 0
    )
    assert not manager._metadata_path(run_id).exists()


@pytest.mark.parametrize("condition", ["dirty", "moved_branch", "moved_head", "detached"])
def test_finalize_refusal_preserves_recoverable_run(repo: Path, condition: str):
    manager = WorktreeManager(repo)
    run_id = new_id()
    worktree = manager.create("topic", run_id)
    (worktree / "result.txt").write_text("done\n", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "result")

    if condition == "dirty":
        (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    elif condition == "moved_branch":
        git(repo, "checkout", "-b", "other")
    elif condition == "moved_head":
        (repo / "base.txt").write_text("moved\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "moved")
    else:
        git(repo, "checkout", "--detach")

    with pytest.raises(RuntimeError):
        manager.finalize_local("topic", run_id)

    assert worktree.exists()
    assert manager._metadata_path(run_id).exists()
    assert git(worktree, "branch", "--show-current") == f"feature/topic/{run_id}"


def test_finalize_local_names_current_and_run_base_when_branches_differ(repo: Path) -> None:
    manager = WorktreeManager(repo)
    run_id = new_id()
    manager.create("topic", run_id)
    git(repo, "checkout", "-b", "other")

    with pytest.raises(
        RuntimeError, match="primary checkout is on other, not the run's base trunk"
    ):
        manager.finalize_local("topic", run_id)


def test_finalize_refuses_dirty_run_worktree_without_losing_changes(repo: Path):
    manager = WorktreeManager(repo)
    run_id = new_id()
    worktree = manager.create("topic", run_id)
    changed = worktree / "uncommitted.txt"
    changed.write_text("keep me\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="run worktree is dirty"):
        manager.finalize_local("topic", run_id)

    assert changed.read_text(encoding="utf-8") == "keep me\n"
    assert manager.exists("topic", run_id)
    assert manager._metadata_path(run_id).exists()


def test_non_fast_forward_failure_preserves_worktree_and_branch(repo: Path):
    manager = WorktreeManager(repo)
    run_id = new_id()
    worktree = manager.create("topic", run_id)
    git(worktree, "reset", "--hard", "HEAD^")
    (worktree / "other.txt").write_text("other\n", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "diverged")

    with pytest.raises(RuntimeError, match="cannot fast-forward"):
        manager.finalize_local("topic", run_id)

    assert worktree.exists()
    assert manager.exists("topic", run_id)


def test_finalize_rebase_lands_on_moved_head(repo: Path):
    manager = WorktreeManager(repo)
    run_id = new_id()
    worktree = manager.create("topic", run_id)
    (worktree / "run.txt").write_text("run\n", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "run work")
    (repo / "elsewhere.txt").write_text("moved\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "moved")
    moved = git(repo, "rev-parse", "HEAD")

    manager.finalize_local("topic", run_id, rebase=True)

    assert git(repo, "rev-parse", "HEAD^") == moved
    assert (repo / "run.txt").read_text(encoding="utf-8") == "run\n"
    assert (repo / "elsewhere.txt").exists()
    assert not worktree.exists()
    assert not manager._metadata_path(run_id).exists()


def test_finalize_remote_pushes_and_cleans_without_touching_dirty_primary(
    repo: Path, tmp_path: Path
):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(repo, "remote", "add", "origin", str(remote))
    manager = WorktreeManager(repo)
    run_id = new_id()
    worktree = manager.create("topic", run_id)
    branch = f"feature/topic/{run_id}"
    (worktree / "result.txt").write_text("done\n", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "result")
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    metadata = manager.finalize_remote("topic", run_id)

    assert metadata == {"base_branch": "trunk", "branch_name": branch}
    assert git(remote, "rev-parse", f"refs/heads/{branch}") == git(repo, "rev-parse", branch)
    assert not worktree.exists()
    assert not manager._metadata_path(run_id).exists()
    assert branch in git(repo, "branch", "--list")


def test_finalize_remote_pushes_and_delivers_explicit_base(repo: Path, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "branch", "programme/x")
    manager = WorktreeManager(repo)
    run_id = new_id()
    worktree = manager.create("topic", run_id, base="programme/x")
    branch = f"feature/topic/{run_id}"
    (worktree / "result.txt").write_text("done\n", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "result")
    delivered: list[tuple[str, str]] = []

    metadata = manager.finalize_remote(
        "topic",
        run_id,
        deliver=lambda base, head: delivered.append((base, head)) or "https://example.test/pr/1",
    )

    assert delivered == [("programme/x", branch)]
    assert metadata["base_branch"] == "programme/x"
    assert git(remote, "rev-parse", f"refs/heads/{branch}") == git(repo, "rev-parse", branch)


def test_finalize_remote_rebases_onto_moved_base(repo: Path, tmp_path: Path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(repo, "remote", "add", "origin", str(remote))
    manager = WorktreeManager(repo)
    run_id = new_id()
    worktree = manager.create("topic", run_id)
    (worktree / "run.txt").write_text("run\n", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "run")
    (repo / "base-moved.txt").write_text("moved\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "move base")
    moved = git(repo, "rev-parse", "HEAD")

    metadata = manager.finalize_remote("topic", run_id, rebase=True)

    assert git(repo, "rev-parse", f"{metadata['branch_name']}^") == moved
    assert git(remote, "rev-parse", f"refs/heads/{metadata['branch_name']}") == git(
        repo, "rev-parse", metadata["branch_name"]
    )


def test_finalize_rebase_conflict_is_aborted_and_refused(repo: Path):
    manager = WorktreeManager(repo)
    run_id = new_id()
    worktree = manager.create("topic", run_id)
    (worktree / "base.txt").write_text("run side\n", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "run work")
    (repo / "base.txt").write_text("primary side\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "moved")

    with pytest.raises(RuntimeError, match="rebase onto moved HEAD failed"):
        manager.finalize_local("topic", run_id, rebase=True)

    assert worktree.exists()
    assert not (worktree / ".git" / "rebase-merge").exists()
    assert git(worktree, "status", "--porcelain") == ""
    assert (worktree / "base.txt").read_text(encoding="utf-8") == "run side\n"
    assert manager._metadata_path(run_id).exists()


def test_finalize_remote_delivers_before_removing_and_keeps_worktree_on_failure(
    repo: Path, tmp_path: Path
):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(repo, "remote", "add", "origin", str(remote))
    manager = WorktreeManager(repo)
    run_id = new_id()
    worktree = manager.create("topic", run_id)
    (worktree / "result.txt").write_text("done\n", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "result")
    seen: list[tuple[str, str, bool]] = []

    def failing(base: str, head: str) -> str:
        seen.append((base, head, worktree.exists()))
        raise RuntimeError("gh: HTTP 502")

    with pytest.raises(RuntimeError, match="502"):
        manager.finalize_remote("topic", run_id, deliver=failing)

    assert seen == [("trunk", f"feature/topic/{run_id}", True)]
    assert worktree.exists() and manager._metadata_path(run_id).exists()

    metadata = manager.finalize_remote(
        "topic", run_id, deliver=lambda base, head: "https://example.test/pr/9"
    )

    assert metadata["pr_url"] == "https://example.test/pr/9"
    assert not worktree.exists()


def test_git_hooks_do_not_see_the_worker_code_snapshot(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project's git hooks run on the worktree's code, not the worker's snapshot."""
    seen = tmp_path / "hook-env.txt"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        f'#!/bin/sh\necho "${{PYTHONPATH-}}|${{HELMRAISER_CODE_SNAPSHOT-}}" > {seen}\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)
    monkeypatch.setenv("PYTHONPATH", "/data/code/abc123/src:/keep")
    monkeypatch.setenv("HELMRAISER_CODE_SNAPSHOT", "abc123")
    monkeypatch.setenv("HELMRAISER_SOURCE_REPO", "/checkout")
    manager = WorktreeManager(repo)
    worktree = manager.create("topic", new_id())
    (worktree / "agent.txt").write_text("work\n", encoding="utf-8")

    manager.salvage_uncommitted(worktree)

    assert seen.read_text(encoding="utf-8").strip() == "/keep|"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("retained", [False, True])
def test_restore_pr(repo, tmp_path, legacy, retained):
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    manager = WorktreeManager(repo, None if legacy else tmp_path / "trees")
    run_id = new_id()
    tree = manager.create("topic", run_id)
    metadata = manager.metadata(run_id)
    git(tree, "commit", "--allow-empty", "-m", "published")
    head = git(tree, "rev-parse", "HEAD")
    git(tree, "push", "origin", metadata["branch_name"])
    if not retained:
        manager.discard("topic", run_id)
    # A newly configured manager must still recover an old canonical legacy path.
    manager = WorktreeManager(repo, tmp_path / "trees")
    restored = manager.restore_pr(
        "topic",
        run_id,
        branch_name=metadata["branch_name"],
        base_branch="trunk",
        worktree_path=str(tree),
    )
    assert restored["head_commit"] == head
    assert restored["worktree_recreated"] is (not retained)
    assert manager.metadata(run_id)["base_commit"] == metadata["base_commit"]
    assert manager.create("topic", run_id, resume=True) == tree
    assert git(tree, "rev-parse", "HEAD") == head
    assert not git(repo, "for-each-ref", "refs/helmraiser/pr-restore")


@pytest.mark.parametrize(
    "problem", ["dirty", "path", "branch", "local", "remote", "ahead", "diverged"]
)
def test_restore_pr_refuses_unsafe_state(repo, tmp_path, problem):
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    manager = WorktreeManager(repo)
    run_id = new_id()
    tree = manager.create("topic", run_id)
    branch = manager.metadata(run_id)["branch_name"]
    git(tree, "commit", "--allow-empty", "-m", "published")
    git(tree, "push", "origin", branch)
    if problem == "dirty":
        (tree / "base.txt").write_text("dirty")
    else:
        manager.discard("topic", run_id)
    if problem == "local":
        git(repo, "branch", "-D", branch)
    elif problem == "remote":
        git(repo, "push", "origin", "--delete", branch)
    elif problem in ("ahead", "diverged"):
        git(repo, "checkout", branch)
        if problem == "diverged":
            git(repo, "reset", "--hard", "trunk")
        git(repo, "commit", "--allow-empty", "-m", "local only")
        git(repo, "checkout", "trunk")
    with pytest.raises(RuntimeError):
        manager.restore_pr(
            "topic",
            run_id,
            branch_name="wrong" if problem == "branch" else branch,
            base_branch="trunk",
            worktree_path=str(repo if problem == "path" else tree),
        )
    if problem == "dirty":
        assert (tree / "base.txt").read_text() == "dirty"
    assert not git(repo, "for-each-ref", "refs/helmraiser/pr-restore")


def test_restore_pr_fast_forwards_retained_local_branch(repo, tmp_path):
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    manager = WorktreeManager(repo)
    run_id = new_id()
    tree = manager.create("topic", run_id)
    branch = manager.metadata(run_id)["branch_name"]
    old = git(tree, "rev-parse", "HEAD")
    git(tree, "commit", "--allow-empty", "-m", "remote ahead")
    head = git(tree, "rev-parse", "HEAD")
    git(tree, "push", "origin", branch)
    manager.discard("topic", run_id)
    git(repo, "update-ref", f"refs/heads/{branch}", old, head)
    restored = manager.restore_pr(
        "topic",
        run_id,
        branch_name=branch,
        base_branch="trunk",
        worktree_path=str(tree),
    )
    assert restored["head_commit"] == git(tree, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("ordinary", [False, True])
def test_salvage_excludes_placeholders(repo, ordinary):
    from helmraiser.isolation.placeholders import PLACEHOLDER_NAMES

    manager = WorktreeManager(repo)
    tree = manager.create("topic", str(uuid.uuid4()))
    before = git(tree, "rev-parse", "HEAD")
    for name in PLACEHOLDER_NAMES:
        (tree / name).touch()
    if ordinary:
        (tree / "work.txt").write_text("agent work")
    manager.salvage_uncommitted(tree)
    assert not set(PLACEHOLDER_NAMES) & set(git(tree, "ls-files").splitlines())
    assert (git(tree, "rev-parse", "HEAD") != before) == ordinary
    assert not git(tree, "status", "--porcelain")


def test_salvage_preserves_legitimate_candidates(repo):
    manager = WorktreeManager(repo)
    tree = manager.create("topic", str(uuid.uuid4()))
    (tree / ".gitmodules").touch()
    git(tree, "add", ".gitmodules")
    git(tree, "commit", "-m", "tracked candidate")
    (tree / ".mcp.json").write_text("legitimate config")
    (tree / ".bashrc").touch()
    manager.salvage_uncommitted(tree)
    assert git(tree, "ls-files", ".gitmodules", ".mcp.json").splitlines() == [
        ".gitmodules",
        ".mcp.json",
    ]
    assert not (tree / ".bashrc").exists()


def test_salvage_cleanup_failure_never_stages(repo, monkeypatch):
    from helmraiser.isolation import placeholders

    manager = WorktreeManager(repo)
    tree = manager.create("topic", str(uuid.uuid4()))
    (tree / ".bashrc").touch()
    real_git = manager._git
    calls = []

    def observe(*args, **kwargs):
        calls.append(args)
        return real_git(*args, **kwargs)

    def fail(cwd):
        raise PermissionError("busy")

    monkeypatch.setattr(manager, "_git", observe)
    monkeypatch.setattr(placeholders, "remove_placeholders", fail)
    with pytest.raises(PermissionError):
        manager.salvage_uncommitted(tree)
    assert not any(args[0] in ("add", "commit") for args in calls)
