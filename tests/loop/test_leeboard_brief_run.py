"""Run-level Leeboard delivery: lazy, cached, implementation-only, resume-safe."""

from dataclasses import replace

import pytest

from helmraiser.loop.runner import GovernedRun
from helmraiser.task.model import TaskOrigin
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

REVIEW = '```json\n{"verdict": "pass", "findings": [], "evidence_checks": []}\n```'
BRIEF = "# Leeboard: project widget\n\n## Rules\n- [rule] Use local tests"


def brief_calls(fake_lb):
    return [call for call in fake_lb.calls() if call["argv"][0] == "brief"]


def test_leeboard_lazy_project_fallback(make_task, make_run, fake_lb):
    task = make_task()
    fake_lb.write_script({"brief": {"stdout": BRIEF}})
    run, _ = make_run(task, ScriptedPiAdapter())
    assert brief_calls(fake_lb) == []
    run.rundir.create()
    run._write_directives(1, 4)
    assert brief_calls(fake_lb)[0]["argv"] == ["brief", "--cwd", str(task.project)]
    assert BRIEF in run.rundir.directives_md.read_text()


@pytest.mark.parametrize(
    ("origin", "expected_session"),
    [
        ({"kind": "claude-code", "session": "a"}, "a"),
        ({"kind": "claude-code", "session": "abc-123"}, "abc-123"),
        ({"kind": "claude-code", "session": "Az09._:-"}, "Az09._:-"),
        ({"kind": "claude-code", "session": "a" * 128}, "a" * 128),
        (None, None),
        ({"kind": "claude-code", "session": ""}, None),
        ({"kind": "claude-code", "session": "has space"}, None),
        ({"kind": "claude-code", "session": "has/slash"}, None),
        ({"kind": "claude-code", "session": 'a";echo bad'}, None),
        ({"kind": "claude-code", "session": "café"}, None),
        ({"kind": "claude-code", "session": "abc\n"}, None),
        ({"kind": "claude-code", "session": "a" * 129}, None),
        ({"kind": "fo", "session": "/tmp/transcript.jsonl"}, None),
        ({"kind": "fo", "session": "abc-123"}, None),
    ],
)
def test_leeboard_handoff_target(
    make_task, make_run, fake_lb, origin: TaskOrigin | None, expected_session: str | None
):
    task = replace(make_task(), origin=origin)
    fake_lb.write_script({"brief": {"stdout": BRIEF}})
    run, _ = make_run(task, ScriptedPiAdapter())
    run.rundir.create()
    for iteration in (1, 2):
        run._write_directives(iteration, 4)
        text = run.rundir.directives_md.read_text()
        if expected_session is not None:
            assert f'lb handoff --to session:{expected_session} --title "..." --body "..."' in text
        else:
            assert "lb handoff" not in text
        assert "lb request" in text
        assert "lb add rule" in text
    assert len(brief_calls(fake_lb)) == 1


async def test_leeboard_fresh_run(make_task, make_run, fake_lb):
    fake_lb.write_script({"brief": {"stdout": BRIEF}})
    seen = []
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(
                files=[("done.txt", "ok\n")],
                action=lambda cwd: seen.append(adapter.calls[-1].system_prompt_path.read_text()),
            ),
            "review": PiScript(final_text=REVIEW),
        }
    )
    task = replace(make_task(), origin={"kind": "claude-code", "session": "abc-123"})
    run, _ = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval"
    assert len(seen) == 1 and BRIEF in seen[0]
    assert "lb handoff --to session:abc-123" in seen[0]
    (call,) = brief_calls(fake_lb)
    assert call["argv"] == ["brief", "--cwd", str(run.worktree)]
    assert call["env"]["LEEBOARD_HARNESS"] == "helmraiser"
    assert call["env"]["LEEBOARD_SESSION"] == run.run_id
    assert adapter.calls_for("implement")[0].system_prompt_path == run.rundir.directives_md


