"""Run-authored entries are read exclusively through the Logbook CLI."""

import json
import os
import subprocess
from dataclasses import FrozenInstanceError

import pytest

from helmraiser import logbook


def test_logbook_run_entries_order_identity_and_distinct_ids(fake_lb, monkeypatch):
    monkeypatch.setenv("LOGBOOK_HARNESS", "pi")
    monkeypatch.setenv("LOGBOOK_SESSION", "reviewer-session")
    fake_lb.write_script(
        {
            "list": {"stdout": "[]"},
            "list@first": {
                "stdout": json.dumps(
                    [
                        {
                            "id": "newest",
                            "kind": "request",
                            "scope": "global",
                            "status": "done",
                            "title": "Already answered",
                            "author": {"harness": "helmraiser", "session": "first"},
                        },
                        {
                            "id": "older",
                            "kind": "rule",
                            "scope": "project:/repo",
                            "status": "superseded",
                            "title": "Old rule",
                        },
                    ]
                )
            },
            "list@second": {"stdout": '[{"id":"second-entry"}]'},
        }
    )
    result = logbook.run_entries(iter(["first", "second", "first", "empty"]))
    assert list(result) == ["first", "second", "empty"]
    assert result["first"] == [
        logbook.RunEntry("newest", "request", "global", "done", "Already answered"),
        logbook.RunEntry("older", "rule", "project:/repo", "superseded", "Old rule"),
    ]
    assert result["second"] == [logbook.RunEntry("second-entry", "", "", "", "")]
    assert result["empty"] == []
    with pytest.raises(FrozenInstanceError):
        result["first"][0].title = "changed"
    calls = fake_lb.calls()
    assert [call["argv"] for call in calls] == [
        ["list", "--session", run_id, "--harness", "helmraiser", "--json"] for run_id in result
    ]
    assert all(
        call["env"]
        == {
            "LOGBOOK_HOME": os.environ["LOGBOOK_HOME"],
            "LOGBOOK_HARNESS": "helmraiser",
            "LOGBOOK_SESSION": None,
        }
        for call in calls
    )


def test_logbook_run_entries_malformed_items_and_titles(fake_lb):
    fake_lb.write_script(
        {
            "list": {
                "stdout": json.dumps(
                    [
                        None,
                        17,
                        "not an object",
                        {},
                        {"id": 5},
                        {"id": "missing-title"},
                        {"id": "null-title", "title": None},
                        {"id": "number-title", "title": 7, "kind": [], "scope": False},
                        {"id": "good", "title": "Preserve\nraw title"},
                    ]
                )
            }
        }
    )
    assert logbook.run_entries(["run"])["run"] == [
        logbook.RunEntry("missing-title", "", "", "", ""),
        logbook.RunEntry("null-title", "", "", "", ""),
        logbook.RunEntry("number-title", "", "", "", ""),
        logbook.RunEntry("good", "", "", "", "Preserve\nraw title"),
    ]


@pytest.mark.parametrize(
    "response",
    [
        {"stdout": "[]"},
        {"exit": 1, "stdout": '[{"id":"ignore-on-error"}]', "stderr": "lb: broken"},
        {"stdout": "not JSON"},
        {"stdout": ""},
        {"stdout": "{}"},
        {"stdout": "null"},
        {"stdout": '"text"'},
    ],
)
def test_logbook_run_entries_failure_is_local(fake_lb, response):
    fake_lb.write_script(
        {
            "list@bad": response,
            "list@good": {"stdout": '[{"id":"found"}]'},
        }
    )
    result = logbook.run_entries(["bad", "good"])
    assert result["bad"] == []
    assert [entry.id for entry in result["good"]] == ["found"]
    assert len(fake_lb.calls()) == 2


def test_logbook_run_entries_another_session_is_not_queried(fake_lb):
    fake_lb.write_script(
        {
            "list": {"stdout": "[]"},
            "list@other-session": {"stdout": '[{"id":"not-ours"}]'},
        }
    )
    assert logbook.run_entries(["our-session"]) == {"our-session": []}
    assert fake_lb.calls()[0]["argv"] == [
        "list",
        "--session",
        "our-session",
        "--harness",
        "helmraiser",
        "--json",
    ]


def test_logbook_run_entries_missing_binary_stops(fake_lb, monkeypatch):
    lookups = []

    def missing():
        lookups.append(True)
        return None

    monkeypatch.setattr(logbook, "find_lb", missing)
    assert logbook.run_entries(["first", "second"]) == {"first": [], "second": []}
    assert lookups == [True]
    assert fake_lb.calls() == []


def test_logbook_run_entries_disappeared_binary_stops(fake_lb, monkeypatch):
    monkeypatch.setattr(logbook, "find_lb", lambda: str(fake_lb.bin.with_name("missing")))
    calls = []
    real_run = subprocess.run

    def record(*args, **kwargs):
        calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(logbook.subprocess, "run", record)
    assert logbook.run_entries(["first", "second"]) == {"first": [], "second": []}
    assert len(calls) == 1
    assert fake_lb.calls() == []


def test_logbook_run_entries_timeout_stops_remaining_calls(fake_lb, monkeypatch):
    fake_lb.write_script(
        {
            "list@first": {"stdout": '[{"id":"before-timeout"}]'},
            "list@hung": {"sleep": 5},
            "list@last": {"stdout": '[{"id":"never-read"}]'},
        }
    )
    monkeypatch.setattr(logbook, "LIST_TIMEOUT_S", 0.5)
    result = logbook.run_entries(["first", "hung", "last"])
    assert [entry.id for entry in result["first"]] == ["before-timeout"]
    assert result["hung"] == result["last"] == []
    assert [call["argv"][2] for call in fake_lb.calls()] == ["first", "hung"]


def test_logbook_run_entries_oserror_and_subprocess_options(fake_lb, monkeypatch):
    real_run = subprocess.run
    calls = []

    def fail_first(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == str(fake_lb.bin)
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == logbook.LIST_TIMEOUT_S
        if len(calls) == 1:
            raise PermissionError("not executable")
        return real_run(argv, **kwargs)

    fake_lb.write_script({"list": {"stdout": '[{"id":"success"}]'}})
    monkeypatch.setattr(logbook.subprocess, "run", fail_first)
    result = logbook.run_entries(["first", "second"])
    assert result["first"] == []
    assert result["second"][0].id == "success"
    assert len(calls) == 2
    assert len(fake_lb.calls()) == 1


def test_logbook_run_entries_empty_input_does_not_find_lb(monkeypatch):
    def unexpected():
        pytest.fail("empty reports must not resolve lb")

    monkeypatch.setattr(logbook, "find_lb", unexpected)
    assert logbook.run_entries([]) == {}
