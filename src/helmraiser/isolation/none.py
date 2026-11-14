"""``isolation: none`` — the pi process runs unconfined (spec §7)."""

from __future__ import annotations

from pathlib import Path


class NoIsolation:
    level = "none"

    def wrap(self, argv: list[str], cwd: Path, run_dir: Path) -> list[str]:
        return list(argv)

    def check_available(self) -> str | None:
        return None