@pytest.mark.parametrize(
    "response",
    [{"stdout": BRIEF}, {"exit": 1, "stderr": "lb: broken"}, {}],
    ids=["text", "unavailable", "empty"],
)
async def test_leeboard_cached_across_iterations(make_task, make_run, fake_lb, response):
    fake_lb.write_script({"brief": response})
    seen = []

    def observe(cwd):
        seen.append(adapter.calls[-1].system_prompt_path.read_text())
        fake_lb.write_script({"brief": {"stdout": "CHANGED BRIEF"}})

    adapter = ScriptedPiAdapter(
        {
            "implement": [
                PiScript(files=[("done.txt", "nope\n")], action=observe),
                PiScript(files=[("done.txt", "ok\n")], action=observe),
            ],
            "review": PiScript(final_text=REVIEW),
        }
    )
    run, _ = make_run(make_task([("content", "grep -q ok done.txt")]), adapter)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval"
    assert outcome.iterations == 2
    assert len(seen) == 2
    assert len(brief_calls(fake_lb)) == 1
    for text in seen:
        assert "CHANGED BRIEF" not in text
        if response.get("stdout"):
            assert BRIEF in text
            assert "Writing to Leeboard" in text
        elif response.get("exit"):
            assert (
                "Leeboard is unavailable (lb brief exited 1: lb: broken): skip brief and recall steps."
                in text
            )
            assert "## Leeboard" not in text
            assert "Writing to Leeboard" not in text
        else:
            assert "## Leeboard" in text
            assert "Writing to Leeboard" in text
            assert "Leeboard is unavailable" not in text


async def test_leeboard_resume_without_materialise(
    make_task, make_run, fake_lb, ledger, config, data_dir, monkeypatch
):
    fake_lb.write_script({"brief": {"stdout": "OLD BRIEF"}})
    seen = []
    adapter = ScriptedPiAdapter(
        {
            "plan": PiScript(final_text="Create done.txt and verify."),
            "implement": PiScript(
                files=[("done.txt", "ok\n")],
                action=lambda cwd: seen.append(adapter.calls[-1].system_prompt_path.read_text()),
            ),
            "review": PiScript(final_text=REVIEW),
        }
    )
    first, _ = make_run(make_task(policy="approve-plan-first"), adapter)
    paused = await first.run()
    assert paused.pending_approval == "plan"
    assert len(brief_calls(fake_lb)) == 1
    ledger.decide_approval(int(ledger.pending_approval(first.run_id)["id"]), "approved", "go")
    fake_lb.write_script({"brief": {"stdout": BRIEF}})
    resumed = GovernedRun.resume(
        first.run_id,
        config=config,
        ledger=ledger,
        adapter_factory=lambda state, isolation: adapter,
        pi_version="0.87.1",
        data_dir=data_dir,
        auth_check=lambda model: None,
    )
    assert len(brief_calls(fake_lb)) == 1

    def forbidden():
        pytest.fail("resume must not materialise")

    monkeypatch.setattr(resumed, "_materialise", forbidden)
    outcome = await resumed.run()
    assert outcome.status == "awaiting_approval" and outcome.pending_approval == "merge"
    assert len(seen) == 1 and BRIEF in seen[0] and "OLD BRIEF" not in seen[0]
    calls = brief_calls(fake_lb)
    assert len(calls) == 2
    assert calls[1]["argv"] == ["brief", "--cwd", str(resumed.worktree)]
    assert calls[1]["env"]["LEEBOARD_SESSION"] == first.run_id


async def test_leeboard_only_implementation_receives_brief(make_task, make_run, fake_lb):
    fake_lb.write_script({"brief": {"stdout": BRIEF}})
    seen = {}

    def observe(cwd):
        req = adapter.calls[-1]
        seen[req.stage] = req.system_prompt_path.read_text()

    adapter = ScriptedPiAdapter(
        {
            "plan": PiScript(final_text="Create done.txt.", action=observe),
            "implement": PiScript(files=[("done.txt", "ok\n")], action=observe),
            "review": PiScript(final_text=REVIEW, action=observe),
        }
    )
    run, _ = make_run(make_task(plan=True), adapter)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval"
    assert set(seen) == {"plan", "implement", "review"}
    assert BRIEF in seen["implement"]
    for stage in ("plan", "review"):
        assert "Leeboard" not in seen[stage]
        assert "lb brief" not in seen[stage]
        assert "Writing to Leeboard" not in seen[stage]
        assert "lb handoff" not in seen[stage]
        assert "lb request" not in seen[stage]
        assert "lb add rule" not in seen[stage]
    assert len(brief_calls(fake_lb)) == 1
