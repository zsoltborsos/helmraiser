"""Safe loading and rendering of owner-maintained project notes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROJECT_NOTES_RELATIVE = Path(".helmraiser/notes.md")
PROJECT_NOTES_MAX_BYTES = 8 * 1024
PROJECT_NOTES_TRUNCATION_MARKER = "[Project notes truncated by Helmraiser.]"


@dataclass(frozen=True)
class ProjectNotes:
    text: str
    size_bytes: int
    truncated: bool


def inside_without_symlinks(repo_real: Path, path: Path, relative: Path) -> bool:
    """Return whether path is exactly the expected resolved path below the repository."""
    try:
        return path.resolve(strict=True) == repo_real / relative
    except (OSError, RuntimeError):
        return False


def read_project_notes(repo: Path) -> ProjectNotes | None:
    """Read bounded project notes, refusing missing, non-file, and symlinked paths."""
    try:
        repo_real = repo.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    path = repo / PROJECT_NOTES_RELATIVE
    if not path.is_file() or not inside_without_symlinks(repo_real, path, PROJECT_NOTES_RELATIVE):
        return None
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            raw = handle.read(PROJECT_NOTES_MAX_BYTES + 1)
    except OSError:
        return None
    truncated = size > PROJECT_NOTES_MAX_BYTES
    if truncated:
        suffix = ("\n\n" + PROJECT_NOTES_TRUNCATION_MARKER).encode()
        raw = raw[: PROJECT_NOTES_MAX_BYTES - len(suffix)]
        text = raw.decode("utf-8", errors="ignore") + suffix.decode()
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return ProjectNotes(text=text, size_bytes=size, truncated=truncated)


def project_notes_section(text: str) -> str:
    """Render project notes as a shared Markdown section."""
    return f"## Project notes (Helmraiser)\n\n{text.strip()}"


def append_project_notes(body: str, text: str | None) -> str:
    """Append notes unless their exact stripped text is already carried by the body."""
    notes = text.strip() if text else ""
    if not notes or notes in body:
        return body
    return body.rstrip() + "\n\n" + project_notes_section(notes) + "\n"


def project_notes_for_body(repo: Path, body: str) -> str | None:
    """Return safe project notes unless the task body already contains them."""
    notes = read_project_notes(repo)
    if notes is None or not notes.text.strip() or notes.text.strip() in body:
        return None
    return notes.text
