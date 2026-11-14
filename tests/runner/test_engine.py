"""Tests for ExecutionEngine — subprocess calls mocked, except run_pty's own spawn."""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helmraiser.runner.engine import ExecResult, ExecutionEngine, StdinHandle
from helmraiser.runner.process_control import ProcessIdentity
from helmraiser.storage.state import StateManager

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


class FakeProcessController:
    def __init__(self):
        self.identity = ProcessIdentity(4242, 4242, "token")
        self.terminated: list[ProcessIdentity] = []
        self.killed: list[ProcessIdentity] = []

    def identify(self, pid: int) -> ProcessIdentity:
        return self.identity

    def is_running(self, identity: ProcessIdentity) -> bool:
        return bool(self.terminated) and not self.killed

    def terminate(self, identity: ProcessIdentity) -> bool:
        self.terminated.append(identity)
        return True

    def kill(self, identity: ProcessIdentity) -> bool:
        self.killed.append(identity)
        return True


def _make_mock_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    proc = MagicMock()
    proc.pid = 4242
    proc.returncode = returncode
    proc.stdout = AsyncMock()
    proc.stdout.read = AsyncMock(return_value=stdout)
    proc.stdout.readline = AsyncMock(return_value=b"")
    proc.stderr = AsyncMock()
    proc.stderr.read = AsyncMock(return_value=stderr)
    proc.stdin = AsyncMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    proc.terminate = MagicMock()
    proc.poll = MagicMock(return_value=returncode)
    return proc


# ──────────────────────────────────────────────
# ExecResult
# ──────────────────────────────────────────────


class TestExecResult:
    def test_default_values(self):
        result = ExecResult(success=True)
        assert result.stdout == ""
        assert result.stderr == ""
        assert result.returncode == 0
        assert result.timed_out is False
        assert result.killed is False

    def test_failure_result(self):
        result = ExecResult(success=False, stderr="error", returncode=1)
        assert result.success is False
        assert result.stderr == "error"
        assert result.returncode == 1


# ──────────────────────────────────────────────
# run_stdin — Happy path
# ──────────────────────────────────────────────


class TestRunStdinHappyPath:
    def test_executes_command_with_stdin(self):
        engine = ExecutionEngine()
        proc = _make_mock_proc(returncode=0, stdout=b"hello output", stderr=b"")

        async def run():
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                return await engine.run_stdin(
                    cmd=["my-agent", "--prompt"],
                    input="test input",
                    timeout=10,
                    cwd="/tmp",
                )

        result = asyncio.run(run())
        assert result.success is True
        assert result.stdout == "hello output"
        assert result.stderr == ""
        proc.stdin.write.assert_called_once()
        proc.stdin.drain.assert_called_once()

    def test_captures_stderr(self):
        engine = ExecutionEngine()
        proc = _make_mock_proc(returncode=0, stdout=b"", stderr=b"error message")

        async def run():
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                return await engine.run_stdin(cmd=["cmd"], input="", timeout=10, cwd="/tmp")

        result = asyncio.run(run())
        assert result.stderr == "error message"


# ──────────────────────────────────────────────
# run_stdin — Disaster path
# ──────────────────────────────────────────────


