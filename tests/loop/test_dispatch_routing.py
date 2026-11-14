"""Fresh-dispatch routing with fake subscription readings and parsed task intent."""

from __future__ import annotations

import copy
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from helmraiser.cli import commands
from helmraiser.cli.app import app
from helmraiser.cli.supervisor import SupervisorResult
from helmraiser.config import DEFAULT_CONFIG, SUBSCRIPTION_TEMPLATES
from helmraiser.loop.routing import format_dispatch_decision, route_dispatch
from helmraiser.loop.runner import GovernedRun
from helmraiser.task.loader import parse_task
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

WEEK = 7 * 86400
NOW = 1_800_000_000.0
GO = "opencode-go/glm-5.3-flash:high"
CODEX = "openai-codex/gpt-6.1-sol:high"
OVERFLOW = "opencode-go/deepseek-v4.1-flash:high"


def _configure_overflow(config):
    config["profiles"]["implement"] = CODEX
    config["profiles"]["fallback"] = "openai-codex/gpt-6.1-sol:xhigh"
    config["routing"]["implement"]["overflow"] = OVERFLOW


def _readings(go=0.2, codex=0.2, *, now=NOW):
    return {
        name: {"week": {"fraction": used, "resets_at": now + WEEK / 2, "source": "live"}}
        for name, used in (("opencode-go", go), ("openai-codex", codex))
        if used is not None
    }


@pytest.fixture(autouse=True)
def named_subscriptions(config):
    config["subscriptions"] = copy.deepcopy(SUBSCRIPTION_TEMPLATES)


@pytest.fixture
def dispatch_task(tmp_project, config, ledger):
    config["profiles"]["implement"] = GO
    config["profiles"]["fallback"] = CODEX

    def make(**overrides):
        header = {
            "id": "dispatch-widget",
            "title": "Dispatch widget",
            "project": str(tmp_project),
            "status": "queued",
            "policy": "review",
            "isolation": "none",
            "verify": [{"name": "marker", "run": "test -f done.txt"}],
            **overrides,
        }
        text = "---\n" + yaml.safe_dump(header) + "---\n## Goal\n\nFix the widget.\n"
        path = tmp_project.parent / "dispatch.md"
        path.write_text(text)
        task = parse_task(text, path, config)
        ledger.upsert_task(task, text)
        return task

    return make


def test_dispatch_levers_off_by_default(dispatch_task, config):
    task = dispatch_task(size="L")
    effective, decisions = route_dispatch(task, config, _readings(), NOW)
    assert effective is task
    assert decisions == []


def test_omitted_plan_routes_without_mutating_intent(dispatch_task, config, ledger):
    config["routing"]["plan"]["enabled"] = True
    task = dispatch_task()
    source = task.source_path.read_text()
    effective, decisions = route_dispatch(task, config, _readings(), NOW)
    assert effective.plan is True and task.plan is False
    assert effective.policy == task.policy == "review"
    assert effective.plan_explicit is False
    assert effective.explicit_models == task.explicit_models == frozenset()
    assert effective.models is task.models
    assert decisions == [
        {
            "lever": "plan",
            "from": False,
            "to": True,
            "reason": "plan_headroom",
            "headroom": {
                "openai-codex": {
                    "has_headroom": True,
                    "week": {
                        "used": 0.2,
                        "elapsed": 0.5,
                        "target": 0.95,
                        "headroom": 0.275,
                    },
                }
            },
        }
    ]
    assert task.source_path.read_text() == source
    assert ledger.get_task(task.task_id)["snapshot"] == source


@pytest.mark.parametrize("plan", [False, True])
def test_explicit_plan_preserved(dispatch_task, config, plan):
    config["routing"]["plan"]["enabled"] = True
    task = dispatch_task(plan=plan)
    effective, decisions = route_dispatch(task, config, _readings(), NOW)
    assert effective is task
    assert decisions[0]["reason"] == "explicit_plan"
    assert decisions[0]["from"] is decisions[0]["to"] is plan


@pytest.mark.parametrize(
    "readings",
    [
        {},
        _readings(codex=None),
        _readings(codex=0.9),
        {"openai-codex": {"week": {"fraction": None}}},
    ],
)
def test_plan_missing_or_saturated_readings_fail_closed(dispatch_task, config, readings):
    config["routing"]["plan"]["enabled"] = True
    task = dispatch_task()
    effective, decisions = route_dispatch(task, config, readings, NOW)
    assert effective is task
    assert decisions[0]["reason"] == "no_plan_headroom"


@pytest.mark.parametrize(
    "used,expected,reason",
    [(0.3, True, "plan_under_max_used"), (0.7, False, "no_plan_headroom")],
)
def test_plan_under_max_used_ignores_pace(dispatch_task, config, used, expected, reason):
    config["routing"]["plan"] = {"enabled": True, "max_used": 0.7}
    task = dispatch_task()
    # Early in the window: 30% used at 5% elapsed is ahead of pace, but under the cap.
    readings = {"openai-codex": {"week": {"fraction": used, "resets_at": NOW + WEEK * 0.95}}}
    readings["openai-codex"]["week"]["source"] = "live"
    effective, decisions = route_dispatch(task, config, readings, NOW)
    assert decisions[0]["headroom"]["openai-codex"]["has_headroom"] is False
    assert effective.plan is expected
    assert decisions[0]["reason"] == reason


@pytest.mark.parametrize("readings", [{}, {"openai-codex": {"week": {"fraction": None}}}])
def test_plan_max_used_fails_closed_without_readings(dispatch_task, config, readings):
    config["routing"]["plan"] = {"enabled": True, "max_used": 0.7}
    effective, decisions = route_dispatch(dispatch_task(), config, readings, NOW)
    assert effective.plan is False
    assert decisions[0]["reason"] == "no_plan_headroom"


