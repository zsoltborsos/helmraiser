"""Isolation seam (spec §7): how a pi process is confined for a run.

Levels: ``none`` (default; cross-project reads allowed), ``process``
(anthropics/sandbox-runtime ``srt`` over bubblewrap) and ``vm`` (later).
``for_level`` returns the implementation; ``work`` refuses to start a run
whose isolation ``check_available()`` returns a reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from helmraiser.isolation.none import NoIsolation
from helmraiser.isolation.process import ProcessIsolation
from helmraiser.isolation.vm import VmIsolation


class Isolation(Protocol):
    level: str

    def wrap(self, argv: list[str], cwd: Path, run_dir: Path) -> list[str]: ...

    def check_available(self) -> str | None: ...


def for_level(level: str, config: dict[str, Any] | None = None) -> Isolation:
    if level == "none":
        return NoIsolation()
    if level == "process":
        return ProcessIsolation(config or {})
    if level == "vm":
        return VmIsolation()
    raise ValueError(f"unknown isolation level: {level}")


__all__ = ["Isolation", "NoIsolation", "ProcessIsolation", "VmIsolation", "for_level"]
