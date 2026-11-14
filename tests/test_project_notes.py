from pathlib import Path

from helmraiser.project_notes import (
    PROJECT_NOTES_MAX_BYTES,
    PROJECT_NOTES_TRUNCATION_MARKER,
    append_project_notes,
    project_notes_for_body,
    read_project_notes,
)


def write_project_notes(repo: Path, text: str) -> Path:
    path = repo / ".helmraiser" / "notes.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_project_notes_are_read_with_exact_size(tmp_path: Path) -> None:
    text = "Use café tests.\n"
    write_project_notes(tmp_path, text)

    notes = read_project_notes(tmp_path)

    assert notes is not None
    assert notes.text == text
    assert notes.size_bytes == len(text.encode())
    assert notes.truncated is False


def test_project_notes_are_utf8_safe_and_capped(tmp_path: Path) -> None:
    text = "é" * PROJECT_NOTES_MAX_BYTES
    write_project_notes(tmp_path, text)

    notes = read_project_notes(tmp_path)

    assert notes is not None
    assert notes.truncated is True
    assert notes.size_bytes == len(text.encode())
    assert notes.text.endswith(PROJECT_NOTES_TRUNCATION_MARKER)
    assert len(notes.text.encode()) <= PROJECT_NOTES_MAX_BYTES


def test_project_notes_symlinked_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target.md"
    target.write_text("inside target", encoding="utf-8")
    notes = tmp_path / ".helmraiser" / "notes.md"
    notes.parent.mkdir()
    notes.symlink_to(target)

    assert read_project_notes(tmp_path) is None


def test_project_notes_symlinked_directory_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    (target / "notes.md").write_text("inside target", encoding="utf-8")
    (tmp_path / ".helmraiser").symlink_to(target, target_is_directory=True)

    assert read_project_notes(tmp_path) is None


def test_project_notes_are_not_duplicated_in_body(tmp_path: Path) -> None:
    text = "Use the sandbox-safe command."
    write_project_notes(tmp_path, text)
    body = f"## Goal\n\nDo it.\n\n## Existing constraints\n\n{text}\n"

    assert append_project_notes(body, text) == body
    assert project_notes_for_body(tmp_path, body) is None

    appended = append_project_notes("## Goal\n\nDo it.\n", text)
    assert appended.count(text) == 1
    assert appended.count("## Project notes (Helmraiser)") == 1


def test_missing_project_notes_change_nothing(tmp_path: Path) -> None:
    from helmraiser.project_notes import append_project_notes, project_notes_for_body

    assert project_notes_for_body(tmp_path, "## Goal\n\nBody") is None
    assert append_project_notes("## Goal\n\nBody\n", None) == "## Goal\n\nBody\n"
