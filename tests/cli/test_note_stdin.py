"""Owner notes read from stdin preserve text and validate before side effects."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, commands
from helmraiser.ledger.db import Ledger
from helmraiser.task.project import worktree_manager
from tests.cli.test_approve_reject import (
    cli_env,  # noqa: F401 -- shared isolated configuration fixture
    queue_and_run,
)
from tests.cli.test_approve_reject import (
    data_dir as data_dir,  # shared isolated data directory fixture
)
from tests.cli.test_record_landed import failed_run, merge_by_hand
from tests.cli.test_scope_request import decision_state, park_on_scope

runner = CliRunner()


def invoke(command, run_id, data_dir, *, note="-", stdin="", extra=()):
    return runner.invoke(
        app,
        [command, run_id, *extra, "--note", note, "--json", "--data-dir", str(data_dir)],
        input=stdin,
    )


@pytest.mark.parametrize(
    "command,policy,kind,decision,status",
    [
        ("approve", "review", "merge", "approved", "landed"),
        ("approve", "approve-plan-first", "plan", "approved", "resuming"),
        ("reject", "review", "merge", "rejected", "failed"),
    ],
)
@pytest.mark.parametrize(
    "stdin,expected",
    [
        ("  Owner café\nKeep - as text  \n\n", "  Owner café\nKeep - as text  \n"),
        ("no terminal newline", "no terminal newline"),
        ("-\n", "-"),
    ],
)
def test_approval_and_rejection_store_resolved_note(
    tmp_project, data_dir, fake_pi, command, policy, kind, decision, status, stdin, expected
):
    outcome = queue_and_run(tmp_project, data_dir, fake_pi, policy=policy)
    run_id = outcome["run_id"]
    result = invoke(command, run_id, data_dir, stdin=stdin)
    assert result.exit_code == 0, result.output
    with Ledger(data_dir / "helmraiser.db") as ledger:
        (approval,) = ledger.approvals(run_id)
        assert (approval["kind"], approval["decision"], approval["note"]) == (
            kind,
            decision,
            expected,
        )
        assert ledger.get_run(run_id)["status"] == status


def test_rework_stores_stdin_note(tmp_project, data_dir, fake_pi):
    outcome = queue_and_run(tmp_project, data_dir, fake_pi)
    run_id = outcome["run_id"]
    note = "  Owner café\nRework - this contract  \n"
    result = invoke("rework", run_id, data_dir, stdin=note + "\n")
    assert result.exit_code == 0, result.output
    assert (json.loads(result.output)["status"], json.loads(result.output)["stage"]) == (
        "resuming",
        "implement",
    )
    with Ledger(data_dir / "helmraiser.db") as ledger:
        assert ledger.approvals(run_id)[0]["note"] == note
        assert ledger.reworks(run_id)[0]["note"] == note
        row = ledger.get_run(run_id)
        assert (row["status"], row["stage"]) == ("resuming", "implement")


def test_record_landed_stores_stdin_note(tmp_project, data_dir, fake_pi):
    outcome = failed_run(tmp_project, data_dir, fake_pi)
    run_id = outcome["run_id"]
    commit = merge_by_hand(tmp_project, outcome)
    note = "  Owner café\nLanded - by hand  \n"
    result = invoke(
        "record-landed", run_id, data_dir, stdin=note + "\n", extra=("--commit", commit)
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "landed"
    assert payload["note"] == note
    with Ledger(data_dir / "helmraiser.db") as ledger:
        assert ledger.get_run(run_id)["status"] == "landed"
        event = ledger.events(run_id, types=("landed_outside",))[0]
        assert event["payload"]["note"] == note


def test_stdin_plan_note_reaches_implementer(tmp_project, data_dir, fake_pi):
    outcome = queue_and_run(tmp_project, data_dir, fake_pi, policy="approve-plan-first")
    run_id = outcome["run_id"]
    plan_path = data_dir / "runs" / run_id / "plan.md"
    original_plan = plan_path.read_text()
    note = "  Owner decision: café\n" + "Revised contract.\n" * 600 + "UNIQUE STDIN NOTE END  \n"
    calls_before = fake_pi.calls()
    result = invoke("approve", run_id, data_dir, stdin=note + "\n")
    assert result.exit_code == 0, result.output
    assert fake_pi.calls() == calls_before
    expected_plan = original_plan + f"\n## Owner's note on approval\n\n{note}\n"
    assert plan_path.read_text() == expected_plan
    with Ledger(data_dir / "helmraiser.db") as ledger:
        assert ledger.approvals(run_id)[0]["note"] == note

    result = runner.invoke(app, ["work", "--once", "--json", "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["run_id"] == run_id
    assert (resumed["status"], resumed["pending_approval"]) == ("awaiting_approval", "merge")
    implement = next(c for c in fake_pi.calls() if c["session_id"].endswith("-implement"))
    prompt = Path(implement["argv"][-1][1:]).read_text()
    assert "## Approved plan" in prompt
    assert expected_plan.rstrip() in prompt
    assert note.strip() in prompt
    assert "UNIQUE STDIN NOTE END" in prompt


@pytest.mark.parametrize("decline", [False, True])
def test_scope_decision_reads_stdin(tmp_project, data_dir, fake_pi, decline):
    outcome, source = park_on_scope(tmp_project, data_dir, fake_pi)
    before = source.read_bytes()
    note = "  Owner scope decision\nKeep - literally  \n"
    result = invoke(
        "approve",
        outcome["run_id"],
        data_dir,
        stdin=note + "\n",
        extra=("--decline",) if decline else (),
    )
    assert result.exit_code == 0, result.output
    with Ledger(data_dir / "helmraiser.db") as ledger:
        (approval,) = ledger.approvals(outcome["run_id"])
        assert (approval["kind"], approval["decision"], approval["note"]) == (
            "scope",
            "declined" if decline else "approved",
            note,
        )
        event = ledger.events(outcome["run_id"], types=("handed_back",))[-1]
        assert event["payload"]["scope"]["note"] == note
    if decline:
        assert source.read_bytes() == before


@pytest.mark.parametrize(
    "command", ["approve", "approve-decline", "reject", "rework", "record-landed"]
)
@pytest.mark.parametrize("stdin", ["", "\n", " \n\t\n"])
def test_empty_stdin_preserves_decision_state(tmp_project, data_dir, fake_pi, command, stdin):
    if command == "approve-decline":
        outcome, source = park_on_scope(tmp_project, data_dir, fake_pi)
        command, extra = "approve", ("--decline",)
    elif command == "record-landed":
        outcome = failed_run(tmp_project, data_dir, fake_pi)
        source = tmp_project.parent / "tasks" / "fix-widget.md"
        extra = ("--commit", merge_by_hand(tmp_project, outcome))
    else:
        outcome = queue_and_run(tmp_project, data_dir, fake_pi)
        source = tmp_project.parent / "tasks" / "fix-widget.md"
        extra = ()
    run_id = outcome["run_id"]
    with Ledger(data_dir / "helmraiser.db") as ledger:
        before = decision_state(ledger, run_id, source, data_dir), ledger.reworks(run_id)
        result = invoke(command, run_id, data_dir, stdin=stdin, extra=extra)
        assert result.exit_code == 2, result.output
        assert json.loads(result.output)["error"] == "note read from stdin was empty"
        assert (decision_state(ledger, run_id, source, data_dir), ledger.reworks(run_id)) == before
    assert not (data_dir / "runs" / run_id / "rework.lock").exists()
    assert Path(outcome["worktree_path"]).is_dir()
    assert worktree_manager(tmp_project)._metadata_path(run_id).is_file()


@pytest.mark.parametrize(
    "command", ["approve", "approve-decline", "reject", "rework", "record-landed"]
)
def test_empty_stdin_is_resolved_before_ledger_access(data_dir, monkeypatch, command):
    def forbidden(*args, **kwargs):
        pytest.fail("empty stdin must be rejected before ledger access")

    monkeypatch.setattr(commands, "_ledger", forbidden)
    extra = ()
    if command == "approve-decline":
        command, extra = "approve", ("--decline",)
    elif command == "record-landed":
        extra = ("--commit", "HEAD")
    result = invoke(command, "unknown-run", data_dir, extra=extra)
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["error"] == "note read from stdin was empty"
    assert not data_dir.exists()


@pytest.mark.parametrize("command", ["approve", "reject", "rework", "record-landed"])
def test_inline_note_is_unchanged(tmp_project, data_dir, fake_pi, command):
    extra = ()
    if command == "record-landed":
        outcome = failed_run(tmp_project, data_dir, fake_pi)
        extra = ("--commit", merge_by_hand(tmp_project, outcome))
    else:
        outcome = queue_and_run(tmp_project, data_dir, fake_pi)
    run_id = outcome["run_id"]
    note = "  keep - literally  \n"
    result = invoke(command, run_id, data_dir, note=note, stdin="not the note\n", extra=extra)
    assert result.exit_code == 0, result.output
    with Ledger(data_dir / "helmraiser.db") as ledger:
        if command == "record-landed":
            assert json.loads(result.output)["note"] == note
            assert ledger.events(run_id, types=("landed_outside",))[0]["payload"]["note"] == note
        else:
            assert ledger.approvals(run_id)[0]["note"] == note
            if command == "rework":
                assert ledger.reworks(run_id)[0]["note"] == note


@pytest.mark.parametrize("command", ["approve", "reject", "rework", "record-landed"])
def test_note_help_mentions_stdin(command):
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output
    assert "--note" in result.output
    assert "reads stdin" in " ".join(result.output.replace("│", " ").split())