@pytest.mark.parametrize("model,expected", [(GO, True), ("unknown/model", False)])
def test_plan_uses_effective_subscription(dispatch_task, config, model, expected):
    config["routing"]["plan"]["enabled"] = True
    task = dispatch_task(models={"plan": model})
    effective, decisions = route_dispatch(task, config, _readings(codex=0.9), NOW)
    assert effective.plan is expected
    assert list(decisions[0]["headroom"]) == [model.split("/")[0]]


@pytest.mark.parametrize(
    "override,reason",
    [
        ({"policy": "approve-plan-first"}, "policy_plan"),
        ({"plan": True}, "already_planned"),
        ({"plan": "invalid"}, "no_plan_headroom"),
    ],
)
def test_plan_keeps_policy_existing_and_invalid_values(dispatch_task, config, override, reason):
    config["routing"]["plan"]["enabled"] = True
    task = replace(dispatch_task(), **override)
    effective, decisions = route_dispatch(task, config, _readings(), NOW)
    assert effective is task
    assert decisions[0]["reason"] == reason


@pytest.mark.parametrize("size", [None, "S", "M", "L"])
def test_implement_saturation_trigger_has_no_size_gate(dispatch_task, config, size):
    config["routing"]["implement"]["enabled"] = True
    task = dispatch_task(**({"size": size} if size else {}))
    effective, decisions = route_dispatch(task, config, _readings(go=0.9), NOW)
    assert effective.models["implement"].render() == CODEX
    assert decisions[0]["reason"] == "implement_no_headroom"
    assert decisions[0]["from"] == GO and decisions[0]["to"] == CODEX
    assert decisions[0]["headroom"]["opencode-go"]["has_headroom"] is False
    assert task.models["implement"].render() == GO
    assert effective.models["fallback"] == task.models["fallback"]
    assert effective.models["review"] == task.models["review"]


@pytest.mark.parametrize(
    "size,expected",
    [("M", True), ("L", True), ("S", False), (None, False), ("m", False), ("XL", False)],
)
def test_large_headroom_size_gate(dispatch_task, config, size, expected):
    config["routing"]["implement"]["enabled"] = True
    task = dispatch_task(**({"size": size} if size else {}))
    effective, decisions = route_dispatch(task, config, _readings(), NOW)
    assert effective.models["implement"].render() == (CODEX if expected else GO)
    assert decisions[0]["reason"] == ("large_task_headroom" if expected else "no_implement_trigger")


@pytest.mark.parametrize(
    "used,threshold,expected",
    [
        (0.325, 0.15, False),
        (0.326, 0.15, False),
        (0.324, 0.15, True),
        (0.2, 0.3, False),
        (0.2, 0.25, True),
    ],
)
def test_implement_strict_configurable_threshold(dispatch_task, config, used, threshold, expected):
    config["routing"]["implement"].update(enabled=True, codex_headroom=threshold)
    task = dispatch_task(size="L")
    effective, _ = route_dispatch(task, config, _readings(codex=used), NOW)
    assert effective.models["implement"].render() == (CODEX if expected else GO)


@pytest.mark.parametrize(
    "go,short_used,expected", [(0.2, 0.85, False), (0.2, 0.7, True), (0.9, 0.94, False)]
)
def test_all_fallback_windows_matter(dispatch_task, config, go, short_used, expected):
    config["routing"]["implement"]["enabled"] = True
    task = dispatch_task(size="L")
    readings = _readings(go=go)
    readings["openai-codex"]["5h"] = {
        "fraction": short_used,
        "resets_at": NOW + 3600,
        "source": "live",
    }
    effective, _ = route_dispatch(task, config, readings, NOW)
    assert effective.models["implement"].render() == (CODEX if expected else GO)


@pytest.mark.parametrize(
    "go,codex,reason",
    [
        (None, 0.2, "implement_no_headroom"),
        (0.9, None, "no_fallback_headroom"),
        (0.2, None, "no_fallback_headroom"),
        (0.9, 0.9, "no_fallback_headroom"),
    ],
)
def test_implement_missing_readings(dispatch_task, config, go, codex, reason):
    config["routing"]["implement"]["enabled"] = True
    task = dispatch_task()
    effective, decisions = route_dispatch(task, config, _readings(go=go, codex=codex), NOW)
    assert decisions[0]["reason"] == reason
    assert effective.models["implement"].render() == (CODEX if codex == 0.2 else GO)


@pytest.mark.parametrize("model", [GO, "other/custom:high"])
def test_explicit_implementer_wins(dispatch_task, config, model):
    config["routing"]["implement"]["enabled"] = True
    task = dispatch_task(size="L", models={"implement": model})
    effective, decisions = route_dispatch(task, config, _readings(go=0.9), NOW)
    assert effective is task
    assert decisions[0]["reason"] == "explicit_implement"
    assert decisions[0]["to"] == model


@pytest.mark.parametrize("thinking", ["high", "low"])
def test_effective_reviewer_identity_guard(dispatch_task, config, thinking):
    config["routing"]["implement"]["enabled"] = True
    task = dispatch_task(models={"review": "openai-codex/gpt-6.1-sol:" + thinking})
    effective, decisions = route_dispatch(task, config, _readings(go=0.9), NOW)
    assert effective is task
    assert decisions[0]["reason"] == "reviewer_equal"


def test_configured_fallback_not_task_fallback(dispatch_task, config):
    config["routing"]["implement"]["enabled"] = True
    task = dispatch_task(models={"fallback": "other/task-fallback:low"})
    effective, _ = route_dispatch(task, config, _readings(go=0.9), NOW)
    assert effective.models["implement"].render() == CODEX
    assert effective.models["fallback"] == task.models["fallback"]


