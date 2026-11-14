"""Hermetic base synchronization against a local bare upstream."""

import subprocess
from pathlib import Path

import pytest

from helmraiser.pipeline.worktree import WorktreeManager


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def setup_remote(project: Path, tmp_path: Path) -> tuple[Path, Path, str, str]:
    origin = tmp_path / "origin.git"
    git(project, "init", "--bare", str(origin))
    git(project, "remote", "add", "origin", str(origin))
    git(project, "push", "-u", "origin", "main")
    writer = tmp_path / "writer"
    git(project, "clone", "--branch", "main", str(origin), str(writer))
    git(writer, "config", "user.name", "Test")
    git(writer, "config", "user.email", "test@example.com")
    old = git(project, "rev-parse", "main")
    (writer / "README.md").write_text("upstream content\n")
    git(writer, "commit", "-am", "upstream change")
    git(writer, "push", "origin", "main")
    return origin, writer, old, git(writer, "rev-parse", "HEAD")


@pytest.fixture
def remote_repo(tmp_project, tmp_path):
    origin, writer, old, new = setup_remote(tmp_project, tmp_path)
    return tmp_project, origin, writer, old, new


def assert_clean_refs(project: Path) -> None:
    assert git(project, "for-each-ref", "refs/helmraiser/base-sync/") == ""


def test_base_sync_unchecked_base_fast_forwards_ref(remote_repo):
    project, _, _, old, new = remote_repo
    git(project, "checkout", "-b", "other")
    result = WorktreeManager(project).sync_base("main")
    assert result["status"] == "synced"
    assert (result["from_commit"], result["to_commit"], result["upstream_commit"]) == (
        old,
        new,
        new,
    )
    assert git(project, "rev-parse", "main") == new
    assert git(project, "rev-parse", "HEAD") == old
    assert (project / "README.md").read_text() == "# Test Project\n"
    assert_clean_refs(project)


def test_base_sync_checked_out_clean_updates_checkout(remote_repo):
    project, _, _, old, new = remote_repo
    result = WorktreeManager(project).sync_base()
    assert result["status"] == "synced"
    assert (result["from_commit"], result["to_commit"]) == (old, new)
    assert git(project, "rev-parse", "HEAD") == new
    assert git(project, "status", "--porcelain", "--untracked-files=no") == ""
    assert (project / "README.md").read_text() == "upstream content\n"
    assert_clean_refs(project)


def test_base_sync_overrides_configured_squash(remote_repo):
    project, _, _, old, new = remote_repo
    git(project, "config", "branch.main.mergeOptions", "--squash")
    result = WorktreeManager(project).sync_base()
    assert result["status"] == "synced"
    assert (result["from_commit"], result["to_commit"]) == (old, new)
    assert git(project, "rev-parse", "HEAD") == new
    assert git(project, "write-tree") == git(project, "rev-parse", f"{new}^{{tree}}")
    assert git(project, "status", "--porcelain", "--untracked-files=no") == ""
    assert (project / "README.md").read_text() == "upstream content\n"
    assert git(project, "config", "branch.main.mergeOptions") == "--squash"
    assert_clean_refs(project)


@pytest.mark.parametrize("checked_out", [False, True])
def test_base_sync_verifies_tip_before_reporting_success(remote_repo, monkeypatch, checked_out):
    project, _, _, old, new = remote_repo
    if not checked_out:
        git(project, "checkout", "-b", "other")
    manager = WorktreeManager(project)
    original = manager._git

    def unchanged_update(*args, **kwargs):
        if args[0] == "merge" or args[:2] == ("update-ref", "refs/heads/main"):
            return subprocess.CompletedProcess(["git", *args], 0, "", "")
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "_git", unchanged_update)
    result = manager.sync_base("main")
    assert result["status"] == "failed" and result["reason"] == "update_failed"
    assert result["from_commit"] == result["to_commit"] == old
    assert result["upstream_commit"] == new
    assert git(project, "rev-parse", "main") == old
    assert git(project, "status", "--porcelain", "--untracked-files=no") == ""
    assert_clean_refs(project)


