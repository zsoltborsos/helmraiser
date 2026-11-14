"""Bounded, fail-soft Logbook briefs and implementation-only rendering."""

import os
import subprocess
from unittest.mock import patch

import pytest

from helmraiser.logbook import BRIEF_MAX_CHARS, BriefResult, fetch_brief
from helmraiser.pi.directives import (
    render_implement_directives,
    render_plan_directives,
    render_review_directives,
    render_scout_directives,
)


def implement(**kwargs):
    return render_implement_directives(
        run_id="3f2c1a9e-6b7d-4c1e-9a2b-1f0e8d7c6b5a",
        title="Remove heartbeat prompt injection from AgentRunner",
        worktree="/srv/projects/example/.helmraiser/worktrees/rm-heartbeat-injection",
        task_path="/home/user/.helmraiser/runs/3f2c1a9e-6b7d-4c1e-9a2b-1f0e8d7c6b5a/task.md",
        task_id="rm-heartbeat-injection",
        scout_model="opencode-go/deepseek-v4.1-flash:high",
        max_iterations=4,
        max_minutes=90,
        max_cost_usd=5.0,
        iteration=1,
        **kwargs,
    )


@pytest.mark.parametrize("brief", [None, BriefResult(None, None)])
def test_logbook_renderer_no_context(brief):
    assert implement(logbook_brief=brief) == implement()
    assert "## Logbook" not in implement(logbook_brief=brief)


def test_logbook_renderer_context_and_precedence():
    brief = "# Logbook: project squad\n\n## Rules\n- [rule] Keep {braces}"
    text = implement(
        logbook_brief=BriefResult(brief, None),
        project_notes="Use local tests.",
        service_vars=("DATABASE_URL",),
        has_plan_approval_note=True,
    )
    assert text.count(brief) == 1
    assert text.index("## Project notes") < text.index("## Logbook") < text.index(brief)
    assert "generated the brief below for this run" in text
    assert "advisory context written by the owner and other agents" in text
    assert "the task file and these directives win on conflict" in text
    assert "skip any other brief or recall step" in text
    assert "do not run `lb brief` again" in text
    assert "Services: only the verify and evidence steps get DATABASE_URL" in text
    assert "owner's note on approval" in text
    assert "Use local tests." in text


@pytest.mark.parametrize(
    "note", ["lb not found on PATH", "lb brief exited 1: lb: error", "lb brief timed out after 15s"]
)
def test_logbook_renderer_unavailable(note):
    text = implement(logbook_brief=BriefResult(None, note))
    assert (
        text == implement() + f"\nLogbook is unavailable ({note}): skip brief and recall steps.\n"
    )
    assert "## Logbook" not in text


def test_logbook_renderer_budget():
    assert len(implement().encode("utf-8")) < 1536
    assert len(implement(logbook_brief=BriefResult("x" * 8000, None)).encode()) > 8000


def test_logbook_other_stages_unchanged():
    args = {"run_id": "r", "title": "Task", "worktree": "/tmp/worktree"}
    for text in (
        render_review_directives(**args),
        render_plan_directives(**args, task_id="task", wait_for_approval=False),
        render_scout_directives(
            scout_id="s", project="/tmp/project", worktree="/tmp/worktree", exclusions=()
        ),
    ):
        assert "Logbook" not in text
        assert "lb brief" not in text


def test_logbook_fetch_identity_and_argv(fake_lb, tmp_path, monkeypatch):
    monkeypatch.setenv("LOGBOOK_HARNESS", "pi")
    monkeypatch.setenv("LOGBOOK_SESSION", "parent")
    fake_lb.write_script({"brief": {"stdout": "# Logbook\n\n  content {x}  \n\t"}})
    assert fetch_brief(tmp_path, "run-1") == BriefResult("# Logbook\n\n  content {x}", None)
    assert fake_lb.calls() == [
        {
            "argv": ["brief", "--cwd", str(tmp_path)],
            "cwd": os.getcwd(),
            "env": {
                "LOGBOOK_HOME": os.environ["LOGBOOK_HOME"],
                "LOGBOOK_HARNESS": "helmraiser",
                "LOGBOOK_SESSION": "run-1",
            },
        }
    ]
    assert os.environ["LOGBOOK_HARNESS"] == "pi"
    assert os.environ["LOGBOOK_SESSION"] == "parent"


@pytest.mark.parametrize("stdout", ["", " \n\t"])
def test_logbook_empty_success(fake_lb, tmp_path, stdout):
    fake_lb.write_script({"brief": {"stdout": stdout}})
    assert fetch_brief(tmp_path, "r") == BriefResult("", None)


def test_logbook_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("helmraiser.logbook.find_lb", lambda: None)
    assert fetch_brief(tmp_path, "r") == BriefResult(None, None)


@pytest.mark.parametrize(
    ("stderr", "note"),
    [
        ("\n  lb: bad store\nsecret second line", "lb brief exited 1: lb: bad store"),
        ("", "lb brief exited 1"),
    ],
)
def test_logbook_nonzero(fake_lb, tmp_path, stderr, note):
    fake_lb.write_script({"brief": {"exit": 1, "stderr": stderr, "stdout": "ignored"}})
    assert fetch_brief(tmp_path, "r") == BriefResult(None, note)


def test_logbook_timeout(fake_lb, tmp_path, monkeypatch):
    fake_lb.write_script({"brief": {"sleep": 1}})
    monkeypatch.setattr("helmraiser.logbook.BRIEF_TIMEOUT_S", 0.05)
    assert fetch_brief(tmp_path, "r") == BriefResult(None, "lb brief timed out after 0.05s")


def test_logbook_oserror(tmp_path, monkeypatch):
    monkeypatch.setattr("helmraiser.logbook.find_lb", lambda: str(tmp_path / "missing"))
    result = fetch_brief(tmp_path, "r")
    assert result.text is None
    assert "No such file" in result.note
    assert "\n" not in result.note


def test_logbook_discovery_error(tmp_path, monkeypatch):
    def fail():
        raise RuntimeError("discovery failed\nsecond line")

    monkeypatch.setattr("helmraiser.logbook.find_lb", fail)
    assert fetch_brief(tmp_path, "r") == BriefResult(None, "lb brief failed: discovery failed")


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("x" * BRIEF_MAX_CHARS, "x" * BRIEF_MAX_CHARS),
        ("規則\n" * 3000, "規則\n" * 2660 + "… brief truncated"),
        ("x" * (BRIEF_MAX_CHARS + 1), "… brief truncated"),
    ],
)
def test_logbook_bounds(fake_lb, tmp_path, stdout, expected):
    fake_lb.write_script({"brief": {"stdout": stdout}})
    result = fetch_brief(tmp_path, "r")
    assert result == BriefResult(expected, None)
    assert len(result.text) <= BRIEF_MAX_CHARS


def test_logbook_subprocess_contract(tmp_path):
    with (
        patch("helmraiser.logbook.find_lb", return_value="/fake/lb"),
        patch(
            "helmraiser.logbook.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "brief", ""),
        ) as run,
    ):
        assert fetch_brief(tmp_path, "r") == BriefResult("brief", None)
    run.assert_called_once_with(
        ["/fake/lb", "brief", "--cwd", str(tmp_path)],
        env={**os.environ, "LOGBOOK_HARNESS": "helmraiser", "LOGBOOK_SESSION": "r"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=15.0,
        shell=False,
        check=False,
    )
