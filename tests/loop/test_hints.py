"""Environment hints in real run failures: a failed verify or evidence step carries the
hints of its full output (``loop/hints.py``), the implementer's feedback and the run's
summary show them, and a loopback port that one of the run's services provides gets
none."""

from __future__ import annotations

from pathlib import Path

import pytest

from helmraiser.loop import services
from helmraiser.loop.excerpt import FailureReport
from helmraiser.loop.hints import DENIED_HINT, loopback_hint, sandbox_hints
from helmraiser.loop.prompts import feedback_prompt
from helmraiser.loop.verify import VerifyResult, run_step
from helmraiser.task.model import VerifyStep
from tests.conftest import FakeDocker
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter

# node:test's TAP reporter on a repository test whose Postgres is not there: the
# refused port shows only in the YAML block of the failure, far from the tail.
TAP_REFUSED = """TAP version 13
# Subtest: users repository
    # Subtest: creates a user
    not ok 1 - creates a user
      ---
      duration_ms: 3.51
      location: '/work/repo/test/users.test.ts:12:3'
      failureType: 'testCodeFailure'
      error: 'connect ECONNREFUSED 127.0.0.1:{port}'
      code: 'ECONNREFUSED'
      stack: |-
        TCPConnectWrap.afterConnect [as oncomplete] (node:net:1637:16)
      ...
    1..1
not ok 1 - users repository
  ---
  duration_ms: 5.02
  type: 'suite'
  failureType: 'subtestsFailed'
  error: '1 subtest failed'
  code: 'ERR_TEST_FAILURE'
  ...
{padding}1..1
# tests 1
# suites 1
# pass 0
# fail 1
# cancelled 0
# skipped 0
# todo 0
# duration_ms 311.2
"""

UNPROVIDED = 55440
PROVIDED = 55441


def tap(port: int, padding: int = 0) -> str:
    return TAP_REFUSED.format(port=port, padding="".join(f"# log {i}\n" for i in range(padding)))


class TestSandboxHints:
    def test_a_refused_port_that_no_service_provides_gets_the_hint(self) -> None:
        assert sandbox_hints(tap(UNPROVIDED), [PROVIDED]) == [
            loopback_hint(f"127.0.0.1:{UNPROVIDED}", sandboxed=True)
        ]

    def test_a_refused_port_that_a_service_provides_gets_none(self) -> None:
        assert sandbox_hints(tap(PROVIDED), [PROVIDED]) == []

    def test_outside_the_sandbox_only_the_service_hint_applies(self) -> None:
        output = tap(UNPROVIDED) + "EACCES: permission denied, mkdir '/home/u/.cache/x'\n"
        assert sandbox_hints(output) == [
            loopback_hint(f"127.0.0.1:{UNPROVIDED}", sandboxed=True),
            DENIED_HINT,
        ]
        assert sandbox_hints(output, sandboxed=False) == [
            f"nothing listens on 127.0.0.1:{UNPROVIDED}: no [[services]] entry in "
            ".helmraiser/project.toml provides it; declare the service and use the variable "
            "it provides"
        ]


