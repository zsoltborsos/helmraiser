"""Scripted srt substitute: snapshots settings, optionally vanishes a denied sibling."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

_SCRIPT = r"""
import json, os, shutil, sys, time
from pathlib import Path
scenario_path, log_path = map(Path, sys.argv[1:3])
argv = sys.argv[3:]
scenario = json.loads(scenario_path.read_text())
settings = json.loads(Path(argv[1]).read_text())
previous = [json.loads(line) for line in log_path.read_text().splitlines()] if log_path.exists() else []
kind = Path(argv[1]).name
entry = {"argv": argv, "settings": settings, "kind": kind}
with log_path.open("a") as log:
    log.write(json.dumps(entry) + "\n")
if scenario.get("kind", kind) == kind:
    attempt = sum(e["kind"] == kind for e in previous)
    if attempt < scenario.get("failures", 1):
        path = Path(scenario["vanished"])
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        time.sleep(scenario.get("delay", 0))
        print(f"bwrap: Can't create {scenario.get('node', 'file')} {path}: Read-only file system", file=sys.stderr)
        sys.exit(1)
command = argv[argv.index("--") + 1:]
os.execvp(command[0], command)
"""


class FakeSrt:
    def __init__(
        self,
        root: Path,
        vanished: Path,
        *,
        failures: int = 1,
        kind: str | None = None,
        delay: float = 0,
    ):
        root.mkdir(parents=True, exist_ok=True)
        self.log = root / "srt.jsonl"
        self.scenario = root / "scenario.json"
        self.scenario.write_text(
            json.dumps(
                {
                    "vanished": str(vanished),
                    "failures": failures,
                    **({"kind": kind} if kind else {}),
                    "delay": delay,
                }
            )
        )
        script = root / "fake_srt.py"
        script.write_text(_SCRIPT)
        self.bin = root / "srt"
        self.bin.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} {shlex.quote(str(self.scenario))} {shlex.quote(str(self.log))} "$@"\n'
        )
        self.bin.chmod(0o755)

    def calls(self) -> list[dict]:
        return (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )


def unwrap_guard(argv: list[str]) -> list[str]:
    """Validate the actual launch guard, then return its original payload."""
    from helmraiser.isolation.retry import _GUARD

    assert argv[:3] == ["/bin/bash", "-c", _GUARD]
    assert argv[3] == "helmraiser-start"
    assert Path(argv[4]).name.startswith("helmraiser-start-")
    return argv[5:]
