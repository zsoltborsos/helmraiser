"""Root-only, index-aware placeholder cleanup."""

import errno
import subprocess
from pathlib import Path

import pytest

from helmraiser.isolation.placeholders import PLACEHOLDER_NAMES, remove_placeholders


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    return tmp_path


@pytest.mark.parametrize("directory", [False, True])
def test_all_names(repo, directory):
    for name in PLACEHOLDER_NAMES:
        path = repo / name
        path.mkdir() if directory else path.touch()
    assert remove_placeholders(repo) == list(PLACEHOLDER_NAMES)
    assert remove_placeholders(repo) == []


def test_preserved_paths(repo):
    (repo / ".gitmodules").touch()
    (repo / ".idea").mkdir()
    (repo / ".idea/tracked").touch()
    git(repo, "add", ".gitmodules", ".idea/tracked")
    (repo / ".mcp.json").write_text("config")
    (repo / ".vscode").mkdir()
    (repo / ".vscode/config").touch()
    (repo / "subdir").mkdir()
    (repo / "subdir/.bashrc").touch()
    (repo / "unrelated").touch()
    (repo / ".profile").symlink_to("unrelated")
    (repo / ".zshrc").symlink_to("absent")
    assert remove_placeholders(repo) == []
    assert (repo / ".gitmodules").exists()
    assert (repo / ".idea/tracked").exists()
    assert (repo / ".vscode/config").exists()
    assert (repo / "subdir/.bashrc").exists()
    assert (repo / ".profile").is_symlink()
    assert (repo / ".zshrc").is_symlink()


def test_non_root_noop(repo, tmp_path):
    nested = repo / "nested"
    nested.mkdir()
    (nested / ".bashrc").touch()
    assert remove_placeholders(nested) == []
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (outside / ".bashrc").touch()
    assert remove_placeholders(outside) == []
    assert (nested / ".bashrc").exists()
    assert (outside / ".bashrc").exists()


def test_linked_worktree(repo):
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@t",
        "commit",
        "--allow-empty",
        "-m",
        "init",
    )
    linked = repo / "linked"
    git(repo, "worktree", "add", "-b", "linked", str(linked))
    (linked / ".bashrc").touch()
    assert remove_placeholders(linked) == [".bashrc"]


def test_busy_propagates(repo, monkeypatch):
    (repo / ".bashrc").touch()

    def busy(self):
        raise OSError(errno.EBUSY, "mounted")

    monkeypatch.setattr(Path, "unlink", busy)
    with pytest.raises(OSError, match="mounted"):
        remove_placeholders(repo)
    assert (repo / ".bashrc").exists()


def test_index_error(repo, monkeypatch):
    real_run = subprocess.run

    def run(argv, **kwargs):
        if "ls-files" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "index error")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    (repo / ".bashrc").touch()
    with pytest.raises(subprocess.CalledProcessError):
        remove_placeholders(repo)
    assert (repo / ".bashrc").exists()
