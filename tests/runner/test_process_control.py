"""Pure validation tests: no real process is signaled in this module."""

from unittest.mock import MagicMock, patch

import pytest

from helmraiser.runner.process_control import PosixProcessController, ProcessIdentity


def _stat(pid: int, token: str = "777") -> str:
    # pid + comm, then fields 3..21, then field 22 starttime
    return f"{pid} (safe test process) " + " ".join(["S"] + ["0"] * 18 + [token])


@pytest.mark.parametrize("unsafe", [True, False, 0, 1, -1, MagicMock(), "123"])
def test_identify_rejects_unsafe_or_non_integer_pid(tmp_path, unsafe):
    controller = PosixProcessController(tmp_path)
    with pytest.raises(ValueError, match="unsafe or non-integer"):
        controller.identify(unsafe)  # type: ignore[arg-type]


def test_identify_requires_process_to_lead_its_group(tmp_path):
    controller = PosixProcessController(tmp_path)
    with patch("os.getpgid", return_value=41):
        with pytest.raises(ValueError, match="process-group leader"):
            controller.identify(42)


def test_identify_captures_linux_start_token(tmp_path):
    proc_dir = tmp_path / "42"
    proc_dir.mkdir()
    (proc_dir / "stat").write_text(_stat(42), encoding="utf-8")
    controller = PosixProcessController(tmp_path)
    with patch("os.getpgid", return_value=42):
        assert controller.identify(42) == ProcessIdentity(42, 42, "777")


def test_terminate_refuses_reused_pid_without_signaling(tmp_path):
    proc_dir = tmp_path / "42"
    proc_dir.mkdir()
    (proc_dir / "stat").write_text(_stat(42, "new-token"), encoding="utf-8")
    controller = PosixProcessController(tmp_path)
    with patch("os.getpgid", return_value=42), patch("os.killpg") as killpg:
        assert controller.terminate(ProcessIdentity(42, 42, "old-token")) is False
        killpg.assert_not_called()


@pytest.mark.parametrize(
    "identity",
    [
        ProcessIdentity(1, 1, "x"),
        ProcessIdentity(42, 1, "x"),
        ProcessIdentity(42, 43, "x"),
        ProcessIdentity(True, True, "x"),  # type: ignore[arg-type]
    ],
)
def test_terminate_refuses_unsafe_identity_without_signaling(tmp_path, identity):
    controller = PosixProcessController(tmp_path)
    with patch("os.killpg") as killpg:
        assert controller.terminate(identity) is False
        killpg.assert_not_called()


def test_terminate_signals_only_a_fully_validated_identity(tmp_path):
    proc_dir = tmp_path / "42"
    proc_dir.mkdir()
    (proc_dir / "stat").write_text(_stat(42), encoding="utf-8")
    controller = PosixProcessController(tmp_path)
    with patch("os.getpgid", return_value=42), patch("os.killpg") as killpg:
        assert controller.terminate(ProcessIdentity(42, 42, "777")) is True
        killpg.assert_called_once()
