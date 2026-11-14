"""Tests for conftest.py fixtures."""

import os
import shutil
import subprocess


def test_gh_guard(_silent_notify_bin):
    assert shutil.which("gh") == str(_silent_notify_bin / "gh")
    result = subprocess.run(["gh", "pr", "view"], capture_output=True, text=True)
    assert result.returncode == 97
    assert result.stderr.strip() == (
        "the test suite must not run the real gh; put a fake gh on PATH"
    )


def test_gh_guard_can_be_overridden(tmp_path, monkeypatch):
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\necho fake\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    result = subprocess.run(["gh"], capture_output=True, text=True)
    assert (result.returncode, result.stdout.strip()) == (0, "fake")


class TestGitInitFixture:
    def test_git_init_creates_repo(self, git_init, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        git_init(repo)
        assert (repo / ".git").exists()

    def test_git_init_has_initial_commit(self, git_init, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        git_init(repo)
        result = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "Initial commit" in result.stdout

    def test_git_init_has_readme(self, git_init, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        git_init(repo)
        assert (repo / "README.md").exists()
        assert (repo / "README.md").read_text() == "# Test Project\n"


class TestTmpProjectFixture:
    def test_tmp_project_is_git_initialized(self, tmp_project):
        assert (tmp_project / ".git").exists()

    def test_tmp_project_has_readme(self, tmp_project):
        assert (tmp_project / "README.md").exists()
