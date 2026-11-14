"""Validated process-group identity and signaling boundary.

Only :class:`PosixProcessController` may emit process-group signals. Callers and
unit tests depend on the protocol so tests never need to signal the host OS.
"""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ProcessIdentity:
    """Stable identity for one subprocess session leader."""

    leader_pid: int
    pgid: int
    start_token: str


class ProcessController(Protocol):
    def identify(self, pid: int) -> ProcessIdentity: ...

    def is_running(self, identity: ProcessIdentity) -> bool: ...

    def terminate(self, identity: ProcessIdentity) -> bool: ...

    def kill(self, identity: ProcessIdentity) -> bool: ...


class PosixProcessController:
    """Linux process controller guarded against PID reuse and broad signals."""

    def __init__(self, proc_root: Path = Path("/proc")):
        self.proc_root = proc_root

    @staticmethod
    def _real_safe_pid(value: object) -> bool:
        return type(value) is int and value > 1

    def _start_token(self, pid: int) -> str:
        stat = (self.proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        # The comm field is parenthesized and may contain spaces. Fields after
        # the final ')' begin at field 3; starttime is field 22, index 19 here.
        fields_after_comm = stat.rsplit(")", 1)[1].split()
        return fields_after_comm[19]

    def identify(self, pid: int) -> ProcessIdentity:
        if not self._real_safe_pid(pid):
            raise ValueError("refusing unsafe or non-integer process ID")
        pgid = os.getpgid(pid)
        if not self._real_safe_pid(pgid) or pgid != pid:
            raise ValueError("child is not a safe process-group leader")
        return ProcessIdentity(pid, pgid, self._start_token(pid))

    def _still_matches(self, identity: ProcessIdentity) -> bool:
        if not self._real_safe_pid(identity.leader_pid):
            return False
        if not self._real_safe_pid(identity.pgid) or identity.leader_pid != identity.pgid:
            return False
        try:
            return (
                os.getpgid(identity.leader_pid) == identity.pgid
                and self._start_token(identity.leader_pid) == identity.start_token
            )
        except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError, OSError):
            return False

    def is_running(self, identity: ProcessIdentity) -> bool:
        return self._still_matches(identity)

    def _signal(self, identity: ProcessIdentity, sig: signal.Signals) -> bool:
        if not self._still_matches(identity):
            return False
        try:
            os.killpg(identity.pgid, sig)
            return True
        except ProcessLookupError:
            return True

    def terminate(self, identity: ProcessIdentity) -> bool:
        return self._signal(identity, signal.SIGTERM)

    def kill(self, identity: ProcessIdentity) -> bool:
        return self._signal(identity, signal.SIGKILL)
