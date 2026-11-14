"""End-to-end through the scripted fake ``pi``: streaming, leases, timeout, abort race.

No network, no real model: ``pi`` on PATH is ``tests/mocks/fake_pi.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from helmraiser.isolation.process import ProcessIsolation
from helmraiser.pi.adapter import PiJsonAdapter, PiVersionError, pi_version
from helmraiser.pi.protocol import PiEvent, PiRequest
from helmraiser.runner.engine import ExecutionEngine
from helmraiser.runner.process_control import ProcessIdentity
from helmraiser.storage.state import StateManager
from helmraiser.task.model import ModelSpec
from tests.mocks.fake_srt import FakeSrt, unwrap_guard
from tests.pi.conftest import FakePi, SingleProcessController

RUN_ID = "0b1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
SESSION = f"{RUN_ID}-implement"


def _request(tmp_path: Path, **overrides) -> PiRequest:
    run_dir = tmp_path / "runs" / RUN_ID
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    (run_dir / "sessions").mkdir(parents=True, exist_ok=True)
    (run_dir / "directives.md").write_text("# directives\n")
    fields = {
        "prompt": "Implement the thing.",
        "session_id": SESSION,
        "model": ModelSpec("openai-codex/gpt-6.1-sol", "medium"),
        "cwd": worktree,
        "session_dir": run_dir / "sessions",
        "system_prompt_path": run_dir / "directives.md",
        "timeout_s": 30,
    }
    fields.update(overrides)
    return PiRequest(**fields)


def _adapter(state: StateManager | None, controller: SingleProcessController) -> PiJsonAdapter:
    return PiJsonAdapter(engine=ExecutionEngine(state, controller))


def _active_process(state: StateManager, run_id: str) -> dict | None:
    row = (
        state._get_conn()
        .execute("SELECT * FROM active_processes WHERE run_id = ?", (run_id,))
        .fetchone()
    )
    return dict(row) if row else None


class _Recorder:
    def __init__(self) -> None:
        self.events: list[PiEvent] = []

    async def __call__(self, event: PiEvent) -> None:
        self.events.append(event)


@pytest.mark.parametrize("rpc", [False, True])
@pytest.mark.parametrize("failures", [1, 2])
async def test_sandbox_start_retry(tmp_path, fake_pi, process_controller, rpc, failures):
    req = _request(tmp_path, rpc=rpc)
    sibling = req.run_dir.parent / "vanished sibling"
    sibling.mkdir()
    fake = FakeSrt(tmp_path / "srt-bin", sibling, failures=failures)
    isolation = ProcessIsolation(
        {"isolation": {"srt": str(fake.bin), "tmpdir": str(tmp_path / "temp")}},
        data_dir=req.run_dir.parent.parent,
    )
    fake_pi.write_script({"default": {"text": "done", "cost": 0.25}})
    state = StateManager(tmp_path / "state.db")
    state.create_run(RUN_ID, "task")
    adapter = _adapter(state, process_controller)
    adapter.argv_wrapper = isolation.wrap
    recorder = _Recorder()
    result = await adapter.run(req, recorder, RUN_ID)
    calls = fake.calls()
    assert len(calls) == 2
    assert str(sibling) in calls[0]["settings"]["filesystem"]["denyRead"]
    assert str(sibling) not in calls[1]["settings"]["filesystem"]["denyRead"]
    payloads = [unwrap_guard(call["argv"][3:]) for call in calls]
    assert payloads[0] == payloads[1]
    assert len(list(req.run_dir.glob("prompt-*.md"))) == (0 if rpc else 1)
    assert all(not Path(call["argv"][7]).exists() for call in calls)
    assert _active_process(state, RUN_ID) is None
    [event] = [json.loads(line) for line in (req.run_dir / "events.jsonl").read_text().splitlines()]
    assert event["event_type"] == "sandbox_retry" and event["kind"] == "session"
    if failures == 1:
        assert result.ok
        assert len(fake_pi.calls()) == 1
        assert result.usage.cost_usd == 0.25
        assert [e.type for e in recorder.events].count("agent_settled") == 1
    else:
        assert not result.ok and result.error_kind == "sandbox"
        assert str(sibling) in result.error_message
        assert fake_pi.calls() == []
        assert recorder.events == []


@pytest.mark.parametrize("rpc", [False, True])
async def test_started_pi_failure_not_startup_retried(tmp_path, fake_pi, process_controller, rpc):
    req = _request(tmp_path, rpc=rpc)
    sibling = req.run_dir.parent / "gone"
    fake = FakeSrt(tmp_path / "srt-bin", sibling, failures=0)
    isolation = ProcessIsolation(
        {"isolation": {"srt": str(fake.bin), "tmpdir": str(tmp_path / "temp")}}
    )
    adapter = _adapter(None, process_controller)
    adapter.argv_wrapper = isolation.wrap
    fake_pi.write_script(
        {
            "default": {
                "exit_code": 7,
                "settle": False,
                "stderr": f"bwrap: Can't create file {sibling}: Read-only file system\n",
            }
        }
    )
    result = await adapter.run(req, _Recorder(), RUN_ID)
    assert not result.ok
    assert len(fake.calls()) == len(fake_pi.calls()) == 1
    assert not (req.run_dir / "events.jsonl").exists()
    assert not list(isolation.tmpdir.glob("*.marker"))


async def test_streams_events_and_builds_result(
    tmp_path: Path, fake_pi: FakePi, process_controller: SingleProcessController
):
    fake_pi.write_script(
        {
            "sessions": {
                SESSION: {
                    "text": "All done.",
                    "cost": 0.0123,
                    "tokens_in": 1200,
                    "tokens_out": 34,
                    "tools": ["bash", "edit"],
                    "files": [["src/new.txt", "hello\n"]],
                }
            }
        }
    )
    state = StateManager(tmp_path / "state.db")
    state.create_run(RUN_ID, "task")
    recorder = _Recorder()
    req = _request(tmp_path)

    result = await _adapter(state, process_controller).run(req, recorder, RUN_ID)

    assert result.ok is True
    assert result.error_kind is None
    assert result.stop_reason == "stop"
    assert result.final_text == "All done."
    assert result.usage.cost_usd == pytest.approx(0.0123)
    assert (result.usage.tokens_in, result.usage.tokens_out) == (1200, 34)
    assert result.tool_calls == 2
    assert result.returncode == 0 and not result.timed_out and not result.killed
    assert result.session_id == SESSION

    types = [e.type for e in recorder.events]
    assert types[0] == "session"
    assert recorder.events[0].raw["id"] == SESSION
    assert "agent_start" in types and "agent_settled" in types
    assert types.count("tool_execution_start") == 2
    assert "message_update" not in types  # deltas dropped
    assert types[-1] == "agent_settled"
    # Side effect landed in the worktree (cwd).
    assert (req.cwd / "src" / "new.txt").read_text() == "hello\n"

    [call] = fake_pi.calls()
    argv = call["argv"]
    assert argv[:2] == ["--mode", "json"]
    assert argv[argv.index("--session-id") + 1] == SESSION
    assert argv[argv.index("--session-dir") + 1] == str(req.session_dir)
    assert argv[argv.index("--model") + 1] == "openai-codex/gpt-6.1-sol:medium"
    assert argv[-2:] == ["--", "Implement the thing."]
    assert call["stdin"] == ""  # prompt goes as the positional, not via stdin
    assert call["cwd"] == str(req.cwd)
    assert call["env"]["HELMRAISER_RUN_ID"] == RUN_ID
    assert call["env"]["HELMRAISER_STAGE"] == "implement"
    assert call["env"]["HELMRAISER_EVIDENCE_DIR"] == str(req.run_dir / "evidence")


@pytest.mark.parametrize("wrapped", [False, True])
async def test_rpc_round_trip_matches_json(
    tmp_path: Path, fake_pi: FakePi, process_controller: SingleProcessController, wrapped
):
    fake_pi.write_script(
        {
            "default": {
                "text": "All done.",
                "cost": 0.25,
                "tokens_in": 120,
                "tokens_out": 34,
                "tools": ["bash", "edit"],
            }
        }
    )
    adapter = _adapter(None, process_controller)
    json_events = _Recorder()
    json_result = await adapter.run(_request(tmp_path), json_events, RUN_ID)
    if wrapped:
        wrapper = tmp_path / "exec-wrapper"
        wrapper.write_text('#!/bin/sh\nexec "$@"\n')
        wrapper.chmod(0o755)
        adapter.argv_wrapper = lambda argv, cwd, run_dir: [str(wrapper), *argv]
    prompt = "Implement\né\u2028\u2029" + "x" * 100_000
    req = _request(tmp_path, rpc=True, prompt=prompt)
    rpc_events = _Recorder()
    rpc_result = await adapter.run(req, rpc_events, RUN_ID)
    assert rpc_result == json_result
    assert [e.type for e in rpc_events.events] == [e.type for e in json_events.events[1:]]
    assert not list(req.run_dir.glob("prompt-*.md"))
    calls = fake_pi.calls()
    assert json.loads(calls[-1]["stdin"]) == {
        "id": "prompt",
        "type": "prompt",
        "message": prompt,
    }
    assert calls[-1]["stdin"].endswith("\n")
    assert calls[-1]["argv"][:2] == ["--mode", "rpc"]
    assert "--" not in calls[-1]["argv"]
    records = [json.loads(line) for line in fake_pi.log.read_text().splitlines()]
    assert records[-1] == {"pid": calls[-1]["pid"], "eof_before_settle": False}


async def test_rpc_rejected_prompt_is_protocol(
    tmp_path: Path, fake_pi: FakePi, process_controller: SingleProcessController
):
    fake_pi.write_script({"default": {"rpc_reject": "no thanks"}})
    recorder = _Recorder()
    started = time.monotonic()
    result = await _adapter(None, process_controller).run(
        _request(tmp_path, rpc=True), recorder, RUN_ID
    )
    assert time.monotonic() - started < 5
    assert (result.ok, result.error_kind, result.returncode) == (False, "protocol", 0)
    assert "no thanks" in result.error_message
    assert recorder.events == []


async def test_rpc_closes_stdin_when_settled_callback_raises(
    tmp_path: Path, fake_pi: FakePi, process_controller: SingleProcessController
):
    seen = []

    async def on_event(event):
        seen.append(event.type)
        if event.type == "agent_settled":
            raise RuntimeError("heartbeat failed")

    result = await _adapter(None, process_controller).run(
        _request(tmp_path, rpc=True, timeout_s=2), on_event, RUN_ID
    )
    assert result.ok
    assert seen[-1] == "agent_settled"
    assert "response" not in seen


@pytest.mark.parametrize("rpc", [False, True])
async def test_lease_published_during_run_and_cleared_after(
    tmp_path: Path, fake_pi: FakePi, process_controller: SingleProcessController, rpc
):
    fake_pi.write_script({"sessions": {SESSION: {"text": "ok"}}})
    state = StateManager(tmp_path / "state.db")
    state.create_run(RUN_ID, "task")
    seen: list[dict | None] = []

    async def on_event(event: PiEvent) -> None:
        if event.type == "agent_start":
            seen.append(_active_process(state, RUN_ID))

    result = await _adapter(state, process_controller).run(
        _request(tmp_path, rpc=rpc), on_event, RUN_ID
    )

    assert result.ok
    assert len(seen) == 1 and seen[0] is not None
    lease = seen[0]
    [call] = fake_pi.calls()
    # The pi process itself is the session leader the lease points at (no shell in between).
    assert lease["leader_pid"] == lease["pgid"] == call["pid"] > 1
    assert _active_process(state, RUN_ID) is None
    assert state.get_run(RUN_ID)["status"] == "running"
    assert process_controller.terminated == []


@pytest.mark.parametrize("rpc", [False, True])
async def test_timeout_terminates_process_group(
    tmp_path: Path, fake_pi: FakePi, process_controller: SingleProcessController, rpc
):
    fake_pi.write_script(
        {
            "sessions": {
                SESSION: {
                    "text": "working...",
                    "sleep": 20,
                    "settle": not rpc,
                }
            }
        }
    )
    state = StateManager(tmp_path / "state.db")
    state.create_run(RUN_ID, "task")
    recorder = _Recorder()
    started = time.monotonic()

    result = await _adapter(state, process_controller).run(
        _request(tmp_path, timeout_s=1, rpc=rpc), recorder, RUN_ID
    )

    assert time.monotonic() - started < 10
    assert result.timed_out and result.killed
    assert (result.ok, result.error_kind) == (False, "timeout")
    assert result.returncode == -1
    [identity] = process_controller.terminated
    assert process_controller.killed == []
    with pytest.raises(ProcessLookupError):
        os.kill(identity.leader_pid, 0)
    # Events emitted before the hang were still streamed; lease is gone.
    assert ("agent_settled" in [e.type for e in recorder.events]) is not rpc
    assert _active_process(state, RUN_ID) is None


async def test_rpc_prompt_delivery_timeout_when_child_never_reads_stdin(
    tmp_path: Path, process_controller: SingleProcessController, monkeypatch
):
    # A stalled pi/wrapper fills the pipe and blocks drain() before any events.
    stalled_pi = tmp_path / "stalled-pi"
    stalled_pi.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    stalled_pi.chmod(0o755)
    state = StateManager(tmp_path / "state.db")
    state.create_run(RUN_ID, "task")
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def record_spawn(*args, **kwargs):
        proc = await spawn(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record_spawn)
    adapter = PiJsonAdapter(
        pi_bin=str(stalled_pi), engine=ExecutionEngine(state, process_controller)
    )
    recorder = _Recorder()
    req = _request(tmp_path, rpc=True, prompt="x" * (8 * 1024 * 1024), timeout_s=1)
    started = time.monotonic()
    try:
        # Bound the regression itself: without the engine deadline, drain hangs.
        result = await asyncio.wait_for(adapter.run(req, recorder, RUN_ID), timeout=5)
        assert time.monotonic() - started < 5
        assert (result.ok, result.error_kind) == (False, "timeout")
        assert result.timed_out and result.killed and result.returncode == -1
        [proc] = processes
        [identity] = process_controller.terminated
        assert identity.leader_pid == proc.pid
        assert process_controller.killed == []
        assert proc.returncode is not None
        assert proc.stdin.is_closing()
        assert _active_process(state, RUN_ID) is None
        assert recorder.events == []
    finally:
        # Cancellation in the unfixed engine does not stop the child; never leak it.
        for proc in processes:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()


@pytest.mark.parametrize("rpc", [False, True])
async def test_spawn_loses_abort_race(
    tmp_path: Path, fake_pi: FakePi, process_controller: SingleProcessController, rpc
):
    fake_pi.write_script({"sessions": {SESSION: {"text": "never mind", "sleep": 20}}})
    state = StateManager(tmp_path / "state.db")
    state.create_run(RUN_ID, "task")
    state.request_abort(RUN_ID)
    recorder = _Recorder()

    result = await _adapter(state, process_controller).run(
        _request(tmp_path, rpc=rpc), recorder, RUN_ID
    )

    assert (result.ok, result.error_kind) == (False, "aborted")
    assert "run is aborting" in (result.error_message or "")
    [identity] = process_controller.terminated
    assert _active_process(state, RUN_ID) is None
    assert state.get_run(RUN_ID)["status"] == "aborting"
    # Let the loop reap the terminated child; then it must be gone.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(identity.leader_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("aborted pi process still alive")


@pytest.mark.parametrize("rpc", [False, True])
async def test_active_lease_abort(
    tmp_path: Path, fake_pi: FakePi, process_controller: SingleProcessController, rpc
):
    fake_pi.write_script({"default": {"settle": False, "sleep": 20}})
    state = StateManager(tmp_path / "state.db")
    state.create_run(RUN_ID, "task")
    seen = []

    async def on_event(event):
        if event.type == "agent_start":
            abort = state.request_abort(RUN_ID)
            assert abort.lease is not None
            identity = ProcessIdentity(
                abort.lease.leader_pid, abort.lease.pgid, abort.lease.process_start_token
            )
            assert process_controller.is_running(identity)
            seen.append(identity)
            process_controller.terminate(identity)

    result = await _adapter(state, process_controller).run(
        _request(tmp_path, rpc=rpc), on_event, RUN_ID
    )
    assert (result.ok, result.error_kind) == (False, "aborted")
    assert process_controller.terminated == seen and len(seen) == 1
    assert _active_process(state, RUN_ID) is None
    assert len(fake_pi.calls()) == 1
    if rpc:
        assert json.loads(fake_pi.calls()[0]["stdin"])["type"] == "prompt"


async def test_same_session_id_continues_by_invocation_count(
    tmp_path: Path, fake_pi: FakePi, process_controller: SingleProcessController
):
    fake_pi.write_script(
        {
            "sessions": {
                SESSION: [{"text": "first try", "cost": 0.1}, {"text": "fixed", "cost": 0.2}]
            }
        }
    )
    adapter = _adapter(None, process_controller)
    recorder = _Recorder()

    first = await adapter.run(_request(tmp_path), recorder, RUN_ID)
    second = await adapter.run(_request(tmp_path, prompt="verify failed: ..."), recorder, RUN_ID)

    assert (first.final_text, second.final_text) == ("first try", "fixed")
    calls = fake_pi.calls()
    assert [c["invocation"] for c in calls] == [0, 1]
    assert calls[1]["argv"][-1] == "verify failed: ..."
    assert {c["argv"][c["argv"].index("--session-id") + 1] for c in calls} == {SESSION}


@pytest.mark.parametrize("rpc", [False, True])
@pytest.mark.parametrize(
    "error,kind", [("429 rate limit", "quota"), ("connection failed", "provider")]
)
async def test_error_stop_reason_is_classified_not_exit_code(
    tmp_path: Path, fake_pi: FakePi, process_controller: SingleProcessController, rpc, error, kind
):
    fake_pi.write_script(
        {"sessions": {SESSION: {"text": "", "stop_reason": "error", "error_message": error}}}
    )
    result = await _adapter(None, process_controller).run(
        _request(tmp_path, rpc=rpc), _Recorder(), RUN_ID
    )
    assert result.returncode == 0  # both transports exit 0 even on assistant errors
    assert (result.ok, result.error_kind, result.error_message) == (
        False,
        kind,
        error,
    )


async def test_crash_without_settled_is_protocol_with_stderr(
    tmp_path: Path, fake_pi: FakePi, process_controller: SingleProcessController
):
    fake_pi.write_script(
        {
            "sessions": {
                SESSION: {
                    "events": [{"type": "agent_start"}],
                    "raw_lines": ["not json at all"],
                    "stderr": "Error: auth failed for openai-codex\n",
                    "exit_code": 1,
                }
            }
        }
    )
    recorder = _Recorder()
    result = await _adapter(None, process_controller).run(_request(tmp_path), recorder, RUN_ID)
    assert (result.ok, result.error_kind) == (False, "protocol")
    assert "auth failed" in (result.error_message or "")
    assert "not json at all" in (result.error_message or "")
    assert [e.type for e in recorder.events] == ["session", "agent_start"]


def test_pi_version_through_fake(fake_pi: FakePi):
    assert pi_version("pi") == "0.87.1"
    fake_pi.write_script({"version": "0.99.2"})
    assert pi_version("pi") == "0.99.2"
    fake_pi.write_script({"version": ""})
    with pytest.raises(PiVersionError, match="gave no version"):
        pi_version("pi")


def test_pi_version_missing_binary(tmp_path: Path):
    with pytest.raises(PiVersionError):
        pi_version(str(tmp_path / "no-such-pi"))


@pytest.mark.parametrize("rpc", [False, True])
async def test_records_longer_than_64k_stream_through(
    tmp_path: Path, fake_pi: FakePi, process_controller: SingleProcessController, rpc
):
    """A ``turn_end`` carries every tool result of the turn; the dogfood run hit
    asyncio's 64 KiB readline limit and was misreported as aborted."""
    big = "x" * 200_000
    assistant = {
        "role": "assistant",
        "content": [{"type": "text", "text": "done"}],
        "stopReason": "stop",
        "usage": {"input": 1, "output": 1, "cost": {"total": 0.001}},
    }
    fake_pi.write_script(
        {
            "sessions": {
                SESSION: {
                    "events": [
                        {"type": "agent_start"},
                        {"type": "turn_end", "message": assistant, "toolResults": [big]},
                        {"type": "message_end", "message": assistant},
                        {"type": "agent_settled"},
                    ]
                }
            }
        }
    )
    recorder = _Recorder()
    result = await _adapter(None, process_controller).run(
        _request(tmp_path, rpc=rpc), recorder, RUN_ID
    )
    assert (result.ok, result.error_kind) == (True, None), result.error_message
    assert result.returncode == 0
    turn_end = next(e for e in recorder.events if e.type == "turn_end")
    assert turn_end.raw["toolResults"] == [big]


@pytest.mark.parametrize("rpc", [False, True])
async def test_assistant_message_without_usage_fails_closed(
    tmp_path: Path, fake_pi: FakePi, process_controller: SingleProcessController, rpc
):
    """A pi whose JSON drops ``usage`` would make every session look free; the
    session fails as ``contract`` so budget caps are never silently skipped."""
    assistant = {
        "role": "assistant",
        "content": [{"type": "text", "text": "done"}],
        "stopReason": "stop",
    }
    fake_pi.write_script(
        {
            "sessions": {
                SESSION: {
                    "events": [
                        {"type": "agent_start"},
                        {"type": "message_end", "message": assistant},
                        {"type": "agent_settled"},
                    ]
                }
            }
        }
    )
    result = await _adapter(None, process_controller).run(
        _request(tmp_path, rpc=rpc), _Recorder(), RUN_ID
    )
    assert (result.ok, result.error_kind) == (False, "contract")
    assert "no readable usage" in (result.error_message or "")
