"""Run directory layout under ``$HELMRAISER_DATA_DIR/runs/<run-id>/`` (spec §2).

task.md        snapshot given to the agent
plan.md        approved implementation plan
events.jsonl   JSONL mirror of every ledger event (ledger/event_logger.EventLogger)
sessions/      pi --session-dir for all stages of this run
evidence/      $HELMRAISER_EVIDENCE_DIR for evidence commands
logs/          full output of failed verify / evidence steps (loop/excerpt.py)
review.md      reviewer verdict, verbatim
diff.patch     git diff base_commit..head_commit at review time
summary.md     morning-report fragment
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from helmraiser.ledger.db import default_data_dir
from helmraiser.ledger.event_logger import EventLogger


class RunDir:
    """Paths inside one run's directory; ``create()`` materialises it."""

    def __init__(self, run_id: str, data_dir: Path | None = None, *, path: Path | None = None):
        self.run_id = run_id
        self.path = path if path is not None else (data_dir or default_data_dir()) / "runs" / run_id

    @property
    def task_md(self) -> Path:
        return self.path / "task.md"

    @property
    def events_jsonl(self) -> Path:
        return self.path / "events.jsonl"

    @property
    def sessions(self) -> Path:
        return self.path / "sessions"

    @property
    def evidence(self) -> Path:
        return self.path / "evidence"

    @property
    def logs(self) -> Path:
        return self.path / "logs"

    @property
    def directives_md(self) -> Path:
        return self.path / "directives.md"

    @property
    def plan_directives_md(self) -> Path:
        return self.path / "directives-plan.md"

    @property
    def plan_md(self) -> Path:
        return self.path / "plan.md"

    @property
    def review_directives_md(self) -> Path:
        return self.path / "directives-review.md"

    @property
    def review_md(self) -> Path:
        return self.path / "review.md"

    def review_round_md(self, number: int) -> Path:
        return self.path / f"review-{number}.md"

    @property
    def diff_patch(self) -> Path:
        return self.path / "diff.patch"

    @property
    def summary_md(self) -> Path:
        return self.path / "summary.md"

    def create(self) -> RunDir:
        self.sessions.mkdir(parents=True, exist_ok=True)
        self.evidence.mkdir(parents=True, exist_ok=True)
        return self

    def event_logger(self) -> EventLogger:
        return EventLogger(self.path)


def restamp_summary(
    rundir: RunDir, status: str, *, pr_url: str | None = None, note: str | None = None
) -> bool:
    """Atomically update a summary's status and optional PR without regenerating it.
    ``note`` becomes the first bullet."""
    try:
        with rundir.summary_md.open(encoding="utf-8", newline="") as fh:
            lines = fh.read().splitlines(keepends=True)
    except FileNotFoundError:
        return False
    if not lines or not lines[0].startswith("## ") or " — " not in lines[0]:
        return False

    def ending(line: str) -> str:
        return line[len(line.rstrip("\r\n")) :]

    heading_end = ending(lines[0])
    lines[0] = lines[0].rsplit(" — ", 1)[0] + " — " + status + heading_end
    if note is not None:
        newline = heading_end or "\n"
        if len(lines) == 1:  # a bare heading: add the blank line before the bullets
            lines[0] += "" if heading_end else newline
            lines.append(newline)
        lines.insert(2, f"- {note}" + newline)
    if pr_url is not None:
        for index, line in enumerate(lines):
            if line.startswith("- pr:"):
                lines[index] = f"- pr: {pr_url}" + ending(line)
                break
        else:
            anchor = 0
            for prefix in ("- base:", "- branch:"):
                found = next((i for i, line in enumerate(lines) if line.startswith(prefix)), None)
                if found is not None:
                    anchor = found
                    break
            newline = ending(lines[anchor]) or next(
                (ending(line) for line in lines if ending(line)), "\n"
            )
            if not ending(lines[anchor]):
                lines[anchor] += newline
            lines.insert(anchor + 1, f"- pr: {pr_url}" + newline)

    fd, tmp_name = tempfile.mkstemp(prefix=".summary-", suffix=".md", dir=rundir.path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write("".join(lines))
        os.replace(tmp_name, rundir.summary_md)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return True