def _write_project_models(tmp_project: Path, body: str) -> None:
    project_file = tmp_project / ".helmraiser" / "project.toml"
    project_file.parent.mkdir(parents=True, exist_ok=True)
    project_file.write_text(body, encoding="utf-8")


def test_project_profiles_route_implementer(dispatch_task, config, tmp_project):
    _write_project_models(tmp_project, f'[models]\nimplement = "{GO}"\nfallback = "{CODEX}"\n')
    config["profiles"]["implement"] = "other/config-implement:low"
    config["profiles"]["fallback"] = "other/config-fallback:low"
    config["routing"]["implement"]["enabled"] = True

    task = dispatch_task()
    effective, decisions = route_dispatch(task, config, _readings(go=0.9), NOW)

    assert task.explicit_models == frozenset()
    assert decisions[0]["reason"] == "implement_no_headroom"
    assert decisions[0]["from"] == GO and decisions[0]["to"] == CODEX
    assert task.models["implement"].render() == GO
    assert effective.models["implement"].render() == CODEX


def test_project_profiles_preserve_explicit_implementer(dispatch_task, config, tmp_project):
    _write_project_models(tmp_project, f'[models]\nimplement = "{GO}"\nfallback = "{CODEX}"\n')
    config["profiles"]["implement"] = "other/config-implement:low"
    config["profiles"]["fallback"] = "other/config-fallback:low"
    config["routing"]["implement"]["enabled"] = True

    task = dispatch_task(models={"implement": "other/custom:high"})
    effective, decisions = route_dispatch(task, config, _readings(go=0.9), NOW)

    assert effective is task
    assert decisions[0]["reason"] == "explicit_implement"


def test_project_reviewer_blocks_fallback(dispatch_task, config, tmp_project):
    _write_project_models(
        tmp_project,
        f'[models]\nimplement = "{GO}"\nfallback = "{CODEX}"\nreview = "{CODEX}"\n',
    )
    config["profiles"]["implement"] = "other/config-implement:low"
    config["profiles"]["fallback"] = "other/config-fallback:low"
    config["routing"]["implement"]["enabled"] = True

    task = dispatch_task()
    effective, decisions = route_dispatch(task, config, _readings(go=0.9), NOW)

    assert effective is task
    assert decisions[0]["reason"] == "reviewer_equal"


@pytest.mark.parametrize("missing", ["implement", "fallback", "provenance"])
def test_unavailable_profiles_or_provenance_keep_default(dispatch_task, config, missing):
    config["routing"]["implement"]["enabled"] = True
    task = dispatch_task()
    if missing == "provenance":
        task = replace(task, explicit_models=None)
    else:
        config["profiles"][missing] = None
    effective, decisions = route_dispatch(task, config, _readings(go=0.9), NOW)
    assert effective is task
    assert decisions[0]["reason"] == (
        "unknown_implement_provenance" if missing == "provenance" else "missing_profile"
    )


def test_both_levers_and_independent_disable(dispatch_task, config):
    config["routing"]["plan"]["enabled"] = True
    task = dispatch_task(size="M")
    effective, decisions = route_dispatch(task, config, _readings(), NOW)
    assert effective.plan is True
    assert effective.models["implement"].render() == GO
    assert [d["lever"] for d in decisions] == ["plan"]
    config["routing"]["implement"]["enabled"] = True
    effective, decisions = route_dispatch(task, config, _readings(), NOW)
    assert effective.plan is True
    assert effective.models["implement"].render() == CODEX
    assert [d["lever"] for d in decisions] == ["plan", "implement"]
    assert task.plan is False and task.models["implement"].render() == GO


def test_already_fallback_and_shared_subscription(dispatch_task, config):
    config["routing"]["implement"]["enabled"] = True
    config["profiles"]["implement"] = CODEX
    task = dispatch_task(size="L")
    effective, decisions = route_dispatch(task, config, _readings(), NOW)
    assert effective is task
    assert decisions[0]["reason"] == "already_fallback"
    assert list(decisions[0]["headroom"]) == ["openai-codex"]


def test_overflow_is_off_by_default(dispatch_task, config):
    assert "overflow" not in DEFAULT_CONFIG["routing"]["implement"]
    task = dispatch_task()
    effective, decisions = route_dispatch(task, config, _readings(codex=0.95), NOW)
    assert effective is task and decisions == []


def test_overflow_selects_when_the_implement_subscription_is_near_its_limit(dispatch_task, config):
    _configure_overflow(config)
    task = dispatch_task()
    effective, decisions = route_dispatch(task, config, _readings(codex=0.95), NOW)
    assert effective.models["implement"].render() == OVERFLOW
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision["lever"] == "implement"
    assert decision["reason"] == "implement_overflow"
    assert decision["from"] == CODEX and decision["to"] == OVERFLOW
    assert decision["overflow"] == {
        "model": OVERFLOW,
        "outcome": "implement_overflow",
        "trigger": "near_limit",
    }
    assert list(decision["headroom"]) == ["openai-codex", "opencode-go"]
    for role in ("fallback", "review"):
        assert effective.models[role] == task.models[role]
    assert effective.explicit_models == task.explicit_models
    assert task.models["implement"].render() == CODEX


@pytest.mark.parametrize(
    "readings,held,reason",
    [
        (_readings(codex=0.2), {}, "overflow_not_needed"),
        (_readings(codex=0.6), {}, "overflow_not_needed"),
        (_readings(codex=None), {}, "overflow_usage_unknown"),
        ({"openai-codex": {"week": {"fraction": None}}}, {}, "overflow_usage_unknown"),
        (_readings(codex=0.95, go=0.97), {}, "overflow_no_headroom"),
        (_readings(codex=0.95, go=None), {}, "overflow_no_headroom"),
        (_readings(codex=0.95), {"opencode-go": {"detail": "quota"}}, "overflow_held"),
    ],
)
def test_overflow_keeps_the_default_for_each_outcome(dispatch_task, config, readings, held, reason):
    _configure_overflow(config)
    task = dispatch_task()
    effective, decisions = route_dispatch(task, config, readings, NOW, held_providers=held)
    assert effective is task
    assert decisions[0]["reason"] == decisions[0]["overflow"]["outcome"] == reason


