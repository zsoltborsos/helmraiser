"""Atomic merge rework transitions."""

import pytest

from helmraiser.ledger.db import Ledger, TaskActive
from tests.ledger.test_db import make_spec, run_row


def request(approval_id):
    return {
        "approval_id": approval_id,
        "note": " café\nfix this ",
        "findings": [],
        "iteration_allowance": 2,
        "extra_cost_usd": 2.5,
        "extra_minutes": 45,
    }


@pytest.mark.parametrize(
    "status,kind,stale",
    [
        ("running", "merge", False),
        ("landed", "merge", False),
        ("awaiting_approval", "plan", False),
        ("awaiting_approval", None, False),
        ("awaiting_approval", "merge", True),
    ],
)
def test_rework_refusal_is_atomic(tmp_path, status, kind, stale):
    with Ledger(tmp_path / "db") as ledger:
        task = make_spec(tmp_path)
        ledger.upsert_task(task, "snapshot")
        ledger.create_run(run_row("run", task.task_id, str(tmp_path), status))
        approval = ledger.record_approval("run", kind, "pending") if kind else 0
        before = ledger.get_run("run"), ledger.get_task(task.task_id), ledger.approvals("run")
        assert not ledger.hand_back("run", "implement", rework=request(approval + int(stale)))
        assert before == (
            ledger.get_run("run"),
            ledger.get_task(task.task_id),
            ledger.approvals("run"),
        )
        assert ledger.reworks("run") == []


def test_rework_rounds_atomic_claim_and_worker_selection(tmp_path):
    path = tmp_path / "db"
    with Ledger(path) as ledger, Ledger(path) as other:
        task = make_spec(tmp_path)
        ledger.upsert_task(task, "snapshot")
        ledger.create_run(run_row("run", task.task_id, str(tmp_path), "awaiting_approval"))
        for round_n in (1, 2):
            approval = ledger.record_approval("run", "merge", "pending")
            payload = request(approval)
            assert ledger.hand_back("run", "implement", rework=payload)
            assert not other.hand_back("run", "implement", rework=payload)
            assert ledger.approvals("run")[-1]["decision"] == "rework"
            assert ledger.approvals("run")[-1]["note"] == payload["note"]
            assert ledger.reworks("run")[-1] == {
                **payload,
                "round": round_n,
                "iterations_before": 0,
            }
            assert ledger.get_task(task.task_id)["status"] == "resuming"
            assert ledger.next_resumable(config={})["run_id"] == "run"
            assert ledger.claim_resumable("run")
            assert not other.claim_resumable("run")
            ledger.finish_run("run", "awaiting_approval")


def reopen_metadata():
    return {
        "pr_url": "https://example/pr/1",
        "head_commit": "published",
        "worktree_path": "/restored",
        "base_commit": "base",
        "worktree_recreated": True,
    }


def test_reopen_pr_atomic_and_duplicate(tmp_path):
    path = tmp_path / "db"
    with Ledger(path) as ledger, Ledger(path) as other:
        task = make_spec(tmp_path)
        ledger.upsert_task(task, "snapshot")
        ledger.create_run(run_row("run", task.task_id, str(tmp_path), "landed"))
        ledger.update_run(
            "run",
            delivery="pr",
            pr_url=reopen_metadata()["pr_url"],
            head_commit="published",
            ended_at=123,
        )
        ledger.record_approval("run", "merge", "approved", "original")
        approvals = ledger.approvals("run")
        assert ledger.hand_back(
            "run", "implement", rework=request(None), reopen_pr=reopen_metadata()
        )
        assert not other.hand_back(
            "run", "implement", rework=request(None), reopen_pr=reopen_metadata()
        )
        row = ledger.get_run("run")
        assert row["status"] == "resuming" and row["stage"] == "implement"
        assert row["head_commit"] is None and row["ended_at"] == 123
        assert row["pr_url"] == reopen_metadata()["pr_url"]
        assert row["base_commit"] == "base" and row["worktree_path"] == "/restored"
        assert ledger.approvals("run") == approvals
        assert len(ledger.reworks("run")) == 1
        assert len(ledger.events("run", types=("rework_reopened",))) == 1
        assert ledger.get_task(task.task_id)["status"] == "resuming"


