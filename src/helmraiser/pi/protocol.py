"""pi session interface (spec §3.2): request/result dataclasses and the adapter protocol.

``PiJsonAdapter`` (``pi/adapter.py``) implements ``PiAdapter``; tests use an
in-process scripted stand-in. Nothing here spawns a process.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from helmraiser.task.model import ModelSpec


@dataclass(frozen=True)
class PiEvent:
    """One ``--mode json`` JSONL record, parsed."""

    type: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class PiRequest:
    prompt: str
    session_id: str
    model: ModelSpec
    cwd: Path
    session_dir: Path
    system_prompt_path: Path | None = None
    tools: tuple[str, ...] | None = None
    context_files: bool = True
    timeout_s: int = 3600
    env: dict[str, str] = field(default_factory=dict)
    # Stage name and attempt number (the ``-<n>`` of the session id) — used for
    # ``HELMRAISER_STAGE`` and the large-prompt spill file ``prompt-<stage>-<n>.md``.
    stage: str = "implement"
    attempt: int = 1
    rpc: bool = False
    # Extra pi flags before the prompt: skill flags (``pi/skills.py``) for a stage with a
    # skill folder, and the review replay's variant flags.
    extra_args: tuple[str, ...] = ()

    @property
    def run_dir(self) -> Path:
        """``session_dir`` is ``<run-dir>/sessions`` (spec §2)."""
        return self.session_dir.parent


@dataclass
class PiSessionResult:
    ok: bool
    session_id: str
    final_text: str
    stop_reason: str | None
    # None | provider | quota | timeout | aborted | protocol | contract | task | sandbox
    # sandbox: terminal pre-command sandbox-start failure (startup retry exhausted)
    error_kind: str | None
    error_message: str | None
    usage: Usage
    tool_calls: int
    returncode: int
    timed_out: bool
    killed: bool


OnEvent = Callable[[PiEvent], Awaitable[None]]


class PiAdapter(Protocol):
    async def run(self, req: PiRequest, on_event: OnEvent, run_id: str) -> PiSessionResult: ...
