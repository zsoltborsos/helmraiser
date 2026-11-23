"""Leeboard hold checks, bounded implementation briefs and run-authored entries."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

HARNESS = "helmraiser"
CHECK_TAG = "helmraiser-run"
CHECK_TIMEOUT_S = 5.0
BRIEF_TIMEOUT_S = 15.0
BRIEF_MAX_CHARS = 8000
LIST_TIMEOUT_S = 10.0
RETIRED_ENV_PREFIX = "LOGBOOK_"


def find_lb() -> str | None:
    return shutil.which("lb")


def lb_install_root() -> Path | None:
    """The nearest package root behind the executable on PATH, following symlinks."""
    binary = find_lb()
    if binary is None:
        return None
    for parent in Path(binary).resolve().parents:
        if (parent / "package.json").is_file():
            return parent
    return None


def leeboard_home(env: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if env is None else env
    return Path(environment.get("LEEBOARD_HOME") or "~/.leeboard").expanduser().absolute()


def lb_environment(identity: Mapping[str, str]) -> dict[str, str]:
    """The host environment for an lb call: ``os.environ`` without the retired
    ``LOGBOOK_*`` names (lb refuses to run while one is set), plus ``identity``."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(RETIRED_ENV_PREFIX)}
    return {**env, **identity}


def leeboard_identity(run_id: str | None) -> dict[str, str]:
    identity = {"LEEBOARD_HARNESS": HARNESS}
    if run_id is not None:
        identity["LEEBOARD_SESSION"] = run_id
    return identity


@dataclass(frozen=True)
class RunEntry:
    id: str
    kind: str
    scope: str
    status: str
    title: str


def run_entries(run_ids: Iterable[str]) -> dict[str, list[RunEntry]]:
    """List current entries by author; an unavailable CLI looks like no entries."""
    entries: dict[str, list[RunEntry]] = {run_id: [] for run_id in run_ids}
    if not entries:
        return entries
    try:
        binary = find_lb()
    except (OSError, ValueError):
        return entries
    if binary is None:
        return entries
    env = lb_environment(leeboard_identity(None))
    env.pop("LEEBOARD_SESSION", None)
    try:
        result = subprocess.run(
            [binary, "list", "--harness", HARNESS, "--json"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=LIST_TIMEOUT_S,
            shell=False,
            check=False,
        )
        if result.returncode != 0:
            return entries
        parsed = json.loads(result.stdout)
        if not isinstance(parsed, list):
            return entries
        for item in parsed:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            author = item.get("author")
            if not isinstance(author, dict):
                continue
            session = author.get("session")
            if not isinstance(session, str) or session not in entries:
                continue
            entries[session].append(
                RunEntry(
                    id=item["id"],
                    **{
                        key: item[key] if isinstance(item.get(key), str) else ""
                        for key in ("kind", "scope", "status", "title")
                    },
                )
            )
    except (OSError, subprocess.TimeoutExpired, ValueError, RecursionError):
        return entries
    return entries


def _cap(text: str) -> str:
    return text if len(text) <= 300 else text[:299] + "…"


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


@dataclass(frozen=True)
class BriefResult:
    text: str | None
    note: str | None


def fetch_brief(cwd: Path, run_id: str) -> BriefResult:
    """Fetch once on the host; unavailable Leeboard context never blocks a run."""
    try:
        binary = find_lb()
        if binary is None:
            return BriefResult(None, None)
        result = subprocess.run(
            [binary, "brief", "--cwd", str(cwd)],
            env=lb_environment(leeboard_identity(run_id)),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=BRIEF_TIMEOUT_S,
            shell=False,
            check=False,
        )
        if result.returncode != 0:
            message = _first_line(result.stderr)
            note = f"lb brief exited {result.returncode}" + (f": {message}" if message else "")
            return BriefResult(None, _cap(note))
        text = result.stdout.rstrip()
        if len(text) > BRIEF_MAX_CHARS:
            marker = "… brief truncated"
            prefix = text[: BRIEF_MAX_CHARS - len(marker) - 1]
            boundary = prefix.rfind("\n")
            text = prefix[:boundary] + "\n" + marker if boundary >= 0 else marker
        return BriefResult(text, None)
    except subprocess.TimeoutExpired:
        return BriefResult(None, f"lb brief timed out after {BRIEF_TIMEOUT_S:g}s")
    except Exception as exc:
        return BriefResult(
            None, _cap(f"lb brief failed: {_first_line(str(exc)) or type(exc).__name__}")
        )


@dataclass(frozen=True)
class HoldCheck:
    status: Literal["clear", "blocked", "unavailable"]
    holds: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    detail: str = ""

    def skip_text(self) -> str:
        reasons = []
        for hold in self.holds:
            reason = f"[{hold.get('id', '?')}] {hold.get('title', '')}"
            if hold.get("release"):
                reason += f" (release: {hold['release']})"
            reasons.append(reason)
        reason = "; ".join(reasons) or _first_line(self.text) or "lb check blocked (exit 3)"
        return _cap("leeboard hold: " + reason)

    def full_text(self) -> str:
        """Uncapped hold diagnostics for an interactive owner action."""
        reasons = []
        for hold in self.holds:
            reason = f"[{hold.get('id', '?')}] {hold.get('title', '')}"
            if hold.get("release"):
                reason += f" (release: {hold['release']})"
            if hold.get("until"):
                reason += f" (until: {hold['until']})"
            reasons.append(reason)
        return "leeboard hold: " + (
            "; ".join(reasons) or self.text.strip() or "lb check blocked (exit 3)"
        )

    def skip_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "leeboard_hold": [
                {key: hold[key] for key in ("id", "title", "release", "until") if hold.get(key)}
                for hold in self.holds
            ]
        }
        if not self.holds:
            payload["text"] = _cap(self.text)
        return payload


