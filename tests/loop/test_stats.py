"""Dashboard contract over temporary ledgers; no agents or run files."""

import json
from datetime import UTC, datetime

import pytest

from helmraiser.loop import stats
from helmraiser.loop.scout import draft_acceptance_scope_hash
from helmraiser.loop.scout_lenses import LENSES
from tests.cli.test_status_report_log import seed_run

NOW = datetime(2026, 1, 5, 12, tzinfo=UTC).timestamp()


def build(ledger, **kwargs):
    return stats.build_stats(ledger, config={}, now=NOW, **kwargs)


def test_empty(ledger):
    data = build(ledger)
    assert set(data) == {
        "window",
        "runs",
        "quality",
        "failures",
        "cost",
        "latency",
        "scouts",
        "weekly",
    }
    assert data["quality"]["first_time_right"] == {"count": 0, "rate": None}
    assert data["quality"]["rework_rate"] is None
    assert data["quality"]["scope_request_rate"] is None
    assert data["cost"]["per_landed_usd"] is None
    assert data["latency"]["active_minutes"] == {"median": None, "p90": None}
    assert len(data["weekly"]) == 2


def test_outcomes_cost_failures(ledger, make_task):
    task = make_task()
    for i, (status, reason) in enumerate(
        [
            ("landed", None),
            ("failed", "budget:iterations"),
            ("aborted", "rejected"),
            ("failed", "plain reason"),
            ("failed", ""),
            ("running", None),
            ("awaiting_approval", None),
        ]
    ):
        seed_run(
            ledger,
            task.task_id,
            str(i),
            status,
            started_at=NOW - 100 + i,
            failure_reason=reason,
            verdict="pass" if i == 0 else None,
        )
    ledger.start_iteration("0", 1, "session", "model")
    ledger.update_run("0", plan_cost_usd=9)
    ledger.record_approval("0", "merge", "rework")
    ledger.record_approval("1", "scope", "pending")
    data = build(ledger)
    assert data["runs"]["overall"] == {
        "landed": 1,
        "failed": 3,
        "aborted": 1,
        "active": 1,
        "awaiting_approval": 1,
    }
    assert data["quality"] == {
        "first_time_right": {"count": 0, "rate": 0.0},
        "rework_rate": 1.0,
        "scope_request_rate": 0.143,
        "retried_tasks": 1,
        "review_verdicts": {"pass": 1, "concerns": 0, "uncertain": 0, "none": 6},
    }
    assert set(data["failures"]) == {"budget", "rejected", "plain reason", "unknown"}
    assert data["failures"]["budget"] == {"count": 1, "run_ids": ["1"]}
    assert data["cost"] == {
        "total_usd": 3.5,
        "plan_usd": 9,
        "implement_usd": 0,
        "other_usd": 0,
        "per_landed_usd": 3.5,
    }
    assert build(ledger, project="/elsewhere")["runs"]["overall"]["landed"] == 0


@pytest.mark.parametrize("exclusion", [None, "iterations", "rework", "scope", "event", "earlier"])
def test_first_time_right(ledger, make_task, exclusion):
    task = make_task()
    if exclusion == "earlier":
        seed_run(ledger, task.task_id, "old", "failed", started_at=NOW - 86400 * 20)
    seed_run(ledger, task.task_id, "run", "landed", started_at=NOW - 10)
    ledger.start_iteration("run", 1, "s", "m")
    if exclusion == "iterations":
        ledger.start_iteration("run", 2, "s2", "m")
    if exclusion in {"rework", "scope"}:
        ledger.record_approval(
            "run",
            "merge" if exclusion == "rework" else "scope",
            "rework" if exclusion == "rework" else "pending",
        )
    if exclusion == "event":
        ledger.append_event("run", "helmraiser", "scope_requested", {})
    assert build(ledger)["quality"]["first_time_right"]["count"] == (1 if exclusion is None else 0)


def test_latency_two_waits(ledger, make_task, monkeypatch):
    task = make_task()
    seed_run(ledger, task.task_id, "run", "landed", started_at=NOW - 1000)
    for ts, kind, active, minutes in [(NOW - 900, "plan", 2, 2), (NOW - 600, "merge", 8, 4)]:
        monkeypatch.setattr("helmraiser.ledger.db.time.time", lambda ts=ts: ts)
        ledger.append_event(
            "run",
            "helmraiser",
            "run_end",
            {"status": "awaiting_approval", "active_minutes": active},
        )
        ledger.record_approval("run", kind, "pending")
        monkeypatch.setattr(
            "helmraiser.ledger.db.time.time", lambda ts=ts, minutes=minutes: ts + minutes * 60
        )
        ledger.record_approval("run", kind, "approve")
    latency = build(ledger)["latency"]
    assert latency["active_minutes"] == {"median": 10, "p90": 10}
    assert latency["approval_wait_minutes"]["plan"] == {"median": 2, "p90": 2}
    assert latency["approval_wait_minutes"]["merge"] == {"median": 4, "p90": 4}
    assert stats._distribution([1, 3, 9]) == {"median": 3, "p90": 7.800000000000001}


