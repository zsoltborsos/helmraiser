"""JSONL parsing, usage accounting and the §3.4 classification table."""

from __future__ import annotations

import json
from typing import Any

import pytest

from helmraiser.pi.events import (
    GARBAGE_TYPE,
    UNREADABLE_USAGE_MESSAGE,
    EventCollector,
    classify,
    final_text,
    parse_line,
    sum_usage,
)
from helmraiser.pi.protocol import PiEvent, Usage
from helmraiser.runner.engine import ExecResult


def _usage(tokens_in: int = 0, tokens_out: int = 0, cost: float = 0.0, **extra: int) -> dict:
    return {
        "input": tokens_in,
        "output": tokens_out,
        "cacheRead": extra.get("cacheRead", 0),
        "cacheWrite": extra.get("cacheWrite", 0),
        "totalTokens": tokens_in + tokens_out,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": cost},
    }


def _assistant_end(
    text: str = "done",
    stop_reason: str = "stop",
    usage: dict | None = None,
    error_message: str | None = None,
) -> PiEvent:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}] if text else [],
        "usage": usage or _usage(),
        "stopReason": stop_reason,
        "timestamp": 0,
    }
    if error_message:
        message["errorMessage"] = error_message
    return PiEvent("message_end", {"type": "message_end", "message": message})


def _ev(type_: str, **raw: Any) -> PiEvent:
    return PiEvent(type_, {"type": type_, **raw})


def _line(record: dict) -> str:
    return json.dumps(record) + "\n"


class TestParseLine:
    def test_parses_record_and_strips_crlf(self):
        event = parse_line('{"type":"agent_start"}\r\n')
        assert event.type == "agent_start"
        assert event.raw == {"type": "agent_start"}

    def test_non_json_is_garbage(self):
        event = parse_line("Warning: something on stdout\n")
        assert event.type == GARBAGE_TYPE
        assert event.raw["line"] == "Warning: something on stdout"

    def test_json_without_type_is_garbage(self):
        assert parse_line('{"foo": 1}').type == GARBAGE_TYPE
        assert parse_line("[1, 2]").type == GARBAGE_TYPE

    def test_unicode_separators_inside_strings_survive(self):
        event = parse_line('{"type":"x","text":"a\\u2028b"}\n')
        assert event.raw["text"] == "a b"


class TestEventCollector:
    def test_drops_deltas_and_keeps_latest_usage(self):
        c = EventCollector()
        assert c.feed(_line({"type": "message_start", "message": {"role": "assistant"}}))
        delta1 = {
            "type": "message_update",
            "usage": _usage(10, 1, 0.001),
            "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "He"},
        }
        delta2 = {**delta1, "usage": _usage(10, 5, 0.002)}
        assert c.feed(_line(delta1)) is None
        assert c.feed(_line(delta2)) is None
        assert [e.type for e in c.events] == ["message_start"]
        assert c.latest_delta_usage == _usage(10, 5, 0.002)
        # Message never completed: the latest delta usage counts as spent.
        assert c.usage == Usage(10, 5, 0.002)

    def test_drops_tool_output_deltas(self):
        """Each update repeats the args and all output so far; tool_execution_end has it."""
        c = EventCollector()
        update = {"type": "tool_execution_update", "toolName": "bash", "partialResult": "x" * 100}
        assert c.feed(_line(update)) is None
        assert c.events == []

    def test_garbage_and_blank_lines_are_not_forwarded(self):
        c = EventCollector()
        assert c.feed("\n") is None
        assert c.feed("not json\n") is None
        assert c.feed("   \n") is None
        assert c.events == []
        assert c.garbage == ["not json"]

    def test_usage_sums_across_assistant_message_ends(self):
        c = EventCollector()
        c.feed(_line(_assistant_end("a", usage=_usage(100, 10, 0.01, cacheRead=50)).raw))
        c.feed(_line({"type": "message_end", "message": {"role": "user", "content": "x"}}))
        c.feed(_line(_assistant_end("b", usage=_usage(200, 20, 0.02, cacheWrite=5)).raw))
        assert c.usage == Usage(tokens_in=355, tokens_out=30, cost_usd=pytest.approx(0.03))
        assert sum_usage(c.events) == c.usage

    def test_message_end_replaces_delta_usage(self):
        c = EventCollector()
        c.feed(_line({"type": "message_start", "message": {"role": "assistant"}}))
        c.feed(
            _line(
                {
                    "type": "message_update",
                    "usage": _usage(10, 1, 0.5),
                    "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": ""},
                }
            )
        )
        c.feed(_line(_assistant_end(usage=_usage(10, 3, 0.02)).raw))
        assert c.usage == Usage(10, 3, 0.02)
        assert c.latest_delta_usage is None

    def test_tool_calls_counted(self):
        c = EventCollector()
        for n in range(3):
            c.feed(_line({"type": "tool_execution_start", "toolCallId": str(n), "toolName": "b"}))
            c.feed(_line({"type": "tool_execution_end", "toolCallId": str(n), "toolName": "b"}))
        assert c.tool_calls == 3