@pytest.mark.parametrize(
    "threshold,used,selected",
    [
        (None, 0.9, True),
        (None, 0.89, False),
        (0.5, 0.6, True),
        (0.5, 0.4, False),
        ("x", 0.9, True),
        ("x", 0.89, False),
        (0.0, 0.0, True),
    ],
)
def test_overflow_at_threshold_is_inclusive_and_configurable(
    dispatch_task, config, threshold, used, selected
):
    _configure_overflow(config)
    if threshold is not None:
        config["routing"]["implement"]["overflow_at"] = threshold
    task = dispatch_task()
    effective, decisions = route_dispatch(task, config, _readings(codex=used), NOW)
    assert (effective.models["implement"].render() == OVERFLOW) is selected
    assert decisions[0]["reason"] == ("implement_overflow" if selected else "overflow_not_needed")


def test_overflow_any_measured_window_can_trigger(dispatch_task, config):
    _configure_overflow(config)
    readings = _readings()
    readings["openai-codex"]["5h"] = {
        "fraction": 0.93,
        "resets_at": NOW + 3600,
        "source": "live",
    }
    effective, decisions = route_dispatch(dispatch_task(), config, readings, NOW)
    assert effective.models["implement"].render() == OVERFLOW
    assert decisions[0]["overflow"]["trigger"] == "near_limit"


@pytest.mark.parametrize("go,selected", [(0.6, False), (0.5, False), (0.4, True), (0.3, True)])
def test_overflow_target_follows_the_overflow_subscription_stop_at(
    dispatch_task, config, go, selected
):
    _configure_overflow(config)
    config["subscriptions"]["opencode-go"]["stop_at"] = 0.5
    readings = _readings(go=go, codex=0.95)
    readings["opencode-go"]["week"]["resets_at"] = NOW + WEEK * 0.95
    effective, decisions = route_dispatch(dispatch_task(), config, readings, NOW)
    assert (effective.models["implement"].render() == OVERFLOW) is selected
    assert decisions[0]["headroom"]["opencode-go"]["has_headroom"] is False
    assert decisions[0]["reason"] == ("implement_overflow" if selected else "overflow_no_headroom")


@pytest.mark.parametrize(
    "setting,implement,reason",
    [
        (OVERFLOW, GO, "overflow_same_provider"),
        ("deepseek", CODEX, "invalid_overflow"),
        (7, CODEX, None),
        ("", CODEX, None),
        ("  ", CODEX, None),
    ],
)
def test_overflow_same_provider_and_invalid_setting_keep_the_task(
    dispatch_task, config, setting, implement, reason
):
    _configure_overflow(config)
    config["profiles"]["implement"] = implement
    config["routing"]["implement"]["overflow"] = setting
    task = dispatch_task()
    effective, decisions = route_dispatch(task, config, _readings(codex=0.95), NOW)
    assert effective is task
    if reason is None:
        assert decisions == []
    else:
        assert decisions[0]["reason"] == decisions[0]["overflow"]["outcome"] == reason
        assert decisions[0]["overflow"]["trigger"] is None


def test_overflow_never_overrides_an_explicit_implementer(dispatch_task, config):
    _configure_overflow(config)
    task = dispatch_task(models={"implement": GO})
    effective, decisions = route_dispatch(task, config, _readings(codex=0.95), NOW)
    assert effective is task
    assert decisions[0]["reason"] == "explicit_implement"
    assert decisions[0]["overflow"]["outcome"] == "explicit_implement"
    assert decisions[0]["overflow"]["trigger"] is None


@pytest.mark.parametrize("thinking", ["high", "low"])
@pytest.mark.parametrize("project_review", [False, True])
def test_overflow_reviewer_must_differ(
    dispatch_task, config, tmp_project, thinking, project_review
):
    _configure_overflow(config)
    reviewer = "opencode-go/deepseek-v4.1-flash:" + thinking
    if project_review:
        _write_project_models(tmp_project, f'[models]\nreview = "{reviewer}"\n')
    task = dispatch_task(**({} if project_review else {"models": {"review": reviewer}}))
    effective, decisions = route_dispatch(task, config, _readings(codex=0.95), NOW)
    assert effective is task
    assert decisions[0]["reason"] == "reviewer_equal"
    assert decisions[0]["overflow"]["trigger"] == "near_limit"


def test_overflow_provider_hold_triggers_when_the_reviewer_is_elsewhere(dispatch_task, config):
    _configure_overflow(config)
    task = dispatch_task(models={"review": "other/reviewer:high"})
    effective, decisions = route_dispatch(
        task,
        config,
        _readings(),
        NOW,
        held_providers={"openai-codex": {"detail": "quota"}},
    )
    assert effective.models["implement"].render() == OVERFLOW
    assert decisions[0]["reason"] == "implement_overflow"
    assert decisions[0]["overflow"]["trigger"] == "provider_held"


@pytest.mark.parametrize(
    "codex,held,reason,trigger",
    [
        (0.95, "openai-codex", "review_held", "provider_held"),
        (0.2, "opencode-go", "overflow_not_needed", None),
    ],
)
def test_overflow_waits_when_the_reviewer_provider_is_held(
    dispatch_task, config, codex, held, reason, trigger
):
    _configure_overflow(config)
    task = dispatch_task()
    effective, decisions = route_dispatch(
        task,
        config,
        _readings(codex=codex),
        NOW,
        held_providers={held: {"detail": "quota"}},
    )
    assert effective is task
    assert decisions[0]["reason"] == decisions[0]["overflow"]["outcome"] == reason
    assert decisions[0]["overflow"]["trigger"] == trigger