class TestStdinHandle:
    @pytest.mark.parametrize("external", [False, True])
    def test_stdin_lifetime_during_callback(self, external):
        proc = _make_mock_proc()
        proc.stdout.readline.side_effect = [b"ready\n", b""]
        handle = StdinHandle()
        observed = []

        async def on_line(line):
            observed.append((line, proc.stdin.close.call_count))
            if external:
                handle.close()
                handle.close()
            observed.append(proc.stdin.close.call_count)

        async def run():
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                return await ExecutionEngine().run_stdin(
                    ["agent"],
                    "prompt\n",
                    10,
                    "/tmp",
                    on_line,
                    stdin_handle=handle if external else None,
                )

        assert asyncio.run(run()).success
        # Assert outside the callback: callback exceptions are deliberately swallowed.
        assert observed == [("ready\n", 0 if external else 1), 1]
        proc.stdin.close.assert_called_once()

    @pytest.mark.parametrize("outcome", ["normal", "timeout", "refused", "stream", "cancel"])
    def test_finally_closes_unclosed_handle(self, outcome, tmp_path):
        proc = _make_mock_proc()
        controller = FakeProcessController()
        state = StateManager(tmp_path / "state.db")
        state.create_run("run", "topic")
        if outcome == "refused":
            state.request_abort("run")
        if outcome == "stream":
            proc.stdout.read.side_effect = OSError("stream failed")
        if outcome in {"timeout", "cancel"}:
            proc.stdout.read.side_effect = (
                TimeoutError() if outcome == "timeout" else asyncio.CancelledError()
            )
        handle = StdinHandle()

        async def run():
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                return await ExecutionEngine(state, controller).run_stdin(
                    ["agent"],
                    "prompt\n",
                    10,
                    str(tmp_path),
                    run_id="run",
                    stdin_handle=handle,
                )

        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(run())
            assert controller.terminated == []
        else:
            result = asyncio.run(run())
            assert result.success == (outcome == "normal")
            assert result.timed_out == (outcome == "timeout")
        proc.stdin.close.assert_called_once()
        handle.close()
        proc.stdin.close.assert_called_once()
        assert state.request_abort("run").lease is None


class TestRunStdinDisasterPath:
    def test_command_not_found(self):
        engine = ExecutionEngine()

        async def run():
            with patch(
                "asyncio.create_subprocess_exec", side_effect=FileNotFoundError("no such file")
            ):
                return await engine.run_stdin(cmd=["nonexistent"], input="", timeout=10, cwd="/tmp")

        result = asyncio.run(run())
        assert result.success is False
        assert result.returncode == -1

    def test_timeout_terminates_validated_process_group(self):
        controller = FakeProcessController()
        engine = ExecutionEngine(process_controller=controller)
        proc = _make_mock_proc()

        async def fake_gather(*args, **kwargs):
            await asyncio.sleep(100)
            return (b"", b"")

        async def run():
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                with patch("asyncio.gather", side_effect=fake_gather):
                    return await engine.run_stdin(cmd=["slow-cmd"], input="", timeout=1, cwd="/tmp")

        result = asyncio.run(run())
        assert result.timed_out is True
        assert result.killed is True
        assert result.success is False
        assert controller.terminated == [controller.identity]
        assert controller.killed == []
        proc.kill.assert_not_called()

    def test_child_exiting_without_reading_stdin_reports_its_own_exit(self):
        # A child gone before stdin is drained must not turn into "Connection lost".
        engine = ExecutionEngine()
        result = asyncio.run(
            engine.run_stdin(
                cmd=["sh", "-c", "exec 0<&-; echo bad flag >&2; exit 3"],
                input="x" * (1 << 20),
                timeout=10,
                cwd="/tmp",
            )
        )
        assert (result.success, result.returncode) == (False, 3)
        assert result.stderr == "bad flag\n"

    def test_nonzero_exit_code(self):
        engine = ExecutionEngine()
        proc = _make_mock_proc(returncode=42, stdout=b"", stderr=b"failed")

        async def run():
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                return await engine.run_stdin(cmd=["fail-cmd"], input="", timeout=10, cwd="/tmp")

        result = asyncio.run(run())
        assert result.success is False
        assert result.returncode == 42


# ──────────────────────────────────────────────
# run_pty — Disaster path
# ──────────────────────────────────────────────