class TestFinalText:
    def test_joins_text_blocks_of_last_assistant_message(self):
        events = [
            _assistant_end("first"),
            PiEvent(
                "message_end",
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [
                            {"type": "thinking", "thinking": "hmm"},
                            {"type": "text", "text": "line one"},
                            {"type": "toolCall", "id": "c", "name": "bash", "arguments": {}},
                            {"type": "text", "text": "line two"},
                        ],
                    },
                },
            ),
        ]
        assert final_text(events) == "line one\nline two"

    def test_empty_without_assistant_message(self):
        assert final_text([_ev("agent_start"), _ev("agent_settled")]) == ""


OK = ExecResult(True, returncode=0)


class TestClassify:
    def test_settled_and_stop_is_ok(self):
        events = [_ev("agent_start"), _assistant_end(), _ev("agent_end"), _ev("agent_settled")]
        assert classify(events, OK) == (True, None, None, "stop")

    def test_length_is_ok_turn_ended(self):
        events = [_assistant_end(stop_reason="length"), _ev("agent_settled")]
        assert classify(events, OK) == (True, None, None, "length")

    def test_length_without_settled_is_still_ok(self):
        events = [_assistant_end(stop_reason="length")]
        assert classify(events, OK)[:2] == (True, None)

    @pytest.mark.parametrize(
        "message",
        [
            "429 Too Many Requests",
            "Rate limit exceeded",
            "rate-limited by provider",
            "quota exhausted for this billing period",
            "insufficient_quota",
            "529 overloaded",
            "Provider Overloaded",
        ],
    )
    def test_error_matching_quota_pattern(self, message: str):
        events = [_assistant_end("", "error", error_message=message), _ev("agent_settled")]
        ok, kind, msg, stop = classify(events, OK)
        assert (ok, kind, stop) == (False, "quota", "error")
        assert msg == message

    def test_other_error_is_provider(self):
        events = [
            _assistant_end("", "error", error_message="invalid_request"),
            _ev("agent_settled"),
        ]
        assert classify(events, OK) == (False, "provider", "invalid_request", "error")

    def test_error_without_message_is_provider(self):
        events = [_assistant_end("", "error"), _ev("agent_settled")]
        ok, kind, msg, _ = classify(events, OK)
        assert (ok, kind) == (False, "provider")
        assert msg

    def test_auto_retry_failure_is_provider(self):
        events = [
            _ev("auto_retry_start", attempt=1, maxAttempts=3, errorMessage="boom"),
            _ev("auto_retry_end", success=False, attempt=3, finalError="upstream down"),
            _ev("agent_settled"),
        ]
        assert classify(events, OK) == (False, "provider", "upstream down", None)

    def test_auto_retry_failure_with_quota_text_is_quota(self):
        events = [
            _ev("auto_retry_end", success=False, finalError="429 still"),
            _ev("agent_settled"),
        ]
        assert classify(events, OK)[1] == "quota"

    def test_successful_auto_retry_does_not_fail(self):
        events = [
            _ev("auto_retry_end", success=True, attempt=2),
            _assistant_end(),
            _ev("agent_settled"),
        ]
        assert classify(events, OK)[0] is True

    def test_stop_reason_aborted(self):
        events = [_assistant_end("", "aborted"), _ev("agent_settled")]
        assert classify(events, OK)[:2] == (False, "aborted")

    def test_killed_by_abort_signal(self):
        events = [_ev("agent_start"), _ev("turn_start")]
        killed = ExecResult(False, returncode=-15)
        assert classify(events, killed)[:2] == (False, "aborted")

    def test_engine_internal_failure_is_protocol_not_aborted(self):
        # run_stdin reports its own exceptions as returncode -1 without a kill
        # (e.g. the dogfood's "Separator is found, but chunk is longer than limit").
        internal = ExecResult(False, stderr="Separator is found, but chunk is longer than limit")
        internal.returncode = -1
        events = [_ev("agent_start"), _ev("message_end", message={"role": "toolResult"})]
        ok, kind, message, _ = classify(events, internal)
        assert (ok, kind) == (False, "protocol")
        assert "longer than limit" in message

    def test_lost_abort_race_is_aborted(self):
        refused = ExecResult(False, stderr="run is aborting; refusing a newly spawned agent")
        refused.returncode = -1
        assert classify([], refused)[:2] == (False, "aborted")

    def test_timeout_wins_over_everything(self):
        events = [_assistant_end("", "error", error_message="429")]
        timed_out = ExecResult(False, returncode=-1, timed_out=True, killed=True)
        assert classify(events, timed_out)[:2] == (False, "timeout")

    def test_nonzero_exit_without_settled_is_protocol_with_stderr(self):
        crashed = ExecResult(False, stderr="Error: unknown option --bogus\n", returncode=1)
        ok, kind, msg, stop = classify([], crashed)
        assert (ok, kind, stop) == (False, "protocol", None)
        assert "unknown option --bogus" in (msg or "")

    def test_nonzero_exit_after_settled_stop_is_ok(self):
        events = [_assistant_end(), _ev("agent_settled")]
        assert classify(events, ExecResult(False, returncode=1))[0] is True

    def test_clean_exit_without_settled_is_protocol(self):
        events = [_ev("agent_start"), _assistant_end(stop_reason="toolUse")]
        ok, kind, msg, stop = classify(events, OK)
        assert (ok, kind, stop) == (False, "protocol", "toolUse")
        assert "agent_settled" in (msg or "")

    def test_empty_stream_is_protocol(self):
        assert classify([], OK)[:2] == (False, "protocol")

    def test_uses_last_assistant_message(self):
        events = [
            _assistant_end("", "error", error_message="429"),
            _ev("auto_retry_end", success=True),
            _assistant_end("recovered"),
            _ev("agent_settled"),
        ]
        assert classify(events, OK) == (True, None, None, "stop")


