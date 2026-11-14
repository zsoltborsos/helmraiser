"""Live subscription usage from Omarchy records, with an OpenCode Go API fallback.

The ledger only sees Helmraiser's own runs, so `spend.usage` can only estimate
subscription usage. Omarchy's OpenCode and Codex records (or the OpenCode Go
usage API without a usable record) replace the estimate; the estimate stays
as the fallback and for per-model attribution.

All network, auth-file and collector access lives in this module. The key and
the collector output are never logged or printed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from helmraiser import __version__

OPENCODE_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
CODEX_SOURCE = "omarchy-codex"
CODEX_COLLECTOR = "omarchy-agent-usage-codex"
OPENCODE_COLLECTOR = "omarchy-agent-usage-opencode"

# Cache: `status` and the widget refresh must not call the API every time.
CACHE_NAME = "live-usage.json"
CACHE_TTL = 5 * 60
# The Omarchy record's own freshness: the collector runs only when it is older.
OMARCHY_RECORD_MAX_AGE = 15 * 60

# OpenCode percent is an integer percentage, the collector's percent a fraction;
# both are normalised to a fraction here.
OPENCODE_WINDOW = {"rolling": "5h", "weekly": "week", "monthly": "month"}
OMARCHY_WINDOW_PREFIXES = (
    ("Session", "5h"),
    ("Rolling", "5h"),
    ("Weekly", "week"),
    ("Monthly", "month"),
)


def _usage_dir(name: str) -> Path:
    """The Omarchy agents usage directory, honouring ``XDG_STATE_HOME``."""
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state).expanduser() if state else Path.home() / ".local" / "state"
    return base / "omarchy" / "agents" / "usage" / name


def _json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def opencode_go_key() -> str | None:
    """The OpenCode Go API key: ``OPENCODE_API_KEY`` else pi's auth file."""
    key = os.environ.get("OPENCODE_API_KEY")
    if key:
        return str(key)
    auth = _json_file(Path.home() / ".pi" / "agent" / "auth.json")
    if isinstance(auth, dict):
        entry = auth.get("opencode-go")
        if isinstance(entry, dict) and entry.get("key"):
            return str(entry["key"])
    return None


