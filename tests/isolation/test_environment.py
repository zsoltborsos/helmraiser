"""What a run's child processes inherit from the worker (S1)."""

from __future__ import annotations

import sys

import pytest

from helmraiser.isolation.environment import (
    command_environment,
    is_stripped,
    session_environment,
    stripped_names,
)

WORKER = {
    "PATH": "/usr/bin",
    "HOME": "/home/u",
    "LANG": "en_US.UTF-8",
    "LC_TIME": "C",
    "XDG_CONFIG_HOME": "/home/u/.config",
    "PI_OFFLINE": "1",
    "SSH_AUTH_SOCK": "/run/agent.sock",
    "CLAUDE_CODE_SESSION_ID": "s",
    "CLAUDECODE": "1",
    "GH_TOKEN": "t",
    "OPENCODE_API_KEY": "k",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/bus",
    "WAYLAND_DISPLAY": "wayland-1",
    "HUSKY": "1",
    "JAVA_HOME": "/usr/lib/jvm",
}


@pytest.mark.parametrize(
    "name",
    [
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "GPG_AGENT_INFO",
        "CLAUDECODE",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "opencode_api_key",
        "NPM_TOKEN",
        "HF_TOKEN",
        "DB_PASSWORD",
        "MYSQL_PASSWD",
        "STRIPE_SECRET",
        "STARSHIP_SESSION_KEY",
        "SERVICE_APIKEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GH_HOST",
        "GITHUB_ACTIONS",
        "AWS_PROFILE",
        "AZURE_TENANT",
        "CLOUDFLARE_ACCOUNT_ID",
        "CF_ZONE",
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "VIRTUAL_ENV",
        "VIRTUAL_ENV_PROMPT",
        "PYTHONHOME",
    ],
)
def test_secret_shaped_names_are_stripped(name: str):
    assert is_stripped(name)


@pytest.mark.parametrize(
    "name", ["PATH", "HOME", "HUSKY", "XDG_RUNTIME_DIR", "KEYBOARD", "NODE_OPTIONS", "MONKEY"]
)
def test_ordinary_names_are_kept(name: str):
    assert not is_stripped(name)


