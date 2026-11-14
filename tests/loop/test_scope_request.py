"""``SCOPE:`` answers park the run for the owner; an approval resumes it in place."""

import subprocess

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app
from helmraiser.loop.report import build_report, render_report
from helmraiser.loop.runner import GovernedRun, parse_scope_request
from helmraiser.pi.adapter import session_id_for
from helmraiser.task.paths import add_paths_to_frontmatter
from tests.loop.test_evidence_review_merge import verdict_text
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

runner = CliRunner()

REQUEST = "SCOPE: tests/old_test.txt, docs/notes.md — the old test encodes the replaced behaviour"
PLAN = "# Implementation plan\n\n1. Update the old test and write done.txt.\n2. Run verify."


def git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def events(ledger, run, kind):
    return [e["payload"] for e in ledger.events(run.run_id) if e["type"] == kind]


def approve_scope(ledger, run, task, paths, note=None, *, decline=False):
    """What ``helmraiser approve`` does: snapshot the task and hand the run back."""
    source = task.source_path
    if not decline:
        source.write_text(add_paths_to_frontmatter(source.read_text(), paths))
    assert ledger.set_task_snapshot(task.task_id, source.read_text(), parked_run=run.run_id)
    ledger.decide_approval(
        ledger.pending_approval(run.run_id)["id"], "declined" if decline else "approved", note
    )
    assert ledger.hand_back(run.run_id, "implement")
    ledger.append_event(
        run.run_id,
        "helmraiser",
        "handed_back",
        {
            "stage": "implement",
            "scope": (
                {"paths": [], "declined": paths, "note": note}
                if decline
                else {"paths": paths, "note": note}
            ),
        },
    )


def resume(run, adapter, config, ledger, data_dir):
    return GovernedRun.resume(
        run.run_id,
        config=config,
        ledger=ledger,
        data_dir=data_dir,
        adapter_factory=lambda state, isolation: adapter,
        pi_version="fake",
        auth_check=lambda model: None,
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        (" a.txt, `b/c.md` — why", (["a.txt", "b/c.md"], "why")),
        (" a.txt - why not", (["a.txt"], "why not")),
        (" a.txt", (["a.txt"], "")),
        (" a.txt, a.txt, done.txt — dup", (["a.txt"], "dup")),
        (" — no path", None),
        ("", None),
        (" /etc/passwd — absolute", None),
        (" ../other/x.py — outside", None),
        (" done.txt — already allowed", None),
    ],
)
def test_parse_scope_request(text, expected):
    assert parse_scope_request(text, ("done.txt",)) == expected


def test_parse_scope_request_without_declared_paths():
    assert parse_scope_request(" a.txt — why", None) is None


async def test_scope_request_parks_with_work_kept(make_task, make_run, ledger, data_dir, config):
    task = make_task(paths=("done.txt",))
    adapter = ScriptedPiAdapter(
        {"implement": PiScript(final_text=REQUEST + "\nDetails.", files=[("done.txt", "part")])}
    )
    run, _ = make_run(task, adapter)
    outcome = await run.run()

    assert outcome.status == "awaiting_approval" and outcome.failure_reason is None
    assert outcome.pending_approval == "scope"
    request = {
        "paths": ["tests/old_test.txt", "docs/notes.md"],
        "reason": "the old test encodes the replaced behaviour",
    }
    assert outcome.scope_request == request
    assert events(ledger, run, "scope_requested") == [{"n": 1, **request}]
    assert not events(ledger, run, "verify_result") and not adapter.calls_for("review")
    assert len(adapter.calls_for("implement")) == 1
    pending = ledger.pending_approval(run.run_id)
    assert pending["kind"] == "scope" and pending["decision"] == "pending"
    assert ledger.get_run(run.run_id)["status"] == "awaiting_approval"
    # The worktree and the branch keep the in-scope work, committed.
    worktree = outcome.worktree_path
    assert git(worktree, "status", "--porcelain") == ""
    assert git(worktree, "show", "HEAD:done.txt") == "part"
    assert outcome.head_commit == git(worktree, "rev-parse", "HEAD") != outcome.base_commit
    assert ledger.iterations(run.run_id)[0]["commit_sha"] == outcome.head_commit
    summary = run.rundir.summary_md.read_text()
    assert "- scope request: `tests/old_test.txt`, `docs/notes.md` — the old test" in summary
    assert "the scope request" in summary and "--paths" in summary
    report = render_report(build_report(ledger, data_dir, 86400, config=config), "24h")
    assert "- scope request: `tests/old_test.txt`, `docs/notes.md`" in report
    assert "the scope request" in report
    decline_line = f'- decline: `helmraiser approve {run.run_id} --decline --note "<why>"`'
    assert decline_line in summary
    assert decline_line in report


