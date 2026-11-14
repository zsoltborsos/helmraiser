"""``helmraiser approve`` / ``reject`` on a scope request, and the intake warning, with
subprocess fake pi."""

import json
import subprocess
from pathlib import Path

import pytest

from helmraiser.ledger.db import Ledger
from helmraiser.task.loader import parse_task
from tests.cli.test_approve_reject import (
    REVIEW_PASS,
    TASK,
    cli_env,  # noqa: F401 -- shared isolated configuration fixture
    invoke,
)

REQUEST = "SCOPE: tests/old_test.txt, docs/notes.md — the old test encodes the replaced behaviour"
REQUESTED = ["tests/old_test.txt", "docs/notes.md"]
PLAN = "# Implementation plan\n\n1. Update the old test and write done.txt.\n2. Run verify."


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


def prompt_of(call):
    prompt = call["argv"][-1]
    return Path(prompt[1:]).read_text() if prompt.startswith("@") else prompt


def implement_calls(fake_pi):
    return [c for c in fake_pi.calls() if c["session_id"] and "-implement" in c["session_id"]]


def park_on_scope(tmp_project, data_dir, fake_pi, *, plan=False):
    path = tmp_project.parent / "tasks" / "fix-widget.md"  # keep the primary clean
    path.parent.mkdir(exist_ok=True)
    text = TASK.format(
        project=tmp_project,
        policy="review",
        delivery="merge",
        task_id="fix-widget",
        marker="done.txt",
    )
    if plan:
        text = text.replace("isolation: none\n", "isolation: none\nplan: true\n")
    path.write_text(text.replace("isolation: none\n", 'isolation: none\npaths: ["done.txt"]\n'))
    result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    fake_pi.write_script(
        {
            "sessions": (
                {"*-plan": {"text": f"{REQUEST}\n\n{PLAN}"}}
                if plan
                else {"*-implement": {"text": REQUEST, "files": [["done.txt", "part\n"]]}}
            )
        }
    )
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    outcome = json.loads(result.output.splitlines()[0])
    assert outcome["status"] == "awaiting_approval" and outcome["pending_approval"] == "scope"
    if plan:
        assert not implement_calls(fake_pi)
    return outcome, path


def test_scope_request_shows_in_status_and_report(tmp_project, data_dir, fake_pi):
    outcome, _ = park_on_scope(tmp_project, data_dir, fake_pi)
    request = {"paths": REQUESTED, "reason": "the old test encodes the replaced behaviour"}
    assert outcome["scope_request"] == request
    status = json.loads(invoke("status", "--json", data_dir=data_dir).output)
    row = next(r for r in status["awaiting"] if r["run_id"] == outcome["run_id"])
    assert row["pending"] == "scope" and row["scope_request"] == request
    report = json.loads(invoke("report", "--json", data_dir=data_dir).output)
    entry = next(e for e in report["awaiting"] if e["run_id"] == outcome["run_id"])
    assert entry["pending"] == "scope" and entry["scope_request"] == request
    human = invoke("status", data_dir=data_dir).output
    assert "tests/old_test.txt, docs/notes.md — the old test" in human


