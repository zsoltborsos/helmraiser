"""Live Logbook entries accompany all three report sections, including CLI JSON."""

import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from helmraiser import logbook
from helmraiser.cli import app, monitor
from helmraiser.ledger.rundir import RunDir
from helmraiser.loop import report as report_module
from helmraiser.loop.report import build_report, render_report
from tests.cli.test_status_report_log import seed_run


@pytest.fixture
def report_runs(ledger, make_task):
    task = make_task()
    now = time.time()

    def add(run_id, status="landed", *, age=60):
        return seed_run(
            ledger,
            task.task_id,
            run_id,
            status,
            started_at=now - age,
            ended_at=None if status in ("running", "awaiting_approval") else now - age,
            verdict="pass" if status == "landed" else "concerns",
        )

    return add


def lb_entry(entry_id, *, title="A lesson", status="active"):
    return {
        "id": entry_id,
        "kind": "handoff",
        "scope": "global",
        "status": status,
        "title": title,
    }


def test_logbook_report_only_queries_selected_runs_once(
    ledger, data_dir, config, report_runs, fake_lb, monkeypatch
):
    report_runs("landed")
    report_runs("awaiting", "awaiting_approval", age=10 * 86400)
    report_runs("failed", "failed")
    report_runs("aborted", "aborted")
    report_runs("empty", "landed")
    report_runs("running", "running")
    report_runs("old-landed", age=10 * 86400)
    report_runs("old-failed", "failed", age=10 * 86400)
    fake_lb.write_script(
        {
            "list": {
                "stdout": json.dumps(
                    [
                        {**lb_entry(run_id + "-entry"), "author": {"session": run_id}}
                        for run_id in (
                            "landed",
                            "awaiting",
                            "failed",
                            "aborted",
                            "running",
                            "old-landed",
                        )
                    ]
                )
            },
        }
    )
    lookups = []
    real_lookup = logbook.run_entries

    def record(ids):
        lookups.append(list(ids))
        return real_lookup(lookups[-1])

    monkeypatch.setattr(report_module, "run_entries", record)
    result = build_report(ledger, data_dir, 86400, config=config)
    included = result["landed"] + result["awaiting"] + result["failed"]
    ids = [entry["run_id"] for entry in included]
    assert set(ids) == {"landed", "awaiting", "failed", "aborted", "empty"}
    assert lookups == [ids]
    assert [call["argv"] for call in fake_lb.calls()] == [
        ["list", "--harness", "helmraiser", "--json"]
    ]
    for entry in included:
        assert entry["logbook_entries"] == (
            [] if entry["run_id"] == "empty" else [lb_entry(entry["run_id"] + "-entry")]
        )
        assert "logbook_more" not in entry
    assert all(call["env"]["LOGBOOK_SESSION"] is None for call in fake_lb.calls())


