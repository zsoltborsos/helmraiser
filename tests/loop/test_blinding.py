"""Blinding: which runs hide their setup, and what ``Blind`` hides in text, in ledger
events, in the pair's refusals and in the sums that are keyed by model or provider."""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import pytest

from helmraiser.ledger.db import Ledger
from helmraiser.loop import blinding, experiments, pairs
from helmraiser.loop.blinding import HIDDEN, Blind
from helmraiser.loop.compare import run_facts
from helmraiser.loop.model_report import build_model_report
from helmraiser.loop.report import build_report
from helmraiser.loop.runner import run_record
from helmraiser.loop.spend import cost_entries, spend_by_provider
from helmraiser.pi import skills
from tests.loop.test_experiment_forking import start
from tests.loop.test_pairs import claim_pair, fail, park


@pytest.fixture(autouse=True)
def reading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experiments, "codex_weekly_fraction", lambda ledger, config, now: 0.2)


def event(type_: str, payload: dict[str, Any], source: str = "helmraiser") -> dict[str, Any]:
    return {"id": 1, "ts": 1.0, "source": source, "stage": None, "type": type_, "payload": payload}


def other_run(ledger: Ledger, task, started_at: float) -> str:
    """One more run of ``task`` that is no run of its pair."""
    run_id = str(uuid4())
    ledger.create_run(run_record(task, run_id, pi_version="0.87.1", started_at=started_at))
    return run_id


def pick(ledger: Ledger, pair: dict[str, Any], label: str = "X") -> None:
    assert ledger.set_pair_pick(int(pair["id"]), label, decided_by="judged", reason="r", judge="j")


# ── which runs are blinded ────────────────────────────────────────────


def test_the_runs_of_a_pair_are_blinded_until_its_pick(make_task, ledger, config):
    start(ledger)
    task = make_task()
    pair = claim_pair(ledger, task, config)
    unpaired = other_run(ledger, make_task(task_id="other"), 2000.0)

    for run_id in (pair["run_x"], pair["run_y"]):
        assert blinding.blinding_pair(ledger, run_id)["id"] == pair["id"]
        assert blinding.blinded(ledger, run_id)
    assert not blinding.blinded(ledger, unpaired)
    assert not blinding.blinded(ledger, "no-such-run")
    assert blinding.blinded_runs(ledger) == {pair["run_x"], pair["run_y"]}

    pick(ledger, pair)

    assert not blinding.blinded(ledger, pair["run_x"])
    assert not blinding.blinded(ledger, pair["run_y"])
    assert blinding.blinded_runs(ledger) == set()


def test_a_retried_pairs_earlier_runs_stay_blinded_and_later_runs_do_not(make_task, ledger, config):
    """A retried pair keeps its labels and arms: the arm of an earlier run of its task
    would give them away. A run that the task gets after the pair's runs is no arm."""
    start(ledger)
    task = make_task()
    pair = claim_pair(ledger, task, config)
    earlier = other_run(ledger, task, 500.0)
    later = other_run(ledger, task, 5000.0)

    assert blinding.blinding_pair(ledger, earlier)["id"] == pair["id"]
    assert not blinding.blinded(ledger, later)
    assert blinding.blinded_runs(ledger) == {pair["run_x"], pair["run_y"], earlier}

    pick(ledger, pair)

    assert not blinding.blinded(ledger, earlier)


def test_reveal_marks_an_unpicked_pair_once(make_task, ledger, config):
    start(ledger)
    picked = claim_pair(ledger, make_task(task_id="picked"), config)
    for run_id in (picked["run_x"], picked["run_y"]):
        fail(ledger, run_id)
    pick(ledger, picked)
    pair = claim_pair(ledger, make_task(), config)

    assert ledger.set_pair_unblinded(int(pair["id"])) is True
    assert ledger.set_pair_unblinded(int(pair["id"])) is False
    # A picked pair's setups are no secret: nothing to mark.
    assert ledger.set_pair_unblinded(int(picked["id"])) is False

    assert ledger.pair_for_task(pair["task_id"])["unblinded"] is True
    assert ledger.pair_for_task(picked["task_id"])["unblinded"] is False


# ── what is hidden ────────────────────────────────────────────────────


