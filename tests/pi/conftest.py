"""Fixtures for pi adapter tests. The scripted ``pi`` on PATH (``fake_pi``) lives in
the root ``tests/conftest.py`` so loop and CLI tests share it (spec §8)."""

from __future__ import annotations

import os
import signal

import pytest

from helmraiser.runner.process_control import PosixProcessController, ProcessIdentity
from tests.conftest import FakePi  # noqa: F401  (re-export for existing imports)


class SingleProcessController:
    """Real identity discovery, but signals only the leader pid (never a group).

    The suite forbids ``os.killpg``; every child here is its own session leader
    (``start_new_session=True``) so signalling the pid is equivalent.
    """

    def __init__(self) -> None:
        self._posix = PosixProcessController()
        self.terminated: list[ProcessIdentity] = []
        self.killed: list[ProcessIdentity] = []

    def identify(self, pid: int) -> ProcessIdentity:
        return self._posix.identify(pid)

    def is_running(self, identity: ProcessIdentity) -> bool:
        return self._posix.is_running(identity)

    def _signal(self, identity: ProcessIdentity, sig: signal.Signals) -> bool:
        if not self._posix.is_running(identity):
            return False
        try:
            os.kill(identity.leader_pid, sig)
        except ProcessLookupError:
            pass
        return True

    def terminate(self, identity: ProcessIdentity) -> bool:
        self.terminated.append(identity)
        return self._signal(identity, signal.SIGTERM)

    def kill(self, identity: ProcessIdentity) -> bool:
        self.killed.append(identity)
        return self._signal(identity, signal.SIGKILL)


@pytest.fixture
def process_controller() -> SingleProcessController:
    return SingleProcessController()