def test_approve_scope_edits_task_file_resumes_and_lands(tmp_project, data_dir, fake_pi):
    outcome, path = park_on_scope(tmp_project, data_dir, fake_pi)
    run_id = outcome["run_id"]
    ledger = Ledger(data_dir / "helmraiser.db")
    before = path.read_text()
    first_session = implement_calls(fake_pi)[0]["session_id"]

    result = invoke(
        "approve", run_id[:8], "--note", "Keep the fixture", "--json", data_dir=data_dir
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "approved": "scope",
        "paths": REQUESTED,
        "run_id": run_id,
        "task_id": outcome["task_id"],
        "project": str(tmp_project),
        "status": "resuming",
        "stage": "implement",
        "worker": "none",
        "worker_serves_project": False,
    }
    after = path.read_text()
    # A minimal text edit: only the new list entries are added.
    assert "paths:\n- done.txt\n- tests/old_test.txt\n- docs/notes.md\n" in after
    assert after.replace("- tests/old_test.txt\n- docs/notes.md\n", "") == before
    assert ledger.get_task(outcome["task_id"])["snapshot"] == after
    assert (data_dir / "runs" / run_id / "task.md").read_text() == after
    approval = ledger.approvals(run_id)[0]
    assert (approval["kind"], approval["decision"], approval["note"]) == (
        "scope",
        "approved",
        "Keep the fixture",
    )

    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {
                    "text": "Done.",
                    "files": [["tests/old_test.txt", "new\n"], ["done.txt", "ok\n"]],
                },
                "*-review*": {"text": REVIEW_PASS},
            }
        }
    )
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["run_id"] == run_id and resumed["status"] == "awaiting_approval"
    assert resumed["pending_approval"] == "merge", resumed["failure_reason"]
    assert resumed["worktree_path"] == outcome["worktree_path"]
    call = implement_calls(fake_pi)[1]
    assert call["session_id"] == first_session
    prompt = prompt_of(call)
    assert "The owner approved these paths: `tests/old_test.txt`, `docs/notes.md`." in prompt
    assert "Keep the fixture" in prompt

    result = invoke("approve", run_id, "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert (tmp_project / "tests" / "old_test.txt").read_text() == "new\n"
    assert (tmp_project / "done.txt").read_text() == "ok\n"


@pytest.mark.parametrize("edited", [False, True])
def test_decline_scope_resumes_without_editing_task(tmp_project, data_dir, fake_pi, edited):
    outcome, path = park_on_scope(tmp_project, data_dir, fake_pi)
    run_id = outcome["run_id"]
    ledger = Ledger(data_dir / "helmraiser.db")
    if edited:
        path.write_text(path.read_text() + "\nThe owner changed the verify contract.\n")
    before = path.read_bytes()
    first_session = implement_calls(fake_pi)[0]["session_id"]
    note = "Do NOT edit those files; change the verify contract instead."
    result = invoke("approve", run_id[:8], "--decline", "--note", note, "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "declined": "scope",
        "paths": REQUESTED,
        "run_id": run_id,
        "task_id": outcome["task_id"],
        "project": str(tmp_project),
        "status": "resuming",
        "stage": "implement",
        "worker": "none",
        "worker_serves_project": False,
    }
    assert path.read_bytes() == before
    assert ledger.get_task(outcome["task_id"])["snapshot"] == before.decode()
    assert (data_dir / "runs" / run_id / "task.md").read_bytes() == before
    row = ledger.get_run(run_id)
    assert (row["status"], row["stage"]) == ("resuming", "implement")
    approval = ledger.approvals(run_id)[0]
    assert (approval["kind"], approval["decision"], approval["note"]) == ("scope", "declined", note)
    assert ledger.pending_approval(run_id) is None
    assert ledger.events(run_id, types=("handed_back",))[-1]["payload"] == {
        "stage": "implement",
        "scope": {"paths": [], "declined": REQUESTED, "note": note},
    }
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {"text": "Done.", "files": [["done.txt", "ok\n"]]},
                "*-review*": {"text": REVIEW_PASS},
            }
        }
    )
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["run_id"] == run_id and resumed["pending_approval"] == "merge"
    call = implement_calls(fake_pi)[1]
    assert call["session_id"] == first_session
    prompt = prompt_of(call)
    assert f"Scope request declined (tests/old_test.txt, docs/notes.md): {note}" in prompt
    assert "The owner approved these paths" not in prompt
    assert ledger.events(run_id, types=("run_resume",))[-1]["payload"]["from"] == "scope"
    assert invoke("approve", run_id, "--json", data_dir=data_dir).exit_code == 0
    assert path.read_bytes() == before
    assert (tmp_project / "done.txt").read_text() == "ok\n"


def decision_state(ledger, run_id, path, data_dir):
    """All decision-related state, including the run directory, for atomic refusals."""
    return (
        ledger.get_run(run_id),
        ledger.get_task(ledger.get_run(run_id)["task_id"]),
        ledger.approvals(run_id),
        ledger.events(run_id),
        path.read_bytes(),
        {str(p): p.read_bytes() for p in (data_dir / "runs" / run_id).rglob("*") if p.is_file()},
    )


@pytest.mark.parametrize(
    "args,words",
    [
        ([], ["--note"]),
        (["--note", ""], ["--note"]),
        (["--note", "  "], ["--note"]),
        (["--note", "no", "--paths", "docs/notes.md"], ["--decline", "--paths"]),
        (["--note", "no", "--paths", ""], ["--decline", "--paths"]),
    ],
)
def test_decline_scope_refusals_are_read_only(tmp_project, data_dir, fake_pi, args, words):
    outcome, path = park_on_scope(tmp_project, data_dir, fake_pi)
    run_id = outcome["run_id"]
    ledger = Ledger(data_dir / "helmraiser.db")
    before = decision_state(ledger, run_id, path, data_dir)
    result = invoke("approve", run_id, "--decline", *args, "--json", data_dir=data_dir)
    assert result.exit_code == 2, result.output
    error = json.loads(result.output)["error"]
    assert all(word in error for word in words)
    assert decision_state(ledger, run_id, path, data_dir) == before


