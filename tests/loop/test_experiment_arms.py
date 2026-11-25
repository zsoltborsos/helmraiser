"""Validated arm values and their application to one governed run."""

from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError

import pytest

from helmraiser.loop.experiments import RunArm, validate_experiment
from helmraiser.task.model import ModelSpec
from tests.loop.test_planned_runs import PLAN, REVIEW
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter


def test_arm_from_validated_overrides(config):
    definition = validate_experiment(
        {
            "name": "test",
            "arms": {
                "A": {},
                "B": {
                    "models": {"implement": "test/worker:high", "plan": {"model": "test/planner"}},
                    "skills": "none",
                    "pi_rpc_implement": False,
                },
            },
        },
        config,
    )
    original = copy.deepcopy(definition.arms)
    arm = RunArm.from_overrides("B", definition.arms["B"])
    assert arm.models["implement"] == ModelSpec("test/worker", "high")
    assert arm.models["plan"] == ModelSpec("test/planner")
    assert json.loads(json.dumps(arm.overrides())) == original["B"]
    payload = arm.overrides()
    payload["models"]["implement"]["model"] = "changed"
    assert arm.overrides() == original["B"]
    assert definition.arms == original


def test_arm_omitted_fields_and_explicit_false():
    arm = RunArm.from_overrides("A", {})
    assert arm.skills is None and arm.pi_rpc_implement is None
    assert arm.overrides() == {}
    assert RunArm.from_overrides("B", {"pi_rpc_implement": False}).overrides() == {
        "pi_rpc_implement": False
    }


def passing_adapter():
    return ScriptedPiAdapter(
        {
            "plan": PiScript(final_text=PLAN),
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": PiScript(final_text=REVIEW),
        }
    )


@pytest.mark.parametrize("stages", [("implement",), ("plan", "review")])
async def test_arm_models_feed_requests_and_ledger_without_changing_task(
    make_task, make_run, ledger, config, stages
):
    task = make_task(plan=True)
    original_models = dict(task.models)
    original_config = copy.deepcopy(config)
    snapshot = task.source_path.read_text()
    arm = RunArm("B", {stage: ModelSpec(f"test/{stage}", "high") for stage in stages})
    adapter = passing_adapter()
    run, _ = make_run(task, adapter, arm=arm)
    assert run.implement_model == arm.models.get("implement", task.models["implement"])

    outcome = await run.run()

    assert outcome.status == "awaiting_approval", outcome.failure_reason
    assert outcome.pending_approval == "merge"
    effective = {**original_models, **arm.models}
    assert run.task.models == effective
    row = ledger.get_run(run.run_id)
    for stage in ("plan", "implement", "review"):
        assert adapter.calls_for(stage)[0].model == effective[stage]
        assert row[f"model_{stage}"] == effective[stage].render()
    assert row["model_fallback"] == original_models["fallback"].render()
    events = ledger.events(run.run_id, types=("experiment_arm",))
    assert len(events) == 1
    assert events[0]["payload"] == {
        "name": "B",
        "overrides": arm.overrides(),
        "replaced": {"models": {stage: original_models[stage].render() for stage in stages}},
    }
    assert task.models == original_models
    assert config == original_config
    assert task.source_path.read_text() == snapshot
    assert ledger.get_task(task.task_id)["snapshot"] == snapshot
    assert run.rundir.task_md.read_text() == snapshot


async def test_auth_failure_still_records_arm(make_task, make_run, ledger):
    arm = RunArm("B", {"implement": ModelSpec("test/worker", "high")})
    checked = []

    def auth(model):
        checked.append(model)
        return "missing credentials"

    adapter = passing_adapter()
    run, _ = make_run(make_task(), adapter, arm=arm, auth_check=auth)
    outcome = await run.run()
    assert outcome.status == "failed"
    assert outcome.failure_reason == "auth: missing credentials"
    assert checked == [arm.models["implement"]]
    assert adapter.calls == []
    assert len(ledger.events(run.run_id, types=("experiment_arm",))) == 1


async def test_unarmed_models_unchanged(make_task, make_run, ledger):
    task = make_task(plan=True)
    adapter = passing_adapter()
    run, _ = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.pending_approval == "merge", outcome.failure_reason
    for request in adapter.calls:
        assert request.model == task.models[request.stage]
    assert ledger.events(run.run_id, types=("experiment_arm",)) == []


def test_arm_is_frozen_and_copies_models():
    models = {"implement": ModelSpec("test/worker")}
    arm = RunArm("B", models)
    models["implement"] = ModelSpec("changed")
    assert arm.models["implement"] == ModelSpec("test/worker")
    with pytest.raises(FrozenInstanceError):
        arm.name = "changed"
    with pytest.raises(TypeError):
        arm.models["implement"] = ModelSpec("changed")