@pytest.mark.parametrize(
    "problem", ["status", "delivery", "url", "pending", "active", "stage", "request"]
)
def test_reopen_pr_refusal_atomic(tmp_path, problem):
    with Ledger(tmp_path / "db") as ledger:
        task = make_spec(tmp_path)
        ledger.upsert_task(task, "snapshot")
        ledger.create_run(run_row("run", task.task_id, str(tmp_path), "landed"))
        ledger.update_run(
            "run", delivery="pr", pr_url=reopen_metadata()["pr_url"], head_commit="published"
        )
        if problem == "status":
            ledger.update_run("run", status="awaiting_approval")
        elif problem == "delivery":
            ledger.update_run("run", delivery="local")
        elif problem == "url":
            ledger.update_run("run", pr_url="other")
        elif problem == "pending":
            ledger.record_approval("run", "merge", "pending")
        elif problem == "active":
            ledger.create_run(run_row("other", task.task_id, str(tmp_path), "running"))
        before = ledger.get_run("run"), ledger.get_task(task.task_id), ledger.approvals("run")
        assert not ledger.hand_back(
            "run",
            "verify" if problem == "stage" else "implement",
            rework=None if problem == "request" else request(None),
            reopen_pr=reopen_metadata(),
        )
        assert before == (
            ledger.get_run("run"),
            ledger.get_task(task.task_id),
            ledger.approvals("run"),
        )
        assert ledger.reworks("run") == []
        assert ledger.events("run", types=("rework_reopened",)) == []


def test_reworks_reads_only_rework_events(tmp_path):
    """Status calls reworks() per run; other event payloads must not be decoded."""
    with Ledger(tmp_path / "db") as ledger:
        task = make_spec(tmp_path)
        ledger.upsert_task(task, "snapshot")
        ledger.create_run(run_row("run", task.task_id, str(tmp_path), "running"))
        ledger.append_event("run", "approver", "rework_requested", {"note": "one"})
        with ledger._write() as conn:
            conn.execute(
                "INSERT INTO run_events (run_id, ts, source, stage, session_id, type, payload) "
                "VALUES ('run', 0, 'pi', 'implement', 's1', 'tool_execution_update', 'not json')"
            )
        ledger.append_event("run", "approver", "rework_requested", {"note": "two"})
        assert ledger.reworks("run") == [{"note": "one"}, {"note": "two"}]


def test_events_filters_by_type_without_decoding_others(tmp_path):
    with Ledger(tmp_path / "db") as ledger:
        task = make_spec(tmp_path)
        ledger.upsert_task(task, "snapshot")
        ledger.create_run(run_row("run", task.task_id, str(tmp_path), "running"))
        ledger.append_event("run", "helmraiser", "routing", {"n": 1})
        with ledger._write() as conn:
            conn.execute(
                "INSERT INTO run_events (run_id, ts, source, stage, session_id, type, payload) "
                "VALUES ('run', 0, 'pi', 'implement', 's1', 'message_end', 'not json')"
            )
        ledger.append_event("run", "helmraiser", "run_end", {"n": 2})
        events = ledger.events("run", types=("routing", "run_end"))
        assert [(e["type"], e["payload"]) for e in events] == [
            ("routing", {"n": 1}),
            ("run_end", {"n": 2}),
        ]


@pytest.mark.parametrize("status", ["awaiting_approval", "running", "resuming"])
def test_snapshot_changes_only_for_the_parked_run_named(tmp_path, status):
    with Ledger(tmp_path / "db") as ledger:
        task = make_spec(tmp_path)
        ledger.upsert_task(task, "snapshot")
        ledger.create_run(run_row("run", task.task_id, str(tmp_path), status))
        if status == "awaiting_approval":
            assert ledger.set_task_snapshot(task.task_id, "amended", parked_run="run")
            assert ledger.get_task(task.task_id)["snapshot"] == "amended"
            with pytest.raises(TaskActive):
                ledger.set_task_snapshot(task.task_id, "again", parked_run="other")
        else:
            with pytest.raises(TaskActive):
                ledger.set_task_snapshot(task.task_id, "amended", parked_run="run")