@pytest.mark.parametrize("kind", ["plan", "merge", "landed", "unknown"])
def test_decline_needs_pending_scope(tmp_project, data_dir, fake_pi, kind):
    from tests.cli.test_approve_reject import queue_and_run

    outcome = queue_and_run(
        tmp_project,
        data_dir,
        fake_pi,
        policy="approve-plan-first" if kind == "plan" else "review",
    )
    run_id = outcome["run_id"]
    if kind == "landed":
        assert invoke("approve", run_id, "--json", data_dir=data_dir).exit_code == 0
    ledger = Ledger(data_dir / "helmraiser.db")
    path = Path(ledger.get_task(outcome["task_id"])["source_path"])
    before = decision_state(ledger, run_id, path, data_dir)
    result = invoke(
        "approve",
        "unknown" if kind == "unknown" else run_id,
        "--decline",
        "--note",
        "no",
        "--json",
        data_dir=data_dir,
    )
    assert result.exit_code == (1 if kind == "unknown" else 2), result.output
    error = json.loads(result.output)["error"]
    if kind == "landed":
        assert error == f"run {run_id} is landed, not awaiting approval"
    elif kind == "unknown":
        assert "unknown run" in error
    else:
        assert error == "--decline applies only to a pending scope request"
    assert decision_state(ledger, run_id, path, data_dir) == before


def test_decline_invalid_task_is_read_only(tmp_project, data_dir, fake_pi):
    outcome, path = park_on_scope(tmp_project, data_dir, fake_pi)
    path.write_text(path.read_text().replace("policy: review", "policy: nonsense"))
    run_id = outcome["run_id"]
    ledger = Ledger(data_dir / "helmraiser.db")
    before = decision_state(ledger, run_id, path, data_dir)
    result = invoke("approve", run_id, "--decline", "--note", "no", "--json", data_dir=data_dir)
    assert result.exit_code == 2, result.output
    assert "error" in json.loads(result.output)
    assert decision_state(ledger, run_id, path, data_dir) == before


