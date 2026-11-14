"""``helmraiser retry`` of a run that failed on a budget: the budgets stay cumulative."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, commands, monitor
from helmraiser.loop import prompts
from helmraiser.loop.runner import GovernedRun
from helmraiser.loop.verify import VerifyResult
from helmraiser.pi.protocol import Usage
from helmraiser.task.loader import render_task
from tests.loop.conftest import FakeClock
from tests.loop.test_evidence_review_merge import verdict_text
from tests.loop.test_rework import hand_back_rework
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

cli = CliRunner()
HEALTHY = [("marker", "test -f done.txt && test ! -f broken.txt")]
REVIEW_PASS = PiScript(final_text=verdict_text("pass"))


@pytest.fixture(autouse=True)
def cli_config(monkeypatch: pytest.MonkeyPatch, config: dict[str, Any]) -> None:
    monkeypatch.setattr(commands, "load_config", lambda: config)
    monkeypatch.setattr(monitor, "load_config", lambda: config)


@pytest.fixture
def retry(data_dir: Path) -> Callable[..., Any]:
    def _retry(run_id: str, *flags: str) -> Any:
        return cli.invoke(app, ["retry", run_id, *flags, "--json", "--data-dir", str(data_dir)])

    return _retry


@pytest.fixture
def resume(config, ledger, data_dir) -> Callable[..., GovernedRun]:
    def _resume(run_id: str, adapter: ScriptedPiAdapter, **overrides: Any) -> GovernedRun:
        return GovernedRun.resume(
            run_id,
            config=config,
            ledger=ledger,
            data_dir=data_dir,
            adapter_factory=lambda state, isolation: adapter,
            pi_version="fake",
            auth_check=lambda model: None,
            **overrides,
        )

    return _resume


@pytest.fixture
def show(data_dir: Path) -> Callable[[str], dict[str, Any]]:
    def _show(run_id: str) -> dict[str, Any]:
        result = cli.invoke(app, ["show", run_id, "--json", "--data-dir", str(data_dir)])
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    return _show


def raise_budget(task, *, verify: str | None = None, **caps):
    """The owner edits the task file; only ``retry`` makes it the run's snapshot."""
    raised = replace(task, budget=replace(task.budget, **caps))
    if verify is not None:
        raised = replace(raised, verify=[replace(task.verify[0], run=verify)])
    task.source_path.write_text(render_task(raised), encoding="utf-8")
    return raised


def budget_events(ledger, run_id: str) -> list[dict[str, Any]]:
    return [e["payload"] for e in ledger.events(run_id, types=("budget_exceeded",))]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def failing_sessions() -> ScriptedPiAdapter:
    """Every implement session changes the branch and leaves verify failing."""
    n = 0

    def touch(cwd: Path) -> None:
        nonlocal n
        n += 1
        (cwd / "broken.txt").write_text(f"attempt {n}\n")

    return ScriptedPiAdapter({"implement": PiScript(action=touch), "review": REVIEW_PASS})


async def stopped_at_rework_allowance(make_task, make_run, ledger, resume):
    """Run ``0895335a``: max 9, reworks at 4 and 6 iterations, stopped at 8 by the second
    rework's allowance, with finished code on its branch and verify failing on a late edit."""
    task = make_task(HEALTHY, max_iterations=9, fallback=False)
    first, _ = make_run(
        task,
        ScriptedPiAdapter(
            {
                "implement": [
                    PiScript(files=[("a.txt", "1")]),
                    PiScript(files=[("a.txt", "2")]),
                    PiScript(files=[("a.txt", "3")]),
                    PiScript(files=[("done.txt", "done")]),
                ],
                "review": REVIEW_PASS,
            }
        ),
    )
    assert (await first.run()).status == "awaiting_approval"
    run_id = first.run_id
    assert len(ledger.iterations(run_id)) == 4

    hand_back_rework(ledger, run_id, task)
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(files=[("broken.txt", "x")]),
                PiScript(action=lambda cwd: (cwd / "broken.txt").unlink()),
            ],
            "review": REVIEW_PASS,
        }
    )
    assert (await resume(run_id, adapter).run()).status == "awaiting_approval"
    assert len(ledger.iterations(run_id)) == 6

    hand_back_rework(ledger, run_id, task)
    outcome = await resume(run_id, failing_sessions()).run()
    assert (outcome.status, outcome.failure_reason) == ("failed", "budget:iterations")
    return task, run_id, outcome