def test_overflow_takes_precedence_over_the_fallback_swap(dispatch_task, config):
    config["routing"]["implement"].update(enabled=True, overflow="other/cheap:high")
    task = dispatch_task()
    readings = _readings(go=0.95)
    readings["other"] = {"week": {"fraction": 0.1, "resets_at": NOW + WEEK / 2, "source": "live"}}
    effective, decisions = route_dispatch(task, config, readings, NOW)
    assert effective.models["implement"].render() == "other/cheap:high"
    assert decisions[0]["reason"] == "implement_overflow"
    config["routing"]["implement"]["overflow"] = OVERFLOW
    effective, decisions = route_dispatch(task, config, readings, NOW)
    assert effective.models["implement"].render() == CODEX
    assert decisions[0]["reason"] == "implement_no_headroom"
    assert decisions[0]["overflow"]["outcome"] == "overflow_same_provider"


def test_overflow_does_not_disturb_the_large_task_trigger(dispatch_task, config):
    _configure_overflow(config)
    config["routing"]["implement"]["enabled"] = True
    task = dispatch_task(size="M")
    effective, decisions = route_dispatch(task, config, _readings(), NOW)
    assert decisions[0]["reason"] == "large_task_headroom"
    assert effective.models["implement"] == task.models["fallback"]
    assert decisions[0]["overflow"]["outcome"] == "overflow_not_needed"


@pytest.mark.parametrize("missing", ["implement", "fallback", "provenance"])
def test_overflow_initial_guards_win(dispatch_task, config, missing):
    _configure_overflow(config)
    task = dispatch_task()
    if missing == "provenance":
        task = replace(task, explicit_models=None)
    else:
        config["profiles"][missing] = None
    effective, decisions = route_dispatch(task, config, _readings(codex=0.95), NOW)
    assert effective is task
    reason = "unknown_implement_provenance" if missing == "provenance" else "missing_profile"
    assert decisions[0]["reason"] == decisions[0]["overflow"]["outcome"] == reason
    assert decisions[0]["overflow"]["trigger"] is None


PLAN = "1. Create done.txt.\n2. Run verification."
REVIEW = '```json\n{"verdict": "pass", "findings": [], "evidence_checks": []}\n```'


def _adapter():
    return ScriptedPiAdapter(
        {
            "plan": PiScript(final_text=PLAN),
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": PiScript(final_text=REVIEW),
        }
    )


def _assert_recorded(run, ledger, decisions):
    events = [e for e in ledger.events(run.run_id) if e["type"] == "routing"]
    assert [e["payload"] for e in events] == decisions
    jsonl = [json.loads(line) for line in run.rundir.events_jsonl.read_text().splitlines()]
    assert [e["payload"] for e in jsonl if e["event_type"] == "routing"] == decisions
    summary = run.rundir.summary_md.read_text()
    assert summary.count("- routing:") == len(decisions)
    for decision in decisions:
        assert summary.count("- routing: " + format_dispatch_decision(decision)) == 1


async def test_routed_stages_events_and_original_snapshot(dispatch_task, config, make_run, ledger):
    config["routing"]["plan"]["enabled"] = True
    config["routing"]["implement"]["enabled"] = True
    original = dispatch_task(size="M")
    snapshot = original.source_path.read_text()
    task, decisions = route_dispatch(original, config, _readings(), NOW)
    adapter = _adapter()
    run, _ = make_run(task, adapter, routing_decisions=decisions)
    outcome = await run.run()

    assert outcome.status == "awaiting_approval" and outcome.pending_approval == "merge"
    assert [r.stage for r in adapter.calls] == ["plan", "implement", "review"]
    assert adapter.calls_for("plan")[0].model == original.models["plan"]
    assert adapter.calls_for("implement")[0].model.render() == CODEX
    for stage in ("implement", "review"):
        prompt = adapter.calls_for(stage)[0].prompt
        assert "## Plan" in prompt and PLAN in prompt and "## Approved plan" not in prompt
    assert ledger.pending_approval(run.run_id)["kind"] == "merge"
    assert len(ledger.approvals(run.run_id)) == 1
    assert ledger.get_run(run.run_id)["model_implement"] == CODEX
    assert ledger.iterations(run.run_id)[0]["model"] == CODEX
    _assert_recorded(run, ledger, decisions)
    assert original.source_path.read_text() == snapshot
    assert ledger.get_task(task.task_id)["snapshot"] == snapshot
    assert run.rundir.task_md.read_text() == snapshot
    # A later summary rewrite reads persisted decisions, not constructor state.
    run.routing_decisions.clear()
    run._write_summary(outcome)
    _assert_recorded(run, ledger, decisions)


@pytest.mark.parametrize("policy", ["review", "approve-plan-first"])
async def test_overflow_run_records_the_event_and_keeps_the_model_on_resume(
    dispatch_task, config, make_run, ledger, data_dir, policy
):
    _configure_overflow(config)
    original = dispatch_task(policy=policy)
    snapshot = original.source_path.read_text()
    task, decisions = route_dispatch(original, config, _readings(codex=0.95), NOW)
    adapter = _adapter()
    run, _ = make_run(task, adapter, routing_decisions=decisions)
    outcome = await run.run()
    if policy == "approve-plan-first":
        assert outcome.pending_approval == "plan"
        pending = ledger.pending_approval(run.run_id)
        ledger.decide_approval(pending["id"], "approved", "go")
        config["profiles"]["implement"] = "other/new-default:low"
        config["profiles"]["fallback"] = "other/new-fallback:low"
        run = GovernedRun.resume(
            run.run_id,
            config=config,
            ledger=ledger,
            data_dir=data_dir,
            adapter_factory=lambda state, isolation: adapter,
            pi_version="0.87.1",
            auth_check=lambda model: None,
        )
        outcome = await run.run()
        assert len(adapter.calls_for("plan")) == 1
    assert outcome.pending_approval == "merge"
    assert adapter.calls_for("implement")[0].model.render() == OVERFLOW
    assert adapter.calls_for("review")[0].model.render() == "openai-codex/gpt-6-astra:high"
    assert ledger.get_run(run.run_id)["model_implement"] == OVERFLOW
    _assert_recorded(run, ledger, decisions)
    assert ledger.get_task(task.task_id)["snapshot"] == snapshot
    assert run.rundir.task_md.read_text() == snapshot
    assert original.source_path.read_text() == snapshot


