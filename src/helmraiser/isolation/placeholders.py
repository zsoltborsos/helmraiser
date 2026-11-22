"""Conservative removal of srt's root-level mount-point placeholders."""

from __future__ import annotations

import errno
import os
import stat
import subprocess
from pathlib import Path

from helmraiser.code_snapshot import without_snapshot

PLACEHOLDER_NAMES = (
    ".gitconfig",
    ".gitmodules",
    ".bashrc",
    ".bash_profile",
    ".zshrc",
    ".zprofile",
    ".profile",
    ".ripgreprc",
    ".mcp.json",
    ".vscode",
    ".idea",
)


class PlaceholderCleanupError(OSError):
    """A removal failed after zero or more successful removals."""

    def __init__(self, error: OSError, removed_names: list[str]) -> None:
        super().__init__(error.errno, error.strerror, error.filename)
        self.removed_names = list(removed_names)


def remove_placeholders(worktree: Path) -> list[str]:
    """Remove only unindexed, empty regular files/directories at a worktree root.

    Removal errors carry successful removals in PlaceholderCleanupError so callers
    can log partial cleanup. Automatic staging must still fail closed.
    """

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env=without_snapshot(os.environ),
        )

    root = git("rev-parse", "--show-toplevel")
    if root.returncode != 0 or Path(root.stdout.strip()).resolve() != worktree.resolve():
        return []
    indexed = git(
        "ls-files", "--cached", "-z", "--", *(f":(top,literal){name}" for name in PLACEHOLDER_NAMES)
    )
    indexed.check_returncode()
    tracked = indexed.stdout.split("\0")
    removed: list[str] = []
    for name in PLACEHOLDER_NAMES:
        if any(p == name or p.startswith(name + "/") for p in tracked):
            continue
        path = worktree / name
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_size == 0:
                path.unlink()
            elif stat.S_ISDIR(info.st_mode):
                # rmdir is atomic and refuses a directory that becomes non-empty.
                path.rmdir()
            else:
                continue
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                continue
            raise PlaceholderCleanupError(exc, removed) from exc
        removed.append(name)
    return removed