class TestStepHints:
    def run(self, tmp_path: Path, port: int, provided_ports: tuple[int, ...]) -> VerifyResult:
        (tmp_path / "out.tap").write_text(tap(port, padding=400))
        step = VerifyStep("test", "cat out.tap; exit 1", 30)
        failure = FailureReport(40, log_dir=tmp_path / "logs", provided_ports=provided_ports)
        return run_step(step, tmp_path, "r1", 20, failure=failure)

    def test_a_tap_run_refused_on_an_unprovided_port_gets_the_hint(self, tmp_path: Path) -> None:
        result = self.run(tmp_path, UNPROVIDED, provided_ports=(PROVIDED,))

        assert result.hints == [loopback_hint(f"127.0.0.1:{UNPROVIDED}", sandboxed=False)]
        assert result.as_dict()["hints"] == result.hints

    def test_the_hints_come_from_the_full_output(self, tmp_path: Path) -> None:
        noise = "".join(f'{{"level":"info","msg":"request {i}"}}\n' for i in range(400))
        (tmp_path / "out.log").write_text(
            f'{{"level":"error","msg":"connect ECONNREFUSED 127.0.0.1:{UNPROVIDED}"}}\n'
            + noise
            + "1 failed\n"
        )
        step = VerifyStep("test", "cat out.log; exit 1", 30)

        result = run_step(step, tmp_path, "r1", 20, failure=FailureReport(max_lines=40))

        assert "ECONNREFUSED" not in result.tail
        assert result.hints == [loopback_hint(f"127.0.0.1:{UNPROVIDED}", sandboxed=False)]

    def test_a_provided_port_gets_none(self, tmp_path: Path) -> None:
        assert self.run(tmp_path, PROVIDED, provided_ports=(PROVIDED,)).hints == []

    def test_a_passing_step_has_none(self, tmp_path: Path) -> None:
        (tmp_path / "out.txt").write_text("connect ECONNREFUSED 127.0.0.1:1\n")
        step = VerifyStep("test", "cat out.txt", 30)
        assert run_step(step, tmp_path, "r1", 20, failure=FailureReport()).hints == []


class TestFeedback:
    def test_the_feedback_has_an_environment_hints_block(self) -> None:
        hint = loopback_hint(f"127.0.0.1:{UNPROVIDED}", sandboxed=True)
        results = [
            VerifyResult("lint", True, 0, 0.1, "ok"),
            VerifyResult("test", False, 1, 0.3, "not ok 1", hints=[hint]),
            VerifyResult("e2e", False, 1, 0.3, "not ok 2", hints=[hint]),
        ]

        prompt = feedback_prompt(results)

        block = prompt.split("## Environment hints\n\n", 1)[1]
        assert "answer `BLOCKED:` with the reason" in block
        assert block.count(f"- {hint}") == 1
        assert prompt.endswith("Fix these and run the verify commands again before stopping.")

    def test_no_block_without_hints(self) -> None:
        results = [VerifyResult("test", False, 1, 0.3, "AssertionError: 1 != 2")]
        assert "Environment hints" not in feedback_prompt(results)


PROJECT = f"""
[[services]]
name = "postgres"
image = "postgres:16"
port = 5432
host_port = {PROVIDED}
ready_timeout = 1
provides = {{ DATABASE_URL = "postgresql://postgres@{{host}}:{{port}}/app" }}
"""


@pytest.mark.parametrize(
    ("port", "hinted"), [(UNPROVIDED, True), (PROVIDED, False)], ids=["unprovided", "provided"]
)
async def test_a_failed_run_shows_the_hints_in_feedback_and_summary(
    make_task,
    make_run,
    tmp_project: Path,
    tmp_path: Path,
    fake_docker: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    port: int,
    hinted: bool,
):
    project_toml = tmp_project / ".helmraiser" / "project.toml"
    project_toml.parent.mkdir(exist_ok=True)
    project_toml.write_text(PROJECT)
    monkeypatch.setattr(services, "port_ready", lambda _port: True)
    output = tmp_path / "out.tap"
    output.write_text(tap(port, padding=400))
    task = make_task(verify=[("test", f"cat {output}; exit 1")], max_iterations=2, fallback=False)
    adapter = ScriptedPiAdapter(
        {"implement": [PiScript(files=[("a.txt", "1\n")]), PiScript(files=[("b.txt", "2\n")])]}
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert (outcome.status, outcome.failure_reason) == ("failed", "verify_exhausted")
    hint = loopback_hint(f"127.0.0.1:{UNPROVIDED}", sandboxed=False)
    expected = [hint] if hinted else []
    assert [r["hints"] for r in outcome.verify_results] == [expected]
    second = adapter.calls_for("implement")[1].prompt
    summary = run.rundir.summary_md.read_text()
    if hinted:
        assert "## Environment hints" in second and f"- {hint}" in second
        assert f"- environment hint: {hint}" in summary
        assert "- test: FAIL (exit 1) in" in summary and f"  - hint: {hint}" in summary
    else:
        assert "Environment hints" not in second
        assert "hint" not in summary
