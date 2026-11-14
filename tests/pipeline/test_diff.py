"""Real Git coverage of cumulative changed paths, without models."""

import subprocess

import pytest

from helmraiser.pipeline.diff import changed_paths


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    for name in (
        "committed",
        "staged",
        "unstaged",
        "deleted",
        "rename",
        "restore",
        ".helmraiser/tracked",
    ):
        p = tmp_path / name
        p.parent.mkdir(exist_ok=True)
        p.write_text(name)
    (tmp_path / ".gitignore").write_text("ignored\n.helmraiser/*\n")
    git(tmp_path, "add", "-f", ".")
    git(tmp_path, "commit", "-m", "base")
    return tmp_path, git(tmp_path, "rev-parse", "HEAD")


def test_cumulative_changes(repo):
    path, base = repo
    (path / "committed").write_text("changed")
    git(path, "commit", "-am", "change")
    (path / "staged").write_text("changed")
    git(path, "add", "staged")
    (path / "unstaged").write_text("changed")
    (path / "deleted").unlink()
    (path / ".helmraiser/tracked").write_text("changed")
    (path / ".helmraiser/ignored").write_text("ignored artifact")
    (path / "ignored").write_text("ignored")
    (path / "nested").mkdir()
    (path / "nested/untracked").write_text("new")
    (path / " spaces \n").write_text("new")
    assert changed_paths(path, base) == tuple(
        sorted(
            [
                "committed",
                "staged",
                "unstaged",
                "deleted",
                ".helmraiser/tracked",
                "nested/untracked",
                " spaces \n",
            ]
        )
    )


@pytest.mark.parametrize("committed", [False, True])
def test_rename_both_endpoints(repo, committed):
    path, base = repo
    git(path, "mv", "rename", "new-name")
    if committed:
        git(path, "commit", "-m", "rename")
    assert changed_paths(path, base) == ("new-name", "rename")


def test_net_restoration(repo):
    path, base = repo
    (path / "restore").write_text("changed")
    git(path, "commit", "-am", "change")
    (path / "restore").write_text("restore")
    assert changed_paths(path, base) == ()


def test_invalid_base_fails(repo):
    path, _ = repo
    with pytest.raises(RuntimeError, match="git diff failed"):
        changed_paths(path, "not-a-commit")


def test_unstaged_rename(repo):
    path, base = repo
    (path / "rename").rename(path / "untracked-name")
    assert changed_paths(path, base) == ("rename", "untracked-name")
