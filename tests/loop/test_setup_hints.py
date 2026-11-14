"""Setup diagnostics use full output, with fake srt and no model calls."""

import dataclasses
import shlex

import pytest

from helmraiser.isolation.process import ProcessIsolation
from helmraiser.loop import runner
from helmraiser.loop.hints import SETUP_READONLY_HINT, setup_readonly_line
from helmraiser.loop.shell import ShellResult
from tests.loop.test_process_isolation import fake_srt  # noqa: F401
from tests.mocks.scripted_pi_adapter import ScriptedPiAdapter


def test_hint_text_and_first_signature():
    assert SETUP_READONLY_HINT == (
        "setup tried to write a read-only path: inside the sandbox it writes only the worktree, "
        "/tmp, the per-project npm/pip cache and [setup] write in .helmraiser/project.toml; add the "
        "directory there or point the tool's cache at /tmp"
    )
    assert setup_readonly_line("noise\nRead-only file system\nEROFS") == "Read-only file system"
    assert setup_readonly_line("unrelated") is None


@pytest.mark.parametrize(
    "signature", ["EROFS", "erofs", "Read-only file system", "EROFS " + "x" * 600]
)
@pytest.mark.parametrize("record", [True, False])
@pytest.mark.usefixtures("fake_srt")
async def test_readonly_hint_in_event_summary_and_first_reason(
    make_task, make_run, ledger, signature, record
):
    output = f"first {signature}\nsecond EROFS\nA complete log can be found elsewhere\n"
    command = "sh -c 'exit 3'" if record else "exit 3"
    task = dataclasses.replace(
        make_task(setup=f"printf %s {shlex.quote(output)}; {command}"), isolation="process"
    )
    iso = ProcessIsolation({})
    iso.check_available = lambda: None
    adapter = ScriptedPiAdapter({})
    run, _ = make_run(task, adapter, isolation=iso)
    run.tail_lines = 1
    outcome = await run.run()
    assert outcome.status == "failed"
    reason = outcome.failure_reason
    assert "first " + signature[:30] in reason
    assert "second EROFS" not in reason and "complete log" not in reason
    assert len(reason) <= runner.SETUP_REASON_CHARS
    assert (" — " in reason) == record
    if not record:
        assert reason.startswith("setup: exit 3: first ")
    event = next(e["payload"] for e in ledger.events(run.run_id) if e["type"] == "setup_result")
    assert event["hints"] == [SETUP_READONLY_HINT]
    assert event["tail"] == "A complete log can be found elsewhere"
    summary = run.rundir.summary_md.read_text()
    assert "### Setup failure" in summary and f"- hint: {SETUP_READONLY_HINT}" in summary
    assert adapter.calls == []


@pytest.mark.parametrize("case", ["host", "none", "timeout", "unrelated", "success"])
@pytest.mark.usefixtures("fake_srt")
async def test_no_setup_hint_when_ineligible(
    make_task, make_run, ledger, tmp_project, monkeypatch, case
):
    if case == "host":
        project = tmp_project / ".helmraiser" / "project.toml"
        project.parent.mkdir(exist_ok=True)
        project.write_text('[defaults]\nrequire_approval = true\n[setup]\nsandbox = "host"\n')
    task = dataclasses.replace(
        make_task(setup="echo EROFS; exit 3"), isolation="none" if case == "none" else "process"
    )
    iso = ProcessIsolation({})
    iso.check_available = lambda: None
    adapter = ScriptedPiAdapter({})
    run, _ = make_run(task, adapter, **({} if case == "none" else {"isolation": iso}))
    if case in ("timeout", "unrelated", "success"):
        result = (
            ShellResult(None, 0, "EROFS", timed_out=True)
            if case == "timeout"
            else ShellResult(0 if case == "success" else 3, 0, "unrelated")
        )
        monkeypatch.setattr(runner, "run_shell", lambda *args: result)
    outcome = await run.run()
    event = next(e["payload"] for e in ledger.events(run.run_id) if e["type"] == "setup_result")
    assert "hints" not in event
    assert "- hint:" not in run.rundir.summary_md.read_text()
    if case == "timeout":
        assert outcome.failure_reason.startswith("setup: timed out:")
