"""``helmraiser record-landed``: a run whose branch reached its base outside Helmraiser is
recorded as landed, so the tasks that depend on it are released."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, commands
from helmraiser.ledger.db import Ledger
from helmraiser.loop.cleanup import clean_worktrees
from helmraiser.storage.state import StateManager
from helmraiser.task.loader import render_task
from helmraiser.task.project import worktree_manager
from tests.conftest import FakePi, configured_test_config
from tests.ledger.test_db import make_spec, run_row

runner = CliRunner()

TASK = """---
id: fix-widget
title: Fix the widget
project: {project}
status: draft
policy: review
isolation: none
budget:
  max_cost_usd: 2.0
  max_minutes: 30
  max_iterations: 3
verify:
  - name: marker
    run: test -f wrong.txt
---
## Goal

Make the widget work.
"""

NOTE = "agent finished the branch outside Helmraiser; fast-forwarded by hand"


@pytest.fixture(autouse=True)
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = configured_test_config()
    monkeypatch.setattr(commands, "load_config", lambda: config)
    monkeypatch.setattr(commands, "get_intake_adapter", lambda config: None)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def invoke(*args: str, data_dir: Path):
    return runner.invoke(app, [*args, "--data-dir", str(data_dir)])


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def failed_run(project: Path, data_dir: Path, fake_pi: FakePi) -> dict:
    """A run of ``fix-widget`` that ended ``failed``: blocked, with a commit on its branch."""
    path = project.parent / "tasks" / "fix-widget.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(TASK.format(project=project), encoding="utf-8")
    assert invoke("intake", str(path), "--yes", "--json", data_dir=data_dir).exit_code == 0
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": [
                    {"text": "Done.", "cost": 0.05, "files": [["done.txt", "ok\n"]]},
                    {"text": "BLOCKED: the verify step checks wrong.txt", "cost": 0.01},
                ],
            }
        }
    )
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 1, result.output
    outcome = json.loads(result.output.splitlines()[0])
    assert outcome["status"] == "failed", outcome
    assert _git(project, "rev-parse", outcome["branch_name"]) != outcome["base_commit"]
    return outcome


def merge_by_hand(project: Path, outcome: dict) -> str:
    """Fast-forward the primary checkout over the run's branch; the commit now in the base."""
    _git(project, "merge", "--ff-only", outcome["branch_name"])
    return _git(project, "rev-parse", "HEAD")


def queue_dependent(ledger: Ledger, project: Path):
    spec = make_spec(project, "dependent", depends_on=("fix-widget",))
    ledger.upsert_task(spec, render_task(spec))
    return spec