class TestSessionEnvironment:
    def test_keeps_only_the_allowlist_plus_extra(self):
        env = session_environment({"HELMRAISER_RUN_ID": "r"}, WORKER)
        assert env == {
            "PATH": "/usr/bin",
            "HOME": "/home/u",
            "LANG": "en_US.UTF-8",
            "LC_TIME": "C",
            "XDG_CONFIG_HOME": "/home/u/.config",
            "PI_OFFLINE": "1",
            "HELMRAISER_RUN_ID": "r",
        }

    def test_extra_opts_in_and_wins(self):
        env = session_environment({"HUSKY": "0", "GH_TOKEN": "explicit", "PATH": "/x"}, WORKER)
        assert env["HUSKY"] == "0" and env["GH_TOKEN"] == "explicit" and env["PATH"] == "/x"

    def test_defaults_to_the_process_environment(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")
        monkeypatch.setenv("TERM", "xterm")
        env = session_environment({})
        assert env["TERM"] == "xterm" and "SSH_AUTH_SOCK" not in env


class TestCommandEnvironment:
    def test_keeps_everything_but_secret_shaped_names(self):
        env = command_environment({"HELMRAISER_RUN_ID": "r"}, WORKER)
        assert env == {
            "PATH": "/usr/bin",
            "HOME": "/home/u",
            "LANG": "en_US.UTF-8",
            "LC_TIME": "C",
            "XDG_CONFIG_HOME": "/home/u/.config",
            "PI_OFFLINE": "1",
            "HUSKY": "1",
            "JAVA_HOME": "/usr/lib/jvm",
            "HELMRAISER_RUN_ID": "r",
        }

    def test_a_worker_code_snapshot_does_not_reach_the_steps(self):
        """verify must test the worktree's code, not the snapshot the worker runs."""
        worker = {
            **WORKER,
            "PYTHONPATH": "/data/code/abc123/src:/extra",
            "HELMRAISER_CODE_SNAPSHOT": "abc123",
            "HELMRAISER_SOURCE_REPO": "/src/helmraiser",
        }
        env = command_environment({}, worker)
        assert env["PYTHONPATH"] == "/extra"
        assert "HELMRAISER_CODE_SNAPSHOT" not in env and "HELMRAISER_SOURCE_REPO" not in env
        only = {**worker, "PYTHONPATH": "/data/code/abc123/src"}
        assert "PYTHONPATH" not in command_environment({}, only)

    def test_project_env_re_adds_a_stripped_name(self):
        env = command_environment({"GH_TOKEN": "t", "PATH": "/tools:/usr/bin"}, WORKER)
        assert env["GH_TOKEN"] == "t" and env["PATH"] == "/tools:/usr/bin"
        assert "SSH_AUTH_SOCK" not in env


@pytest.fixture(params=[command_environment, session_environment])
def environment_builder(request: pytest.FixtureRequest):
    return request.param


class TestWorkerInterpreterEnvironment:
    @pytest.fixture(autouse=True)
    def worker_interpreter(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(sys, "prefix", "/worker")
        monkeypatch.setattr(sys, "base_prefix", "/system")
        monkeypatch.setattr(sys, "executable", "/worker/bin/python")

    @pytest.mark.parametrize(
        "path",
        [
            "/worker/bin:/tools:/usr/bin",
            "/tools:/worker/bin:/usr/bin",
            "/tools:/usr/bin:/worker/bin",
            "/worker/bin:/tools:/worker/bin:/usr/bin:/worker/bin",
        ],
    )
    def test_removes_venv_bin_everywhere(self, environment_builder, path: str):
        env = environment_builder({}, {"VIRTUAL_ENV": "/worker", "PATH": path})
        assert env["PATH"] == "/tools:/usr/bin"

    def test_removes_distinct_activated_and_interpreter_bins(self, environment_builder):
        base = {
            "VIRTUAL_ENV": "/activated",
            "VIRTUAL_ENV_PROMPT": "worker",
            "PYTHONHOME": "/python-home",
            "PATH": "/activated/bin:/tools:/worker/bin:/usr/bin:/activated/bin",
        }
        original = dict(base)
        env = environment_builder({}, base)
        assert env["PATH"] == "/tools:/usr/bin"
        assert not {"VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME"} & env.keys()
        assert base == original

    def test_interpreter_venv_without_activation(self, environment_builder):
        assert environment_builder({}, {"PATH": "/worker/bin:/tools"})["PATH"] == "/tools"

    def test_non_venv_interpreter_keeps_its_bin(
        self, environment_builder, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(sys, "prefix", sys.base_prefix)
        base = {"PATH": "/worker/bin:/tools:/worker/bin", "HOME": "/home/u"}
        assert environment_builder({}, base) == base
        # An activated venv is still filtered even with a system interpreter.
        assert environment_builder({}, {**base, "VIRTUAL_ENV": "/worker"})["PATH"] == "/tools"

    def test_pythonhome_is_stripped_without_a_venv(
        self, environment_builder, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(sys, "prefix", sys.base_prefix)
        assert environment_builder({}, {"PYTHONHOME": "/python-home"}) == {}

    def test_explicit_overrides_win_without_mutation(self, environment_builder):
        extra = {
            "PATH": "/worker/bin:/activated/bin:/project/bin",
            "VIRTUAL_ENV": "/explicit",
            "VIRTUAL_ENV_PROMPT": "explicit",
            "PYTHONHOME": "/explicit-home",
        }
        original = dict(extra)
        base = {"VIRTUAL_ENV": "/activated", "PATH": "/worker/bin:/activated/bin"}
        assert environment_builder(extra, base) == original
        assert extra == original
        assert base == {"VIRTUAL_ENV": "/activated", "PATH": "/worker/bin:/activated/bin"}

    def test_preserves_other_entries_exactly_in_order(self, environment_builder):
        path = ":/worker/bin:/tools:/worker/bin-extra:/tools::/worker/bin/:/worker/bin:"
        assert environment_builder({}, {"PATH": path})["PATH"] == (
            ":/tools:/worker/bin-extra:/tools::/worker/bin/:"
        )

    def test_missing_and_entirely_removed_path(self, environment_builder):
        assert "PATH" not in environment_builder({}, {"VIRTUAL_ENV": "/worker"})
        assert environment_builder({}, {"PATH": "/worker/bin:/worker/bin"})["PATH"] == ""

    def test_default_process_environment(
        self, environment_builder, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("VIRTUAL_ENV", "/activated")
        monkeypatch.setenv("VIRTUAL_ENV_PROMPT", "worker")
        monkeypatch.setenv("PYTHONHOME", "/python-home")
        monkeypatch.setenv("PATH", "/activated/bin:/tools:/worker/bin:/usr/bin")
        env = environment_builder({})
        assert env["PATH"] == "/tools:/usr/bin"
        assert not {"VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME"} & env.keys()


def test_stripped_names_lists_names_only():
    assert stripped_names(WORKER) == [
        "CLAUDECODE",
        "CLAUDE_CODE_SESSION_ID",
        "DBUS_SESSION_BUS_ADDRESS",
        "GH_TOKEN",
        "OPENCODE_API_KEY",
        "SSH_AUTH_SOCK",
        "WAYLAND_DISPLAY",
    ]
