"""Main flows with Leeboard genuinely absent from PATH."""

import json
import os
import shutil
from pathlib import Path

import pytest

from helmraiser import leeboard
from helmraiser.cli import commands, supervisor
from helmraiser.ledger.db import Ledger, ProjectScope
from helmraiser.ledger.rundir import RunDir
from helmraiser.loop.scout import collect_scout_inputs
from helmraiser.pi.directives import render_scout_directives
from tests.cli.test_approve_reject import REVIEW_PASS, TASK, invoke
from tests.cli.test_doctor import ready_checks  # noqa: F401
from tests.cli.test_land import GH
from tests.cli.test_supervisor import Harness, events, queue_real
from tests.conftest import configured_test_config
from tests.loop.test_reconcile import FakeController
from tests.pi.test_leeboard_directives import implement
from tests.pipeline.test_worktree import git


@pytest.fixture
def cli_env(no_lb, monkeypatch, tmp_path):
    config = configured_test_config()
    monkeypatch.setattr(commands, "load_config", lambda: config)
    monkeypatch.setattr(commands, "get_intake_adapter", lambda config: None)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("COLUMNS", "200")


def assert_no_warning(text):
    lowered = text.lower()
    for phrase in ("leeboard: hold check skipped", "leeboard is unavailable", "leeboard hold"):
        assert phrase not in lowered


def assert_unused(fake_lb):
    assert leeboard.find_lb() is None
    assert list(leeboard.leeboard_home().iterdir()) == []
    assert fake_lb.calls() == []


def checked(*args, data_dir):
    result = invoke(*args, data_dir=data_dir)
    assert result.exit_code == 0, result.output
    assert_no_warning(result.output)
    return result


def run_task(project, data, fake_pi, delivery):
    task_path = project.parent / "task.md"
    task_path.write_text(
        TASK.format(
            project=project, policy="review", delivery=delivery, task_id="fix", marker="done.txt"
        )
    )
    # Inspect intake itself, not just the subsequent work result.
    intake = checked("intake", str(task_path), "--yes", "--json", data_dir=data)
    json.loads(intake.output)
    ledger = Ledger(data / "helmraiser.db")
    assert [task["status"] for task in ledger.list_tasks()] == ["queued"]
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {"text": "Done.", "cost": 0.05, "files": [["done.txt", "ok\n"]]},
                "*-review": {"text": REVIEW_PASS, "cost": 0.02},
            }
        }
    )
    result = checked("work", "--once", "--json", data_dir=data)
    outcome = json.loads(result.output.splitlines()[0])
    assert outcome["status"] == "awaiting_approval"
    run_id = outcome["run_id"]
    assert ledger.pending_approval(run_id)["kind"] == "merge"
    directives = RunDir(run_id, data).directives_md
    assert_no_leeboard_context(directives.read_text())
    implement_calls = [call for call in fake_pi.calls() if str(directives) in call["argv"]]
    assert len(implement_calls) == 1
    assert "--append-system-prompt" in implement_calls[0]["argv"]
    for event in ledger.events(run_id):
        assert_no_warning(json.dumps(event))
        assert "leeboard_hold" not in json.dumps(event)
    return outcome


def assert_report(data, run_id, lane):
    human = checked("report", data_dir=data)
    assert "Leeboard" not in human.output
    report = json.loads(checked("report", "--json", data_dir=data).output)
    row = next(row for row in report[lane] if row["run_id"] == run_id)
    assert row["leeboard_entries"] == []


def test_intake_work_approve_and_monitor_without_leeboard(
    cli_env, tmp_project, tmp_path, fake_pi, fake_lb
):
    data = tmp_path / "data"
    outcome = run_task(tmp_project, data, fake_pi, "merge")
    run_id = outcome["run_id"]
    ledger = Ledger(data / "helmraiser.db")
    assert_report(data, run_id, "awaiting")
    status = json.loads(checked("status", "--json", data_dir=data).output)
    assert status["awaiting"][0]["run_id"] == run_id
    checked("approve", run_id, "--json", data_dir=data)
    assert ledger.get_run(run_id)["status"] == "landed"
    assert ledger.get_task(outcome["task_id"])["status"] == "landed"
    assert (tmp_project / "done.txt").read_text() == "ok\n"
    assert_report(data, run_id, "landed")
    human = checked("doctor", data_dir=data)
    assert "leeboard" in human.output and "info" in human.output
    assert "not installed (optional)" in " ".join(human.output.split())
    doctor = json.loads(checked("doctor", "--json", data_dir=data).output)
    assert doctor["leeboard"] == {"lb": None, "home": str(leeboard.leeboard_home()), "exists": True}
    assert_unused(fake_lb)