# ── Part 2: the event names the limit that fired ──────────────────────────


async def test_the_rework_allowance_stops_a_run_below_max_iterations(
    make_task, make_run, ledger, resume
):
    _, run_id, outcome = await stopped_at_rework_allowance(make_task, make_run, ledger, resume)

    assert outcome.iterations == 8
    stop = budget_events(ledger, run_id)[-1]
    assert (stop["kind"], stop["iterations"], stop["limit"], stop["limit_source"]) == (
        "iterations",
        8,
        8,
        "rework_allowance",
    )
    assert ledger.get_run(run_id)["failure_reason"] == "budget:iterations"


async def test_show_reports_the_limit_the_runner_stopped_at(
    make_task, make_run, ledger, resume, show
):
    task, run_id, _ = await stopped_at_rework_allowance(make_task, make_run, ledger, resume)

    limits = show(run_id)["limits"]

    stop = budget_events(ledger, run_id)[-1]
    assert (
        limits["iterations"]
        == {
            "used": stop["iterations"],
            "limit": stop["limit"],
            "limit_source": stop["limit_source"],
        }
        == {"used": 8, "limit": 8, "limit_source": "rework_allowance"}
    )
    extras = ledger.reworks(run_id)
    assert task.budget.max_iterations == 9 and len(extras) == 2
    assert limits["cost_usd"]["limit"] == pytest.approx(
        task.budget.max_cost_usd + sum(r["extra_cost_usd"] for r in extras), abs=1e-3
    )
    assert limits["minutes"]["limit"] == pytest.approx(
        task.budget.max_minutes + sum(r["extra_minutes"] for r in extras), abs=1e-2
    )


async def test_show_follows_the_limit_through_a_budget_retry(
    make_task, make_run, ledger, resume, retry, show
):
    task, run_id, _ = await stopped_at_rework_allowance(make_task, make_run, ledger, resume)

    assert retry(run_id, "--implement").exit_code == 0
    handed_back = show(run_id)
    assert handed_back["status"] == "resuming"
    assert handed_back["limits"]["iterations"] == {
        "used": 8,
        "limit": 9,
        "limit_source": "max_iterations",
    }

    outcome = await resume(run_id, failing_sessions()).run()

    assert outcome.failure_reason == "budget:iterations"
    stop = budget_events(ledger, run_id)[-1]
    assert (
        show(run_id)["limits"]["iterations"]
        == {
            "used": stop["iterations"],
            "limit": stop["limit"],
            "limit_source": stop["limit_source"],
        }
        == {"used": 9, "limit": 9, "limit_source": "max_iterations"}
    )


async def test_show_applies_a_rework_ceiling_from_hand_back_to_the_end_of_its_segment(
    make_task, make_run, ledger, resume, show
):
    task = make_task(HEALTHY, max_iterations=9, fallback=False)
    first, _ = make_run(
        task,
        ScriptedPiAdapter(
            {"implement": PiScript(files=[("done.txt", "done")]), "review": REVIEW_PASS}
        ),
    )
    assert (await first.run()).status == "awaiting_approval"
    run_id = first.run_id
    assert show(run_id)["limits"]["iterations"] == {
        "used": 1,
        "limit": 9,
        "limit_source": "max_iterations",
    }

    hand_back_rework(ledger, run_id, task)
    assert show(run_id)["limits"]["iterations"] == {
        "used": 1,
        "limit": 3,
        "limit_source": "rework_allowance",
    }

    adapter = ScriptedPiAdapter(
        {"implement": PiScript(files=[("more.txt", "x")]), "review": REVIEW_PASS}
    )
    assert (await resume(run_id, adapter).run()).status == "awaiting_approval"
    reworked = show(run_id)
    assert reworked["rework_rounds"] == 1 and reworked["pending_approval"] == "merge"
    assert reworked["limits"]["iterations"] == {
        "used": 2,
        "limit": 3,
        "limit_source": "rework_allowance",
    }


