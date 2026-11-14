"""Isolation seam (spec §7): settings shape, availability reasons, level mapping."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from helmraiser.isolation import NoIsolation, ProcessIsolation, VmIsolation, for_level
from helmraiser.isolation import process as process_module
from helmraiser.isolation.environment import session_environment
from helmraiser.isolation.process import (
    DEFAULT_ALLOWED_DOMAINS,
    DENY_READ,
    SETTINGS_FILENAME,
    SETUP_SETTINGS_FILENAME,
    tracked_denial_warnings,
    tracked_denials,
)
from helmraiser.loop.evidence import run_evidence
from helmraiser.loop.verify import run_verify
from helmraiser.task.loader import parse_task
from helmraiser.task.model import EvidenceStep, VerifyStep
from helmraiser.task.schema import validate_task

WORKTREE = Path("/repo/.helmraiser/worktrees/worktree-t-r")
RUN_DIR = Path("/home/u/.helmraiser/runs/r")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_lb) -> Path:
    """``~`` is a temp dir, so the settings never reads the real ``~/.pi/agent``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LOGBOOK_HOME", raising=False)
    return home


@pytest.fixture
def slash_tmp() -> Iterator[Path]:
    """A directory under /tmp, whatever pytest's TMPDIR is: these tests cover a git
    common directory inside /tmp, which drops the broad /tmp write root."""
    path = Path(tempfile.mkdtemp(prefix="helmraiser-test-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _builtin_denies(home: Path) -> list[str]:
    return [str(home / p.removeprefix("~/")) if p.startswith("~/") else p for p in DENY_READ]


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(
        path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
    )
    return path


def _unix_sockets_available() -> bool:
    try:
        sock = socket.socket(socket.AF_UNIX)
    except OSError:
        return False
    sock.close()
    return True


class TestTmpdirConfig:
    def test_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(process_module, "DEFAULT_SRT_TMPDIR", Path("/tmp/claude"))
        assert ProcessIsolation({}).tmpdir == Path("/tmp/claude")

    def test_custom_path_is_not_created(self, tmp_path: Path):
        tmpdir = tmp_path / "disk" / "temp"
        assert ProcessIsolation({"isolation": {"tmpdir": str(tmpdir)}}).tmpdir == tmpdir
        assert not tmpdir.exists()

    def test_expands_home(self, home: Path):
        iso = ProcessIsolation({"isolation": {"tmpdir": "~/sandbox-temp"}})
        assert iso.tmpdir == home / "sandbox-temp"

    @pytest.mark.parametrize("value", ["", "relative/dir", "."])
    def test_rejects_relative_path(self, value: str):
        with pytest.raises(ValueError, match="isolation.tmpdir.*absolute"):
            ProcessIsolation({"isolation": {"tmpdir": value}})


def _settings_kind(iso: ProcessIsolation, kind: str, worktree: Path = WORKTREE, write=()):
    if kind == "setup":
        return iso.setup_settings(worktree, RUN_DIR, write, ())
    return iso.settings(worktree, RUN_DIR, command=kind == "command")


@pytest.mark.parametrize("kind", ["session", "readonly", "command", "setup"])
class TestTmpdirSettings:
    @pytest.mark.parametrize("path", ["/disk/helmraiser-temp", "/tmp/custom-helmraiser-temp"])
    def test_custom_root(self, home: Path, kind: str, path: str):
        tmpdir = Path(path)
        iso = ProcessIsolation({"isolation": {"tmpdir": str(tmpdir)}}, readonly=kind == "readonly")
        roots = _settings_kind(iso, kind)["filesystem"]["allowWrite"]
        assert roots.count(str(tmpdir)) == 1
        if kind == "readonly":
            assert roots == [str(RUN_DIR / "sessions"), str(tmpdir)]

    def test_default_is_covered_by_tmp(
        self, home: Path, kind: str, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(process_module, "DEFAULT_SRT_TMPDIR", Path("/tmp/claude"))
        roots = _settings_kind(ProcessIsolation({}, readonly=kind == "readonly"), kind)[
            "filesystem"
        ]["allowWrite"]
        if kind == "readonly":
            assert roots == [str(RUN_DIR / "sessions"), "/tmp/claude"]
        else:
            assert "/tmp" in roots and "/tmp/claude" not in roots

    @pytest.mark.parametrize("equal", [False, True])
    def test_rejects_git_common_dir(self, home: Path, tmp_path: Path, kind: str, equal: bool):
        repo = _repo(tmp_path / "repo")
        common = repo / ".git"
        tmpdir = common if equal else repo
        iso = ProcessIsolation({"isolation": {"tmpdir": str(tmpdir)}}, readonly=kind == "readonly")
        with pytest.raises(RuntimeError) as exc:
            _settings_kind(iso, kind, repo)
        assert str(tmpdir) in str(exc.value) and str(common) in str(exc.value)

    def test_covered_root_is_not_repeated(self, home: Path, kind: str):
        root = RUN_DIR / "sessions" if kind == "readonly" else WORKTREE
        tmpdir = root / "temp"
        iso = ProcessIsolation({"isolation": {"tmpdir": str(tmpdir)}}, readonly=kind == "readonly")
        roots = _settings_kind(iso, kind)["filesystem"]["allowWrite"]
        assert str(root) in roots and str(tmpdir) not in roots


def test_setup_declared_root_covers_tmpdir(home: Path):
    root = Path("/disk/cache")
    iso = ProcessIsolation({"isolation": {"tmpdir": str(root / "temp")}})
    roots = _settings_kind(iso, "setup", write=(root,))["filesystem"]["allowWrite"]
    assert str(root) in roots and str(iso.tmpdir) not in roots


class TestSettings:
    def test_default_settings_shape(self, home: Path):
        settings = ProcessIsolation({}).settings(WORKTREE, RUN_DIR)
        assert settings == {
            "filesystem": {
                "allowRead": [],
                "allowWrite": [str(WORKTREE), "/tmp", str(RUN_DIR / "sessions")],
                "denyRead": _builtin_denies(home),
                "denyWrite": [],
            },
            "network": {"allowedDomains": list(DEFAULT_ALLOWED_DOMAINS), "deniedDomains": []},
        }
        assert str(home / ".ssh") in settings["filesystem"]["denyRead"]
        assert "api.openai.com" in settings["network"]["allowedDomains"]

    def test_project_read_denials_cover_primary_and_worktree(
        self, home: Path, tmp_path: Path
    ) -> None:
        primary = tmp_path / "repo"
        worktree = tmp_path / "worktree"
        iso = ProcessIsolation({}, deny_read=("secrets", "~/private"), primary_checkout=primary)

        deny_read = iso.settings(worktree, RUN_DIR)["filesystem"]["denyRead"]

        assert deny_read == [
            *_builtin_denies(home),
            str(primary / "secrets"),
            str(worktree / "secrets"),
            str(home / "private"),
        ]

    def test_paths_are_absolute(self, home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        iso = ProcessIsolation({"isolation": {"projects_root": "~/projects"}})
        fs = iso.settings(Path("wt"), Path("runs/r"))["filesystem"]
        assert fs["allowWrite"][:3] == [
            str(tmp_path / "wt"),
            "/tmp",
            str(tmp_path / "runs/r/sessions"),
        ]
        for key in ("allowRead", "allowWrite", "denyRead", "denyWrite"):
            assert all(p.startswith("/") and "~" not in p for p in fs[key]), key

    def test_config_overrides_domains(self, home: Path):
        iso = ProcessIsolation({"isolation": {"allowed_domains": ["example.com"]}})
        settings = iso.settings(WORKTREE, RUN_DIR)
        assert settings["network"]["allowedDomains"] == ["example.com"]

    def test_credential_locations_are_denied_whether_or_not_they_exist(self, home: Path):
        (home / ".claude").mkdir()
        deny_read = ProcessIsolation({}).settings(WORKTREE, RUN_DIR)["filesystem"]["denyRead"]
        for path in (".claude", ".codex", ".config/gh", ".helmraiser", ".pi/agent/sessions"):
            assert str(home / path) in deny_read, path
        for path in ("/srv/data/private", "/srv/data/backups", "/srv/data/documents"):
            assert path not in deny_read
        assert str(home / ".someapp") not in deny_read
        assert str(home / ".pi" / "agent") not in deny_read

    @pytest.mark.parametrize("kind", ["session", "readonly", "command", "setup"])
    @pytest.mark.parametrize("exists", [False, True])
    def test_secrets_are_denied_whether_or_not_they_exist(
        self, home: Path, kind: str, exists: bool
    ):
        secrets = home / ".secrets"
        if exists:
            secrets.mkdir()
        assert secrets.exists() == exists
        iso = ProcessIsolation({"isolation": {"deny_read": []}}, readonly=kind == "readonly")
        deny_read = _settings_kind(iso, kind)["filesystem"]["denyRead"]
        assert str(secrets) in deny_read
        assert str(home / ".ssh") in deny_read

    def test_global_deny_read_is_appended_before_the_project_list(self, home: Path, tmp_path: Path):
        primary = tmp_path / "repo"
        worktree = tmp_path / "wt"
        iso = ProcessIsolation(
            {"isolation": {"deny_read": ["~/vault", "/srv/keys", ".env"]}},
            deny_read=("secrets",),
            primary_checkout=primary,
        )
        deny_read = iso.settings(worktree, RUN_DIR)["filesystem"]["denyRead"]
        assert deny_read[len(DENY_READ) :] == [
            str(home / "vault"),
            "/srv/keys",
            str(primary / ".env"),
            str(worktree / ".env"),
            str(primary / "secrets"),
            str(worktree / "secrets"),
        ]
        assert iso.configured_deny_read() == ("~/vault", "/srv/keys", ".env", "secrets")

    def test_other_projects_are_denied_the_own_project_stays_readable(
        self, home: Path, tmp_path: Path
    ):
        personal = tmp_path / "projects" / "personal"
        work = tmp_path / "projects" / "work"
        repo = _repo(personal / "app" / "monorepo")
        (personal / "app" / "notes").mkdir()
        (personal / "other").mkdir()
        (personal / "file.md").write_text("x")
        (work / "client").mkdir(parents=True)
        worktree = repo / ".helmraiser" / "worktrees" / "wt"
        _git(repo, "worktree", "add", "-q", "-b", "sq/wt", str(worktree))
        iso = ProcessIsolation(
            {"isolation": {"projects_root": [str(personal), str(work)]}},
            primary_checkout=repo,
        )

        fs = iso.settings(worktree, RUN_DIR)["filesystem"]

        others = [p for p in fs["denyRead"] if p.startswith(str(tmp_path / "projects"))]
        assert others == [str(personal / "other"), str(work / "client")]
        assert fs["allowWrite"][0] == str(worktree)
        assert str((repo / ".git" / "objects").resolve()) in fs["allowWrite"]

    def test_projects_root_may_be_one_string(self, home: Path, tmp_path: Path):
        root = tmp_path / "projects"
        repo = _repo(root / "repo")
        (root / "sibling").mkdir()
        iso = ProcessIsolation({"isolation": {"projects_root": str(root)}}, primary_checkout=repo)
        deny_read = iso.settings(repo / "wt", RUN_DIR)["filesystem"]["denyRead"]
        assert str(root / "sibling") in deny_read and str(repo) not in deny_read

    def test_a_worktree_outside_the_repository_keeps_its_directory(
        self, home: Path, tmp_path: Path
    ):
        root = tmp_path / "projects"
        worktrees = tmp_path / "worktrees"
        repo = _repo(root / "repo")
        (worktrees / "other-project").mkdir(parents=True)
        worktree = worktrees / "repo" / "wt"
        _git(repo, "worktree", "add", "-q", "-b", "sq/wt", str(worktree))
        iso = ProcessIsolation(
            {"isolation": {"projects_root": [str(root), str(worktrees)]}}, primary_checkout=repo
        )
        deny_read = iso.settings(worktree, RUN_DIR)["filesystem"]["denyRead"]
        assert str(worktrees / "other-project") in deny_read
        assert not [p for p in deny_read if p.startswith((str(repo), str(worktrees / "repo")))]

    def test_the_data_dir_is_denied_around_the_run_dir(self, home: Path, tmp_path: Path):
        data = home / ".helmraiser"
        run_dir = data / "runs" / "r"
        (run_dir / "sessions").mkdir(parents=True)
        (data / "runs" / "older").mkdir()
        (data / "helmraiser.db").write_text("")
        (data / "config.json").write_text("{}")

        deny_read = ProcessIsolation({}).settings(WORKTREE, run_dir)["filesystem"]["denyRead"]

        assert str(data) not in deny_read and str(run_dir) not in deny_read
        assert [p for p in deny_read if p.startswith(str(data))] == [
            str(data / "config.json"),
            str(data / "helmraiser.db"),
            str(data / "runs" / "older"),
        ]

    def test_a_data_dir_elsewhere_is_denied_too(self, home: Path, tmp_path: Path):
        run_dir = tmp_path / "data" / "runs" / "r"
        run_dir.mkdir(parents=True)
        (tmp_path / "data" / "helmraiser.db").write_text("")
        deny_read = ProcessIsolation({}).settings(WORKTREE, run_dir)["filesystem"]["denyRead"]
        assert str(tmp_path / "data" / "helmraiser.db") in deny_read

    def test_what_pi_loads_from_other_projects_stays_readable(self, home: Path, tmp_path: Path):
        root = tmp_path / "projects"
        repo = _repo(root / "repo")
        (root / "pkg").mkdir(parents=True)
        (root / "abs-pkg").mkdir()
        (root / "hub" / "skills" / "tdd").mkdir(parents=True)
        (root / "hub" / "AGENTS.md").write_text("x")
        (repo / "own-skill").mkdir()
        (home / ".claude" / "skills" / "c").mkdir(parents=True)
        agent = home / ".pi" / "agent"
        (agent / "skills").mkdir(parents=True)
        packages = ["npm:@x/y@1", f"../../{os.path.relpath(root / 'pkg', home)}"]
        packages.append({"source": str(root / "abs-pkg")})
        (agent / "settings.json").write_text(
            json.dumps({"packages": packages, "skills": ["!**/x", "https://e/x"]})
        )
        (agent / "AGENTS.md").symlink_to(root / "hub" / "AGENTS.md")
        (agent / "skills" / "tdd").symlink_to(root / "hub" / "skills" / "tdd")
        (agent / "skills" / "own").symlink_to(repo / "own-skill")
        (agent / "skills" / "creds").symlink_to(home / ".claude" / "skills" / "c")
        (agent / "skills" / "gone").symlink_to(root / "missing")
        iso = ProcessIsolation({"isolation": {"projects_root": str(root)}}, primary_checkout=repo)

        fs = iso.settings(repo / "wt", RUN_DIR)["filesystem"]

        assert sorted(fs["allowRead"]) == sorted(
            [
                str(root / "pkg"),
                str(root / "abs-pkg"),
                str(root / "hub" / "AGENTS.md"),
                str(root / "hub" / "skills" / "tdd"),
            ]
        )
        assert str(root / "hub") in fs["denyRead"] and str(root / "pkg") in fs["denyRead"]

    def test_only_the_run_git_directories_are_writable(self, home: Path, slash_tmp: Path):
        repo = _repo(slash_tmp / "repo")
        worktree = slash_tmp / "wt"
        _git(repo, "worktree", "add", "-q", "-b", "feature/topic/run", str(worktree))

        fs = ProcessIsolation({}).settings(worktree, RUN_DIR)["filesystem"]
        common = (repo / ".git").resolve()
        assert fs["allowWrite"] == [
            str(worktree),
            str(RUN_DIR / "sessions"),
            str(common / "objects"),
            str(common / "worktrees" / "wt"),
            str(common / "refs" / "heads" / "feature" / "topic"),
            str(common / "logs" / "refs" / "heads" / "feature" / "topic"),
            str(process_module.DEFAULT_SRT_TMPDIR),
        ]
        assert fs["denyWrite"] == [str(common / "worktrees" / "wt" / "config.worktree")]

    def test_primary_git_paths_are_not_covered_by_any_write_root(self, home: Path, tmp_path: Path):
        repo = _repo(tmp_path / "repo")
        worktree = tmp_path / "wt"
        _git(repo, "worktree", "add", "-q", "-b", "feature/topic/run", str(worktree))
        common = repo / ".git"

        fs = ProcessIsolation({}).settings(worktree, RUN_DIR)["filesystem"]

        roots = [Path(root) for root in fs["allowWrite"]]
        for relative in (
            "refs/heads/main",
            "refs/heads/other",
            "refs/tags/v1",
            "packed-refs",
            "HEAD",
            "logs/HEAD",
            "logs/refs/heads/main",
            "hooks/pre-commit",
            "config",
        ):
            target = common / relative
            assert not any(root == target or root in target.parents for root in roots), target

    @pytest.mark.parametrize("branch", ["main", "feature/topic/run", None])
    def test_primary_checkout_gets_no_git_write_roots(
        self, home: Path, slash_tmp: Path, branch: str | None
    ):
        repo = _repo(slash_tmp / "repo")
        if branch is None:
            _git(repo, "checkout", "--detach")
        elif branch != "main":
            _git(repo, "checkout", "-qb", branch)

        fs = ProcessIsolation({}).settings(repo, RUN_DIR)["filesystem"]

        assert fs["allowWrite"] == [
            str(repo),
            str(RUN_DIR / "sessions"),
            str(process_module.DEFAULT_SRT_TMPDIR),
        ]
        assert fs["denyWrite"] == []
        assert not (repo / ".git" / "config.worktree").exists()

    def test_missing_branch_and_reflog_directories_are_created(self, home: Path, tmp_path: Path):
        repo = _repo(tmp_path / "repo")
        worktree = tmp_path / "wt"
        _git(repo, "worktree", "add", "-q", "-b", "feature/topic/run", str(worktree))
        _git(repo, "pack-refs", "--all", "--prune")
        refs = repo / ".git" / "refs" / "heads" / "feature" / "topic"
        assert not refs.exists()
        logs = repo / ".git" / "logs" / "refs" / "heads" / "feature" / "topic"
        shutil.rmtree(logs)

        fs = ProcessIsolation({}).settings(worktree, RUN_DIR)["filesystem"]

        for directory in (refs, logs):
            assert directory.is_dir()
            assert str(directory) in fs["allowWrite"]

    def test_detached_worktree_only_writes_objects_and_its_admin_dir(
        self, home: Path, slash_tmp: Path
    ):
        repo = _repo(slash_tmp / "repo")
        worktree = slash_tmp / "wt"
        _git(repo, "worktree", "add", "-q", "--detach", str(worktree))

        fs = ProcessIsolation({}).settings(worktree, RUN_DIR)["filesystem"]

        assert fs["allowWrite"][2:] == [
            str(repo / ".git" / "objects"),
            str(repo / ".git" / "worktrees" / "wt"),
            str(process_module.DEFAULT_SRT_TMPDIR),
        ]

    def test_branch_without_directory_is_rejected(self, home: Path, tmp_path: Path):
        repo = _repo(tmp_path / "repo")
        worktree = tmp_path / "wt"
        _git(repo, "worktree", "add", "-q", "-b", "run", str(worktree))

        with pytest.raises(
            RuntimeError, match="sandbox branch must have a directory.*refs/heads/run"
        ):
            ProcessIsolation({}).settings(worktree, RUN_DIR)

    def test_worktree_config_is_created_and_denied_without_overwriting_it(
        self, home: Path, tmp_path: Path
    ):
        repo = _repo(tmp_path / "repo")
        worktree = tmp_path / "wt"
        _git(repo, "worktree", "add", "-q", "-b", "feature/topic/run", str(worktree))
        config = repo / ".git" / "worktrees" / "wt" / "config.worktree"
        assert not config.exists()

        iso = ProcessIsolation({})
        fs = iso.settings(worktree, RUN_DIR)["filesystem"]
        assert config.read_text() == ""
        assert str(config) in fs["denyWrite"]
        config.write_text("[core]\n    autocrlf = false\n")
        _git(repo, "config", "extensions.worktreeConfig", "true")
        assert iso.settings(worktree, RUN_DIR)["filesystem"] == fs
        assert config.read_text() == "[core]\n    autocrlf = false\n"

    def test_pi_agent_dir_is_writable_for_auth_and_locks_only(self, home: Path):
        agent = home / ".pi" / "agent"
        (agent / "skills").mkdir(parents=True)
        (agent / "sessions").mkdir()
        (agent / "auth.json.lock").mkdir()  # stale lock from a killed session
        for name in ("auth.json", "settings.json", "models-store.json"):
            (agent / name).write_text("{}")

        fs = ProcessIsolation({}).settings(WORKTREE, RUN_DIR)["filesystem"]
        assert fs["allowWrite"][-1] == str(agent)
        assert fs["denyWrite"] == [
            str(agent / name)
            for name in ("models-store.json", "sessions", "settings.json", "skills")
        ]

    def test_write_settings_lands_in_run_dir(self, home: Path, tmp_path: Path):
        run_dir = tmp_path / "runs" / "r"
        path = ProcessIsolation({}).write_settings(tmp_path / "wt", run_dir)
        assert path == run_dir / "srt-settings.json"
        data = json.loads(path.read_text())
        assert data["filesystem"]["allowWrite"][0] == str(tmp_path / "wt")
        assert data["filesystem"]["allowWrite"][2] == str(run_dir / "sessions")
        assert str(run_dir) not in data["filesystem"]["allowWrite"]


class TestSetupSettings:
    def test_shape_and_wrap(self, home: Path, tmp_path: Path):
        iso = ProcessIsolation({"isolation": {"allowed_domains": ["api.openai.com"]}})
        caches = (home / ".npm", Path("/srv/cache/.pnpm-store"), home / ".npm")
        setup = iso.setup_settings(WORKTREE, RUN_DIR, caches, ("registry.npmjs.org",))
        session = iso.settings(WORKTREE, RUN_DIR)
        assert setup == {
            "filesystem": {
                "allowRead": [],
                "allowWrite": [
                    str(WORKTREE),
                    "/tmp",
                    str(home / ".npm"),
                    "/srv/cache/.pnpm-store",
                ],
                "denyRead": session["filesystem"]["denyRead"],
                "denyWrite": [],
            },
            "network": {
                "allowedDomains": ["registry.npmjs.org"],
                "deniedDomains": [],
            },
        }
        serialized = json.dumps(setup)
        assert "allowAllUnixSockets" not in serialized
        assert "sessions" not in setup["filesystem"]["allowWrite"]
        assert "evidence" not in setup["filesystem"]["allowWrite"]
        assert str(home / ".pi" / "agent") not in setup["filesystem"]["allowWrite"]

        run_dir = tmp_path / "runs" / "r"
        argv = ["/bin/bash", "-c", "npm install"]
        wrapped = iso.wrap_setup(argv, tmp_path / "wt", run_dir, caches, ("registry.npmjs.org",))
        assert wrapped == [
            shutil.which("env"),
            f"CLAUDE_CODE_TMPDIR={iso.tmpdir}",
            shutil.which("srt"),
            "--settings",
            str(run_dir / SETUP_SETTINGS_FILENAME),
            "--",
            *argv,
        ]
        assert json.loads((run_dir / SETUP_SETTINGS_FILENAME).read_text())["network"][
            "allowedDomains"
        ] == ["registry.npmjs.org"]


class TestCommandSettings:
    """Verify and evidence: the session's read denials; writes to the worktree, the run's
    evidence dir and /tmp only; the configured network."""

    def test_shape(self, home: Path):
        iso = ProcessIsolation({"isolation": {"allowed_domains": ["pypi.org"]}})
        session = iso.settings(WORKTREE, RUN_DIR)
        command = iso.settings(WORKTREE, RUN_DIR, command=True)
        assert command == {
            "filesystem": {
                "allowRead": [],
                "allowWrite": [str(WORKTREE), str(RUN_DIR / "evidence"), "/tmp"],
                "denyRead": session["filesystem"]["denyRead"],
                "denyWrite": [],
            },
            "network": {"allowedDomains": ["pypi.org"], "deniedDomains": []},
        }
        assert "allowAllUnixSockets" not in json.dumps(command)

    def test_service_ports_are_literal_loopback_entries_of_the_command_settings_only(
        self, home: Path, tmp_path: Path
    ):
        iso = ProcessIsolation({"isolation": {"allowed_domains": ["pypi.org"]}})
        command = iso.settings(WORKTREE, RUN_DIR, command=True, service_ports=(55432, 6333))
        assert command["network"] == {
            "allowedDomains": ["pypi.org", "127.0.0.1:55432", "127.0.0.1:6333"],
            "deniedDomains": [],
        }
        assert "allowAllUnixSockets" not in json.dumps(command)
        with pytest.raises(ValueError, match="command sandbox settings only"):
            iso.settings(WORKTREE, RUN_DIR, service_ports=(55432,))
        run_dir = tmp_path / "runs" / "r"
        argv = iso.wrap_command(["/bin/bash", "-c", "true"], WORKTREE, run_dir, "verify", (55432,))
        index = argv.index("--settings")
        assert argv[:index] == [
            shutil.which("env"),
            f"CLAUDE_CODE_TMPDIR={iso.tmpdir}",
            shutil.which("srt"),
        ]
        assert argv[index : index + 3] == [
            "--settings",
            str(run_dir / "srt-settings-verify.json"),
            "--",
        ]
        assert argv[index + 3 : index + 6] == ["/bin/bash", "-o", "pipefail"]
        assert "55432" in argv[-1]
        written = json.loads((run_dir / "srt-settings-verify.json").read_text())
        assert written["network"]["allowedDomains"][-1] == "127.0.0.1:55432"
        plain = iso.wrap_command(["/bin/bash", "-c", "true"], WORKTREE, run_dir, "evidence")
        assert plain[plain.index("--") + 1 :] == ["/bin/bash", "-c", "true"]

    def test_same_denials_but_no_pi_carve_outs_git_or_pi_agent_writes(
        self, home: Path, tmp_path: Path
    ):
        root = tmp_path / "projects"
        repo = _repo(root / "repo")
        worktree = repo / ".helmraiser" / "worktrees" / "wt"
        _git(repo, "worktree", "add", "-q", "-b", "sq/wt", str(worktree))
        _repo(root / "sibling")
        (root / "hub" / "skills" / "tdd").mkdir(parents=True)
        agent = home / ".pi" / "agent"
        (agent / "skills").mkdir(parents=True)
        (agent / "skills" / "tdd").symlink_to(root / "hub" / "skills" / "tdd")
        iso = ProcessIsolation(
            {"isolation": {"projects_root": str(root)}},
            deny_read=("secrets",),
            primary_checkout=repo,
        )
        run_dir = tmp_path / "data" / "runs" / "r"

        session = iso.settings(worktree, run_dir)["filesystem"]
        command = iso.settings(worktree, run_dir, command=True)["filesystem"]

        assert session["allowRead"] == [str(root / "hub" / "skills" / "tdd")]
        assert command["allowRead"] == []
        assert command["denyRead"] == session["denyRead"]
        for denied in (root / "sibling", home / ".ssh", repo / "secrets", worktree / "secrets"):
            assert str(denied) in command["denyRead"]
        assert str(tmp_path / "data" / "helmraiser.db") not in command["allowWrite"]
        assert command["allowWrite"] == [str(worktree), str(run_dir / "evidence"), "/tmp"]
        assert command["denyWrite"] == []

    def test_wrap_command_writes_one_settings_file_per_kind(self, home: Path, tmp_path: Path):
        run_dir = tmp_path / "runs" / "r"
        iso = ProcessIsolation({})
        argv = ["/bin/bash", "-o", "pipefail", "-c", "pytest -q"]

        verify = iso.wrap_command(argv, tmp_path / "wt", run_dir, "verify")
        evidence = iso.wrap_command(argv, tmp_path / "wt", run_dir, "evidence")

        assert verify == [
            shutil.which("env"),
            f"CLAUDE_CODE_TMPDIR={iso.tmpdir}",
            shutil.which("srt"),
            "--settings",
            str(run_dir / "srt-settings-verify.json"),
            "--",
            *argv,
        ]
        assert evidence[evidence.index("--settings") + 1] == str(
            run_dir / "srt-settings-evidence.json"
        )
        assert (run_dir / "evidence").is_dir()
        assert not (run_dir / SETTINGS_FILENAME).exists()
        data = json.loads((run_dir / "srt-settings-verify.json").read_text())
        assert data["filesystem"]["allowWrite"][1] == str(run_dir / "evidence")


class TestTrackedDenials:
    def test_relative_entries_matching_tracked_files(self, tmp_path: Path):
        repo = _repo(tmp_path / "repo")
        (repo / "config").mkdir()
        (repo / "config" / "app.env").write_text("x")
        (repo / "notes.env").write_text("x")
        (repo / "untracked.env").write_text("x")
        _git(repo, "add", "config/app.env", "notes.env")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c")

        found = tracked_denials(repo, ("config", "*.env", "missing", "/abs/config", "~/x"))

        assert found == {"config": ["config/app.env"], "*.env": ["config/app.env", "notes.env"]}
        warning, _ = tracked_denial_warnings(found)
        assert warning.startswith("sandbox deny_read 'config' matches 1 tracked file(s)")

    def test_outside_a_repository_nothing_is_found(self, tmp_path: Path):
        assert tracked_denials(tmp_path, ("secrets",)) == {}


class TestWrap:
    @pytest.mark.parametrize("launch", ["wrap", "wrap_setup", "wrap_command"])
    @pytest.mark.parametrize("existing", [False, True])
    def test_srt_tmpdir(
        self,
        home: Path,
        tmp_path: Path,
        launch: str,
        existing: bool,
    ):
        tmpdir = tmp_path / "nested directory" / "srt-tmp"
        marker = tmpdir / "keep"
        if existing:
            tmpdir.mkdir(parents=True)
            marker.write_text("unchanged")
            before = tmpdir.stat()

        iso = ProcessIsolation({"isolation": {"tmpdir": str(tmpdir)}})
        args = (["true"], tmp_path / "wt", tmp_path / "run")
        if launch == "wrap_setup":
            argv = iso.wrap_setup(*args, (), ())
        elif launch == "wrap_command":
            argv = iso.wrap_command(*args, "verify")
        else:
            argv = iso.wrap(*args)

        assert argv[:3] == [
            shutil.which("env"),
            f"CLAUDE_CODE_TMPDIR={tmpdir}",
            shutil.which("srt"),
        ]
        assert Path(argv[0]).is_absolute()
        assert tmpdir.is_dir()
        if existing:
            assert marker.read_text() == "unchanged"
            assert tmpdir.stat() == before

    @pytest.mark.parametrize("env_path", ["tools/env", None])
    def test_env_resolution(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_path):
        which = shutil.which
        monkeypatch.setattr(
            process_module.shutil, "which", lambda name: env_path if name == "env" else which(name)
        )
        monkeypatch.chdir(tmp_path)
        iso = ProcessIsolation({})
        if env_path is None:
            with pytest.raises(RuntimeError, match="env not found on PATH"):
                iso.wrap(["true"], tmp_path / "wt", tmp_path / "run")
        else:
            argv = iso.wrap(["true"], tmp_path / "wt", tmp_path / "run")
            assert argv[0] == str(tmp_path / env_path)

    def test_argv_shape(self, home: Path, tmp_path: Path):
        run_dir = tmp_path / "runs" / "r"
        argv = ["pi", "--mode", "json", "--", "the prompt"]
        iso = ProcessIsolation({})
        wrapped = iso.wrap(argv, tmp_path / "wt", run_dir)
        assert wrapped == [
            shutil.which("env"),
            f"CLAUDE_CODE_TMPDIR={iso.tmpdir}",
            shutil.which("srt"),
            "--settings",
            str(run_dir / SETTINGS_FILENAME),
            "--",
            *argv,
        ]
        assert (run_dir / "sessions").is_dir()

    def test_writes_the_settings_with_the_worktree_as_writable_root(
        self, home: Path, tmp_path: Path
    ):
        run_dir = tmp_path / "runs" / "r"
        ProcessIsolation({}).wrap(["pi"], tmp_path / "wt", run_dir)
        data = json.loads((run_dir / SETTINGS_FILENAME).read_text())
        assert data["filesystem"]["allowWrite"][0] == str(tmp_path / "wt")
        assert data["filesystem"]["allowWrite"][2] == str(run_dir / "sessions")
        assert str(run_dir) not in data["filesystem"]["allowWrite"]
        assert data["network"]["deniedDomains"] == [] and data["filesystem"]["denyWrite"] == []

    def test_default_path_keeps_real_srt_guard(self, tmp_path: Path):
        argv = ProcessIsolation({}).wrap_command(
            ["true"], tmp_path / "wt", tmp_path / "run", "verify"
        )
        srt = argv[argv.index("--settings") - 1]
        assert srt == shutil.which("srt")
        assert Path(srt).is_absolute()
        assert "must not start the real srt sandbox" in Path(srt).read_text()

    @pytest.mark.parametrize("launch", ["wrap", "wrap_setup", "wrap_command"])
    @pytest.mark.parametrize("valid", [True, False])
    def test_all_wrappers_resolve_configured_srt(self, tmp_path: Path, launch: str, valid: bool):
        custom = tmp_path / "custom-srt"
        if valid:
            custom.write_text("#!/bin/sh\nexit 0\n")
            custom.chmod(0o755)
        iso = ProcessIsolation({"isolation": {"srt": str(custom)}})
        args = (["true"], tmp_path / "wt", tmp_path / "run")
        extra = (
            ((), ()) if launch == "wrap_setup" else ("verify",) if launch == "wrap_command" else ()
        )
        if valid:
            wrapped = getattr(iso, launch)(*args, *extra)
            assert wrapped[wrapped.index("--settings") - 1] == str(custom)
        else:
            with pytest.raises(process_module.SrtNotFoundError):
                getattr(iso, launch)(*args, *extra)


class TestCheckAvailable:
    def test_missing_srt(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(process_module.shutil, "which", lambda name: None)
        import helmraiser.config as config

        reason = ProcessIsolation({}).check_available()
        assert reason is not None and reason.startswith("srt not found on PATH")
        assert "isolation.srt" in reason
        assert str(config.CONFIG_FILE) in reason

    def test_missing_bwrap_after_srt_ok(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            process_module.shutil, "which", lambda name: "/usr/bin/srt" if name == "srt" else None
        )
        monkeypatch.setattr(
            process_module.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "srt 0.1.0\n", ""),
        )
        assert ProcessIsolation({}).check_available() == "bwrap not found on PATH"

    def test_version_failure_is_the_reason(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(process_module.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            process_module.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "boom\n"),
        )
        assert ProcessIsolation({}).check_available() == "srt --version failed: boom"

    def test_missing_srt_linux_dependency(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            process_module.shutil, "which", lambda name: None if name == "socat" else f"/b/{name}"
        )
        monkeypatch.setattr(
            process_module.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "1.0\n", ""),
        )
        assert ProcessIsolation({}).check_available() == "socat not found on PATH (srt needs it)"

    def test_configured_path_is_checked_with_srt_label(self, tmp_path, monkeypatch):
        custom = tmp_path / "another-name"
        custom.write_text("#!/bin/sh\nexit 1\n")
        custom.chmod(0o755)
        calls = []

        def version(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "boom")

        monkeypatch.setattr(process_module.subprocess, "run", version)
        iso = ProcessIsolation({"isolation": {"srt": str(custom)}})
        assert iso.check_available() == "srt --version failed: boom"
        assert calls == [[str(custom), "--version"]]

    def test_bad_override_precedes_other_tools(self, tmp_path, monkeypatch):
        monkeypatch.setattr(process_module.shutil, "which", lambda _: pytest.fail("lookup ran"))
        iso = ProcessIsolation({"isolation": {"srt": str(tmp_path / "missing")}})
        assert "isolation.srt" in iso.check_available()

    def test_both_present(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(process_module.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            process_module.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "1.0\n", ""),
        )
        assert ProcessIsolation({}).check_available() is None


class TestLevels:
    def test_none_is_identity_and_available(self):
        iso = for_level("none")
        assert isinstance(iso, NoIsolation) and iso.level == "none"
        assert iso.check_available() is None
        argv = ["pi", "--mode", "json"]
        assert iso.wrap(argv, WORKTREE, RUN_DIR) == argv

    def test_vm_reports_not_implemented(self):
        iso = for_level("vm")
        assert isinstance(iso, VmIsolation)
        assert iso.check_available() == "vm isolation not implemented"

    def test_process_level(self):
        assert isinstance(for_level("process", {}), ProcessIsolation)

    def test_unknown_level(self):
        with pytest.raises(ValueError):
            for_level("container")


def test_auto_policy_with_none_isolation_is_rejected(tmp_project: Path):
    text = f"""---
id: t
title: T
project: {tmp_project}
status: queued
policy: auto
isolation: none
verify:
  - name: v
    run: 'true'
evidence:
  - name: e
    run: 'true'
    artifacts: [x]
    criteria: c
---
body
"""
    errors = validate_task(parse_task(text, tmp_project / "t.md", {}))
    assert "policy auto requires isolation process or vm" in errors


# Resolved at import, before the suite's guard stub shadows srt on PATH (tests/conftest.py)
# and before any fake pi is put first. ``pi --version`` makes no model call.
REAL_SRT = shutil.which("srt")
REAL_PI = shutil.which("pi")
# srt sets SANDBOX_RUNTIME inside its sandbox, where Helmraiser runs its own suite as a
# verify step: bwrap cannot nest there, so the real-srt tests would only fail.
IN_SANDBOX = bool(os.environ.get("SANDBOX_RUNTIME"))
needs_real_srt = pytest.mark.skipif(
    IN_SANDBOX
    or ProcessIsolation({}).check_available() is not None
    or not _unix_sockets_available(),
    reason=(
        "running inside an srt sandbox (SANDBOX_RUNTIME is set); srt cannot nest"
        if IN_SANDBOX
        else "srt/bwrap or Unix sockets unavailable"
    ),
)


@needs_real_srt
def test_real_srt_enforces_the_settings(home: Path, tmp_path: Path):
    """Runs the real srt with a run's environment: the worktree and the run's sessions
    and custom TMPDIR are writable, commits move the run branch and the own repository
    is readable;
    the primary's Git refs, HEAD, packed-refs and hooks, a project
    denial, a sibling project, a credential directory, the ledger and other runs are
    not; pi still starts. Every path is a temp dir (``home`` is a fake ``~``)."""
    projects = tmp_path / "projects"
    repo = _repo(projects / "repo")
    (repo / "README.md").write_text("own repo\n")
    worktree = repo / ".helmraiser" / "worktrees" / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "feature/topic/run", str(worktree))
    common = repo / ".git"
    main_before = (common / "refs" / "heads" / "main").read_text()
    head_before = (common / "HEAD").read_text()
    hook = common / "hooks" / "pre-commit"
    secret = repo / "secrets" / "x"
    secret.parent.mkdir()
    secret.write_text("private\n")
    sibling = projects / "sibling" / "notes.md"
    sibling.parent.mkdir()
    sibling.write_text("sibling-content\n")
    credential = home / ".claude" / "fake-credential.json"
    credential.parent.mkdir()
    credential.write_text("fake-credential\n")
    run_dir = tmp_path / "data" / "runs" / "r"
    (run_dir.parent / "older").mkdir(parents=True)
    (run_dir.parent / "older" / "events.jsonl").write_text("older-run\n")
    (tmp_path / "data" / "helmraiser.db").write_text("ledger-content\n")
    pi = f"{REAL_PI} --version; echo pi_rc=$?; " if REAL_PI else ""
    custom_tmpdir = tmp_path / "disk" / "sandbox-temp"
    script = (
        'echo "$TMPDIR"; echo temporary > "$TMPDIR/marker"; '
        f"echo ok > {worktree / 'ok.txt'}; "
        f"echo s > {run_dir / 'sessions' / 's.jsonl'}; "
        "git add ok.txt; "
        'git -c user.name=t -c user.email=t@t commit -qm sandbox; echo "commit_rc=$?"; '
        'git update-ref refs/heads/main HEAD; echo "update_ref_rc=$?"; '
        f'echo bad > {common / "refs/heads/main"}; echo "loose_ref_rc=$?"; '
        f'echo bad > {common / "packed-refs"}; echo "packed_refs_rc=$?"; '
        f'echo bad > {common / "HEAD"}; echo "head_rc=$?"; '
        f'echo bad > {common / "worktrees/wt/config.worktree"}; echo "worktree_config_rc=$?"; '
        f'echo planted > {hook}; echo "hook_rc=$?"; '
        f'cat {secret}; echo "read_rc=$?"; '
        f"cat {repo / 'README.md'}; "
        f'cat {sibling}; echo "sibling_rc=$?"; '
        f'cat {credential}; echo "credential_rc=$?"; '
        f"cat {tmp_path / 'data' / 'helmraiser.db'} {run_dir.parent / 'older' / 'events.jsonl'}; "
        f'echo "data_rc=$?"; {pi}'
    )

    isolation = ProcessIsolation(
        {"isolation": {"projects_root": str(projects), "tmpdir": str(custom_tmpdir)}},
        deny_read=("secrets",),
        primary_checkout=repo,
    )
    argv = isolation.wrap(["sh", "-c", script], worktree, run_dir)
    index = argv.index("--settings")
    assert argv[index - 1] == shutil.which("srt") and REAL_SRT is not None
    argv[index - 1] = REAL_SRT
    proc = subprocess.run(
        argv,
        cwd=worktree,
        env=session_environment({}),
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )

    assert proc.returncode == 0, proc.stderr
    assert str(custom_tmpdir) in proc.stdout.splitlines()
    assert (custom_tmpdir / "marker").read_text() == "temporary\n"
    assert (worktree / "ok.txt").read_text() == "ok\n"
    assert (run_dir / "sessions" / "s.jsonl").read_text() == "s\n"
    assert "commit_rc=0" in proc.stdout, proc.stdout + proc.stderr
    for label in ("update_ref", "loose_ref", "packed_refs", "head", "worktree_config"):
        assert f"{label}_rc=0" not in proc.stdout, proc.stdout + proc.stderr
        assert f"{label}_rc=" in proc.stdout
    assert (common / "refs/heads/main").read_text() == main_before
    assert (common / "HEAD").read_text() == head_before
    assert not (common / "packed-refs").exists()
    assert (common / "refs/heads/feature/topic/run").read_text() != main_before
    assert (common / "worktrees/wt/config.worktree").read_text() == ""
    assert "hook_rc=1" in proc.stdout
    assert "read_rc=1" in proc.stdout and "private" not in proc.stdout
    assert "own repo" in proc.stdout
    assert "sibling_rc=1" in proc.stdout and "sibling-content" not in proc.stdout
    assert "credential_rc=1" in proc.stdout and "fake-credential" not in proc.stdout
    assert "data_rc=1" in proc.stdout
    assert "ledger-content" not in proc.stdout and "older-run" not in proc.stdout
    if REAL_PI:
        assert "pi_rc=0" in proc.stdout, proc.stdout + proc.stderr
    assert not hook.exists()


@needs_real_srt
def test_real_srt_runs_verify_and_evidence_under_the_command_settings(home: Path, tmp_path: Path):
    """Verify and evidence steps through the real srt: a step that reads a sibling project
    or a credential directory fails, one that writes the worktree passes, evidence lands
    in the run's evidence dir, stdin is /dev/null and a Unix socket cannot be opened. A
    ``sandbox: host`` step runs outside. Every path is a temp dir (``home`` is a fake ``~``),
    so the negative write cases (run dir, git dir) are covered by ``TestCommandSettings``."""
    projects = tmp_path / "projects"
    repo = _repo(projects / "repo")
    worktree = repo / ".helmraiser" / "worktrees" / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "sq/wt", str(worktree))
    sibling = projects / "sibling" / "notes.md"
    sibling.parent.mkdir()
    sibling.write_text("sibling-content\n")
    credential = home / ".ssh" / "id_fake"
    credential.parent.mkdir()
    credential.write_text("fake-key\n")
    run_dir = tmp_path / "data" / "runs" / "r"
    evidence_dir = run_dir / "evidence"
    iso = ProcessIsolation({"isolation": {"projects_root": str(projects)}}, primary_checkout=repo)
    srt = REAL_SRT
    assert srt is not None

    def wrap_for(kind: str) -> Callable[[list[str]], list[str]]:
        def wrap(argv: list[str]) -> list[str]:
            wrapped = iso.wrap_command(argv, worktree, run_dir, kind)
            wrapped[wrapped.index("--settings") - 1] = srt
            return wrapped

        return wrap

    python = shlex.quote(sys.executable)
    steps = [
        VerifyStep("sibling", f"cat {sibling}"),
        VerifyStep("credential", f"cat {credential}"),
        VerifyStep("write", "echo ok > written.txt"),
        VerifyStep("stdin", 'test "$(readlink /proc/self/fd/0)" = /dev/null'),
        VerifyStep("socket", f"{python} -c 'import socket; socket.socket(socket.AF_UNIX)'"),
        VerifyStep("inside", 'test "$SANDBOX_RUNTIME" = 1'),
        VerifyStep("host", 'test -z "${SANDBOX_RUNTIME:-}"', sandbox="host"),
    ]
    results = {r.name: r for r in run_verify(steps, worktree, "r", 20, wrap=wrap_for("verify"))}
    proof = EvidenceStep(
        "proof", 'echo seen > "$HELMRAISER_EVIDENCE_DIR/proof.txt"', ("*.txt",), "c"
    )
    (evidence,) = run_evidence([proof], worktree, "r", evidence_dir, wrap=wrap_for("evidence"))

    assert not results["sibling"].passed and "sibling-content" not in results["sibling"].tail
    assert not results["credential"].passed and "fake-key" not in results["credential"].tail
    assert results["write"].passed and (worktree / "written.txt").read_text() == "ok\n"
    assert results["stdin"].passed, results["stdin"].tail
    assert not results["socket"].passed, results["socket"].tail
    assert results["inside"].passed and results["inside"].sandbox == "srt"
    assert results["host"].passed and results["host"].sandbox == "host"
    assert evidence.ok, evidence.tail
    assert (evidence_dir / "proof.txt").read_text() == "seen\n"
    assert (run_dir / "srt-settings-verify.json").is_file()
    assert (run_dir / "srt-settings-evidence.json").is_file()


@pytest.mark.parametrize("external_run", [False, True])
def test_setup_cache_read_exception_is_narrow(home: Path, tmp_path: Path, external_run: bool):
    data = tmp_path / "data"
    run = tmp_path / "external" if external_run else data / "runs" / "r"
    run.mkdir(parents=True)
    cache = data / "setup-cache" / "own"
    for directory in (
        cache / "npm",
        cache / "pip",
        data / "setup-cache" / "other",
        data / "runs" / "other",
    ):
        directory.mkdir(parents=True)
    ledger = data / "ledger.db"
    ledger.touch()
    baseline = ProcessIsolation({}, deny_read=("secrets",), primary_checkout=tmp_path / "repo")
    iso = ProcessIsolation(
        {}, deny_read=("secrets",), primary_checkout=tmp_path / "repo", data_dir=data
    )
    for command in (False, True):
        original = baseline.settings(WORKTREE, run, command=command)
        protected = iso.settings(WORKTREE, run, command=command)
        if external_run:
            # Only the missing real data-dir denial changes; environments and all
            # other settings stay as before. Normal layouts remain byte-identical.
            protected["filesystem"]["denyRead"].remove(str(data))
        assert json.dumps(protected) == json.dumps(original)
    # Freeze all ordinary settings after directory creation, before setup preparation.
    frozen = [
        iso.write_settings(WORKTREE, run, command_kind=kind).read_bytes()
        for kind in (None, "verify", "evidence")
    ]
    argv = iso.wrap_setup(
        ["true"], WORKTREE, run, (tmp_path / "declared", cache), (), setup_cache=cache
    )
    srt_settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
    fs = srt_settings["filesystem"]
    assert fs["allowRead"] == []
    assert fs["allowWrite"] == [str(WORKTREE), "/tmp", str(tmp_path / "declared"), str(cache)]
    assert not any(cache.is_relative_to(Path(p)) for p in fs["denyRead"])
    for denied in (
        data / "setup-cache" / "other",
        ledger,
        data / "runs" / "other",
        home / ".ssh",
        WORKTREE / "secrets",
    ):
        assert any(denied.is_relative_to(Path(p)) for p in fs["denyRead"])
    assert srt_settings["network"]["allowedDomains"] == []
    for kind, before in zip((None, "verify", "evidence"), frozen, strict=True):
        assert iso.write_settings(WORKTREE, run, command_kind=kind).read_bytes() == before
        fs = json.loads(before)["filesystem"]
        for denied in (cache, data / "setup-cache" / "other", ledger):
            assert any(denied.is_relative_to(Path(p)) for p in fs["denyRead"])
        assert not any(run.is_relative_to(Path(p)) for p in fs["denyRead"])
        assert str(cache) not in fs["allowWrite"]
