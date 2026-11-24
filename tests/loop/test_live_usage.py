"""Live subscription usage from Omarchy records and the OpenCode Go API fallback.

No network: ``urlopen`` and the collectors are stubbed, and HOME and
XDG_STATE_HOME point at ``tmp_path``.
"""

from __future__ import annotations

import copy
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from helmraiser.config import DEFAULT_CONFIG, SUBSCRIPTION_TEMPLATES
from helmraiser.loop import live_usage as lu
from helmraiser.loop import spend
from helmraiser.loop.live_usage import (
    fetch_opencode_go,
    live_usage,
    opencode_go_key,
    read_codex,
)

NOW = datetime(2026, 9, 24, 15, 0, tzinfo=UTC).timestamp()

# Captured before the autouse conftest fixture stubs the module's fetchers.
REAL_FETCH = lu.fetch_opencode_go
REAL_READ_OPENCODE = lu.read_opencode


def _config(**subs: Any) -> dict:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["subscriptions"] = copy.deepcopy(SUBSCRIPTION_TEMPLATES)
    for name, overrides in subs.items():
        config["subscriptions"][name].update(overrides)
    return config


# ── Environment and stubs ──────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("XDG_STATE_HOME", str(path / "state"))
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setattr(lu.shutil, "which", lambda _name: None)
    return path


def _opencode_payload() -> dict:
    return {
        "usage": {
            "rolling": {"status": "ok", "percent": 7, "resetsAt": "2026-09-24T14:28:19.363Z"},
            "weekly": {"status": "ok", "percent": 4, "resetsAt": "2026-09-28T00:00:00.000Z"},
            "monthly": {
                "status": "rate-limited",
                "percent": 2,
                "resetsAt": "2026-10-17T17:00:00.000Z",
            },
        }
    }


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class FakeUrlopen:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.response: Any = b"{}"

    def __call__(self, request: Any, timeout: float = 0) -> _FakeResponse:
        self.requests.append(
            {
                "url": request.get_full_url(),
                "auth": request.get_header("Authorization"),
                "user_agent": request.get_header("User-agent"),
                "timeout": timeout,
            }
        )
        if isinstance(self.response, Exception):
            raise self.response
        return _FakeResponse(self.response)


@pytest.fixture
def urlopen(monkeypatch: pytest.MonkeyPatch) -> FakeUrlopen:
    fake = FakeUrlopen()
    monkeypatch.setattr(lu.urllib.request, "urlopen", fake)
    return fake


def _codex_record(fraction: float = 0.61, updated_offset: float = 0.0) -> dict:
    return {
        "updatedAt": datetime.fromtimestamp(NOW + updated_offset, tz=UTC).isoformat(),
        "limits": [
            {
                "label": "Weekly (7-day)",
                "percent": fraction,
                "resetsAt": "2026-09-28T09:41:28+00:00",
            }
        ],
    }


def _opencode_record(updated_offset: float = 0.0) -> dict:
    return {
        "schemaVersion": 1,
        "id": "opencode",
        "name": "OpenCode",
        "updatedAt": datetime.fromtimestamp(NOW + updated_offset, tz=UTC).isoformat(),
        "ready": True,
        "tierLabel": "Go",
        "limits": [
            {"label": "Rolling (5h)", "percent": 0.0, "resetsAt": "2026-09-24T20:00:00Z"},
            {"label": "Weekly (7-day)", "percent": 0.0, "resetsAt": "2026-09-28T00:00:00Z"},
            {"label": "Monthly", "percent": 0.14, "resetsAt": "2026-10-17T17:20:25Z"},
        ],
        "limitsStale": False,
    }


def _write_record(home: Path, name: str, record: dict) -> None:
    usage_dir = home / "state" / "omarchy" / "agents" / "usage"
    usage_dir.mkdir(parents=True, exist_ok=True)
    (usage_dir / name).write_text(json.dumps(record))


