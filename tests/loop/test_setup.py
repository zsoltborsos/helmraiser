"""Setup failure diagnostics through ``GovernedRun``: the reason names the failing
command, first error and final output line, while the whole tail stays in the event and
``summary.md``."""

from __future__ import annotations

import dataclasses
import shlex

import pytest

from helmraiser.loop.runner import setup_failure_detail
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

REVIEW_PASS = 'ok\n\n```json\n{"verdict": "pass", "findings": [], "evidence_checks": []}\n```'


async def test_chained_setup_failure_names_the_command_and_stderr(make_task, make_run, ledger):
    setup = "printf 'noise\\n'; sh -c 'echo real-error >&2; exit 3'"
    task = dataclasses.replace(make_task(setup=setup), isolation="none")
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == (
        "setup: exit 3 (last failed command: sh -c 'echo real-error >&2; exit 3') — real-error"
    )
    assert adapter.calls == []  # failed before any implement/review session
    event = next(e for e in ledger.events(run.run_id) if e["type"] == "setup_result")
    assert event["payload"]["ok"] is False
    assert event["payload"]["exit_code"] == 3
    assert "noise" in event["payload"]["tail"] and "real-error" in event["payload"]["tail"]
    assert "HELMRAISER_SETUP_RECORD" not in event["payload"]["tail"]
    summary = run.rundir.summary_md.read_text(encoding="utf-8")
    assert outcome.failure_reason in summary
    assert "### Setup failure" in summary
    assert "noise" in summary and "real-error" in summary


async def test_the_last_failure_wins(make_task, make_run, ledger):
    setup = "false || true; sh -c 'echo first-err >&2; exit 2'; sh -c 'echo real-error >&2; exit 3'"
    task = dataclasses.replace(make_task(setup=setup), isolation="none")
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == (
        "setup: exit 3 (last failed command: sh -c 'echo real-error >&2; exit 3') — real-error"
    )


async def test_long_failure_is_capped_but_the_tail_is_kept(make_task, make_run, ledger):
    long_error = "e" * 400
    setup = "sh -c " + shlex.quote(f"printf '{long_error}\\n' >&2; exit 3")
    task = dataclasses.replace(make_task(setup=setup), isolation="none")
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason is not None
    assert len(outcome.failure_reason) <= 500
    assert outcome.failure_reason.endswith("…")
    event = next(e for e in ledger.events(run.run_id) if e["type"] == "setup_result")
    assert long_error in event["payload"]["tail"]
    assert long_error in run.rundir.summary_md.read_text(encoding="utf-8")


async def test_explicit_exit_keeps_the_fallback(make_task, make_run):
    setup = "echo installing; echo 'no such package' >&2; exit 7"
    task = dataclasses.replace(make_task(setup=setup), isolation="none")
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == "setup: exit 7: no such package"


async def test_a_tolerated_failure_does_not_fail_the_run(make_task, make_run, ledger):
    setup = "false || true; echo ready"
    task = dataclasses.replace(make_task(setup=setup), isolation="none")
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": PiScript(final_text=REVIEW_PASS),
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval", outcome
    events = [e for e in ledger.events(run.run_id) if e["type"] == "setup_result"]
    assert [e["payload"]["ok"] for e in events] == [True]


async def test_matching_status_explicit_exit_keeps_the_fallback(make_task, make_run):
    setup = "false; echo final-error >&2; exit 1"
    task = dataclasses.replace(make_task(setup=setup), isolation="none")
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "failed"
    # The record names the last failed command, not necessarily the one that ended
    # the script; the reason says so.
    assert outcome.failure_reason == "setup: exit 1 (last failed command: false) — final-error"


async def test_set_x_successful_setup_output_is_unchanged(make_task, make_run, ledger):
    task = dataclasses.replace(make_task(setup="set -x; echo ok"), isolation="none")
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": PiScript(final_text=REVIEW_PASS),
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval", outcome
    event = next(e for e in ledger.events(run.run_id) if e["type"] == "setup_result")
    assert event["payload"]["tail"] == "ok\n+ echo ok"
    assert "HELMRAISER_SETUP_RECORD" not in event["payload"]["tail"]


async def test_set_x_failure_names_the_command_and_real_error(make_task, make_run, ledger):
    setup = "set -x; sh -c 'echo real-error >&2; exit 3'"
    task = dataclasses.replace(make_task(setup=setup), isolation="none")
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == (
        "setup: exit 3 (last failed command: sh -c 'echo real-error >&2; exit 3') — real-error"
    )
    event = next(e for e in ledger.events(run.run_id) if e["type"] == "setup_result")
    tail = event["payload"]["tail"]
    assert "real-error" in tail
    assert "HELMRAISER_SETUP_RECORD" not in tail and "printf" not in tail


async def test_setup_syntax_error_does_not_leak_instrumentation(make_task, make_run, ledger):
    task = dataclasses.replace(make_task(setup="echo )"), isolation="none")
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "failed"
    event = next(e for e in ledger.events(run.run_id) if e["type"] == "setup_result")
    tail = event["payload"]["tail"]
    assert "syntax error" in tail and "echo )" in tail
    assert "HELMRAISER_SETUP_RECORD" not in tail and "BASH_COMMAND" not in tail


