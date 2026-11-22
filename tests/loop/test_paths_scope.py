"""Scope failures use normal iteration accounting and repair feedback."""

import errno
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from helmraiser.loop import runner
from helmraiser.loop.report import build_report, render_report
from tests.loop.test_evidence_review_merge import verdict_text
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter


class ScopeAdapter(ScriptedPiAdapter):
    def __init__(self, scripts, actions=None):
        super().__init__(scripts)
        self.actions = actions or {}
        self.directives = []

    async def run(self, req, on_event, run_id):
        if req.stage == "implement":
            n = len(self.calls_for("implement")) + 1
            action = self.actions.get(n)
            if action:
                action(req.cwd)
        if req.system_prompt_path:
            self.directives.append((req.stage, req.system_prompt_path.read_text()))
        return await super().run(req, on_event, run_id)


def git(path, *args):
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def scope_events(ledger, run):
    return [e for e in ledger.events(run.run_id) if e["type"] == "path_scope_failed"]


@pytest.fixture
def verify_spy(monkeypatch):
    spy = Mock(wraps=runner.run_verify)
    monkeypatch.setattr(runner, "run_verify", spy)
    return spy


def adapter(scripts, actions=None):
    return ScopeAdapter(
        {"implement": scripts, "review": PiScript(final_text=verdict_text("pass"))}, actions
    )


async def test_in_scope_reaches_verify_and_review(make_task, make_run, ledger, verify_spy):
    task = make_task(paths=("done.txt", "src/**"))
    pi = adapter(PiScript(files=[("done.txt", "ok")]))
    run, _ = make_run(task, pi)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval"
    assert verify_spy.call_count == 1
    assert len(pi.calls_for("review")) == 1
    assert scope_events(ledger, run) == []
    for _stage, text in pi.directives:
        assert "done.txt" in text and "src/**" in text
    assert pi.calls_for("implement")[0].prompt.index("done.txt") < pi.calls_for("implement")[
        0
    ].prompt.index("## Goal")
    assert "src/**" in pi.calls_for("review")[0].prompt


async def test_out_of_scope_feedback_then_repair(make_task, make_run, ledger, verify_spy):
    pi = adapter(
        [
            PiScript(files=[("forbidden.txt", "bad")]),
            PiScript(files=[("done.txt", "ok")]),
        ],
        {2: lambda cwd: (cwd / "forbidden.txt").unlink()},
    )
    run, _ = make_run(make_task(paths=("done.txt",), max_iterations=2, fallback=False), pi)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval"
    assert verify_spy.call_count == 1
    feedback = pi.calls_for("implement")[1].prompt
    assert "forbidden.txt" in feedback and "done.txt" in feedback
    assert "acceptance commands were skipped" in feedback
    iterations = ledger.iterations(run.run_id)
    assert [i["verify_passed"] for i in iterations] == [False, True]
    events = scope_events(ledger, run)
    assert len(events) == 1 and events[0]["payload"]["n"] == 1
    assert events[0]["payload"]["paths"] == ["done.txt"]


@pytest.mark.parametrize("committed", [False, True])
async def test_forbidden_untracked_or_committed(make_task, make_run, ledger, verify_spy, committed):
    def create(cwd):
        (cwd / "forbidden.txt").write_text("bad")
        if committed:
            git(cwd, "add", "forbidden.txt")
            git(cwd, "commit", "-m", "forbidden")

    pi = adapter(PiScript(), {1: create})
    run, _ = make_run(make_task(paths=("done.txt",), max_iterations=1, fallback=False), pi)
    # Keep the untracked case untracked at the gate rather than the normal auto-commit.
    if not committed:
        run._commit_all = lambda message: True
    outcome = await run.run()
    assert outcome.failure_reason == "verify_exhausted"
    assert verify_spy.call_count == 0 and not pi.calls_for("review")
    assert "forbidden.txt" in scope_events(ledger, run)[0]["payload"]["diagnostic"]
    assert ledger.iterations(run.run_id)[0]["verify_results"][0]["name"] == "task-paths"


@pytest.mark.parametrize("second", [PiScript(), PiScript(files=[("done.txt", "ok")])])
async def test_cumulative_violation_survives_unchanged_or_allowed_turn(
    make_task, make_run, ledger, verify_spy, second
):
    pi = adapter([PiScript(files=[("forbidden.txt", "bad")]), second])
    run, _ = make_run(make_task(paths=("done.txt",), max_iterations=2, fallback=False), pi)
    outcome = await run.run()
    assert outcome.failure_reason == "verify_exhausted"
    assert verify_spy.call_count == 0
    assert len(scope_events(ledger, run)) == 2
    assert [i["verify_passed"] for i in ledger.iterations(run.run_id)] == [False, False]
    assert not any(e["type"] == "no_changes" for e in ledger.events(run.run_id))


async def test_fallback_has_complete_scope_history(make_task, make_run, ledger, verify_spy):
    files = [(f"forbidden-{n:02}.txt", "bad") for n in range(15)]

    def repair(cwd):
        for name, _ in files:
            (cwd / name).unlink()

    pi = adapter([PiScript(files=files), PiScript(files=[("done.txt", "ok")])], {2: repair})
    run, _ = make_run(make_task(paths=("done.txt",), max_iterations=1), pi)
    outcome = await run.run()
    assert outcome.escalated and outcome.status == "awaiting_approval"
    history = pi.calls_for("implement")[1].prompt
    assert all(name in history for name, _ in files)
    assert verify_spy.call_count == 1
    summary = run.rundir.summary_md.read_text()
    report = render_report(build_report(ledger, run.data_dir, 86400, config=run.config), "24h")
    for text in (summary, report):
        assert text.count("### Task path scope failures") == 1
        assert all(name in text for name, _ in files)
        assert "Iteration 1" in text


