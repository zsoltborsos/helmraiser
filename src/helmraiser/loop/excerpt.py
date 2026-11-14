"""The output excerpt of a failing verify / evidence step.

A plain tail misses failures reported far above the end of a noisy run (a TAP suite
interleaved with JSON logs, say). ``failure_excerpt`` keeps, in priority order and
within a line and character cap: the lines that look like test failures anywhere in the
output with a little context (``FAILURE_PATTERNS``), the runner's own totals
(``TOTAL_PATTERNS``), then the tail. Kept lines stay in output order; each omitted span
becomes one ``… N lines omitted …`` line. Without a failure match it is the tail alone.
The full output goes to a log file in the run directory (``FailureReport.log_path``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_EXCERPT_LINES = 300
DEFAULT_EXCERPT_CHARS = 30_000
# Failure excerpts may use this share of the caps; the rest is left for totals and tail.
FAILURE_SHARE = 0.75


@dataclass(frozen=True)
class FailurePattern:
    """A line matching ``start`` is kept with ``before`` lines above it and up to
    ``after`` lines below it; the context below ends at (and includes) the first
    later line matching ``end``."""

    name: str
    start: re.Pattern[str]
    before: int = 0
    after: int = 0
    end: re.Pattern[str] | None = None


FAILURE_PATTERNS: tuple[FailurePattern, ...] = (
    # TAP: `not ok 3 - name` and its indented YAML block (error, expected, actual, …).
    FailurePattern(
        "tap",
        re.compile(r"^\s*not ok \d+"),
        after=30,
        end=re.compile(r"^\s*(?:\.\.\.\s*$|(?:not )?ok \d+)"),
    ),
    # node:test spec reporter: `✖ name (1ms)` with its error, and the "failing tests:" list.
    FailurePattern("node-spec", re.compile(r"^\s*✖ "), after=20, end=re.compile(r"^\s*[✔✖▶ℹ] ")),
    FailurePattern("node-spec-location", re.compile(r"^\s*test at \S+:\d+:\d+")),
    # vitest / jest.
    FailurePattern("vitest-fail", re.compile(r"^\s*FAIL\s"), after=15, end=re.compile(r"^\s*⎯{3}")),
    FailurePattern("jest-failure", re.compile(r"^\s*● "), after=15),
    FailurePattern("test-cross", re.compile(r"^\s*[×✗✕] "), after=2),
    FailurePattern(
        "error-header", re.compile(r"^\s*\w*(?:Error|Exception)\b[^:]{0,40}: "), after=6
    ),
    # pytest: a failure section from its `___ test ___` header to the location line,
    # `E   ` lines (with the `>` line above), locations, the short summary.
    FailurePattern(
        "pytest-header",
        re.compile(r"^_{3,} .+ _{3,}$"),
        after=30,
        end=re.compile(r"^[\w./-]+\.py:\d+: \w"),
    ),
    FailurePattern("pytest-e", re.compile(r"^E\s"), before=1),
    FailurePattern("pytest-location", re.compile(r"^[\w./-]+\.py:\d+: \w")),
    FailurePattern("pytest-summary", re.compile(r"^=+ short test summary info =+$")),
    FailurePattern("pytest-failed", re.compile(r"^(?:FAILED|ERROR) ")),
    # Python tracebacks, up to the unindented exception line.
    FailurePattern(
        "traceback",
        re.compile(r"^\s*Traceback \(most recent call last\):"),
        after=40,
        end=re.compile(r"^\S"),
    ),
)

TOTAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # TAP (`# tests 66`) and the node:test spec reporter (`ℹ fail 3`).
    re.compile(r"^\s*[#ℹ]\s*(?:tests|suites|pass|fail|cancelled|skipped|todo|duration_ms)\s+\d"),
    # pytest's final `==== 3 failed, 63 passed in 1.2s ====`.
    re.compile(r"^=+ .*\b(?:passed|failed|errors?)\b.* in [\d.]+s.*=+$"),
    # vitest `Tests  3 failed | 63 passed (66)`, jest `Tests:       3 failed, 63 total`.
    re.compile(r"^\s*(?:Test Files|Test Suites|Tests):?\s+\d"),
)


@dataclass(frozen=True)
class FailureReport:
    """How a failing step's output is reported: an excerpt within ``max_lines`` and
    ``max_chars``, the full output under ``log_dir`` as ``<log_prefix>-<n>-<name>.log``
    (no file without a ``log_dir``), and hints for known environment signatures in it
    (``loop/hints.py``), where ``provided_ports`` are the run's service ports."""

    max_lines: int = DEFAULT_EXCERPT_LINES
    max_chars: int = DEFAULT_EXCERPT_CHARS
    log_dir: Path | None = None
    log_prefix: str = "step"
    provided_ports: tuple[int, ...] = ()

    def log_path(self, index: int, name: str) -> Path | None:
        if self.log_dir is None:
            return None
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "step"
        return self.log_dir / f"{self.log_prefix}-{index}-{slug}.log"

    def write_log(self, index: int, name: str, output: str) -> str | None:
        path = self.log_path(index, name)
        if path is None:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
        return str(path)


def omitted_marker(count: int) -> str:
    return f"… {count} line{'s' if count != 1 else ''} omitted …"


def failure_blocks(lines: list[str]) -> list[range]:
    """The line ranges the failure patterns select, in output order."""
    blocks: list[range] = []
    for i, line in enumerate(lines):
        for pattern in FAILURE_PATTERNS:
            if not pattern.start.search(line):
                continue
            stop = min(len(lines), i + pattern.after + 1)
            if pattern.end is not None:
                for j in range(i + 1, stop):
                    if pattern.end.search(lines[j]):
                        stop = j + 1
                        break
            blocks.append(range(max(0, i - pattern.before), stop))
            break
    return blocks


class _Budget:
    def __init__(self, lines: int, chars: int) -> None:
        self.lines = lines
        self.chars = chars

    def fits(self, line: str) -> bool:
        return self.lines > 0 and len(line) + 1 <= self.chars

    def charge(self, line: str) -> None:
        self.lines -= 1
        self.chars -= len(line) + 1


def failure_excerpt(text: str, tail: int, max_lines: int, max_chars: int) -> str:
    """Failure excerpts, totals and the last ``tail`` lines of ``text``, capped."""
    lines = text.rstrip().splitlines()
    keep: set[int] = set()
    budget = _Budget(max_lines, max_chars)

    def add(index: int, *budgets: _Budget) -> bool:
        if index in keep:
            return True
        if not all(b.fits(lines[index]) for b in budgets):
            return False
        for b in budgets:
            b.charge(lines[index])
        keep.add(index)
        return True

    blocks = failure_blocks(lines)
    if blocks:
        share = _Budget(int(max_lines * FAILURE_SHARE), int(max_chars * FAILURE_SHARE))
        full = False
        for block in blocks:
            for index in block:
                if not add(index, share, budget):
                    full = True
                    break
            if full:
                break
        for index, line in enumerate(lines):
            if any(pattern.search(line) for pattern in TOTAL_PATTERNS) and not add(index, budget):
                break
    for index in range(len(lines) - 1, max(0, len(lines) - tail) - 1, -1):
        if not add(index, budget):
            break

    out: list[str] = []
    previous = -1
    for index in sorted(keep):
        if index > previous + 1:
            out.append(omitted_marker(index - previous - 1))
        out.append(lines[index])
        previous = index
    if previous < len(lines) - 1:
        out.append(omitted_marker(len(lines) - 1 - previous))
    return "\n".join(out)