def test_pr_land_without_leeboard(cli_env, tmp_project, tmp_path, fake_pi, fake_lb, monkeypatch):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(tmp_project, "remote", "add", "origin", str(origin))
    git(tmp_project, "push", "origin", "main")
    bin_dir = tmp_path / "gh-bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(GH)
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    for name, filename, content in (
        ("LAND_CHECKS", "checks", "0|passed\n"),
        ("LAND_STATE", "state", "auto"),
        ("LAND_HEADS", "heads", ""),
        ("LAND_VISIBLE_HEAD", "visible-head", ""),
    ):
        path = tmp_path / filename
        path.write_text(content)
        monkeypatch.setenv(name, str(path))
    checks_by_head = tmp_path / "checks-by-head"
    checks_by_head.mkdir()
    gh_log = tmp_path / "gh.log"
    monkeypatch.setenv("LAND_CHECKS_BY_HEAD", str(checks_by_head))
    monkeypatch.setenv("LAND_GH_LOG", str(gh_log))
    monkeypatch.setenv("LAND_ORIGIN", str(origin))
    data = tmp_path / "data"
    outcome = run_task(tmp_project, data, fake_pi, "pr")
    monkeypatch.setenv("LAND_BRANCH", outcome["branch_name"])
    # Leave the visible-head file absent until gh's first head query.
    (tmp_path / "visible-head").unlink()
    result = checked(
        "land", outcome["run_id"], "--json", "--interval", "0", "--timeout", "30", data_dir=data
    )
    payload = json.loads(result.output)
    assert payload["landed"] and payload["pr_url"] == "https://example.test/pr/1"
    assert all(step["status"] != "warn" for step in payload["steps"])
    assert (
        git(tmp_project, "rev-parse", "main") == git(origin, "rev-parse", "main") == payload["head"]
    )
    calls = gh_log.read_text().splitlines()
    assert any(call.startswith("pr create") for call in calls)
    assert any(call.startswith("pr checks") for call in calls)
    assert any(call.startswith("pr view") and "state" in call for call in calls)
    assert_unused(fake_lb)


def assert_no_leeboard_context(text):
    for phrase in ("Leeboard", "lb brief", "lb handoff", "lb request", "lb add rule"):
        assert phrase not in text


def test_worker_admits_without_leeboard(
    cli_env, tmp_project, tmp_path, fake_lb, monkeypatch, capsys
):
    data = tmp_path / "data"
    ledger = Ledger(data / "helmraiser.db")
    task_id = queue_real(ledger, tmp_project, "fix")
    harness = Harness(ledger)
    monkeypatch.setattr(supervisor, "_read_child_stat", lambda pid: ("S", str(pid)))

    def make_supervisor(**options):
        harness.run_config = options["run_config"]
        options.update(
            spawn=harness.spawn,
            clock=harness.clock,
            sleep=harness.sleep,
            controller=FakeController(),
        )
        return supervisor.Supervisor(**options)

    monkeypatch.setattr(commands, "Supervisor", make_supervisor)
    commands._supervise(
        ledger,
        configured_test_config(),
        data,
        ProjectScope.of(),
        watch=False,
        max_parallel_runs=1,
        max_runs=1,
        no_caps=False,
        notify=False,
        as_json=True,
        data_dir=data,
    )
    output = capsys.readouterr().out
    assert_no_warning(output)
    emitted = events(output)
    assert emitted
    assert all("warning" not in event and "skipped" not in event for event in emitted)
    assert "leeboard_hold" not in output
    assert len(harness.spawned) == 1
    assert len(harness.children) == 1 and harness.children[0].code == 0
    assert ledger.get_task(task_id)["status"] == "landed"
    assert_unused(fake_lb)


def test_scout_context_without_leeboard(no_lb, tmp_project, tmp_path, fake_lb, capsys):
    ledger = Ledger(tmp_path / "data" / "helmraiser.db")
    inputs = collect_scout_inputs(tmp_project, ledger, ())
    assert inputs["leeboard"] is None
    text = render_scout_directives(
        scout_id="s", project=str(tmp_project), worktree=str(tmp_project), exclusions=()
    )
    assert_no_leeboard_context(text)
    assert capsys.readouterr().out == ""
    assert_unused(fake_lb)


def test_absent_brief_has_no_directive_context(no_lb, fake_lb, tmp_project):
    brief = leeboard.fetch_brief(tmp_project, "r")
    assert brief == leeboard.BriefResult(None, None)
    assert_no_leeboard_context(implement(leeboard_brief=brief))
    assert fake_lb.calls() == []


def test_absent_hold_gate_is_silent(no_lb, fake_lb, tmp_project):
    warnings = []
    gate = leeboard.HoldGate(warnings.append)
    assert gate.check(str(tmp_project)) == leeboard.HoldCheck("clear")
    assert gate.check(str(tmp_project)) == leeboard.HoldCheck("clear")
    gate.clear()
    assert gate.check(str(tmp_project)) == leeboard.HoldCheck("clear")
    assert warnings == []
    assert fake_lb.calls() == []


def test_no_lb_fixture_hides_binary(no_lb, fake_lb, fake_pi):
    assert shutil.which("lb") is None
    assert leeboard.find_lb() is None
    assert list(Path(os.environ["LEEBOARD_HOME"]).iterdir()) == []
    assert shutil.which("git")
    assert shutil.which("pi") == str(fake_pi.bin)
    assert fake_lb.calls() == []