def test_log_shows_the_limit_that_fired():
    summary = monitor.summarize_payload(
        "budget_exceeded",
        {
            "kind": "iterations",
            "cost_usd": 1.5,
            "minutes": 12.25,
            "iterations": 8,
            "limit": 8,
            "limit_source": "rework_allowance",
        },
    )
    assert summary == "iterations: 8 used, limit 8 (rework_allowance), cost_usd=1.5, minutes=12.25"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "cost", "cost_usd": 2.5, "minutes": 1.0},
        {"kind": "iterations", "cost_usd": 2.5, "minutes": 1.0},  # recorded before limit_source
    ],
)
def test_log_keeps_other_budget_events_as_they_were(payload):
    assert monitor.summarize_payload("budget_exceeded", payload).startswith(
        f'kind="{payload["kind"]}", cost_usd=2.5'
    )


async def test_cost_and_time_stops_do_not_claim_an_iteration_limit(make_task, make_run, ledger):
    task = make_task(HEALTHY, max_cost_usd=1)
    run, _ = make_run(task, ScriptedPiAdapter({"implement": PiScript(usage=Usage(1, 1, 1.5))}))
    assert (await run.run()).failure_reason == "budget:cost"
    stop = budget_events(ledger, run.run_id)[-1]
    assert stop["kind"] == "cost" and "limit_source" not in stop and "iterations" not in stop


# ── the 0895335a regression ───────────────────────────────────────────────


async def test_retry_implement_after_raising_max_iterations_gets_only_what_remains(
    make_task, make_run, ledger, resume, retry
):
    task, run_id, _ = await stopped_at_rework_allowance(make_task, make_run, ledger, resume)
    raise_budget(task, max_iterations=12)

    result = retry(run_id, "--implement")
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["retried"] == "budget:iterations" and out["stage"] == "implement"

    adapter = failing_sessions()
    run = resume(run_id, adapter)
    outcome = await run.run()

    assert (outcome.status, outcome.failure_reason) == ("failed", "budget:iterations")
    assert len(adapter.calls_for("implement")) == 4  # 12 in all, not 12 more
    assert [i["n"] for i in ledger.iterations(run_id)] == list(range(1, 13))
    stop = budget_events(ledger, run_id)[-1]
    assert (stop["iterations"], stop["limit"], stop["limit_source"]) == (12, 12, "max_iterations")
    assert "Budget: 4 verify iterations" in run.rundir.directives_md.read_text()
    resumes = ledger.events(run_id, types=("run_resume",))
    assert resumes[-1]["payload"]["from"] == "retry" and resumes[-1]["payload"]["budget_retry"]


async def test_retry_is_refused_with_the_file_unchanged_after_9_of_9(
    make_task, make_run, ledger, resume, retry
):
    _, run_id, _ = await stopped_at_rework_allowance(make_task, make_run, ledger, resume)
    # The rework allowance fired at 8, so the unchanged file still leaves 1 iteration.
    assert retry(run_id, "--implement").exit_code == 0
    adapter = failing_sessions()
    outcome = await resume(run_id, adapter).run()
    assert outcome.failure_reason == "budget:iterations"
    assert len(adapter.calls_for("implement")) == 1
    snapshot = ledger.get_run(run_id)

    result = retry(run_id)

    assert result.exit_code == 2, result.output
    assert "iterations: 9 used, limit 9 (max_iterations)" in json.loads(result.output)["error"]
    assert ledger.get_run(run_id) == snapshot
    assert ledger.events(run_id, types=("handed_back",))[-1]["payload"]["stage"] == "implement"


