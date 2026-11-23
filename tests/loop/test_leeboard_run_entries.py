"""Run-authored entries are read exclusively through the Leeboard CLI."""

import json
import os
import subprocess
from dataclasses import FrozenInstanceError

import pytest

from helmraiser import leeboard


def test_leeboard_run_entries_order_identity_and_distinct_ids(fake_lb, monkeypatch):
    monkeypatch.setenv("LEEBOARD_HARNESS", "pi")
    monkeypatch.setenv("LEEBOARD_SESSION", "reviewer-session")
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
    result = leeboard.run_entries(iter(["first", "second", "first", "empty"]))
    assert list(result) == ["first", "second", "empty"]
    assert result["first"] == [
        leeboard.RunEntry("newest", "request", "global", "done", "Already answered"),
        leeboard.RunEntry("older", "rule", "project:/repo", "superseded", "Old rule"),
    ]
    assert result["second"] == [leeboard.RunEntry("second-entry", "", "", "", "")]
    assert result["empty"] == []
    with pytest.raises(FrozenInstanceError):
        result["first"][0].title = "changed"
    calls = fake_lb.calls()
    assert [call["argv"] for call in calls] == [["list", "--harness", "helmraiser", "--json"]]
    assert all(
        call["env"]
        == {
            "LEEBOARD_HOME": os.environ["LEEBOARD_HOME"],
            "LEEBOARD_HARNESS": "helmraiser",
            "LEEBOARD_SESSION": None,
        }
        for call in calls
    )


def test_leeboard_run_entries_malformed_items_and_titles(fake_lb):
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
    assert leeboard.run_entries(["run"])["run"] == [
        leeboard.RunEntry("missing-title", "", "", "", ""),
        leeboard.RunEntry("null-title", "", "", "", ""),
        leeboard.RunEntry("number-title", "", "", "", ""),
        leeboard.RunEntry("good", "", "", "", "Preserve\nraw title"),
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
def test_leeboard_run_entries_bulk_failure(fake_lb, response):
    fake_lb.write_script({"list": response})
    assert leeboard.run_entries(["first", "second", "third"]) == {
        "first": [],
        "second": [],
        "third": [],
    }
    assert len(fake_lb.calls()) == 1


def test_leeboard_run_entries_another_session_is_ignored(fake_lb):
    fake_lb.write_script(
        {
            "list": {"stdout": '[{"id":"not-ours","author":{"session":"other-session"}}]'},
        }
    )
    assert leeboard.run_entries(["our-session"]) == {"our-session": []}
    assert fake_lb.calls()[0]["argv"] == [
        "list",
        "--harness",
        "helmraiser",
        "--json",
    ]


def test_leeboard_run_entries_missing_binary_stops(fake_lb, monkeypatch):
    lookups = []

    def missing():
        lookups.append(True)
        return None

    monkeypatch.setattr(leeboard, "find_lb", missing)
    assert leeboard.run_entries(["first", "second"]) == {"first": [], "second": []}
    assert lookups == [True]
    assert fake_lb.calls() == []


def test_leeboard_run_entries_disappeared_binary_stops(fake_lb, monkeypatch):
    monkeypatch.setattr(leeboard, "find_lb", lambda: str(fake_lb.bin.with_name("missing")))
    calls = []
    real_run = subprocess.run

    def record(*args, **kwargs):
        calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(leeboard.subprocess, "run", record)
    assert leeboard.run_entries(["first", "second"]) == {"first": [], "second": []}
    assert len(calls) == 1
    assert fake_lb.calls() == []


def test_leeboard_run_entries_bulk_timeout(fake_lb, monkeypatch):
    fake_lb.write_script({"list": {"sleep": 5}})
    monkeypatch.setattr(leeboard, "LIST_TIMEOUT_S", 0.5)
    assert leeboard.run_entries(["first", "hung", "last"]) == {"first": [], "hung": [], "last": []}
    assert len(fake_lb.calls()) == 1


def test_leeboard_run_entries_oserror_and_subprocess_options(fake_lb, monkeypatch):
    calls = []

    def fail(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == str(fake_lb.bin)
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == leeboard.LIST_TIMEOUT_S
        assert kwargs["capture_output"] is True
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert kwargs["check"] is False
        raise PermissionError("not executable")

    monkeypatch.setattr(leeboard.subprocess, "run", fail)
    assert leeboard.run_entries(["first", "second"]) == {"first": [], "second": []}
    assert len(calls) == 1
    assert fake_lb.calls() == []


def test_leeboard_run_entries_many_runs_one_process(fake_lb):
    ids = [f"run-{i}" for i in range(60)]
    transport = [
        {"id": session, "author": {"session": session}}
        for session in [ids[-1], "unrelated", ids[0], ids[30]]
    ]
    fake_lb.write_script({"list": {"stdout": json.dumps(transport)}})
    expected = {run_id: [] for run_id in ids}
    for session in (ids[-1], ids[0], ids[30]):
        expected[session] = [leeboard.RunEntry(session, "", "", "", "")]
    assert leeboard.run_entries(ids) == expected
    assert [call["argv"] for call in fake_lb.calls()] == [
        ["list", "--harness", "helmraiser", "--json"]
    ]


@pytest.mark.parametrize("error", [OSError, ValueError])
def test_leeboard_run_entries_lookup_failure(fake_lb, monkeypatch, error):
    def fail():
        raise error("lookup failed")

    def unexpected(*args, **kwargs):
        pytest.fail("lookup failure must not start a process")

    monkeypatch.setattr(leeboard, "find_lb", fail)
    monkeypatch.setattr(leeboard.subprocess, "run", unexpected)
    assert leeboard.run_entries(["first", "second"]) == {"first": [], "second": []}
    assert fake_lb.calls() == []


def test_leeboard_run_entries_empty_input_does_not_find_lb(monkeypatch):
    def unexpected():
        pytest.fail("empty reports must not resolve lb")

    monkeypatch.setattr(leeboard, "find_lb", unexpected)
    assert leeboard.run_entries([]) == {}
