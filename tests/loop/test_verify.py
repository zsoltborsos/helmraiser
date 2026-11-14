"""Unit tests for verify-step result recording."""

from pathlib import Path

from helmraiser.loop import verify
from helmraiser.loop.evidence import run_evidence_step
from helmraiser.loop.prompts import feedback_prompt
from helmraiser.loop.shell import ShellResult
from helmraiser.task.model import EvidenceStep, VerifyStep


def test_verify_timeout_records_named_tail_and_null_exit_code(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        verify,
        "run_shell",
        lambda command, cwd, timeout, env, wrap=None: ShellResult(
            exit_code=None,
            seconds=2.0,
            output="partial output\n",
            timed_out=True,
        ),
    )

    result = verify.run_step(
        VerifyStep(name="tests", run="sleep 10", timeout=2),
        tmp_path,
        "run-1",
        tail=10,
    )

    assert result.exit_code is None
    assert not result.passed
    assert result.tail.splitlines()[-1] == ("[helmraiser] verify step 'tests' timed out after 2s")


def test_invalid_utf8_verify_failure_produces_feedback_and_runs_next_step(tmp_path: Path) -> None:
    results = verify.run_verify(
        [
            VerifyStep(
                name="invalid-output",
                run=r"printf 'stdout:\377\n'; printf 'stderr:\376\n' >&2; exit 7",
                timeout=5,
            ),
            VerifyStep(name="following-success", run="true", timeout=5),
        ],
        tmp_path,
        "run-1",
    )

    assert results[0].exit_code == 7
    assert not results[0].passed
    assert results[0].tail == "stdout:\ufffd\nstderr:\ufffd"
    assert results[1].exit_code == 0
    assert results[1].passed
    feedback = feedback_prompt(results)
    assert "invalid-output (exit 7)" in feedback
    assert "stdout:\ufffd\nstderr:\ufffd" in feedback
    assert "following-success" not in feedback


def test_verify_and_evidence_steps_do_not_see_worker_secrets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/fake-agent.sock")
    monkeypatch.setenv("WORKER_ONLY", "plain")
    check = 'test -z "${SSH_AUTH_SOCK:-}" && test "$WORKER_ONLY" = plain && test "$OPT" = in'

    result = verify.run_step(VerifyStep(name="env", run=check), tmp_path, "r", 5, {"OPT": "in"})
    evidence = run_evidence_step(
        EvidenceStep("env", check, (), "c"), tmp_path, "r", tmp_path / "e", 5, {"OPT": "in"}
    )

    assert result.passed, result.tail
    assert evidence.ok, evidence.tail


def _recording_wrap(seen: list[list[str]]):
    def wrap(argv: list[str]) -> list[str]:
        seen.append(argv)
        return ["/usr/bin/env", "IN_WRAP=1", *argv]

    return wrap


def test_steps_run_through_the_wrap_unless_they_say_sandbox_host(tmp_path: Path) -> None:
    seen: list[list[str]] = []
    wrap = _recording_wrap(seen)
    check = 'test "${IN_WRAP:-0}" = 1'

    wrapped = verify.run_step(VerifyStep("in", check), tmp_path, "r", 5, wrap=wrap)
    host = verify.run_step(VerifyStep("out", check, sandbox="host"), tmp_path, "r", 5, wrap=wrap)
    unwrapped = verify.run_step(VerifyStep("none", "true"), tmp_path, "r", 5)
    evidence = run_evidence_step(
        EvidenceStep("ev", check, (), "c"), tmp_path, "r", tmp_path / "e", 5, wrap=wrap
    )
    host_evidence = run_evidence_step(
        EvidenceStep("ev-host", "true", (), "c", sandbox="host"),
        tmp_path,
        "r",
        tmp_path / "e",
        5,
        wrap=wrap,
    )

    assert (wrapped.passed, wrapped.sandbox) == (True, "srt")
    assert (host.passed, host.sandbox) == (False, "host")
    assert unwrapped.sandbox == "none" and unwrapped.as_dict()["sandbox"] == "none"
    assert (evidence.ok, evidence.sandbox) == (True, "srt")
    assert host_evidence.sandbox == "host"
    assert len(seen) == 2 and all(argv[-1] == check for argv in seen)
