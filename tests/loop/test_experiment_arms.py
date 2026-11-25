"""Validated arm values and their application to one governed run."""

from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError

import pytest

from helmraiser.loop.experiments import RunArm, validate_experiment
from helmraiser.loop.runner import GovernedRun
from helmraiser.pi import skills
from helmraiser.task.loader import parse_task, render_task
from helmraiser.task.model import ModelSpec
from tests.loop.test_planned_runs import PLAN, REVIEW
from tests.loop.test_stage_skills import commit_project_skill, flags, skill
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


@pytest.mark.parametrize("stage", ["implement", "review"])
@pytest.mark.parametrize("resumed", [False, True])
async def test_valid_arm_conflicting_with_task_models_fails_before_sessions(
    make_task, make_run, ledger, data_dir, config, stage, resumed
):
    # Experiment validation sees config profiles, not the task-file overrides.
    config["profiles"]["implement"] = "test/config-implement"
    config["profiles"]["review"] = "test/config-review"
    task = make_task(plan=True, policy="approve-plan-first" if resumed else "review")
    other_stage = "review" if stage == "implement" else "implement"
    shared_id = task.models[other_stage].model
    definition = validate_experiment(
        {"name": "test", "arms": {"A": {}, "B": {"models": {stage: f"{shared_id}:low"}}}},
        config,
    )
    arm = RunArm.from_overrides("B", definition.arms["B"])
    adapter = passing_adapter()
    if resumed:
        run, _ = make_run(task, adapter)
        outcome = await run.run()
        assert outcome.pending_approval == "plan", outcome.failure_reason
        approval = ledger.pending_approval(run.run_id)
        assert ledger.decide_approval(approval["id"], "approved", "go")
        adapter.calls.clear()
        run = GovernedRun.resume(
            run.run_id,
            config=config,
            ledger=ledger,
            data_dir=data_dir,
            adapter_factory=lambda state, isolation: adapter,
            pi_version="0.87.1",
            auth_check=lambda model: None,
            arm=arm,
        )
    else:
        run, _ = make_run(task, adapter, arm=arm)

    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == (
        f"models: reviewer must differ from implementer (both {shared_id})"
    )
    assert adapter.calls == []
    assert ledger.get_run(run.run_id)["failure_reason"] == outcome.failure_reason
    assert ledger.get_task(task.task_id)["snapshot"] == task.source_path.read_text()
    if not resumed:
        assert len(ledger.events(run.run_id, types=("experiment_arm",))) == 1
        assert run.worktree is None


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


async def test_unarmed_models_and_session_policy_unchanged(
    make_task, make_run, ledger, config, tmp_project
):
    own = commit_project_skill(tmp_project)
    worker = {
        stage: skill(skills.SKILLS_ROOT / stage, stage) for stage in ("plan", "implement", "review")
    }
    config["pi_rpc_implement"] = True
    task = make_task(plan=True)
    adapter = passing_adapter()
    run, _ = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.pending_approval == "merge", outcome.failure_reason
    for request in adapter.calls:
        assert request.model == task.models[request.stage]
        assert request.extra_args == flags(worker[request.stage], request.cwd / own)
        assert request.rpc == (request.stage == "implement")
    assert ledger.events(run.run_id, types=("experiment_arm",)) == []