async def test_approved_scope_resumes_in_the_same_session_and_lands(
    make_task, make_run, ledger, data_dir, config
):
    task = make_task(paths=("done.txt",), verify=[("both", "test -f done.txt")])
    first = ScriptedPiAdapter({"implement": PiScript(final_text=REQUEST)})
    run, _ = make_run(task, first)
    parked = await run.run()
    assert parked.pending_approval == "scope"

    approve_scope(ledger, run, task, ["tests/old_test.txt"], note="Keep the old fixture")
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("tests/old_test.txt", "new"), ("done.txt", "ok")]),
            "review": PiScript(final_text=verdict_text("pass")),
        }
    )
    resumed = resume(run, adapter, config, ledger, data_dir)
    assert resumed.claim_resume()
    outcome = await resumed.run()

    assert outcome.status == "awaiting_approval", outcome.failure_reason
    assert outcome.pending_approval == "merge" and outcome.scope_request is None
    assert (outcome.worktree_path, outcome.branch_name) == (
        parked.worktree_path,
        parked.branch_name,
    )
    call = adapter.calls_for("implement")[0]
    assert call.session_id == session_id_for(run.run_id, "implement")
    assert call.session_id == first.calls_for("implement")[0].session_id
    assert "The owner approved these paths: `tests/old_test.txt`. Continue the task." in (
        call.prompt
    )
    assert "Keep the old fixture" in call.prompt
    assert "`tests/old_test.txt`" in call.system_prompt_path.read_text()
    assert not events(ledger, run, "path_scope_failed")
    assert events(ledger, run, "run_resume")[-1]["from"] == "scope"
    assert [(a["kind"], a["decision"]) for a in ledger.approvals(run.run_id)] == [
        ("scope", "approved"),
        ("merge", "pending"),
    ]
    assert outcome.iterations == 2


async def test_scope_request_with_out_of_scope_edits_fails(make_task, make_run, ledger):
    task = make_task(paths=("done.txt",), max_iterations=3)
    adapter = ScriptedPiAdapter(
        {"implement": PiScript(final_text=REQUEST, files=[("docs/notes.md", "edited anyway")])}
    )
    run, _ = make_run(task, adapter)
    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == "verify_failed: task-paths"
    assert len(adapter.calls_for("implement")) == 1
    assert "docs/notes.md" in events(ledger, run, "path_scope_failed")[0]["diagnostic"]
    assert not events(ledger, run, "scope_requested")
    assert ledger.pending_approval(run.run_id) is None


@pytest.mark.parametrize("text", ["SCOPE: — I need more files", "SCOPE:"])
async def test_malformed_scope_request_fails_as_blocked(make_task, make_run, ledger, text):
    task = make_task(paths=("done.txt",))
    run, _ = make_run(task, ScriptedPiAdapter({"implement": PiScript(final_text=text)}))
    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == f"blocked: {text[len('SCOPE:'):].strip()}"
    assert not events(ledger, run, "scope_requested")
    assert ledger.pending_approval(run.run_id) is None


@pytest.mark.parametrize("decline", [False, True])
async def test_fallback_scope_request_resumes_with_the_fallback(
    make_task, make_run, ledger, data_dir, config, decline
):
    # The primary's one iteration fails verify; the fallback then asks for scope.
    task = make_task(paths=("done.txt",), max_iterations=1, verify=[("never", "false")])
    first = ScriptedPiAdapter(
        {"implement": [PiScript(files=[("done.txt", "x")]), PiScript(final_text=REQUEST)]}
    )
    run, _ = make_run(task, first)
    parked = await run.run()
    assert parked.pending_approval == "scope" and parked.escalated

    approve_scope(ledger, run, task, ["docs/notes.md"], "Find another way", decline=decline)
    adapter = ScriptedPiAdapter({"implement": PiScript(final_text="BLOCKED: still failing")})
    resumed = resume(run, adapter, config, ledger, data_dir)
    assert resumed.claim_resume()
    outcome = await resumed.run()
    assert outcome.failure_reason == "blocked: still failing"
    call = adapter.calls_for("implement")[0]
    assert call.session_id == session_id_for(run.run_id, "implement", 2)
    assert call.model == task.models["fallback"]
    if decline:
        assert call.prompt.startswith("Scope request declined (docs/notes.md): Find another way")
        assert "The owner approved these paths" not in call.prompt
    assert events(ledger, run, "run_resume")[-1]["from"] == "scope"