def test_decline_human_output_and_repeated_request(tmp_project, data_dir, fake_pi, monkeypatch):
    from rich.console import Console

    from helmraiser.cli import commands

    monkeypatch.setattr(commands, "console", Console(width=1000))
    outcome, path = park_on_scope(tmp_project, data_dir, fake_pi)
    run_id = outcome["run_id"]
    before = path.read_bytes()
    note = "[bold]literal note[/bold]"
    result = invoke("approve", run_id, "--decline", "--note", note, data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert f"scope request declined for {outcome['task_id']} run {run_id}:" in result.output
    assert ", ".join(REQUESTED) in result.output
    assert "paths: is unchanged; the run resumes without new paths" in result.output
    assert f"the worker resumes run {run_id} at implement" in result.output
    fake_pi.write_script({"sessions": {"*-implement": {"text": REQUEST}}})
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["run_id"] == run_id and resumed["pending_approval"] == "scope"
    assert resumed["scope_request"]["paths"] == REQUESTED
    ledger = Ledger(data_dir / "helmraiser.db")
    approvals = ledger.approvals(run_id)
    assert [(a["kind"], a["decision"]) for a in approvals] == [
        ("scope", "declined"),
        ("scope", "pending"),
    ]
    assert approvals[0]["id"] != approvals[1]["id"]
    assert approvals[0]["note"] == note
    assert len(ledger.events(run_id, types=("scope_requested",))) == 2
    assert path.read_bytes() == before


def test_approve_scope_subset(tmp_project, data_dir, fake_pi):
    outcome, path = park_on_scope(tmp_project, data_dir, fake_pi)
    result = invoke(
        "approve", outcome["run_id"], "--paths", " docs/notes.md ", "--json", data_dir=data_dir
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["paths"] == ["docs/notes.md"]
    text = path.read_text()
    assert "docs/notes.md" in text and "tests/old_test.txt" not in text
    events = Ledger(data_dir / "helmraiser.db").events(outcome["run_id"], types=("handed_back",))
    assert events[-1]["payload"]["scope"] == {"paths": ["docs/notes.md"], "note": None}


@pytest.mark.parametrize("paths", ["other.txt", "docs/notes.md,other.txt", " , "])
def test_approve_scope_refuses_unrequested_paths(tmp_project, data_dir, fake_pi, paths):
    outcome, path = park_on_scope(tmp_project, data_dir, fake_pi)
    before = path.read_text()
    result = invoke("approve", outcome["run_id"], "--paths", paths, "--json", data_dir=data_dir)
    assert result.exit_code == 2, result.output
    assert "--paths must name requested paths" in json.loads(result.output)["error"]
    ledger = Ledger(data_dir / "helmraiser.db")
    assert path.read_text() == before
    assert ledger.get_run(outcome["run_id"])["status"] == "awaiting_approval"
    assert ledger.pending_approval(outcome["run_id"])["kind"] == "scope"


def test_approve_scope_restores_the_file_when_the_task_is_invalid(tmp_project, data_dir, fake_pi):
    outcome, path = park_on_scope(tmp_project, data_dir, fake_pi)
    invalid = path.read_text().replace("policy: review", "policy: nonsense")
    path.write_text(invalid)
    result = invoke("approve", outcome["run_id"], "--json", data_dir=data_dir)
    assert result.exit_code == 2, result.output
    assert path.read_text() == invalid
    assert Ledger(data_dir / "helmraiser.db").pending_approval(outcome["run_id"])["kind"] == "scope"


def test_paths_option_needs_a_scope_request(tmp_project, data_dir, fake_pi):
    from tests.cli.test_approve_reject import queue_and_run

    outcome = queue_and_run(tmp_project, data_dir, fake_pi)
    result = invoke("approve", outcome["run_id"], "--paths", "a.txt", "--json", data_dir=data_dir)
    assert result.exit_code == 2, result.output
    assert Ledger(data_dir / "helmraiser.db").get_run(outcome["run_id"])["status"] == (
        "awaiting_approval"
    )


def test_reject_scope_request_fails_the_run(tmp_project, data_dir, fake_pi):
    outcome, path = park_on_scope(tmp_project, data_dir, fake_pi)
    before = path.read_text()
    result = invoke("reject", outcome["run_id"], "--note", "not those", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["rejected"] == "scope"
    ledger = Ledger(data_dir / "helmraiser.db")
    run = ledger.get_run(outcome["run_id"])
    assert (run["status"], run["failure_reason"]) == ("failed", "rejected")
    approval = ledger.approvals(outcome["run_id"])[0]
    assert (approval["kind"], approval["decision"], approval["note"]) == (
        "scope",
        "rejected",
        "not those",
    )
    assert path.read_text() == before


def test_plan_scope_approve_paths_resumes_with_saved_plan(tmp_project, data_dir, fake_pi):
    outcome, path = park_on_scope(tmp_project, data_dir, fake_pi, plan=True)
    run_id = outcome["run_id"]
    ledger = Ledger(data_dir / "helmraiser.db")
    plan_path = data_dir / "runs" / run_id / "plan.md"
    assert plan_path.read_text() == PLAN + "\n"
    result = invoke(
        "approve",
        run_id,
        "--paths",
        "tests/old_test.txt",
        "--note",
        "Keep the fixture",
        "--json",
        data_dir=data_dir,
    )
    assert result.exit_code == 0, result.output
    decision = json.loads(result.output)
    assert (decision["approved"], decision["status"], decision["stage"]) == (
        "scope",
        "resuming",
        "implement",
    )
    assert decision["paths"] == ["tests/old_test.txt"]
    snapshot = ledger.get_task(outcome["task_id"])["snapshot"]
    assert snapshot == path.read_text()
    assert parse_task(snapshot, path, {}).paths == ("done.txt", "tests/old_test.txt")
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {
                    "text": "Done.",
                    "files": [["tests/old_test.txt", "new\n"], ["done.txt", "ok\n"]],
                },
                "*-review*": {"text": REVIEW_PASS},
            }
        }
    )
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["run_id"] == run_id and resumed["pending_approval"] == "merge"
    assert resumed["worktree_path"] == outcome["worktree_path"]
    (call,) = implement_calls(fake_pi)
    prompt = prompt_of(call)
    assert "The owner approved these paths: `tests/old_test.txt`." in prompt
    assert "Keep the fixture" in prompt and f"## Plan\n\n{PLAN}" in prompt
    assert "## Approved plan" not in prompt and REQUEST not in prompt
    assert plan_path.read_text() == PLAN + "\n"
    assert len(ledger.iterations(run_id)) == 1


