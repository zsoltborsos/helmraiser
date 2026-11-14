"""Shared pytest fixtures for Helmraiser tests.

Extracts and shares fixtures currently duplicated across test files.
"""

import copy
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

# Isolate test config from real ~/.helmraiser/
_TEST_CONFIG_DIR = Path(
    os.environ.get("HELMRAISER_CONFIG_DIR", str(Path.home() / ".helmraiser-test"))
)
os.environ["HELMRAISER_CONFIG_DIR"] = str(_TEST_CONFIG_DIR)


def configured_test_config() -> dict[str, Any]:
    """Explicit test choices, independent of model-free production defaults."""
    from helmraiser.config import DEFAULT_CONFIG

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["profiles"] = {
        role: {"model": model, "thinking": "xhigh" if role == "fallback" else "high"}
        for role, model in {
            "intake": "openai-codex/gpt-6-astra",
            "plan": "openai-codex/gpt-6.1-sol",
            "implement": "openai-codex/gpt-6.1-sol",
            "fallback": "openai-codex/gpt-6.1-sol",
            "review": "openai-codex/gpt-6-astra",
            "scout": "opencode-go/glm-5.3-flash",
            "judge": "openai-codex/gpt-6-astra",
        }.items()
    }
    config["routing"]["scout"]["model"] = "openai-codex/gpt-6.1-sol:high"
    return config


@pytest.fixture
def configured_config() -> dict[str, Any]:
    return configured_test_config()


@pytest.fixture(autouse=True)
def isolate_default_paths(tmp_path, monkeypatch):
    """Keep runtime defaults and import-time config paths inside this test."""
    import helmraiser.config as config
    from helmraiser.isolation import process

    process.reset_srt_cache()
    # Like the real default (/tmp/claude), the default sandbox tmpdir sits under /tmp,
    # wherever pytest's tmp_path is (verify sandboxes put TMPDIR on disk).
    srt_tmp_root = Path(tempfile.mkdtemp(prefix="helmraiser-test-srt-", dir="/tmp"))
    monkeypatch.setattr(process, "DEFAULT_SRT_TMPDIR", srt_tmp_root / "srt-tmp")
    config_dir = tmp_path / "helmraiser-config"
    monkeypatch.setenv("HELMRAISER_DATA_DIR", str(tmp_path / "helmraiser-data"))
    monkeypatch.setenv("HELMRAISER_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config, "CONFIG_FILE", config_dir / "config.json")
    try:
        yield
    finally:
        process.reset_srt_cache()
        shutil.rmtree(srt_tmp_root, ignore_errors=True)


@pytest.fixture(scope="session")
def _silent_notify_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    bin_dir = tmp_path_factory.mktemp("silent-notify-bin")
    stub = bin_dir / "notify-send"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    # Nor the real srt sandbox: inside a Helmraiser run it would nest in the run's own
    # sandbox and hang until the session times out. A task fixture that runs `work`
    # must say `isolation: none`; tests of process isolation bring their own fake srt.
    srt = bin_dir / "srt"
    srt.write_text(
        "#!/bin/sh\necho 'the test suite must not start the real srt sandbox;"
        " give the task fixture isolation: none' >&2\nexit 97\n"
    )
    srt.chmod(0o755)
    # Nor the real docker: project services would start real containers. Tests of
    # services install tests/mocks/fake_docker.py with the ``fake_docker`` fixture.
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\necho 'the test suite must not run the real docker;"
        " use the fake_docker fixture (tests/mocks/fake_docker.py)' >&2\nexit 97\n"
    )
    docker.chmod(0o755)
    # Nor the real ss: it would read the host's real ports.
    ss = bin_dir / "ss"
    ss.write_text(
        "#!/bin/sh\necho 'the test suite must not read the host ports with the real ss;"
        " put a fake ss on PATH' >&2\nexit 97\n"
    )
    ss.chmod(0o755)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\necho 'the test suite must not run the real gh;"
        " put a fake gh on PATH' >&2\nexit 97\n"
    )
    gh.chmod(0o755)
    return bin_dir