async def test_plain_retry_goes_to_verify_evidence_and_review_without_iterations(
    make_task, make_run, ledger, resume, retry
):
    task, run_id, _ = await stopped_at_rework_allowance(make_task, make_run, ledger, resume)
    # The late edit that kept verify failing is what the owner corrects for.
    raise_budget(task, max_iterations=12, verify="test -f done.txt")

    result = retry(run_id)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["stage"] == "verify"

    adapter = ScriptedPiAdapter({"review": REVIEW_PASS})
    outcome = await resume(run_id, adapter).run()

    assert outcome.status == "awaiting_approval" and outcome.pending_approval == "merge"
    assert adapter.calls_for("implement") == []
    assert adapter.calls_for("review")
    assert len(ledger.iterations(run_id)) == 8


async def test_fixes_after_a_plain_budget_retry_stay_bounded_by_what_remains(
    make_task, make_run, ledger, resume, retry
):
    task, run_id, _ = await stopped_at_rework_allowance(make_task, make_run, ledger, resume)
    raise_budget(task, max_iterations=12)
    assert retry(run_id).exit_code == 0
    outcome = await resume(run_id, ScriptedPiAdapter()).run()
    assert outcome.failure_reason == "verify_failed: marker"

    assert retry(run_id, "--implement").exit_code == 0
    adapter = failing_sessions()
    outcome = await resume(run_id, adapter).run()

    assert outcome.failure_reason == "budget:iterations"
    assert len(adapter.calls_for("implement")) == 4
    assert len(ledger.iterations(run_id)) == 12


async def test_review_fixes_after_a_budget_retry_are_bounded_too(
    make_task, make_run, ledger, resume, retry
):
    task, run_id, _ = await stopped_at_rework_allowance(make_task, make_run, ledger, resume)
    raise_budget(task, max_iterations=10)
    assert retry(run_id, "--implement").exit_code == 0

    severe = PiScript(
        final_text=verdict_text(
            "concerns", [{"severity": "severe", "file": "a.txt", "line": 1, "note": "wrong"}]
        )
    )
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(action=lambda cwd: (cwd / "broken.txt").unlink()),
                PiScript(files=[("broken.txt", "x")]),
                PiScript(files=[("never.txt", "x")]),
            ],
            "review": severe,
        }
    )
    outcome = await resume(run_id, adapter).run()

    assert len(adapter.calls_for("implement")) == 2
    assert len(ledger.iterations(run_id)) == 10
    assert outcome.status == "awaiting_approval"


# ── refused before anything is written ────────────────────────────────────


async def test_retry_is_refused_when_the_budget_has_no_room(make_task, make_run, ledger, retry):
    task = make_task(HEALTHY, max_cost_usd=1, max_iterations=3)
    run, _ = make_run(task, ScriptedPiAdapter({"implement": PiScript(usage=Usage(1, 1, 1.25))}))
    assert (await run.run()).failure_reason == "budget:cost"
    before = ledger.get_task(task.task_id)["snapshot"]
    task_md = run.rundir.task_md.read_text()
    # A raised file is not re-read when a cap stays out of reach.
    raise_budget(task, max_iterations=9)

    result = retry(run.run_id)

    assert result.exit_code == 2, result.output
    error = json.loads(result.output)
    assert list(error) == ["error"]
    assert "cost: $1.25 used, limit $1.00 (max_cost_usd)" in error["error"]
    assert str(task.source_path) in error["error"]
    assert "iterations" not in error["error"].split(str(task.source_path))[0]
    assert ledger.get_task(task.task_id)["snapshot"] == before
    assert run.rundir.task_md.read_text() == task_md
    row = ledger.get_run(run.run_id)
    assert (row["status"], row["failure_reason"]) == ("failed", "budget:cost")
    assert ledger.events(run.run_id, types=("handed_back",)) == []


async def test_an_invalid_task_file_is_reported_once_and_not_as_a_budget_error(
    make_task, make_run, retry
):
    task = make_task(HEALTHY, max_cost_usd=1)
    run, _ = make_run(task, ScriptedPiAdapter({"implement": PiScript(usage=Usage(1, 1, 1.25))}))
    await run.run()
    task.source_path.write_text("not a task")

    result = retry(run.run_id)

    assert result.exit_code == 2, result.output
    assert "no budget left" not in result.output


