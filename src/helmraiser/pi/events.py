"""JSONL event parsing for ``pi --mode json`` (spec §3.3–3.4; pi docs json.md).

Framing: one JSON object per LF-terminated line, optional preceding CR. Deltas
(``message_update``) are dropped except for their cumulative ``usage``, and tool
output deltas (``tool_execution_update``) are dropped; every other record is kept
verbatim. Completion is derived from events, not the exit
code, because json mode exits 0 even when the assistant response errored.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from helmraiser.pi.protocol import PiEvent, Usage

if TYPE_CHECKING:
    from helmraiser.runner.engine import ExecResult

GARBAGE_TYPE = "stderr_or_garbage"

QUOTA_PATTERN = re.compile(r"(429|rate.?limit|quota|insufficient|overloaded|529)", re.IGNORECASE)

UNREADABLE_USAGE_MESSAGE = (
    "pi reported no readable usage on an assistant message (input/output/cost.total); "
    "budget caps cannot be enforced — check the installed pi version's JSON output"
)


def parse_line(line: str) -> PiEvent:
    """Parse one stdout line. Non-JSON or non-record lines become ``GARBAGE_TYPE``."""
    text = line.rstrip("\n").rstrip("\r")
    try:
        obj = json.loads(text)
    except ValueError:
        return PiEvent(GARBAGE_TYPE, {"type": GARBAGE_TYPE, "line": text})
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        return PiEvent(GARBAGE_TYPE, {"type": GARBAGE_TYPE, "line": text})
    return PiEvent(obj["type"], obj)


def usage_of(usage: dict[str, Any] | None) -> Usage:
    """Convert a pi ``Usage`` object into ours. ``tokens_in`` includes cache traffic."""
    if not usage:
        return Usage()
    cost = usage.get("cost") or {}
    return Usage(
        tokens_in=int(usage.get("input") or 0)
        + int(usage.get("cacheRead") or 0)
        + int(usage.get("cacheWrite") or 0),
        tokens_out=int(usage.get("output") or 0),
        cost_usd=float(cost.get("total") or 0.0),
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def usage_readable(usage: Any) -> bool:
    """True when ``usage`` carries numeric ``input``, ``output`` and ``cost.total``
    (zero counts; extra fields are ignored)."""
    if not isinstance(usage, dict):
        return False
    cost = usage.get("cost")
    return (
        _is_number(usage.get("input"))
        and _is_number(usage.get("output"))
        and isinstance(cost, dict)
        and _is_number(cost.get("total"))
    )


def _add(total: Usage, part: Usage) -> Usage:
    return Usage(
        tokens_in=total.tokens_in + part.tokens_in,
        tokens_out=total.tokens_out + part.tokens_out,
        cost_usd=total.cost_usd + part.cost_usd,
    )


def is_assistant_message_end(event: PiEvent) -> bool:
    message = event.raw.get("message")
    return (
        event.type == "message_end"
        and isinstance(message, dict)
        and message.get("role") == "assistant"
    )


class EventCollector:
    """Accumulates one session's records as they stream in.

    ``feed`` returns the event to forward, or ``None`` for blank lines, deltas
    and garbage (garbage lines are counted and their tail kept for diagnostics).
    """

    def __init__(self) -> None:
        self.events: list[PiEvent] = []
        self.garbage: list[str] = []
        self.latest_delta_usage: dict[str, Any] | None = None
        self._settled_usage = Usage()
        self._open_assistant = False

    def feed(self, line: str) -> PiEvent | None:
        if not line.strip():
            return None
        event = parse_line(line)
        if event.type == GARBAGE_TYPE:
            self.garbage.append(str(event.raw.get("line", "")))
            del self.garbage[:-20]
            return None
        if event.type == "message_update":
            usage = event.raw.get("usage")
            if isinstance(usage, dict):
                self.latest_delta_usage = usage
            return None
        if event.type == "tool_execution_update":
            return None
        if event.type == "message_start":
            message = event.raw.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                self._open_assistant = True
                self.latest_delta_usage = None
        if is_assistant_message_end(event):
            self._settled_usage = _add(
                self._settled_usage, usage_of(event.raw["message"].get("usage"))
            )
            self._open_assistant = False
            self.latest_delta_usage = None
        self.events.append(event)
        return event

    @property
    def usage(self) -> Usage:
        """Summed assistant ``message_end`` usage, plus the latest delta usage of an
        assistant message that never completed (killed mid-stream)."""
        if self._open_assistant and self.latest_delta_usage:
            return _add(self._settled_usage, usage_of(self.latest_delta_usage))
        return self._settled_usage

    @property
    def tool_calls(self) -> int:
        return sum(1 for e in self.events if e.type == "tool_execution_start")


def ledger_payload(event: PiEvent) -> dict[str, Any] | None:
    """What the run ledger keeps of a forwarded record; ``None`` keeps nothing.

    ``message_start`` and ``turn_end`` repeat what ``message_end`` and
    ``tool_execution_end`` hold, and ``agent_end`` drops its ``messages``
    transcript: pi's session file keeps the full record.
    """
    if event.type in ("message_start", "turn_end"):
        return None
    if event.type == "agent_end":
        return {k: v for k, v in event.raw.items() if k != "messages"}
    return event.raw


def last_assistant_message(events: list[PiEvent]) -> dict[str, Any] | None:
    for event in reversed(events):
        if is_assistant_message_end(event):
            message: dict[str, Any] = event.raw["message"]
            return message
    return None


def final_text(events: list[PiEvent]) -> str:
    """Text blocks of the last assistant message, joined."""
    message = last_assistant_message(events)
    if message is None:
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts = [
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(t for t in texts if t)


def sum_usage(events: list[PiEvent]) -> Usage:
    total = Usage()
    for event in events:
        if is_assistant_message_end(event):
            total = _add(total, usage_of(event.raw["message"].get("usage")))
    return total


def _tail(text: str, lines: int = 20) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def classify(
    events: list[PiEvent], exec_result: ExecResult
) -> tuple[bool, str | None, str | None, str | None]:
    """Completion/error classification table of spec §3.4.

    Returns ``(ok, error_kind, error_message, stop_reason)``.
    """
    message = last_assistant_message(events)
    stop_reason = message.get("stopReason") if message else None
    settled = any(e.type == "agent_settled" for e in events)
    stderr_tail = _tail(exec_result.stderr)

    if exec_result.timed_out:
        return False, "timeout", "pi session exceeded its time limit", stop_reason

    if stop_reason == "error":
        error_message = str((message or {}).get("errorMessage") or "assistant response errored")
        kind = "quota" if QUOTA_PATTERN.search(error_message) else "provider"
        return False, kind, error_message, stop_reason

    for event in reversed(events):
        if event.type == "auto_retry_end" and event.raw.get("success") is False:
            error_message = str(event.raw.get("finalError") or "automatic retry failed")
            kind = "quota" if QUOTA_PATTERN.search(error_message) else "provider"
            return False, kind, error_message, stop_reason

    # ``-1`` is the engine's own marker for an internal failure (spawn error, stream
    # limit); a process stopped by a signal reports ``-<signo>`` (≤ -2).
    killed_by_abort = (
        exec_result.killed or exec_result.returncode < -1 or "run is aborting" in exec_result.stderr
    )
    if stop_reason == "aborted" or killed_by_abort:
        return False, "aborted", stderr_tail or "pi session aborted", stop_reason

    if exec_result.returncode != 0 and not settled:
        return (
            False,
            "protocol",
            stderr_tail or f"pi exited with status {exec_result.returncode}",
            stop_reason,
        )

    if (settled and stop_reason == "stop") or stop_reason == "length":
        # Budget caps rely on pi's usage: fail closed rather than count it as $0.
        if not all(
            usage_readable(e.raw["message"].get("usage"))
            for e in events
            if is_assistant_message_end(e)
        ):
            return False, "contract", UNREADABLE_USAGE_MESSAGE, stop_reason
        return True, None, None, stop_reason

    if settled and stop_reason is None and exec_result.returncode == 0:
        return False, "protocol", "pi settled without an assistant message", stop_reason

    return (
        False,
        "protocol",
        stderr_tail or f"pi session ended without agent_settled (stopReason={stop_reason})",
        stop_reason,
    )
