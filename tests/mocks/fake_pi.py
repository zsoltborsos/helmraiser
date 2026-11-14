#!/usr/bin/env python3
"""Scripted stand-in for the ``pi`` CLI, installed as ``pi`` on PATH in tests.

Driven by ``FAKE_PI_SCRIPT`` (JSON file) and logs every invocation to
``FAKE_PI_LOG`` (JSONL: argv, cwd, session id, stdin, HELMRAISER_* env plus any
names listed under the script's top-level ``log_env``).

Script shape::

    {
      "version": "0.87.1",                      # reply to --version
      "sessions": {
        "<session-id>": {...spec...} | [{...spec 1...}, {...spec 2...}],
        "*-review": {...spec...},               # fnmatch patterns are accepted
        "*-intake": [{...first draft...}, {...second draft...}],
      },
      "default": {...spec...}                   # when the session id is unknown
    }

A list scripts successive invocations of the same session id (retry with
feedback); the last entry repeats once exhausted. Invocation counts come from
the log file, so the script file itself is never mutated.

Spec keys (all optional):

- ``text`` final assistant text; ``stop_reason`` (default ``stop``);
  ``error_message``; ``cost`` (USD); ``tokens_in``/``tokens_out``;
  ``tools`` list of tool names (one start/end pair each); ``settle`` (default
  true, emit ``agent_settled``);
- ``events`` raw records emitted instead of the synthesized sequence (the
  session header is still emitted first unless ``header`` is false);
  ``raw_lines`` strings written verbatim after the header (garbage tests);
- ``files`` ``[[relative_path, content], ...]`` written in cwd; ``commit``
  message for ``git add -A && git commit``; ``stderr`` text written to stderr;
  ``sleep`` seconds to sleep after emitting (timeout tests); ``exit_code``.
- ``wait_for_sessions`` barrier: ``{pattern, count, timeout}``; wait for matching
  logged calls before emitting anything; timeout fails instead of hanging.

With ``--mode rpc``, read and log one prompt command, reply with a response,
emit events without a session header, then wait for stdin EOF. ``rpc_reject``
rejects the command with the supplied error. Append-only EOF diagnostics have
no ``argv`` and do not count as invocations.

Event shapes follow pi's ``docs/json.md``.
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import os
import select
import subprocess
import sys
import time
from typing import Any


def _emit(record: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def _parse_argv(argv: list[str]) -> dict[str, Any]:
    opts: dict[str, Any] = {"session_id": None, "model": None, "prompt": None, "mode": "json"}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            opts["prompt"] = " ".join(argv[i + 1 :])
            break
        if arg == "--mode" and i + 1 < len(argv):
            opts["mode"] = argv[i + 1]
            i += 2
            continue
        if arg == "--session-id" and i + 1 < len(argv):
            opts["session_id"] = argv[i + 1]
            i += 2
            continue
        if arg == "--model" and i + 1 < len(argv):
            opts["model"] = argv[i + 1]
            i += 2
            continue
        i += 1
    return opts


def _previous_invocations(
    log_path: str | None, session_id: str | None, pattern: str | None = None
) -> int:
    if not log_path or not os.path.exists(log_path):
        return 0
    count = 0
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                record = json.loads(line)
                if "argv" not in record:
                    continue
                previous = record.get("session_id")
                if (pattern is not None and fnmatch.fnmatchcase(previous or "", pattern)) or (
                    pattern is None and previous == session_id
                ):
                    count += 1
            except ValueError:
                continue
    return count


def _select(
    script: dict[str, Any],
    session_id: str | None,
    invocation: int,
    log_path: str | None,
) -> dict[str, Any]:
    sessions = script.get("sessions", {})
    entry = sessions.get(session_id)
    if entry is None and session_id is not None:
        for pattern, candidate in sessions.items():
            if fnmatch.fnmatchcase(session_id, pattern):
                entry = candidate
                if isinstance(entry, list):
                    # The current call is logged before selection; exclude it.
                    invocation = max(0, _previous_invocations(log_path, session_id, pattern) - 1)
                break
    if entry is None:
        entry = script.get("default", {})
    if isinstance(entry, list):
        if not entry:
            return {}
        return dict(entry[min(invocation, len(entry) - 1)])
    return dict(entry)


def _usage(spec: dict[str, Any]) -> dict[str, Any]:
    cost = float(spec.get("cost", 0.0))
    tokens_in = int(spec.get("tokens_in", 0))
    tokens_out = int(spec.get("tokens_out", 0))
    return {
        "input": tokens_in,
        "output": tokens_out,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": tokens_in + tokens_out,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": cost},
    }


def _synthesize(spec: dict[str, Any], opts: dict[str, Any]) -> list[dict[str, Any]]:
    now_ms = int(time.time() * 1000)
    text = str(spec.get("text", "done"))
    stop_reason = str(spec.get("stop_reason", "stop"))
    user = {"role": "user", "content": opts.get("prompt") or "", "timestamp": now_ms}
    assistant: dict[str, Any] = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}] if text else [],
        "api": "openai-responses",
        "provider": "openai-codex",
        "model": opts.get("model") or "fake/model",
        "usage": _usage(spec),
        "stopReason": stop_reason,
        "timestamp": now_ms,
    }
    if spec.get("error_message"):
        assistant["errorMessage"] = str(spec["error_message"])
    events: list[dict[str, Any]] = [
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_start", "message": user},
        {"type": "message_end", "message": user},
        {
            "type": "message_start",
            "message": {**assistant, "content": [], "stopReason": "pending"},
        },
    ]
    for n, tool in enumerate(spec.get("tools", []), start=1):
        call_id = f"call_{n}"
        events.append(
            {"type": "tool_execution_start", "toolCallId": call_id, "toolName": tool, "args": {}}
        )
        events.append(
            {
                "type": "tool_execution_end",
                "toolCallId": call_id,
                "toolName": tool,
                "result": {"content": [{"type": "text", "text": "ok"}], "details": {}},
                "isError": False,
            }
        )
    if text:
        events.append(
            {
                "type": "message_update",
                "usage": _usage(spec),
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "contentIndex": 0,
                    "delta": text[: max(1, len(text) // 2)],
                },
            }
        )
    events.append({"type": "message_end", "message": assistant})
    events.append({"type": "turn_end", "message": assistant, "toolResults": []})
    events.append({"type": "agent_end", "messages": [assistant], "willRetry": False})
    if spec.get("settle", True):
        events.append({"type": "agent_settled"})
    return events


def main(argv: list[str]) -> int:
    log_path = os.environ.get("FAKE_PI_LOG")
    script_path = os.environ.get("FAKE_PI_SCRIPT")
    script: dict[str, Any] = {}
    if script_path and os.path.exists(script_path):
        with open(script_path, encoding="utf-8") as fh:
            script = json.load(fh)

    if "--version" in argv or "-v" in argv:
        print(script.get("version", "0.87.1"))
        return 0
    if argv[:2] == ["auth", "check"]:
        # ``auth`` in the script: "ready" (default) | "not_ready" | "invalid".
        status = str(script.get("auth", "ready"))
        model = argv[argv.index("--model") + 1] if "--model" in argv else None
        provider = model.split("/", 1)[0] if model else None
        print(json.dumps({"status": status, "provider": provider}))
        return {"ready": 0, "not_ready": 1}.get(status, 2)

    opts = _parse_argv(argv)
    rpc = opts["mode"] == "rpc"
    if rpc:
        stdin_text = sys.stdin.readline()
        command = json.loads(stdin_text)
        opts["prompt"] = command["message"]
    else:
        stdin_text = "" if sys.stdin.isatty() else sys.stdin.read()
    invocation = _previous_invocations(log_path, opts["session_id"])
    logged_env = set(script.get("log_env", []))
    if log_path:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "argv": argv,
                        "pid": os.getpid(),
                        "cwd": os.getcwd(),
                        "session_id": opts["session_id"],
                        "invocation": invocation,
                        "stdin": stdin_text,
                        "env": {
                            k: v
                            for k, v in os.environ.items()
                            if k.startswith("HELMRAISER_") or k in logged_env
                        },
                    }
                )
                + "\n"
            )

    spec = _select(script, opts["session_id"], invocation, log_path)
    barrier = spec.get("wait_for_sessions")
    if barrier:
        deadline = time.monotonic() + float(barrier.get("timeout", 15))
        while _previous_invocations(log_path, None, barrier["pattern"]) < int(barrier["count"]):
            if time.monotonic() >= deadline:
                sys.stderr.write("fake pi session barrier timed out\n")
                return 1
            time.sleep(0.02)

    if rpc:
        rejected = "rpc_reject" in spec
        response = {
            "type": "response",
            "id": command.get("id"),
            "command": command.get("type"),
            "success": not rejected,
        }
        if rejected:
            response["error"] = spec["rpc_reject"]
        _emit(response)
        if rejected:
            sys.stdin.read()
            return int(spec.get("exit_code", 0))

    if not rpc and spec.get("header", True):
        _emit(
            {
                "type": "session",
                "version": 3,
                "id": opts["session_id"] or "fake-session",
                "timestamp": _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z"),
                "cwd": os.getcwd(),
            }
        )
    for raw in spec.get("raw_lines", []):
        sys.stdout.write(str(raw) + "\n")
        sys.stdout.flush()
    events = spec["events"] if "events" in spec else _synthesize(spec, opts)
    for event in events:
        if rpc and event.get("type") == "agent_settled":
            ready, _, _ = select.select([sys.stdin], [], [], 0.02)
            eof = bool(ready) and sys.stdin.readline() == ""
            if log_path:
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"pid": os.getpid(), "eof_before_settle": eof}) + "\n")
        _emit(event)

    for rel_path, content in spec.get("files", []):
        os.makedirs(os.path.dirname(os.path.join(os.getcwd(), rel_path)) or ".", exist_ok=True)
        with open(rel_path, "w", encoding="utf-8") as fh:
            fh.write(content)
    if spec.get("commit"):
        subprocess.run(["git", "add", "-A"], check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", str(spec["commit"])], check=True, capture_output=True
        )
    if spec.get("stderr"):
        sys.stderr.write(str(spec["stderr"]))
        sys.stderr.flush()
    if spec.get("sleep"):
        time.sleep(float(spec["sleep"]))
    if rpc:
        sys.stdin.read()
    return int(spec.get("exit_code", 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