def test_base_sync_uses_configured_remote_and_source(remote_repo):
    project, _, writer, _, new = remote_repo
    git(writer, "push", "origin", "main:refs/heads/trunk")
    git(project, "remote", "rename", "origin", "elsewhere")
    git(project, "config", "branch.main.merge", "refs/heads/trunk")
    # A dangerous configured fetch mapping must not bypass safety checks.
    git(project, "config", "remote.elsewhere.fetch", "+refs/heads/*:refs/heads/*")
    result = WorktreeManager(project).sync_base()
    assert result["status"] == "synced"
    assert result["remote"] == "elsewhere"
    assert result["upstream"] == "refs/heads/trunk"
    assert result["to_commit"] == new
    assert_clean_refs(project)


def test_base_sync_up_to_date(remote_repo):
    project, _, _, _, new = remote_repo
    manager = WorktreeManager(project)
    manager.sync_base()
    result = manager.sync_base()
    assert result["status"] == "skipped"
    assert result["reason"] == "up_to_date"
    assert result["from_commit"] == result["to_commit"] == new
    assert_clean_refs(project)


@pytest.mark.parametrize("change", ["unstaged", "staged", "deleted", "helmraiser"])
def test_base_sync_dirty_checkout(remote_repo, change):
    project, _, _, old, _ = remote_repo
    path = project / "README.md"
    if change == "helmraiser":
        path = project / ".helmraiser" / "tracked.txt"
        path.parent.mkdir()
        path.write_text("original")
        git(project, "add", str(path))
        git(project, "commit", "-m", "tracked helmraiser file")
        old = git(project, "rev-parse", "HEAD")
    if change == "deleted":
        path.unlink()
    else:
        path.write_text("local edits")
    if change == "staged":
        git(project, "add", str(path))
    before = git(project, "status", "--porcelain", "--untracked-files=no")
    index = git(project, "write-tree")
    result = WorktreeManager(project).sync_base()
    assert result["status"] == "skipped" and result["reason"] == "dirty_checkout"
    assert result["from_commit"] == result["to_commit"] == old
    assert git(project, "rev-parse", "HEAD") == old
    assert git(project, "status", "--porcelain", "--untracked-files=no") == before
    assert git(project, "write-tree") == index
    assert not path.exists() if change == "deleted" else path.read_text() == "local edits"
    assert_clean_refs(project)


def test_base_sync_other_worktree(remote_repo, tmp_path):
    project, _, _, old, _ = remote_repo
    git(project, "checkout", "-b", "other")
    other = tmp_path / "other checkout\nwith newline"
    git(project, "worktree", "add", str(other), "main")
    result = WorktreeManager(project).sync_base("main")
    assert result["reason"] == "other_worktree"
    assert result["from_commit"] == result["to_commit"] == old
    assert git(other, "rev-parse", "HEAD") == old
    assert (other / "README.md").read_text() == "# Test Project\n"
    assert_clean_refs(project)


@pytest.mark.parametrize("history", ["ahead", "diverged"])
def test_base_sync_not_fast_forward(remote_repo, history):
    project, _, _, _, new = remote_repo
    if history == "ahead":
        git(project, "fetch", "origin")
        git(project, "merge", "--ff-only", new)
    (project / "local.txt").write_text("local commit")
    git(project, "add", "local.txt")
    git(project, "commit", "-m", "local change")
    old = git(project, "rev-parse", "HEAD")
    before = (project / "README.md").read_text()
    result = WorktreeManager(project).sync_base()
    assert result["status"] == "skipped" and result["reason"] == "not_fast_forward"
    assert result["from_commit"] == result["to_commit"] == old
    assert git(project, "rev-parse", "HEAD") == old
    assert (project / "README.md").read_text() == before
    assert (project / "local.txt").read_text() == "local commit"
    assert_clean_refs(project)


@pytest.mark.parametrize("config", ["missing", "multiple"])
def test_base_sync_unsupported_upstream_no_fetch(remote_repo, monkeypatch, config):
    project, _, _, old, _ = remote_repo
    if config == "missing":
        git(project, "config", "--unset", "branch.main.remote")
    else:
        git(project, "config", "--add", "branch.main.merge", "refs/heads/other")
    manager = WorktreeManager(project)
    original = manager._git

    def no_fetch(*args, **kwargs):
        assert args[0] != "fetch"
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "_git", no_fetch)
    result = manager.sync_base()
    assert result["reason"] == ("no_upstream" if config == "missing" else "unsupported_upstream")
    assert result["from_commit"] == result["to_commit"] == old
    assert_clean_refs(project)


