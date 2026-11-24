"""Reviewer stage (spec §4.5.2): prompt assembly and verdict parsing."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from helmraiser.loop.evidence import EvidenceResult
from helmraiser.loop.prompts import acceptance_block, paths_block
from helmraiser.loop.shell import SANDBOX_HOST, UNCONFINED_NOTE
from helmraiser.loop.verify import VerifyResult
from helmraiser.task.model import TaskSpec

VERDICTS: tuple[str, ...] = ("pass", "concerns", "uncertain")
DIFF_INLINE_LIMIT = 200 * 1024
EVIDENCE_INLINE_LIMIT = 8 * 1024
EVIDENCE_INLINE_TOTAL = 64 * 1024
OWNER_NOTE_INLINE_LIMIT = 16 * 1024
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
JUNIT_ROOT_TAGS = frozenset({"testsuites", "testsuite"})
JUNIT_MAX_TEST_NAMES = 300
JUNIT_MESSAGE_LINES = 3
JUNIT_MESSAGE_LINE_LIMIT = 300
READABLE_LINE_LIMIT = 2000

VERDICT_INSTRUCTION = """\
Answer with a fenced JSON block as the LAST thing in your message:
```json
{"verdict": "pass" | "concerns" | "uncertain",
 "findings": [{"severity": "severe"|"minor", "file": "...", "line": 0, "note": "..."}],
 "evidence_checks": [{"name": "...", "meets_criteria": true|false|null, "note": "..."}]}
```
Rules: "pass" only if no severe findings and every evidence check meets its criteria.
Set meets_criteria to null for files you did not read, and true only when their contents
show the criteria are met. Never modify files."""

_JSON_FENCE = re.compile(
    r"^[ \t]*```json[ \t]*\r?\n(.*?)^[ \t]*```[ \t]*\r?$", re.DOTALL | re.MULTILINE
)
_CONTROL_BYTES = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _junit_total(element: ElementTree.Element, name: str) -> str:
    value = element.get(name)
    return value if value else "unknown"


def _junit_test_label(testcase: ElementTree.Element) -> str:
    classname = testcase.get("classname")
    name = testcase.get("name")
    if classname and name:
        return f"{classname}.{name}"
    return name or classname or "(unnamed testcase)"


def _junit_name(testcase: ElementTree.Element) -> str:
    return testcase.get("name") or "(unnamed testcase)"


def _junit_message_lines(node: ElementTree.Element) -> list[str]:
    pieces: list[str] = []
    message = node.get("message")
    if message:
        pieces.append(message)
    if node.text:
        pieces.append(node.text)
    raw = [line.strip() for line in "\n".join(pieces).splitlines() if line.strip()]
    rendered: list[str] = []
    for line in raw[:JUNIT_MESSAGE_LINES]:
        if len(line) > JUNIT_MESSAGE_LINE_LIMIT:
            line = line[:JUNIT_MESSAGE_LINE_LIMIT] + "..."
        rendered.append(line)
    if len(raw) > JUNIT_MESSAGE_LINES:
        rendered.append(f"... ({len(raw) - JUNIT_MESSAGE_LINES} more lines)")
    return rendered


def _junit_summary(text: str) -> str | None:
    """A compact plain-text summary of JUnit XML, or ``None`` if it is not JUnit."""
    root = ElementTree.fromstring(text)
    if _local_name(root.tag) not in JUNIT_ROOT_TAGS:
        return None
    lines = ["JUnit summary"]
    for suite in root.iter():
        if _local_name(suite.tag) != "testsuite":
            continue
        name = suite.get("name")
        lines.append(
            f"{'suite ' + name if name else 'suite'}: "
            f"tests={_junit_total(suite, 'tests')} "
            f"failures={_junit_total(suite, 'failures')} "
            f"errors={_junit_total(suite, 'errors')} "
            f"skipped={_junit_total(suite, 'skipped')} "
            f"time={_junit_total(suite, 'time')}"
        )
    problems: list[str] = []
    names: list[str] = []
    for testcase in root.iter():
        if _local_name(testcase.tag) != "testcase":
            continue
        names.append(_junit_name(testcase))
        for child in testcase:
            kind = _local_name(child.tag)
            if kind not in ("failure", "error"):
                continue
            problems.append(f"- {_junit_test_label(testcase)} [{kind}]")
            problems += [f"  {line}" for line in _junit_message_lines(child)]
    if problems:
        lines.append("failing/erroring tests:")
        lines += problems
    shown = names[:JUNIT_MAX_TEST_NAMES]
    lines.append(f"test names ({len(shown)} of {len(names)}):")
    lines += [f"- {name}" for name in shown]
    if len(names) > len(shown):
        lines.append(f"[{len(names) - len(shown)} more test names not shown]")
    return "\n".join(lines)


def _wrap_readable_text(text: str) -> str:
    wrapped: list[str] = []
    for line in text.split("\n"):
        if len(line) <= READABLE_LINE_LIMIT:
            wrapped.append(line)
            continue
        wrapped += [
            line[index : index + READABLE_LINE_LIMIT]
            for index in range(0, len(line), READABLE_LINE_LIMIT)
        ]
    return "\n".join(wrapped)


def _write_readable_copy(path: Path, text: str) -> Path | None:
    """Write a line-wrapped copy beside ``path`` without overwriting existing files."""
    data = _wrap_readable_text(text).encode("utf-8")
    stem = f"{path.name}.readable"
    candidates = [path.with_name(f"{stem}.txt")]
    candidates += [path.with_name(f"{stem}.{index}.txt") for index in range(1, 100)]
    for candidate in candidates:
        try:
            if candidate.is_symlink():
                continue
            if candidate.exists():
                if candidate.is_file() and candidate.read_bytes() == data:
                    return candidate
                continue
            with candidate.open("xb") as handle:
                handle.write(data)
            return candidate
        except FileExistsError:
            continue
        except OSError:
            return None
    return None


def _has_long_line(text: str) -> bool:
    return any(len(line) > READABLE_LINE_LIMIT for line in text.split("\n"))


def _truncate_lines(text: str, budget: int, notice: str) -> str | None:
    """The largest whole-line prefix of ``text`` plus ``notice`` within ``budget`` bytes."""
    notice_bytes = len(notice.encode("utf-8")) + 1  # the newline before the notice
    if notice_bytes > budget:
        return None
    kept: list[str] = []
    used = 0
    for line in text.split("\n"):
        cost = len(line.encode("utf-8")) + (1 if kept else 0)
        if used + cost + notice_bytes > budget:
            break
        kept.append(line)
        used += cost
    if not kept:
        return None
    return "\n".join(kept) + "\n" + notice


def _inline_junit_summary(
    parts: list[str], rel: str, path: Path, summary: str, available: int
) -> int:
    """Inline a JUnit summary, falling back to a readable sidecar when it is too large."""
    summary_bytes = summary.encode("utf-8")
    if len(summary_bytes) <= available:
        parts += [f"### {rel} (JUnit summary)", "```", summary, "```", ""]
        return len(summary_bytes)
    sidecar = _write_readable_copy(path, summary)
    if sidecar is not None:
        parts.append(f"- {rel} (derived JUnit summary): `{sidecar.resolve()}`")
    notice = "[JUnit summary truncated; see the readable copy for the complete failure list]"
    excerpt = _truncate_lines(summary, available, notice)
    if excerpt is None:
        return 0
    label = f"### {rel} (JUnit summary, truncated)"
    if sidecar is None:
        label = f"### {rel} (JUnit summary, truncated; readable copy unavailable)"
    parts += [label, "```", excerpt, "```", ""]
    return len(excerpt.encode("utf-8"))


@dataclass
class ScopeDecision:
    decision: str
    paths: tuple[str, ...]
    note: str | None


@dataclass
class Verdict:
    verdict: str  # pass | concerns | uncertain
    findings: list[dict[str, Any]] = field(default_factory=list)
    evidence_checks: list[dict[str, Any]] = field(default_factory=list)
    parsed: bool = True
    error: str | None = None
    forced: str | None = None  # why Helmraiser overrode the reviewer's verdict, if it did

    @property
    def severe(self) -> list[dict[str, Any]]:
        return [f for f in self.findings if str(f.get("severity")) == "severe"]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_verdict(text: str) -> Verdict:
    """The LAST fenced ```json block; missing or malformed → ``uncertain``."""
    blocks = _JSON_FENCE.findall(text)
    if not blocks:
        return Verdict("uncertain", parsed=False, error="no fenced json block in the review")
    try:
        data = json.loads(blocks[-1])
    except ValueError as exc:
        return Verdict("uncertain", parsed=False, error=f"verdict block is not valid JSON: {exc}")
    if not isinstance(data, dict) or data.get("verdict") not in VERDICTS:
        return Verdict("uncertain", parsed=False, error="verdict block has no valid 'verdict'")
    for name in ("findings", "evidence_checks"):
        entries = data.get(name, [])
        if not isinstance(entries, list):
            return Verdict(
                "uncertain", parsed=False, error=f"verdict block '{name}' must be a list"
            )
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                return Verdict(
                    "uncertain",
                    parsed=False,
                    error=f"verdict block '{name}[{index}]' must be an object",
                )
    return Verdict(
        verdict=str(data["verdict"]),
        findings=data.get("findings", []),
        evidence_checks=data.get("evidence_checks", []),
    )


def diff_block(diff_text: str, diff_path: Path) -> str:
    """The review prompt's diff: inline below ``DIFF_INLINE_LIMIT``, else a pointer to the file."""
    if len(diff_text.encode("utf-8")) < DIFF_INLINE_LIMIT:
        return "\n".join(["```diff", diff_text.rstrip("\n") or "(empty diff)", "```"])
    return (
        f"The diff is {len(diff_text) // 1024} KB; read it from `{diff_path}` "
        "(and inspect the worktree) before judging."
    )


def build_review_prompt(
    task: TaskSpec,
    *,
    diff_path: Path,
    diff_text: str,
    verify_results: list[VerifyResult],
    evidence_results: list[EvidenceResult],
    evidence_dir: Path,
    plan: str | None = None,
    plan_approved: bool = True,
    plan_approval_note: str | None = None,
    scope_decisions: Sequence[ScopeDecision] = (),
    rework_notes: Sequence[str] = (),
) -> str:
    """``scope_decisions`` and ``rework_notes`` arrive oldest first for this run."""
    parts = [f"# Review: {task.title}"]
    if task.paths is not None:
        parts += ["", paths_block(task).rstrip(), "", "Check the diff against the allowed paths."]
    if plan_approval_note:
        note_bytes = plan_approval_note.encode("utf-8")
        cut = len(note_bytes) > OWNER_NOTE_INLINE_LIMIT
        if cut:
            plan_approval_note = note_bytes[:OWNER_NOTE_INLINE_LIMIT].decode(
                "utf-8", errors="ignore"
            )
        parts += [
            "",
            "## Owner's decisions on approval",
            "",
            "These decisions were made by the owner after the task was written. "
            "They override the task brief and acceptance block where they conflict. "
            "Judge the implementation against these decisions; do not report following "
            "them as a finding.",
            "",
            plan_approval_note,
        ]
        if cut:
            parts += ["", f"[Owner's approval note cut after {OWNER_NOTE_INLINE_LIMIT} bytes.]"]
    if scope_decisions:
        parts += [
            "",
            "## Owner's scope decisions",
            "",
            "These decisions were made by the owner after the task was written. "
            "They override the task brief and acceptance block where they conflict. "
            "Judge the implementation against these decisions; do not report following "
            "them as a finding.",
        ]
        for number, scope in enumerate(scope_decisions, start=1):
            parts += [
                "",
                f"### Decision {number}: {scope.decision.capitalize()}",
                "",
                (
                    "Paths: " + ", ".join(f"`{path}`" for path in scope.paths)
                    if scope.paths
                    else "Paths unavailable in the ledger"
                ),
            ]
            if scope.note is not None:
                note_bytes = scope.note.encode("utf-8")
                parts.append("")
                if len(note_bytes) > OWNER_NOTE_INLINE_LIMIT:
                    parts += [
                        note_bytes[:OWNER_NOTE_INLINE_LIMIT].decode("utf-8", errors="ignore"),
                        "",
                        f"[Scope decision note cut after {OWNER_NOTE_INLINE_LIMIT} bytes.]",
                    ]
                else:
                    parts.append(scope.note)
    if rework_notes:
        parts += [
            "",
            "## Owner's rework requests",
            "",
            "The owner asked for these changes after reviewing earlier rounds, oldest first. "
            "They amend the task brief and acceptance block where they conflict. Judge the "
            "implementation against them; do not report following them as a finding.",
        ]
        for round_n, note in enumerate(rework_notes, start=1):
            note_bytes = note.encode("utf-8")
            parts += ["", f"### Round {round_n}", ""]
            if len(note_bytes) > OWNER_NOTE_INLINE_LIMIT:
                cut_note = note_bytes[:OWNER_NOTE_INLINE_LIMIT].decode("utf-8", errors="ignore")
                parts += [cut_note, "", f"[Rework note cut after {OWNER_NOTE_INLINE_LIMIT} bytes.]"]
            else:
                parts.append(note)

    parts += [
        "",
        "## Task",
        "",
        task.body.strip(),
        "",
        acceptance_block(task),
    ]

    if plan is not None:
        plan_bytes = plan.encode("utf-8")
        cut = len(plan_bytes) > EVIDENCE_INLINE_LIMIT
        if cut:
            plan = plan_bytes[:EVIDENCE_INLINE_LIMIT].decode("utf-8", errors="ignore")
        if plan_approved:
            parts += ["", "## Approved plan", "", plan.rstrip("\n")]
        else:
            lead_in = (
                "A planning model wrote this plan for the task; the implementer should follow "
                "it and say so when deviating."
            )
            parts += ["", "## Plan", "", lead_in, "", plan.rstrip("\n")]
        if cut:
            parts += ["", f"[Plan cut after {EVIDENCE_INLINE_LIMIT} bytes.]"]

    parts += ["", "## Diff", "", diff_block(diff_text, diff_path)]

    parts += ["", "## Verify results", ""]
    for result in verify_results:
        mark = "pass" if result.passed else f"FAIL (exit {result.exit_code})"
        note = f"; {UNCONFINED_NOTE}" if result.sandbox == SANDBOX_HOST else ""
        parts += [
            f"### {result.name}: {mark} in {result.seconds}s{note}",
            "```",
            result.tail or "(no output)",
            "```",
            "",
        ]
    if not verify_results:
        parts.append("(no verify steps)")

    parts += ["", "## Evidence", ""]
    attached_images: set[Path] = set()
    for evidence in evidence_results:
        if evidence.ok:
            status = "collected"
        else:
            detail = f"exit {evidence.exit_code}" if evidence.exit_code != 0 else "ok exit"
            if evidence.missing:
                detail += f", no file for {', '.join(evidence.missing)}"
            status = f"NOT OK ({detail}); Helmraiser will force the verdict to uncertain"
        if evidence.sandbox == SANDBOX_HOST:
            status += f"; {UNCONFINED_NOTE}"
        parts += [f"### {evidence.name}: {status}", ""]
        parts += [f"Criteria: {evidence.criteria or '(none given)'}", ""]
        for rel in evidence.artifacts:
            path = evidence_dir / rel
            if path.suffix.lower() in IMAGE_SUFFIXES:
                if path not in attached_images:
                    parts.append(f"- {rel} (image, attached): @{path}")
                    attached_images.add(path)
                else:
                    parts.append(f"- {rel} (image; already attached)")
            else:
                parts.append(f"- {rel}: `{path}`")
        if evidence.tail:
            parts += ["", "Output tail:", "```", evidence.tail, "```"]
        if evidence.log:
            parts += ["", f"Full output: `{evidence.log}`"]
        parts.append("")
    if not evidence_results:
        parts.append("(no evidence steps; evidence_checks may be an empty list)")

    parts += ["", "## Evidence files", ""]
    files = sorted(path for path in evidence_dir.rglob("*") if path.is_file())
    if not files:
        parts.append("(evidence directory is empty)")
    inline_bytes = 0
    for path in files:
        rel = path.relative_to(evidence_dir).as_posix()
        size = path.stat().st_size
        if path.suffix.lower() in IMAGE_SUFFIXES:
            if path not in attached_images:
                parts.append(f"- {rel} (image, attached): @{path}")
                attached_images.add(path)
            continue
        over_budget = size > EVIDENCE_INLINE_LIMIT or inline_bytes + size > EVIDENCE_INLINE_TOTAL
        if not over_budget:
            data = path.read_bytes()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is None or _CONTROL_BYTES.search(text):
                parts.append(f"- {rel}: `{path.resolve()}` ({size} bytes; not inlined)")
                continue
            parts += [f"### {rel}", "```", text.rstrip("\n"), "```", ""]
            inline_bytes += size
            continue

        parts.append(f"- {rel}: `{path.resolve()}` ({size} bytes; not inlined)")
        try:
            data = path.read_bytes()
        except OSError:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if _CONTROL_BYTES.search(text):
            continue
        if path.suffix.lower() == ".xml":
            try:
                summary = _junit_summary(text)
            except (ElementTree.ParseError, ValueError):
                continue
            if summary is not None:
                available = min(EVIDENCE_INLINE_LIMIT, EVIDENCE_INLINE_TOTAL - inline_bytes)
                inline_bytes += _inline_junit_summary(parts, rel, path, summary, available)
                continue
        if _has_long_line(text):
            readable = _write_readable_copy(path, text)
            if readable is not None:
                parts.append(f"- {rel} readable copy: `{readable.resolve()}`")

    parts += ["", VERDICT_INSTRUCTION]
    return "\n".join(parts)
