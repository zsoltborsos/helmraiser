"""Logbook attribution for pi sessions, never inherited interactive identities."""

from pathlib import Path

import pytest

from helmraiser.isolation.environment import command_environment, session_environment
from helmraiser.pi.adapter import PiJsonAdapter
from helmraiser.pi.protocol import PiRequest
from helmraiser.task.model import ModelSpec

RUN_ID = "3f2c1a9e-6b7d-4c1e-9a2b-1f0e8d7c6b5a"


def _request(tmp_path: Path, **overrides) -> PiRequest:
    fields = {
        "prompt": "Begin.",
        "session_id": f"{RUN_ID}-implement-2",
        "model": ModelSpec("test/model", "medium"),
        "cwd": tmp_path / "worktree",
        "session_dir": tmp_path / "runs" / RUN_ID / "sessions",
    }
    fields.update(overrides)
    return PiRequest(**fields)


@pytest.mark.parametrize("stage", ["implement", "plan", "review"])
def test_logbook_session_uses_run_identity(tmp_path, stage):
    req = _request(tmp_path, stage=stage, session_id=f"{RUN_ID}-{stage}-2")
    env = PiJsonAdapter().environment(req, RUN_ID)
    assert env["LOGBOOK_HARNESS"] == "helmraiser"
    assert env["LOGBOOK_SESSION"] == RUN_ID
    assert env["HELMRAISER_RUN_ID"] == RUN_ID
    assert env["HELMRAISER_STAGE"] == stage
    assert env["HELMRAISER_EVIDENCE_DIR"] == str(req.run_dir / "evidence")


def test_logbook_session_uses_scout_identity(tmp_path):
    env = PiJsonAdapter().environment(_request(tmp_path), "scout-123")
    assert env["LOGBOOK_SESSION"] == "scout-123"
    assert env["LOGBOOK_HARNESS"] == "helmraiser"


def test_logbook_session_replaces_parent_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("LOGBOOK_HARNESS", "claude-code")
    monkeypatch.setenv("LOGBOOK_SESSION", "interactive-session")
    env = PiJsonAdapter().environment(_request(tmp_path), RUN_ID)
    assert env["LOGBOOK_HARNESS"] == "helmraiser"
    assert env["LOGBOOK_SESSION"] == RUN_ID


def test_logbook_home_is_inherited(tmp_path, monkeypatch):
    store = str(tmp_path / "store")
    monkeypatch.setenv("LOGBOOK_HOME", store)
    assert PiJsonAdapter().environment(_request(tmp_path), RUN_ID)["LOGBOOK_HOME"] == store


def test_logbook_home_unset_stays_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("LOGBOOK_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    env = PiJsonAdapter().environment(_request(tmp_path), RUN_ID)
    assert "LOGBOOK_HOME" not in env
    assert env["HOME"] == str(tmp_path)


def test_logbook_task_env_overrides_defaults(tmp_path):
    overrides = {
        "LOGBOOK_HARNESS": "user",
        "LOGBOOK_SESSION": "explicit-session",
        "LOGBOOK_HOME": str(tmp_path / "explicit-store"),
    }
    env = PiJsonAdapter().environment(_request(tmp_path, env=overrides), RUN_ID)
    assert all(env[key] == value for key, value in overrides.items())


def test_logbook_commands_strip_parent_identity():
    base = {
        "LOGBOOK_HARNESS": "claude-code",
        "LOGBOOK_SESSION": "interactive-session",
        "LOGBOOK_HOME": "/fake/store",
        "HOME": "/fake/home",
        "PATH": "/usr/bin",
    }
    assert command_environment({}, base) == {
        "LOGBOOK_HOME": "/fake/store",
        "HOME": "/fake/home",
        "PATH": "/usr/bin",
    }


def test_logbook_commands_preserve_explicit_overrides():
    explicit = {"LOGBOOK_HARNESS": "user", "LOGBOOK_SESSION": "explicit"}
    assert command_environment(explicit, {"LOGBOOK_HARNESS": "claude-code"}) == explicit


def test_logbook_session_allowlist_drops_parent_identity():
    assert (
        session_environment(
            {}, {"LOGBOOK_HARNESS": "claude-code", "LOGBOOK_SESSION": "interactive"}
        )
        == {}
    )