async def test_exhausted_scope_report_is_complete(make_task, make_run, ledger):
    files = [(f"outside-{n:02}.txt", "bad") for n in range(15)]
    pi = adapter(PiScript(files=files))
    run, _ = make_run(make_task(paths=("done.txt",), max_iterations=1, fallback=False), pi)
    outcome = await run.run()
    assert outcome.failure_reason == "verify_exhausted"
    payload = build_report(ledger, run.data_dir, 86400, config=run.config)
    assert payload["failed"][0]["path_scope_failures"] == [scope_events(ledger, run)[0]["payload"]]
    for text in (run.rundir.summary_md.read_text(), render_report(payload, "24h")):
        assert text.count("### Task path scope failures") == 1
        assert all(name in text for name, _ in files)
        assert "done.txt" in text


async def test_pathless_never_collects_scope(make_task, make_run, monkeypatch, verify_spy):
    def forbidden(*args):
        raise AssertionError("Scope must not be collected")

    monkeypatch.setattr(runner, "changed_paths", forbidden)
    pi = adapter(PiScript(files=[("done.txt", "ok"), ("outside.txt", "also ok")]))
    run, _ = make_run(make_task(), pi)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval" and verify_spy.call_count == 1


@pytest.mark.parametrize(
    "allowed,destination,offender",
    [("done.txt", "done.txt", "README.md"), ("README.md", "outside.txt", "outside.txt")],
)
async def test_rename_across_scope_boundary(
    make_task, make_run, ledger, verify_spy, allowed, destination, offender
):
    pi = adapter(PiScript(), {1: lambda cwd: git(cwd, "mv", "README.md", destination)})
    run, _ = make_run(make_task(paths=(allowed,), max_iterations=1, fallback=False), pi)
    outcome = await run.run()
    assert outcome.failure_reason == "verify_exhausted"
    assert verify_spy.call_count == 0
    assert offender in scope_events(ledger, run)[0]["payload"]["diagnostic"]


async def test_collection_error_fails_closed(make_task, make_run, monkeypatch, ledger, verify_spy):
    def broken(*args):
        raise RuntimeError("git diff failed")

    monkeypatch.setattr(runner, "changed_paths", broken)
    pi = adapter(PiScript(files=[("done.txt", "ok")]))
    run, _ = make_run(make_task(paths=("done.txt",), fallback=False, max_iterations=1), pi)
    outcome = await run.run()
    assert outcome.failure_reason == "verify_exhausted" and verify_spy.call_count == 0
    assert "Scope could not be established" in scope_events(ledger, run)[0]["payload"]["diagnostic"]


async def test_placeholders_never_enter_scope(make_task, make_run, ledger, verify_spy):
    from helmraiser.isolation.placeholders import PLACEHOLDER_NAMES

    def create(cwd):
        for name in PLACEHOLDER_NAMES:
            (cwd / name).touch()

    pi = adapter(PiScript(files=[("done.txt", "ok")]), {1: create})
    run, _ = make_run(make_task(paths=("done.txt",)), pi)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval"
    assert verify_spy.call_count == 1
    assert pi.calls_for("review")
    assert not scope_events(ledger, run)
    assert not set(PLACEHOLDER_NAMES) & set(run._git("ls-files").stdout.splitlines())
    events = [e for e in ledger.events(run.run_id) if e["type"] == "sandbox_placeholders_removed"]
    assert events[0]["payload"]["names"] == list(PLACEHOLDER_NAMES)
    assert "sandbox_placeholders_removed" in (run.rundir.path / "events.jsonl").read_text()


def test_commit_placeholders_only(make_task, make_run, tmp_path):
    from helmraiser.isolation.placeholders import PLACEHOLDER_NAMES

    tmp_path = tmp_path / "commit-repo"
    tmp_path.mkdir()
    git(tmp_path, "init")
    git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@t",
        "commit",
        "--allow-empty",
        "-m",
        "init",
    )
    run, _ = make_run(make_task(), adapter(PiScript()))
    run.worktree = tmp_path
    run._event = Mock()
    before = run._git("rev-parse", "HEAD").stdout
    for name in PLACEHOLDER_NAMES:
        (tmp_path / name).touch()
    assert run._commit_all("no empty commit")
    assert run._git("rev-parse", "HEAD").stdout == before


def test_partial_cleanup_logs_before_refusing_staging(make_task, make_run, tmp_path, monkeypatch):
    cwd = tmp_path / "partial-repo"
    cwd.mkdir()
    git(cwd, "init")
    (cwd / ".gitconfig").touch()
    (cwd / ".gitmodules").touch()
    run, _ = make_run(make_task(), adapter(PiScript()))
    run.worktree = cwd
    run._event = Mock()
    spy = Mock()
    monkeypatch.setattr(run, "_git", spy)
    real_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == cwd / ".gitmodules":
            raise OSError(errno.EBUSY, "mounted")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert not run._commit_all("must not stage")
    spy.assert_not_called()
    run._event.assert_called_once_with(
        "sandbox_placeholders_removed", {"cwd": str(cwd), "names": [".gitconfig"]}
    )
    assert not (cwd / ".gitconfig").exists()
    assert (cwd / ".gitmodules").exists()


def test_commit_cleanup_failure_never_stages(make_task, make_run, tmp_path, monkeypatch):
    run, _ = make_run(make_task(), adapter(PiScript()))
    run.worktree = tmp_path
    spy = Mock()
    monkeypatch.setattr(run, "_git", spy)

    def fail(cwd):
        raise PermissionError("busy")

    monkeypatch.setattr(runner, "remove_placeholders", fail)
    assert not run._commit_all("must not stage")
    spy.assert_not_called()
