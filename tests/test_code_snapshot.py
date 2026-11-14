"""The background worker's code snapshots: ``<data_dir>/code/<commit>/src/helmraiser``.

A throwaway git repository holds a stand-in ``src/helmraiser`` package, so the import
checks really run a Python that must prefer the snapshot over this checkout's
editable install.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import helmraiser
from helmraiser import code_snapshot
from helmraiser.code_snapshot import (
    SNAPSHOT_ENV,
    SOURCE_REPO_ENV,
    SnapshotError,
    exec_into,
    export,
    import_problem,
    loaded_from,
    prune,
    snapshot_env,
    snapshot_path,
    source_repo,
    without_snapshot,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str = "change") -> str:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


PACKAGE = {
    "src/helmraiser/__init__.py": "MARK = 'one'\n",
    "src/helmraiser/cli/__init__.py": "",
    "src/helmraiser/cli/app.py": "app = 'stand-in'\n",
}


@pytest.fixture
def repo(tmp_path: Path, git_init: Any) -> Path:
    path = tmp_path / "checkout"
    path.mkdir()
    git_init(path)
    commit(path, PACKAGE, "package")
    return path


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return tmp_path / "data"


class TestExport:
    def test_exports_the_committed_package_only(self, repo: Path, base: Path):
        head = _git(repo, "rev-parse", "HEAD")
        (repo / "src/helmraiser/__init__.py").write_text("MARK = 'dirty'\n")
        (repo / "src/helmraiser/untracked.py").write_text("X = 1\n")
        (repo / "README.md").write_text("dirty docs\n")

        snapshot = export(repo, head, base)

        assert snapshot == base / "code" / head == snapshot_path(base, head)
        package = snapshot / "src" / "helmraiser"
        assert (package / "__init__.py").read_text() == "MARK = 'one'\n"
        assert (package / "cli" / "app.py").is_file()
        assert not (package / "untracked.py").exists()
        assert not (snapshot / "README.md").exists()
        assert [p.name for p in (base / "code").iterdir()] == [head]  # no temp dir left

    def test_reuses_an_existing_snapshot(self, repo: Path, base: Path):
        head = _git(repo, "rev-parse", "HEAD")
        snapshot = export(repo, head, base)
        (snapshot / "marker").write_text("kept\n")

        assert export(repo, head, base) == snapshot
        assert (snapshot / "marker").read_text() == "kept\n"

    def test_an_unknown_commit_fails_and_leaves_nothing(self, repo: Path, base: Path):
        with pytest.raises(SnapshotError):
            export(repo, "f" * 40, base)

        assert list((base / "code").iterdir()) == []


class TestEnvironment:
    def test_prepends_the_snapshot_and_sets_the_markers(self, repo: Path, base: Path):
        snapshot = base / "code" / "abc"
        env = snapshot_env(snapshot, "abc", repo, {"PYTHONPATH": "/extra", "HOME": "/h"})

        assert env["PYTHONPATH"] == f"{snapshot / 'src'}{os.pathsep}/extra"
        assert env[SNAPSHOT_ENV] == "abc" and env[SOURCE_REPO_ENV] == str(repo)
        assert env["HOME"] == "/h"

    def test_a_new_snapshot_replaces_the_previous_one(self, repo: Path, base: Path):
        old = snapshot_env(base / "code" / "old", "old", repo, {"PYTHONPATH": "/extra"})
        new = snapshot_env(base / "code" / "new", "new", repo, old)

        assert new["PYTHONPATH"] == f"{base / 'code' / 'new' / 'src'}{os.pathsep}/extra"
        assert new[SNAPSHOT_ENV] == "new"

    def test_without_snapshot_restores_the_original_environment(self, repo: Path, base: Path):
        assert without_snapshot(snapshot_env(base / "code" / "c", "c", repo, {})) == {}
        env = snapshot_env(base / "code" / "c", "c", repo, {"PYTHONPATH": "/extra", "A": "1"})
        assert without_snapshot(env) == {"PYTHONPATH": "/extra", "A": "1"}


class TestImportCheck:
    def test_the_snapshot_wins_over_the_editable_install(self, repo: Path, base: Path):
        head = _git(repo, "rev-parse", "HEAD")
        snapshot = export(repo, head, base)
        env = snapshot_env(snapshot, head, repo, os.environ)

        assert import_problem(env, snapshot) is None
        out = subprocess.run(
            [
                sys.executable,
                "-c",
                "import helmraiser; print(helmraiser.__file__, helmraiser.MARK)",
            ],
            env=env,
            cwd=snapshot,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        assert Path(out[0]).resolve().is_relative_to(snapshot.resolve()) and out[1] == "one"

    def test_a_broken_snapshot_is_reported(self, repo: Path, base: Path):
        broken = commit(repo, {"src/helmraiser/cli/app.py": "def broken(:\n"}, "broken")
        snapshot = export(repo, broken, base)

        problem = import_problem(snapshot_env(snapshot, broken, repo, os.environ), snapshot)

        assert problem is not None and problem.startswith("the new code does not import: ")
        assert "SyntaxError" in problem

    def test_code_imported_from_elsewhere_is_reported(self, repo: Path, base: Path):
        head = _git(repo, "rev-parse", "HEAD")
        snapshot = export(repo, head, base)

        problem = import_problem(without_snapshot(os.environ), snapshot)

        assert problem is not None and "does not load from the snapshot" in problem

    def test_loaded_from(self, base: Path):
        here = Path(helmraiser.__file__).resolve().parent
        assert loaded_from(here.parent.parent)
        assert not loaded_from(base / "code" / "abc")


class TestExecInto:
    def test_execs_the_same_command_on_the_snapshot(
        self, repo: Path, base: Path, monkeypatch: pytest.MonkeyPatch
    ):
        head = _git(repo, "rev-parse", "HEAD")
        calls: list[Any] = []

        def execve(path: str, argv: list[str], env: dict[str, str]) -> None:
            calls.append((path, argv, env))
            raise OSError("exec refused")

        monkeypatch.setattr(code_snapshot.os, "execve", execve)

        problem = exec_into(head, repo, base, lambda: calls.append("announced"))

        assert problem == "exec failed: exec refused"
        assert calls[0] == "announced"
        path, argv, env = calls[1]
        assert (path, argv) == (sys.executable, sys.orig_argv)
        assert env[SNAPSHOT_ENV] == head and env[SOURCE_REPO_ENV] == str(repo)
        assert env["PYTHONPATH"].split(os.pathsep)[0] == str(base / "code" / head / "src")

    def test_a_broken_commit_is_not_execed(
        self, repo: Path, base: Path, monkeypatch: pytest.MonkeyPatch
    ):
        broken = commit(repo, {"src/helmraiser/cli/app.py": "import nowhere_at_all\n"}, "broken")
        calls: list[Any] = []
        monkeypatch.setattr(code_snapshot.os, "execve", lambda *a: calls.append("exec"))

        problem = exec_into(broken, repo, base, lambda: calls.append("announced"))

        assert problem is not None and "nowhere_at_all" in problem and calls == []

    def test_an_export_failure_is_not_execed(
        self, repo: Path, base: Path, monkeypatch: pytest.MonkeyPatch
    ):
        calls: list[Any] = []
        monkeypatch.setattr(code_snapshot.os, "execve", lambda *a: calls.append("exec"))

        problem = exec_into("f" * 40, repo, base, lambda: calls.append("announced"))

        assert problem is not None and problem.startswith("could not export ffffffff")
        assert calls == []


class TestPrune:
    def test_keeps_the_current_and_the_two_most_recent_others(self, base: Path):
        code = base / "code"
        names = ["c1", "c2", "c3", "c4", "current"]
        for age, name in enumerate(names):
            (code / name / "src").mkdir(parents=True)
            os.utime(code / name, (1000 + age, 1000 + age))
        os.utime(code / "current", (1, 1))  # the oldest, but in use
        (code / ".c5-tmp").mkdir()  # an interrupted export

        removed = prune(base, "current")

        assert sorted(p.name for p in removed) == [".c5-tmp", "c1", "c2"]
        assert sorted(p.name for p in code.iterdir()) == ["c3", "c4", "current"]

    def test_protected_child_snapshots_outlive_retention(self, base: Path):
        code = base / "code"
        for age, name in enumerate(["c1", "c2", "c3", "c4", "c5", "current"]):
            (code / name).mkdir(parents=True)
            os.utime(code / name, (1000 + age, 1000 + age))
        (code / ".interrupted").mkdir()
        removed = prune(base, "current", protected_commits={"c1", "c2"})
        assert sorted(p.name for p in removed) == [".interrupted", "c3"]
        assert sorted(p.name for p in code.iterdir()) == ["c1", "c2", "c4", "c5", "current"]
        assert sorted(p.name for p in prune(base, "current")) == ["c1", "c2"]

    def test_without_a_code_dir(self, base: Path):
        assert prune(base, "current") == []


class TestSourceRepo:
    def test_the_marker_wins(self, tmp_path: Path):
        assert source_repo({SOURCE_REPO_ENV: str(tmp_path)}) == tmp_path

    def test_the_checkout_the_package_is_loaded_from(self):
        repo = source_repo({})
        here = Path(helmraiser.__file__).resolve().parent
        assert repo is not None and (repo / "src" / "helmraiser").resolve() == here

    def test_a_package_outside_a_checkout_has_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(code_snapshot, "package_dir", lambda: tmp_path / "helmraiser")
        assert source_repo({}) is None