async def test_declined_scope_resumes_in_same_session_with_commits_kept(
    make_task, make_run, ledger, data_dir, config
):
    task = make_task(paths=("done.txt",))
    first = ScriptedPiAdapter(
        {"implement": PiScript(final_text=REQUEST, files=[("done.txt", "part")])}
    )
    run, _ = make_run(task, first)
    parked = await run.run()
    before = task.source_path.read_bytes()
    approve_scope(
        ledger,
        run,
        task,
        ["tests/old_test.txt", "docs/notes.md"],
        "Use the allowed paths",
        decline=True,
    )
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "part\nfinished")]),
            "review": PiScript(final_text=verdict_text("pass")),
        }
    )
    resumed = resume(run, adapter, config, ledger, data_dir)
    assert resumed.claim_resume()
    outcome = await resumed.run()
    assert outcome.pending_approval == "merge", outcome.failure_reason
    call = adapter.calls_for("implement")[0]
    assert call.session_id == first.calls_for("implement")[0].session_id
    assert call.prompt.startswith(
        "Scope request declined (tests/old_test.txt, docs/notes.md): Use the allowed paths"
    )
    assert "The owner approved these paths" not in call.prompt
    assert task.source_path.read_bytes() == before
    assert git(outcome.worktree_path, "show", "HEAD:done.txt") == "part\nfinished"
    assert git(outcome.worktree_path, "show", f"{parked.head_commit}:done.txt") == "part"
    assert (
        git(outcome.worktree_path, "merge-base", "--is-ancestor", parked.head_commit, "HEAD") == ""
    )
    assert events(ledger, run, "run_resume")[-1]["from"] == "scope"
    assert [(a["kind"], a["decision"]) for a in ledger.approvals(run.run_id)] == [
        ("scope", "declined"),
        ("merge", "pending"),
    ]


async def test_plan_scope_request_parks_with_saved_plan(make_task, make_run, ledger):
    task = make_task(plan=True, paths=("done.txt",))
    adapter = ScriptedPiAdapter({"plan": PiScript(final_text=f"  {REQUEST}\n\n{PLAN}\n  ")})
    run, _ = make_run(task, adapter)
    outcome = await run.run()

    assert outcome.status == "awaiting_approval" and outcome.failure_reason is None
    assert outcome.pending_approval == "scope"
    request = {
        "paths": ["tests/old_test.txt", "docs/notes.md"],
        "reason": "the old test encodes the replaced behaviour",
    }
    assert outcome.scope_request == request
    pending = ledger.pending_approval(run.run_id)
    assert (pending["kind"], pending["decision"]) == ("scope", "pending")
    assert ledger.get_run(run.run_id)["status"] == "awaiting_approval"
    assert run.rundir.plan_md.read_text() == PLAN + "\n"
    assert events(ledger, run, "plan_written") == [
        {"path": str(run.rundir.plan_md), "chars": len(PLAN)}
    ]
    all_events = ledger.events(run.run_id)
    written = next(e for e in all_events if e["type"] == "plan_written")
    scope = next(e for e in all_events if e["type"] == "scope_requested")
    assert written["id"] < scope["id"]
    assert scope["stage"] == "plan"
    assert scope["session_id"] == adapter.calls_for("plan")[0].session_id
    assert scope["payload"] == request  # No iteration number in a plan request.
    assert not adapter.calls_for("implement") and not adapter.calls_for("review")
    assert not events(ledger, run, "verify_result")
    assert not ledger.iterations(run.run_id) and outcome.iterations == 0
    assert git(outcome.worktree_path, "status", "--porcelain") == ""
    assert git(outcome.worktree_path, "rev-parse", "HEAD") == outcome.base_commit
    assert outcome.head_commit is None