def test_plan_scope_decline_resumes_with_saved_plan(tmp_project, data_dir, fake_pi):
    outcome, path = park_on_scope(tmp_project, data_dir, fake_pi, plan=True)
    run_id = outcome["run_id"]
    ledger = Ledger(data_dir / "helmraiser.db")
    before = path.read_bytes()
    note = "Use only the allowed paths"
    result = invoke("approve", run_id, "--decline", "--note", note, "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    decision = json.loads(result.output)
    assert (decision["declined"], decision["status"], decision["stage"]) == (
        "scope",
        "resuming",
        "implement",
    )
    assert path.read_bytes() == before
    assert parse_task(path.read_text(), path, {}).paths == ("done.txt",)
    assert ledger.get_task(outcome["task_id"])["snapshot"] == before.decode()
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {"text": "Done.", "files": [["done.txt", "ok\n"]]},
                "*-review*": {"text": REVIEW_PASS},
            }
        }
    )
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["run_id"] == run_id and resumed["pending_approval"] == "merge"
    (call,) = implement_calls(fake_pi)
    prompt = prompt_of(call)
    assert prompt.startswith(f"Scope request declined (tests/old_test.txt, docs/notes.md): {note}")
    assert f"## Plan\n\n{PLAN}" in prompt
    assert "The owner approved these paths" not in prompt
    assert path.read_bytes() == before and len(ledger.iterations(run_id)) == 1


def test_plan_scope_reject_ends_run(tmp_project, data_dir, fake_pi):
    outcome, path = park_on_scope(tmp_project, data_dir, fake_pi, plan=True)
    before = path.read_bytes()
    result = invoke("reject", outcome["run_id"], "--note", "not those", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["rejected"] == "scope"
    ledger = Ledger(data_dir / "helmraiser.db")
    run = ledger.get_run(outcome["run_id"])
    assert (run["status"], run["failure_reason"]) == ("failed", "rejected")
    (approval,) = ledger.approvals(outcome["run_id"])
    assert (approval["kind"], approval["decision"], approval["note"]) == (
        "scope",
        "rejected",
        "not those",
    )
    assert path.read_bytes() == before and not implement_calls(fake_pi)


def test_intake_warns_about_verify_files_outside_paths(tmp_project, data_dir):
    (tmp_project / "tests").mkdir()
    (tmp_project / "tests" / "test_old.py").write_text("def test_x(): pass\n")
    path = tmp_project.parent / "tasks" / "fix-widget.md"
    path.parent.mkdir(exist_ok=True)
    text = TASK.format(
        project=tmp_project,
        policy="review",
        delivery="merge",
        task_id="fix-widget",
        marker="done.txt && python -m pytest tests/test_old.py",
    )
    path.write_text(text.replace("isolation: none\n", 'isolation: none\npaths: ["done.txt"]\n'))
    result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "queued"
    assert (
        "verify runs tests/test_old.py, which is outside paths: "
        "add it if the change needs to edit it"
    ) in payload["notes"]


@pytest.mark.parametrize("declared", [True, False])
def test_intake_warns_about_body_files_outside_paths(tmp_project, data_dir, declared):
    (tmp_project / "tests").mkdir()
    (tmp_project / "tests/test_old.py").write_text("def test_x(): pass\n")
    subprocess.run(["git", "-C", str(tmp_project), "add", "tests/test_old.py"], check=True)
    path = tmp_project.parent / "tasks/fix-widget.md"
    path.parent.mkdir(exist_ok=True)
    text = TASK.format(
        project=tmp_project,
        policy="review",
        delivery="merge",
        task_id="fix-widget",
        marker="done.txt",
    )
    if declared:
        text = text.replace("isolation: none\n", 'isolation: none\npaths: ["done.txt"]\n')
    path.write_text(text + "\nEdit tests/test_old.py.\n")

    result = invoke("intake", str(path), "--yes", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "queued"
    body_notes = [n for n in payload["notes"] if n.startswith("body mentions")]
    assert body_notes == (
        [
            "body mentions files outside paths: tests/test_old.py"
            " — add them to paths if the task edits them"
        ]
        if declared
        else []
    )
    expected = ("done.txt",) if declared else None
    saved = path.read_text()
    assert parse_task(saved, path, {}).paths == expected
    snapshot = Ledger(data_dir / "helmraiser.db").get_task(payload["task_id"])["snapshot"]
    assert parse_task(snapshot, path, {}).paths == expected
