"""CLI rework admission and worker pickup, with subprocess fake pi."""

import json
from pathlib import Path

import pytest

from helmraiser.cli import commands
from helmraiser.ledger.db import Ledger
from helmraiser.loop.prompts import REBASE_NOTE
from helmraiser.task.loader import render_task
from tests.cli.test_approve_reject import (
    REVIEW_PASS,
    _git,
    cli_env,  # noqa: F401 -- shared isolated configuration fixture
    invoke,
    queue_and_run,
)
from tests.ledger.test_db import make_spec


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


@pytest.mark.parametrize("delivery", ["merge", "pr"])
def test_rework_worker_pickup_and_json(tmp_project, data_dir, fake_pi, delivery):
    outcome = queue_and_run(tmp_project, data_dir, fake_pi, delivery=delivery, paths=("done.txt",))
    run_id = outcome["run_id"]
    ledger = Ledger(data_dir / "helmraiser.db")
    findings = [{"severity": "minor", "file": "done.txt", "line": 1, "note": "clarify widget"}]
    ledger.append_event(run_id, "helmraiser", "review_verdict", {"findings": findings})
    # Rework re-reads the task file like retry: an invalid one refuses the rework.
    source = Path(ledger.get_task(outcome["task_id"])["source_path"])
    valid = source.read_text()
    source.write_text("invalid task")
    refused = invoke("rework", run_id[:8], "--note", "x", "--json", data_dir=data_dir)
    assert refused.exit_code == 2, refused.output
    assert not ledger.reworks(run_id) and ledger.get_run(run_id)["status"] == "awaiting_approval"
    amended = valid + "\nOwner amendment: keep the 30 s limit.\n"
    source.write_text(amended)
    before = len(fake_pi.calls())
    note = "  Owner café\n[bold]fix this[/bold]  "
    result = invoke("rework", run_id[:8], "--note", note, "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "reworked": "merge",
        "run_id": run_id,
        "task_id": outcome["task_id"],
        "project": str(tmp_project),
        "status": "resuming",
        "stage": "implement",
        "worker": "none",
        "worker_serves_project": False,
    }
    assert len(fake_pi.calls()) == before
    assert ledger.get_task(outcome["task_id"])["snapshot"] == amended
    assert (data_dir / "runs" / run_id / "task.md").read_text() == amended
    assert ledger.approvals(run_id)[0]["decision"] == "rework"
    assert ledger.approvals(run_id)[0]["note"] == note
    request = ledger.reworks(run_id)[0]
    assert "rebase" not in request
    assert request["findings"] == findings and request["iteration_allowance"] == 2
    assert request["extra_minutes"] == 20
    assert request["extra_cost_usd"] == pytest.approx(4 / 3)
    again = invoke("rework", run_id, "--note", "again", "--json", data_dir=data_dir)
    assert again.exit_code == 2
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {"text": "Done", "files": [["done.txt", "fixed\n"]]},
                "*-review*": {"text": REVIEW_PASS},
            }
        }
    )
    filtered = invoke(
        "work",
        "--once",
        "--project",
        str(tmp_project.parent / "other"),
        "--json",
        data_dir=data_dir,
    )
    assert filtered.exit_code == 0, filtered.output
    assert ledger.get_run(run_id)["status"] == "resuming"
    dry = invoke("work", "--once", "--dry-run", "--json", data_dir=data_dir)
    assert dry.exit_code == 0, dry.output
    assert ledger.get_run(run_id)["status"] == "resuming"
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    resumed = json.loads(result.output.splitlines()[0])
    assert resumed["run_id"] == run_id and resumed["status"] == "awaiting_approval"
    assert resumed["worktree_path"] == outcome["worktree_path"]
    assert ledger.pending_approval(run_id)["kind"] == "merge"
    implement = next(
        c for c in fake_pi.calls()[before:] if c["session_id"] and "implement" in c["session_id"]
    )
    prompt = Path(implement["argv"][-1][1:]).read_text()
    assert prompt.index(note) < prompt.index("clarify widget") < prompt.index("## Acceptance")
    assert "Verify failed" not in prompt
    assert not (tmp_project / "done.txt").exists()  # both modes still parked
    if delivery == "merge":
        result = invoke("approve", run_id, "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        assert (tmp_project / "done.txt").read_text() == "fixed\n"
        assert ledger.approvals(run_id)[-1]["decision"] == "approved"


@pytest.mark.parametrize(
    "args",
    [["--note", ""], ["--note", " \n\t"], ["--rebase", "--note", " \n\t"]],
)
def test_rework_requires_nonempty_note(tmp_project, data_dir, fake_pi, args):
    out = queue_and_run(tmp_project, data_dir, fake_pi)
    result = invoke("rework", out["run_id"], *args, "--json", data_dir=data_dir)
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["error"] == "--note must be non-empty"
    ledger = Ledger(data_dir / "helmraiser.db")
    assert ledger.get_run(out["run_id"])["status"] == "awaiting_approval"
    assert ledger.pending_approval(out["run_id"])["decision"] == "pending"
    assert not ledger.reworks(out["run_id"])


def test_rework_requires_a_note_or_rebase(tmp_project, data_dir, fake_pi):
    out = queue_and_run(tmp_project, data_dir, fake_pi)
    run_id = out["run_id"]
    ledger = Ledger(data_dir / "helmraiser.db")
    before = ledger.get_run(run_id), ledger.approvals(run_id), ledger.events(run_id)
    result = invoke("rework", run_id, "--json", data_dir=data_dir)
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["error"] == "--note or --rebase is required"
    assert before == (ledger.get_run(run_id), ledger.approvals(run_id), ledger.events(run_id))
    assert not ledger.reworks(run_id)


def move_base(project):
    (project / "upstream.txt").write_text("landed meanwhile\n")
    _git(project, "add", "upstream.txt")
    _git(project, "commit", "-m", "move base")


@pytest.mark.parametrize("delivery", ["merge", "pr"])
def test_rework_rebase_alone_hands_back_with_the_default_note(
    tmp_project, data_dir, fake_pi, delivery
):
    out = queue_and_run(tmp_project, data_dir, fake_pi, delivery=delivery)
    run_id = out["run_id"]
    ledger = Ledger(data_dir / "helmraiser.db")
    ledger.append_event(run_id, "helmraiser", "review_verdict", {"findings": [{"note": "old"}]})
    move_base(tmp_project)
    before = len(fake_pi.calls())
    result = invoke("rework", run_id, "--rebase", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "reworked": "merge",
        "rebase": True,
        "run_id": run_id,
        "task_id": out["task_id"],
        "project": str(tmp_project),
        "status": "resuming",
        "stage": "implement",
        "worker": "none",
        "worker_serves_project": False,
    }
    request = ledger.reworks(run_id)[0]
    assert request["rebase"] is True
    assert request["note"] == REBASE_NOTE and request["findings"] == []
    assert ledger.approvals(run_id)[0]["note"] == REBASE_NOTE
    row = ledger.get_run(run_id)
    assert row["status"] == "resuming" and row["stage"] == "implement"
    assert len(fake_pi.calls()) == before


def test_rework_rebase_with_a_note_keeps_the_note_and_findings(tmp_project, data_dir, fake_pi):
    out = queue_and_run(tmp_project, data_dir, fake_pi)
    run_id = out["run_id"]
    ledger = Ledger(data_dir / "helmraiser.db")
    findings = [{"severity": "minor", "file": "done.txt", "line": 1, "note": "clarify widget"}]
    ledger.append_event(run_id, "helmraiser", "review_verdict", {"findings": findings})
    move_base(tmp_project)
    note = "keep the 30 s limit"
    result = invoke("rework", run_id, "--rebase", "--note", note, data_dir=data_dir)
    assert result.exit_code == 0, result.output
    base = ledger.get_run(run_id)["base_branch"]
    assert (
        f"rework requested for {out['task_id']} run {run_id} (rebase onto {base})" in result.output
    )
    request = ledger.reworks(run_id)[0]
    assert request["note"] == note and request["findings"] == findings
    assert request["rebase"] is True


@pytest.mark.parametrize("args", [[], ["--note", "fix this too"]])
def test_rework_rebase_is_refused_when_the_base_has_not_moved(tmp_project, data_dir, fake_pi, args):
    out = queue_and_run(tmp_project, data_dir, fake_pi)
    run_id = out["run_id"]
    ledger = Ledger(data_dir / "helmraiser.db")
    before = ledger.get_run(run_id), ledger.approvals(run_id), ledger.events(run_id)
    result = invoke("rework", run_id, "--rebase", *args, "--json", data_dir=data_dir)
    assert result.exit_code == 2 and "nothing to rebase" in result.output
    assert before == (ledger.get_run(run_id), ledger.approvals(run_id), ledger.events(run_id))
    assert ledger.get_run(run_id)["status"] == "awaiting_approval"
    assert ledger.pending_approval(run_id)["kind"] == "merge"
    assert not ledger.reworks(run_id)


def test_rework_rebase_after_a_refused_approve(tmp_project, data_dir, fake_pi):
    out = queue_and_run(tmp_project, data_dir, fake_pi, marker="README.md", content="second run\n")
    run_id = out["run_id"]
    (tmp_project / "README.md").write_text("first run\n")
    _git(tmp_project, "commit", "-am", "landed meanwhile")
    result = invoke("approve", run_id, "--json", data_dir=data_dir)
    assert result.exit_code == 1 and "finalize_refused" in result.output
    result = invoke("rework", run_id, "--rebase", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["rebase"] is True
    assert Ledger(data_dir / "helmraiser.db").get_run(run_id)["status"] == "resuming"


@pytest.mark.parametrize("status", ["running", "landed", "failed", "resuming", "plan", "missing"])
def test_rework_refuses_non_merge_approval(tmp_project, data_dir, fake_pi, status):
    out = queue_and_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    run_id = out["run_id"]
    if status in ("plan", "missing"):
        ledger.decide_approval(ledger.pending_approval(run_id)["id"], "approved")
        if status == "plan":
            ledger.record_approval(run_id, "plan", "pending")
    else:
        ledger.update_run(run_id, status=status)
    before = ledger.get_run(run_id), ledger.approvals(run_id)
    result = invoke("rework", run_id, "--note", "fix", "--json", data_dir=data_dir)
    assert result.exit_code == 2, result.output
    assert before == (ledger.get_run(run_id), ledger.approvals(run_id))
    assert not ledger.reworks(run_id)


def test_rework_missing_worktree_preserves_approval(tmp_project, data_dir, fake_pi):
    out = queue_and_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    ledger.update_run(out["run_id"], worktree_path=str(tmp_project / "gone"))
    result = invoke("rework", out["run_id"], "--note", "fix", "--json", data_dir=data_dir)
    assert result.exit_code == 1 and "gone" in result.output
    assert ledger.pending_approval(out["run_id"])["kind"] == "merge"
    assert ledger.get_run(out["run_id"])["status"] == "awaiting_approval"


def test_rework_scope_still_blocks_acceptance(tmp_project, data_dir, fake_pi):
    out = queue_and_run(tmp_project, data_dir, fake_pi, paths=("done.txt",))
    result = invoke("rework", out["run_id"], "--note", "fix", data_dir=data_dir)
    assert result.exit_code == 0
    fake_pi.write_script({"sessions": {"*-implement": {"files": [["forbidden.txt", "no"]]}}})
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    failed = json.loads(result.output.splitlines()[0])
    assert failed["status"] == "failed" and failed["failure_reason"] == "budget:iterations"
    assert [r["name"] for r in failed["verify_results"]] == ["task-paths"]


@pytest.mark.parametrize("serves", [True, False])
def test_rework_worker_serving_hints_and_queued_priority(
    tmp_project,
    data_dir,
    fake_pi,
    monkeypatch,
    serves,
):
    out = queue_and_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    queued = make_spec(tmp_project, "next")
    ledger.upsert_task(queued, render_task(queued))
    monkeypatch.setattr(commands, "worker_state", lambda data_dir: "running")
    monkeypatch.setattr(commands, "worker_serves", lambda data_dir, project: serves)
    result = invoke("rework", out["run_id"], "--note", "fix", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["worker"] == "running" and payload["worker_serves_project"] == serves
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {"files": [["done.txt", "fixed"]]},
                "*-review*": {"text": REVIEW_PASS},
            }
        }
    )
    result = invoke("work", "--once", "--json", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output.splitlines()[0])["run_id"] == out["run_id"]
    assert ledger.get_task(queued.task_id)["status"] == "queued"


