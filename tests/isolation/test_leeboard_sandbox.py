"""Leeboard store and tool permissions belong only to ordinary pi sessions."""

import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from helmraiser import leeboard
from helmraiser.isolation import ProcessIsolation
from helmraiser.isolation.environment import session_environment
from tests.isolation.test_process_settings import REAL_SRT, _git, needs_real_srt
from tests.isolation.test_process_settings import _repo as _primary_repo
from tests.isolation.test_process_settings import home as home

# Capture before the fake-home fixture moves HOME beneath /tmp.
REAL_HOME = Path.home()


def _repo(path: Path) -> Path:
    """Session settings require a linked worktree on a namespaced run branch."""
    primary = _primary_repo(path)
    worktree = primary / ".helmraiser" / "worktrees" / "wt"
    _git(primary, "worktree", "add", "-q", "-b", "feature/topic/run", str(worktree))
    return worktree


@pytest.mark.parametrize("explicit", [False, True])
def test_leeboard_store_is_writable(home, tmp_path, monkeypatch, explicit):
    store = tmp_path / "custom-store" if explicit else home / ".leeboard"
    store.mkdir()
    if explicit:
        monkeypatch.setenv("LEEBOARD_HOME", str(store))
    repo = _repo(tmp_path / "repo")
    fs = ProcessIsolation().settings(repo, tmp_path / "run")["filesystem"]
    assert str(store) in fs["allowWrite"]
    assert fs["allowWrite"][0] == str(repo)


@pytest.mark.parametrize("git_file", [False, True])
def test_leeboard_store_git_is_denied(home, tmp_path, git_file):
    store = home / ".leeboard"
    store.mkdir()
    git = store / ".git"
    if git_file:
        git.write_text("gitdir: elsewhere\n")
    else:
        git.mkdir()
    fs = ProcessIsolation().settings(_repo(tmp_path / "repo"), tmp_path / "run")["filesystem"]
    assert str(store) in fs["allowWrite"]
    assert str(git) in fs["denyWrite"]


def test_leeboard_store_without_git_adds_no_git_denial(home, tmp_path):
    store = home / ".leeboard"
    store.mkdir()
    fs = ProcessIsolation().settings(_repo(tmp_path / "repo"), tmp_path / "run")["filesystem"]
    assert str(store) in fs["allowWrite"]
    assert str(store / ".git") not in fs["denyWrite"]
    assert not (store / ".git").exists()


def test_leeboard_missing_store_is_not_created(home, tmp_path):
    store = home / ".leeboard"
    fs = ProcessIsolation().settings(_repo(tmp_path / "repo"), tmp_path / "run")["filesystem"]
    assert str(store) not in fs["allowWrite"]
    assert not store.exists()


def test_leeboard_store_file_is_not_a_write_root(home, tmp_path):
    store = home / ".leeboard"
    store.touch()
    fs = ProcessIsolation().settings(_repo(tmp_path / "repo"), tmp_path / "run")["filesystem"]
    assert str(store) not in fs["allowWrite"]
    assert store.is_file()


def _non_session_settings(mode: str, isolation: ProcessIsolation, repo: Path, run: Path):
    if mode == "setup":
        return isolation.setup_settings(repo, run, (), ())
    return isolation.settings(repo, run, command=mode == "command")


@pytest.mark.parametrize("mode", ["command", "readonly", "setup"])
def test_leeboard_non_session_settings_get_no_store_grant(home, tmp_path, mode):
    store = home / ".leeboard"
    (store / ".git").mkdir(parents=True)
    repo = _repo(tmp_path / "repo")
    isolation = ProcessIsolation(readonly=mode == "readonly")
    fs = _non_session_settings(mode, isolation, repo, tmp_path / "run")["filesystem"]
    assert str(store) not in fs["allowWrite"]
    assert str(store / ".git") not in fs["denyWrite"]


def _lb_install(package: Path, tmp_path: Path, monkeypatch) -> Path:
    (package / "dist").mkdir(parents=True)
    (package / "node_modules").mkdir()
    (package / "package.json").write_text("{}")
    executable = package / "dist" / "cli.js"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    bin_dir = tmp_path / "tool-bin"
    bin_dir.mkdir()
    (bin_dir / "lb").symlink_to(executable)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return executable


