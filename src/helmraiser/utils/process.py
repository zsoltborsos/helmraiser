"""Process-related utilities."""

import os


def is_process_alive(pid: int | None) -> bool:
    """Check if a process is still running by PID."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