async def test_exit_trap_output_does_not_hide_the_command(make_task, make_run):
    setup = "trap 'echo cleanup >&2' EXIT; sh -c 'echo real-error >&2; exit 3'"
    task = dataclasses.replace(make_task(setup=setup), isolation="none")
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == (
        "setup: exit 3 (last failed command: sh -c 'echo real-error >&2; exit 3') "
        "— real-error … cleanup"
    )


async def test_errexit_failure_still_names_the_command(make_task, make_run):
    setup = "set -e; sh -c 'echo real-error >&2; exit 3'"
    task = dataclasses.replace(make_task(setup=setup), isolation="none")
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == (
        "setup: exit 3 (last failed command: sh -c 'echo real-error >&2; exit 3') — real-error"
    )


async def test_multiline_command_is_not_truncated(make_task, make_run):
    setup = "sh -c '\necho real-error >&2\nexit 3'"
    task = dataclasses.replace(make_task(setup=setup), isolation="none")
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason is not None
    assert "echo real-error >&2" in outcome.failure_reason
    assert outcome.failure_reason.endswith("— real-error")


async def test_errexit_with_broken_path_keeps_the_exit_status(make_task, make_run):
    setup = "set -e; PATH=/nonexistent; false"
    task = dataclasses.replace(make_task(setup=setup), isolation="none")
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == "setup: exit 1 (last failed command: false)"


async def test_record_file_is_removed(make_task, make_run):
    setup = "sh -c 'exit 3'"
    task = dataclasses.replace(make_task(setup=setup), isolation="none")
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "ok\n")])})
    run, _ = make_run(task, adapter)

    await run.run()

    assert run.worktree is not None
    assert not list(run.worktree.glob(".helmraiser-setup-*"))


@pytest.mark.parametrize(
    ("final_command", "prefix"),
    [
        ("exit 1", "setup: exit 1: "),
        ("sh -c 'exit 1'", "setup: exit 1 (last failed command: sh -c 'exit 1') — "),
    ],
)
async def test_coloured_mise_failure_names_the_real_error(
    make_task, make_run, ledger, final_command: str, prefix: str
):
    output = (
        r"\033[31mmise\033[0m \033[31mERROR\033[0m No version is set for shim: srt\n"
        r"Set a global default version with one of the following:\n"
        r"mise use -g node@26.8.1\n"
        r"\033[31mmise\033[0m \033[31mERROR\033[0m Version: 2026.10.13 linux-x64 (2026-10-17)\n"
        r"\033[31mmise\033[0m \033[31mERROR\033[0m "
        r"\033[2mRun with --verbose or MISE_VERBOSE=1 for more information\033[0m\n"
    )
    setup = f"printf '%b' {shlex.quote(output)} >&2; {final_command}"
    task = dataclasses.replace(make_task(setup=setup), isolation="none")
    adapter = ScriptedPiAdapter({})
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == (
        prefix + "mise ERROR No version is set for shim: srt … "
        "mise ERROR Run with --verbose or MISE_VERBOSE=1 for more information"
    )
    assert "No version is set for shim: srt" in outcome.failure_reason
    assert "\x1b" not in outcome.failure_reason
    event = next(e for e in ledger.events(run.run_id) if e["type"] == "setup_result")
    assert event["payload"]["tail"] == (
        "mise ERROR No version is set for shim: srt\n"
        "Set a global default version with one of the following:\n"
        "mise use -g node@26.8.1\n"
        "mise ERROR Version: 2026.10.13 linux-x64 (2026-10-17)\n"
        "mise ERROR Run with --verbose or MISE_VERBOSE=1 for more information"
    )
    summary = run.rundir.summary_md.read_text(encoding="utf-8")
    assert outcome.failure_reason in summary
    assert "\x1b" not in summary
    assert adapter.calls == []


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        (
            "mise ERROR No version is set for shim: srt\n"
            "Set a global default version with one of the following:\n"
            "mise use -g node@26.8.1\n"
            "mise ERROR Version: 2026.10.13 linux-x64 (2026-10-17)\n"
            "mise ERROR Run with --verbose or MISE_VERBOSE=1 for more information",
            "mise ERROR No version is set for shim: srt … "
            "mise ERROR Run with --verbose or MISE_VERBOSE=1 for more information",
        ),
        ("real-error\ncleanup", "real-error … cleanup"),
        ("installing\nno such package", "no such package"),
        ("final-error", "final-error"),
        ("building\nERROR: x\n\n", "ERROR: x"),
        ("", ""),
        ("  \n", ""),
        ("building\nfAtAl: broken\ncleanup", "fAtAl: broken … cleanup"),
        ("building\nnpm err! broken\ncleanup", "npm err! broken … cleanup"),
        ("building\nerr_connection_refused\ncleanup", "err_connection_refused … cleanup"),
        ("ERROR first\nERROR second\ndone", "ERROR first … done"),
        ("\n  error: broken  \n \n  done \n\n", "error: broken … done"),
        ("ERROR same\nprogress\nERROR same", "ERROR same"),
        ("+ sh -c 'echo real-error >&2; exit 3'\nreal-error", "real-error"),
        (
            "++ pnpm install --reporter=errors-only\nERR_PNPM_X boom\ndone",
            "ERR_PNPM_X boom … done",
        ),
        ("real-error\n+ echo cleanup", "real-error … + echo cleanup"),
    ],
)
def test_setup_failure_detail(tail: str, expected: str):
    assert setup_failure_detail(tail) == expected
