"""ScriptedPiAdapter — an in-process ``PiJsonAdapter`` stand-in (spec §8).

Mirrors ``ScriptedAgentRunner``: scripts are keyed by stage (``intake``,
``implement``, ``review``, ...) taken from ``PiRequest.stage``. A list scripts
successive calls to the same stage; the last entry repeats once exhausted.
Unscripted stages succeed with a stock answer. Nothing here spawns a process.

Usage:
    adapter = ScriptedPiAdapter({
        "intake": PiScript(final_text="```yaml\\nverify: [...]\\n```"),
        "implement": [PiScript(files=[("src/x.py", "...")]), PiScript()],
    })
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from helmraiser.pi.protocol import OnEvent, PiEvent, PiRequest, PiSessionResult, Usage


@dataclass
class PiScript:
    """Scripted behaviour for one pi session.

    - final_text / stop_reason / error_kind / error_message: what the fake session reports
    - events: PiEvents streamed to ``on_event`` before returning
    - files: (relative_path, content) written under ``req.cwd`` before returning
    - action: called with the session's working directory after ``files`` are written
      and before ``commit``
    - commit: commit everything under ``req.cwd`` after writing ``files``
    - side_effect: exception raised instead of returning
    """

    final_text: str = "scripted pi output"
    ok: bool = True
    stop_reason: str | None = "stop"
    error_kind: str | None = None
    error_message: str | None = None
    usage: Usage = field(default_factory=Usage)
    tool_calls: int = 0
    events: list[PiEvent] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)
    action: Callable[[Path], None] | None = None
    commit: bool = False
    side_effect: Exception | None = None


class ScriptedPiAdapter:
    """PiAdapter implementation that dispatches on ``req.stage``."""

    def __init__(self, scripts: dict[str, PiScript | list[PiScript]] | None = None):
        self.scripts = scripts or {}
        self.calls: list[PiRequest] = []
        self.run_ids: list[str] = []
        self._call_counts: dict[str, int] = {}

    def calls_for(self, stage: str) -> list[PiRequest]:
        return [c for c in self.calls if c.stage == stage]

    def _script_for(self, stage: str) -> PiScript:
        entry = self.scripts.get(stage)
        n = self._call_counts.get(stage, 0)
        self._call_counts[stage] = n + 1
        if entry is None:
            return PiScript()
        if isinstance(entry, list):
            return entry[min(n, len(entry) - 1)]
        return entry

    async def run(self, req: PiRequest, on_event: OnEvent, run_id: str) -> PiSessionResult:
        self.calls.append(req)
        self.run_ids.append(run_id)
        script = self._script_for(req.stage)

        if script.side_effect is not None:
            raise script.side_effect

        for event in script.events:
            await on_event(event)

        for rel_path, content in script.files:
            full_path = Path(req.cwd) / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
        if script.action is not None:
            script.action(Path(req.cwd))
        if script.commit:
            for argv in (["add", "-A"], ["commit", "-q", "-m", "scripted"]):
                subprocess.run(["git", *argv], cwd=req.cwd, check=True, capture_output=True)

        return PiSessionResult(
            ok=script.ok,
            session_id=req.session_id,
            final_text=script.final_text,
            stop_reason=script.stop_reason,
            error_kind=script.error_kind,
            error_message=script.error_message,
            usage=script.usage,
            tool_calls=script.tool_calls,
            returncode=0,
            timed_out=False,
            killed=False,
        )