def _without_usage(event: PiEvent, **usage_override: Any) -> PiEvent:
    """``event`` with its usage removed (no kwargs) or replaced by ``usage``."""
    message = {k: v for k, v in event.raw["message"].items() if k != "usage"}
    if "usage" in usage_override:
        message["usage"] = usage_override["usage"]
    return PiEvent("message_end", {"type": "message_end", "message": message})


def _unreadable_usages() -> list[Any]:
    full = _usage(10, 5, 0.01)
    without_input = {k: v for k, v in full.items() if k != "input"}
    without_output = {k: v for k, v in full.items() if k != "output"}
    without_cost = {k: v for k, v in full.items() if k != "cost"}
    return [
        None,
        "10 tokens",
        without_input,
        without_output,
        without_cost,
        {**full, "input": "10"},
        {**full, "output": None},
        {**full, "input": True},
        {**full, "cost": 0.01},
        {**full, "cost": {"input": 0, "output": 0}},
        {**full, "cost": {"total": "0.01"}},
    ]


class TestUnreadableUsageFailsClosed:
    """Budget caps rely on pi's usage; an otherwise ok session without readable
    usage on an assistant message is a ``contract`` failure, not $0."""

    def test_missing_usage_is_contract(self):
        events = [_without_usage(_assistant_end()), _ev("agent_settled")]
        assert classify(events, OK) == (False, "contract", UNREADABLE_USAGE_MESSAGE, "stop")
        assert "budget caps" in UNREADABLE_USAGE_MESSAGE

    @pytest.mark.parametrize("usage", _unreadable_usages())
    def test_unreadable_usage_is_contract(self, usage: Any):
        events = [_without_usage(_assistant_end(), usage=usage), _ev("agent_settled")]
        assert classify(events, OK)[:2] == (False, "contract")

    def test_length_stop_without_usage_is_contract(self):
        events = [_without_usage(_assistant_end(stop_reason="length")), _ev("agent_settled")]
        assert classify(events, OK)[:2] == (False, "contract")

    def test_any_assistant_message_without_usage_counts(self):
        events = [
            _without_usage(_assistant_end("thinking", "toolUse")),
            _ev("tool_execution_start", toolCallId="c", toolName="bash"),
            _assistant_end("done"),
            _ev("agent_settled"),
        ]
        assert classify(events, OK)[:2] == (False, "contract")

    def test_zero_usage_and_extra_fields_are_readable(self):
        usage = {**_usage(0, 0, 0.0), "reasoning": 7, "cost": {"total": 0, "extra": 1}}
        events = [_without_usage(_assistant_end(), usage=usage), _ev("agent_settled")]
        assert classify(events, OK) == (True, None, None, "stop")

    def test_minimal_usage_is_readable(self):
        usage = {"input": 3, "output": 2, "cost": {"total": 0.5}}
        events = [_without_usage(_assistant_end(), usage=usage), _ev("agent_settled")]
        assert classify(events, OK)[0] is True

    def test_non_assistant_message_without_usage_is_fine(self):
        events = [
            _ev("message_end", message={"role": "user", "content": "x"}),
            _assistant_end(),
            _ev("agent_settled"),
        ]
        assert classify(events, OK)[0] is True

    def test_failures_keep_their_own_kind(self):
        errored = _without_usage(_assistant_end("", "error", error_message="429"))
        assert classify([errored, _ev("agent_settled")], OK)[1] == "quota"
        aborted = _without_usage(_assistant_end("", "aborted"))
        assert classify([aborted, _ev("agent_settled")], OK)[1] == "aborted"
        unsettled = _without_usage(_assistant_end(stop_reason="toolUse"))
        assert classify([unsettled], OK)[1] == "protocol"
