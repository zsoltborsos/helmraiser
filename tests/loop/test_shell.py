"""Shared shell runner behavior."""

from __future__ import annotations

import json
import os
import shlex
import time
from pathlib import Path

import pytest

from helmraiser.isolation.process import ProcessIsolation
from helmraiser.loop import shell
from helmraiser.loop.shell import run_shell
from tests.mocks.fake_srt import FakeSrt, unwrap_guard


def sandbox(tmp_path, failures=1, delay=0):
    data = tmp_path / "data"
    sibling = data / "runs" / "vanished sibling"
    sibling.mkdir(parents=True)
    fake = FakeSrt(tmp_path / "bin", sibling, failures=failures, delay=delay)
    isolation = ProcessIsolation(
        {"isolation": {"srt": str(fake.bin), "tmpdir": str(tmp_path / "temp")}}, data_dir=data
    )
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    run_dir = data / "runs" / "own"
    return fake, isolation, cwd, run_dir, sibling


@pytest.mark.parametrize("failures", [1, 2])
def test_vanished_sibling_rebuilds_settings_once(tmp_path, failures):
    fake, isolation, cwd, run_dir, sibling = sandbox(tmp_path, failures)
    result = run_shell(
        "echo once >> counter; echo done",
        cwd,
        5,
        dict(os.environ),
        lambda argv: isolation.wrap_command(argv, cwd, run_dir, "verify"),
    )
    calls = fake.calls()
    assert len(calls) == 2
    assert str(sibling) in calls[0]["settings"]["filesystem"]["denyRead"]
    assert str(sibling) not in calls[1]["settings"]["filesystem"]["denyRead"]
    assert result.exit_code == (0 if failures == 1 else 1)
    if failures == 1:
        assert (cwd / "counter").read_text() == "once\n"
        assert result.output == "done\n"
    else:
        assert not (cwd / "counter").exists()
        assert str(sibling) in result.output
    [event] = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert event["event_type"] == "sandbox_retry"
    assert event["vanished_path"] == str(sibling)
    for call in calls:
        guarded = call["argv"][3:]
        unwrap_guard(guarded)
        assert not Path(guarded[4]).exists()


@pytest.mark.parametrize("diagnostic", [False, True])
@pytest.mark.parametrize("stdout", [False, True])
def test_started_failure_never_retried(tmp_path, diagnostic, stdout):
    fake, isolation, cwd, run_dir, sibling = sandbox(tmp_path, 0)
    sibling.rmdir()
    output = (
        f"bwrap: Can't create file {sibling}: Read-only file system"
        if diagnostic
        else "ordinary error"
    )
    command = (
        f"echo once >> counter; printf '%s\\n' {shlex.quote(output)}"
        + ("" if stdout else " >&2")
        + "; exit 7"
    )
    result = run_shell(
        command,
        cwd,
        5,
        dict(os.environ),
        lambda argv: isolation.wrap_command(argv, cwd, run_dir, "verify"),
    )
    assert result.exit_code == 7
    assert len(fake.calls()) == 1
    assert (cwd / "counter").read_text() == "once\n"
    assert not (run_dir / "events.jsonl").exists()


def test_retry_uses_remaining_timeout(tmp_path):
    fake, isolation, cwd, run_dir, _ = sandbox(tmp_path, delay=0.6)
    started = time.monotonic()
    result = run_shell(
        "exec sleep 2",
        cwd,
        1,
        dict(os.environ),
        lambda argv: isolation.wrap_command(argv, cwd, run_dir, "verify"),
    )
    assert result.timed_out
    assert len(fake.calls()) == 2
    assert time.monotonic() - started < 1.5
    assert not list(isolation.tmpdir.glob("*.marker"))


@pytest.mark.parametrize("command", ["false | true", "true | false"])
def test_pipeline_failure_is_reported(command: str, tmp_path: Path):
    result = run_shell(command, tmp_path, 5, dict(os.environ))

    assert result.exit_code not in (None, 0)
    assert not result.timed_out


def test_plain_success_exits_zero(tmp_path: Path):
    result = run_shell("true", tmp_path, 5, dict(os.environ))

    assert result.exit_code == 0