@pytest.mark.parametrize(
    "header",
    ["SCOPE:", "SCOPE: — no path", "SCOPE: /etc/passwd — absolute", "SCOPE: done.txt — allowed"],
)
async def test_plan_scope_malformed_request_fails_as_blocked(make_task, make_run, ledger, header):
    task = make_task(plan=True, paths=("done.txt",))
    adapter = ScriptedPiAdapter({"plan": PiScript(final_text=f"{header}\n\n{PLAN}")})
    run, _ = make_run(task, adapter)
    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == f"blocked: {header[len('SCOPE:'):].strip()}"
    assert ledger.pending_approval(run.run_id) is None
    assert not events(ledger, run, "scope_requested")
    assert not ledger.iterations(run.run_id)
    assert not adapter.calls_for("implement")


@pytest.mark.parametrize(
    "task_kwargs, instruction",
    [
        ({"plan": True}, "`SCOPE: <path>[, <path>…] — <reason>`"),
        ({"policy": "approve-plan-first"}, "outside the allowed paths under the plan's open"),
    ],
)
async def test_plan_stage_tells_the_planner_about_declared_paths(
    make_task, make_run, task_kwargs, instruction
):
    task = make_task(paths=("done.txt",), **task_kwargs)
    adapter = ScriptedPiAdapter({"plan": PiScript(final_text=PLAN)})
    run, _ = make_run(task, adapter)
    await run.run()

    (call,) = adapter.calls_for("plan")
    assert instruction in call.system_prompt_path.read_text()
    assert instruction in call.prompt


async def test_plan_without_paths_ignores_a_scope_line_and_continues(
    make_task, make_run, ledger, data_dir
):
    """A run whose task has no ``paths:`` has nothing to approve."""
    line = "SCOPE: tools/example-cli, azure-pipelines-app.yml, package.json — pipeline files"
    task = make_task(plan=True)
    adapter = ScriptedPiAdapter(
        {
            "plan": PiScript(final_text=f"{line}\n\n{PLAN}"),
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": PiScript(final_text=verdict_text("pass")),
        }
    )
    run, _ = make_run(task, adapter)
    outcome = await run.run()

    assert outcome.status == "awaiting_approval", outcome.failure_reason
    assert outcome.pending_approval == "merge" and outcome.scope_request is None
    assert run.rundir.plan_md.read_text() == PLAN + "\n"
    assert events(ledger, run, "plan_written") == [
        {"path": str(run.rundir.plan_md), "chars": len(PLAN)}
    ]
    (implement_call,) = adapter.calls_for("implement")
    assert PLAN in implement_call.prompt and "SCOPE" not in implement_call.prompt
    assert not events(ledger, run, "scope_requested")
    assert [a["kind"] for a in ledger.approvals(run.run_id)] == ["merge"]
    (ignored,) = [e for e in ledger.events(run.run_id) if e["type"] == "scope_line_ignored"]
    assert ignored["payload"] == {"line": line}
    assert ignored["stage"] == "plan"
    assert ignored["session_id"] == adapter.calls_for("plan")[0].session_id

    logged = runner.invoke(app, ["log", run.run_id, "--data-dir", str(data_dir)])
    assert logged.exit_code == 0, logged.output
    (row,) = [text for text in logged.output.splitlines() if "scope_line_ignored" in text]
    assert "plan" in row and line in row  # The em dash is not escaped.


@pytest.mark.parametrize("tail", ["", "\n\n  \n"])
async def test_plan_without_paths_and_only_a_scope_line_has_no_plan(
    make_task, make_run, ledger, tail
):
    task = make_task(plan=True)
    adapter = ScriptedPiAdapter({"plan": PiScript(final_text=REQUEST + tail)})
    run, _ = make_run(task, adapter)
    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == "plan: the planning session produced no plan"
    assert not run.rundir.plan_md.exists()
    assert not events(ledger, run, "plan_written")
    assert ledger.pending_approval(run.run_id) is None
    assert not adapter.calls_for("implement")


@pytest.mark.parametrize("tail", ["", "\n\n  \n"])
async def test_plan_scope_request_requires_plan_body(make_task, make_run, ledger, tail):
    task = make_task(plan=True, paths=("done.txt",))
    adapter = ScriptedPiAdapter({"plan": PiScript(final_text=REQUEST + tail)})
    run, _ = make_run(task, adapter)
    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == "plan: the planning session produced no plan"
    assert not run.rundir.plan_md.exists()
    assert not events(ledger, run, "plan_written")
    assert not events(ledger, run, "scope_requested")
    assert ledger.pending_approval(run.run_id) is None
    assert not ledger.iterations(run.run_id) and not adapter.calls_for("implement")