@pytest.mark.parametrize("collision", [False, True])
def test_base_sync_untracked_files(remote_repo, collision):
    project, _, writer, old, new = remote_repo
    path = project / "untracked.txt"
    path.write_text("preserve me")
    if collision:
        (writer / path.name).write_text("incoming")
        git(writer, "add", path.name)
        git(writer, "commit", "-m", "incoming file")
        git(writer, "push", "origin", "main")
    result = WorktreeManager(project).sync_base()
    assert result["status"] == ("failed" if collision else "synced")
    assert result["to_commit"] == (old if collision else new)
    if collision:
        assert result["reason"] == "update_failed"
    assert path.read_text() == "preserve me"
    assert_clean_refs(project)


@pytest.mark.parametrize("failure", ["missing_remote", "timeout"])
def test_base_sync_fetch_failure(remote_repo, monkeypatch, failure):
    project, _, _, old, _ = remote_repo
    if failure == "missing_remote":
        git(project, "remote", "set-url", "origin", str(project / "missing.git"))
    original = subprocess.run
    fetches = []
    monkeypatch.setenv("HELMRAISER_CODE_SNAPSHOT", "abc")
    monkeypatch.setenv("HELMRAISER_SOURCE_REPO", str(project))
    monkeypatch.setenv("PYTHONPATH", "/snapshot/code/abc/src:/keep")

    def intercept(command, **kwargs):
        if command[:2] == ["git", "fetch"]:
            fetches.append(command)
            assert kwargs["timeout"] == 60
            assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
            assert "HELMRAISER_CODE_SNAPSHOT" not in kwargs["env"]
            assert "HELMRAISER_SOURCE_REPO" not in kwargs["env"]
            assert kwargs["env"]["PYTHONPATH"] == "/keep"
            assert kwargs["cwd"] == project
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", intercept)
    result = WorktreeManager(project).sync_base()
    assert len(fetches) == 1
    assert result["status"] == "failed" and result["reason"] == "fetch_failed"
    assert result["from_commit"] == result["to_commit"] == old
    assert git(project, "rev-parse", "HEAD") == old
    assert_clean_refs(project)


@pytest.mark.parametrize("change", ["tip", "ownership", "dirty"])
def test_base_sync_rechecks_after_fetch(remote_repo, monkeypatch, tmp_path, change):
    project, _, _, old, _ = remote_repo
    git(project, "checkout", "-b", "other")
    manager = WorktreeManager(project)
    original = manager._git
    changed_tip = old

    def intercept(*args, **kwargs):
        nonlocal changed_tip
        result = original(*args, **kwargs)
        if args[0] == "fetch":
            if change == "tip":
                changed_tip = git(
                    project, "commit-tree", f"{old}^{{tree}}", "-p", old, "-m", "concurrent"
                )
                git(project, "update-ref", "refs/heads/main", changed_tip, old)
            elif change == "ownership":
                git(project, "worktree", "add", str(tmp_path / "other"), "main")
            else:
                git(project, "checkout", "main")
                (project / "README.md").write_text("concurrent edit")
        return result

    monkeypatch.setattr(manager, "_git", intercept)
    result = manager.sync_base("main")
    assert result["status"] == "skipped"
    assert (
        result["reason"]
        == {"tip": "base_moved", "ownership": "other_worktree", "dirty": "dirty_checkout"}[change]
    )
    assert result["from_commit"] == old
    assert result["to_commit"] == changed_tip
    assert git(project, "rev-parse", "main") == changed_tip
    assert_clean_refs(project)


def test_base_sync_detached_explicit_base(remote_repo):
    project, _, _, old, new = remote_repo
    git(project, "checkout", "--detach")
    manager = WorktreeManager(project)
    assert manager.sync_base()["reason"] == "detached_head"
    result = manager.sync_base("main")
    assert result["status"] == "synced" and result["to_commit"] == new
    assert git(project, "rev-parse", "HEAD") == old
    assert_clean_refs(project)


