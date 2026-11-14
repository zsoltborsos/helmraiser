"""Evidence, review and merge (spec §4.5): artifact globs, uncertain forcing,
verdict parsing, the policy table, and finalize refusal."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from helmraiser.ledger.db import Ledger
from helmraiser.loop.abort import abort_run
from helmraiser.loop.evidence import expand_artifacts, run_evidence
from helmraiser.loop.merge import MergeDecision, decide
from helmraiser.loop.review import EVIDENCE_INLINE_LIMIT, build_review_prompt, parse_verdict
from helmraiser.task.model import EvidenceStep, TaskSpec
from tests.mocks.scripted_pi_adapter import PiScript, ScriptedPiAdapter


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def verdict_text(
    verdict: str,
    findings: list[dict] | None = None,
    prefix: str = "",
    evidence_checks: list[dict] | None = None,
) -> str:
    body = json.dumps(
        {
            "verdict": verdict,
            "findings": findings or [],
            "evidence_checks": evidence_checks or [],
        }
    )
    return f"{prefix}I read the diff.\n\n```json\n{body}\n```"


def _review_prompt(make_task, evidence_dir: Path) -> str:
    return build_review_prompt(
        make_task(),
        diff_path=evidence_dir.parent / "diff.patch",
        diff_text="",
        verify_results=[],
        evidence_results=[],
        evidence_dir=evidence_dir,
    )


def _summary_block(prompt: str, rel: str = "junit.xml") -> str:
    marker = f"### {rel} (JUnit summary"
    body = prompt[prompt.index(marker) :]
    fence = body.index("```\n") + len("```\n")
    return body[fence : body.index("\n```", fence)]


EVIDENCE = [
    {
        "name": "shots",
        "run": 'echo hello > "$HELMRAISER_EVIDENCE_DIR/out.txt"',
        "artifacts": ["out.txt"],
        "criteria": "out.txt says hello",
    }
]
SHOTS_MET = [{"name": "shots", "meets_criteria": True}]


# ── unit: globs and verdict parsing ──────────────────────────────────


class TestArtifacts:
    def test_globs_expand_relative_to_the_evidence_dir(self, tmp_path: Path):
        (tmp_path / "shots").mkdir()
        (tmp_path / "shots" / "a.png").write_bytes(b"")
        (tmp_path / "shots" / "b.png").write_bytes(b"")
        (tmp_path / "log.txt").write_text("x")
        matched, missing = expand_artifacts(("shots/*.png", "log.txt", "nope/*"), tmp_path)
        assert matched == ["shots/a.png", "shots/b.png", "log.txt"]
        assert missing == ["nope/*"]

    def test_step_ok_needs_exit_0_and_every_glob(self, tmp_path: Path):
        evidence_dir = tmp_path / "evidence"
        steps = [
            EvidenceStep("good", 'echo hi > "$HELMRAISER_EVIDENCE_DIR/hi.txt"', ("hi.txt",), "c"),
            EvidenceStep("no-file", "true", ("missing.txt",), "c"),
            EvidenceStep(
                "bad-exit", 'echo x > "$HELMRAISER_EVIDENCE_DIR/x.txt"; exit 4', ("x.txt",), "c"
            ),
        ]
        results = run_evidence(steps, tmp_path, "run-1", evidence_dir, tail=5)
        assert [(r.name, r.ok, r.exit_code) for r in results] == [
            ("good", True, 0),
            ("no-file", False, 0),
            ("bad-exit", False, 4),
        ]
        assert results[0].artifacts == ["hi.txt"] and results[1].missing == ["missing.txt"]
        assert (evidence_dir / "hi.txt").read_text() == "hi\n"

    def test_timeout_is_not_ok(self, tmp_path: Path):
        step = EvidenceStep("slow", "sleep 5", (), "c", timeout=1)
        (result,) = run_evidence([step], tmp_path, "r", tmp_path / "e", tail=5)
        assert not result.ok and result.exit_code is None
        assert "timed out after 1s" in result.tail

    def test_task_env_reaches_the_command(self, tmp_path: Path):
        step = EvidenceStep("env", 'echo "$WIDGET" > "$HELMRAISER_EVIDENCE_DIR/w"', ("w",), "c")
        (result,) = run_evidence([step], tmp_path, "r", tmp_path / "e", 5, {"WIDGET": "on"})
        assert result.ok and (tmp_path / "e" / "w").read_text() == "on\n"


def test_review_prompt_includes_every_evidence_file(make_task, tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    (evidence_dir / "nested").mkdir(parents=True)
    small = evidence_dir / "nested" / "small.txt"
    large = evidence_dir / "large.txt"
    image = evidence_dir / "screen.png"
    binary = evidence_dir / "result.bin"
    small.write_text("all good\n")
    large.write_text("x" * (EVIDENCE_INLINE_LIMIT + 1))
    image.write_bytes(b"not a real image")
    binary.write_bytes(b"binary\x01\x02data")

    prompt = build_review_prompt(
        make_task(),
        diff_path=tmp_path / "diff.patch",
        diff_text="",
        verify_results=[],
        evidence_results=[],
        evidence_dir=evidence_dir,
    )

    assert "### nested/small.txt\n```\nall good\n```" in prompt
    assert f"- large.txt: `{large}` ({large.stat().st_size} bytes; not inlined)" in prompt
    assert f"- result.bin: `{binary}` ({binary.stat().st_size} bytes; not inlined)" in prompt
    assert prompt.count(f"@{image}") == 1


def test_review_prompt_includes_truncated_approved_plan(make_task, tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    plan = "é" * EVIDENCE_INLINE_LIMIT

    prompt = build_review_prompt(
        make_task(),
        diff_path=tmp_path / "diff.patch",
        diff_text="",
        verify_results=[],
        evidence_results=[],
        evidence_dir=evidence_dir,
        plan=plan,
    )

    approved = prompt.split("## Approved plan\n\n", 1)[1].split("\n\n## Diff", 1)[0]
    assert approved.startswith("é" * (EVIDENCE_INLINE_LIMIT // 2))
    assert f"[Plan cut after {EVIDENCE_INLINE_LIMIT} bytes.]" in approved


def test_review_prompt_respects_total_inline_budget(make_task, tmp_path: Path, monkeypatch):
    monkeypatch.setattr("helmraiser.loop.review.EVIDENCE_INLINE_TOTAL", 5)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    first = evidence_dir / "a.txt"
    second = evidence_dir / "b.txt"
    first.write_text("1234")
    second.write_text("5678")

    prompt = build_review_prompt(
        make_task(),
        diff_path=tmp_path / "diff.patch",
        diff_text="",
        verify_results=[],
        evidence_results=[],
        evidence_dir=evidence_dir,
    )

    assert "### a.txt\n```\n1234\n```" in prompt
    assert f"- b.txt: `{second}` (4 bytes; not inlined)" in prompt
    assert "### b.txt" not in prompt


def test_review_prompt_junit_failures_summary(make_task, tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    padding = "PAD" * 20000
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<testsuites>"
        '<testsuite name="alpha" tests="2" failures="1" errors="0" skipped="3" time="1.25">'
        '<testcase classname="tests.test_alpha" name="test_ok" time="0.1"/>'
        '<testcase classname="tests.test_alpha" name="test_bad" time="0.2">'
        '<failure message="boom first line">failure body line one</failure>'
        "</testcase>"
        f"<system-out>{padding}</system-out>"
        "</testsuite>"
        '<testsuite name="beta" tests="1" failures="0" errors="1" skipped="0" time="2.5">'
        '<testcase classname="tests.test_beta" name="test_error" time="2.5">'
        '<error message="kaboom">error detail</error>'
        "</testcase>"
        "</testsuite>"
        "</testsuites>"
    )
    junit = evidence_dir / "junit.xml"
    junit.write_text(xml, encoding="utf-8")
    assert junit.stat().st_size > 50_000
    original = junit.read_bytes()

    prompt = _review_prompt(make_task, evidence_dir)

    assert "JUnit summary" in prompt
    assert "suite alpha: tests=2 failures=1 errors=0 skipped=3 time=1.25" in prompt
    assert "suite beta: tests=1 failures=0 errors=1 skipped=0 time=2.5" in prompt
    assert "tests.test_alpha.test_bad [failure]" in prompt
    assert "tests.test_beta.test_error [error]" in prompt
    assert "boom first line" in prompt and "failure body line one" in prompt
    assert "kaboom" in prompt
    assert "- test_ok" in prompt and "- test_bad" in prompt and "- test_error" in prompt
    assert padding not in prompt
    assert junit.read_bytes() == original


@pytest.mark.parametrize("root", ["testsuite", "testsuites"])
@pytest.mark.parametrize("namespaced", [False, True])
def test_review_prompt_junit_passing_names(make_task, tmp_path: Path, root, namespaced):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    xmlns = ' xmlns="http://example.invalid/junit"' if namespaced else ""
    cases = "".join(
        f'<testcase classname="tests.regression" name="test_new_case_{i}"/>' for i in range(5)
    )
    padding = "Z" * 55000
    attrs = 'name="passing" tests="5" failures="0" errors="0" skipped="0" time="0.5"'
    if root == "testsuites":
        suite = f"<testsuite {attrs}>{cases}<system-out>{padding}</system-out></testsuite>"
        body = f"<testsuites{xmlns}>{suite}</testsuites>"
    else:
        body = (
            f"<testsuite {attrs}{xmlns}>{cases}" f"<system-out>{padding}</system-out></testsuite>"
        )
    junit = evidence_dir / "junit.xml"
    junit.write_text(f'<?xml version="1.0" encoding="UTF-8"?>{body}', encoding="utf-8")
    assert junit.stat().st_size > 50_000

    prompt = _review_prompt(make_task, evidence_dir)

    assert "JUnit summary" in prompt
    assert "suite passing: tests=5 failures=0 errors=0 skipped=0 time=0.5" in prompt
    assert "test names (5 of 5):" in prompt
    for i in range(5):
        assert f"- test_new_case_{i}" in prompt


def test_review_prompt_junit_malformed_falls_back(make_task, tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    junit = evidence_dir / "junit.xml"
    junit.write_text("<testsuites><testsuite tests='1'>" + "x" * 60000, encoding="utf-8")
    original = junit.read_bytes()
    size = junit.stat().st_size

    prompt = _review_prompt(make_task, evidence_dir)

    assert f"- junit.xml: `{junit.resolve()}` ({size} bytes; not inlined)" in prompt
    assert "JUnit summary" not in prompt
    assert "readable copy" not in prompt
    assert not (evidence_dir / "junit.xml.readable.txt").exists()
    assert junit.read_bytes() == original


def test_review_prompt_small_junit_remains_inline(make_task, tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    xml = '<testsuites><testsuite tests="1"><testcase name="test_x"/></testsuite></testsuites>'
    (evidence_dir / "junit.xml").write_text(xml, encoding="utf-8")

    prompt = _review_prompt(make_task, evidence_dir)

    assert "### junit.xml" in prompt
    assert xml in prompt
    assert "JUnit summary" not in prompt


def test_review_prompt_junit_names_are_capped(make_task, tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    cases = "".join(f'<testcase classname="tests.cap" name="test_{i}"/>' for i in range(349))
    cases += (
        '<testcase classname="tests.cap" name="test_349">'
        '<failure message="late fail"/></testcase>'
    )
    xml = (
        '<testsuites><testsuite name="cap" tests="350" failures="1" errors="0" '
        f'skipped="0" time="1.0">{cases}</testsuite></testsuites>'
    )
    (evidence_dir / "junit.xml").write_text(xml, encoding="utf-8")

    prompt = _review_prompt(make_task, evidence_dir)

    assert "test names (300 of 350):" in prompt
    assert "- test_0" in prompt and "- test_299" in prompt
    assert "- test_300" not in prompt
    assert "[50 more test names not shown]" in prompt
    assert "tests.cap.test_349 [failure]" in prompt
    assert "late fail" in prompt


@pytest.mark.parametrize("mode", ["per_file", "cumulative"])
def test_review_prompt_junit_summary_respects_inline_budget(
    make_task, tmp_path: Path, monkeypatch, mode
):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    filler_bytes = 0
    limit = EVIDENCE_INLINE_LIMIT
    if mode == "per_file":
        limit = 400
        monkeypatch.setattr("helmraiser.loop.review.EVIDENCE_INLINE_LIMIT", limit)
    else:
        monkeypatch.setattr("helmraiser.loop.review.EVIDENCE_INLINE_TOTAL", 2000)
        filler_bytes = 1500
        (evidence_dir / "filler.txt").write_text("f" * filler_bytes, encoding="utf-8")
    cases = "".join(
        f'<testcase classname="tests.\u00e9" name="test_\u00e9_{i}">'
        f'<failure message="\u00e9chec num\u00e9ro {i} with some padding text">'
        f'{"d\u00e9tail" * 5}</failure></testcase>'
        for i in range(20)
    )
    xml = (
        '<testsuites><testsuite name="\u00e9" tests="20" failures="20" errors="0" '
        f'skipped="0" time="3.0">{cases}</testsuite></testsuites>'
    )
    (evidence_dir / "junit.xml").write_text(xml, encoding="utf-8")

    prompt = _review_prompt(make_task, evidence_dir)

    assert "JUnit summary truncated" in prompt
    excerpt = _summary_block(prompt)
    available = limit if mode == "per_file" else 2000 - filler_bytes
    assert len(excerpt.encode("utf-8")) <= available
    assert "truncated" in excerpt
    sidecar = evidence_dir / "junit.xml.readable.txt"
    if not sidecar.exists():
        candidates = sorted(evidence_dir.glob("junit.xml.readable*.txt"))
        assert candidates, "expected a readable summary sidecar"
        sidecar = candidates[0]
    assert str(sidecar.resolve()) in prompt
    sidecar_text = sidecar.read_text(encoding="utf-8")
    for i in range(20):
        assert f"tests.\u00e9.test_\u00e9_{i} [failure]" in sidecar_text


def test_review_prompt_junit_readable_copy_is_safe(make_task, tmp_path: Path, monkeypatch):
    monkeypatch.setattr("helmraiser.loop.review.EVIDENCE_INLINE_LIMIT", 300)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    conflict = evidence_dir / "junit.xml.readable.txt"
    conflict.write_text("do not touch\n", encoding="utf-8")
    cases = "".join(
        f'<testcase classname="tests" name="t{i}"><failure message="m{i}"/></testcase>'
        for i in range(20)
    )
    junit = evidence_dir / "junit.xml"
    junit.write_text(f'<testsuite name="s" tests="20" failures="20">{cases}</testsuite>')
    original = junit.read_bytes()

    first = _review_prompt(make_task, evidence_dir)
    assert conflict.read_text(encoding="utf-8") == "do not touch\n"
    chosen = evidence_dir / "junit.xml.readable.1.txt"
    assert chosen.exists()
    assert str(chosen.resolve()) in first

    second = _review_prompt(make_task, evidence_dir)
    assert str(chosen.resolve()) in second
    names = sorted(path.name for path in evidence_dir.glob("*.txt"))
    assert "junit.xml.readable.txt" in names
    assert "junit.xml.readable.txt.readable.txt" not in names
    assert junit.read_bytes() == original


def test_review_prompt_junit_readable_write_failure_falls_back(
    make_task, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("helmraiser.loop.review.EVIDENCE_INLINE_LIMIT", 300)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    cases = "".join(
        f'<testcase classname="tests" name="t{i}"><failure message="m{i}"/></testcase>'
        for i in range(20)
    )
    (evidence_dir / "junit.xml").write_text(
        f'<testsuite name="s" tests="20" failures="20">{cases}</testsuite>'
    )

    real_open = Path.open

    def failing_open(self, *args, **kwargs):
        if ".readable" in self.name:
            raise OSError("no space left on device")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)

    prompt = _review_prompt(make_task, evidence_dir)

    assert "JUnit summary" in prompt
    assert "unavailable" in prompt
    assert "junit.xml.readable.txt" not in prompt
    assert not (evidence_dir / "junit.xml.readable.txt").exists()


@pytest.mark.parametrize("name", ["big.txt", "data.xml"])
def test_review_prompt_long_line_readable_copy(make_task, tmp_path: Path, name):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    source = "x" * (50 * 1024)
    if name.endswith(".xml"):
        source = f"<root>{source}</root>"
    path = evidence_dir / name
    path.write_text(source, encoding="utf-8")
    original = path.read_bytes()

    prompt = _review_prompt(make_task, evidence_dir)

    readable = evidence_dir / f"{name}.readable.txt"
    assert f"- {name}: `{path.resolve()}`" in prompt
    assert f"- {name} readable copy: `{readable.resolve()}`" in prompt
    assert readable.exists()
    content = readable.read_text(encoding="utf-8")
    assert all(len(line) <= 2000 for line in content.split("\n"))
    assert content.replace("\n", "") == source
    assert path.read_bytes() == original


def test_review_prompt_readable_copy_for_total_budget_fallback(
    make_task, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("helmraiser.loop.review.EVIDENCE_INLINE_TOTAL", 100)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    path = evidence_dir / "long.txt"
    path.write_text("y" * 2500, encoding="utf-8")

    prompt = _review_prompt(make_task, evidence_dir)

    readable = evidence_dir / "long.txt.readable.txt"
    assert f"- long.txt: `{path.resolve()}` (2500 bytes; not inlined)" in prompt
    assert f"- long.txt readable copy: `{readable.resolve()}`" in prompt
    assert readable.exists()


def test_review_prompt_short_lines_need_no_readable_copy(make_task, tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    path = evidence_dir / "log.txt"
    path.write_text("\n".join(f"line {i}" for i in range(5000)), encoding="utf-8")
    assert path.stat().st_size > EVIDENCE_INLINE_LIMIT

    prompt = _review_prompt(make_task, evidence_dir)

    assert f"- log.txt: `{path.resolve()}`" in prompt
    assert "readable copy" not in prompt
    assert not (evidence_dir / "log.txt.readable.txt").exists()


@pytest.mark.parametrize(
    "content", [b"\xff\xfe" + b"a" * 60000, b"\x00\x01" + b"a" * 60000], ids=["bom", "nul"]
)
def test_review_prompt_binary_evidence_has_no_readable_copy(make_task, tmp_path: Path, content):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    path = evidence_dir / "blob.bin"
    path.write_bytes(content)

    prompt = _review_prompt(make_task, evidence_dir)

    assert f"- blob.bin: `{path.resolve()}`" in prompt
    assert "readable copy" not in prompt
    assert not (evidence_dir / "blob.bin.readable.txt").exists()


def test_review_prompt_readable_copy_handles_write_failure(make_task, tmp_path: Path, monkeypatch):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    path = evidence_dir / "long.txt"
    path.write_text("z" * 60000, encoding="utf-8")

    real_open = Path.open

    def failing_open(self, *args, **kwargs):
        if ".readable" in self.name:
            raise OSError("disk full")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)

    prompt = _review_prompt(make_task, evidence_dir)

    assert f"- long.txt: `{path.resolve()}` (60000 bytes; not inlined)" in prompt
    assert "readable copy" not in prompt
    assert not (evidence_dir / "long.txt.readable.txt").exists()


class TestParseVerdict:
    @pytest.mark.parametrize("closing", ["```", " \t``` \t", "```\n"])
    def test_backticks_in_findings_do_not_close_block(self, closing):
        findings = [{"severity": "minor", "note": 'Handle "```json" and inline ``` fences'}]
        text = '```json\n{"verdict": "pass"}\n```\nThen:\n'
        text += "```json\n" + json.dumps({"verdict": "concerns", "findings": findings})
        text += f"\n{closing}"

        verdict = parse_verdict(text)

        assert verdict.parsed and verdict.verdict == "concerns"
        assert verdict.findings == findings

    @pytest.mark.parametrize(
        "text",
        [
            '```json\n{"verdict": "pass"} ```',
            '```json\n{"verdict": "pass"}\n``` trailing text',
            'prefix ```json\n{"verdict": "pass"}\n```',
            '```json extra\n{"verdict": "pass"}\n```',
            '```json\n{"verdict": "pass"}\n',
        ],
    )
    def test_only_line_delimited_fences_complete_a_block(self, text):
        verdict = parse_verdict(text)
        assert not verdict.parsed and verdict.verdict == "uncertain"
        assert "no fenced json block" in verdict.error

    def test_last_json_block_wins(self):
        text = (
            '```json\n{"verdict": "pass"}\n```\nOn reflection:\n'
            '```json\n{"verdict": "concerns", "findings": [{"severity": "minor", '
            '"file": "a.py", "line": 3, "note": "typo"}]}\n```\n'
        )
        verdict = parse_verdict(text)
        assert verdict.verdict == "concerns" and verdict.parsed
        assert verdict.findings[0]["file"] == "a.py" and verdict.severe == []

    @pytest.mark.parametrize(
        ("text", "error"),
        [
            ("No block here.", "no fenced json block"),
            ("```json\n{not json\n```", "not valid JSON"),
            ('```json\n{"verdict": "maybe"}\n```', "no valid 'verdict'"),
            ('```json\n["pass"]\n```', "no valid 'verdict'"),
        ],
    )
    def test_missing_or_invalid_block_is_uncertain(self, text: str, error: str):
        verdict = parse_verdict(text)
        assert verdict.verdict == "uncertain" and not verdict.parsed
        assert error in (verdict.error or "")

    @pytest.mark.parametrize("field", ["findings", "evidence_checks"])
    @pytest.mark.parametrize("value", ["oops", "", {}, None, False, 0])
    def test_wrong_container_type_is_uncertain(self, field, value):
        payload = {"verdict": "pass", field: value}
        verdict = parse_verdict(f"```json\n{json.dumps(payload)}\n```")

        assert verdict.verdict == "uncertain" and not verdict.parsed
        assert verdict.error == f"verdict block '{field}' must be a list"
        assert verdict.findings == [] and verdict.evidence_checks == []

    @pytest.mark.parametrize(
        ("field", "valid"),
        [
            ("findings", {"severity": "minor", "note": "typo"}),
            ("evidence_checks", {"name": "shots", "meets_criteria": True}),
        ],
    )
    @pytest.mark.parametrize("invalid", ["not-an-object", 42, None, [], False])
    def test_mixed_valid_and_invalid_entries_are_uncertain(self, field, valid, invalid):
        payload = {"verdict": "pass", field: [valid, invalid, valid]}
        verdict = parse_verdict(f"```json\n{json.dumps(payload)}\n```")

        assert verdict.verdict == "uncertain" and not verdict.parsed
        assert verdict.error == f"verdict block '{field}[1]' must be an object"
        assert verdict.findings == [] and verdict.evidence_checks == []

    @pytest.mark.parametrize("fields", [{}, {"findings": [], "evidence_checks": []}])
    def test_omitted_or_empty_lists_are_valid(self, fields):
        payload = {"verdict": "pass", **fields}
        verdict = parse_verdict(f"```json\n{json.dumps(payload)}\n```")

        assert verdict.verdict == "pass" and verdict.parsed and verdict.error is None
        assert verdict.findings == [] and verdict.evidence_checks == []

    def test_valid_entries_are_preserved(self):
        findings = [{"severity": "minor", "note": "typo"}]
        checks = [{"name": "shots", "meets_criteria": None}]
        verdict = parse_verdict(verdict_text("concerns", findings, evidence_checks=checks))

        assert verdict.verdict == "concerns" and verdict.parsed and verdict.error is None
        assert verdict.findings == findings and verdict.evidence_checks == checks


class TestMergeTable:
    @pytest.mark.parametrize(
        ("policy", "verdict", "action", "status"),
        [
            ("auto", "pass", "finalize", "landed"),
            ("auto", "concerns", "keep", "awaiting_approval"),
            ("auto", "uncertain", "keep", "awaiting_approval"),
            ("review", "pass", "keep", "awaiting_approval"),
            ("review", "uncertain", "keep", "awaiting_approval"),
            ("approve-plan-first", "pass", "keep", "awaiting_approval"),
        ],
    )
    def test_policy_table(self, policy: str, verdict: str, action: str, status: str):
        calls: list[str] = []
        decision = decide(policy, verdict, lambda: calls.append("finalized"))
        assert decision == MergeDecision(action, status)
        assert calls == (["finalized"] if action == "finalize" else [])

    def test_finalize_refusal_awaits_with_reason(self):
        def refuse() -> None:
            raise RuntimeError("primary checkout is dirty; refusing local finalization")

        decision = decide("auto", "pass", refuse)
        assert decision.action == "finalize_refused" and decision.status == "awaiting_approval"
        assert decision.failure_reason == (
            "finalize_refused: primary checkout is dirty; refusing local finalization"
        )

    def test_unknown_policy(self):
        with pytest.raises(ValueError, match="unknown policy"):
            decide("yolo", "pass", lambda: None)


# ── governed run: evidence → review → merge ──────────────────────────


async def test_evidence_and_review_flow(make_task, make_run, ledger: Ledger, data_dir: Path):
    task: TaskSpec = make_task(evidence=EVIDENCE)
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": PiScript(
                final_text=verdict_text(
                    "concerns",
                    [{"severity": "minor", "file": "done.txt", "line": 1, "note": "no newline"}],
                    evidence_checks=[{"name": "shots", "meets_criteria": True}],
                )
            ),
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval" and outcome.failure_reason is None
    assert outcome.pending_approval == "merge"
    run_dir = data_dir / "runs" / run.run_id

    # Evidence ran in the worktree with HELMRAISER_EVIDENCE_DIR; artifacts expanded; ledger row.
    (evidence,) = outcome.evidence_results
    assert evidence["ok"] and evidence["artifacts"] == ["out.txt"] and evidence["missing"] == []
    assert (run_dir / "evidence" / "out.txt").read_text() == "hello\n"
    (row,) = ledger.evidence(run.run_id)
    assert row["ok"] == 1 and row["artifacts"] == ["out.txt"]
    assert "done.txt" in (run_dir / "diff.patch").read_text()

    # Review: one clean-context session with the review model, read-only tools.
    review = adapter.calls_for("review")[0]
    assert review.session_id == f"{run.run_id}-review"
    assert review.model.render() == "openai-codex/gpt-6-astra:high"
    assert review.tools == ("read", "grep", "find", "ls") and review.context_files is False
    assert review.cwd == run.worktree
    assert review.system_prompt_path == run_dir / "directives-review.md"
    assert "independent review" in review.system_prompt_path.read_text()
    prompt = review.prompt
    assert "Make the widget work." in prompt and "## Acceptance" in prompt
    assert "```diff" in prompt and "+ok" in prompt
    assert "### marker: pass" in prompt
    assert "### shots: collected" in prompt and "Criteria: out.txt says hello" in prompt
    assert f"`{run_dir / 'evidence' / 'out.txt'}`" in prompt
    assert prompt.rstrip().endswith("Never modify files.")
    assert '"verdict": "pass" | "concerns" | "uncertain"' in prompt

    # Verdict recorded everywhere.
    assert outcome.verdict["verdict"] == "concerns" and outcome.verdict["forced"] is None
    assert ledger.get_run(run.run_id)["verdict"] == "concerns"
    assert (run_dir / "review.md").read_text() == adapter.scripts["review"].final_text
    events = {e["type"]: e for e in ledger.events(run.run_id)}
    assert events["review_verdict"]["payload"]["verdict"] == "concerns"
    assert events["review_verdict"]["stage"] == "review"
    assert events["evidence_result"]["payload"]["name"] == "shots"
    assert events["merge_decision"]["payload"] == {
        "action": "keep",
        "status": "awaiting_approval",
        "verdict": "concerns",
        "failure_reason": None,
    }
    summary = (run_dir / "summary.md").read_text()
    assert "### Review: concerns" in summary and "[minor] done.txt:1 — no newline" in summary
    assert "- shots: ok" in summary
    stage_events = [
        e["payload"]["stage"] for e in ledger.events(run.run_id) if e["type"] == "stage_start"
    ]
    assert stage_events[:3] == ["implement", "review", "merge"]


async def test_failed_evidence_forces_uncertain_but_review_still_runs(
    make_task, make_run, ledger, data_dir
):
    task = make_task(
        evidence=[
            {"name": "shots", "run": "true", "artifacts": ["never.png"], "criteria": "c"},
        ]
    )
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "")]),
            "review": PiScript(final_text=verdict_text("pass")),
        }
    )
    run, _ = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval"
    assert outcome.verdict["verdict"] == "uncertain"
    assert outcome.verdict["forced"] == "evidence not ok: shots"
    assert ledger.get_run(run.run_id)["verdict"] == "uncertain"
    (review,) = adapter.calls_for("review")
    assert "### shots: NOT OK (ok exit, no file for never.png)" in review.prompt
    assert (
        "- forced: evidence not ok: shots"
        in (data_dir / "runs" / run.run_id / "summary.md").read_text()
    )


async def test_pass_with_unmet_evidence_check_becomes_concerns(make_task, make_run, ledger):
    task = make_task(evidence=EVIDENCE)
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "")]),
            "review": PiScript(
                final_text=verdict_text(
                    "pass",
                    evidence_checks=[{"name": "shots", "meets_criteria": False}],
                )
            ),
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.verdict["verdict"] == "concerns"
    assert outcome.verdict["forced"] == "evidence check shots not met"
    event = next(e for e in ledger.events(run.run_id) if e["type"] == "review_verdict")
    assert event["payload"]["forced"] == "evidence check shots not met"


async def test_pass_with_unassessed_evidence_check_becomes_uncertain(make_task, make_run, ledger):
    task = make_task(evidence=EVIDENCE)
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "")]),
            "review": PiScript(
                final_text=verdict_text(
                    "pass",
                    evidence_checks=[{"name": "shots", "meets_criteria": None}],
                )
            ),
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.verdict["verdict"] == "uncertain"
    assert outcome.verdict["forced"] == "evidence check shots not assessed"
    event = next(e for e in ledger.events(run.run_id) if e["type"] == "review_verdict")
    assert event["payload"]["forced"] == "evidence check shots not assessed"


async def test_pass_without_evidence_checks_becomes_uncertain(make_task, make_run, ledger):
    task = make_task(evidence=EVIDENCE)
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "")]),
            "review": PiScript(final_text=verdict_text("pass")),
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.verdict["verdict"] == "uncertain"
    assert outcome.verdict["forced"] == "no evidence checks"
    event = next(e for e in ledger.events(run.run_id) if e["type"] == "review_verdict")
    assert event["payload"]["forced"] == "no evidence checks"


async def test_pass_without_evidence_steps_stays_pass(make_task, make_run, ledger):
    task = make_task()
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "")]),
            "review": PiScript(final_text=verdict_text("pass")),
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.verdict["verdict"] == "pass"
    assert outcome.verdict["forced"] is None
    event = next(e for e in ledger.events(run.run_id) if e["type"] == "review_verdict")
    assert event["payload"]["forced"] is None


async def test_image_artifacts_are_attached_and_large_diff_goes_by_path(
    make_task, make_run, data_dir, monkeypatch
):
    monkeypatch.setattr("helmraiser.loop.review.DIFF_INLINE_LIMIT", 10)
    task = make_task(
        evidence=[
            {
                "name": "shots",
                "run": 'printf x > "$HELMRAISER_EVIDENCE_DIR/home.png"; printf y > "$HELMRAISER_EVIDENCE_DIR/log.txt"',
                "artifacts": ["*.png", "log.txt"],
                "criteria": "screenshot shows the widget",
            }
        ]
    )
    adapter = ScriptedPiAdapter({"implement": PiScript(files=[("done.txt", "")])})
    run, _ = make_run(task, adapter)
    await run.run()
    prompt = adapter.calls_for("review")[0].prompt
    evidence_dir = data_dir / "runs" / run.run_id / "evidence"
    assert f"- home.png (image, attached): @{evidence_dir / 'home.png'}" in prompt
    assert prompt.count(f"@{evidence_dir / 'home.png'}") == 1
    assert f"- log.txt: `{evidence_dir / 'log.txt'}`" in prompt
    assert "```diff" not in prompt
    assert f"read it from `{data_dir / 'runs' / run.run_id / 'diff.patch'}`" in prompt


async def test_review_without_json_block_is_uncertain(make_task, make_run, ledger):
    task = make_task()
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "")]),
            "review": PiScript(final_text="Looks fine to me, no block."),
        }
    )
    run, _ = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.verdict["verdict"] == "uncertain" and outcome.verdict["parsed"] is False
    assert "no fenced json block" in outcome.verdict["error"]
    assert ledger.get_run(run.run_id)["verdict"] == "uncertain"


async def test_review_session_error_is_uncertain_not_failure(make_task, make_run, ledger):
    task = make_task()
    err = PiScript(ok=False, error_kind="provider", error_message="502 upstream")
    adapter = ScriptedPiAdapter(
        {"implement": PiScript(files=[("done.txt", "")]), "review": [err, err]}
    )
    run, sleep = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval"
    assert outcome.verdict["verdict"] == "uncertain"
    assert outcome.verdict["forced"] == "review session failed: provider"
    assert sleep.calls == [30.0]  # one retry, same as implement
    assert len(adapter.calls_for("review")) == 2


async def test_review_quota_holds_provider_without_pausing_queue(make_task, make_run, ledger):
    task = make_task()
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "")]),
            "review": PiScript(ok=False, error_kind="quota", error_message="429 rate limit"),
        }
    )
    run, _ = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.status == "failed" and outcome.failure_reason == "quota: 429 rate limit"
    assert ledger.is_paused() is None
    assert "openai-codex" in ledger.provider_holds(run.clock())


async def test_review_cost_counts_but_does_not_kill_the_review(make_task, make_run, ledger):
    from helmraiser.pi.protocol import Usage

    task = make_task(max_cost_usd=1.0)
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "")], usage=Usage(1, 1, 0.9)),
            "review": PiScript(final_text=verdict_text("pass"), usage=Usage(1, 1, 0.5)),
        }
    )
    run, _ = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval" and outcome.cost_usd == 1.4
    assert ledger.get_run(run.run_id)["cost_usd"] == 1.4


# ── policy auto ──────────────────────────────────────────────────────


async def test_auto_pass_lands_via_finalize_local(make_task, make_run, ledger, tmp_project):
    task = make_task(policy="auto", evidence=EVIDENCE)
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": PiScript(final_text=verdict_text("pass", evidence_checks=SHOTS_MET)),
        }
    )
    run, _ = make_run(task, adapter)
    outcome = await run.run()

    assert outcome.status == "landed" and outcome.failure_reason is None
    assert outcome.pending_approval is None
    assert _git(tmp_project, "rev-parse", "HEAD") == outcome.head_commit
    assert (tmp_project / "done.txt").read_text() == "ok\n"
    assert not Path(outcome.worktree_path).exists()
    assert outcome.branch_name not in _git(tmp_project, "branch", "--list")
    assert ledger.get_run(run.run_id)["status"] == "landed"
    assert ledger.get_task(task.task_id)["status"] == "landed"
    stage_events = [
        e["payload"]["stage"] for e in ledger.events(run.run_id) if e["type"] == "stage_start"
    ]
    assert stage_events[:2] == ["implement", "review"]


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("findings", "oops", "'findings' must be a list"),
        (
            "findings",
            [{"severity": "minor", "note": "ok"}, "not-a-finding"],
            "'findings[1]' must be an object",
        ),
        ("evidence_checks", {"name": "shots"}, "'evidence_checks' must be a list"),
        ("evidence_checks", [*SHOTS_MET, 42], "'evidence_checks[1]' must be an object"),
    ],
)
async def test_auto_malformed_review_cannot_land(
    make_task, make_run, ledger, tmp_project, field, value, error
):
    task = make_task(policy="auto", evidence=EVIDENCE)
    payload = {"verdict": "pass", "findings": [], "evidence_checks": SHOTS_MET, field: value}
    review_text = f"```json\n{json.dumps(payload)}\n```"
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": PiScript(final_text=review_text),
        }
    )
    primary_head = _git(tmp_project, "rev-parse", "HEAD")
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval" and outcome.pending_approval == "merge"
    assert outcome.failure_reason is None
    assert all(result.passed for result in run.last_results)
    assert outcome.evidence_results[0]["ok"]
    assert outcome.verdict["verdict"] == "uncertain" and not outcome.verdict["parsed"]
    assert error in outcome.verdict["error"]
    assert outcome.verdict["forced"] is None
    assert _git(tmp_project, "rev-parse", "HEAD") == primary_head == outcome.base_commit
    assert not (tmp_project / "done.txt").exists()
    assert (Path(outcome.worktree_path) / "done.txt").read_text() == "ok\n"
    assert ledger.get_run(run.run_id)["verdict"] == "uncertain"
    event = next(e for e in ledger.events(run.run_id) if e["type"] == "review_verdict")
    assert event["payload"]["parsed"] is False and error in event["payload"]["error"]


async def test_auto_pass_opens_pull_request(
    make_task, make_run, ledger, tmp_project, tmp_path, monkeypatch
):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(tmp_project, "remote", "add", "origin", str(remote))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "gh-calls"
    gh = bin_dir / "gh"
    gh.write_text(
        f'#!/bin/sh\necho "$*" >> {calls}\necho https://example.test/pr/1\n',
        encoding="utf-8",
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    task = make_task(policy="auto", evidence=EVIDENCE, delivery="pr")
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": PiScript(final_text=verdict_text("pass", evidence_checks=SHOTS_MET)),
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "landed" and outcome.pr_url == "https://example.test/pr/1"
    argv = calls.read_text(encoding="utf-8")
    row = ledger.get_run(run.run_id)
    assert f"--base {row['base_branch']} --head {outcome.branch_name}" in argv
    assert row["delivery"] == "pr" and row["pr_url"] == outcome.pr_url
    delivered = [event for event in ledger.events(run.run_id) if event["type"] == "delivered"]
    assert delivered[0]["payload"] == {"pr_url": outcome.pr_url}


async def test_missing_explicit_base_fails_at_worktree(make_task, make_run):
    task = make_task(base="programme/missing")
    run, _ = make_run(task, ScriptedPiAdapter({}))

    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.failure_reason == "worktree: base branch programme/missing does not exist"


async def test_auto_pull_request_uses_explicit_base(
    make_task, make_run, tmp_project, tmp_path, monkeypatch
):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(tmp_project, "remote", "add", "origin", str(remote))
    _git(tmp_project, "checkout", "-b", "programme/x")
    (tmp_project / "programme.txt").write_text("programme\n", encoding="utf-8")
    _git(tmp_project, "add", "programme.txt")
    _git(tmp_project, "commit", "-m", "programme")
    _git(tmp_project, "checkout", "main")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "gh-calls"
    gh = bin_dir / "gh"
    gh.write_text(
        f'#!/bin/sh\necho "$*" >> {calls}\necho https://example.test/pr/1\n',
        encoding="utf-8",
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    task = make_task(policy="auto", evidence=EVIDENCE, delivery="pr", base="programme/x")
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": PiScript(final_text=verdict_text("pass", evidence_checks=SHOTS_MET)),
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "landed"
    assert f"--base programme/x --head {outcome.branch_name}" in calls.read_text(encoding="utf-8")
    assert (Path(outcome.worktree_path) / "programme.txt").exists() is False


async def test_auto_pr_push_failure_awaits_without_calling_gh(
    make_task, make_run, ledger, tmp_project, tmp_path, monkeypatch
):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(tmp_project, "remote", "add", "origin", str(remote))
    calls = tmp_path / "gh-calls"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!/bin/sh\necho called >> {calls}\n", encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    task = make_task(policy="auto", evidence=EVIDENCE, delivery="pr")
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": PiScript(final_text=verdict_text("pass", evidence_checks=SHOTS_MET)),
        }
    )
    run, _ = make_run(task, adapter)
    review = run.run_review

    async def review_then_remove_remote(plan: str | None = None) -> str:
        verdict = await review(plan)
        shutil.rmtree(remote)
        return verdict

    run.run_review = review_then_remove_remote  # type: ignore[method-assign]
    outcome = await run.run()

    assert outcome.status == "awaiting_approval"
    assert outcome.failure_reason and outcome.failure_reason.startswith("finalize_refused:")
    assert not calls.exists()
    assert ledger.get_run(run.run_id)["pr_url"] is None


class _NoSignals:
    """ProcessController for abort tests: the scripted run holds no lease to signal."""

    def terminate(self, identity):  # type: ignore[no-untyped-def]
        raise AssertionError("no process to signal")

    kill = terminate

    def is_running(self, identity):  # type: ignore[no-untyped-def]
        return False


async def test_auto_abort_after_review_pass_does_not_land(make_task, make_run, ledger, tmp_project):
    task = make_task(policy="auto", evidence=EVIDENCE)
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": PiScript(final_text=verdict_text("pass", evidence_checks=SHOTS_MET)),
        }
    )
    run, _ = make_run(task, adapter)
    review = run.run_review

    async def review_then_abort(plan: str | None = None) -> str:
        verdict = await review(plan)
        assert abort_run(tmp_project, run.run_id, _NoSignals())
        return verdict

    run.run_review = review_then_abort  # type: ignore[method-assign]
    outcome = await run.run()

    assert (outcome.status, outcome.failure_reason) == (
        "aborted",
        "aborted: before the merge decision",
    )
    assert outcome.verdict is not None and outcome.verdict["verdict"] == "pass"
    assert _git(tmp_project, "rev-parse", "HEAD") == outcome.base_commit
    assert not (tmp_project / "done.txt").exists()
    assert Path(outcome.worktree_path).is_dir()
    assert outcome.branch_name in _git(tmp_project, "branch", "--list")
    assert ledger.get_run(run.run_id)["status"] == "aborted"
    assert ledger.get_task(task.task_id)["status"] == "aborted"
    assert run.state.get_run(run.run_id)["status"] == "aborted"
    assert "merge_decision" not in [e["type"] for e in ledger.events(run.run_id)]


@pytest.mark.parametrize("verdict", ["concerns", "uncertain"])
async def test_auto_without_pass_awaits(make_task, make_run, tmp_project, verdict):
    task = make_task(policy="auto", evidence=EVIDENCE)
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": PiScript(final_text=verdict_text(verdict, evidence_checks=SHOTS_MET)),
        }
    )
    run, _ = make_run(task, adapter)
    outcome = await run.run()
    assert outcome.status == "awaiting_approval" and outcome.pending_approval == "merge"
    assert Path(outcome.worktree_path).is_dir()
    assert _git(tmp_project, "rev-parse", "HEAD") == outcome.base_commit


async def test_auto_finalize_refused_awaits_with_reason(make_task, make_run, tmp_project, ledger):
    task = make_task(policy="auto", evidence=EVIDENCE)
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": PiScript(final_text=verdict_text("pass", evidence_checks=SHOTS_MET)),
        }
    )
    run, _ = make_run(task, adapter)
    (tmp_project / "README.md").write_text("dirty primary checkout\n")  # finalize refuses
    outcome = await run.run()
    assert outcome.status == "awaiting_approval"
    assert outcome.failure_reason == (
        "finalize_refused: primary checkout is dirty; refusing local finalization"
    )
    assert Path(outcome.worktree_path).is_dir()
    row = ledger.get_run(run.run_id)
    assert row["status"] == "awaiting_approval" and row["failure_reason"].startswith(
        "finalize_refused"
    )
    assert row["verdict"] == "pass"


async def test_auto_pr_gh_failure_awaits_and_keeps_the_worktree(
    make_task, make_run, ledger, tmp_project, tmp_path, monkeypatch
):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(tmp_project, "remote", "add", "origin", str(remote))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\necho 'gh: HTTP 502' >&2\nexit 1\n", encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    task = make_task(policy="auto", evidence=EVIDENCE, delivery="pr")
    adapter = ScriptedPiAdapter(
        {
            "implement": PiScript(files=[("done.txt", "ok\n")]),
            "review": PiScript(final_text=verdict_text("pass", evidence_checks=SHOTS_MET)),
        }
    )
    run, _ = make_run(task, adapter)

    outcome = await run.run()

    assert outcome.status == "awaiting_approval"
    assert outcome.failure_reason == "finalize_refused: gh: HTTP 502"
    assert outcome.worktree_path and Path(outcome.worktree_path).exists()
    assert ledger.get_run(run.run_id)["pr_url"] is None


def test_truncate_lines_never_exceeds_budget():
    from helmraiser.loop.review import _truncate_lines

    text = "\n".join(f"line-{i}" for i in range(20))
    notice = "[truncated]"
    for budget in range(len(notice), len(text) + len(notice) + 3):
        excerpt = _truncate_lines(text, budget, notice)
        assert excerpt is None or len(excerpt.encode("utf-8")) <= budget, budget