async def test_plan_scope_approved_resumes_at_implement_with_plan(
    make_task, make_run, ledger, data_dir, config
):
    task = make_task(plan=True, paths=("done.txt",))
    first = ScriptedPiAdapter({"plan": PiScript(final_text=f"{REQUEST}\n\n{PLAN}")})
    run, _ = make_run(task, first)
    parked = await run.run()
    assert parked.pending_approval == "scope"
    approve_scope(ledger, run, task, ["tests/old_test.txt"], note="Keep the old fixture")
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "ok"), ("tests/old_test.txt", "new")]),
            "review": PiScript(final_text=verdict_text("pass")),
        }
    )
    resumed = resume(run, adapter, config, ledger, data_dir)
    assert resumed.claim_resume()
    outcome = await resumed.run()

    assert outcome.pending_approval == "merge", outcome.failure_reason
    assert (outcome.run_id, outcome.worktree_path) == (parked.run_id, parked.worktree_path)
    (call,) = adapter.calls_for("implement")
    assert "The owner approved these paths: `tests/old_test.txt`." in call.prompt
    assert "Keep the old fixture" in call.prompt
    assert f"## Plan\n\n{PLAN}" in call.prompt
    assert "## Approved plan" not in call.prompt and REQUEST not in call.prompt
    assert "`tests/old_test.txt`" in call.system_prompt_path.read_text()
    assert resumed.task.paths == ("done.txt", "tests/old_test.txt")
    assert not events(ledger, run, "path_scope_failed")
    assert not adapter.calls_for("plan") and len(first.calls_for("plan")) == 1
    assert events(ledger, run, "run_resume")[-1]["from"] == "scope"
    assert outcome.iterations == 1


async def test_plan_scope_declined_resumes_at_implement_with_plan(
    make_task, make_run, ledger, data_dir, config
):
    task = make_task(plan=True, paths=("done.txt",))
    first = ScriptedPiAdapter({"plan": PiScript(final_text=f"{REQUEST}\n\n{PLAN}")})
    run, _ = make_run(task, first)
    parked = await run.run()
    assert parked.pending_approval == "scope"
    before = task.source_path.read_bytes()
    approve_scope(
        ledger,
        run,
        task,
        ["tests/old_test.txt", "docs/notes.md"],
        "Use allowed paths",
        decline=True,
    )
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "ok")]),
            "review": PiScript(final_text=verdict_text("pass")),
        }
    )
    resumed = resume(run, adapter, config, ledger, data_dir)
    assert resumed.claim_resume()
    outcome = await resumed.run()

    assert outcome.pending_approval == "merge", outcome.failure_reason
    (call,) = adapter.calls_for("implement")
    assert call.prompt.startswith(
        "Scope request declined (tests/old_test.txt, docs/notes.md): Use allowed paths"
    )
    assert f"## Plan\n\n{PLAN}" in call.prompt
    assert "The owner approved these paths" not in call.prompt
    assert "## Approved plan" not in call.prompt and REQUEST not in call.prompt
    assert task.source_path.read_bytes() == before
    assert resumed.task.paths == task.paths == ("done.txt",)
    assert not adapter.calls_for("plan") and len(first.calls_for("plan")) == 1
    assert events(ledger, run, "run_resume")[-1]["from"] == "scope"
    assert outcome.iterations == 1


async def test_plan_scope_approval_first_saves_header_as_ordinary_plan(make_task, make_run, ledger):
    task = make_task(policy="approve-plan-first", paths=("done.txt",))
    text = f"{REQUEST}\n\n{PLAN}"
    adapter = ScriptedPiAdapter({"plan": PiScript(final_text=text)})
    run, _ = make_run(task, adapter)
    outcome = await run.run()

    assert outcome.status == "awaiting_approval" and outcome.pending_approval == "plan"
    assert outcome.scope_request is None
    assert run.rundir.plan_md.read_text() == text + "\n"
    assert ledger.pending_approval(run.run_id)["kind"] == "plan"
    assert not events(ledger, run, "scope_requested")
    assert not ledger.iterations(run.run_id) and not adapter.calls_for("implement")
