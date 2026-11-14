from pathlib import Path

import pytest

from helmraiser.ledger import rundir as module
from helmraiser.ledger.rundir import RunDir, restamp_summary


@pytest.fixture
def rundir(tmp_path: Path) -> RunDir:
    return RunDir("r1", tmp_path).create()


def test_restamp_missing_summary(tmp_path: Path) -> None:
    before = set(tmp_path.iterdir())
    assert restamp_summary(RunDir("missing", tmp_path), "failed") is False
    assert set(tmp_path.iterdir()) == before
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("text", ["", "something else\n", "## No status\n"])
def test_restamp_invalid_summary(rundir: RunDir, text: str) -> None:
    rundir.summary_md.write_text(text)
    before = set(rundir.path.iterdir())
    assert restamp_summary(rundir, "failed") is False
    assert rundir.summary_md.read_bytes() == text.encode()
    assert set(rundir.path.iterdir()) == before


@pytest.mark.parametrize("title", ["Fix widget", "Fix the — dash title"])
def test_restamp_changes_only_heading_status(rundir: RunDir, title: str) -> None:
    body = "\n- worktree: `/w`\n- cost: 1\n"
    rundir.summary_md.write_text(f"## {title} — awaiting_approval\n{body}")
    assert restamp_summary(rundir, "landed") is True
    assert rundir.summary_md.read_text() == f"## {title} — landed\n{body}"


@pytest.mark.parametrize("newline", ["\r\n", ""])
def test_restamp_preserves_line_endings(rundir: RunDir, newline: str) -> None:
    rundir.summary_md.write_bytes(f"## Title — awaiting_approval{newline}".encode())
    assert restamp_summary(rundir, "failed") is True
    assert rundir.summary_md.read_bytes() == f"## Title — failed{newline}".encode()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("- branch: b\n", "- branch: b\n- pr: url\n"),
        ("- branch: b\n- base: main\n", "- branch: b\n- base: main\n- pr: url\n"),
        ("- base: main\n", "- base: main\n- pr: url\n"),
        ("\n- task: t\n", "- pr: url\n\n- task: t\n"),
        ("- branch: b", "- branch: b\n- pr: url\n"),
        ("- branch: b\r\n", "- branch: b\r\n- pr: url\r\n"),
    ],
)
def test_restamp_inserts_pr(rundir: RunDir, body: str, expected: str) -> None:
    rundir.summary_md.write_bytes(("## Title — old\n" + body).encode())
    assert restamp_summary(rundir, "landed", pr_url="url") is True
    assert rundir.summary_md.read_bytes() == ("## Title — landed\n" + expected).encode()


def test_restamp_replaces_existing_pr(rundir: RunDir) -> None:
    rundir.summary_md.write_bytes(b"## Title \xe2\x80\x94 old\r\n- pr: old\r\n- base: main\r\n")
    assert restamp_summary(rundir, "landed", pr_url="new") is True
    assert (
        rundir.summary_md.read_bytes()
        == "## Title — landed\r\n- pr: new\r\n- base: main\r\n".encode()
    )


def test_restamp_without_pr_preserves_existing_pr(rundir: RunDir) -> None:
    rundir.summary_md.write_text("## Title — old\n- pr: old\n")
    assert restamp_summary(rundir, "failed") is True
    assert rundir.summary_md.read_text() == "## Title — failed\n- pr: old\n"


def test_restamp_repeated_call_returns_true(rundir: RunDir) -> None:
    rundir.summary_md.write_text("## Title — old\n")
    for _ in range(2):
        assert restamp_summary(rundir, "landed", pr_url="url") is True
    assert rundir.summary_md.read_text() == "## Title — landed\n- pr: url\n"


def test_restamp_uses_atomic_replace(rundir: RunDir, monkeypatch: pytest.MonkeyPatch) -> None:
    original = "## Title — old\n"
    expected = "## Title — failed\n"
    rundir.summary_md.write_text(original)
    before = set(rundir.path.iterdir())
    real_replace = module.os.replace
    replaced = []

    def replace(source: str, destination: Path) -> None:
        assert rundir.summary_md.read_text() == original
        assert Path(source).parent == rundir.path
        assert Path(source).read_text() == expected
        assert destination == rundir.summary_md
        replaced.append(source)
        real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", replace)
    assert restamp_summary(rundir, "failed") is True
    assert len(replaced) == 1
    assert rundir.summary_md.read_text() == expected
    assert set(rundir.path.iterdir()) == before


def test_restamp_replace_error_preserves_original(
    rundir: RunDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = "## Title — old\n"
    rundir.summary_md.write_text(original)
    before = set(rundir.path.iterdir())

    def fail(*args: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        restamp_summary(rundir, "failed")
    assert rundir.summary_md.read_text() == original
    assert set(rundir.path.iterdir()) == before


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("## T — old\n\n- task: t\n", "## T — landed\n\n- note\n- task: t\n"),
        ("## T — old\r\n\r\n- task: t\r\n", "## T — landed\r\n\r\n- note\r\n- task: t\r\n"),
        ("## T — old\n", "## T — landed\n\n- note\n"),
        ("## T — old", "## T — landed\n\n- note\n"),
    ],
)
def test_restamp_inserts_note_as_first_bullet(rundir: RunDir, text: str, expected: str) -> None:
    rundir.summary_md.write_bytes(text.encode())
    assert restamp_summary(rundir, "landed", note="note") is True
    assert rundir.summary_md.read_bytes() == expected.encode()
