"""Failure excerpts of failed verify / evidence steps (loop/excerpt.py)."""

from __future__ import annotations

import json
import random
from pathlib import Path

from helmraiser.loop import verify
from helmraiser.loop.evidence import run_evidence_step
from helmraiser.loop.excerpt import FailureReport, failure_excerpt
from helmraiser.loop.review import build_review_prompt
from helmraiser.loop.shell import ShellResult, tail_lines
from helmraiser.task.model import EvidenceStep, VerifyStep

FAILING = {17: "rejects an expired token", 42: "paginates hub listings", 88: "retries on 503"}


def _pino(rng: random.Random, n: int) -> str:
    return "# " + json.dumps(
        {
            "level": 30,
            "time": 1790000000000 + n,
            "pid": 4242,
            "hostname": "ci",
            "reqId": f"req-{rng.randrange(10**6)}",
            "msg": "request completed",
            "responseTime": rng.random() * 20,
        }
    )


def _tap_run(tests: int = 100, logs: int = 300) -> str:
    """node --test TAP output: ``tests`` results, three failing, under ``logs`` pino lines."""
    rng = random.Random(7)
    log_at = sorted(rng.sample(range(tests * 3), logs)) if logs <= tests * 3 else []
    log_positions = {pos: n for n, pos in enumerate(log_at)}
    lines = ["TAP version 13"]
    for t in range(1, tests + 1):
        for slot in range(3):
            if (t - 1) * 3 + slot in log_positions:
                lines.append(_pino(rng, log_positions[(t - 1) * 3 + slot]))
        lines.append(f"# Subtest: case {t}")
        if t in FAILING:
            lines += [
                f"not ok {t} - {FAILING[t]}",
                "  ---",
                "  duration_ms: 3.1",
                f"  location: '/work/hub/test/api.test.js:{t * 10}:3'",
                "  failureType: 'testCodeFailure'",
                "  error: |-",
                f"    Expected values to be strictly equal (case {t}):",
                "    ",
                "    401 !== 200",
                "    ",
                "  code: 'ERR_ASSERTION'",
                "  name: 'AssertionError'",
                "  expected: 200",
                f"  actual: {400 + t}",
                "  operator: 'strictEqual'",
                "  stack: |-",
                f"    TestContext.<anonymous> (file:///work/hub/test/api.test.js:{t * 10}:12)",
                "  ...",
            ]
        else:
            lines += [f"ok {t} - case {t}", "  ---", "  duration_ms: 0.8", "  ..."]
    lines += [f"1..{tests}", f"# tests {tests}", "# suites 0", f"# pass {tests - 3}", "# fail 3"]
    lines += [_pino(rng, 9000 + n) for n in range(5)]
    lines.append("AssertionError: Hub API suite must pass")
    return "\n".join(lines) + "\n"


def test_tap_failures_buried_in_json_logs_are_all_in_the_excerpt() -> None:
    output = _tap_run()
    assert len(output.splitlines()) > 400
    old = tail_lines(output, 120)
    assert "not ok" not in old  # the old feedback missed every failure

    excerpt = failure_excerpt(output, 120, 300, 30_000)

    for t, name in FAILING.items():
        assert f"not ok {t} - {name}" in excerpt
        assert f"Expected values to be strictly equal (case {t}):" in excerpt
        assert f"  actual: {400 + t}" in excerpt
        assert f"api.test.js:{t * 10}:3'" in excerpt
    assert "# fail 3" in excerpt and "# tests 100" in excerpt
    assert excerpt.splitlines()[-1] == "AssertionError: Hub API suite must pass"
    assert "lines omitted …" in excerpt
    assert excerpt.index("not ok 17") < excerpt.index("not ok 42") < excerpt.index("not ok 88")
    assert excerpt.count("not ok 42 - paginates") == 1  # deduplicated


PYTEST = """\
============================= test session starts ==============================
collected 40 items

tests/test_a.py ........................................F                [100%]
{noise}
=================================== FAILURES ===================================
_______________________________ test_totals_add ________________________________

    def test_totals_add():
        order = make_order()
>       assert order.total == 30
E       AssertionError: assert 25 == 30
E        +  where 25 = <Order>.total

tests/test_a.py:41: AssertionError
----------------------------- Captured log call ------------------------------
{noise}
=========================== short test summary info ============================
FAILED tests/test_a.py::test_totals_add - AssertionError: assert 25 == 30
========================= 1 failed, 39 passed in 2.31s =========================
{noise}
"""