@pytest.mark.parametrize("redirect", ["", " >&2"], ids=["stdout", "stderr"])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_invalid_utf8_preserves_output_and_exit_code(tmp_path: Path, redirect: str, exit_code: int):
    command = rf"printf 'valid:\303\251 invalid:\377\n'{redirect}; exit {exit_code}"

    result = run_shell(command, tmp_path, 5, dict(os.environ))

    assert result.exit_code == exit_code
    assert result.output == "valid:é invalid:\ufffd\n"
    assert not result.timed_out
    assert result.error is None


@pytest.mark.parametrize("redirect", ["", " >&2"], ids=["stdout", "stderr"])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_ansi_sequences_are_stripped(tmp_path: Path, redirect: str, exit_code: int):
    command = (
        r"printf '\033[31mred\033[0m \033]8;;https://x.test\007bel-link\033]8;;\007 "
        rf"\033]8;;https://x.test\033\\st-link\033]8;;\033\\\n'{redirect}; exit {exit_code}"
    )

    result = run_shell(command, tmp_path, 5, dict(os.environ))

    assert result.exit_code == exit_code
    assert result.output == "red bel-link st-link\n"
    assert not result.timed_out
    assert result.error is None


@pytest.mark.parametrize("redirect", ["", " >&2"], ids=["stdout", "stderr"])
def test_timeout_output_has_ansi_sequences_stripped(tmp_path: Path, redirect: str):
    command = (
        r"printf '\033[31mred\033[0m \033]8;;https://x.test\007bel-link\033]8;;\007 "
        rf"\033]8;;https://x.test\033\\st-link\033]8;;\033\\\n'{redirect}; exec sleep 5"
    )

    result = run_shell(command, tmp_path, 1, dict(os.environ))

    assert result.exit_code is None
    assert result.timed_out
    assert result.output == "red bel-link st-link\n"


def test_timeout_has_no_exit_code(tmp_path: Path):
    result = run_shell("sleep 5", tmp_path, 1, dict(os.environ))

    assert result.exit_code is None
    assert result.timed_out


def test_a_wrap_runs_the_bash_argv_it_returns_with_stdin_on_dev_null(tmp_path: Path):
    seen: list[list[str]] = []

    def wrap(argv: list[str]) -> list[str]:
        seen.append(argv)
        return ["/usr/bin/env", "WRAPPED=1", *argv]

    result = run_shell(
        'echo "$WRAPPED"; readlink /proc/self/fd/0', tmp_path, 5, dict(os.environ), wrap
    )

    assert seen == [
        ["/bin/bash", "-o", "pipefail", "-c", 'echo "$WRAPPED"; readlink /proc/self/fd/0']
    ]
    assert result.exit_code == 0
    assert result.output.splitlines() == ["1", "/dev/null"]


def test_long_command_runs_from_a_script_and_leaves_none(tmp_path: Path):
    filler = "#" + "x" * (shell.SCRIPT_THRESHOLD + 10) + "\n"
    command = filler + 'echo "in $(pwd)"; exit 3'
    before = set(Path("/tmp").glob("helmraiser-step-*.sh"))

    result = run_shell(command, tmp_path, 5, dict(os.environ))

    assert result.exit_code == 3
    assert result.output.strip() == f"in {tmp_path}"
    assert set(Path("/tmp").glob("helmraiser-step-*.sh")) == before


def test_long_command_keeps_pipefail(tmp_path: Path):
    command = "#" + "x" * (shell.SCRIPT_THRESHOLD + 10) + "\nfalse | true"

    assert run_shell(command, tmp_path, 5, dict(os.environ)).exit_code not in (None, 0)


def test_long_command_is_wrapped_as_a_script(tmp_path: Path):
    seen: list[list[str]] = []

    def wrap(argv: list[str]) -> list[str]:
        seen.append(argv)
        return argv

    command = "#" + "x" * (shell.SCRIPT_THRESHOLD + 10) + "\ntrue"
    assert run_shell(command, tmp_path, 5, dict(os.environ), wrap=wrap).exit_code == 0
    assert seen[0][:3] == ["/bin/bash", "-o", "pipefail"]
    assert seen[0][3].startswith("/tmp/helmraiser-step-") and len(seen[0]) == 4
