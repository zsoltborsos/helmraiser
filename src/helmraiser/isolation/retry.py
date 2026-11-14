"""Launch metadata for one conservative retry of a vanished-denial mount failure.

The inner guard is the proof that the payload has started. Ordinary argv consumers
still see the unguarded list; only actual launchers opt in to the guard.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from helmraiser.ledger.event_logger import EventLogger

RetryObserver = Callable[[dict[str, Any]], None]
_MOUNT_FAILURE = re.compile(r"bwrap: Can't create (?:file|dir) (/[^\r\n]+): Read-only file system")
_GUARD = '(umask 077; set -C; : > "$1") || exit 125; shift; exec "$@"'


def _absent(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        pass
    return False


class SandboxLaunch(list[str]):
    def __init__(
        self,
        argv: list[str],
        settings_path: Path,
        tmpdir: Path,
        kind: str,
        observer: RetryObserver | None = None,
    ) -> None:
        super().__init__(argv)
        self.settings_path = settings_path
        self.kind = kind
        self.observer = observer
        self.marker = tmpdir / f"helmraiser-start-{uuid.uuid4()}.marker"
        filesystem = json.loads(settings_path.read_text())["filesystem"]
        self.denials = tuple(
            Path(os.path.normpath(p))
            for key in ("denyRead", "denyWrite")
            for p in filesystem.get(key, [])
            if Path(p).is_absolute()
        )

    def guarded_argv(self) -> list[str]:
        split = self.index("--") + 1
        return [
            *self[:split],
            "/bin/bash",
            "-c",
            _GUARD,
            "helmraiser-start",
            str(self.marker),
            *self[split:],
        ]

    def mount_failure_path(
        self,
        returncode: int | None,
        stdout: str | bool,
        stderr: str,
        *,
        timed_out: bool = False,
        killed: bool = False,
    ) -> Path | None:
        """Recognize a pre-payload mount failure, independently of current denials."""
        if returncode is None or returncode <= 0 or stdout or timed_out or killed:
            return None
        if not _absent(self.marker):
            return None
        for line in stderr.splitlines():
            match = _MOUNT_FAILURE.fullmatch(line)
            if match is not None:
                return Path(os.path.normpath(match[1]))
        return None

    def retry_path(
        self,
        returncode: int | None,
        stdout: str | bool,
        stderr: str,
        *,
        timed_out: bool = False,
        killed: bool = False,
    ) -> Path | None:
        path = self.mount_failure_path(
            returncode, stdout, stderr, timed_out=timed_out, killed=killed
        )
        if path is not None and any(path.is_relative_to(p) for p in self.denials) and _absent(path):
            return path
        return None

    def record_retry(self, path: Path) -> None:
        payload: dict[str, Any] = {"kind": self.kind, "vanished_path": str(path), "attempt": 2}
        if self.observer is not None:
            self.observer(payload)
        else:
            EventLogger(self.settings_path.parent).log("sandbox_retry", **payload)

    def cleanup(self) -> None:
        try:
            self.marker.unlink(missing_ok=True)
        except OSError:
            pass  # Cleanup must not replace the command's result.