@pytest.mark.parametrize("mode", ["none", "discover", "default", "test"])
async def test_arm_skills_on_every_stage(make_task, make_run, ledger, tmp_project, mode):
    own = commit_project_skill(tmp_project)
    defaults = {
        stage: skill(skills.SKILLS_ROOT / stage, stage) for stage in ("plan", "implement", "review")
    }
    named = {
        stage: skill(skills.SKILL_SETS_ROOT / "test" / stage, stage)
        for stage in ("plan", "implement")
    }  # Review intentionally absent: project-only, not discovery or default skills.
    arm = RunArm("B", skills=mode)
    adapter = passing_adapter()
    run, _ = make_run(make_task(plan=True), adapter, arm=arm)
    outcome = await run.run()
    assert outcome.pending_approval == "merge", outcome.failure_reason
    assert [request.stage for request in adapter.calls] == ["plan", "implement", "review"]
    for request in adapter.calls:
        expected = flags(request.cwd / own)
        if mode == "discover":
            expected = ()
        elif mode == "default":
            expected = flags(defaults[request.stage], request.cwd / own)
        elif mode == "test" and request.stage in named:
            expected = flags(named[request.stage], request.cwd / own)
        assert request.extra_args == expected
    (event,) = ledger.events(run.run_id, types=("experiment_arm",))
    assert event["payload"] == {
        "name": "B",
        "overrides": {"skills": mode},
        "replaced": {"skills": "default"},
    }


@pytest.mark.parametrize("arm_rpc,config_rpc", [(True, False), (False, True), (None, True)])
async def test_arm_rpc_only_affects_implement(
    make_task, make_run, ledger, config, arm_rpc, config_rpc
):
    config["pi_rpc_implement"] = config_rpc
    adapter = passing_adapter()
    arm = RunArm("B", pi_rpc_implement=arm_rpc)
    run, _ = make_run(make_task(plan=True), adapter, arm=arm)
    outcome = await run.run()
    assert outcome.pending_approval == "merge", outcome.failure_reason
    effective_rpc = arm_rpc if arm_rpc is not None else config_rpc
    for request in adapter.calls:
        assert request.rpc == (request.stage == "implement" and effective_rpc)
    (event,) = ledger.events(run.run_id, types=("experiment_arm",))
    assert event["payload"]["replaced"] == (
        {"pi_rpc_implement": config_rpc} if arm_rpc is not None else {}
    )
    assert event["payload"]["overrides"] == arm.overrides()


@pytest.mark.parametrize("rpc", [True, False])
async def test_feedback_and_fallback_keep_arm_policy(make_task, make_run, config, tmp_project, rpc):
    own = commit_project_skill(tmp_project)
    skill(skills.SKILLS_ROOT / "implement", "excluded")
    worker = skill(skills.SKILL_SETS_ROOT / "test" / "implement", "worker")
    config["pi_rpc_implement"] = not rpc
    arm = RunArm(
        "B",
        {
            "implement": ModelSpec("test/worker", "high"),
            "fallback": ModelSpec("test/fallback", "low"),
        },
        skills="test",
        pi_rpc_implement=rpc,
    )
    adapter = passing_adapter()
    adapter.scripts["implement"] = [
        PiScript(files=[("x", "1")]),
        PiScript(files=[("x", "2")]),
        PiScript(files=[("done.txt", "ok\\n")]),
    ]
    run, _ = make_run(make_task(max_iterations=2), adapter, arm=arm)
    outcome = await run.run()
    assert outcome.pending_approval == "merge", outcome.failure_reason
    assert outcome.escalated
    requests = adapter.calls_for("implement")
    assert [request.attempt for request in requests] == [1, 1, 2]
    assert requests[0].session_id == requests[1].session_id != requests[2].session_id
    assert [request.model for request in requests] == [arm.models["implement"]] * 2 + [
        arm.models["fallback"]
    ]
    for request in requests:
        assert request.extra_args == flags(worker, request.cwd / own)
        assert request.rpc is rpc