@pytest.fixture(autouse=True)
def silence_desktop_notifications(monkeypatch, _silent_notify_bin: Path):
    """No test may reach the real notify-send (it pops up on the desktop), srt, docker or gh.

    Tests that assert on notifications, wrap pi in srt or start services put their own
    stub earlier on PATH.
    """
    monkeypatch.setenv("PATH", f"{_silent_notify_bin}{os.pathsep}{os.environ['PATH']}")


@pytest.fixture(autouse=True)
def forbid_real_process_group_signals(monkeypatch):
    """A unit/integration test must never signal a host process group."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("real os.killpg is forbidden in the test suite")

    monkeypatch.setattr(os, "killpg", refuse)


@pytest.fixture(autouse=True)
def forbid_real_exec(monkeypatch):
    """A test must never replace the pytest process (the worker's re-exec), nor see
    the code snapshot markers of a worker that runs this suite as a verify step."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("real os.execv / os.execve is forbidden in the test suite")

    monkeypatch.setattr(os, "execv", refuse)
    monkeypatch.setattr(os, "execve", refuse)
    monkeypatch.delenv("HELMRAISER_CODE_SNAPSHOT", raising=False)
    monkeypatch.delenv("HELMRAISER_SOURCE_REPO", raising=False)


@pytest.fixture(autouse=True)
def no_live_subscription_fetches(monkeypatch):
    """No test may call the usage API or either Omarchy collector (no network).

    Tests for live usage stub these themselves, which overrides this fixture.
    """
    from helmraiser.loop import live_usage as lu

    monkeypatch.setattr(lu, "fetch_opencode_go", lambda *args, **kwargs: None)
    monkeypatch.setattr(lu, "read_codex", lambda *args, **kwargs: None)
    monkeypatch.setattr(lu, "read_opencode", lambda *args, **kwargs: None)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)


# ──────────────────────────────────────────────
# Git helpers
# ──────────────────────────────────────────────


def _git_init(repo_dir: Path) -> None:
    """Initialize a git repo with an initial commit."""
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repo_dir,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo_dir,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_dir,
        capture_output=True,
        check=True,
    )
    (repo_dir / "README.md").write_text("# Test Project\n")
    subprocess.run(
        ["git", "add", "."],
        cwd=repo_dir,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_dir,
        capture_output=True,
        check=True,
    )


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a project dir with .git initialized."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _git_init(project_dir)
    return project_dir


@pytest.fixture
def git_init():
    """Return the _git_init helper function."""
    return _git_init


# ──────────────────────────────────────────────
# Scripted pi on PATH (spec §8)
# ──────────────────────────────────────────────


FAKE_PI_SOURCE = Path(__file__).resolve().parent / "mocks" / "fake_pi.py"


@dataclass
class FakePi:
    bin: Path
    log: Path
    script: Path

    def write_script(self, script: dict[str, Any]) -> None:
        self.script.write_text(json.dumps(script), encoding="utf-8")

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        records = [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]
        return [record for record in records if "argv" in record]