@pytest.mark.parametrize("checked_out", [False, True])
def test_require_current_base_fast_forwards(remote_repo, checked_out):
    project, _, _, old, new = remote_repo
    if not checked_out:
        git(project, "checkout", "-b", "scratch")
    result = WorktreeManager(project).require_current_base("main")
    assert result == {
        "status": "synced",
        "reason": "fast_forward",
        "base_branch": "main",
        "remote": "origin",
        "upstream": "refs/heads/main",
        "from_commit": old,
        "to_commit": new,
        "upstream_commit": new,
    }
    assert git(project, "rev-parse", "main") == new
    assert git(project, "rev-parse", "HEAD") == (new if checked_out else old)
    assert (project / "README.md").read_text() == (
        "upstream content\n" if checked_out else "# Test Project\n"
    )
    assert git(project, "status", "--porcelain", "--untracked-files=no") == ""
    assert_clean_refs(project)


def test_require_current_base_already_current(remote_repo):
    project, _, _, _, new = remote_repo
    manager = WorktreeManager(project)
    manager.sync_base("main")
    result = manager.require_current_base("main")
    assert result["reason"] == "up_to_date"
    assert result["from_commit"] == result["to_commit"] == new
    assert git(project, "rev-parse", "main") == new
    assert_clean_refs(project)


@pytest.mark.parametrize("current", [False, True])
@pytest.mark.parametrize("unsafe", ["other_worktree", "dirty_checkout"])
@pytest.mark.parametrize("custom_upstream", [False, True])
def test_require_current_base_unsafe_checkout_read_only(
    remote_repo, tmp_path, monkeypatch, current, unsafe, custom_upstream
):
    project, _, writer, old, new = remote_repo
    manager = WorktreeManager(project)
    if current:
        manager.sync_base("main")
    checkout = project
    if unsafe == "other_worktree":
        git(project, "checkout", "-b", "scratch")
        checkout = tmp_path / "side checkout"
        git(project, "worktree", "add", str(checkout), "main")
    # Preserve staged and unstaged changes in both unsafe checkout types.
    (checkout / "README.md").write_text("staged edits\n")
    git(checkout, "add", "README.md")
    (checkout / "README.md").write_text("unstaged edits\n")
    remote, source = "origin", "refs/heads/main"
    if custom_upstream:
        git(writer, "push", "origin", "main:refs/heads/trunk")
        git(project, "remote", "rename", "origin", "elsewhere")
        git(project, "config", "branch.main.merge", "refs/heads/trunk")
        remote, source = "elsewhere", "refs/heads/trunk"
    # Neither this dangerous refmap nor FETCH_HEAD may be touched by the fallback fetch.
    git(project, "config", f"remote.{remote}.fetch", "+refs/heads/*:refs/heads/*")
    fetch_head = Path(git(project, "rev-parse", "--git-path", "FETCH_HEAD"))
    if not fetch_head.is_absolute():
        fetch_head = project / fetch_head
    fetch_head.write_text("preserve FETCH_HEAD\n")
    refs = git(project, "for-each-ref", "refs/remotes/")
    index = git(checkout, "write-tree")
    status = git(checkout, "status", "--porcelain", "--untracked-files=no")
    primary_head = git(project, "rev-parse", "HEAD")
    primary_contents = (project / "README.md").read_bytes()
    original = subprocess.run
    fetches = []

    def intercept(command, **kwargs):
        if command[:2] == ["git", "fetch"]:
            fetches.append(command)
            assert command[2:8] == [
                "--no-tags",
                "--no-recurse-submodules",
                "--no-write-fetch-head",
                "--refmap=",
                "--",
                remote,
            ]
            assert command[8].startswith(f"{source}:refs/helmraiser/base-sync/")
            assert kwargs["timeout"] == 60
            assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", intercept)
    if current:
        result = manager.require_current_base("main")
        assert result["reason"] == unsafe
        assert result["upstream_commit"] == new
        assert (result["remote"], result["upstream"]) == (remote, source)
    else:
        with pytest.raises(RuntimeError) as exc:
            manager.require_current_base("main")
        text = str(exc.value)
        assert all(word in text for word in ("main", remote, source, "fetch", "approve again"))
    assert len(fetches) == 1
    assert manager.command_timeout is None
    assert git(project, "rev-parse", "main") == (new if current else old)
    assert git(checkout, "rev-parse", "HEAD") == (new if current else old)
    assert git(checkout, "write-tree") == index
    assert git(checkout, "status", "--porcelain", "--untracked-files=no") == status
    assert (checkout / "README.md").read_text() == "unstaged edits\n"
    assert git(project, "rev-parse", "HEAD") == primary_head
    assert (project / "README.md").read_bytes() == primary_contents
    assert git(project, "for-each-ref", "refs/remotes/") == refs
    assert fetch_head.read_text() == "preserve FETCH_HEAD\n"
    assert_clean_refs(project)