@pytest.mark.parametrize("enabled", [False, True])
async def test_keep_or_disabled_decisions_recorded(
    dispatch_task, config, make_run, ledger, enabled
):
    config["routing"]["plan"]["enabled"] = enabled
    config["routing"]["implement"]["enabled"] = enabled
    task, decisions = route_dispatch(dispatch_task(plan=False), config, {}, NOW)
    run, _ = make_run(task, _adapter(), routing_decisions=decisions)
    await run.run()
    assert all(d["from"] == d["to"] for d in decisions)
    assert len(decisions) == (2 if enabled else 0)
    _assert_recorded(run, ledger, decisions)


async def test_preflight_failure_still_has_routing_summary(dispatch_task, config, make_run, ledger):
    config["routing"]["plan"]["enabled"] = True
    task, decisions = route_dispatch(dispatch_task(), config, _readings(), NOW)
    adapter = _adapter()
    run, _ = make_run(
        task, adapter, routing_decisions=decisions, auth_check=lambda model: "no auth"
    )
    outcome = await run.run()
    assert outcome.status == "failed"
    assert adapter.calls == []
    _assert_recorded(run, ledger, decisions)


async def test_approved_plan_resume_preserves_routed_implementer(
    dispatch_task, config, make_run, ledger, data_dir
):
    config["routing"]["plan"]["enabled"] = True
    config["routing"]["implement"]["enabled"] = True
    original = dispatch_task(policy="approve-plan-first")
    snapshot = original.source_path.read_text()
    task, decisions = route_dispatch(original, config, _readings(go=0.9), NOW)
    adapter = _adapter()
    run, _ = make_run(task, adapter, routing_decisions=decisions)
    paused = await run.run()
    assert paused.pending_approval == "plan"
    pending = ledger.pending_approval(run.run_id)
    ledger.decide_approval(pending["id"], "approved", "go")
    config["profiles"]["implement"] = "other/new-default:low"
    config["profiles"]["fallback"] = "other/new-fallback:low"

    resumed = GovernedRun.resume(
        run.run_id,
        config=config,
        ledger=ledger,
        data_dir=data_dir,
        adapter_factory=lambda state, isolation: adapter,
        pi_version="0.87.1",
        auth_check=lambda model: None,
    )
    outcome = await resumed.run()
    assert outcome.pending_approval == "merge"
    assert adapter.calls_for("implement")[0].model.render() == CODEX
    assert "## Approved plan" in adapter.calls_for("implement")[0].prompt
    assert len(adapter.calls_for("plan")) == 1
    _assert_recorded(resumed, ledger, decisions)
    assert ledger.get_task(task.task_id)["snapshot"] == snapshot
    assert resumed.rundir.task_md.read_text() == snapshot


async def test_failed_routed_implementation_retry_restores_plan_and_model(
    dispatch_task, config, make_run, ledger, data_dir
):
    config["routing"]["plan"]["enabled"] = True
    config["routing"]["implement"]["enabled"] = True
    original = dispatch_task(size="M")
    task, decisions = route_dispatch(original, config, _readings(), NOW)
    adapter = ScriptedPiAdapter(
        {
            "plan": PiScript(final_text=PLAN),
            "implement": PiScript(error_kind="protocol", error_message="broken output"),
        }
    )
    run, _ = make_run(task, adapter, routing_decisions=decisions)
    failed = await run.run()
    assert failed.status == "failed" and failed.failure_reason
    ledger.update_run(run.run_id, status="starting", stage="implement")
    config["profiles"]["implement"] = "other/new-default"
    config["routing"]["plan"]["enabled"] = False
    config["routing"]["implement"]["enabled"] = False
    retry_adapter = _adapter()
    resumed = GovernedRun.resume(
        run.run_id,
        config=config,
        ledger=ledger,
        data_dir=data_dir,
        adapter_factory=lambda state, isolation: retry_adapter,
        pi_version="0.87.1",
        auth_check=lambda model: None,
    )
    assert resumed.task.plan is True
    outcome = await resumed.run()
    assert outcome.status == "awaiting_approval"
    assert retry_adapter.calls_for("implement")[0].model.render() == CODEX
    assert retry_adapter.calls_for("plan") == []
    assert "## Approved plan" not in retry_adapter.calls_for("implement")[0].prompt
    assert PLAN in retry_adapter.calls_for("review")[0].prompt
    assert "## Approved plan" not in retry_adapter.calls_for("review")[0].prompt
    _assert_recorded(resumed, ledger, decisions)
    assert resumed.rundir.task_md.read_text() == original.source_path.read_text()


