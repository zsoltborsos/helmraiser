from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from helmraiser.ledger.db import Ledger
from helmraiser.loop import runner
from helmraiser.loop.prompts import review_feedback_prompt
from helmraiser.loop.report import build_report, render_report
from helmraiser.pi.protocol import Usage
from tests.loop.test_evidence_review_merge import verdict_text
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

SEVERE = [{"severity": "severe", "file": "done.txt", "line": 1, "note": "race remains"}]
MINOR = [{"severity": "minor", "file": "done.txt", "line": 2, "note": "clarify output"}]

FEEDBACK_OPENING = (
    "The independent review found issues. Fix every finding below, add a regression test "
    "for each where feasible, stay within the task's scope, and commit the fix."
)


@pytest.mark.parametrize(
    "findings,sections",
    [
        (SEVERE, "\n\n## Severe findings\n- done.txt:1: race remains"),
        (MINOR, "\n\n## Minor findings\n- done.txt:2: clarify output"),
        (
            MINOR + SEVERE,
            "\n\n## Severe findings\n- done.txt:1: race remains"
            "\n\n## Minor findings\n- done.txt:2: clarify output",
        ),
    ],
    ids=["severe", "minor", "mixed"],
)
def test_review_feedback_prompt_requires_every_finding(findings, sections):
    prompt = review_feedback_prompt(findings)
    assert prompt == FEEDBACK_OPENING + sections
    assert ("## Severe findings" in prompt) == (SEVERE[0] in findings)
    assert ("## Minor findings" in prompt) == (MINOR[0] in findings)
    assert "optional" not in prompt


def test_review_feedback_prompt_without_findings():
    assert review_feedback_prompt([]) == FEEDBACK_OPENING


async def test_severe_review_gets_fix_round_and_second_review(
    make_task, make_run, ledger: Ledger, data_dir: Path
):
    task = make_task()
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(files=[("done.txt", "first\n")]),
                PiScript(files=[("fixed.txt", "fixed\n")]),
            ],
            "review": [
                PiScript(final_text=verdict_text("concerns", SEVERE)),
                PiScript(final_text=verdict_text("pass")),
            ],
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.review_rounds == 1
    assert outcome.verdict["verdict"] == "pass"
    assert len(adapter.calls_for("review")) == 2
    fix = adapter.calls_for("implement")[1]
    assert fix.session_id == f"{run.run_id}-implement"
    assert "done.txt:1" in fix.prompt and "race remains" in fix.prompt
    run_dir = data_dir / "runs" / run.run_id
    assert (run_dir / "review-1.md").is_file()
    assert '"pass"' in (run_dir / "review.md").read_text()
    event = next(e for e in ledger.events(run.run_id) if e["type"] == "review_feedback")
    assert event["payload"]["severe"] == 1


@pytest.mark.parametrize("verdict", ["concerns", "pass"])
async def test_minor_review_gets_fix_round_and_second_review(
    make_task, make_run, ledger, data_dir, verdict
):
    first_review = verdict_text(verdict, MINOR)
    clean_review = verdict_text("pass")
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(files=[("done.txt", "first\n")]),
                PiScript(files=[("done.txt", "clear output\n")]),
            ],
            "review": [
                PiScript(final_text=first_review),
                PiScript(final_text=clean_review),
            ],
        }
    )
    run, _ = make_run(make_task(), adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval" and outcome.failure_reason is None
    assert outcome.review_rounds == 1 and outcome.iterations == 2
    assert not outcome.escalated
    assert outcome.verdict["parsed"] and outcome.verdict["verdict"] == "pass"
    assert outcome.verdict["findings"] == []
    assert len(adapter.calls_for("implement")) == len(adapter.calls_for("review")) == 2
    initial, fix = adapter.calls_for("implement")
    assert fix.model == initial.model
    assert fix.model.render() == "openai-codex/gpt-6.1-sol:medium"
    assert fix.session_id == initial.session_id == f"{run.run_id}-implement"
    assert "done.txt:2: clarify output" in fix.prompt
    assert (Path(outcome.worktree_path) / "done.txt").read_text() == "clear output\n"
    run_dir = data_dir / "runs" / run.run_id
    assert (run_dir / "review-1.md").read_text() == first_review
    assert (run_dir / "review.md").read_text() == clean_review
    assert outcome.verify_results and all(result["passed"] for result in outcome.verify_results)
    events = ledger.events(run.run_id)
    feedback = [e for e in events if e["type"] == "review_feedback"]
    assert len(feedback) == 1 and feedback[0]["payload"]["severe"] == 0
    assert not [e for e in events if e["type"] == "escalated"]


@pytest.mark.parametrize("limit", [1, 2])
async def test_minor_feedback_respects_limit_without_fallback(
    make_task, make_run, config, ledger, limit
):
    config["review_feedback_rounds"] = limit
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(files=[("done.txt", f"version {iteration}\n")])
                for iteration in range(limit + 1)
            ],
            "review": PiScript(final_text=verdict_text("concerns", MINOR)),
        }
    )
    task = make_task()
    assert task.models["fallback"] != task.models["implement"]
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval" and outcome.failure_reason is None
    assert outcome.review_rounds == limit and outcome.iterations == limit + 1
    assert outcome.verdict["verdict"] == "concerns" and outcome.verdict["findings"] == MINOR
    assert not outcome.escalated
    assert len(adapter.calls_for("implement")) == len(adapter.calls_for("review")) == limit + 1
    for call in adapter.calls_for("implement"):
        assert call.model == task.models["implement"]
        assert call.session_id == f"{run.run_id}-implement"
    assert (Path(outcome.worktree_path) / "done.txt").read_text() == f"version {limit}\n"
    assert outcome.verify_results and all(result["passed"] for result in outcome.verify_results)
    events = ledger.events(run.run_id)
    feedback = [e for e in events if e["type"] == "review_feedback"]
    assert len(feedback) == limit
    assert all(e["payload"]["severe"] == 0 for e in feedback)
    assert not [e for e in events if e["type"] == "escalated"]