def _noise(n: int, tag: str) -> str:
    return "\n".join(f"INFO {tag} worker heartbeat {i}" for i in range(n))


def test_pytest_failure_keeps_assertion_lines_and_summary() -> None:
    output = PYTEST.format(noise=_noise(200, "x"))
    excerpt = failure_excerpt(output, 10, 300, 30_000)
    assert "_ test_totals_add _" in excerpt
    assert ">       assert order.total == 30" in excerpt
    assert "E       AssertionError: assert 25 == 30" in excerpt
    assert "tests/test_a.py:41: AssertionError" in excerpt
    assert "FAILED tests/test_a.py::test_totals_add - AssertionError" in excerpt
    assert "= 1 failed, 39 passed in 2.31s =" in excerpt
    assert "INFO x worker heartbeat 199" in excerpt  # tail
    assert "INFO x worker heartbeat 100" not in excerpt


VITEST = """\
 RUN  v2.1.4 /work/web

{noise}
 ❯ src/cart.test.ts (12 tests | 1 failed) 31ms
   × cart > applies the discount 9ms
     → expected 90 to be 81 // Object.is equality
{noise}
⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯

 FAIL  src/cart.test.ts > cart > applies the discount
AssertionError: expected 90 to be 81 // Object.is equality

- Expected
+ Received

- 81
+ 90

 ❯ src/cart.test.ts:22:31
⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[1/1]⎯

 Test Files  1 failed | 7 passed (8)
      Tests  1 failed | 95 passed (96)
{noise}
"""


def test_vitest_failure_keeps_fail_block_and_totals() -> None:
    output = VITEST.format(noise=_noise(150, "vite"))
    excerpt = failure_excerpt(output, 5, 300, 30_000)
    assert "× cart > applies the discount" in excerpt
    assert "FAIL  src/cart.test.ts > cart > applies the discount" in excerpt
    assert "AssertionError: expected 90 to be 81" in excerpt
    assert "+ 90" in excerpt and "❯ src/cart.test.ts:22:31" in excerpt
    assert "Test Files  1 failed | 7 passed (8)" in excerpt
    assert "Tests  1 failed | 95 passed (96)" in excerpt


def test_no_failure_pattern_is_the_tail_with_an_omitted_marker() -> None:
    output = _noise(500, "build") + "\nmake: *** [all] Error 2\n"
    excerpt = failure_excerpt(output, 20, 300, 30_000)
    assert excerpt.splitlines() == ["… 481 lines omitted …", *tail_lines(output, 20).splitlines()]
    short = "one\ntwo\n"
    assert failure_excerpt(short, 20, 300, 30_000) == tail_lines(short, 20)


def test_the_caps_are_enforced() -> None:
    output = _tap_run(tests=300, logs=600)
    many = "\n".join(
        f"not ok {i} - broken {i}\n  ---\n  error: 'boom {i}'\n  ..." for i in range(1, 400)
    )
    for text in (output, many + "\n" + _noise(300, "tail")):
        excerpt = failure_excerpt(text, 120, 100, 30_000)
        kept = [line for line in excerpt.splitlines() if not line.endswith("omitted …")]
        assert len(kept) <= 100
        excerpt = failure_excerpt(text, 120, 1000, 2_000)
        kept = [line for line in excerpt.splitlines() if not line.endswith("omitted …")]
        assert sum(len(line) + 1 for line in kept) <= 2_000
    # Failures take at most three quarters; the last lines survive a flood of failures.
    excerpt = failure_excerpt(many + "\n" + _noise(300, "tail"), 120, 100, 30_000)
    assert "not ok 1 - broken 1" in excerpt
    assert excerpt.splitlines()[-1] == "INFO tail worker heartbeat 299"


def _fake_shell(monkeypatch, module, output: str, exit_code: int) -> None:
    monkeypatch.setattr(
        module,
        "run_shell",
        lambda command, cwd, timeout, env, wrap=None: ShellResult(exit_code, 1.0, output),
    )