def test_hash_normalization():
    original = (
        "---\npaths: [src/a]\nverify: [{run: test x}]\nbudget: {max_cost_usd: 2}\n---\n Body\n"
    )
    queued = original.replace(
        "paths: [src/a]",
        "paths: [src/a, tests/a]\nintake: {related_tests_added: [tests/a]}\nstatus: queued\norigin: scout",
    )
    assert draft_acceptance_scope_hash(original) == draft_acceptance_scope_hash(
        queued.replace("\n", "\r\n")
    )
    assert draft_acceptance_scope_hash(original) != draft_acceptance_scope_hash(
        original.replace("test x", "test y")
    )
    with pytest.raises(ValueError):
        draft_acceptance_scope_hash("---\n[invalid\n---\n")


def test_grid_and_unavailable(ledger, tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "head_commit", lambda p: "head")
    config = {"routing": {"scout": {"focus_projects": [str(tmp_path)]}}}
    data = stats.build_stats(ledger, config=config, now=NOW)
    assert len(data["scouts"]) == len(LENSES)
    assert all(
        r["in_focus"]
        and r["scouts"] == 0
        and r["last_scout_ok"] is None
        and r["next_eligible_at"] is None
        for r in data["scouts"]
    )

    def unavailable(p):
        raise OSError("missing repository")

    monkeypatch.setattr(stats, "head_commit", unavailable)
    assert all(
        r["scheduling_error"] for r in stats.build_stats(ledger, config=config, now=NOW)["scouts"]
    )


def test_bulk_query_count(ledger, make_task):
    task = make_task()
    conn = ledger._get_conn()

    def populate(start, stop):
        for i in range(start, stop):
            seed_run(ledger, task.task_id, str(i), "landed", started_at=NOW - 100)
        with ledger._write() as db:
            db.executemany(
                "INSERT INTO run_events(run_id,ts,source,type,payload) VALUES (?,?,'pi','message',?)",
                [
                    (str(i), NOW - 90, json.dumps({"text": "ignored"}))
                    for i in range(start, stop)
                    for _ in range(25)
                ],
            )

    def count():
        queries = []
        conn.set_trace_callback(queries.append)
        data = build(ledger, since="all")
        conn.set_trace_callback(None)
        assert all("WHERE e.type IN" in q for q in queries if "FROM run_events" in q)
        return len(queries), data

    populate(0, 10)
    small, _ = count()
    populate(10, 2000)
    large, data = count()
    assert small == large
    assert data["runs"]["overall"]["landed"] == 2000