@pytest.fixture
def fake_pi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakePi:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "fake_pi.log"
    script = tmp_path / "fake_pi.json"
    wrapper = bin_dir / "pi"
    # The log and script paths are baked in: pi sessions get an allowlisted environment.
    wrapper.write_text(
        f"#!/bin/sh\nFAKE_PI_LOG={shlex.quote(str(log))} FAKE_PI_SCRIPT={shlex.quote(str(script))}"
        f' exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_PI_SOURCE))} "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    fake = FakePi(bin=wrapper, log=log, script=script)
    fake.write_script({})
    return fake


# ──────────────────────────────────────────────
# Fake docker on PATH (project services)
# ──────────────────────────────────────────────


FAKE_LB_SOURCE = Path(__file__).resolve().parent / "mocks" / "fake_lb.py"


@dataclass
class FakeLb:
    bin: Path
    log: Path
    script: Path

    def write_script(self, script: dict[str, Any]) -> None:
        self.script.write_text(json.dumps(script), encoding="utf-8")

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def fake_lb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeLb:
    """No test may reach the owner's Logbook executable or store."""
    bin_dir = tmp_path / "lb-bin"
    bin_dir.mkdir()
    log = tmp_path / "fake_lb.log"
    script = tmp_path / "fake_lb.json"
    wrapper = bin_dir / "lb"
    wrapper.write_text(
        f"#!/bin/sh\nFAKE_LB_LOG={shlex.quote(str(log))}"
        f" FAKE_LB_SCRIPT={shlex.quote(str(script))}"
        f' exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_LB_SOURCE))} "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    home = tmp_path / "logbook-home"
    home.mkdir()
    monkeypatch.setenv("LOGBOOK_HOME", str(home))
    monkeypatch.delenv("LOGBOOK_HARNESS", raising=False)
    monkeypatch.delenv("LOGBOOK_SESSION", raising=False)
    fake = FakeLb(bin=wrapper, log=log, script=script)
    fake.write_script({})
    return fake


@pytest.fixture
def no_lb(tmp_path, monkeypatch, fake_lb, silence_desktop_notifications):
    """Hide every lb executable without hiding other tools in shared PATH dirs."""
    from helmraiser.logbook import find_lb

    paths = []
    for index, entry in enumerate(os.environ["PATH"].split(os.pathsep)):
        directory = Path(entry or ".").absolute()
        binary = directory / "lb"
        if binary.is_file() and os.access(binary, os.X_OK):
            mirror = tmp_path / f"no-lb-{index}"
            mirror.mkdir()
            for tool in directory.iterdir():
                if tool.name != "lb":
                    (mirror / tool.name).symlink_to(tool)
            paths.append(str(mirror))
        else:
            paths.append(str(directory))
    monkeypatch.setenv("PATH", os.pathsep.join(paths))
    assert find_lb() is None


FAKE_DOCKER_SOURCE = Path(__file__).resolve().parent / "mocks" / "fake_docker.py"


@dataclass
class FakeDocker:
    bin: Path
    log: Path
    state: Path
    script: Path

    def write_script(self, script: dict[str, Any]) -> None:
        self.script.write_text(json.dumps(script), encoding="utf-8")

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def containers(self) -> dict[str, dict[str, Any]]:
        if not self.state.exists():
            return {}
        return dict(json.loads(self.state.read_text())["containers"])

    def add_container(self, name: str, labels: dict[str, str]) -> str:
        """A container that exists before the test (an orphan, say)."""
        state: dict[str, Any] = {"containers": {}, "next": 0}
        if self.state.exists():
            state = json.loads(self.state.read_text())
        cid = f"pre{len(state['containers']):09d}"
        state["containers"][cid] = {"name": name, "labels": labels, "ports": {}, "env": {}}
        self.state.write_text(json.dumps(state), encoding="utf-8")
        return cid


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    """A scripted docker (tests/mocks/fake_docker.py) first on PATH."""
    bin_dir = tmp_path / "docker-bin"
    bin_dir.mkdir()
    log = tmp_path / "fake_docker.log"
    state = tmp_path / "fake_docker_state.json"
    script = tmp_path / "fake_docker.json"
    wrapper = bin_dir / "docker"
    wrapper.write_text(
        f"#!/bin/sh\nFAKE_DOCKER_LOG={shlex.quote(str(log))}"
        f" FAKE_DOCKER_STATE={shlex.quote(str(state))}"
        f" FAKE_DOCKER_SCRIPT={shlex.quote(str(script))}"
        f' exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_DOCKER_SOURCE))} "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    fake = FakeDocker(bin=wrapper, log=log, state=state, script=script)
    fake.write_script({})
    return fake