@pytest.mark.parametrize("error", ["provider", "protocol"])
async def test_session_retry_keeps_arm_policy(make_task, make_run, tmp_project, error):
    own = commit_project_skill(tmp_project)
    skill(skills.SKILLS_ROOT / "implement", "excluded")
    arm = RunArm(
        "B", {"implement": ModelSpec("test/worker", "high")}, skills="none", pi_rpc_implement=True
    )
    adapter = passing_adapter()
    adapter.scripts["implement"] = [
        PiScript(ok=False, error_kind=error, error_message="retry me"),
        PiScript(files=[("done.txt", "ok\\n")]),
    ]
    run, sleep = make_run(make_task(), adapter, arm=arm)
    outcome = await run.run()
    assert outcome.pending_approval == "merge", outcome.failure_reason
    requests = adapter.calls_for("implement")
    assert len(requests) == 2
    assert sleep.calls
    assert requests[0].session_id == requests[1].session_id
    for request in requests:
        assert request.model == arm.models["implement"]
        assert request.extra_args == flags(request.cwd / own)
        assert request.rpc is True


@pytest.mark.parametrize("rpc", [True, False])
async def test_plan_approval_resume_restores_recorded_arm(
    make_task, make_run, ledger, data_dir, config, tmp_project, rpc
):
    own = commit_project_skill(tmp_project)
    for stage in ("plan", "implement", "review"):
        skill(skills.SKILLS_ROOT / stage, "excluded")
    arm = RunArm(
        "B",
        {stage: ModelSpec(f"test/{stage}", "high") for stage in ("plan", "implement", "review")},
        skills="none",
        pi_rpc_implement=rpc,
    )
    task = make_task(policy="approve-plan-first")
    snapshot = task.source_path.read_text()
    adapter = passing_adapter()
    run, _ = make_run(task, adapter, arm=arm)
    outcome = await run.run()
    assert outcome.pending_approval == "plan", outcome.failure_reason
    approval = ledger.pending_approval(run.run_id)
    assert ledger.decide_approval(approval["id"], "approved", "go")
    config["pi_rpc_implement"] = not rpc
    config["profiles"]["implement"] = "changed/default:low"
    resumed = GovernedRun.resume(
        run.run_id,
        config=config,
        ledger=ledger,
        data_dir=data_dir,
        adapter_factory=lambda state, isolation: adapter,
        pi_version="0.87.1",
        auth_check=lambda model: None,
    )
    assert resumed.arm == arm
    assert resumed.implement_model == arm.models["implement"]
    outcome = await resumed.run()
    assert outcome.pending_approval == "merge", outcome.failure_reason
    assert [request.stage for request in adapter.calls] == ["plan", "implement", "review"]
    for request in adapter.calls:
        assert request.model == arm.models[request.stage]
        assert request.extra_args == flags(request.cwd / own)
        assert request.rpc == (request.stage == "implement" and rpc)
        assert (
            ledger.get_run(run.run_id)[f"model_{request.stage}"]
            == arm.models[request.stage].render()
        )
    assert len(ledger.events(run.run_id, types=("experiment_arm",))) == 1
    assert resumed.rundir.task_md.read_text() == snapshot
    assert ledger.get_task(task.task_id)["snapshot"] == snapshot


def test_constructor_without_snapshot_captures_original_task(make_task, ledger, data_dir, config):
    task = make_task()
    adapter = passing_adapter()
    # The make_run fixture supplies a source snapshot; cover the constructor's default.
    arm = RunArm("B", {"implement": ModelSpec("test/worker")})
    run = GovernedRun(
        task,
        config=config,
        ledger=ledger,
        data_dir=data_dir,
        adapter_factory=lambda state, isolation: adapter,
        pi_version="0.87.1",
        arm=arm,
        auth_check=lambda model: None,
    )
    assert run.snapshot == render_task(task)
    assert parse_task(run.snapshot, task.source_path, config).models == task.models
    assert run.task.models["implement"] == arm.models["implement"]


def test_arm_is_frozen_and_copies_models():
    models = {"implement": ModelSpec("test/worker")}
    arm = RunArm("B", models)
    models["implement"] = ModelSpec("changed")
    assert arm.models["implement"] == ModelSpec("test/worker")
    with pytest.raises(FrozenInstanceError):
        arm.name = "changed"
    with pytest.raises(TypeError):
        arm.models["implement"] = ModelSpec("changed")
