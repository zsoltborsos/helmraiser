"""Public task-origin contract tests; all state is temporary and no real model runs."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, commands, monitor
from helmraiser.config import DEFAULT_CONFIG, SUBSCRIPTION_TEMPLATES
from helmraiser.ledger.db import Ledger
from helmraiser.ledger.rundir import RunDir
from helmraiser.task.loader import TaskLoadError, parse_task, render_task
from helmraiser.utils.markdown import parse_frontmatter, serialize_frontmatter
from tests.conftest import configured_test_config
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    config = configured_test_config()
    monkeypatch.setattr(commands, "load_config", lambda: config)
    monkeypatch.setattr(monitor, "load_config", lambda: config)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    return config


def task_text(project: Path, *, origin: Any = None, include_origin: bool = True) -> str:
    frontmatter: dict[str, Any] = {
        "id": "origin-task",
        "title": "Origin task",
        "project": str(project),
        "status": "draft",
        "policy": "review",
        "isolation": "none",
        "verify": [{"name": "tests", "run": "true"}],
    }
    if include_origin:
        frontmatter["origin"] = origin
    return serialize_frontmatter(frontmatter, "## Goal\n\nTest origins.") + "\n"


def write_task(project: Path, text: str) -> Path:
    path = project / "topics" / "origin-task.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def invoke(*args: str, data_dir: Path):
    return runner.invoke(app, [*args, "--data-dir", str(data_dir)])


def disable_intake_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands, "get_intake_adapter", lambda config: None)


@pytest.mark.parametrize("kind", ["claude-code", "fo"])
def test_valid_origin_round_trips(kind: str, tmp_project: Path) -> None:
    origin = {"kind": kind, "session": "s" * 1024}
    text = task_text(tmp_project, origin=origin)

    spec = parse_task(text, tmp_project / "task.md", copy.deepcopy(DEFAULT_CONFIG))
    rendered = render_task(spec)

    assert spec.origin == origin
    assert parse_frontmatter(rendered)["frontmatter"]["origin"] == origin
    assert parse_task(rendered, tmp_project / "task.md", {}).origin == origin


def test_absent_origin_round_trips_as_absent(tmp_project: Path) -> None:
    spec = parse_task(task_text(tmp_project, include_origin=False), tmp_project / "task.md", {})

    assert spec.origin is None
    assert "origin" not in parse_frontmatter(render_task(spec))["frontmatter"]


@pytest.mark.parametrize(
    ("origin", "message"),
    [
        ("fo:session", "origin must be a mapping"),
        ({"kind": "fo"}, "origin must contain exactly kind and session"),
        (
            {"kind": "fo", "session": "session", "extra": True},
            "origin must contain exactly kind and session",
        ),
        (
            {"kind": "other", "session": "session"},
            "origin.kind must be one of claude-code, fo",
        ),
        ({"kind": "fo", "session": 3}, "origin.session must be a non-empty string"),
        ({"kind": "fo", "session": ""}, "origin.session must be a non-empty string"),
        (
            {"kind": "fo", "session": "s" * 1025},
            "origin.session must be at most 1024 characters",
        ),
        (
            {"kind": "fo", "session": "bad\x7fsession"},
            "origin.session must not contain control characters",
        ),
    ],
)
def test_malformed_file_origin_exits_2(
    origin: Any, message: str, tmp_project: Path, tmp_path: Path
) -> None:
    path = write_task(tmp_project, task_text(tmp_project, origin=origin))

    result = runner.invoke(
        app,
        ["add", str(path), "--json", "--data-dir", str(tmp_path / "data")],
    )

    assert result.exit_code == 2
    assert message in result.output


def test_origin_from_malformed_yaml_is_a_load_error() -> None:
    from helmraiser.task.loader import origin_from_text

    with pytest.raises(TaskLoadError, match="invalid YAML frontmatter"):
        origin_from_text("---\norigin: [\n---\n")


def test_intake_yes_stamps_file_and_ledger_and_splits_first_colon(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    path = write_task(tmp_project, task_text(tmp_project, include_origin=False))
    disable_intake_model(monkeypatch)

    result = invoke(
        "intake",
        str(path),
        "--yes",
        "--origin",
        "fo:/sessions/one:two",
        "--json",
        data_dir=data_dir,
    )

    assert result.exit_code == 0, result.output
    expected = {"kind": "fo", "session": "/sessions/one:two"}
    assert parse_frontmatter(path.read_text())["frontmatter"]["origin"] == expected
    row = Ledger(data_dir / "helmraiser.db").get_task(json.loads(result.output)["task_id"])
    assert row is not None
    assert parse_frontmatter(row["snapshot"])["frontmatter"]["origin"] == expected


def test_intake_replaces_existing_origin_in_file_and_ledger(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    old = {"kind": "claude-code", "session": "old"}
    path = write_task(tmp_project, task_text(tmp_project, origin=old))
    disable_intake_model(monkeypatch)

    result = invoke(
        "intake",
        str(path),
        "--yes",
        "--origin",
        "fo:new",
        "--json",
        data_dir=data_dir,
    )

    expected = {"kind": "fo", "session": "new"}
    assert result.exit_code == 0, result.output
    assert parse_task(path.read_text(), path, {}).origin == expected
    row = Ledger(data_dir / "helmraiser.db").get_task(json.loads(result.output)["task_id"])
    assert row is not None and parse_task(row["snapshot"], path, {}).origin == expected


def test_intake_propose_stamps_origin_but_keeps_draft(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    path = write_task(tmp_project, task_text(tmp_project, include_origin=False))
    disable_intake_model(monkeypatch)

    result = invoke(
        "intake",
        str(path),
        "--propose",
        "--origin",
        "claude-code:session-id",
        "--json",
        data_dir=data_dir,
    )

    assert result.exit_code == 0, result.output
    expected = {"kind": "claude-code", "session": "session-id"}
    assert parse_task(path.read_text(), path, {}).origin == expected
    row = Ledger(data_dir / "helmraiser.db").get_task(json.loads(result.output)["task_id"])
    assert row is not None and row["status"] == "draft"
    assert parse_task(row["snapshot"], path, {}).origin == expected


def test_model_proposal_cannot_set_or_replace_origin(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    text = task_text(tmp_project, include_origin=False)
    frontmatter = parse_frontmatter(text)["frontmatter"]
    frontmatter.pop("policy")
    frontmatter.pop("verify")
    path = write_task(
        tmp_project,
        serialize_frontmatter(frontmatter, "## Goal\n\nTest origins.") + "\n",
    )
    proposal = """```yaml
policy: review
isolation: none
verify:
  - name: tests
    run: pytest -q
origin:
  kind: claude-code
  session: model-owned
```"""
    adapter = ScriptedPiAdapter({"intake": PiScript(final_text=proposal)})
    monkeypatch.setattr(commands, "get_intake_adapter", lambda config: adapter)

    result = invoke(
        "intake",
        str(path),
        "--yes",
        "--origin",
        "fo:caller-owned",
        "--json",
        data_dir=data_dir,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["proposal"].get("origin") is None
    assert parse_task(path.read_text(), path, {}).origin == {
        "kind": "fo",
        "session": "caller-owned",
    }

    frontmatter["id"] = "proposal-no-origin"
    frontmatter["title"] = "Proposal no origin"
    absent_path = path.with_name("proposal-no-origin.md")
    absent_path.write_text(
        serialize_frontmatter(frontmatter, "## Goal\n\nTest origins.") + "\n",
        encoding="utf-8",
    )
    absent = invoke("intake", str(absent_path), "--yes", "--json", data_dir=data_dir)
    assert absent.exit_code == 0, absent.output
    assert parse_task(absent_path.read_text(), absent_path, {}).origin is None


def test_intake_without_override_preserves_existing_origin(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = {"kind": "claude-code", "session": "existing-session"}
    path = write_task(tmp_project, task_text(tmp_project, origin=existing))
    disable_intake_model(monkeypatch)

    result = invoke("intake", str(path), "--yes", "--json", data_dir=tmp_path / "data")

    assert result.exit_code == 0, result.output
    assert parse_task(path.read_text(), path, {}).origin == existing


def test_editor_cannot_override_cli_origin(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_task(tmp_project, task_text(tmp_project, include_origin=False))
    disable_intake_model(monkeypatch)
    monkeypatch.setattr(commands, "_is_tty", lambda: True)

    def edit_origin(text: str, extension: str) -> str:
        assert extension == ".md"
        return text.replace("session: authoritative", "session: editor-owned")

    monkeypatch.setattr(commands.click, "edit", edit_origin)

    result = invoke(
        "intake",
        str(path),
        "--origin",
        "fo:authoritative",
        data_dir=tmp_path / "data",
    )

    assert result.exit_code == 0, result.output
    assert parse_task(path.read_text(), path, {}).origin == {
        "kind": "fo",
        "session": "authoritative",
    }


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("other:session", "origin.kind"),
        ("fo:", "origin.session must be a non-empty string"),
        ("fo:bad\x7fsession", "control characters"),
        ("claude-code:" + "s" * 1025, "at most 1024"),
    ],
)
def test_invalid_origin_option_exits_2_before_adapter_call(
    value: str,
    message: str,
    tmp_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_task(tmp_project, task_text(tmp_project, include_origin=False))

    def unexpected_adapter(config: dict[str, Any]) -> None:
        raise AssertionError("intake adapter must not be requested")

    monkeypatch.setattr(commands, "get_intake_adapter", unexpected_adapter)

    result = invoke(
        "intake",
        str(path),
        "--yes",
        "--origin",
        value,
        "--json",
        data_dir=tmp_path / "data",
    )

    assert result.exit_code == 2
    assert message in result.output


def _create_run(ledger: Ledger, task_id: str, run_id: str, status: str, now: float) -> None:
    task = ledger.get_task(task_id)
    assert task is not None
    ledger.create_run(
        {
            "run_id": run_id,
            "task_id": task_id,
            "project": task["project"],
            "status": status,
            "stage": "implement",
            "policy": "review",
            "isolation": "none",
            "model_implement": "opencode-go/implement",
            "model_review": "openai-codex/review",
            "pi_version": "test",
            "started_at": now - 60,
            "ended_at": now - 30 if status in {"failed", "landed"} else None,
        }
    )


def test_status_json_origins_use_task_and_run_snapshots_for_every_lane(
    tmp_project: Path,
    tmp_path: Path,
    isolated_config: dict[str, Any],
) -> None:
    data_dir = tmp_path / "data"
    ledger = Ledger(data_dir / "helmraiser.db")
    queue_origin = {"kind": "claude-code", "session": "queued-session"}
    queue_text = task_text(tmp_project, origin=queue_origin).replace(
        "status: draft", "status: queued"
    )
    queue_path = write_task(
        tmp_project,
        task_text(tmp_project, origin={"kind": "fo", "session": "mutable-file"}),
    )
    queue_spec = parse_task(queue_text, queue_path.resolve(), isolated_config)
    task_id = ledger.upsert_task(queue_spec, queue_text)

    # This task is both held by the active project run and never-startable by projection.
    for name in ("implement", "fallback", "review"):
        isolated_config["profiles"][name]["model"] = f"opencode-go/{name}"
    isolated_config["subscriptions"]["opencode-go"] = copy.deepcopy(
        SUBSCRIPTION_TEMPLATES["opencode-go"]
    )
    isolated_config["subscriptions"]["opencode-go"]["stop_at"] = 0.7
    isolated_config["subscriptions"]["opencode-go"]["projection_usd"] = 5.0

    now = time.time()
    statuses = {
        "running": "run-running",
        "resuming": "run-resuming",
        "awaiting_approval": "run-awaiting",
        "failed": "run-failed",
        "landed": "run-landed",
    }
    expected: dict[str, dict[str, str] | None] = {}
    for index, (status, run_id) in enumerate(statuses.items()):
        _create_run(ledger, task_id, run_id, status, now)
        if status == "landed":
            expected[run_id] = None
            continue
        origin = {"kind": "fo", "session": f"{status}-{index}"}
        expected[run_id] = origin
        rundir = RunDir(run_id, data_dir).create()
        rundir.task_md.write_text(task_text(tmp_project, origin=origin), encoding="utf-8")

    result = invoke("status", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    queue_row = next(row for row in payload["queue_next"] if row["task_id"] == task_id)
    assert queue_row["origin"] == queue_origin
    held_row = next(row for row in payload["held"] if row["task_id"] == task_id)
    assert held_row["origin"] == queue_origin
    never_row = next(row for row in payload["never_startable"] if row["task_id"] == task_id)
    assert never_row["origin"] == queue_origin
    assert payload["running"][0]["origin"] == expected["run-running"]
    assert payload["resuming"][0]["origin"] == expected["run-resuming"]
    assert payload["awaiting"][0]["origin"] == expected["run-awaiting"]
    assert payload["failed"][0]["origin"] == expected["run-failed"]
    assert payload["landed"][0]["origin"] is None


def test_status_treats_invalid_utf8_run_snapshot_as_absent_origin(
    tmp_project: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    ledger = Ledger(data_dir / "helmraiser.db")
    text = task_text(tmp_project, include_origin=False)
    spec = parse_task(text, tmp_project / "task.md", {})
    task_id = ledger.upsert_task(spec, text)
    _create_run(ledger, task_id, "run-invalid-utf8", "running", time.time())
    rundir = RunDir("run-invalid-utf8", data_dir).create()
    rundir.task_md.write_bytes(b"\xff")

    result = invoke("status", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["running"][0]["origin"] is None


def test_human_status_shows_short_origin_only_for_awaiting(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "240")
    data_dir = tmp_path / "data"
    ledger = Ledger(data_dir / "helmraiser.db")
    text = task_text(tmp_project, include_origin=False)
    spec = parse_task(text, tmp_project / "task.md", {})
    task_id = ledger.upsert_task(spec, text)
    now = time.time()
    _create_run(ledger, task_id, "run-awaiting", "awaiting_approval", now)
    _create_run(ledger, task_id, "run-awaiting-claude", "awaiting_approval", now)
    _create_run(ledger, task_id, "run-awaiting-absent", "awaiting_approval", now)
    _create_run(ledger, task_id, "run-running", "running", now)
    for run_id, origin in (
        ("run-awaiting", {"kind": "fo", "session": "abcdefgh-more"}),
        (
            "run-awaiting-claude",
            {"kind": "claude-code", "session": "12345678-more"},
        ),
        ("run-running", {"kind": "fo", "session": "running-origin"}),
    ):
        rundir = RunDir(run_id, data_dir).create()
        rundir.task_md.write_text(task_text(tmp_project, origin=origin), encoding="utf-8")

    result = invoke("status", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    assert "(origin: fo:abcdefgh)" in result.output
    assert "(origin: claude-code:12345678)" in result.output
    assert result.output.count("(origin:") == 2
    assert "origin: fo:running-" not in result.output


def test_drafts_json_uses_current_file_origin_and_absolute_source_path(
    tmp_project: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    ledger = Ledger(data_dir / "helmraiser.db")

    path = tmp_project / ".helmraiser" / "tasks" / "origin-task.md"
    path.parent.mkdir(parents=True)
    snapshot = task_text(tmp_project, origin={"kind": "claude-code", "session": "ledger-origin"})
    spec = parse_task(snapshot, path.resolve(), {})
    ledger.upsert_task(spec, snapshot)
    current_origin = {"kind": "fo", "session": "current-file-origin"}
    path.write_text(task_text(tmp_project, origin=current_origin), encoding="utf-8")

    scout_path = path.with_name("scout-task.md")
    scout_text = (
        task_text(tmp_project, include_origin=False)
        .replace("id: origin-task", "id: scout-task")
        .replace("title: Origin task", "title: Scout task")
    )
    scout_path.write_text(scout_text, encoding="utf-8")
    scout_spec = parse_task(scout_text, scout_path.resolve(), {})
    ledger.upsert_task(scout_spec, scout_text)

    result = invoke("drafts", "--all", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    rows = {row["id"]: row for row in json.loads(result.output)}
    assert rows["origin-task"]["origin"] == current_origin
    assert rows["origin-task"]["path"] == str(path.resolve())
    assert rows["scout-task"]["origin"] is None
    assert rows["scout-task"]["path"] == str(scout_path.resolve())