@pytest.fixture
def blind(make_task, ledger, config) -> Blind:
    """A pair whose arm B implements with ``test/arm-b:high`` (``ARMS``) and whose
    runs record the task's ``openai-codex`` models."""
    start(ledger)
    pair = claim_pair(ledger, make_task(), config)
    ledger.start_iteration(pair["run_x"], 1, "s1", "iter-provider/iter-model:low")
    return Blind(ledger, pair)


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("pi_contract: test/arm-b:high gave no usage", "pi_contract: hidden gave no usage"),
        ("quota: test/arm-b out", "quota: hidden out"),
        ("model arm-b (provider test) refused", "model hidden (provider hidden) refused"),
        ("held: iter-provider until 12:00", "held: hidden until 12:00"),
        ("iter-model failed", "hidden failed"),
        # A name stands alone: it is not cut out of a longer word.
        ("the latest contest of arm-bands", "the latest contest of arm-bands"),
        ("verify_failed: marker", "verify_failed: marker"),
    ],
)
def test_text_hides_each_way_a_model_is_written(blind, text, shown):
    assert blind.text(text) == shown


def test_text_hides_the_recorded_models_of_both_runs(blind, ledger):
    run = ledger.list_runs()[0]
    for column in blinding.RUN_MODEL_COLUMNS:
        if run[column]:
            assert blind.text(f"used {run[column]} here") == "used hidden here"
    assert blind.text(None) is None and blind.text("") == ""


def test_text_hides_the_workers_skill_folders(blind):
    for root in (skills.SKILLS_ROOT, skills.SKILL_SETS_ROOT):
        path = root / "implement" / "tdd" / "SKILL.md"
        assert blind.text(f"read {path}, then went on") == "read hidden, then went on"
        assert blind.text(f'"path": "{path}"') == '"path": "hidden"'


def test_an_event_of_a_pi_session_keeps_only_its_type(blind):
    message = {
        "type": "message_end",
        "message": {"role": "assistant", "model": "arm-b", "content": [{"text": "per the skill"}]},
    }
    tool = {"type": "tool_execution_start", "toolName": "read", "args": {"path": "SKILL.md"}}

    assert blind.event(event("message_end", message, "pi"))["payload"] == {
        "type": "message_end",
        "message": HIDDEN,
    }
    assert blind.event(event("tool_execution_start", tool, "pi"))["payload"] == {
        "type": "tool_execution_start",
        "toolName": HIDDEN,
        "args": HIDDEN,
    }


def test_an_event_hides_the_setup_and_keeps_its_keys(blind):
    arm = {"name": "B", "overrides": {"skills": "none"}, "replaced": {"skills": "default"}}
    routing = {"lever": "implement", "from": "a/x", "to": "b/y", "reason": "headroom"}
    held = {"provider": "test", "until": 5.0, "detail": "429"}
    started = {"n": 1, "attempt": 1, "model": "test/arm-b:high", "prompt": "Do the task."}
    retry = {"error_kind": "provider", "error": "test/arm-b:high: overloaded", "n": 2}
    verify = {"n": 1, "passed": True, "results": [{"name": "marker", "passed": True}]}

    assert blind.event(event("experiment_arm", arm))["payload"] == dict.fromkeys(arm, HIDDEN)
    assert blind.event(event("routing", routing))["payload"] == dict.fromkeys(routing, HIDDEN)
    assert blind.event(event("provider_held", held))["payload"] == dict.fromkeys(held, HIDDEN)
    assert blind.event(event("iteration_start", started))["payload"] == {
        **started,
        "model": HIDDEN,
    }
    assert blind.event(event("retry", retry))["payload"] == {
        **retry,
        "error": "hidden: overloaded",
    }
    # What a run did is shown as it is.
    shown = blind.event(event("verify_result", verify))
    assert shown["payload"] == verify and shown["type"] == "verify_result"


def test_hiding_an_event_leaves_the_ledgers_event_alone(blind):
    original = event("iteration_start", {"n": 1, "model": "test/arm-b:high"})

    blind.event(original)

    assert original["payload"] == {"n": 1, "model": "test/arm-b:high"}


