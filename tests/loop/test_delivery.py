"""Remote branch and pull-request delivery helpers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from helmraiser.loop.delivery import open_pull_request, pr_body, push_branch
from helmraiser.loop.evidence import EvidenceResult
from helmraiser.loop.review import Verdict
from helmraiser.loop.verify import VerifyResult
from helmraiser.task.loader import parse_task


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_push_branch_pushes_to_bare_remote_and_reports_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    git(repo, "init", "-b", "topic")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    (repo / "file").write_text("work\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "work")
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(repo, "remote", "add", "origin", str(remote))

    push_branch(repo, "topic")
    assert git(remote, "rev-parse", "refs/heads/topic") == git(repo, "rev-parse", "HEAD")

    with pytest.raises(RuntimeError, match="missing"):
        push_branch(repo, "topic", "missing")


def _stub_gh(tmp_path: Path, *, fail: bool = False) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    script = bin_dir / "gh"
    script.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$GH_CALLS"\n'
        + ("echo 'gh failed' >&2\nexit 1\n" if fail else "echo https://example.test/pr/1\n"),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return bin_dir, calls


def test_open_pull_request_passes_arguments_and_returns_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir, calls = _stub_gh(tmp_path)
    monkeypatch.setenv("GH_CALLS", str(calls))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    url = open_pull_request(
        tmp_path, base="main", head="feature/run", title="Fix it", body="Body\n"
    )

    assert url == "https://example.test/pr/1"
    argv = calls.read_text(encoding="utf-8")
    assert "pr create --base main --head feature/run --title Fix it --body-file" in argv


def test_open_pull_request_reports_gh_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir, calls = _stub_gh(tmp_path, fail=True)
    monkeypatch.setenv("GH_CALLS", str(calls))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    with pytest.raises(RuntimeError, match="gh failed"):
        open_pull_request(tmp_path, base="main", head="topic", title="Title", body="Body")


def test_pr_body_contains_governance_summary(tmp_path: Path) -> None:
    task = parse_task(
        f"---\nid: demo\ntitle: Demo task\nproject: {tmp_path}\n---\nDo the work.\n",
        tmp_path / "task.md",
        {},
    )
    body = pr_body(
        task,
        "run-1",
        Verdict("pass", findings=[{"severity": "minor", "note": "small"}]),
        [
            VerifyResult("tests", True, 0, 1.0, ""),
            VerifyResult("lint", False, 1, 1.0, "bad"),
        ],
        [EvidenceResult("screens", True, 0, 1.0, [], [], "", "looks right")],
        1.25,
    )
    assert "`demo`" in body and "`run-1`" in body and "Verdict: pass" in body
    assert "tests: pass" in body and "lint: FAIL" in body
    assert "screens: ok" in body and "$1.2500" in body
    assert body.rstrip().endswith("Do the work.")
    assert len(body.splitlines()) < 100


def test_pr_body_marks_forced_verdict_and_evidence_checks(tmp_path: Path) -> None:
    task = parse_task(
        f"---\nid: demo\ntitle: Demo task\nproject: {tmp_path}\n---\nDo the work.\n",
        tmp_path / "task.md",
        {},
    )
    verdict = Verdict(
        "concerns",
        findings=[],
        evidence_checks=[
            {"name": "greet", "meets_criteria": False, "note": "output lacks the greeting"},
            {"name": "fence", "meets_criteria": None, "note": ""},
            {"name": "fine", "meets_criteria": True, "note": ""},
        ],
        forced="evidence step greet did not pass and the reviewer marked it read",
    )

    body = pr_body(
        task,
        "run-1",
        verdict,
        [],
        [
            EvidenceResult("greet", True, 0, 1.0, [], [], "", "looks right"),
            EvidenceResult("fence", True, 0, 1.0, [], [], "", ""),
            EvidenceResult("fine", True, 0, 1.0, [], [], "", ""),
            EvidenceResult("other", True, 0, 1.0, [], [], "", ""),
        ],
        0.5,
    )

    assert (
        "- Verdict: concerns (forced: evidence step greet did not pass and "
        "the reviewer marked it read)" in body
    )
    assert "- greet: ok — reviewer: not met: output lacks the greeting" in body
    assert "- fence: ok — reviewer: not read" in body
    assert "- fine: ok\n" in body
    assert "- other: ok\n" in body


def test_pr_body_demotes_task_headings_outside_fenced_blocks(tmp_path: Path) -> None:
    body_text = (
        "## Goal\n\nDo the work.\n\n```\n# inside a fence, not a heading\n## Also inside\n```\n"
        "### Details\n\n~~~\n## tilde fenced\n~~~\n"
    )
    task = parse_task(
        f"---\nid: demo\ntitle: Demo task\nproject: {tmp_path}\n---\n{body_text}",
        tmp_path / "task.md",
        {},
    )

    body = pr_body(task, "run-1", Verdict("pass"), [], [], 0.5)

    tail = body.split("## Task", 1)[1]
    assert "### Goal\n\nDo the work." in tail
    assert "~~~\n## tilde fenced\n~~~" in tail
    assert "#### Also inside" not in tail
    assert "## Also inside" in tail
    assert "### inside a fence, not a heading" not in tail
    assert "#### Details" in tail
    assert len(body.splitlines()) <= 99


def test_pr_body_is_capped_as_a_whole(make_task) -> None:
    verdict = {
        "verdict": "concerns",
        "findings": [
            {"severity": "minor", "file": "a.py", "line": i, "note": "x"} for i in range(200)
        ],
    }

    body = pr_body(make_task(), "run-1", verdict, [], [], 0.5)

    assert len(body.splitlines()) <= 99
    assert body.rstrip().endswith("…")
