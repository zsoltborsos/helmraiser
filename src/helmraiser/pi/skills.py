"""Which skills a pi session loads: per-stage worker skill folders and project skills.

A stage whose folder ``~/.local/share/helmraiser/skills/<stage>/`` exists runs with
``--no-skills``, one ``--skill`` per skill directory in the folder, then the project's
own skills. Without the folder pi discovers skills itself. Arms may instead select
project-only (``none``), pi discovery (``discover``), or a named worker skill set.
Missing named stages are project-only, never discovery. Process isolation opens the
passed skill directories for reading (``skill_read_paths``).
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_SKILL_ROOTS: tuple[str, ...] = (".pi/skills", ".agents/skills")

# One folder per stage: intake, scout, plan, implement (the fallback too) and review.
SKILLS_ROOT = Path.home() / ".local" / "share" / "helmraiser" / "skills"
SKILL_SETS_ROOT = Path.home() / ".local" / "share" / "helmraiser" / "skill-sets"


def project_skill_dirs(start: Path) -> list[Path]:
    """Skill directories (holding ``SKILL.md``) in ``.pi/skills`` and ``.agents/skills``
    from ``start`` up to its repository root, nearest first."""
    found: list[Path] = []
    current = start
    while True:
        for rel in PROJECT_SKILL_ROOTS:
            root = current / rel
            if not root.is_dir():
                continue
            seen: set[str] = set()
            for folder, dirs, files in os.walk(root, followlinks=True):
                real = os.path.realpath(folder)
                if real in seen:
                    dirs[:] = []
                    continue
                seen.add(real)
                if "SKILL.md" in files:
                    found.append(Path(folder))
                    dirs[:] = []
                dirs.sort()
        if (current / ".git").exists() or current.parent == current:
            return found
        current = current.parent


def skill_read_paths(dirs: list[Path]) -> tuple[Path, ...]:
    """What reading ``dirs`` needs: each directory and every symlink target inside it."""
    paths: list[Path] = []
    for directory in dirs:
        paths.append(directory.resolve())
        for folder, subdirs, files in os.walk(directory):
            for name in [*subdirs, *files]:
                entry = Path(folder) / name
                if entry.is_symlink() and entry.exists():
                    paths.append(entry.resolve())
    return tuple(dict.fromkeys(paths))


def skill_args(dirs: list[Path]) -> tuple[str, ...]:
    """``--no-skills`` and one ``--skill`` per directory, in order."""
    args: list[str] = ["--no-skills"]
    for directory in dirs:
        args += ["--skill", str(directory)]
    return tuple(args)


def argv_skill_dirs(argv: list[str]) -> list[Path]:
    """The ``--skill`` directories of a pi argv (options before ``--`` only)."""
    dirs: list[Path] = []
    for flag, value in zip(argv, argv[1:], strict=False):
        if flag == "--":
            break
        if flag == "--skill":
            dirs.append(Path(value))
    return dirs


def stage_skill_args(
    stage: str, cwd: Path, *, checkout: Path | None = None, skills: str = "default"
) -> tuple[str, ...]:
    """Worker skills (sorted children holding ``SKILL.md``), then project skills.

    Only ``default`` falls back to discovery for a missing stage; ``discover`` always
    delegates to pi, and ``none`` selects project skills only. With ``checkout`` (before
    worktree creation), project paths are remapped to the same place under ``cwd``.
    """
    if skills == "discover":
        return ()
    folder = SKILLS_ROOT / stage if skills == "default" else SKILL_SETS_ROOT / skills / stage
    if skills == "default" and not folder.is_dir():
        return ()
    own = (
        sorted(child for child in folder.iterdir() if (child / "SKILL.md").is_file())
        if skills != "none" and folder.is_dir()
        else []
    )
    if checkout is None:
        project = project_skill_dirs(cwd)
    else:
        project = [
            cwd / path.relative_to(checkout)
            for path in project_skill_dirs(checkout)
            if path.is_relative_to(checkout)
        ]
    return skill_args([*own, *project])