def test_a_blinded_runs_failure_reason_names_no_model(make_task, ledger, config):
    start(ledger)
    pair = claim_pair(ledger, make_task(), config)
    fail(ledger, pair["run_x"], "pi_contract: test/arm-b:high gave no usage")
    park(ledger, pair["run_y"])
    run = ledger.get_run(pair["run_x"])

    assert blinding.failure_reason(ledger, run) == "pi_contract: hidden gave no usage"
    # ``pick`` and ``retry`` refuse with the arm's state: that names no model either.
    assert pairs.arms(ledger, pair)["X"].describe() == "failed (pi_contract: hidden gave no usage)"

    pick(ledger, pair, "Y")

    assert blinding.failure_reason(ledger, run) == "pi_contract: test/arm-b:high gave no usage"
    assert "test/arm-b:high" in pairs.arms(ledger, ledger.pair_for_run(run["run_id"]))["X"].failure


# ── sums keyed by model or provider ───────────────────────────────────


def spend(ledger: Ledger, run_id: str, model: str, cost: float) -> None:
    ledger.start_iteration(run_id, 1, f"{run_id}-implement", model)
    ledger.finish_iteration(
        run_id,
        1,
        agent_ok=True,
        commit_sha=None,
        verify_passed=True,
        verify_results=[],
        cost_usd=cost,
    )
    ledger.update_run(run_id, cost_usd=cost, model_implement=model)


def test_costs_of_blinded_runs_are_counted_as_hidden(make_task, ledger, config, data_dir):
    start(ledger)
    pair = claim_pair(ledger, make_task(), config)
    spend(ledger, pair["run_x"], "test/arm-b:high", 0.7)
    spend(ledger, pair["run_y"], "openai-codex/control:high", 0.3)
    now = time.time()

    def views() -> tuple[dict[str, float], set[str], set[str]]:
        report = build_report(ledger, data_dir, 3600.0, now=now, config=config)
        models = build_model_report(ledger, data_dir, now, config, now=now, by="setup")
        return (
            report["cost"]["by_model"],
            set(spend_by_provider(ledger, now)),
            {row["setup"]["implement"] for row in models},
        )

    by_model, providers, setups = views()
    assert by_model == {HIDDEN: 1.0}
    assert providers == {HIDDEN}
    assert setups == {HIDDEN}
    assert {model for _ts, model, _usd in cost_entries(ledger, 0.0)} == {
        "test/arm-b:high",
        "openai-codex/control:high",
    }

    # Two arms on the same provider: its sum says nothing about either.
    spend(ledger, pair["run_x"], "openai-codex/other:high", 0.7)
    for run_id in (pair["run_x"], pair["run_y"]):
        ledger.update_run(run_id, model_review="openai-codex/review", model_fallback=None)
    by_model, providers, setups = views()
    assert by_model == {HIDDEN: 1.0} and providers == {"openai-codex"}
    # And with the same models, neither does a sum per model.
    spend(ledger, pair["run_x"], "openai-codex/control:high", 0.7)
    by_model, providers, setups = views()
    assert by_model == {"openai-codex/control:high": 1.0}
    assert setups == {"openai-codex/control:high"}
    assert blinding.blinded_runs(ledger, str) == set()
    assert blinding.blinded_runs(ledger) == {pair["run_x"], pair["run_y"]}
    spend(ledger, pair["run_x"], "test/arm-b:high", 0.7)

    pick(ledger, pair)

    by_model, providers, setups = views()
    assert by_model == {"test/arm-b:high": 0.7, "openai-codex/control:high": 0.3}
    assert providers == {"test", "openai-codex"}
    assert setups == {"test/arm-b:high", "openai-codex/control:high"}


def test_compare_keeps_a_blinded_runs_setup_hidden(make_task, ledger, config):
    start(ledger)
    pair = claim_pair(ledger, make_task(), config)
    fail(ledger, pair["run_x"], "quota: test/arm-b:high")
    run = ledger.get_run(pair["run_x"])

    facts = run_facts(ledger, run)

    assert set(facts["setup"].values()) == {HIDDEN}
    assert facts["failure_reason"] == "quota: hidden"

    pick(ledger, pair, "Y")

    facts = run_facts(ledger, run)
    assert facts["setup"]["implement"] == run["model_implement"]
    assert facts["failure_reason"] == "quota: test/arm-b:high"