def test_failed_step_gets_the_excerpt_and_a_full_log(tmp_path: Path, monkeypatch) -> None:
    output = _tap_run()
    _fake_shell(monkeypatch, verify, output, 1)
    report = FailureReport(300, 30_000, tmp_path / "logs", "verify-2")
    (result,) = verify.run_verify(
        [VerifyStep("hub api", "node --test")], tmp_path, "r", 120, failure=report
    )
    assert not result.passed
    assert "not ok 42 - paginates hub listings" in result.tail
    assert result.log == str(tmp_path / "logs" / "verify-2-1-hub-api.log")
    assert Path(result.log).read_text(encoding="utf-8") == output
    assert result.as_dict()["log"] == result.log


def test_passing_step_is_unchanged(tmp_path: Path, monkeypatch) -> None:
    output = _tap_run().replace("not ok", "ok")
    _fake_shell(monkeypatch, verify, output, 0)
    report = FailureReport(300, 30_000, tmp_path / "logs")
    result = verify.run_step(VerifyStep("hub", "node --test"), tmp_path, "r", 120, failure=report)
    assert result.passed and result.log is None
    assert result.tail == tail_lines(output, 120)
    assert not (tmp_path / "logs").exists()


def test_without_a_report_a_failed_step_keeps_the_plain_tail(tmp_path: Path, monkeypatch) -> None:
    output = _tap_run()
    _fake_shell(monkeypatch, verify, output, 1)
    result = verify.run_step(VerifyStep("hub", "node --test"), tmp_path, "r", 120)
    assert result.tail == tail_lines(output, 120) and result.log is None


def test_failed_evidence_step_gets_the_excerpt_and_a_full_log(
    tmp_path: Path, monkeypatch, make_task
) -> None:
    import helmraiser.loop.evidence as evidence

    output = _tap_run()
    _fake_shell(monkeypatch, evidence, output, 1)
    report = FailureReport(300, 30_000, tmp_path / "logs", "evidence")
    result = run_evidence_step(
        EvidenceStep("e2e", "node --test", (), ""),
        tmp_path,
        "r",
        tmp_path / "ev",
        120,
        failure=report,
    )
    assert not result.ok
    assert "not ok 88 - retries on 503" in result.tail
    assert result.log is not None and Path(result.log).read_text(encoding="utf-8") == output
    prompt = build_review_prompt(
        make_task(),
        diff_path=tmp_path / "diff.patch",
        diff_text="",
        verify_results=[],
        evidence_results=[result],
        evidence_dir=tmp_path / "ev",
    )
    assert f"Full output: `{result.log}`" in prompt


NODE_SPEC = """\
{noise}
▶ hub api
  ✔ lists hubs (2.1ms)
  ✖ rejects an expired token (3.4ms)
    AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:

    401 !== 200

        at TestContext.<anonymous> (file:///work/hub/test/api.test.js:17:12)
      code: 'ERR_ASSERTION',
      actual: 401,
      expected: 200,
    }}

  ✔ paginates (1.0ms)
▶ hub api (9ms)
{noise}
ℹ tests 66
ℹ pass 65
ℹ fail 1

✖ failing tests:

test at test/api.test.js:17:3
✖ rejects an expired token (3.4ms)
  AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:
{noise}
"""


def test_node_spec_reporter_failure_and_failing_tests_section() -> None:
    output = NODE_SPEC.format(noise=_noise(100, "pino"))
    excerpt = failure_excerpt(output, 3, 300, 30_000)
    assert "  ✖ rejects an expired token (3.4ms)" in excerpt
    assert "      actual: 401," in excerpt
    assert "✖ failing tests:" in excerpt
    assert "test at test/api.test.js:17:3" in excerpt
    assert "ℹ fail 1" in excerpt and "ℹ tests 66" in excerpt


def test_python_traceback_up_to_the_exception_line() -> None:
    output = "\n".join(
        [
            _noise(80, "boot"),
            "Traceback (most recent call last):",
            '  File "/work/app/cli.py", line 12, in <module>',
            "    main()",
            '  File "/work/app/cli.py", line 9, in main',
            "    raise ValueError('bad config')",
            "ValueError: bad config",
            _noise(80, "shutdown"),
        ]
    )
    excerpt = failure_excerpt(output, 3, 300, 30_000)
    assert "Traceback (most recent call last):\n" in excerpt
    assert "    raise ValueError('bad config')\nValueError: bad config\n" in excerpt
    assert "INFO boot worker heartbeat 79" not in excerpt