def test_record_landed_releases_a_dependent_held_by_the_failed_run(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    dependent = queue_dependent(ledger, tmp_project)
    unmet = ledger.held_tasks()[dependent.task_id]["unmet"]
    assert [item["reason"] for item in unmet] == ["failed"]
    held = json.loads(invoke("status", "--json", data_dir=data_dir).output)["held"]
    assert [(h["id"], h["unmet"][0]["id"]) for h in held] == [("dependent", "fix-widget")]
    commit = merge_by_hand(tmp_project, outcome)

    result = invoke(
        "record-landed", outcome["run_id"], "--commit", commit, "--note", NOTE, data_dir=data_dir
    )

    assert result.exit_code == 0, result.output
    assert dependent.task_id not in ledger.held_tasks()
    assert ledger.next_queued()["task_id"] == dependent.task_id
    after = json.loads(invoke("status", "--json", data_dir=data_dir).output)
    assert after["held"] == []
    assert after["failed"] == []  # no longer a failure waiting for the owner


def ledger_state(ledger: Ledger, outcome: dict) -> tuple:
    """Everything a refused record-landed must leave exactly as it was."""
    run_id = outcome["run_id"]
    return (
        ledger.get_run(run_id),
        ledger.get_task(outcome["task_id"]),
        ledger.events(run_id),
        ledger.approvals(run_id),
        (Path(outcome["worktree_path"]).is_dir()),
    )


def test_a_commit_outside_the_base_is_refused_and_nothing_is_written(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    dependent = queue_dependent(ledger, tmp_project)
    branch_tip = _git(tmp_project, "rev-parse", outcome["branch_name"])
    base = _git(tmp_project, "branch", "--show-current")
    before = ledger_state(ledger, outcome)

    result = invoke(
        "record-landed",
        outcome["run_id"],
        "--commit",
        branch_tip,
        "--note",
        NOTE,
        "--json",
        data_dir=data_dir,
    )

    assert result.exit_code == 2, result.output
    error = json.loads(result.output)["error"]
    assert branch_tip in error and f"base branch {base}" in error
    assert ledger_state(ledger, outcome) == before
    assert dependent.task_id in ledger.held_tasks()


@pytest.mark.parametrize(
    "commit, note, fragment",
    [
        ("0123456789abcdef0123456789abcdef01234567", NOTE, "not a commit in"),
        ("no-such-rev", NOTE, "not a commit in"),
        ("--abbrev-ref", NOTE, "not a commit in"),
        ("HEAD", "", "--note"),
        ("HEAD", "  \n\t ", "--note"),
        ("HEAD", None, "--note"),
        (None, NOTE, "--commit"),
        ("", NOTE, "--commit"),
    ],
)
def test_an_unusable_commit_or_note_is_refused_and_nothing_is_written(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, commit, note, fragment
):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    before = ledger_state(ledger, outcome)
    args = ["record-landed", outcome["run_id"], "--json"]
    if commit is not None:
        args += [f"--commit={commit}"]
    if note is not None:
        args += ["--note", note]

    result = invoke(*args, data_dir=data_dir)

    assert result.exit_code == 2, result.output
    assert fragment in json.loads(result.output)["error"]
    assert ledger_state(ledger, outcome) == before


@pytest.mark.parametrize("status", ["running", "awaiting_approval", "resuming", "landed"])
def test_a_run_that_has_not_ended_without_landing_is_refused(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, status: str
):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    commit = merge_by_hand(tmp_project, outcome)
    ledger.update_run(outcome["run_id"], status=status)
    before = ledger_state(ledger, outcome)

    result = invoke(
        "record-landed",
        outcome["run_id"],
        "--commit",
        commit,
        "--note",
        NOTE,
        "--json",
        data_dir=data_dir,
    )

    assert result.exit_code == 2, result.output
    error = json.loads(result.output)["error"]
    assert f"is {status}, not failed or aborted" in error
    assert ledger_state(ledger, outcome) == before


@pytest.mark.parametrize("move_on", ["a later run", "queued again"])
def test_a_task_that_has_moved_on_is_refused(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, move_on: str
):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    commit = merge_by_hand(tmp_project, outcome)
    if move_on == "a later run":
        later = {**run_row("later-run", outcome["task_id"], str(tmp_project), "failed")}
        ledger.create_run(
            {**later, "started_at": float(ledger.get_run(outcome["run_id"])["started_at"]) + 60}
        )
    else:
        ledger.set_task_status(outcome["task_id"], "queued")
    before = ledger_state(ledger, outcome)

    result = invoke(
        "record-landed",
        outcome["run_id"],
        "--commit",
        commit,
        "--note",
        NOTE,
        "--json",
        data_dir=data_dir,
    )

    assert result.exit_code == 2, result.output
    error = json.loads(result.output)["error"]
    assert f"task {outcome['task_id']} has moved on since run {outcome['run_id']}" in error
    assert ledger_state(ledger, outcome) == before


def test_an_aborted_run_is_accepted(tmp_project: Path, data_dir: Path, fake_pi: FakePi):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    dependent = queue_dependent(ledger, tmp_project)
    ledger.update_run(outcome["run_id"], status="aborted", failure_reason=None)
    ledger.set_task_status(outcome["task_id"], "aborted")
    assert ledger.held_tasks()[dependent.task_id]["unmet"][0]["reason"] == "aborted"
    commit = merge_by_hand(tmp_project, outcome)

    result = invoke(
        "record-landed", outcome["run_id"], "--commit", commit, "--note", NOTE, data_dir=data_dir
    )

    assert result.exit_code == 0, result.output
    assert ledger.get_run(outcome["run_id"])["status"] == "landed"
    assert dependent.task_id not in ledger.held_tasks()


def test_the_run_and_task_become_landed_at_the_given_commit(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    commit = merge_by_hand(tmp_project, outcome)
    run_id = outcome["run_id"]
    before = ledger.get_run(run_id)
    assert before["failure_reason"] == "blocked: the verify step checks wrong.txt"
    assert before["head_commit"] != commit

    result = invoke(
        "record-landed", run_id, "--commit", commit[:10], "--note", NOTE, data_dir=data_dir
    )

    assert result.exit_code == 0, result.output
    after = ledger.get_run(run_id)
    assert after["status"] == "landed"
    assert after["head_commit"] == commit  # the full SHA, from the abbreviation given
    assert after["failure_reason"] is None  # as for every landed run; the event keeps it
    assert after["ended_at"] == before["ended_at"]  # when the work stopped, not when recorded
    assert {key: after[key] for key in ("started_at", "cost_usd", "branch_name")} == {
        key: before[key] for key in ("started_at", "cost_usd", "branch_name")
    }
    assert ledger.get_task(outcome["task_id"])["status"] == "landed"
    with StateManager(tmp_project / ".helmraiser" / "state.db") as state:
        assert state.get_run(run_id)["status"] == "succeeded"
    summary = (data_dir / "runs" / run_id / "summary.md").read_text().splitlines()[0]
    assert summary == "## Fix the widget — landed"


def test_the_landed_outside_event_keeps_what_happened(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    commit = merge_by_hand(tmp_project, outcome)
    run_id = outcome["run_id"]
    events_before = ledger.events(run_id)
    head_before = ledger.get_run(run_id)["head_commit"]

    result = invoke("record-landed", run_id, "--commit", commit, "--note", NOTE, data_dir=data_dir)

    assert result.exit_code == 0, result.output
    events = ledger.events(run_id)
    assert events[: len(events_before)] == events_before  # the failure's history is untouched
    recorded, cleaned = events[len(events_before) :]
    assert (recorded["source"], recorded["type"]) == ("helmraiser", "landed_outside")
    assert cleaned["type"] == "worktree_cleaned"  # the clean worktree went after the landing
    assert recorded["payload"] == {
        "commit": commit,
        "note": NOTE,
        "previous_status": "failed",
        "failure_reason": "blocked: the verify step checks wrong.txt",
        "branch": outcome["branch_name"],
        "previous_head_commit": head_before,
    }


def test_log_shows_the_landed_outside_event_readably(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    commit = merge_by_hand(tmp_project, outcome)
    run_id = outcome["run_id"]
    recorded = invoke(
        "record-landed", run_id, "--commit", commit, "--note", "hand-merged", data_dir=data_dir
    )
    assert recorded.exit_code == 0, recorded.output

    result = invoke("log", run_id, data_dir=data_dir)

    assert result.exit_code == 0, result.output
    line = next(line for line in result.output.splitlines() if "landed_outside" in line)
    assert line.endswith(
        f"{commit[:12]} was failed (blocked: the verify step checks wrong.txt): hand-merged"
    )


def test_the_summary_says_helmraiser_did_not_verify_the_landing(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    commit = merge_by_hand(tmp_project, outcome)
    run_id = outcome["run_id"]

    result = invoke(
        "record-landed",
        run_id,
        "--commit",
        commit,
        "--note",
        "agent finished\nthe branch by hand",
        data_dir=data_dir,
    )

    assert result.exit_code == 0, result.output
    lines = (data_dir / "runs" / run_id / "summary.md").read_text().splitlines()
    assert lines[0] == "## Fix the widget — landed"
    assert lines[2] == (
        f"- landed outside Helmraiser at `{commit[:12]}`: not verified or reviewed by Helmraiser"
        " — agent finished the branch by hand"
    )
    # What the run itself did, and how it ended, stays in the summary.
    assert "- failure: blocked: the verify step checks wrong.txt" in lines


def test_the_report_marks_the_landing_as_recorded_not_verified(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    commit = merge_by_hand(tmp_project, outcome)
    recorded = invoke(
        "record-landed", outcome["run_id"], "--commit", commit, "--note", NOTE, data_dir=data_dir
    )
    assert recorded.exit_code == 0, recorded.output

    as_json = json.loads(invoke("report", "--json", data_dir=data_dir).output)
    text = invoke("report", data_dir=data_dir).output

    (entry,) = as_json["landed"]
    assert entry["run_id"] == outcome["run_id"]
    assert entry["verdict"] is None
    assert entry["landed_outside"] == {"commit": commit, "note": NOTE}
    assert as_json["failed"] == []
    assert f"- landed outside Helmraiser: `{commit[:12]}` — {NOTE}" in text


def record(outcome: dict, commit: str, data_dir: Path):
    return invoke(
        "record-landed",
        outcome["run_id"],
        "--commit",
        commit,
        "--note",
        NOTE,
        "--json",
        data_dir=data_dir,
    )


def test_a_clean_worktree_is_removed_and_the_branch_kept(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    worktree = Path(outcome["worktree_path"])
    assert worktree.is_dir() and _git(worktree, "status", "--porcelain") == ""
    commit = merge_by_hand(tmp_project, outcome)

    result = record(outcome, commit, data_dir)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["worktree"] == {
        "action": "removed",
        "path": str(worktree),
        "detail": None,
    }
    assert not worktree.exists()
    assert _git(tmp_project, "rev-parse", f"refs/heads/{outcome['branch_name']}") == commit
    assert str(worktree) not in _git(tmp_project, "worktree", "list", "--porcelain")
    assert not (worktree.parent / f"{outcome['run_id']}.json").exists()
    cleaned = ledger.events(outcome["run_id"], types=("worktree_cleaned",))
    assert [event["payload"] for event in cleaned] == [
        {
            "reason": "landed_outside",
            "worktree_path": str(worktree),
            "branch_name": outcome["branch_name"],
        }
    ]


def test_a_dirty_worktree_is_kept_and_reported(tmp_project: Path, data_dir: Path, fake_pi: FakePi):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    worktree = Path(outcome["worktree_path"])
    (worktree / "unfinished.txt").write_text("not committed\n")
    commit = merge_by_hand(tmp_project, outcome)

    result = record(outcome, commit, data_dir)

    assert result.exit_code == 0, result.output  # the landing is recorded either way
    assert ledger.get_run(outcome["run_id"])["status"] == "landed"
    assert json.loads(result.output)["worktree"] == {
        "action": "kept",
        "path": str(worktree),
        "detail": "dirty: 1 paths",
    }
    assert (worktree / "unfinished.txt").read_text() == "not committed\n"
    assert ledger.events(outcome["run_id"], types=("worktree_cleaned",)) == []


def test_a_worktree_that_is_already_gone_is_reported_as_absent(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    worktree_manager(tmp_project).discard("fix-widget", outcome["run_id"])
    commit = merge_by_hand(tmp_project, outcome)

    result = record(outcome, commit, data_dir)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["worktree"]["action"] == "absent"


def test_the_sweep_clears_earlier_runs_of_the_task_once_one_is_recorded_as_landed(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    manager = worktree_manager(tmp_project)
    earlier = "00000000-0000-4000-8000-000000000001"
    path = manager.create("fix-widget", earlier)
    started = float(ledger.get_run(outcome["run_id"])["started_at"]) - 3600
    ledger.create_run(
        {
            **run_row(earlier, outcome["task_id"], str(tmp_project), "failed"),
            "started_at": started,
            "ended_at": started + 60,
            "worktree_path": str(path),
            "branch_name": manager.metadata(earlier)["branch_name"],
        }
    )
    commit = merge_by_hand(tmp_project, outcome)
    assert clean_worktrees(ledger, config={}, now=time.time()) == []  # nothing landed yet

    assert record(outcome, commit, data_dir).exit_code == 0

    swept = clean_worktrees(ledger, config={}, now=time.time())
    assert [(r["run_id"], r["reason"], r["action"]) for r in swept] == [
        (earlier, "superseded", "removed")
    ]


@pytest.mark.parametrize(
    "damage, fragment",
    [
        ({"base_branch": None}, "has no base branch"),
        ({"base_branch": ""}, "has no base branch"),
        ({"base_branch": "no-such-branch"}, "base branch no-such-branch not found"),
        ({"project": "__missing__"}, "git rev-parse"),
    ],
)
def test_an_unverifiable_base_is_a_clean_refusal_and_nothing_is_written(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi, tmp_path: Path, damage, fragment
):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    commit = merge_by_hand(tmp_project, outcome)
    ledger.update_run(
        outcome["run_id"],
        **{k: str(tmp_path / "gone") if v == "__missing__" else v for k, v in damage.items()},
    )
    before = ledger_state(ledger, outcome)

    result = record(outcome, commit, data_dir)

    assert result.exit_code == 1, result.output
    assert fragment in json.loads(result.output)["error"]
    assert ledger_state(ledger, outcome) == before


def test_a_task_snapshot_that_cannot_be_read_is_a_clean_refusal(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    commit = merge_by_hand(tmp_project, outcome)
    assert ledger.set_task_snapshot(outcome["task_id"], "---\npaths: [\n---\n")
    before = ledger_state(ledger, outcome)

    result = record(outcome, commit, data_dir)

    assert result.exit_code == 1, result.output
    assert "cannot read the task snapshot" in json.loads(result.output)["error"]
    assert ledger_state(ledger, outcome) == before


def test_json_names_what_was_recorded_and_what_is_now_eligible(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    dependent = queue_dependent(ledger, tmp_project)
    commit = merge_by_hand(tmp_project, outcome)
    base = _git(tmp_project, "branch", "--show-current")
    (tmp_project / "later.txt").write_text("the base moved on after the hand merge\n")
    _git(tmp_project, "add", "later.txt")
    _git(tmp_project, "commit", "-m", "later work")

    result = invoke(
        "record-landed",
        outcome["run_id"][:8],
        "--commit",
        commit,
        "--note",
        NOTE,
        "--json",
        data_dir=data_dir,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    eligible = payload.pop("eligible")
    assert payload.pop("worktree")["action"] == "removed"
    assert payload == {
        "recorded": "landed_outside",
        "run_id": outcome["run_id"],
        "task_id": outcome["task_id"],
        "status": "landed",
        "previous_status": "failed",
        "failure_reason": "blocked: the verify step checks wrong.txt",
        "commit": commit,
        "base_branch": base,
        "branch_name": outcome["branch_name"],
        "note": NOTE,
    }
    assert eligible["task_ids"] == [dependent.task_id]
    assert eligible["project"] == str(tmp_project)
    assert ledger.get_run(outcome["run_id"])["head_commit"] == commit  # not the later tip


def test_text_output_says_what_happened_and_what_to_do_next(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    dependent = queue_dependent(ledger, tmp_project)
    worktree = Path(outcome["worktree_path"])
    (worktree / "unfinished.txt").write_text("not committed\n")
    commit = merge_by_hand(tmp_project, outcome)

    result = invoke(
        "record-landed", outcome["run_id"], "--commit", commit, "--note", NOTE, data_dir=data_dir
    )

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        f"recorded {outcome['task_id']} run {outcome['run_id']} as landed at {commit[:12]}"
        " (was failed: blocked: the verify step checks wrong.txt)",
        "  main contains the commit; Helmraiser did not verify or review it",
        f"  note: {NOTE}",
        f"  branch {outcome['branch_name']} kept",
        f"  worktree kept (dirty: 1 paths): {worktree}",
        f"  now eligible: {dependent.task_id}",
        "  no worker is running: helmraiser worker start",
    ]


def test_a_run_recorded_as_landed_can_no_longer_be_retried(
    tmp_project: Path, data_dir: Path, fake_pi: FakePi
):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    commit = merge_by_hand(tmp_project, outcome)
    assert record(outcome, commit, data_dir).exit_code == 0

    result = invoke("retry", outcome["run_id"], "--json", data_dir=data_dir)

    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["error"] == f"run {outcome['run_id']} is landed, not failed"


def test_an_unknown_run_is_refused_in_json(data_dir: Path):
    result = invoke(
        "record-landed",
        "no-such-run",
        "--commit",
        "HEAD",
        "--note",
        NOTE,
        "--json",
        data_dir=data_dir,
    )

    assert result.exit_code == 1, result.output
    assert json.loads(result.output) == {"error": "unknown run: no-such-run"}