@pytest.mark.parametrize("review", ["malformed", REVIEW_PASS])
def test_rework_fallback_review_file(tmp_project, data_dir, fake_pi, review):
    out = queue_and_run(tmp_project, data_dir, fake_pi, policy="approve-plan-first")
    ledger = Ledger(data_dir / "helmraiser.db")
    run_id = out["run_id"]
    ledger.decide_approval(ledger.pending_approval(run_id)["id"], "approved")
    ledger.record_approval(run_id, "merge", "pending")
    review_path = data_dir / "runs" / run_id / "review.md"
    review_path.write_text(review)
    result = invoke("rework", run_id, "--note", "fix", data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert ledger.reworks(run_id)[0]["findings"] == []


def test_rework_ambiguous_prefix(tmp_project, data_dir, fake_pi):
    out = queue_and_run(tmp_project, data_dir, fake_pi)
    ledger = Ledger(data_dir / "helmraiser.db")
    row = ledger.get_run(out["run_id"])
    ledger.create_run({**row, "run_id": out["run_id"][:8] + "-duplicate"})
    result = invoke("rework", out["run_id"][:8], "--note", "fix", "--json", data_dir=data_dir)
    assert result.exit_code == 1 and "ambiguous run prefix" in result.output
    assert not ledger.reworks(out["run_id"])


def test_rework_unknown_run(data_dir):
    result = invoke("rework", "unknown", "--note", "fix", "--json", data_dir=data_dir)
    assert result.exit_code == 1 and "unknown run" in result.output
