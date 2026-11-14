"""Test the fake RPC personality directly, independently of the adapter."""

import asyncio
import json
import time

import pytest


async def _exchange(fake_pi, *, close_early=False):
    proc = await asyncio.create_subprocess_exec(
        str(fake_pi.bin),
        "--mode",
        "rpc",
        "--session-id",
        "test-implement",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    command = {"id": "prompt", "type": "prompt", "message": "Hello\né\u2028world"}
    line = json.dumps(command) + "\n"
    events = []
    assert proc.stdin and proc.stdout
    try:
        proc.stdin.write(line.encode())
        await proc.stdin.drain()
        if close_early:
            proc.stdin.close()
        while True:
            record = json.loads(await asyncio.wait_for(proc.stdout.readline(), 5))
            events.append(record)
            if record["type"] == "agent_settled" or (
                record["type"] == "response" and record["success"] is False
            ):
                break
            # Non-settling scripts can still be exercised without waiting forever.
            if record["type"] == "agent_end" and not _spec_settles(fake_pi):
                break
        if not close_early:
            await asyncio.sleep(0.05)
            assert proc.returncode is None  # stdin is still held open
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), 5)
        assert await proc.stderr.read() == b""
        return events, proc.returncode, line
    finally:
        proc.stdin.close()
        if proc.returncode is None:
            proc.kill()  # only this test's child, never a host process group
        await proc.wait()


def _spec_settles(fake_pi):
    return json.loads(fake_pi.script.read_text()).get("default", {}).get("settle", True)


@pytest.mark.parametrize("close_early", [False, True])
async def test_rpc_prompt_events_and_eof(fake_pi, close_early):
    scripted = [{"type": "agent_start"}, {"type": "agent_settled"}]
    fake_pi.write_script({"default": {"events": scripted}})
    events, code, line = await _exchange(fake_pi, close_early=close_early)
    assert events[0] == {
        "id": "prompt",
        "type": "response",
        "command": "prompt",
        "success": True,
    }
    assert events[1:] == scripted
    assert code == 0
    [call] = fake_pi.calls()
    assert call["stdin"] == line
    records = [json.loads(row) for row in fake_pi.log.read_text().splitlines()]
    assert records[-1] == {"pid": call["pid"], "eof_before_settle": close_early}


async def test_rpc_rejection(fake_pi):
    fake_pi.write_script({"default": {"rpc_reject": "bad prompt"}})
    events, code, _ = await _exchange(fake_pi)
    assert code == 0
    assert len(events) == 1
    assert events[0]["success"] is False
    assert events[0]["error"] == "bad prompt"


async def test_non_settling_sleep_and_exit_code(fake_pi):
    fake_pi.write_script({"default": {"settle": False, "sleep": 0.1, "exit_code": 7}})
    started = time.monotonic()
    events, code, _ = await _exchange(fake_pi)
    assert time.monotonic() - started >= 0.1
    assert code == 7
    assert events[-1]["type"] == "agent_end"
    assert not any(e["type"] in {"session", "agent_settled"} for e in events)


@pytest.mark.parametrize("key", ["test-implement", "*-implement"])
async def test_diagnostics_do_not_affect_invocation_selection(fake_pi, key):
    fake_pi.write_script({"sessions": {key: [{"text": "first"}, {"text": "second"}]}})
    for expected in ["first", "second", "second"]:
        events, code, _ = await _exchange(fake_pi)
        assert code == 0
        assistant = next(e["message"] for e in events if e["type"] == "turn_end")
        assert assistant["content"][0]["text"] == expected
    assert [call["invocation"] for call in fake_pi.calls()] == [0, 1, 2]
    assert len(fake_pi.log.read_text().splitlines()) == 6