# ── cost, and uncommitted work ────────────────────────────────────────────


async def cost_stopped_with_uncommitted_work(make_task, make_run, **task_options):
    """The session wrote ``done.txt`` and crossed the cost cap before it could commit."""
    task = make_task(HEALTHY, max_cost_usd=1, **task_options)
    run, _ = make_run(
        task,
        ScriptedPiAdapter(
            {"implement": PiScript(files=[("done.txt", "done")], usage=Usage(1, 1, 1.25))}
        ),
    )
    outcome = await run.run()
    assert outcome.failure_reason == "budget:cost"
    worktree = Path(outcome.worktree_path)
    assert git(worktree, "rev-parse", "HEAD") == outcome.base_commit
    assert "done.txt" in git(worktree, "status", "--porcelain")
    return task, run, outcome, worktree


async def test_plain_budget_retry_commits_uncommitted_work_and_reviews_that_commit(
    make_task, make_run, ledger, resume, retry
):
    task, first, stopped, worktree = await cost_stopped_with_uncommitted_work(make_task, make_run)
    raise_budget(task, max_cost_usd=2)

    result = retry(first.run_id)
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["stage"] == "verify" and "restarted_without_commits" not in out

    run = resume(first.run_id, ScriptedPiAdapter({"review": REVIEW_PASS}))
    outcome = await run.run()

    assert outcome.status == "awaiting_approval"
    assert git(worktree, "status", "--porcelain") == ""
    assert git(worktree, "rev-parse", "HEAD") == outcome.head_commit != stopped.base_commit
    assert "uncommitted work of" in git(worktree, "log", "-1", "--format=%s")
    assert "done.txt" in run.rundir.diff_patch.read_text()
    assert outcome.cost_usd == pytest.approx(1.25)


async def test_retry_implement_commits_uncommitted_work_before_the_first_session(
    make_task, make_run, ledger, resume, retry
):
    task, first, stopped, worktree = await cost_stopped_with_uncommitted_work(make_task, make_run)
    raise_budget(task, max_cost_usd=2, verify="test -f more.txt")
    result = retry(first.run_id, "--implement")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["stage"] == "implement"
    seen: list[str] = []

    def look(cwd: Path) -> None:
        seen.append(git(cwd, "status", "--porcelain"))
        seen.append(git(cwd, "show", "--stat", "--format=%s", "HEAD"))

    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(action=look, files=[("more.txt", "more")]),
            "review": REVIEW_PASS,
        }
    )
    outcome = await resume(first.run_id, adapter).run()

    # ``files`` are written before ``action``: only they may be dirty when the session looks.
    assert seen[0] == "?? more.txt"
    assert "uncommitted work of" in seen[1] and "done.txt" in seen[1]
    assert outcome.status == "awaiting_approval"
    prompt = adapter.calls_for("implement")[0].prompt
    assert "budget:cost" in prompt and "Verify failed" not in prompt
    assert "finish what is still missing" in prompt


async def test_retry_implement_with_nothing_left_to_do_verifies_the_branch_as_it_stands(
    make_task, make_run, ledger, resume, retry
):
    task, first, _, worktree = await cost_stopped_with_uncommitted_work(make_task, make_run)
    raise_budget(task, max_cost_usd=2)
    assert retry(first.run_id, "--implement").exit_code == 0

    adapter = ScriptedPiAdapter({"implement": PiScript(), "review": REVIEW_PASS})
    outcome = await resume(first.run_id, adapter).run()

    assert outcome.status == "awaiting_approval"
    assert len(adapter.calls_for("implement")) == 1
    assert ledger.events(first.run_id, types=("no_changes",)) == []
    assert ledger.iterations(first.run_id)[-1]["verify_passed"]


