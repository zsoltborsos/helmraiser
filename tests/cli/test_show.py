"""``helmraiser show``: one run's state and limits, read from the ledger and the run's task
snapshot. Over a seeded ledger: no pi, no network. The runner-driven shapes (a rework
allowance that stops a run below ``max_iterations``) are in tests/loop/test_retry_budget.py."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, monitor
from helmraiser.config import DEFAULT_CONFIG
from helmraiser.ledger.db import Ledger
from helmraiser.ledger.rundir import RunDir
from helmraiser.task.loader import parse_task

runner = CliRunner()

TASK = """---
id: {id}
title: Fix it
project: {project}
status: queued
policy: review
isolation: none
budget:
  max_cost_usd: {cost}
  max_minutes: {minutes}
  max_iterations: {iterations}
verify:
  - name: marker
    run: test -f done.txt
---
## Goal

Make the widget work.
"""

RUN_ID = "0123abcd-0000-4000-8000-000000000001"
NOW = 1_800_000_000.0
SHOW_KEYS = {
    "run_id",
    "task_id",
    "project",
    "status",
    "stage",
    "failure_reason",
    "verdict",
    "models",
    "arm",
    "policy",
    "delivery",
    "base_branch",
    "base_commit",
    "head_commit",
    "branch_name",
    "pr_url",
    "worktree_path",
    "worktree_exists",
    "started_at",
    "ended_at",
    "pending_approval",
    "rework_rounds",
    "landed_outside",
    "warnings",
    "limits",
}


@pytest.fixture(autouse=True)
def cli_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monitor, "load_config", lambda: copy.deepcopy(DEFAULT_CONFIG))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def ledger(data_dir: Path) -> Ledger:
    return Ledger(data_dir / "helmraiser.db")


def seed(
    ledger: Ledger,
    data_dir: Path,
    project: Path,
    *,
    run_id: str = RUN_ID,
    status: str = "failed",
    cost: float = 5.0,
    minutes: int = 90,
    iterations: int = 4,
    snapshot_file: bool = True,
    **row: Any,
) -> str:
    text = TASK.format(
        id="fix-widget", project=project, cost=cost, minutes=minutes, iterations=iterations
    )
    spec = parse_task(text, project / "topics" / "fix-widget.md", copy.deepcopy(DEFAULT_CONFIG))
    task_id = ledger.upsert_task(spec, text)
    ledger.create_run(
        {
            "run_id": run_id,
            "task_id": task_id,
            "project": str(project),
            "status": status,
            "policy": "review",
            "isolation": "none",
            "model_implement": "prov/impl",
            "model_review": "prov/rev",
            "pi_version": "0.87.1",
            "started_at": NOW - 7200,
            "ended_at": NOW - 3600 if status in ("failed", "landed") else None,
            **row,
        }
    )
    if snapshot_file:
        rundir = RunDir(run_id, data_dir)
        rundir.create()
        rundir.task_md.write_text(text, encoding="utf-8")
    return task_id


def event(ledger: Ledger, run_id: str, ts: float, type_: str, payload: dict[str, Any]) -> None:
    """``append_event`` stamps the current time; the minutes need chosen ones."""
    with ledger._write() as conn:
        conn.execute(
            "INSERT INTO run_events (run_id, ts, source, stage, type, payload) "
            "VALUES (?, ?, 'helmraiser', NULL, ?, ?)",
            (run_id, ts, type_, json.dumps(payload)),
        )


def show(*args: str, data_dir: Path):
    return runner.invoke(app, ["show", *args, "--data-dir", str(data_dir)])


def test_a_fresh_failed_run_reports_its_state_and_limits(
    ledger: Ledger, data_dir: Path, tmp_path: Path
):
    project = tmp_path / "project"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    task_id = seed(
        ledger,
        data_dir,
        project,
        stage="verify",
        failure_reason="verify_failed: marker",
        verdict="pass",
        delivery="pr",
        base_branch="main",
        base_commit="a" * 40,
        head_commit="b" * 40,
        branch_name="helmraiser/fix-widget",
        pr_url="https://example.test/pr/1",
        worktree_path=str(worktree),
        cost_usd=1.25,
    )
    for n in (1, 2, 3):
        ledger.start_iteration(RUN_ID, n, f"s{n}", "prov/impl")
    event(ledger, RUN_ID, NOW - 7200, "run_start", {})
    event(ledger, RUN_ID, NOW - 3600, "run_end", {"status": "failed", "active_minutes": 12.5})

    result = show(RUN_ID, "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == SHOW_KEYS
    assert payload == {
        "run_id": RUN_ID,
        "task_id": task_id,
        "project": str(project),
        "status": "failed",
        "stage": "verify",
        "failure_reason": "verify_failed: marker",
        "verdict": "pass",
        "models": {
            "plan": None,
            "implement": "prov/impl",
            "fallback": None,
            "review": "prov/rev",
        },
        "arm": None,
        "policy": "review",
        "delivery": "pr",
        "base_branch": "main",
        "base_commit": "a" * 40,
        "head_commit": "b" * 40,
        "branch_name": "helmraiser/fix-widget",
        "pr_url": "https://example.test/pr/1",
        "worktree_path": str(worktree),
        "worktree_exists": True,
        "started_at": "2027-01-15T06:00:00+00:00",
        "ended_at": "2027-01-15T07:00:00+00:00",
        "pending_approval": None,
        "rework_rounds": 0,
        "landed_outside": None,
        "warnings": [],
        "limits": {
            "iterations": {"used": 3, "limit": 4, "limit_source": "max_iterations"},
            "cost_usd": {"used": 1.25, "limit": 5.0},
            "minutes": {"used": 12.5, "limit": 90.0},
        },
    }


def test_text_output_is_short_labelled_lines(ledger: Ledger, data_dir: Path, tmp_path: Path):
    seed(
        ledger,
        data_dir,
        tmp_path / "project",
        failure_reason="budget:iterations",
        branch_name="helmraiser/fix-widget",
        worktree_path=str(tmp_path / "gone"),
        cost_usd=1.5,
    )
    for n in (1, 2, 3, 4):
        ledger.start_iteration(RUN_ID, n, f"s{n}", "prov/impl")
    event(ledger, RUN_ID, NOW - 7200, "run_start", {})
    event(ledger, RUN_ID, NOW - 3600, "run_end", {"status": "failed", "active_minutes": 12.0})

    result = show(RUN_ID[:8], data_dir=data_dir)

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert f"run_id: {RUN_ID}" in lines
    assert "status: failed" in lines
    assert "failure_reason: budget:iterations" in lines
    assert f"worktree_path: {tmp_path / 'gone'} (missing)" in lines
    assert "iterations: 4 of 4 (max_iterations)" in lines
    assert "cost_usd: 1.5000 of 5.0000" in lines
    assert "minutes: 12.0 of 90.0" in lines
    assert not any(
        line.startswith(("pr:", "pr_url", "stage", "pending_approval")) for line in lines
    )
    assert all(len(line) < 200 for line in lines)


def test_pr_link_is_first_in_human_output(ledger: Ledger, data_dir: Path, tmp_path: Path):
    seed(ledger, data_dir, tmp_path / "project", pr_url="https://example.test/pr/1")

    result = show(RUN_ID, data_dir=data_dir)

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "pr: https://example.test/pr/1"
    assert lines.count("pr: https://example.test/pr/1") == 1
    assert not any(line.startswith("pr_url:") for line in lines)
    assert "status: failed" in lines
    assert "iterations: 0 of 4 (max_iterations)" in lines
    assert "cost_usd: 0.0000 of 5.0000" in lines
    assert "minutes: 60.0 of 90.0" in lines


def test_a_unique_prefix_finds_the_run_and_an_ambiguous_one_is_refused(
    ledger: Ledger, data_dir: Path, tmp_path: Path
):
    seed(ledger, data_dir, tmp_path / "project", run_id="abc111", status="landed")
    other = tmp_path / "other"
    seed(ledger, data_dir, other, run_id="abc222", status="landed")

    found = show("abc1", "--json", data_dir=data_dir)
    ambiguous = show("abc", "--json", data_dir=data_dir)

    assert found.exit_code == 0 and json.loads(found.output)["run_id"] == "abc111"
    assert ambiguous.exit_code == 1
    error = json.loads(ambiguous.output)["error"]
    assert "ambiguous run prefix: abc; matches: abc111, abc222" in error
    assert (
        "expected run id, task id (short id or <project-key>:<id>), or unique run prefix" in error
    )


@pytest.mark.parametrize("as_json", [False, True])
def test_an_unknown_reference_is_refused(ledger: Ledger, data_dir: Path, as_json: bool):
    flags = ["--json"] if as_json else []

    shown = show("nope", *flags, data_dir=data_dir)

    assert shown.exit_code == 1
    error = json.loads(shown.output)["error"] if as_json else " ".join(shown.output.split())
    assert "unknown ref: nope" in error
    assert (
        "expected run id, task id (short id or <project-key>:<id>), or unique run prefix" in error
    )


@pytest.mark.parametrize("full_key", [False, True])
@pytest.mark.parametrize("queued", [False, True])
def test_task_reference_selects_latest_run(
    ledger: Ledger, data_dir: Path, tmp_path: Path, full_key: bool, queued: bool
):
    project = tmp_path / "project"
    task_id = seed(ledger, data_dir, project, run_id="newest", started_at=NOW - 100)
    seed(ledger, data_dir, project, run_id="older", started_at=NOW - 200)
    ledger.set_task_status(task_id, "queued" if queued else "failed")

    result = show(task_id if full_key else "fix-widget", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run_id"] == "newest"
    assert payload["task_id"] == task_id


def test_task_reference_breaks_timestamp_ties_by_run_id(
    ledger: Ledger, data_dir: Path, tmp_path: Path
):
    project = tmp_path / "project"
    task_id = seed(ledger, data_dir, project, run_id="zzz", started_at=NOW - 100)
    seed(ledger, data_dir, project, run_id="aaa", started_at=NOW - 100)

    result = show("fix-widget", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run_id"] == "zzz"
    assert payload["task_id"] == task_id


def test_ambiguous_short_task_id_lists_full_keys(ledger: Ledger, data_dir: Path, tmp_path: Path):
    first = seed(ledger, data_dir, tmp_path / "first", run_id="first-run")
    second = seed(ledger, data_dir, tmp_path / "second", run_id="second-run")

    result = show("fix-widget", "--json", data_dir=data_dir)

    assert result.exit_code == 1
    error = json.loads(result.output)["error"]
    assert "ambiguous task id: fix-widget" in error
    assert first in error and second in error
    assert (
        "expected run id, task id (short id or <project-key>:<id>), or unique run prefix" in error
    )


def test_task_without_runs_is_refused(ledger: Ledger, data_dir: Path, tmp_path: Path):
    project = tmp_path / "project"
    text = TASK.format(id="fix-widget", project=project, cost=5, minutes=90, iterations=4)
    spec = parse_task(text, project / "fix-widget.md", copy.deepcopy(DEFAULT_CONFIG))
    ledger.upsert_task(spec, text)

    result = show("fix-widget", "--json", data_dir=data_dir)

    assert result.exit_code == 1
    assert json.loads(result.output)["error"] == "task fix-widget has no runs"


def test_show_help_names_accepted_references(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COLUMNS", "120")
    result = runner.invoke(app, ["show", "--help"])

    assert result.exit_code == 0
    text = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", result.output).split())
    assert "Run id, task id (short id or <project-key>:<id>), or unique run prefix" in text
    assert "most recent run" in text


def test_show_writes_nothing(tmp_path: Path):
    missing = tmp_path / "never-created"

    result = show("anything", "--json", data_dir=missing)

    assert result.exit_code == 1
    assert "cannot read ledger" in json.loads(result.output)["error"]
    assert not missing.exists()


def test_the_limits_come_from_the_run_snapshot_not_the_task_file(
    ledger: Ledger, data_dir: Path, tmp_path: Path
):
    project = tmp_path / "project"
    seed(ledger, data_dir, project, iterations=4, cost=5.0)
    source = project / "topics" / "fix-widget.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        TASK.format(id="fix-widget", project=project, cost=50.0, minutes=900, iterations=40)
    )

    limits = json.loads(show(RUN_ID, "--json", data_dir=data_dir).output)["limits"]

    assert limits["iterations"]["limit"] == 4
    assert limits["cost_usd"]["limit"] == 5.0


def test_without_a_run_directory_the_ledger_snapshot_is_used(
    ledger: Ledger, data_dir: Path, tmp_path: Path
):
    seed(ledger, data_dir, tmp_path / "project", iterations=7, snapshot_file=False)

    payload = json.loads(show(RUN_ID, "--json", data_dir=data_dir).output)

    assert payload["limits"]["iterations"]["limit"] == 7
    assert payload["warnings"] == []


def test_an_unreadable_snapshot_leaves_the_limits_unknown_and_says_why(
    ledger: Ledger, data_dir: Path, tmp_path: Path
):
    seed(ledger, data_dir, tmp_path / "project")
    RunDir(RUN_ID, data_dir).task_md.write_text("no front matter here")
    ledger.start_iteration(RUN_ID, 1, "s1", "prov/impl")

    result = show(RUN_ID, "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["limits"] == {
        "iterations": {"used": 1, "limit": None, "limit_source": None},
        "cost_usd": {"used": 0.0, "limit": None},
        "minutes": {"used": 60.0, "limit": None},  # no run events: from started_at to ended_at
    }
    assert len(payload["warnings"]) == 1 and "no frontmatter" in payload["warnings"][0]
    text = show(RUN_ID, data_dir=data_dir).output
    assert "iterations: 1 of unknown" in text and "warning: " in text


def test_a_rework_adds_its_extras_to_the_cost_and_minutes_limits(
    ledger: Ledger, data_dir: Path, tmp_path: Path
):
    seed(ledger, data_dir, tmp_path / "project", status="awaiting_approval", cost=4.0, minutes=40)
    ledger.record_approval(RUN_ID, "merge", "pending")
    for n in range(1, 5):
        ledger.start_iteration(RUN_ID, n, f"s{n}", "prov/impl")
    ledger.hand_back(
        RUN_ID,
        "implement",
        rework={
            "approval_id": ledger.pending_approval(RUN_ID)["id"],
            "note": "again",
            "findings": [],
            "iteration_allowance": 2,
            "extra_cost_usd": 2.0,
            "extra_minutes": 20.0,
        },
    )

    payload = json.loads(show(RUN_ID, "--json", data_dir=data_dir).output)

    assert payload["rework_rounds"] == 1 and payload["status"] == "resuming"
    assert payload["limits"] == {
        "iterations": {"used": 4, "limit": 6, "limit_source": "rework_allowance"},
        "cost_usd": {"used": 0.0, "limit": 6.0},
        "minutes": {"used": 0.0, "limit": 60.0},
    }


def test_a_running_run_counts_its_current_segment_of_minutes(
    monkeypatch: pytest.MonkeyPatch, ledger: Ledger, data_dir: Path, tmp_path: Path
):
    monkeypatch.setattr(monitor.time, "time", lambda: NOW)
    seed(ledger, data_dir, tmp_path / "project", status="running", stage="implement")
    event(ledger, RUN_ID, NOW - 3000, "run_start", {})
    event(
        ledger, RUN_ID, NOW - 2400, "run_end", {"status": "awaiting_approval", "active_minutes": 10}
    )
    event(ledger, RUN_ID, NOW - 1200, "run_resume", {"from": "plan"})

    minutes = json.loads(show(RUN_ID, "--json", data_dir=data_dir).output)["limits"]["minutes"]

    assert minutes == {"used": 30.0, "limit": 90.0}  # 10 completed + 20 since the resume


def test_a_parked_or_ended_run_does_not_count_time_since_its_last_event(
    monkeypatch: pytest.MonkeyPatch, ledger: Ledger, data_dir: Path, tmp_path: Path
):
    monkeypatch.setattr(monitor.time, "time", lambda: NOW)
    seed(ledger, data_dir, tmp_path / "project", status="awaiting_approval")
    event(ledger, RUN_ID, NOW - 3000, "run_start", {})
    event(
        ledger, RUN_ID, NOW - 2400, "run_end", {"status": "awaiting_approval", "active_minutes": 10}
    )

    minutes = json.loads(show(RUN_ID, "--json", data_dir=data_dir).output)["limits"]["minutes"]

    assert minutes["used"] == 10.0


def test_pending_approval_and_landed_outside_are_reported(
    ledger: Ledger, data_dir: Path, tmp_path: Path
):
    seed(ledger, data_dir, tmp_path / "project", status="awaiting_approval")
    ledger.record_approval(RUN_ID, "plan", "pending")
    sha = "c" * 40
    ledger.append_event(RUN_ID, "helmraiser", "landed_outside", {"commit": sha, "note": "by hand"})

    payload = json.loads(show(RUN_ID, "--json", data_dir=data_dir).output)

    assert payload["pending_approval"] == "plan"
    assert payload["landed_outside"] == {"commit": sha, "note": "by hand"}
    text = show(RUN_ID, data_dir=data_dir).output.splitlines()
    assert "pending_approval: plan" in text
    assert f"landed_outside: {sha} by hand" in text


def test_a_run_without_a_pending_approval_reports_none_not_merge(
    ledger: Ledger, data_dir: Path, tmp_path: Path
):
    seed(ledger, data_dir, tmp_path / "project", status="landed")

    assert json.loads(show(RUN_ID, "--json", data_dir=data_dir).output)["pending_approval"] is None