def check_hold(
    project: Path, identity: Mapping[str, str] | None = None, blocks: str = CHECK_TAG
) -> HoldCheck:
    """Only exit 3 blocks; an unavailable CLI always fails open."""
    try:
        binary = find_lb()
        if binary is None:
            return HoldCheck("clear")
        result = subprocess.run(
            [binary, "check", "--blocks", blocks, "--cwd", str(project), "--json"],
            env=lb_environment(identity if identity is not None else leeboard_identity(None)),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=CHECK_TIMEOUT_S,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return HoldCheck("unavailable", detail=f"timed out after {CHECK_TIMEOUT_S:g}s")
    except (OSError, ValueError) as exc:
        return HoldCheck("unavailable", detail=_cap(str(exc)))
    if result.returncode not in (0, 3):
        message = _first_line(result.stderr)
        detail = f"exit {result.returncode}" + (f": {message}" if message else "")
        return HoldCheck("unavailable", detail=_cap(detail))
    holds: list[dict[str, Any]] = []
    try:
        parsed = json.loads(result.stdout) if result.stdout.strip() else []
        if not isinstance(parsed, list) or any(not isinstance(h, dict) for h in parsed):
            raise ValueError("expected an array of holds")
        holds = parsed
    except ValueError:
        if result.returncode == 0:
            return HoldCheck("unavailable", detail="invalid JSON from lb check")
    if result.returncode == 3:
        return HoldCheck("blocked", holds=holds, text=result.stdout)
    return HoldCheck("clear")


class HoldGate:
    """Cache one scheduling pass; retain warning suppression across passes."""

    def __init__(self, warn: Callable[[str], None]) -> None:
        self.warn = warn
        self._checks: dict[str, HoldCheck] = {}
        self._warned: str | None = None

    def check(self, project: str) -> HoldCheck:
        if project not in self._checks:
            result = check_hold(Path(project))
            self._checks[project] = result
            if result.status == "unavailable":
                if result.detail != self._warned:
                    self.warn(
                        f"leeboard: hold check skipped ({result.detail}); "
                        "tasks are scheduled without it"
                    )
                    self._warned = result.detail
            else:
                self._warned = None
        return self._checks[project]

    def clear(self) -> None:
        self._checks.clear()
