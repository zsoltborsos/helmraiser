"""Sandbox cleanup without launching srt."""

import errno
import json
import subprocess
from pathlib import Path

import pytest

from helmraiser.isolation.retry import SandboxLaunch
from helmraiser.loop import shell


@pytest.mark.parametrize("result", [0, 9, "timeout", "error"])
def test_sandbox_cleanup(tmp_path, monkeypatch, result):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    subprocess.run(["git", "init", str(cwd)], check=True, capture_output=True)
    settings = tmp_path / "settings.json"
    settings.write_text('{"filesystem": {}}')
    launch = SandboxLaunch(["srt", "--", "true"], settings, tmp_path, "verify")
    real_run = subprocess.run

    def run(argv, **kwargs):
        if argv[0] == "git":
            return real_run(argv, **kwargs)
        (cwd / ".bashrc").touch()
        launch.marker.touch()
        if result == "timeout":
            raise subprocess.TimeoutExpired(argv, 1, output=b"partial")
        if result == "error":
            raise OSError("launch failed")
        return subprocess.CompletedProcess(argv, result, "output", "")

    monkeypatch.setattr(subprocess, "run", run)
    outcome = shell.run_shell("true", cwd, 1, {}, lambda argv: launch)
    assert not (cwd / ".bashrc").exists()
    assert not launch.marker.exists()
    if result == "timeout":
        assert outcome.timed_out and outcome.output == "partial"
    elif result == "error":
        assert outcome.error == "launch failed"
    else:
        assert outcome.exit_code == result and outcome.output == "output"
    event = json.loads((tmp_path / "events.jsonl").read_text())
    assert event["event_type"] == "sandbox_placeholders_removed"
    assert event["names"] == [".bashrc"]
    assert event["cwd"] == str(cwd)


@pytest.mark.parametrize("timed_out", [False, True])
def test_partial_cleanup_logs_removals(tmp_path, monkeypatch, timed_out):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    subprocess.run(["git", "init", str(cwd)], check=True, capture_output=True)
    settings = tmp_path / "settings.json"
    settings.write_text('{"filesystem": {}}')
    launch = SandboxLaunch(["srt", "--", "true"], settings, tmp_path, "verify")
    real_run = subprocess.run
    real_unlink = Path.unlink

    def run(argv, **kwargs):
        if argv[0] == "git":
            return real_run(argv, **kwargs)
        (cwd / ".gitconfig").touch()
        (cwd / ".gitmodules").touch()
        if timed_out:
            raise subprocess.TimeoutExpired(argv, 1, output=b"partial")
        return subprocess.CompletedProcess(argv, 9, "partial", "")

    def unlink(path, *args, **kwargs):
        if path == cwd / ".gitmodules":
            raise OSError(errno.EBUSY, "mounted")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(Path, "unlink", unlink)
    outcome = shell.run_shell("true", cwd, 1, {}, lambda argv: launch)
    assert outcome.timed_out == timed_out
    assert outcome.exit_code == (None if timed_out else 9)
    assert outcome.output == "partial"
    assert not (cwd / ".gitconfig").exists()
    assert (cwd / ".gitmodules").exists()
    event = json.loads((tmp_path / "events.jsonl").read_text())
    assert event["event_type"] == "sandbox_placeholders_removed"
    assert event["names"] == [".gitconfig"]
    assert event["cwd"] == str(cwd)
    assert event["kind"] == "verify"


@pytest.mark.parametrize("wrapped", [False, True])
def test_plain_commands_do_not_clean(tmp_path, wrapped):
    (tmp_path / ".bashrc").touch()
    result = shell.run_shell("true", tmp_path, 1, {}, (lambda argv: argv) if wrapped else None)
    assert result.exit_code == 0
    assert (tmp_path / ".bashrc").exists()


def test_cleanup_error_preserves_result(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text('{"filesystem": {}}')
    launch = SandboxLaunch(["srt", "--", "true"], settings, tmp_path, "verify")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 7, "ok", "")
    )

    def fail(cwd):
        raise PermissionError("busy")

    monkeypatch.setattr(shell, "remove_placeholders", fail)
    assert shell.run_shell("true", tmp_path, 1, {}, lambda argv: launch).exit_code == 7
