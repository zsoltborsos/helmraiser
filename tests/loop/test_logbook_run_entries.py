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
            "list": {
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
                        {"id": "second-entry", "author": {"session": "second"}},
                        {
                            "id": "older",
                            "author": {"session": "first"},
                            "kind": "rule",
                            "scope": "project:/repo",
                            "status": "superseded",
                            "title": "Old rule",
                        },
                    ]
                )
            },
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
    assert [call["argv"] for call in calls] == [["list", "--harness", "helmraiser", "--json"]]
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
                        *[
                            {"id": "ignored", "author": author}
                            for author in (
                                None,
                                [],
                                "run",
                                {},
                                {"session": None},
                                {"session": 5},
                                {"session": []},
                                {"session": "other"},
                            )
                        ],
                        {"id": "no-author"},
                        {"id": "missing-title", "author": {"session": "run"}},
                        {"id": "null-title", "title": None, "author": {"session": "run"}},
                        {
                            "id": "number-title",
                            "title": 7,
                            "kind": [],
                            "scope": False,
                            "author": {"session": "run"},
                        },
                        {
                            "id": "good",
                            "title": "Preserve\nraw title",
                            "author": {"session": "run"},
                        },
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
def test_logbook_run_entries_bulk_failure(fake_lb, response):
    fake_lb.write_script({"list": response})
    assert logbook.run_entries(["first", "second", "third"]) == {
        "first": [],
        "second": [],
        "third": [],
    }
    assert len(fake_lb.calls()) == 1


def test_logbook_run_entries_another_session_is_ignored(fake_lb):
    fake_lb.write_script(
        {
            "list": {"stdout": '[{"id":"not-ours","author":{"session":"other-session"}}]'},
        }
    )
    assert logbook.run_entries(["our-session"]) == {"our-session": []}
    assert fake_lb.calls()[0]["argv"] == [
        "list",
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


def test_logbook_run_entries_bulk_timeout(fake_lb, monkeypatch):
    fake_lb.write_script({"list": {"sleep": 5}})
    monkeypatch.setattr(logbook, "LIST_TIMEOUT_S", 0.5)
    assert logbook.run_entries(["first", "hung", "last"]) == {"first": [], "hung": [], "last": []}
    assert len(fake_lb.calls()) == 1


def test_logbook_run_entries_oserror_and_subprocess_options(fake_lb, monkeypatch):
    calls = []

    def fail(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == str(fake_lb.bin)
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == logbook.LIST_TIMEOUT_S
        assert kwargs["capture_output"] is True
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert kwargs["check"] is False
        raise PermissionError("not executable")

    monkeypatch.setattr(logbook.subprocess, "run", fail)
    assert logbook.run_entries(["first", "second"]) == {"first": [], "second": []}
    assert len(calls) == 1
    assert fake_lb.calls() == []


def test_logbook_run_entries_many_runs_one_process(fake_lb):
    ids = [f"run-{i}" for i in range(60)]
    transport = [
        {"id": session, "author": {"session": session}}
        for session in [ids[-1], "unrelated", ids[0], ids[30]]
    ]
    fake_lb.write_script({"list": {"stdout": json.dumps(transport)}})
    expected = {run_id: [] for run_id in ids}
    for session in (ids[-1], ids[0], ids[30]):
        expected[session] = [logbook.RunEntry(session, "", "", "", "")]
    assert logbook.run_entries(ids) == expected
    assert [call["argv"] for call in fake_lb.calls()] == [
        ["list", "--harness", "helmraiser", "--json"]
    ]


@pytest.mark.parametrize("error", [OSError, ValueError])
def test_logbook_run_entries_lookup_failure(fake_lb, monkeypatch, error):
    def fail():
        raise error("lookup failed")

    def unexpected(*args, **kwargs):
        pytest.fail("lookup failure must not start a process")

    monkeypatch.setattr(logbook, "find_lb", fail)
    monkeypatch.setattr(logbook.subprocess, "run", unexpected)
    assert logbook.run_entries(["first", "second"]) == {"first": [], "second": []}
    assert fake_lb.calls() == []


def test_logbook_run_entries_empty_input_does_not_find_lb(monkeypatch):
    def unexpected():
        pytest.fail("empty reports must not resolve lb")

    monkeypatch.setattr(logbook, "find_lb", unexpected)
    assert logbook.run_entries([]) == {}
