"""``isolation: vm`` — reserved (spec §7)."""

from __future__ import annotations

from pathlib import Path


class VmIsolation:
    level = "vm"

    def wrap(self, argv: list[str], cwd: Path, run_dir: Path) -> list[str]:
        raise NotImplementedError("vm isolation not implemented")

    def check_available(self) -> str | None:
        return "vm isolation not implemented"
