"""Bounded first-paragraph reasons for blocked sessions."""

import pytest

from helmraiser.loop.runner import BLOCKED_REASON_CHARS, blocked_detail
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

BLOCKED_ANSWERS = [
    "BLOCKED: Required padding conflicts:\n"
    "- app-header-chrome.test.tsx:72 expected undefined; received 16.\n\n"
    "Ignored later paragraph.",
    "BLOCKED:\nRequired padding conflicts:\n"
    "- app-header-chrome.test.tsx:72 expected undefined; received 16.\n\n"
    "Ignored later paragraph.",
]
EXPECTED_REASON = (
    "blocked: Required padding conflicts: "
    "- app-header-chrome.test.tsx:72 expected undefined; received 16."
)


def test_blocked_reason_limit():
    assert BLOCKED_REASON_CHARS == 500


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("BLOCKED: simple reason", "simple reason"),
        ("  BLOCKED:   spaced  \n\nlater", "spaced"),
        (
            "BLOCKED: Required padding conflicts:\n"
            "- header test expected undefined; received 16.\n"
            "- profile test expected 0; received 16.\n\nIgnored later paragraph.",
            "Required padding conflicts: - header test expected undefined; received 16. "
            "- profile test expected 0; received 16.",
        ),
        (
            "BLOCKED:\nreason on the next line\nand more\n\nignored",
            "reason on the next line and more",
        ),
        ("BLOCKED: headline\ndetail\n \t \nignored", "headline detail"),
        ("BLOCKED:  many\t spaces\n  and   more  ", "many spaces and more"),
        ("", ""),
        ("BLOCKED:", ""),
        ("BLOCKED: headline\r\ndetail\r\n\r\nignored", "headline detail"),
    ],
)
def test_blocked_detail(text, expected):
    assert blocked_detail(text) == expected


def test_blocked_detail_caps_long_paragraph():
    assert blocked_detail("BLOCKED: " + "x" * 500) == "x" * 500
    assert blocked_detail("BLOCKED: " + "x" * 501) == "x" * 499 + "…"
    long = blocked_detail("BLOCKED: " + "word " * 300)
    assert len(long) <= 500
    assert long.endswith("…")


@pytest.mark.parametrize("answer", BLOCKED_ANSWERS)
async def test_implement_blocked_preserves_first_paragraph(answer, make_task, make_run, ledger):
    task = make_task()
    adapter = ScriptedPiAdapter({"implement": PiScript(final_text=answer)})
    run, _ = make_run(task, adapter)
    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == EXPECTED_REASON
    assert ledger.get_run(run.run_id)["failure_reason"] == EXPECTED_REASON
    assert [call.stage for call in adapter.calls] == ["implement"]
    (iteration,) = ledger.iterations(run.run_id)
    assert iteration["agent_ok"] == 1
    assert iteration["verify_passed"] is None


@pytest.mark.parametrize("answer", BLOCKED_ANSWERS)
@pytest.mark.parametrize("approve_plan_first", [False, True])
async def test_plan_blocked_preserves_first_paragraph(
    answer, approve_plan_first, make_task, make_run, ledger
):
    task = make_task(policy="approve-plan-first") if approve_plan_first else make_task(plan=True)
    adapter = ScriptedPiAdapter({"plan": PiScript(final_text=answer)})
    run, _ = make_run(task, adapter)
    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == EXPECTED_REASON
    assert ledger.get_run(run.run_id)["failure_reason"] == EXPECTED_REASON
    assert [call.stage for call in adapter.calls] == ["plan"]
    assert outcome.iterations == 0
    assert ledger.pending_approval(run.run_id) is None