def _forbid_api(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("API access must not occur")


@pytest.fixture
def opencode_source(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lu, "read_opencode", REAL_READ_OPENCODE)
    monkeypatch.setattr(lu, "opencode_go_key", _forbid_api)
    monkeypatch.setattr(lu, "fetch_opencode_go", _forbid_api)


def _seed_go_spend(ledger: Any, make_task: Any, cost: float, when: float) -> None:
    task = make_task()
    ledger.upsert_task(task, "snapshot")
    ledger.create_run(
        {
            "run_id": f"run-{cost}-{when}",
            "task_id": task.task_id,
            "project": str(task.project),
            "status": "landed",
            "policy": "review",
            "isolation": "none",
            "model_implement": "opencode-go/glm-5.3-flash",
            "model_review": "opencode-go/glm-5.3-flash",
            "pi_version": "0.87.1",
            "cost_usd": cost,
            "started_at": when,
            "ended_at": when,
        }
    )


# ── OpenCode Go ────────────────────────────────────────────────────────


def test_opencode_response_parsed_and_mapped(home: Path, urlopen: FakeUrlopen) -> None:
    urlopen.response = json.dumps(_opencode_payload()).encode()

    windows = fetch_opencode_go("secret-key")

    assert windows == {
        "5h": {
            "fraction": 0.07,
            "resets_at": datetime(2026, 9, 24, 14, 28, 19, 363000, tzinfo=UTC).timestamp(),
            "status": "ok",
        },
        "week": {
            "fraction": 0.04,
            "resets_at": datetime(2026, 9, 28, 0, 0, tzinfo=UTC).timestamp(),
            "status": "ok",
        },
        "month": {
            "fraction": 0.02,
            "resets_at": datetime(2026, 10, 17, 17, 0, tzinfo=UTC).timestamp(),
            "status": "rate-limited",
        },
    }
    assert urlopen.requests[0]["url"] == lu.OPENCODE_USAGE_URL
    assert urlopen.requests[0]["auth"] == "Bearer secret-key"
    assert str(urlopen.requests[0]["user_agent"]).startswith("helmraiser/")


def test_opencode_errors_give_none(home: Path, urlopen: FakeUrlopen) -> None:
    key = "secret-key"

    urlopen.response = Exception("HTTP Error 401: Unauthorized")
    assert fetch_opencode_go(key) is None

    urlopen.response = TimeoutError("timed out")
    assert fetch_opencode_go(key) is None

    urlopen.response = b"<html>not json</html>"
    assert fetch_opencode_go(key) is None

    urlopen.response = json.dumps({"other": 1}).encode()
    assert fetch_opencode_go(key) is None

    urlopen.response = json.dumps({"usage": {"rolling": {"percent": "abc"}}}).encode()
    assert fetch_opencode_go(key) is None


def test_opencode_window_without_valid_reset_is_ignored(home: Path, urlopen: FakeUrlopen) -> None:
    payload = _opencode_payload()
    payload["usage"]["rolling"].pop("resetsAt")
    payload["usage"]["monthly"]["resetsAt"] = "not-a-timestamp"
    urlopen.response = json.dumps(payload).encode()

    windows = fetch_opencode_go("secret-key")

    assert windows is not None
    assert set(windows) == {"week"}


def test_key_from_environment_wins_over_auth_file(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_dir = home / ".pi" / "agent"
    auth_dir.mkdir(parents=True)
    (auth_dir / "auth.json").write_text(
        json.dumps({"opencode-go": {"type": "api_key", "key": "auth-file-key"}})
    )
    assert opencode_go_key() == "auth-file-key"

    monkeypatch.setenv("OPENCODE_API_KEY", "env-key")
    assert opencode_go_key() == "env-key"


def test_no_key_gives_none_without_a_request(
    home: Path, urlopen: FakeUrlopen, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_dir = home / ".pi" / "agent"
    auth_dir.mkdir(parents=True)
    (auth_dir / "auth.json").write_text(json.dumps({"opencode-go": {"type": "oauth"}}))

    assert opencode_go_key() is None
    assert live_usage(None, _config(), now=NOW) == {}
    assert urlopen.requests == []


def test_opencode_fresh_record_skips_api(
    home: Path, opencode_source: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_record(home, "opencode.json", _opencode_record())
    monkeypatch.setattr(lu.shutil, "which", _forbid_api)

    assert live_usage(tmp_path / "data", _config(), now=NOW) == {
        "opencode-go": {
            "5h": {
                "fraction": 0.0,
                "resets_at": datetime(2026, 9, 24, 20, tzinfo=UTC).timestamp(),
                "status": "ok",
            },
            "week": {
                "fraction": 0.0,
                "resets_at": datetime(2026, 9, 28, tzinfo=UTC).timestamp(),
                "status": "ok",
            },
            "month": {
                "fraction": 0.14,
                "resets_at": datetime(2026, 10, 17, 17, 20, 25, tzinfo=UTC).timestamp(),
                "status": "ok",
            },
        }
    }


@pytest.mark.parametrize("limits_stale", [False, True])
@pytest.mark.parametrize("collector", ["success", "missing", "nonzero", "oserror", "timeout"])
def test_opencode_stale_record_refresh_and_fallback(
    home: Path,
    opencode_source: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    limits_stale: bool,
    collector: str,
) -> None:
    record = _opencode_record(updated_offset=0 if limits_stale else -3600)
    record["limitsStale"] = limits_stale
    _write_record(home, "opencode.json", record)
    calls = []

    def which(name: str) -> str | None:
        assert name == "omarchy-agent-usage-opencode"
        return None if collector == "missing" else "/usr/bin/omarchy-agent-usage-opencode"

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        assert command == ["/usr/bin/omarchy-agent-usage-opencode", "--limits-only"]
        assert kwargs == {"timeout": 20.0, "capture_output": True, "text": True}
        calls.append(command)
        if collector == "oserror":
            raise OSError("collector failed")
        if collector == "timeout":
            raise subprocess.TimeoutExpired(command, 20.0)
        replacement = _opencode_record()
        replacement["limits"][0]["percent"] = 0.42
        return subprocess.CompletedProcess(
            command, 1 if collector == "nonzero" else 0, stdout=json.dumps(replacement)
        )

    monkeypatch.setattr(lu.shutil, "which", which)
    monkeypatch.setattr(lu.subprocess, "run", run)

    windows = live_usage(tmp_path / "data", _config(), now=NOW)["opencode-go"]

    assert len(calls) == (0 if collector == "missing" else 1)
    assert windows["5h"]["fraction"] == (0.42 if collector == "success" else 0.0)
    assert windows["month"]["fraction"] == 0.14
    assert all(entry["status"] == "ok" for entry in windows.values())


@pytest.mark.parametrize("record_kind", ["missing", "unusable"])
def test_opencode_api_fallback_without_usable_record(
    home: Path,
    opencode_source: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    record_kind: str,
) -> None:
    if record_kind == "unusable":
        record = _opencode_record()
        record["limits"] = [{"label": "Rolling (5h)", "percent": "invalid"}]
        _write_record(home, "opencode.json", record)
    calls = []
    expected = _live_windows(NOW)
    expected["month"]["status"] = "rate-limited"

    def fetch(key: str) -> dict:
        calls.append(key)
        return expected

    monkeypatch.setattr(lu, "opencode_go_key", lambda: "fake-key")
    monkeypatch.setattr(lu, "fetch_opencode_go", fetch)

    assert live_usage(tmp_path / "data", _config(), now=NOW) == {"opencode-go": expected}
    assert calls == ["fake-key"]


@pytest.mark.parametrize("stdout", ["", "not-json", "[]", '{"limits": []}'])
def test_opencode_unusable_collector_output_falls_back_to_api(
    opencode_source: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stdout: str,
) -> None:
    calls = []
    expected = _live_windows(NOW)
    expected["month"]["status"] = "rate-limited"

    def which(name: str) -> str:
        assert name == "omarchy-agent-usage-opencode"
        return "/usr/bin/omarchy-agent-usage-opencode"

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        assert command == ["/usr/bin/omarchy-agent-usage-opencode", "--limits-only"]
        assert kwargs == {"timeout": 20.0, "capture_output": True, "text": True}
        calls.append("collector")
        return subprocess.CompletedProcess(command, 0, stdout=stdout)

    def key() -> str:
        calls.append("key")
        return "fake-key"

    def fetch(api_key: str) -> dict:
        assert api_key == "fake-key"
        calls.append("fetch")
        return expected

    monkeypatch.setattr(lu.shutil, "which", which)
    monkeypatch.setattr(lu.subprocess, "run", run)
    monkeypatch.setattr(lu, "opencode_go_key", key)
    monkeypatch.setattr(lu, "fetch_opencode_go", fetch)

    assert live_usage(tmp_path / "data", _config(), now=NOW) == {"opencode-go": expected}
    assert calls == ["collector", "key", "fetch"]


# ── Codex ──────────────────────────────────────────────────────────────


def test_codex_fresh_record_used_without_collector(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage_dir = home / "state" / "omarchy" / "agents" / "usage"
    usage_dir.mkdir(parents=True)
    (usage_dir / "codex.json").write_text(json.dumps(_codex_record()))
    monkeypatch.setattr(
        lu.shutil, "which", lambda _name: (_ for _ in ()).throw(AssertionError("no call"))
    )

    windows = read_codex(now=NOW)

    assert windows == {
        "week": {
            "fraction": 0.61,
            "resets_at": datetime(2026, 9, 28, 9, 41, 28, tzinfo=UTC).timestamp(),
        }
    }


def test_codex_stale_record_refreshed_by_collector(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage_dir = home / "state" / "omarchy" / "agents" / "usage"
    usage_dir.mkdir(parents=True)
    (usage_dir / "codex.json").write_text(json.dumps(_codex_record(updated_offset=-3600)))

    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        assert kwargs == {"timeout": 20.0, "capture_output": True, "text": True}
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(_codex_record()), stderr=""
        )

    monkeypatch.setattr(lu.shutil, "which", lambda _name: "/usr/bin/omarchy-agent-usage-codex")
    monkeypatch.setattr(lu.subprocess, "run", run)

    windows = read_codex(now=NOW)

    assert calls == [[calls[0][0], "--limits-only"]]
    assert calls[0][0].endswith("omarchy-agent-usage-codex")
    assert windows is not None and windows["week"]["fraction"] == pytest.approx(0.61)


def test_codex_collector_missing_uses_stale_record(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage_dir = home / "state" / "omarchy" / "agents" / "usage"
    usage_dir.mkdir(parents=True)
    (usage_dir / "codex.json").write_text(json.dumps(_codex_record(updated_offset=-3600)))
    monkeypatch.setattr(lu.shutil, "which", lambda _name: None)

    windows = read_codex(now=NOW)

    assert windows is not None and windows["week"]["fraction"] == pytest.approx(0.61)


def test_codex_collector_failure_uses_stale_record(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage_dir = home / "state" / "omarchy" / "agents" / "usage"
    usage_dir.mkdir(parents=True)
    (usage_dir / "codex.json").write_text(json.dumps(_codex_record(updated_offset=-3600)))
    monkeypatch.setattr(lu.shutil, "which", lambda _name: "/usr/bin/collector")
    monkeypatch.setattr(lu.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError))

    windows = read_codex(now=NOW)

    assert windows is not None and windows["week"]["fraction"] == pytest.approx(0.61)


def test_codex_labels_mapped(home: Path) -> None:
    record = {
        "updatedAt": datetime.fromtimestamp(NOW, tz=UTC).isoformat(),
        "limits": [
            {"label": "Session (5-hour)", "percent": 0.2, "resetsAt": "2026-09-24T17:00:00+00:00"},
            {"label": "Weekly (7-day)", "percent": 0.61, "resetsAt": "2026-09-28T09:41:28+00:00"},
            {"label": "Monthly", "percent": 0.14, "resetsAt": "2026-10-17T17:00:00+00:00"},
        ],
    }

    windows = lu._omarchy_windows(record)

    assert set(windows) == {"5h", "week", "month"}
    assert windows["month"]["fraction"] == pytest.approx(0.14)
    assert windows["5h"]["fraction"] == pytest.approx(0.2)
    assert windows["5h"]["resets_at"] == datetime(2026, 9, 24, 17, 0, tzinfo=UTC).timestamp()


def test_codex_nothing_usable_gives_none(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lu.shutil, "which", lambda _name: None)
    assert read_codex(now=NOW) is None


def test_codex_limit_without_valid_reset_is_ignored(home: Path) -> None:
    record = _codex_record()
    record["limits"][0]["resetsAt"] = "not-a-timestamp"

    assert lu._omarchy_windows(record) is None


def test_codex_uses_collector_when_record_is_missing(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lu.shutil, "which", lambda _name: "/usr/bin/collector")
    monkeypatch.setattr(
        lu.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(_codex_record()), stderr=""
        ),
    )

    windows = read_codex(now=NOW)

    assert windows is not None and windows["week"]["fraction"] == pytest.approx(0.61)


@pytest.mark.parametrize("collector_succeeds", [True, False])
def test_codex_limits_stale_triggers_refresh(
    home: Path, monkeypatch: pytest.MonkeyPatch, collector_succeeds: bool
) -> None:
    usage_dir = home / "state" / "omarchy" / "agents" / "usage"
    usage_dir.mkdir(parents=True)
    record = _codex_record()
    record["limitsStale"] = True
    (usage_dir / "codex.json").write_text(json.dumps(record))
    calls = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        assert command == ["/usr/bin/collector", "--limits-only"]
        assert kwargs == {"timeout": 20.0, "capture_output": True, "text": True}
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0 if collector_succeeds else 1, stdout=json.dumps(_codex_record(0.2))
        )

    monkeypatch.setattr(lu.shutil, "which", lambda _name: "/usr/bin/collector")
    monkeypatch.setattr(lu.subprocess, "run", run)

    windows = read_codex(now=NOW)

    assert len(calls) == 1
    assert windows["week"]["fraction"] == (0.2 if collector_succeeds else 0.61)


@pytest.mark.parametrize("age", [900, 901])
def test_codex_freshness_boundary(home: Path, monkeypatch: pytest.MonkeyPatch, age: int) -> None:
    usage_dir = home / "state" / "omarchy" / "agents" / "usage"
    usage_dir.mkdir(parents=True)
    (usage_dir / "codex.json").write_text(json.dumps(_codex_record(updated_offset=-age)))
    calls = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(_codex_record(0.2)))

    monkeypatch.setattr(lu.shutil, "which", lambda _name: "/usr/bin/collector")
    monkeypatch.setattr(lu.subprocess, "run", run)

    windows = read_codex(now=NOW)

    assert len(calls) == (0 if age == 900 else 1)
    assert windows["week"]["fraction"] == (0.61 if age == 900 else 0.2)


# ── Cache ──────────────────────────────────────────────────────────────


def test_cache_reused_within_five_minutes(
    home: Path, monkeypatch: pytest.MonkeyPatch, urlopen: FakeUrlopen, tmp_path: Path
) -> None:
    config = _config()
    data_dir = tmp_path / "data"
    monkeypatch.setattr(lu, "fetch_opencode_go", REAL_FETCH)
    monkeypatch.setattr(lu, "opencode_go_key", lambda: "secret-key")

    urlopen.response = json.dumps(_opencode_payload()).encode()
    first = live_usage(data_dir, config, now=NOW)
    assert "opencode-go" in first

    urlopen.response = Exception("must not be called again")
    assert live_usage(data_dir, config, now=NOW + 60) == first
    assert len(urlopen.requests) == 1

    # After 5 minutes the cache is stale and the fetch happens again.
    urlopen.response = json.dumps(_opencode_payload()).encode()
    assert live_usage(data_dir, config, now=NOW + 301)
    assert len(urlopen.requests) == 2


def test_refresh_bypasses_the_cache(
    home: Path, monkeypatch: pytest.MonkeyPatch, urlopen: FakeUrlopen, tmp_path: Path
) -> None:
    config = _config()
    data_dir = tmp_path / "data"
    monkeypatch.setattr(lu, "fetch_opencode_go", REAL_FETCH)
    monkeypatch.setattr(lu, "opencode_go_key", lambda: "secret-key")
    urlopen.response = json.dumps(_opencode_payload()).encode()

    live_usage(data_dir, config, now=NOW)
    refreshed = live_usage(data_dir, config, now=NOW + 60, refresh=True)

    assert len(urlopen.requests) == 2
    assert "opencode-go" in refreshed


def test_cache_shape(
    home: Path, monkeypatch: pytest.MonkeyPatch, urlopen: FakeUrlopen, tmp_path: Path
) -> None:
    config = _config()
    data_dir = tmp_path / "data"
    monkeypatch.setattr(lu, "fetch_opencode_go", REAL_FETCH)
    monkeypatch.setattr(lu, "opencode_go_key", lambda: "secret-key")
    urlopen.response = json.dumps(_opencode_payload()).encode()

    live_usage(data_dir, config, now=NOW)

    cache = json.loads((data_dir / "live-usage.json").read_text())
    assert set(cache) == {"fetched_at", "opencode-go"}
    assert cache["fetched_at"] == pytest.approx(NOW)
    assert cache["opencode-go"]["5h"]["fraction"] == pytest.approx(0.07)


@pytest.mark.parametrize("disabled", [None, False])
def test_live_disabled_by_config(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    urlopen: FakeUrlopen,
    tmp_path: Path,
    disabled: bool | None,
) -> None:
    config = _config(**{"opencode-go": {"live": disabled}, "openai-codex": {"live": disabled}})
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "live-usage.json").write_text(
        json.dumps(
            {
                "fetched_at": NOW,
                "opencode-go": {"5h": {"fraction": 0.5}},
                "openai-codex": {"week": {"fraction": 0.5}},
            }
        )
    )
    for name in ("opencode_go_key", "fetch_opencode_go", "read_codex", "read_opencode"):
        monkeypatch.setattr(lu, name, _forbid_api)

    assert live_usage(data_dir, config, now=NOW) == {}
    assert live_usage(data_dir, config, now=NOW, refresh=True) == {}
    assert urlopen.requests == []


# ── Live values merged into the usage meter ────────────────────────────


def _live_windows(now: float) -> dict:
    return {
        "5h": {"fraction": 0.07, "resets_at": now + 300, "status": "ok"},
        "week": {"fraction": 0.04, "resets_at": now + 3000, "status": "ok"},
        "month": {"fraction": 0.02, "resets_at": now + 30000, "status": "ok"},
    }


def _warned_live_windows(now: float) -> dict:
    windows = _live_windows(now)
    windows["5h"] = {"fraction": 0.85, "resets_at": now + 300, "status": "ok"}
    return windows


def test_usage_prefers_live_values_and_keeps_the_estimate(
    home: Path, ledger: Any, make_task: Any
) -> None:
    now = 1_790_000_000.0
    _seed_go_spend(ledger, make_task, 12.0, when=now - 60)
    config = _config()

    windows = spend.usage(ledger, config, now, live={"opencode-go": _live_windows(now)})[
        "opencode-go"
    ]
    assert windows["5h"]["fraction"] == pytest.approx(0.07)
    assert windows["5h"]["source"] == "live"
    assert windows["5h"]["live_fraction"] == pytest.approx(0.07)
    assert windows["5h"]["estimate_fraction"] == pytest.approx(1.0)
    assert windows["5h"]["resets_at"] == pytest.approx(now + 300)
    assert windows["week"]["estimate_fraction"] == pytest.approx(0.4)


def test_usage_falls_back_when_live_is_none(home: Path, ledger: Any, make_task: Any) -> None:
    now = 1_790_000_000.0
    _seed_go_spend(ledger, make_task, 12.0, when=now - 60)
    config = _config()

    windows = spend.usage(ledger, config, now, live={})["opencode-go"]

    assert windows["week"]["fraction"] == pytest.approx(0.4)
    assert windows["week"]["source"] == "estimate"
    assert "live_fraction" not in windows["week"]


def test_warning_fires_on_the_live_value(home: Path, ledger: Any, make_task: Any) -> None:
    now = 1_790_000_000.0
    config = _config()

    windows = spend.usage(ledger, config, now, live={"opencode-go": _warned_live_windows(now)})
    resets = datetime.fromtimestamp(now + 300, tz=UTC).strftime("%a %H:%M UTC")

    assert spend.subscription_warnings(config, windows) == [
        f"opencode-go 5h usage 85% (warn at 80%), resets {resets}"
    ]


def test_stop_block_uses_the_live_value(
    home: Path, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from helmraiser.loop.spend import stop_block

    class GoTask:
        models: dict[str, Any] = {}

        def __init__(self) -> None:
            from helmraiser.task.model import Budget, ModelSpec

            self.models = {"implement": ModelSpec.parse("opencode-go/mystery:high")}
            self.budget = Budget(0.3, 90, 4)

    now = 1_790_000_000.0
    config = _config(**{"opencode-go": {"stop_at": 0.9}})
    live_windows = _warned_live_windows(now)
    live_windows["5h"] = {"fraction": 0.87, "resets_at": now + 300, "status": "ok"}
    live = {"opencode-go": live_windows}

    block = stop_block(spend.usage(ledger, config, now, live=live), config, GoTask(), now)

    assert block is not None and block["reason"] == "usage:opencode-go:5h"
    assert block["fraction"] == pytest.approx(0.87)


@pytest.mark.parametrize("provider", ["opencode-go", "openai-codex"])
@pytest.mark.parametrize("config", [{}, {"subscriptions": {}}, {"subscriptions": {"other": {}}}])
def test_live_setting_without_a_block_is_off(provider, config):
    assert lu._live_setting(config, provider) is None


@pytest.mark.parametrize("provider", ["opencode-go", "openai-codex"])
@pytest.mark.parametrize(
    "block, expected",
    [
        (None, None),
        (False, None),
        ({}, "provider"),
        ({"live": None}, None),
        ({"live": False}, None),
        ({"live": "source"}, "source"),
    ],
)
def test_live_setting_with_a_block(provider, block, expected):
    assert lu._live_setting({"subscriptions": {provider: block}}, provider) == (
        provider if expected == "provider" else expected
    )


@pytest.mark.parametrize("config", [{}, {"subscriptions": {}}])
def test_unconfigured_providers_are_not_fetched_or_returned_from_cache(
    tmp_path, monkeypatch, config
):
    (tmp_path / lu.CACHE_NAME).write_text(
        json.dumps(
            {
                "fetched_at": NOW,
                "opencode-go": {"5h": {"fraction": 0.5}},
                "openai-codex": {"week": {"fraction": 0.5}},
            }
        )
    )
    for name in ("opencode_go_key", "fetch_opencode_go", "read_opencode", "read_codex"):
        monkeypatch.setattr(lu, name, _forbid_api)
    assert live_usage(tmp_path, config, now=NOW) == {}
    assert live_usage(tmp_path, config, now=NOW, refresh=True) == {}


@pytest.mark.parametrize("provider", ["opencode-go", "openai-codex"])
def test_only_named_provider_is_fetched(tmp_path, monkeypatch, provider):
    windows = _live_windows(NOW)
    for name in ("opencode_go_key", "fetch_opencode_go", "read_opencode", "read_codex"):
        monkeypatch.setattr(lu, name, _forbid_api)
    reader = "read_opencode" if provider == "opencode-go" else "read_codex"
    monkeypatch.setattr(lu, reader, lambda **kwargs: windows)
    config = {"subscriptions": {provider: {}}}
    assert live_usage(tmp_path, config, now=NOW, refresh=True) == {provider: windows}
    assert live_usage(tmp_path, config, now=NOW) == {provider: windows}


def test_subscription_templates_include_live_sources() -> None:
    subs = SUBSCRIPTION_TEMPLATES
    assert subs["opencode-go"]["live"] == "opencode-go-api"
    assert subs["openai-codex"] == {"live": "omarchy-codex", "warn_at": 0.8, "stop_at": None}