def _iso_to_epoch(text: Any) -> float | None:
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def fetch_opencode_go(key: str, timeout: float = 10.0) -> dict[str, dict] | None:
    """Windows from ``GET /zen/go/v1/usage``, or None on any error."""
    request = urllib.request.Request(
        OPENCODE_USAGE_URL,
        # The endpoint answers 403 to urllib's default User-Agent.
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": f"helmraiser/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except Exception:  # network, HTTP status, timeout — any failure is None
        return None
    try:
        usage = json.loads(raw.decode("utf-8"))["usage"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(usage, dict):
        return None
    windows: dict[str, dict] = {}
    for source, window in OPENCODE_WINDOW.items():
        entry = usage.get(source)
        if not isinstance(entry, dict) or "percent" not in entry:
            continue
        try:
            fraction = float(entry["percent"]) / 100
        except (TypeError, ValueError):
            continue
        resets_at = _iso_to_epoch(entry.get("resetsAt"))
        if resets_at is None:
            continue
        windows[window] = {
            "fraction": fraction,
            "resets_at": resets_at,
            "status": entry.get("status") or "ok",
        }
    return windows or None


def _omarchy_windows(record: Any) -> dict[str, dict] | None:
    limits = record.get("limits") if isinstance(record, dict) else None
    if not isinstance(limits, list):
        return None
    windows: dict[str, dict] = {}
    for limit in limits:
        if not isinstance(limit, dict):
            continue
        label = str(limit.get("label", ""))
        window = next(
            (name for prefix, name in OMARCHY_WINDOW_PREFIXES if label.startswith(prefix)), None
        )
        if window is None or "percent" not in limit:
            continue
        try:
            fraction = float(limit["percent"])
        except (TypeError, ValueError):
            continue
        resets_at = _iso_to_epoch(limit.get("resetsAt"))
        if resets_at is None:
            continue
        windows[window] = {
            "fraction": fraction,
            "resets_at": resets_at,
        }
    return windows or None


def _read_omarchy_record(record_name: str) -> dict | None:
    record = _json_file(_usage_dir(record_name))
    return record if isinstance(record, dict) else None


def _omarchy_is_fresh(record: dict | None, now: float) -> bool:
    record = record or {}
    updated = _iso_to_epoch(record.get("updatedAt"))
    return (
        record.get("limitsStale") is not True
        and updated is not None
        and updated >= now - OMARCHY_RECORD_MAX_AGE
    )


def read_omarchy(
    record_name: str, collector: str, timeout: float = 20.0, now: float | None = None
) -> dict[str, dict] | None:
    """Omarchy limits, refreshed via the collector when stale or missing.

    Records up to 15 minutes old are used as is unless marked ``limitsStale``.
    Otherwise the collector runs with ``--limits-only`` when on ``PATH``.
    A stale record is still used when the collector is missing or fails.
    None when there is nothing usable.
    """
    now = time.time() if now is None else now
    record = _read_omarchy_record(record_name)
    if record is not None and _omarchy_is_fresh(record, now):
        return _omarchy_windows(record)
    executable = shutil.which(collector)
    if executable is not None:
        fresh = _run_omarchy_collector(executable, timeout)
        windows = _omarchy_windows(fresh) if fresh is not None else None
        if windows:
            return windows
    if record is not None:
        return _omarchy_windows(record)
    return None


def read_codex(timeout: float = 20.0, now: float | None = None) -> dict[str, dict] | None:
    """Codex limits from Omarchy's record, refreshed via its collector."""
    return read_omarchy("codex.json", CODEX_COLLECTOR, timeout=timeout, now=now)


def read_opencode(timeout: float = 20.0, now: float | None = None) -> dict[str, dict] | None:
    """OpenCode Go limits from Omarchy's record, refreshed via its collector."""
    windows = read_omarchy("opencode.json", OPENCODE_COLLECTOR, timeout=timeout, now=now)
    if not windows:
        return None
    return {window: {**entry, "status": "ok"} for window, entry in windows.items()}


def _run_omarchy_collector(executable: str, timeout: float) -> dict | None:
    try:
        result = subprocess.run(
            [executable, "--limits-only"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        parsed = json.loads(result.stdout)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _live_setting(config: dict[str, Any], provider: str) -> str | None:
    """The configured live source for ``provider``, None when it is off."""
    subs = config.get("subscriptions") if isinstance(config, dict) else None
    block = subs.get(provider) if isinstance(subs, dict) else None
    if not isinstance(block, dict):
        return None
    if "live" in block:
        live = block["live"]
        return str(live) if live else None
    return provider


def _cache_path(data_dir: Path | str | None) -> Path:
    base = Path(os.path.expanduser(str(data_dir))) if data_dir else _default_data_dir()
    return base / CACHE_NAME


def _default_data_dir() -> Path:
    from helmraiser.ledger.db import default_data_dir

    return default_data_dir()


def _read_cache(path: Path, now: float) -> dict[str, dict] | None:
    cache = _json_file(path)
    if not isinstance(cache, dict):
        return None
    try:
        fetched_at = float(cache["fetched_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if now - fetched_at >= CACHE_TTL:
        return None
    return {
        provider: value
        for provider, value in cache.items()
        if provider != "fetched_at" and isinstance(value, dict)
    }


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def _fetch_all(config: dict[str, Any], now: float) -> dict[str, dict]:
    fresh: dict[str, dict] = {}
    if _live_setting(config, "opencode-go"):
        windows = read_opencode(now=now)
        if not windows:
            key = opencode_go_key()
            windows = fetch_opencode_go(key) if key else None
        if windows:
            fresh["opencode-go"] = windows
    if _live_setting(config, "openai-codex"):
        windows = read_codex(now=now)
        if windows:
            fresh["openai-codex"] = windows
    return fresh


def live_usage(
    data_dir: Path | str | None,
    config: dict[str, Any],
    now: float | None = None,
    refresh: bool = False,
) -> dict[str, dict]:
    """Live subscription usage: ``{provider: {window: {fraction, resets_at}}}``.

    Reuses ``<data_dir>/live-usage.json`` when it is younger than 5 minutes so
    ``status`` and the widget refresh do not call the API each time;
    ``refresh=True`` forces a fresh fetch.
    """
    now = time.time() if now is None else now
    path = _cache_path(data_dir)
    if not refresh:
        cached = _read_cache(path, now)
        if cached is not None:
            return {
                provider: windows
                for provider, windows in cached.items()
                if _live_setting(config, provider) is not None
            }
    fresh = _fetch_all(config, now)
    payload = {"fetched_at": now, **fresh}
    _write_cache(path, payload)
    return fresh