async def test_remaining_severe_escalates_fallback_for_review(make_task, make_run, ledger):
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(files=[("done.txt", "first\n")]),
                PiScript(files=[("fixed.txt", "fixed\n")]),
                PiScript(files=[("fallback.txt", "fallback\n")]),
            ],
            "review": [
                PiScript(final_text=verdict_text("concerns", SEVERE)),
                PiScript(final_text=verdict_text("concerns", SEVERE)),
                PiScript(final_text=verdict_text("pass")),
            ],
        }
    )
    run, _ = make_run(make_task(), adapter)

    outcome = await run.run()

    assert outcome.review_rounds == 2 and outcome.escalated
    assert len(adapter.calls_for("review")) == 3
    fallback = adapter.calls_for("implement")[2]
    assert fallback.model.render() == "openai-codex/gpt-6-astra:high"
    assert fallback.session_id == f"{run.run_id}-implement-2"
    assert "race remains" in fallback.prompt
    event = next(e for e in ledger.events(run.run_id) if e["type"] == "escalated")
    assert event["payload"]["reason"] == "review"


@pytest.mark.parametrize("findings", [SEVERE, MINOR], ids=["severe", "minor"])
async def test_failed_review_fix_is_rolled_back(make_task, make_run, ledger, findings):
    task = make_task(verify=[("clean", "test ! -f broken.txt")], max_iterations=2)
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(files=[("done.txt", "first\n")]),
                PiScript(files=[("broken.txt", "bad\n")]),
                PiScript(files=[("broken.txt", "bad\n")]),
            ],
            "review": PiScript(final_text=verdict_text("concerns", findings)),
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval" and outcome.failure_reason is None
    assert outcome.review_rounds == 1 and outcome.verdict["findings"] == findings
    assert not (Path(outcome.worktree_path) / "broken.txt").exists()
    abandoned = next(e for e in ledger.events(run.run_id) if e["type"] == "review_round_abandoned")
    assert abandoned["payload"]["reason"] == "verify_exhausted"


@pytest.mark.parametrize("repair", [False, True])
async def test_review_fix_scope_feedback_and_rollback(
    make_task, make_run, ledger, monkeypatch, repair
):
    class RepairAdapter(ScriptedPiAdapter):
        async def run(self, req, on_event, run_id):
            if repair and req.stage == "implement" and len(self.calls_for("implement")) == 2:
                (req.cwd / "forbidden.txt").unlink()
            return await super().run(req, on_event, run_id)

    spy = Mock(wraps=runner.run_verify)
    monkeypatch.setattr(runner, "run_verify", spy)
    task = make_task(paths=("done.txt",), max_iterations=4, fallback=False)
    adapter = RepairAdapter(
        {
            "implement": [
                PiScript(files=[("done.txt", "first")]),
                PiScript(files=[("forbidden.txt", "bad")]),
                PiScript(files=[("done.txt", "fixed")]) if repair else PiScript(),
            ],
            "review": [
                PiScript(final_text=verdict_text("concerns", SEVERE)),
                PiScript(final_text=verdict_text("pass")),
            ],
        }
    )
    run, _ = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval"
    assert spy.call_count == (2 if repair else 1)
    assert len(adapter.calls_for("review")) == (2 if repair else 1)
    assert "forbidden.txt" in adapter.calls_for("implement")[2].prompt
    assert "done.txt" in adapter.calls_for("implement")[2].prompt
    assert (
        adapter.calls_for("implement")[1].session_id == adapter.calls_for("implement")[2].session_id
    )
    assert not (run.worktree / "forbidden.txt").exists()
    assert (run.worktree / "done.txt").read_text() == ("fixed" if repair else "first")
    events = [e for e in ledger.events(run.run_id) if e["type"] == "path_scope_failed"]
    assert len(events) == (1 if repair else 2)
    if not repair:
        abandoned = next(
            e for e in ledger.events(run.run_id) if e["type"] == "review_round_abandoned"
        )
        assert abandoned["payload"]["reason"] == "verify_exhausted"
    for text in (
        run.rundir.summary_md.read_text(),
        render_report(build_report(ledger, run.data_dir, 86400, config=run.config), "24h"),
    ):
        assert "Task path scope failures" in text and "forbidden.txt" in text