class TestRunPtyDisasterPath:
    def test_command_not_found(self):
        engine = ExecutionEngine()

        async def run():
            with patch(
                "asyncio.create_subprocess_exec", side_effect=FileNotFoundError("no such file")
            ):
                return await engine.run_pty(
                    cmd=["fake"],
                    completion_signal=None,
                    timeout=10,
                    cwd="/tmp",
                )

        result = asyncio.run(run())
        assert result.success is False
        assert "no such file" in result.stderr

    def test_readline_timeout_uses_returncode_not_poll(self):
        """run_pty readline-timeout path must check proc.returncode, not proc.poll().

        asyncio.subprocess.Process has no poll() method. Calling poll() raises
        AttributeError. This test verifies that after a readline timeout, the
        loop detects process completion via returncode, not poll().
        """
        engine = ExecutionEngine()

        # A pipe stands in for the terminal; its held write end keeps readline waiting
        # (as a grandchild holding the terminal would) so asyncio.wait_for fires.
        read_end, write_end = os.pipe()
        held = os.dup(write_end)

        proc = MagicMock()
        proc.returncode = 0  # Process already done when readline times out
        proc.stderr = AsyncMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.stdin = AsyncMock()
        proc.wait = AsyncMock(return_value=0)
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        # simulate asyncio.Process: no poll() method
        proc.poll.side_effect = AttributeError("asyncio.Process has no poll()")

        # Patch the per-line timeout inside run_pty from 5.0s → 0.02s so test is fast
        original_wait_for = asyncio.wait_for

        async def fast_wait_for(coro, timeout):
            return await original_wait_for(coro, min(timeout, 0.02))

        async def run():
            with (
                patch("pty.openpty", return_value=(read_end, write_end)),
                patch("asyncio.create_subprocess_exec", return_value=proc),
                patch("asyncio.wait_for", side_effect=fast_wait_for),
            ):
                return await engine.run_pty(
                    cmd=["agent"],
                    completion_signal=None,
                    timeout=5,
                    cwd="/tmp",
                )

        try:
            result = asyncio.run(run())
        finally:
            os.close(held)
        # Must not raise AttributeError from proc.poll()
        # Process was done (returncode=0) so success
        assert result.returncode == 0


class _RecordingController(FakeProcessController):
    def __init__(self):
        super().__init__()
        self.identified: list[int] = []

    def identify(self, pid: int) -> ProcessIdentity:
        self.identified.append(pid)
        return ProcessIdentity(pid, pid, "token")


class TestRunPtyExec:
    def test_lease_names_the_command_itself_on_a_terminal(self, tmp_path):
        # ``script -c`` ran the command through ``$SHELL -c``: the lease named script.
        state = StateManager(tmp_path / "state.db")
        state.create_run("run", "topic")
        controller = _RecordingController()
        engine = ExecutionEngine(state, controller)
        cmd = [
            sys.executable,
            "-c",
            "import os, sys; print(os.getpid(), sys.stdout.isatty(), sys.argv[1])",
            "it's $HOME; true",
        ]

        result = asyncio.run(engine.run_pty(cmd, None, 30, str(tmp_path), run_id="run"))

        assert result.success, result
        pid, tty, arg = result.stdout.strip().split(" ", 2)
        assert controller.identified == [int(pid)]
        assert (tty, arg) == ("True", "it's $HOME; true")
        assert state.request_abort("run").lease is None  # cleared after the run

    def test_spawns_argv_directly_without_a_shell(self, tmp_path):
        engine = ExecutionEngine()
        cmd = ["agent", "--prompt", "it's $HOME; true"]

        async def run():
            with patch(
                "asyncio.create_subprocess_exec", side_effect=FileNotFoundError("agent")
            ) as spawn:
                await engine.run_pty(cmd, None, 10, str(tmp_path))
                assert spawn.call_args.args == tuple(cmd)
                assert spawn.call_args.kwargs["start_new_session"] is True

        asyncio.run(run())


# ──────────────────────────────────────────────
# leases and session isolation
# ──────────────────────────────────────────────


