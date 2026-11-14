"""Read the cumulative net changes in a run worktree."""

import os
import subprocess
from pathlib import Path

from helmraiser.code_snapshot import without_snapshot


def changed_paths(worktree: Path, base_commit: str) -> tuple[str, ...]:
    """Return tracked changes from base plus untracked, non-ignored files.

    Rename detection is disabled so both endpoints are subject to scope checks.
    NUL separation preserves whitespace and newline-containing filenames.
    """
    paths: set[str] = set()
    for args in (
        (
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            base_commit,
            "--",
        ),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=worktree,
                capture_output=True,
                check=False,
                timeout=60,
                env=without_snapshot(os.environ),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Cannot collect changed paths: {exc}") from exc
        if result.returncode:
            raise RuntimeError(
                f"git {args[0]} failed collecting changed paths: "
                f"{os.fsdecode(result.stderr).strip()}"
            )
        paths.update(os.fsdecode(p) for p in result.stdout.split(b"\0") if p)
    return tuple(sorted(paths))