@pytest.fixture
def routing_cli(config, monkeypatch, fake_pi):
    config["routing"]["plan"]["enabled"] = True
    config["routing"]["implement"]["enabled"] = True
    monkeypatch.setattr(commands, "load_config", lambda: config)
    monkeypatch.setattr(commands, "live_for", lambda *args: {})
    monkeypatch.setattr(
        commands, "usage", lambda ledger, config, now, **kwargs: _readings(go=0.9, now=now)
    )
    fake_pi.write_script(
        {
            "sessions": {
                "*-plan": {"text": PLAN},
                "*-implement": {"files": [["done.txt", "ok\n"]]},
                "*-review": {"text": REVIEW},
            }
        }
    )
    return CliRunner()


def test_overflow_dispatch_passes_active_holds(dispatch_task, config, ledger, monkeypatch):
    _configure_overflow(config)
    task = dispatch_task(models={"review": "other/reviewer:high"})
    monkeypatch.setattr(commands, "live_for", lambda *args: {})
    monkeypatch.setattr(commands, "usage", lambda ledger, config, now, **kwargs: _readings(now=now))
    ledger.hold_provider("openai-codex", time.time() + 3600, "quota")
    effective, decisions = commands._dispatch_task(task, ledger, config)
    assert effective.models["implement"].render() == OVERFLOW
    assert decisions[0]["reason"] == "implement_overflow"
    assert decisions[0]["overflow"]["trigger"] == "provider_held"
    ledger.clear_provider_hold("openai-codex")
    effective, decisions = commands._dispatch_task(task, ledger, config)
    assert effective is task
    assert decisions[0]["reason"] == "overflow_not_needed"


def test_overflow_without_a_setting_does_not_acquire_usage(
    dispatch_task, config, ledger, monkeypatch
):
    calls = []

    def live(*args):
        calls.append("live")
        return {}

    def readings(ledger, config, now, **kwargs):
        calls.append("usage")
        return _readings(codex=0.95, now=now)

    monkeypatch.setattr(commands, "live_for", live)
    monkeypatch.setattr(commands, "usage", readings)
    task = dispatch_task()
    assert commands._dispatch_task(task, ledger, config) == (task, [])
    assert calls == []
    _configure_overflow(config)
    effective, decisions = commands._dispatch_task(dispatch_task(), ledger, config)
    assert calls == ["live", "usage"]
    assert effective.models["implement"].render() == OVERFLOW
    assert decisions[0]["reason"] == "implement_overflow"


@pytest.mark.parametrize("held", [False, True])
def test_cli_overflow_waits_when_codex_is_held(
    dispatch_task, routing_cli, config, ledger, data_dir, fake_pi, monkeypatch, held
):
    config["routing"]["plan"]["enabled"] = False
    config["routing"]["implement"]["enabled"] = False
    _configure_overflow(config)
    task = dispatch_task()
    monkeypatch.setattr(
        commands, "usage", lambda ledger, config, now, **kwargs: _readings(codex=0.95, now=now)
    )
    if held:
        ledger.hold_provider("openai-codex", time.time() + 3600, "quota")
    result = routing_cli.invoke(
        app, ["work", "--once", "--json", "--no-notify", "--data-dir", str(data_dir)]
    )
    assert result.exit_code == 0, result.output
    runs = ledger.list_runs()
    if held:
        assert runs == []
        assert "quota:openai-codex" in result.output
        assert not any(c["session_id"] for c in fake_pi.calls())
        return
    assert len(runs) == 1
    assert runs[0]["model_implement"] == OVERFLOW
    assert (
        runs[0]["model_review"] == task.models["review"].render() == "openai-codex/gpt-6-astra:high"
    )
    run_id = runs[0]["run_id"]
    events = [e["payload"] for e in ledger.events(run_id) if e["type"] == "routing"]
    assert len(events) == 1
    decision = events[0]
    assert decision["reason"] == "implement_overflow"
    assert decision["overflow"] == {
        "model": OVERFLOW,
        "outcome": "implement_overflow",
        "trigger": "near_limit",
    }
    summary = (data_dir / "runs" / run_id / "summary.md").read_text()
    assert "- routing: " + format_dispatch_decision(decision) in summary
    calls = {c["session_id"].rsplit("-", 1)[-1]: c for c in fake_pi.calls() if c["session_id"]}
    for stage, model in (("implement", OVERFLOW), ("review", task.models["review"].render())):
        argv = calls[stage]["argv"]
        assert argv[argv.index("--model") + 1] == model
    human = routing_cli.invoke(app, ["log", run_id, "--data-dir", str(data_dir)])
    assert human.exit_code == 0, human.output
    assert "implement: implement_overflow" in human.output


def test_cli_overflow_is_skipped_by_the_reviewer_stop_at(
    dispatch_task, routing_cli, config, ledger, data_dir, fake_pi, monkeypatch
):
    config["routing"]["plan"]["enabled"] = False
    config["routing"]["implement"]["enabled"] = False
    _configure_overflow(config)
    config["routing"]["implement"]["overflow_at"] = 0.8
    config["subscriptions"]["openai-codex"]["stop_at"] = 0.8
    monkeypatch.setattr(
        commands, "usage", lambda ledger, config, now, **kwargs: _readings(codex=0.85, now=now)
    )
    task = dispatch_task()
    effective, decisions = commands._dispatch_task(task, ledger, config)
    assert effective.models["implement"].render() == OVERFLOW
    assert decisions[0]["reason"] == "implement_overflow"
    block = commands._task_skip(effective, ledger, config, no_caps=False)
    assert block is not None
    assert "openai-codex" in block[0]["reason"]
    result = routing_cli.invoke(
        app, ["work", "--once", "--json", "--no-notify", "--data-dir", str(data_dir)]
    )
    assert result.exit_code == 0, result.output
    assert "openai-codex" in result.output
    assert ledger.list_runs() == []
    assert not any(c["session_id"] for c in fake_pi.calls())