class TestProcessLeases:
    def test_stdin_starts_new_session_and_clears_its_lease(self, tmp_path):
        state = StateManager(tmp_path / "state.db")
        state.create_run("run", "topic")
        controller = FakeProcessController()
        engine = ExecutionEngine(state, controller)
        proc = _make_mock_proc()

        async def run():
            with patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
                result = await engine.run_stdin(["agent"], "", 10, str(tmp_path), run_id="run")
                assert spawn.call_args.kwargs["start_new_session"] is True
                return result

        assert asyncio.run(run()).success
        assert state.request_abort("run").lease is None

    def test_stdin_spawns_argv_directly_without_a_shell(self, tmp_path):
        # A ``/bin/sh -c`` in between (dash forks instead of exec'ing) would make the
        # lease point at the shell and leave SIGTERM short of the agent.
        engine = ExecutionEngine()
        proc = _make_mock_proc()
        cmd = ["agent", "--prompt", "it's $HOME; true"]

        async def run():
            with (
                patch("asyncio.create_subprocess_shell") as shell,
                patch("asyncio.create_subprocess_exec", return_value=proc) as spawn,
            ):
                result = await engine.run_stdin(cmd, "", 10, str(tmp_path))
                assert spawn.call_args.args == tuple(cmd)
                shell.assert_not_called()
                return result

        assert asyncio.run(run()).success

    def test_spawn_loses_abort_race_and_terminates_new_group(self, tmp_path):
        state = StateManager(tmp_path / "state.db")
        state.create_run("run", "topic")
        state.request_abort("run")
        controller = FakeProcessController()
        engine = ExecutionEngine(state, controller)
        proc = _make_mock_proc()

        async def run():
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                return await engine.run_stdin(["agent"], "", 10, str(tmp_path), run_id="run")

        result = asyncio.run(run())
        assert not result.success
        assert "run is aborting" in result.stderr
        assert controller.terminated == [controller.identity]


# ──────────────────────────────────────────────
# run_stdin on_line streaming callback
# ──────────────────────────────────────────────


class TestRunStdinOnLine:
    def test_on_line_receives_each_stdout_line(self):
        engine = ExecutionEngine()
        seen: list[str] = []

        async def on_line(line: str) -> None:
            seen.append(line)

        result = asyncio.run(
            engine.run_stdin(
                cmd=["printf", "one\\ntwo\\n"],
                input="",
                timeout=10,
                cwd="/tmp",
                on_line=on_line,
            )
        )
        assert result.success
        assert result.stdout == "one\ntwo\n"
        assert [line.strip() for line in seen] == ["one", "two"]

    def test_capture_stdout_false_streams_without_buffering(self):
        engine = ExecutionEngine()
        seen: list[str] = []

        async def on_line(line: str) -> None:
            seen.append(line.strip())

        result = asyncio.run(
            engine.run_stdin(
                cmd=["printf", "one\\ntwo\\n"],
                input="",
                timeout=10,
                cwd="/tmp",
                on_line=on_line,
                capture_stdout=False,
            )
        )
        assert result.success
        assert result.stdout == ""
        assert seen == ["one", "two"]

    def test_capture_stdout_false_without_on_line_drains_stdout(self):
        engine = ExecutionEngine()
        result = asyncio.run(
            engine.run_stdin(
                cmd=["printf", "drained\\n"],
                input="",
                timeout=10,
                cwd="/tmp",
                capture_stdout=False,
            )
        )
        assert result.success
        assert result.stdout == ""

    def test_env_is_the_complete_environment(self, monkeypatch):
        monkeypatch.setenv("INHERITED_VAR", "inherited")
        engine = ExecutionEngine()
        result = asyncio.run(
            engine.run_stdin(
                cmd=["/bin/sh", "-c", "echo $HELMRAISER_RUN_ID x${INHERITED_VAR}x"],
                input="",
                timeout=10,
                cwd="/tmp",
                env={"HELMRAISER_RUN_ID": "run-1"},
            )
        )
        assert result.stdout.strip() == "run-1 xx"

    def test_no_env_inherits(self, monkeypatch):
        monkeypatch.setenv("INHERITED_VAR", "inherited")
        result = asyncio.run(
            ExecutionEngine().run_stdin(
                cmd=["sh", "-c", "echo $INHERITED_VAR"], input="", timeout=10, cwd="/tmp"
            )
        )
        assert result.stdout.strip() == "inherited"

    def test_on_line_errors_do_not_fail_run(self):
        engine = ExecutionEngine()

        async def on_line(line: str) -> None:
            raise RuntimeError("heartbeat down")

        result = asyncio.run(
            engine.run_stdin(
                cmd=["echo", "hello"],
                input="",
                timeout=10,
                cwd="/tmp",
                on_line=on_line,
            )
        )
        assert result.success
        assert "hello" in result.stdout