@pytest.mark.parametrize("history", ["ahead", "diverged"])
def test_require_current_base_local_history(remote_repo, history):
    project, _, _, _, _ = remote_repo
    manager = WorktreeManager(project)
    if history == "ahead":
        manager.sync_base("main")
    git(project, "commit", "--allow-empty", "-m", "local only")
    tip = git(project, "rev-parse", "main")
    if history == "ahead":
        assert manager.require_current_base("main")["reason"] == "not_fast_forward"
    else:
        with pytest.raises(RuntimeError, match="main.*origin/refs/heads/main.*fetch"):
            manager.require_current_base("main")
    assert git(project, "rev-parse", "main") == tip
    assert_clean_refs(project)


@pytest.mark.parametrize("config", ["missing_base", "missing_upstream", "multiple"])
def test_require_current_base_configuration(remote_repo, config):
    project, _, _, _, _ = remote_repo
    base = "absent" if config == "missing_base" else "main"
    if config == "missing_upstream":
        git(project, "branch", "--unset-upstream", "main")
    elif config == "multiple":
        git(project, "config", "--add", "branch.main.merge", "refs/heads/other")
    manager = WorktreeManager(project)
    if config == "multiple":
        with pytest.raises(RuntimeError, match="main.*origin.*ambiguous.*fetch"):
            manager.require_current_base(base)
    else:
        assert manager.require_current_base(base)["reason"] == (
            "no_base" if config == "missing_base" else "no_upstream"
        )
    assert_clean_refs(project)


@pytest.mark.parametrize("unsafe", [False, True])
@pytest.mark.parametrize("failure", ["unreachable", "timeout"])
@pytest.mark.parametrize("timeout", [None, 12])
def test_require_current_base_fetch_failure(remote_repo, monkeypatch, unsafe, failure, timeout):
    project, _, _, old, _ = remote_repo
    assert git(project, "rev-parse", "origin/main") == old
    if unsafe:
        (project / "README.md").write_text("dirty\n")
    if failure == "unreachable":
        git(project, "remote", "set-url", "origin", str(project / "missing.git"))
    original = subprocess.run
    fetches = []

    def intercept(command, **kwargs):
        if command[:2] == ["git", "fetch"]:
            fetches.append(command)
            assert kwargs["timeout"] == 60
            assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
            if failure == "timeout":
                # Simulate an interrupted fetch that already wrote its temporary ref.
                git(project, "update-ref", command[-1].split(":", 1)[1], old)
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", intercept)
    manager = WorktreeManager(project, command_timeout=timeout)
    with pytest.raises(RuntimeError, match="main.*origin/refs/heads/main.*fetch"):
        manager.require_current_base("main")
    assert len(fetches) == 1
    assert manager.command_timeout == timeout
    assert git(project, "rev-parse", "main") == old
    assert_clean_refs(project)


def test_require_current_base_compares_actual_ref(remote_repo, monkeypatch):
    project, _, _, old, _ = remote_repo
    manager = WorktreeManager(project)
    original = manager.sync_base
    calls = []

    def moved_base(*, base):
        calls.append(base)
        payload = original(base=base)
        git(project, "update-ref", "refs/heads/main", old)
        return payload

    monkeypatch.setattr(manager, "sync_base", moved_base)
    with pytest.raises(RuntimeError, match="main.*origin/refs/heads/main.*fetch"):
        manager.require_current_base("main")
    assert calls == ["main"]
    assert_clean_refs(project)


@pytest.mark.parametrize(
    "failure", [OSError("git unavailable"), subprocess.TimeoutExpired("git", 12)]
)
def test_require_current_base_contextualizes_process_errors(remote_repo, monkeypatch, failure):
    project, _, _, _, _ = remote_repo
    manager = WorktreeManager(project, command_timeout=12)
    original = manager._git

    def intercept(*args, **kwargs):
        assert manager.command_timeout == 12
        if args[:2] == ("merge-base", "--is-ancestor") and args[-1] == "refs/heads/main":
            raise failure
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "_git", intercept)
    with pytest.raises(RuntimeError, match="main.*origin/refs/heads/main.*fetch"):
        manager.require_current_base("main")
    assert manager.command_timeout == 12
    assert_clean_refs(project)
