"""Conservative pre-command sandbox failure detection, without real srt."""

import json
import subprocess
from pathlib import Path

import pytest

from helmraiser.isolation.retry import SandboxLaunch


def launch(tmp_path, denied=None):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {"filesystem": {"denyRead": [str(denied or tmp_path / "gone")], "denyWrite": []}}
        )
    )
    return SandboxLaunch(
        ["srt", "--settings", str(settings), "--", "true"], settings, tmp_path, "verify"
    )


@pytest.mark.parametrize("kind", ["file", "dir"])
@pytest.mark.parametrize("child", ["", "/child with spaces"])
def test_vanished_denial(tmp_path, kind, child):
    wrapped = launch(tmp_path)
    path = str(tmp_path / "gone") + child
    diagnostic = f"wrapper warning\nbwrap: Can't create {kind} {path}: Read-only file system\n"
    assert wrapped.retry_path(1, "", diagnostic) == Path(path)
    # A snapshot, not a later read of mutable settings.
    wrapped.settings_path.write_text("{}")
    assert wrapped.retry_path(1, "", diagnostic) == Path(path)


@pytest.mark.parametrize(
    "case",
    [
        "outside",
        "prefix",
        "existing",
        "symlink",
        "permission",
        "relative",
        "embedded",
        "error",
        "zero",
        "signal",
        "timeout",
        "killed",
        "stdout",
        "marker",
    ],
)
def test_not_retryable(tmp_path, monkeypatch, case):
    wrapped = launch(tmp_path)
    path = tmp_path / "gone"
    code, stdout, timed_out, killed = 1, "", False, False
    if case == "outside":
        path = tmp_path / "other"
    elif case == "prefix":
        path = tmp_path / "gone-other"
    elif case == "existing":
        path.touch()
    elif case == "symlink":
        path.symlink_to(tmp_path / "missing-target")
    elif case == "permission":

        def refused(self):
            raise PermissionError

        monkeypatch.setattr(Path, "lstat", refused)
    elif case == "zero":
        code = 0
    elif case == "signal":
        code = -15
    elif case == "timeout":
        timed_out = True
    elif case == "killed":
        killed = True
    elif case == "stdout":
        stdout = "\n"
    elif case == "marker":
        wrapped.marker.touch()
    diagnostic = f"bwrap: Can't create file {path}: Read-only file system\n"
    if case == "relative":
        diagnostic = "bwrap: Can't create file gone: Read-only file system\n"
    elif case == "embedded":
        diagnostic = "app says: " + diagnostic
    elif case == "error":
        diagnostic = diagnostic.replace("Read-only file system", "Permission denied")
    assert wrapped.retry_path(code, stdout, diagnostic, timed_out=timed_out, killed=killed) is None


def test_guard_and_cleanup(tmp_path):
    wrapped = launch(tmp_path)
    args = [
        "/bin/bash",
        "-c",
        'test -f "$1" && printf "%s" "$2"; exit 7',
        "test",
        str(wrapped.marker),
        "a 'quoted' argument",
    ]
    wrapped[wrapped.index("--") + 1 :] = args
    guarded = wrapped.guarded_argv()
    assert not wrapped.marker.exists()
    result = subprocess.run(guarded[guarded.index("--") + 1 :], capture_output=True, text=True)
    assert result.returncode == 7
    assert result.stdout == "a 'quoted' argument"
    assert wrapped.marker.exists()
    wrapped.cleanup()
    wrapped.cleanup()
    assert not wrapped.marker.exists()


def test_local_event(tmp_path):
    wrapped = launch(tmp_path)
    wrapped.record_retry(tmp_path / "gone")
    [event] = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert event["event_type"] == "sandbox_retry"
    assert {k: event[k] for k in ("kind", "vanished_path", "attempt")} == {
        "kind": "verify",
        "vanished_path": str(tmp_path / "gone"),
        "attempt": 2,
    }
