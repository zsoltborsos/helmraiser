"""Validated arm values and their application to one governed run."""

from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError

import pytest

from helmraiser.loop.experiments import RunArm, validate_experiment
from helmraiser.task.model import ModelSpec


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


def test_arm_is_frozen_and_copies_models():
    models = {"implement": ModelSpec("test/worker")}
    arm = RunArm("B", models)
    models["implement"] = ModelSpec("changed")
    assert arm.models["implement"] == ModelSpec("test/worker")
    with pytest.raises(FrozenInstanceError):
        arm.name = "changed"
    with pytest.raises(TypeError):
        arm.models["implement"] = ModelSpec("changed")