@pytest.mark.parametrize("findings", [SEVERE, MINOR], ids=["severe", "minor"])
async def test_budget_during_review_fix_is_rolled_back(make_task, make_run, ledger, findings):
    task = make_task(max_cost_usd=0.01)
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(files=[("done.txt", "first\n")]),
                PiScript(files=[("fixed.txt", "fixed\n")], usage=Usage(cost_usd=0.02)),
            ],
            "review": PiScript(final_text=verdict_text("concerns", findings)),
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval" and outcome.failure_reason is None
    assert not (Path(outcome.worktree_path) / "fixed.txt").exists()
    abandoned = next(e for e in ledger.events(run.run_id) if e["type"] == "review_round_abandoned")
    assert abandoned["payload"]["reason"] == "budget:cost"


async def test_already_escalated_run_fixes_on_fallback(make_task, make_run, ledger):
    task = make_task(max_iterations=2)
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(),
                PiScript(),
                PiScript(files=[("done.txt", "fallback\n")]),
                PiScript(files=[("fixed.txt", "fixed\n")]),
            ],
            "review": [
                PiScript(final_text=verdict_text("concerns", SEVERE)),
                PiScript(final_text=verdict_text("pass")),
            ],
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.escalated and outcome.review_rounds == 1
    fix = adapter.calls_for("implement")[-1]
    assert fix.model.render() == "openai-codex/gpt-6-astra:high"
    assert fix.session_id == f"{run.run_id}-implement-2"
    assert len([e for e in ledger.events(run.run_id) if e["type"] == "escalated"]) == 1


@pytest.mark.parametrize(
    "verdict,findings",
    [("concerns", SEVERE), ("concerns", MINOR), ("pass", MINOR)],
    ids=["severe", "minor-concerns", "minor-pass"],
)
async def test_review_feedback_can_be_disabled(
    make_task, make_run, config, ledger, verdict, findings
):
    config["review_feedback_rounds"] = 0
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "first\n")]),
            "review": PiScript(final_text=verdict_text(verdict, findings)),
        }
    )
    run, _ = make_run(make_task(), adapter)

    outcome = await run.run()

    assert outcome.review_rounds == 0 and not outcome.escalated
    assert len(adapter.calls_for("implement")) == len(adapter.calls_for("review")) == 1
    assert not [
        e for e in ledger.events(run.run_id) if e["type"] in {"review_feedback", "escalated"}
    ]


@pytest.mark.parametrize(
    "findings", [SEVERE, MINOR, SEVERE + MINOR], ids=["severe", "minor", "mixed"]
)
async def test_uncertain_verdict_with_findings_gets_no_round(make_task, make_run, ledger, findings):
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "first\n")]),
            "review": PiScript(final_text=verdict_text("uncertain", findings)),
        }
    )
    run, _ = make_run(make_task(), adapter)

    outcome = await run.run()

    assert outcome.review_rounds == 0 and not outcome.escalated
    assert len(adapter.calls_for("review")) == 1
    assert len(adapter.calls_for("implement")) == 1


async def test_unparsed_review_gets_no_round(make_task, make_run, ledger):
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "first\n")]),
            "review": PiScript(final_text="Could not produce a structured review."),
        }
    )
    run, _ = make_run(make_task(), adapter)

    outcome = await run.run()

    assert not outcome.verdict["parsed"] and outcome.verdict["verdict"] == "uncertain"
    assert outcome.review_rounds == 0 and not outcome.escalated
    assert len(adapter.calls_for("implement")) == len(adapter.calls_for("review")) == 1
    assert not [
        e for e in ledger.events(run.run_id) if e["type"] in {"review_feedback", "escalated"}
    ]


async def test_abort_during_review_fix_closes_the_run_as_aborted(make_task, make_run, ledger):
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(files=[("done.txt", "first\n")]),
                PiScript(error_kind="aborted", error_message="stopped"),
            ],
            "review": PiScript(final_text=verdict_text("concerns", SEVERE)),
        }
    )
    run, _ = make_run(make_task(), adapter)

    outcome = await run.run()

    assert outcome.status == "aborted"


async def test_quota_during_review_fix_keeps_the_reviewed_version(make_task, make_run, ledger):
    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(files=[("done.txt", "first\n")]),
                PiScript(error_kind="quota", error_message="429 quota exhausted"),
            ],
            "review": PiScript(final_text=verdict_text("concerns", SEVERE)),
        }
    )
    run, _ = make_run(make_task(), adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval" and outcome.failure_reason is None
    assert outcome.verdict["findings"] == SEVERE
    abandoned = next(e for e in ledger.events(run.run_id) if e["type"] == "review_round_abandoned")
    assert abandoned["payload"]["reason"].startswith("quota")
    assert ledger.provider_holds(0) != {}
