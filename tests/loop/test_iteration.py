"""Iteration loop (spec §4.3): pass, feedback in the same session, exhaustion,
escalation, BLOCKED, proof-of-work, provider retry, timeout/abort, preflight."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from helmraiser.ledger.db import Ledger
from helmraiser.loop.prompts import NO_CHANGES_PROMPT
from helmraiser.loop.runner import TaskLocked, conventional_history
from helmraiser.pi.protocol import PiEvent, PiSessionResult, Usage
from helmraiser.storage.state import StateManager
from tests.loop.conftest import assistant_message_end
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _events(ledger: Ledger, run_id: str, source: str = "helmraiser") -> list[str]:
    return [e["type"] for e in ledger.events(run_id) if e["source"] == source]


async def test_live_cost_is_persisted_after_each_message(make_task, make_run, ledger):
    task = make_task()
    events = [assistant_message_end(0.01), assistant_message_end(0.02)]

    class ObservingAdapter(ScriptedPiAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.costs: list[float] = []

        async def run(self, req, on_event, run_id):  # type: ignore[no-untyped-def]
            if req.stage != "implement":
                return await super().run(req, on_event, run_id)
            self.calls.append(req)
            self.run_ids.append(run_id)
            for event in events:
                await on_event(event)
                self.costs.append(float(ledger.get_run(run_id)["cost_usd"]))
            (Path(req.cwd) / "done.txt").write_text("ok\n")
            return PiSessionResult(
                ok=True,
                session_id=req.session_id,
                final_text="implemented",
                stop_reason="stop",
                error_kind=None,
                error_message=None,
                usage=Usage(20, 10, 0.03),
                tool_calls=0,
                returncode=0,
                timed_out=False,
                killed=False,
            )

    adapter = ObservingAdapter()
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert adapter.costs == pytest.approx([0.01, 0.03])
    assert outcome.cost_usd == pytest.approx(0.03)
    assert ledger.get_run(run.run_id)["cost_usd"] == pytest.approx(0.03)


async def test_pass_first_try(make_task, make_run, ledger, tmp_project, data_dir, runtime_dir):
    task = make_task()
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, sleep = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval", outcome
    assert outcome.failure_reason is None
    assert outcome.iterations == 1 and not outcome.escalated
    assert outcome.verify_results == [
        {**outcome.verify_results[0], "name": "marker", "passed": True, "exit_code": 0}
    ]
    assert sleep.calls == []

    # One implement turn from the worktree with directives, then the review session.
    req, review = adapter.calls
    assert review.session_id == f"{run.run_id}-review" and review.stage == "review"
    assert "## Approved plan" not in review.prompt
    assert req.session_id == f"{run.run_id}-implement"
    assert req.stage == "implement" and req.attempt == 1
    assert req.model.render() == "openai-codex/gpt-6.1-sol:medium"
    assert Path(req.cwd) == Path(outcome.worktree_path)
    assert req.session_dir == data_dir / "runs" / run.run_id / "sessions"
    assert req.system_prompt_path == data_dir / "runs" / run.run_id / "directives.md"
    assert "Make the widget work." in req.prompt
    assert "## Acceptance" in req.prompt and "`test -f done.txt`" in req.prompt
    assert req.prompt.endswith("Begin.")
    assert req.timeout_s <= 90 * 60

    # Worktree on a run branch, changes committed by Helmraiser.
    worktree = Path(outcome.worktree_path)
    assert worktree.is_dir() and (worktree / "done.txt").exists()
    assert outcome.branch_name == f"feature/fix-widget/{run.run_id}"
    assert _git(worktree, "log", "-1", "--format=%s") == "helmraiser: iteration 1 of fix-widget"
    assert _git(worktree, "rev-parse", "HEAD") == outcome.head_commit
    assert outcome.base_commit == _git(tmp_project, "rev-parse", "HEAD")
    assert _git(worktree, "status", "--porcelain") == ""

    # Ledger: run row, iteration row, task status, events.
    row = ledger.get_run(run.run_id)
    assert row["status"] == "awaiting_approval" and row["ended_at"] is not None
    assert row["head_commit"] == outcome.head_commit and row["base_commit"] == outcome.base_commit
    assert row["branch_name"] == outcome.branch_name
    assert row["pi_version"] == "0.87.1" and row["stage"] is None
    assert row["model_review"] == "openai-codex/gpt-6-astra:high"
    (iteration,) = ledger.iterations(run.run_id)
    assert iteration["verify_passed"] == 1 and iteration["commit_sha"] == outcome.head_commit
    assert iteration["session_id"] == req.session_id
    assert ledger.get_task(task.task_id)["status"] == "awaiting_approval"
    types = _events(ledger, run.run_id)
    for expected in (
        "run_start",
        "preflight",
        "worktree_created",
        "stage_start",
        "iteration_start",
        "verify_result",
        "merge_decision",
        "run_end",
    ):
        assert expected in types, types
    assert ledger.next_queued() is None  # project holds a non-terminal run

    # Run dir materialised; summary + status.json written.
    run_dir = data_dir / "runs" / run.run_id
    assert (run_dir / "task.md").read_text() == task.source_path.read_text()
    assert "governed implementation" in (run_dir / "directives.md").read_text()
    assert (run_dir / "sessions").is_dir() and (run_dir / "evidence").is_dir()
    assert "verify_result" in (run_dir / "events.jsonl").read_text()
    summary = (run_dir / "summary.md").read_text()
    assert "awaiting_approval" in summary and "marker: pass" in summary
    status = json.loads((runtime_dir / "helmraiser" / "status.json").read_text())
    assert status["awaiting_count"] == 1 and status["running"] == [] and status["queued_count"] == 0

    # Per-project state: lock released, run terminal there.
    state = StateManager(tmp_project / ".helmraiser" / "state.db")
    assert state.get_locked_run_id("fix-widget") is None
    assert state.get_run(run.run_id)["status"] == "succeeded"

    # Ledger: stage order implement → review; agent session/model in the iteration row.
    helmraiser_events = [e for e in ledger.events(run.run_id) if e["source"] == "helmraiser"]
    assert [e["payload"]["stage"] for e in helmraiser_events if e["type"] == "stage_start"] == [
        "implement",
        "review",
        "merge",
    ]


async def test_project_notes_reach_implement_and_review_directives(
    make_task, make_run, tmp_project, data_dir
):
    notes = "Use node --import tsx instead of the tsx CLI."
    notes_path = tmp_project / ".helmraiser" / "notes.md"
    notes_path.parent.mkdir(exist_ok=True)
    notes_path.write_text(notes, encoding="utf-8")
    task = make_task()
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval"
    for name in ("directives.md", "directives-review.md"):
        directives = (data_dir / "runs" / run.run_id / name).read_text(encoding="utf-8")
        assert directives.count("## Project notes (Helmraiser)") == 1
        assert directives.count(notes) == 1


async def test_fail_feedback_pass_in_same_session(make_task, make_run, ledger):
    task = make_task([("content", "grep -q ok done.txt")])
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(files=[("done.txt", "nope\n")]),
                PiScript(files=[("done.txt", "ok\n")]),
            ]
        }
    )
    run, sleep = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval", outcome
    assert outcome.iterations == 2
    first, second = adapter.calls_for("implement")
    assert first.session_id == second.session_id == f"{run.run_id}-implement"
    assert second.attempt == 1
    assert "Verify failed" in second.prompt
    assert "### content (exit 1)" in second.prompt
    assert "run the verify commands again before stopping" in second.prompt
    assert sleep.calls == []
    iterations = ledger.iterations(run.run_id)
    assert [i["verify_passed"] for i in iterations] == [0, 1]
    assert iterations[0]["commit_sha"] != iterations[1]["commit_sha"]
    assert iterations[0]["verify_results"][0]["name"] == "content"
    worktree = Path(outcome.worktree_path)
    assert _git(worktree, "log", "--format=%s", "-2").splitlines() == [
        "helmraiser: iteration 2 of fix-widget",
        "helmraiser: iteration 1 of fix-widget",
    ]


def _commit_subjects(repo: Path, subjects: list[str]) -> None:
    for i, subject in enumerate(subjects):
        (repo / f"f{i}.txt").write_text(subject)
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", subject)


def test_conventional_history_needs_most_recent_subjects_to_match(tmp_path, git_init):
    conventional = tmp_path / "conventional"
    conventional.mkdir()
    git_init(conventional)  # "Initial commit" does not match
    _commit_subjects(
        conventional,
        [
            "feat(hub-api): add cursor pagination to audit history",
            "ci: install Node 22 with UseNode@1",
            "fix(web)!: keep ErrorBoundary on theme re-render",
        ],
    )
    assert conventional_history(conventional, "HEAD")
    # Only the last ``sample`` subjects count.
    assert not conventional_history(conventional, "HEAD~3")

    free_form = tmp_path / "free-form"
    free_form.mkdir()
    git_init(free_form)
    _commit_subjects(free_form, ["feat: one", "Add a thing", "preflight: fix"])
    # "preflight: fix" matches too; two of four is not most.
    assert not conventional_history(free_form, "HEAD")
    assert not conventional_history(tmp_path, "HEAD")  # not a repository


async def test_fallback_commit_follows_conventional_history(make_task, make_run, tmp_project):
    _commit_subjects(tmp_project, ["feat(api): add a", "fix(api): repair b", "docs: note c"])
    task = make_task()
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval", outcome
    assert (
        _git(Path(outcome.worktree_path), "log", "-1", "--format=%s")
        == "chore(helmraiser): iteration 1 of fix-widget"
    )


async def test_feedback_carries_tail_of_failed_step(make_task, make_run, config):
    config["verify_tail_lines"] = 2
    task = make_task([("noisy", "printf 'a\\nb\\nc\\n'; exit 3")])
    adapter = ScriptedPiAdapter(
        {"implement": [PiScript(files=[("x", "1")]), PiScript(files=[("x", "2")])]}
    )
    run, _ = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.status == "failed"
    second = adapter.calls[1]
    assert "### noisy (exit 3)" in second.prompt
    assert "```\n… 1 line omitted …\nb\nc\n```" in second.prompt
    log = run.rundir.logs / "verify-1-1-noisy.log"
    assert log.read_text(encoding="utf-8") == "a\nb\nc\n"
    assert f"The full output of this step is in `{log}`." in second.prompt


async def test_exhaustion_without_fallback(make_task, make_run, ledger):
    task = make_task(max_iterations=2, fallback=False)
    adapter = ScriptedPiAdapter(
        {"implement": [PiScript(files=[("x", "1")]), PiScript(files=[("x", "2")])]}
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "failed" and outcome.failure_reason == "verify_exhausted"
    assert outcome.iterations == 2 and not outcome.escalated
    assert len(adapter.calls) == 2
    assert {c.session_id for c in adapter.calls} == {f"{run.run_id}-implement"}
    assert ledger.get_run(run.run_id)["status"] == "failed"
    assert ledger.get_run(run.run_id)["escalated"] == 0
    assert ledger.get_task(task.task_id)["status"] == "failed"
    # Failed run: worktree and branch are kept for inspection.
    assert Path(outcome.worktree_path).is_dir()


async def test_escalation_halves_budget_and_uses_fresh_session(make_task, make_run, ledger):
    task = make_task(max_iterations=4)
    scripts = [PiScript(files=[("x", str(n))]) for n in range(10)]
    adapter = ScriptedPiAdapter({"implement": scripts})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "failed" and outcome.failure_reason == "verify_exhausted"
    assert outcome.escalated and outcome.iterations == 6
    sessions = [c.session_id for c in adapter.calls]
    assert sessions == [f"{run.run_id}-implement"] * 4 + [f"{run.run_id}-implement-2"] * 2
    assert [c.attempt for c in adapter.calls] == [1, 1, 1, 1, 2, 2]
    models = [c.model.render() for c in adapter.calls]
    assert models[:4] == ["openai-codex/gpt-6.1-sol:medium"] * 4
    assert models[4:] == ["openai-codex/gpt-6-astra:high"] * 2
    escalation = adapter.calls[4].prompt
    assert "Make the widget work." in escalation and "## Acceptance" in escalation
    assert "## What was tried so far" in escalation
    assert "Iteration 4 (openai-codex/gpt-6.1-sol:medium): 0/1 verify steps passed" in escalation
    assert f"git diff {outcome.base_commit}" in escalation
    assert "The previous model could not make verify pass." in escalation
    # The fallback's second turn is ordinary feedback in the -2 session.
    assert "Verify failed" in adapter.calls[5].prompt
    row = ledger.get_run(run.run_id)
    assert row["escalated"] == 1
    assert [i["model"] for i in ledger.iterations(run.run_id)][4:] == [
        "openai-codex/gpt-6-astra:high"
    ] * 2
    types = [e["type"] for e in ledger.events(run.run_id)]
    assert "escalated" in types
    escalated = next(e for e in ledger.events(run.run_id) if e["type"] == "escalated")
    assert escalated["payload"]["max_iterations"] == 2


async def test_escalation_rounds_up_odd_budgets(make_task, make_run):
    task = make_task(max_iterations=3)
    adapter = ScriptedPiAdapter({"implement": [PiScript(files=[("x", str(n))]) for n in range(9)]})
    run, _ = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.iterations == 5  # 3 + ceil(3/2)


async def test_blocked_fails_the_run(make_task, make_run, ledger):
    task = make_task()
    adapter = ScriptedPiAdapter(
        {"implement": PiScript(final_text="BLOCKED: the API key for the widget service is missing")}
    )
    run, _ = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.status == "failed"
    assert outcome.failure_reason == "blocked: the API key for the widget service is missing"
    assert len(adapter.calls) == 1  # no review after a blocked run
    (iteration,) = ledger.iterations(run.run_id)
    assert iteration["agent_ok"] == 1 and iteration["verify_passed"] is None


async def test_no_changes_is_fed_back(make_task, make_run, ledger):
    task = make_task()
    adapter = ScriptedPiAdapter(
        {"implement": [PiScript(final_text="I looked around."), PiScript(files=[("done.txt", "")])]}
    )
    run, _ = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval"
    assert adapter.calls[1].prompt == NO_CHANGES_PROMPT
    first, second = ledger.iterations(run.run_id)
    assert first["verify_passed"] is None and first["commit_sha"] is None
    assert second["verify_passed"] == 1
    assert "no_changes" in _events(ledger, run.run_id)


async def test_agent_commits_are_accepted_as_proof_of_work(make_task, make_run, tmp_project):
    """An agent that commits itself leaves a clean tree; HEAD moving is enough."""
    task = make_task()

    class CommittingAdapter(ScriptedPiAdapter):
        async def run(self, req, on_event, run_id):
            if req.stage != "implement":
                return await super().run(req, on_event, run_id)
            (req.cwd / "done.txt").write_text("")
            subprocess.run(["git", "add", "-A"], cwd=req.cwd, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "agent: did it"],
                cwd=req.cwd,
                check=True,
                capture_output=True,
            )
            return await super().run(req, on_event, run_id)

    run, _ = make_run(task, CommittingAdapter())
    outcome = await run.run()
    assert outcome.status == "awaiting_approval"
    assert _git(Path(outcome.worktree_path), "log", "-1", "--format=%s") == "agent: did it"


async def test_provider_error_retries_once_after_30s_in_same_session(make_task, make_run, ledger):
    task = make_task()
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(ok=False, error_kind="provider", error_message="502 upstream"),
                PiScript(files=[("done.txt", "")]),
            ]
        }
    )
    run, sleep = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval"
    assert sleep.calls == [30.0]
    assert outcome.iterations == 1
    first, second = adapter.calls_for("implement")
    assert first.session_id == second.session_id and first.prompt == second.prompt
    assert "retry" in _events(ledger, run.run_id)


async def test_repeated_protocol_error_counts_as_failed_iteration(make_task, make_run, ledger):
    task = make_task(max_iterations=2, fallback=False)
    err = PiScript(ok=False, error_kind="protocol", error_message="pi exited with status 2")
    adapter = ScriptedPiAdapter({"implement": [err, err, PiScript(files=[("done.txt", "")])]})
    run, sleep = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval"
    implement_calls = adapter.calls_for("implement")
    assert len(implement_calls) == 3 and sleep.calls == [30.0]
    assert (
        "previous turn ended with an error (pi exited with status 2)" in implement_calls[2].prompt
    )
    first, second = ledger.iterations(run.run_id)
    assert first["agent_ok"] == 0 and second["verify_passed"] == 1


async def test_timeout_fails_the_run(make_task, make_run):
    task = make_task()
    adapter = ScriptedPiAdapter({"implement": PiScript(ok=False, error_kind="timeout")})
    run, _ = make_run(task, adapter)
    outcome = await run.run()
    assert (outcome.status, outcome.failure_reason) == ("failed", "timeout")


async def test_abort_marks_the_run_aborted(make_task, make_run, ledger):
    task = make_task()
    adapter = ScriptedPiAdapter({"implement": PiScript(ok=False, error_kind="aborted")})
    run, _ = make_run(task, adapter)
    outcome = await run.run()
    assert (outcome.status, outcome.failure_reason) == ("aborted", "aborted")
    assert ledger.get_run(run.run_id)["status"] == "aborted"


async def test_auth_failure_fails_before_any_worktree(make_task, make_run, ledger, tmp_project):
    task = make_task()
    adapter = ScriptedPiAdapter()
    run, _ = make_run(task, adapter, auth_check=lambda m: f"{m.model}: not_ready")
    outcome = await run.run()
    assert outcome.status == "failed"
    assert outcome.failure_reason == "auth: openai-codex/gpt-6.1-sol: not_ready"
    assert adapter.calls == [] and outcome.worktree_path is None
    assert not (tmp_project / ".helmraiser" / "worktrees").exists()
    assert ledger.get_run(run.run_id)["status"] == "failed"


async def test_lock_held_by_another_run_is_not_recorded(
    make_task, make_run, ledger, tmp_project, data_dir
):
    task = make_task()
    holder_run_id = "11111111-1111-4111-8111-111111111111"
    state = StateManager(tmp_project / ".helmraiser" / "state.db")
    state.create_run(holder_run_id, "fix-widget")
    assert state.acquire_lock(holder_run_id, "fix-widget", 1)
    run, _ = make_run(task, ScriptedPiAdapter())

    with pytest.raises(TaskLocked) as exc_info:
        await run.run()

    assert exc_info.value.holder_run_id == holder_run_id
    assert ledger.get_run(run.run_id) is None
    assert ledger.events(run.run_id) == []
    assert ledger.get_task(task.task_id)["status"] == "queued"
    assert not (data_dir / "runs" / run.run_id).exists()
    assert state.get_run(run.run_id) is None
    assert state.get_locked_run_id("fix-widget") == holder_run_id


async def test_blocked_with_committed_work_fails_without_services(make_task, make_run):
    adapter = ScriptedPiAdapter(
        {"implement": PiScript(files=[("done.txt", "")], commit=True, final_text="BLOCKED: no key")}
    )
    run, _ = make_run(make_task(), adapter)
    outcome = await run.run()
    assert outcome.failure_reason == "blocked: no key"
    assert len(adapter.calls) == 1


async def test_pi_events_are_mirrored_to_ledger_and_jsonl(make_task, make_run, ledger, data_dir):
    task = make_task()
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(
                files=[("done.txt", "")],
                events=[
                    PiEvent("agent_start", {"type": "agent_start"}),
                    assistant_message_end(0.01),
                    PiEvent("agent_settled", {"type": "agent_settled"}),
                ],
            )
        }
    )
    run, _ = make_run(task, adapter)
    await run.run()
    pi_events = [e for e in ledger.events(run.run_id) if e["source"] == "pi"]
    assert [e["type"] for e in pi_events] == ["agent_start", "message_end", "agent_settled"]
    assert pi_events[0]["session_id"] == f"{run.run_id}-implement"
    assert pi_events[0]["stage"] == "implement"
    lines = (data_dir / "runs" / run.run_id / "events.jsonl").read_text().splitlines()
    mirrored = [json.loads(line) for line in lines]
    assert any(m["event_type"] == "message_end" and m["source"] == "pi" for m in mirrored)


async def test_ledger_skips_pi_records_that_repeat_other_events(
    make_task, make_run, ledger, data_dir
):
    task = make_task()
    end = assistant_message_end(0.01)
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(
                files=[("done.txt", "")],
                events=[
                    PiEvent("message_start", {"type": "message_start", "message": {}}),
                    end,
                    PiEvent("turn_end", {"type": "turn_end", "message": end.raw["message"]}),
                    PiEvent(
                        "agent_end",
                        {"type": "agent_end", "messages": [end.raw["message"]], "willRetry": False},
                    ),
                    PiEvent("agent_settled", {"type": "agent_settled"}),
                ],
            )
        }
    )
    run, _ = make_run(task, adapter)
    await run.run()
    pi_events = [e for e in ledger.events(run.run_id) if e["source"] == "pi"]
    assert [e["type"] for e in pi_events] == ["message_end", "agent_end", "agent_settled"]
    assert pi_events[1]["payload"] == {"type": "agent_end", "willRetry": False}
    lines = (data_dir / "runs" / run.run_id / "events.jsonl").read_text().splitlines()
    mirrored = {json.loads(line)["event_type"] for line in lines}
    assert not mirrored & {"message_start", "turn_end"}


async def test_recording_failure_releases_the_claim(
    make_task, make_run, ledger, tmp_project, monkeypatch
):
    task = make_task()
    run, _ = make_run(task, ScriptedPiAdapter())

    def boom(_row, **kwargs):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(ledger, "claim_queued_run", boom)

    with pytest.raises(RuntimeError, match="ledger down"):
        await run.run()

    state = StateManager(tmp_project / ".helmraiser" / "state.db")
    assert state.get_locked_run_id("fix-widget") is None
    assert ledger.get_task(task.task_id)["status"] == "queued"