@pytest.mark.parametrize("held", [None, "opencode-go", "openai-codex"])
def test_cli_dispatch_routes_before_provider_holds(
    dispatch_task, routing_cli, ledger, data_dir, fake_pi, held
):
    task = dispatch_task()
    snapshot = task.source_path.read_text()
    if held:
        ledger.hold_provider(held, time.time() + 3600, "quota")
    result = routing_cli.invoke(
        app, ["work", "--once", "--json", "--no-notify", "--data-dir", str(data_dir)]
    )
    assert result.exit_code == 0, result.output
    runs = ledger.list_runs()
    if held == "openai-codex":
        assert runs == []
        assert "quota:openai-codex" in result.output
        assert not any(c["session_id"] for c in fake_pi.calls())
        return
    assert len(runs) == 1 and runs[0]["model_implement"] == CODEX
    run_id = runs[0]["run_id"]
    calls = {c["session_id"].rsplit("-", 1)[-1]: c for c in fake_pi.calls() if c["session_id"]}
    plan_argv = calls["plan"]["argv"]
    implement_argv = calls["implement"]["argv"]
    assert plan_argv[plan_argv.index("--model") + 1] == task.models["plan"].render()
    assert implement_argv[implement_argv.index("--model") + 1] == CODEX
    assert PLAN in Path(implement_argv[-1].removeprefix("@")).read_text()
    events = [e["payload"] for e in ledger.events(run_id) if e["type"] == "routing"]
    assert [e["lever"] for e in events] == ["plan", "implement"]
    run_dir = data_dir / "runs" / run_id
    summary = (run_dir / "summary.md").read_text()
    for decision in events:
        assert "- routing: " + format_dispatch_decision(decision) in summary
    assert (run_dir / "task.md").read_text() == snapshot
    assert ledger.get_task(task.task_id)["snapshot"] == snapshot
    logged = routing_cli.invoke(app, ["log", run_id, "--json", "--data-dir", str(data_dir)])
    assert logged.exit_code == 0, logged.output
    assert [
        json.loads(line)["payload"]
        for line in logged.output.splitlines()
        if json.loads(line)["type"] == "routing"
    ] == events
    human = routing_cli.invoke(app, ["log", run_id, "--data-dir", str(data_dir)])
    assert human.exit_code == 0, human.output
    assert "plan: plan_headroom" in human.output
    assert "implement: implement_no_headroom" in human.output


@pytest.mark.parametrize("selected_block,no_caps", [(False, False), (True, False), (True, True)])
def test_cli_caps_inspect_effective_task(
    dispatch_task, routing_cli, config, ledger, data_dir, selected_block, no_caps
):
    dispatch_task()
    config["subscriptions"]["opencode-go"]["stop_at"] = 0.8
    if selected_block:
        config["subscriptions"]["openai-codex"].update(
            stop_at=0.8,
            monthly_limit_usd={"gpt-6-astra": 1},
            default_monthly_limit_usd=1,
        )
    args = ["work", "--once", "--json", "--no-notify", "--data-dir", str(data_dir)]
    if no_caps:
        args.append("--no-caps")
    result = routing_cli.invoke(app, args)
    assert result.exit_code == 0, result.output
    runs = ledger.list_runs()
    if selected_block and not no_caps:
        assert runs == []
        assert "never_startable:openai-codex" in result.output
    else:
        assert len(runs) == 1 and runs[0]["model_implement"] == CODEX


def test_cli_dry_run_previews_routing_without_recording(
    dispatch_task, routing_cli, ledger, data_dir, tmp_project, fake_pi
):
    task = dispatch_task()
    snapshot = task.source_path.read_text()
    result = routing_cli.invoke(app, ["work", "--dry-run", "--json", "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.output
    preview = json.loads(result.output)
    argv = preview["argv"]
    assert argv[argv.index("--model") + 1] == CODEX
    assert ledger.list_runs() == []
    assert not Path(preview["run_dir"]).exists()
    assert not (tmp_project / ".helmraiser" / "worktrees").exists()
    assert not any(c["session_id"] for c in fake_pi.calls())
    assert ledger.get_task(task.task_id)["snapshot"] == snapshot


def test_disabled_dispatch_does_not_acquire_usage(dispatch_task, config, ledger, monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("disabled dispatch must not acquire usage")

    monkeypatch.setattr(commands, "live_for", refuse)
    monkeypatch.setattr(commands, "usage", refuse)
    task = dispatch_task()
    effective, decisions = commands._dispatch_task(task, ledger, config)
    assert effective is task and decisions == []


@pytest.mark.parametrize(
    "enabled,held,blocked",
    [
        (True, "opencode-go", False),
        (True, "openai-codex", True),
        (False, "opencode-go", True),
    ],
)
def test_supervisor_previews_routing_without_events(
    dispatch_task, routing_cli, config, ledger, data_dir, monkeypatch, enabled, held, blocked
):
    config["routing"]["plan"]["enabled"] = enabled
    config["routing"]["implement"]["enabled"] = enabled
    task = dispatch_task()
    snapshot = task.source_path.read_text()
    ledger.hold_provider(held, time.time() + 3600, "quota")
    previews = []

    class PreviewSupervisor:
        def __init__(self, **kwargs):
            self.skip = kwargs["skip"]

        def run(self):
            previews.append(self.skip(ledger.get_task(task.task_id)))
            return SupervisorResult()

    monkeypatch.setattr(commands, "Supervisor", PreviewSupervisor)
    result = routing_cli.invoke(
        app, ["work", "--max-parallel", "2", "--json", "--no-notify", "--data-dir", str(data_dir)]
    )
    assert result.exit_code == 0, result.output
    assert len(previews) == 1
    if blocked:
        assert previews[0][0]["reason"] == "quota:" + held
    else:
        assert previews == [None]
    assert ledger.list_runs() == []
    assert ledger.get_task(task.task_id)["snapshot"] == snapshot
    assert not (data_dir / "runs").exists()