def test_scout_cohorts_hashes_and_weekly(ledger, make_task, tmp_project, monkeypatch):
    monkeypatch.setattr(stats, "head_commit", lambda p: "head")
    proj = str(tmp_project)
    config = {"routing": {"scout": {"focus_projects": [proj], "cooldown_hours": 12}}}

    def scout(sid, ts, outcome="succeeded", error=None, written=None):
        ledger.create_scout(
            {
                "id": sid,
                "project": proj,
                "model": "fake",
                "lens": "state",
                "head_commit": "head",
                "started_at": ts,
                "ended_at": ts + 60,
                "outcome": outcome,
                "error": error,
                "report_path": "unused",
                "drafts_written": written or [],
            }
        )

    tasks = [make_task(task_id="draft-" + str(i)) for i in range(7)]
    scout("current", NOW - 100, written=[t.task_id for t in tasks])
    base = "---\npaths: [src/a]\nverify: [{run: test x}]\n---\nBody"
    for i, task in enumerate(tasks):
        snapshot = base
        if i == 1:
            snapshot = base.replace("test x", "test y")
        if i == 2:
            snapshot = base.replace(
                "paths: [src/a]",
                "paths: [src/a, tests/a]\nintake: {related_tests_added: [tests/a]}",
            )
        ledger.set_task_snapshot(task.task_id, snapshot)
        ledger.create_scout_draft(
            task.task_id,
            "current",
            {},
            acceptance_scope_sha256=None if i == 3 else draft_acceptance_scope_hash(base),
        )
        if i < 4:
            ledger.set_task_status(task.task_id, "queued")
        elif i == 4:
            ledger.set_task_status(task.task_id, "draft")
        elif i == 5:
            seed_run(ledger, task.task_id, "failed-draft", "aborted", started_at=NOW - 50)
        else:
            seed_run(ledger, task.task_id, "landed-draft", "landed", started_at=NOW - 50)
    # make_task creates queued tasks, so explicitly distinguish never-queued draft.
    with ledger._write() as db:
        db.execute("UPDATE tasks SET queued_at=NULL WHERE task_id=?", (tasks[4].task_id,))
    data = stats.build_stats(ledger, config=config, now=NOW)
    row = next(r for r in data["scouts"] if r["lens"] == "state")
    assert row["queued_unchanged"] == 4  # unchanged, related-only, aborted and landed
    assert row["queued_changed"] == 1 and row["queued_unknown"] == 1
    assert (
        row["pending"] == 1
        and row["queued"] == 4
        and row["failed_drafts"] == 1
        and row["landed"] == 1
    )
    assert row["next_eligible_at"] == stats._iso(NOW - 40 + 12 * 3600)
    expired = stats.build_stats(ledger, config=config, now=NOW + 86400)
    assert (
        next(r for r in expired["scouts"] if r["lens"] == "state")["next_eligible_at"]
        == row["next_eligible_at"]
    )
    monkeypatch.setattr(stats, "head_commit", lambda p: "new-head")
    changed = stats.build_stats(ledger, config=config, now=NOW)
    assert next(r for r in changed["scouts"] if r["lens"] == "state")[
        "next_eligible_at"
    ] == stats._iso(NOW - 40)
    assert data["weekly"][-1]["drafts_landed"] == 1
    # Latest fields ignore the window and retain the failed scout error.
    scout("ancient", NOW - 40 * 86400, "failed", "broken")
    old = stats.build_stats(ledger, "24h", proj, config=config, now=NOW - 30 * 86400)
    oldrow = next(r for r in old["scouts"] if r["lens"] == "state")
    # Future rows are not used as latest at a historical clock in production;
    # use a distinct lens for an old-only cohort instead.
    assert oldrow["scouts"] == 0


def test_old_latest_scout(ledger, tmp_project, monkeypatch):
    monkeypatch.setattr(stats, "head_commit", lambda p: "head")
    ledger.create_scout(
        {
            "id": "old",
            "project": str(tmp_project),
            "lens": "docs",
            "model": "fake",
            "started_at": NOW - 40 * 86400,
            "ended_at": NOW - 40 * 86400 + 60,
            "outcome": "failed",
            "error": "broken",
            "report_path": "unused",
            "drafts_written": [],
        }
    )
    row = next(r for r in build(ledger)["scouts"] if r["lens"] == "docs")
    assert row["scouts"] == 0 and row["last_scout_ok"] is False
    assert row["last_scout_error"] == "broken" and row["last_scout_drafts"] == 0


def test_window_week_boundary(ledger, make_task):
    task = make_task()
    for rid, started in [("sunday", NOW - 86400), ("monday", NOW - 60), ("old", NOW - 40 * 86400)]:
        seed_run(ledger, task.task_id, rid, "landed", started_at=started)
    data = build(ledger)
    assert [(w["week"], w["landed"]) for w in data["weekly"]] == [("2026-W01", 1), ("2026-W02", 1)]
    assert build(ledger, since="all")["runs"]["overall"]["landed"] == 3
    assert build(ledger, since="24h")["runs"]["overall"]["landed"] == 2


def test_hash_column_migration(ledger, make_task, tmp_project):
    import sqlite3

    from helmraiser.ledger.db import Ledger

    task = make_task()
    ledger.create_scout(
        {"id": "s", "project": str(tmp_project), "model": "fake", "report_path": "unused"}
    )
    ledger.create_scout_draft(task.task_id, "s", {})
    db_path = ledger._get_conn().execute("PRAGMA database_list").fetchone()[2]
    ledger.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE scout_drafts DROP COLUMN acceptance_scope_sha256")
        conn.execute("PRAGMA user_version=10")
    with pytest.raises(ValueError):
        Ledger(db_path, read_only=True)
    with Ledger(db_path) as migrated:
        assert migrated.get_scout_draft(task.task_id)["acceptance_scope_sha256"] is None
        migrated.set_scout_draft_hash(task.task_id, "abc")
        assert migrated.stats_drafts()[0]["acceptance_scope_sha256"] == "abc"
