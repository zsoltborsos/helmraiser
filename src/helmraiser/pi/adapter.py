"""``pi --mode json`` / opt-in ``rpc`` adapter (spec §3.1–3.5).

Builds the pi argv, streams the JSONL event stream through
:class:`~helmraiser.runner.engine.ExecutionEngine` (leases, timeouts and abort
are reused unchanged) and turns the events into a :class:`PiSessionResult`.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from helmraiser.isolation.environment import session_environment
from helmraiser.isolation.retry import SandboxLaunch
from helmraiser.leeboard import leeboard_identity
from helmraiser.pi.events import EventCollector, classify, final_text, parse_line
from helmraiser.pi.protocol import OnEvent, PiRequest, PiSessionResult
from helmraiser.runner.engine import ExecutionEngine, StdinHandle
from helmraiser.task.model import ModelSpec

ArgvWrapper = Callable[[list[str], Path, Path], list[str]]

LARGE_PROMPT_BYTES = 60 * 1024

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


class PiVersionError(RuntimeError):
    """``pi --version`` failed or printed no version."""


def session_id_for(run_id: str, stage: str, attempt: int = 1) -> str:
    """``<run-id>-<stage>[-<n>]`` (spec §0); the ``-<n>`` suffix only from attempt 2."""
    return f"{run_id}-{stage}" if attempt <= 1 else f"{run_id}-{stage}-{attempt}"


def pi_version(pi_bin: str = "pi") -> str:
    """Run ``pi --version`` and return the version string (e.g. ``0.87.1``). pi gets
    the session allowlist, as a pi session does."""
    try:
        proc = subprocess.run(
            [pi_bin, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=session_environment({}),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PiVersionError(f"{pi_bin} --version failed: {exc}") from exc
    match = _VERSION_RE.search(proc.stdout) if proc.returncode == 0 else None
    if match is None:
        detail = (proc.stderr or proc.stdout).strip() or f"exit status {proc.returncode}"
        raise PiVersionError(f"{pi_bin} --version gave no version: {detail}")
    return match.group(0)


def check_auth(model: ModelSpec, pi_bin: str = "pi") -> str | None:
    """``pi auth check --model <m> --json``; None when ready, else the reason.

    pi exits 0/1/2 for ready/not_ready/invalid and prints one JSON object. pi gets
    the session allowlist, as a pi session does.
    """
    try:
        proc = subprocess.run(
            [pi_bin, "auth", "check", "--model", model.model, "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env=session_environment({}),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{pi_bin} auth check failed: {exc}"
    if proc.returncode == 0:
        return None
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        payload = {}
    status = payload.get("status") or ("not_ready" if proc.returncode == 1 else "invalid")
    reason = payload.get("reason") or (proc.stderr or proc.stdout).strip()
    return f"{model.model}: {status}" + (f" ({reason})" if reason else "")


class PiJsonAdapter:
    def __init__(
        self,
        pi_bin: str = "pi",
        engine: ExecutionEngine | None = None,
        argv_wrapper: ArgvWrapper | None = None,
    ) -> None:
        self.pi_bin = pi_bin
        self.engine = engine or ExecutionEngine()
        # Isolation seam (spec §7): wraps the argv, e.g. ``srt --settings … pi …``.
        self.argv_wrapper = argv_wrapper

    # ── argv ────────────────────────────────────────────────────────────

    def build_argv(self, req: PiRequest, prompt_file: Path | None = None) -> list[str]:
        """The pi command for ``req``. Pure: a prompt that must travel by file is
        shown as ``@<path>`` but only ``run`` writes it (pass ``prompt_file``).
        RPC carries no positional prompt or prompt file."""
        argv = [
            self.pi_bin,
            "--mode",
            "rpc" if req.rpc else "json",
            "--session-id",
            req.session_id,
            "--session-dir",
            str(req.session_dir),
            "--model",
            req.model.render(),
        ]
        if req.system_prompt_path is not None:
            argv += ["--append-system-prompt", str(req.system_prompt_path)]
        argv += ["--no-extensions", "--no-prompt-templates", "--no-themes"]
        if req.tools:
            argv += ["--tools", ",".join(req.tools)]
        elif req.tools is not None:
            argv.append("--no-tools")
        if not req.context_files:
            argv.append("--no-context-files")
        argv += req.extra_args
        if not req.rpc:
            if prompt_file is not None:
                prompt_arg = f"@{prompt_file}"
            elif self._prompt_needs_file(req):
                prompt_arg = f"@{self._spill_path(req)}"
            else:
                prompt_arg = req.prompt
            argv += ["--", prompt_arg]
        if self.argv_wrapper is not None:
            argv = self.argv_wrapper(argv, req.cwd, req.run_dir)
        return argv

    def _prompt_needs_file(self, req: PiRequest) -> bool:
        # Wrapped (sandboxed) commands become one shell-quoted argument, and
        # quoting can push even a mid-sized prompt past srt's argv limit.
        return self.argv_wrapper is not None or len(req.prompt.encode("utf-8")) > LARGE_PROMPT_BYTES

    def _spill_path(self, req: PiRequest) -> Path:
        """First unused ``prompt-<stage>-<attempt>[-<n>].md`` in ``run_dir``.

        Session ids do not tell prompts apart (retries reuse one session id and
        review rounds all run with ``attempt`` 1), so the counter suffix keeps
        every prompt file instead of overwriting an earlier one.
        """
        candidate = req.run_dir / f"prompt-{req.stage}-{req.attempt}.md"
        counter = 1
        while candidate.exists():
            counter += 1
            candidate = req.run_dir / f"prompt-{req.stage}-{req.attempt}-{counter}.md"
        return candidate

    def environment(self, req: PiRequest, run_id: str) -> dict[str, str]:
        """The session's complete environment: the allowlist plus Helmraiser's own."""
        return session_environment(
            {
                "HELMRAISER_RUN_ID": run_id,
                "HELMRAISER_STAGE": req.stage,
                "HELMRAISER_EVIDENCE_DIR": str(req.run_dir / "evidence"),
                **leeboard_identity(run_id),
                **req.env,
            }
        )

    # ── run ─────────────────────────────────────────────────────────────

    async def run(self, req: PiRequest, on_event: OnEvent, run_id: str) -> PiSessionResult:
        prompt_file: Path | None = None
        if not req.rpc and self._prompt_needs_file(req):
            prompt_file = self._spill_path(req)
            prompt_file.parent.mkdir(parents=True, exist_ok=True)
            prompt_file.write_text(req.prompt, encoding="utf-8")
        started = time.monotonic()
        for attempt in range(2):
            argv = self.build_argv(req, prompt_file)
            collector = EventCollector()
            stdin_handle = StdinHandle() if req.rpc else None
            rpc_error: str | None = None
            saw_stdout = False
            input_text = ""
            if req.rpc:
                input_text = (
                    json.dumps({"id": "prompt", "type": "prompt", "message": req.prompt}) + "\n"
                )

            async def on_line(
                line: str,
                collector: EventCollector = collector,
                stdin_handle: StdinHandle | None = stdin_handle,
            ) -> None:
                nonlocal rpc_error, saw_stdout
                saw_stdout = True
                if req.rpc:
                    record = parse_line(line)
                    if record.type == "response":
                        if record.raw.get("success") is False:
                            rpc_error = (
                                f"pi RPC command failed: {record.raw.get('error', 'unknown error')}"
                            )
                            assert stdin_handle is not None
                            stdin_handle.close()
                        return
                event = collector.feed(line)
                if event is not None:
                    try:
                        await on_event(event)
                    finally:
                        if stdin_handle is not None and event.type == "agent_settled":
                            stdin_handle.close()

            terminal_mount_failure = False
            try:
                remaining = (
                    req.timeout_s
                    if attempt == 0
                    else max(0.0, req.timeout_s - (time.monotonic() - started))
                )
                exec_result = await self.engine.run_stdin(
                    argv.guarded_argv() if isinstance(argv, SandboxLaunch) else argv,
                    input=input_text,
                    # ExecutionEngine delegates to asyncio.timeout, which accepts fractions.
                    timeout=remaining,  # type: ignore[arg-type]
                    cwd=str(req.cwd),
                    on_line=on_line,
                    run_id=run_id,
                    capture_stdout=False,
                    env=self.environment(req, run_id),
                    **({"stdin_handle": stdin_handle} if req.rpc else {}),
                )
                if isinstance(argv, SandboxLaunch):
                    state = {"timed_out": exec_result.timed_out, "killed": exec_result.killed}
                    path = argv.retry_path(
                        exec_result.returncode, saw_stdout, exec_result.stderr, **state
                    )
                    if attempt == 0 and path is not None:
                        argv.record_retry(path)
                        continue
                    terminal_mount_failure = (
                        attempt == 1
                        and argv.mount_failure_path(
                            exec_result.returncode, saw_stdout, exec_result.stderr, **state
                        )
                        is not None
                    )
            finally:
                if isinstance(argv, SandboxLaunch):
                    argv.cleanup()
            ok, error_kind, error_message, stop_reason = classify(collector.events, exec_result)
            if rpc_error is not None:
                ok, error_kind, error_message = False, "protocol", rpc_error
            if terminal_mount_failure:
                ok, error_kind, error_message = False, "sandbox", exec_result.stderr
            if error_kind == "protocol" and collector.garbage and error_message:
                error_message = f"{error_message}\n[stdout] " + "\n".join(collector.garbage[-5:])
            return PiSessionResult(
                ok=ok,
                session_id=req.session_id,
                final_text=final_text(collector.events),
                stop_reason=stop_reason,
                error_kind=error_kind,
                error_message=error_message,
                usage=collector.usage,
                tool_calls=collector.tool_calls,
                returncode=exec_result.returncode,
                timed_out=exec_result.timed_out,
                killed=exec_result.killed,
            )
        raise AssertionError("sandbox launch attempts exhausted")