def test_leeboard_hidden_install_root_is_readable(home, tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    repo = _repo(projects / "repo")
    sibling = projects / "sibling"
    package = sibling / "leeboard"
    _lb_install(package, tmp_path, monkeypatch)
    unrelated = projects / "unrelated"
    unrelated.mkdir()
    fs = ProcessIsolation({"isolation": {"projects_root": str(projects)}}).settings(
        repo, tmp_path / "run"
    )["filesystem"]
    assert fs["allowRead"] == [str(package)]
    assert str(sibling) in fs["denyRead"]
    assert str(unrelated) in fs["denyRead"]
    assert str(sibling) not in fs["allowWrite"]
    assert str(package) not in fs["allowWrite"]
    assert str(unrelated) not in fs["allowWrite"]


def test_leeboard_readable_install_needs_no_carveout(home, tmp_path, monkeypatch):
    package = tmp_path / "leeboard"
    _lb_install(package, tmp_path, monkeypatch)
    repo = _repo(tmp_path / "projects" / "repo")
    fs = ProcessIsolation({"isolation": {"projects_root": str(repo.parent)}}).settings(
        repo, tmp_path / "run"
    )["filesystem"]
    assert fs["allowRead"] == []


@pytest.mark.parametrize("denial", ["builtin", "global", "project", "data"])
def test_leeboard_fixed_denial_wins(home, tmp_path, monkeypatch, denial):
    projects = home if denial == "builtin" else tmp_path / "projects"
    repo = _repo(projects / "repo")
    denied = projects / (".kube" if denial == "builtin" else "private")
    package = denied / "leeboard"
    _lb_install(package, tmp_path, monkeypatch)
    config = {"isolation": {"projects_root": str(projects)}}
    if denial == "global":
        config["isolation"]["deny_read"] = [str(denied)]
    isolation = ProcessIsolation(
        config,
        deny_read=(str(denied),) if denial == "project" else (),
        data_dir=denied if denial == "data" else None,
    )
    fs = isolation.settings(repo, tmp_path / "run")["filesystem"]
    assert str(denied) in fs["denyRead"]
    assert fs["allowRead"] == []


@pytest.mark.parametrize("mode", ["command", "readonly", "setup"])
def test_leeboard_non_session_settings_get_no_install_carveout(home, tmp_path, monkeypatch, mode):
    projects = tmp_path / "projects"
    repo = _repo(projects / "repo")
    package = projects / "leeboard"
    _lb_install(package, tmp_path, monkeypatch)
    isolation = ProcessIsolation(
        {"isolation": {"projects_root": str(projects)}}, readonly=mode == "readonly"
    )
    fs = _non_session_settings(mode, isolation, repo, tmp_path / "run")["filesystem"]
    assert str(package) in fs["denyRead"]
    assert fs["allowRead"] == []


def test_leeboard_missing_install_adds_no_carveout(home, tmp_path, monkeypatch):
    monkeypatch.setattr(leeboard, "find_lb", lambda: None)
    fs = ProcessIsolation().settings(_repo(tmp_path / "repo"), tmp_path / "run")["filesystem"]
    assert fs["allowRead"] == []


@needs_real_srt
def test_leeboard_real_srt_runs_hidden_lb_and_protects_store_git(home, tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    repo = _repo(projects / "repo")
    sibling = projects / "sibling"
    package = sibling / "leeboard"
    executable = _lb_install(package, tmp_path, monkeypatch)
    executable.write_text(
        "#!/bin/sh\nset -eu\n"
        'printf "%s\\n%s\\n" "$LEEBOARD_HARNESS" "$LEEBOARD_SESSION" > "$LEEBOARD_HOME/entry"\n'
        'if (echo planted > "$LEEBOARD_HOME/.git/x") 2>/dev/null; then\n'
        '  echo "unexpected git write" >&2\n  exit 1\nfi\n'
        "echo leeboard-sandbox-ok\n"
    )
    # The store must not inherit /tmp's write grant. Always remove this host-side
    # temporary directory, including when an assertion or srt invocation fails.
    with TemporaryDirectory(prefix=".helmraiser-leeboard-test-", dir=REAL_HOME) as store_dir:
        store = Path(store_dir).resolve()
        (store / ".git").mkdir()
        monkeypatch.setenv("LEEBOARD_HOME", str(store))
        run_dir = tmp_path / "data" / "runs" / "r"
        isolation = ProcessIsolation({"isolation": {"projects_root": str(projects)}})
        argv = isolation.wrap(["lb"], repo, run_dir)
        assert REAL_SRT is not None
        index = argv.index("--settings")
        argv[index - 1] = REAL_SRT
        settings_path = Path(argv[index + 1])
        settings = json.loads(settings_path.read_text())
        fs = settings["filesystem"]
        assert str(sibling) in fs["denyRead"]
        assert fs["allowRead"] == [str(package)]
        assert str(store) in fs["allowWrite"]
        existing_roots = [root for root in fs["allowWrite"] if root != str(store)]
        assert all(not store.is_relative_to(Path(root).resolve()) for root in existing_roots)

        # Change only the store grant: the same lb invocation must fail without it.
        fs["allowWrite"] = existing_roots
        no_grant_path = run_dir / "srt-settings-no-leeboard-grant.json"
        no_grant_path.write_text(json.dumps(settings))
        run_id = "3f2c1a9e-6b7d-4c1e-9a2b-1f0e8d7c6b5a"
        for granted, path in ((False, no_grant_path), (True, settings_path)):
            command = list(argv)
            command[index + 1] = str(path)
            proc = subprocess.run(
                command,
                cwd=repo,
                env=session_environment(leeboard.leeboard_identity(run_id)),
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=120,
            )
            if not granted:
                assert proc.returncode != 0, proc.stdout + proc.stderr
                assert "leeboard-sandbox-ok" not in proc.stdout
                assert not (store / "entry").exists()
            else:
                assert proc.returncode == 0, proc.stdout + proc.stderr
                assert "leeboard-sandbox-ok" in proc.stdout
                assert (store / "entry").read_text() == f"helmraiser\n{run_id}\n"
            assert not (store / ".git" / "x").exists()