async def test_no_changes_after_the_first_session_of_a_budget_retry_is_still_no_progress(
    make_task, make_run, ledger, resume, retry
):
    task, first, _, _ = await cost_stopped_with_uncommitted_work(make_task, make_run)
    raise_budget(
        task,
        max_cost_usd=2,
        max_iterations=3,
        verify="test -f never.txt",
    )
    assert retry(first.run_id, "--implement").exit_code == 0

    adapter = ScriptedPiAdapter({"implement": PiScript()})
    outcome = await resume(first.run_id, adapter).run()

    assert outcome.failure_reason == "budget:iterations"
    assert len(adapter.calls_for("implement")) == 2  # 1 of 3 was used
    assert len(ledger.events(first.run_id, types=("no_changes",))) == 1


async def test_a_raised_cost_cap_counts_what_the_run_already_spent(
    make_task, make_run, ledger, resume, retry
):
    task, first, _, _ = await cost_stopped_with_uncommitted_work(make_task, make_run)
    raise_budget(task, max_cost_usd=2, verify="test -f more.txt")
    assert retry(first.run_id, "--implement").exit_code == 0

    run = resume(first.run_id, ScriptedPiAdapter({"implement": PiScript(usage=Usage(1, 1, 1))}))
    outcome = await run.run()

    assert outcome.failure_reason == "budget:cost"
    assert outcome.cost_usd == pytest.approx(2.25)
    # The directive of that session named what was left, not the whole cap.
    assert "$0.75." in run.rundir.directives_md.read_text()


async def test_cost_room_includes_the_extra_cost_of_reworks(make_task, make_run, ledger, retry):
    task = make_task(HEALTHY, max_cost_usd=1, max_iterations=2)
    run, _ = make_run(task, ScriptedPiAdapter({"implement": PiScript(usage=Usage(1, 1, 1.25))}))
    await run.run()
    ledger.append_event(
        run.run_id, "helmraiser", "rework_requested", {"extra_cost_usd": 1.0, "extra_minutes": 45.0}
    )

    result = retry(run.run_id)

    assert result.exit_code == 0, result.output


# ── time ──────────────────────────────────────────────────────────────────


class Timed(ScriptedPiAdapter):
    def __init__(self, clock: FakeClock, seconds: float, scripts: dict[str, Any]):
        super().__init__(scripts)
        self.clock, self.seconds, self.timeouts = clock, seconds, []

    async def run(self, req, on_event, run_id):
        if req.stage == "implement":
            self.timeouts.append(req.timeout_s)
            self.clock.advance(self.seconds)
        return await super().run(req, on_event, run_id)


async def time_stopped(make_task, make_run, clock):
    task = make_task(HEALTHY, max_minutes=3, fallback=False)
    run, _ = make_run(
        task,
        Timed(clock, 240, {"implement": PiScript(files=[("done.txt", "done")])}),
        clock=clock,
    )
    assert (await run.run()).failure_reason == "budget:time"
    clock.advance(100000)  # waiting for the owner is not active time
    return task, run


async def test_time_retry_is_refused_on_cumulative_minutes(make_task, make_run, retry):
    clock = FakeClock()
    task, run = await time_stopped(make_task, make_run, clock)

    result = retry(run.run_id)

    assert result.exit_code == 2, result.output
    error = json.loads(result.output)["error"]
    assert "time: 4.0 min used, limit 3.0 min (max_minutes)" in error
    assert str(task.source_path) in error


async def test_time_budget_stays_cumulative_without_reworks(
    make_task, make_run, ledger, resume, retry
):
    clock = FakeClock()
    task, first = await time_stopped(make_task, make_run, clock)
    raise_budget(task, max_minutes=6, verify="test -f more.txt")
    assert retry(first.run_id, "--implement").exit_code == 0

    adapter = Timed(clock, 150, {"implement": PiScript()})
    run = resume(first.run_id, adapter, clock=clock)
    outcome = await run.run()

    assert run.guard.prior_minutes == pytest.approx(4)
    assert outcome.failure_reason == "budget:time"  # 2.5 more minutes is over 2 remaining
    assert outcome.minutes == pytest.approx(6.5)
    assert adapter.timeouts[0] <= 120  # the session gets at most what remains
    assert "2.0 minutes" in run.rundir.directives_md.read_text()
    # Each segment still records only its own active time.
    ends = ledger.events(first.run_id, types=("run_end",))
    assert [e["payload"]["active_minutes"] for e in ends] == pytest.approx([4, 2.5])