@pytest.mark.parametrize(
    "status,section",
    [("landed", "landed"), ("awaiting_approval", "awaiting"), ("failed", "failed")],
)
def test_logbook_report_text_and_cli_json(
    ledger, data_dir, config, report_runs, fake_lb, monkeypatch, status, section
):
    report_runs("with-entries", status)
    report_runs("without-entries", status)
    entries = [
        lb_entry("newest", title="new\n\t  lesson", status="released"),
        lb_entry("older", title="x" * 500, status="superseded"),
        lb_entry("untitled", title=" \n\t"),
    ]
    fake_lb.write_script(
        {
            "list": {
                "stdout": json.dumps(
                    [{**entry, "author": {"session": "with-entries"}} for entry in entries]
                )
            },
        }
    )
    monkeypatch.setattr(monitor, "load_config", lambda: config)
    runner = CliRunner()
    result = runner.invoke(app, ["report", "--data-dir", str(data_dir), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    rows = {row["run_id"]: row for row in payload[section]}
    assert rows["with-entries"]["logbook_entries"] == entries
    assert rows["without-entries"]["logbook_entries"] == []
    assert "logbook_more" not in rows["with-entries"]

    result = runner.invoke(app, ["report", "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.output
    text = result.output
    assert text.count("### Logbook entries") == 1
    assert text.count("- logbook entries: 3") == 1
    assert "- [newest] handoff released global: new lesson\n" in text
    assert "- [older] handoff superseded global: " + "x" * 199 + "…\n" in text
    assert "- [untitled] handoff active global: (untitled)\n" in text
    assert "x" * 200 not in text
    assert text.index("[newest]") < text.index("[older]") < text.index("[untitled]")
    assert len(fake_lb.calls()) == 2  # No cache across report invocations.


def test_logbook_report_caps_entries_in_lb_order(ledger, data_dir, config, report_runs, fake_lb):
    report_runs("many")
    entries = [lb_entry(f"entry-{i}") for i in range(25, 0, -1)]
    fake_lb.write_script(
        {
            "list": {
                "stdout": json.dumps(
                    [{**entry, "author": {"session": "many"}} for entry in entries]
                )
            }
        }
    )
    result = build_report(ledger, data_dir, 86400, config=config)
    run = result["landed"][0]
    assert run["logbook_entries"] == entries[:20]
    assert run["logbook_more"] == 5
    text = render_report(result, "24h")
    assert "- logbook entries: 25\n" in text
    assert "… 5 more — lb list" in text
    assert text.count("- [entry-") == 20
    assert "[entry-5]" not in text
    assert text.index("[entry-25]") < text.index("[entry-6]")
    assert len(fake_lb.calls()) == 1


def test_logbook_report_uses_cli_not_store_and_refreshes_status(
    ledger, data_dir, config, report_runs, fake_lb
):
    report_runs("run")
    store_entry = lb_entry("from-file", title="Do not show this")
    store_entry["author"] = {"harness": "helmraiser", "session": "run"}
    store = Path(os.environ["LOGBOOK_HOME"]) / "log.jsonl"
    store.write_text(json.dumps(store_entry) + "\n")
    summary = RunDir("run", data_dir).create().summary_md
    summary.write_text("## Existing summary\n\n### Review: pass\n\nExisting detail\n")
    original_summary = summary.read_text()
    cli_entry = lb_entry("from-cli", status="active")
    fake_lb.write_script(
        {"list": {"stdout": json.dumps([{**cli_entry, "author": {"session": "run"}}])}}
    )
    result = build_report(ledger, data_dir, 86400, config=config)
    assert result["landed"][0]["logbook_entries"] == [cli_entry]
    assert "from-file" not in render_report(result, "24h")
    cli_entry["status"] = "released"
    fake_lb.write_script(
        {"list": {"stdout": json.dumps([{**cli_entry, "author": {"session": "run"}}])}}
    )
    refreshed = build_report(ledger, data_dir, 86400, config=config)
    text = render_report(refreshed, "24h")
    assert "[from-cli] handoff released global" in text
    assert "### Review: pass" in text
    assert summary.read_text() == original_summary
    assert len(fake_lb.calls()) == 2


def test_logbook_report_missing_lb_is_indistinguishable_from_no_entries(
    ledger, data_dir, config, report_runs, fake_lb, monkeypatch
):
    for status in ("landed", "awaiting_approval", "failed"):
        report_runs(status, status)
    fake_lb.write_script({"list": {"stdout": "[]"}})
    now = time.time()
    empty = build_report(ledger, data_dir, 86400, now=now, config=config)
    assert len(fake_lb.calls()) == 1
    monkeypatch.setattr(logbook, "find_lb", lambda: None)
    missing = build_report(ledger, data_dir, 86400, now=now, config=config)
    assert missing == empty
    text = render_report(missing, "24h")
    assert "### Logbook entries" not in text
    assert "- logbook entries:" not in text
    assert len(fake_lb.calls()) == 1


def test_logbook_report_deeply_nested_json_fails_soft(
    ledger, data_dir, config, report_runs, fake_lb
):
    report_runs("deep-json")
    report_runs("healthy", "awaiting_approval")
    fake_lb.write_script({"list": {"stdout": "[" * 100000}})
    result = build_report(ledger, data_dir, 86400, config=config)
    assert result["landed"][0]["logbook_entries"] == []
    assert result["awaiting"][0]["logbook_entries"] == []
    text = render_report(result, "24h")
    assert "- run: `deep-json`" in text
    assert "- run: `healthy`" in text
    assert "### Logbook entries" not in text
    assert [call["argv"] for call in fake_lb.calls()] == [
        ["list", "--harness", "helmraiser", "--json"]
    ]


def test_logbook_report_empty_window_has_no_calls(ledger, data_dir, config, fake_lb):
    result = build_report(ledger, data_dir, 86400, config=config)
    assert result["landed"] == result["awaiting"] == result["failed"] == []
    assert "Logbook" not in render_report(result, "24h")
    assert fake_lb.calls() == []
