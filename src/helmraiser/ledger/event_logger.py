"""EventLogger — local JSONL event logging for Helmraiser runs.

Always writes locally. Never fails (catches all exceptions, logs warning).
"""

import json
import threading
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class EventLogger:
    """Appends one JSON line per event to events.jsonl in the run directory."""

    def __init__(self, run_dir: Path):
        self._run_dir = run_dir
        self._events_file = run_dir / "events.jsonl"
        self._lock = threading.Lock()

    def log(self, event_type: str, agent_id: str | None = None, **data: Any) -> None:
        """Append one JSON line to events.jsonl.

        Never raises — catches all exceptions and logs a warning.
        """
        try:
            self._run_dir.mkdir(parents=True, exist_ok=True)
            entry: dict[str, Any] = {
                "event_type": event_type,
                "agent_id": agent_id,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            entry.update(data)
            line = json.dumps(entry, default=str) + "\n"
            with self._lock:
                with open(self._events_file, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception as exc:
            warnings.warn(f"EventLogger failed to write event: {exc}", stacklevel=2)