async def test_the_first_models_share_comes_from_the_remaining_time(
    make_task, make_run, ledger, resume, retry
):
    clock = FakeClock()
    task, first = await time_stopped(make_task, make_run, clock)
    raise_budget(task, max_minutes=14, verify="test -f more.txt")
    assert retry(first.run_id, "--implement").exit_code == 0

    adapter = Timed(clock, 0, {"implement": PiScript(files=[("more.txt", "x")])})
    run = resume(first.run_id, adapter, clock=clock)
    await run.run()

    # 60% of the 10 minutes left, not 60% of 14.
    assert adapter.timeouts[0] == int(run.first_model_time_share * 10 * 60)


async def test_plain_budget_retry_reports_cumulative_minutes(
    make_task, make_run, ledger, resume, retry
):
    clock = FakeClock()
    task, first = await time_stopped(make_task, make_run, clock)
    raise_budget(task, max_minutes=6)
    assert retry(first.run_id).exit_code == 0

    run = resume(first.run_id, ScriptedPiAdapter({"review": REVIEW_PASS}), clock=clock)
    outcome = await run.run()

    assert outcome.status == "awaiting_approval"
    assert outcome.minutes == pytest.approx(4)


# ── restart without commits ───────────────────────────────────────────────


async def test_restart_without_commits_keeps_the_budgets_cumulative(
    make_task, make_run, ledger, resume, retry
):
    task = make_task(HEALTHY, max_cost_usd=1, max_iterations=3)
    run, _ = make_run(task, ScriptedPiAdapter({"implement": PiScript(usage=Usage(1, 1, 1.25))}))
    assert (await run.run()).failure_reason == "budget:cost"
    raise_budget(task, max_cost_usd=2)

    result = retry(run.run_id)
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["stage"] == "start" and out["restarted_without_commits"] is True

    adapter = ScriptedPiAdapter({"implement": PiScript()})
    outcome = await resume(run.run_id, adapter).run()

    assert outcome.failure_reason == "budget:iterations"
    assert len(adapter.calls_for("implement")) == 2  # 1 of 3 was used
    assert "Nothing is committed" in adapter.calls_for("implement")[0].prompt


# ── the approval policy ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("policy", "status"), [("auto", "landed"), ("review", "awaiting_approval")]
)
async def test_a_budget_retry_does_not_change_the_approval_policy(
    make_task, make_run, ledger, resume, policy, status
):
    """Handed back directly: the CLI refuses ``policy: auto`` under ``isolation: none``."""
    task, first, _, _ = await cost_stopped_with_uncommitted_work(make_task, make_run, policy=policy)
    raised = raise_budget(task, max_cost_usd=2)
    ledger.set_task_snapshot(task.task_id, raised.source_path.read_text())
    assert ledger.hand_back(first.run_id, "verify")

    outcome = await resume(first.run_id, ScriptedPiAdapter({"review": REVIEW_PASS})).run()

    assert outcome.status == status


# ── prompts ───────────────────────────────────────────────────────────────


def verify_result(passed: bool) -> VerifyResult:
    return VerifyResult(
        name="tests", passed=passed, exit_code=0 if passed else 1, seconds=0.0, tail="output"
    )


@pytest.mark.parametrize("results", [[], [verify_result(True)]])
def test_retry_implement_prompt_without_a_failure_asks_to_continue(results):
    prompt = prompts.retry_implement_prompt(results, "budget:iterations")

    assert "budget:iterations" in prompt
    assert "read it again" in prompt
    assert "finish what is still missing" in prompt
    assert "Verify failed" not in prompt and "last verify results" not in prompt


def test_retry_implement_prompt_with_a_failure_keeps_the_verify_feedback():
    prompt = prompts.retry_implement_prompt([verify_result(False)], "budget:cost")

    assert "these are the last verify results" in prompt and "### tests (exit 1)" in prompt
