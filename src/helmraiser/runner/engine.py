"""ExecutionEngine — safe subprocess execution with attempt-scoped leases."""

from __future__ import annotations

import asyncio
import os
import pty
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from helmraiser.runner.process_control import (
    PosixProcessController,
    ProcessController,
    ProcessIdentity,
)

if TYPE_CHECKING:
    from helmraiser.storage.state import StateManager

# asyncio's default StreamReader limit is 64 KiB per line; a ``pi --mode json``
# ``turn_end`` record carries every tool result of the turn and easily exceeds it.
STREAM_LINE_LIMIT = 16 * 1024 * 1024


@dataclass
class ExecResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    timed_out: bool = False
    killed: bool = False


class StdinHandle:
    """Caller-closeable stdin lifetime; the engine always owns final cleanup."""

    def __init__(self) -> None:
        self._writer: asyncio.StreamWriter | None = None

    def _attach(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None


class ExecutionEngine:
    def __init__(
        self,
        state_manager: StateManager | None = None,
        process_controller: ProcessController | None = None,
    ):
        self.state_manager = state_manager
        self.process_controller = process_controller or PosixProcessController()

    def _publish_lease(
        self, proc: asyncio.subprocess.Process, run_id: str | None
    ) -> tuple[str | None, ProcessIdentity | None]:
        if self.state_manager is None or run_id is None:
            return None, None
        from helmraiser.storage.state import ProcessLease

        identity = self.process_controller.identify(proc.pid)
        attempt_id = str(uuid4())
        if not self.state_manager.register_process(
            ProcessLease(
                run_id,
                attempt_id,
                identity.leader_pid,
                identity.pgid,
                identity.start_token,
            )
        ):
            self.process_controller.terminate(identity)
            raise RuntimeError("run is aborting; refusing a newly spawned agent")
        return attempt_id, identity

    def _clear_lease(self, run_id: str | None, attempt_id: str | None) -> None:
        if self.state_manager and run_id and attempt_id:
            self.state_manager.clear_process(run_id, attempt_id)

    async def _stop_process_group(
        self, proc: asyncio.subprocess.Process, identity: ProcessIdentity | None
    ) -> None:
        if identity is None:
            try:
                identity = self.process_controller.identify(proc.pid)
            except (ValueError, OSError):
                # Never guess process identity. This also makes mocked PIDs
                # incapable of reaching the host signal syscall.
                await proc.wait()
                return
        self.process_controller.terminate(identity)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            self.process_controller.kill(identity)
            await proc.wait()

    async def run_stdin(
        self,
        cmd: list[str],
        input: str,
        timeout: int,
        cwd: str,
        on_line: Callable[[str], Awaitable[None]] | None = None,
        run_id: str | None = None,
        capture_stdout: bool = True,
        env: dict[str, str] | None = None,
        *,
        stdin_handle: StdinHandle | None = None,
    ) -> ExecResult:
        """Run ``cmd`` with ``input`` on stdin.

        ``capture_stdout=False`` streams stdout to ``on_line`` without buffering
        it into the result (long JSONL sessions). ``env`` is the child's complete
        environment (see :mod:`helmraiser.isolation.environment`); ``None`` inherits.
        Supplying ``stdin_handle`` keeps stdin open until the caller closes it.
        ``timeout`` covers stdin delivery as well as output and process completion.
        """
        handle = stdin_handle if stdin_handle is not None else StdinHandle()
        attempt_id = None
        identity = None
        proc: asyncio.subprocess.Process | None = None
        leased = False
        try:
            # Exec the argv directly: no ``/bin/sh -c`` in between, so the lease names
            # the agent itself and signals reach it (dash forks rather than exec'ing).
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env=env,
                limit=STREAM_LINE_LIMIT,
            )
            assert proc.stdin is not None
            handle._attach(proc.stdin)
            attempt_id, identity = self._publish_lease(proc, run_id)
            leased = True  # a refused lease has already stopped the child
            assert proc.stdin and proc.stdout and proc.stderr
            stdin = proc.stdin
            stdout_stream = proc.stdout
            stderr_stream = proc.stderr

            async def read_stdout() -> bytes:
                if on_line is None:
                    if capture_stdout:
                        return await stdout_stream.read()
                    while await stdout_stream.read(65536):
                        pass
                    return b""
                chunks: list[bytes] = []
                while line := await stdout_stream.readline():
                    if capture_stdout:
                        chunks.append(line)
                    try:
                        await on_line(line.decode("utf-8", errors="replace"))
                    except Exception:
                        pass
                return b"".join(chunks)

            try:
                async with asyncio.timeout(timeout):
                    stdin.write(input.encode())
                    try:
                        await stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # the child's exit status says why stdin was closed unread
                    if stdin_handle is None:
                        handle.close()
                    stdout, stderr = await asyncio.gather(read_stdout(), stderr_stream.read())
                    returncode = await proc.wait()
            except TimeoutError:
                await self._stop_process_group(proc, identity)
                return ExecResult(False, returncode=-1, timed_out=True, killed=True)
            return ExecResult(
                returncode == 0,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
                returncode,
            )
        except FileNotFoundError as exc:
            return ExecResult(False, stderr=str(exc), returncode=-1)
        except Exception as exc:
            if leased and proc is not None and proc.returncode is None:
                await self._stop_process_group(proc, identity)
            return ExecResult(False, stderr=str(exc), returncode=-1)
        finally:
            handle.close()
            self._clear_lease(run_id, attempt_id)

    async def run_pty(
        self,
        cmd: list[str],
        completion_signal: str | None,
        timeout: int,
        cwd: str,
        run_id: str | None = None,
    ) -> ExecResult:
        """Run ``cmd`` with a pseudo-terminal as its stdin, stdout and stderr.

        Like ``run_stdin`` it execs the argv directly, so the lease names the command
        itself. The terminal's output (stdout and stderr together) is the result's stdout.
        """
        attempt_id = None
        identity = None
        master, slave = pty.openpty()
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=cwd,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    start_new_session=True,
                )
            finally:
                os.close(slave)
            attempt_id, identity = self._publish_lease(proc, run_id)
            reader = asyncio.StreamReader(limit=STREAM_LINE_LIMIT)
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(master, "rb", 0)
            )
            master = -1  # the transport owns it now
            output_lines: list[str] = []
            completion_seen = False
            try:
                while True:
                    try:
                        line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                    except TimeoutError:
                        if proc.returncode is not None:
                            break
                        continue
                    except OSError:  # EIO: every end of the terminal is closed
                        break
                    if not line:
                        break
                    decoded = line.decode("utf-8", errors="replace")
                    output_lines.append(decoded)
                    if completion_signal and completion_signal in decoded:
                        completion_seen = True
            finally:
                transport.close()

            try:
                await asyncio.wait_for(proc.wait(), timeout=15.0 if completion_seen else timeout)
            except TimeoutError:
                await self._stop_process_group(proc, identity)

            returncode = proc.returncode or 0
            return ExecResult(
                returncode == 0,
                "".join(output_lines),
                "",
                returncode,
                killed=returncode < 0,
            )
        except Exception as exc:
            return ExecResult(False, stderr=str(exc), returncode=-1)
        finally:
            if master >= 0:
                os.close(master)
            self._clear_lease(run_id, attempt_id)
