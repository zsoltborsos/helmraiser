"""Snapshots of committed Helmraiser code for the background worker.

The worker's run children start ``python -m helmraiser`` afresh. From an editable
install they would import whatever the checkout's working tree holds at that moment,
so the worker (``work --watch``) instead runs ``<data_dir>/code/<commit>``: the
``src/helmraiser`` of a commit, exported with ``git archive``. It re-execs itself with
``PYTHONPATH=<snapshot>/src`` in front and two markers, ``HELMRAISER_CODE_SNAPSHOT``
(the commit) and ``HELMRAISER_SOURCE_REPO`` (the checkout), and its children inherit
them. The editable install is a plain ``.pth`` path entry, which ``site`` appends
after the ``PYTHONPATH`` entries, so the snapshot wins; ``import_problem`` checks that
the import really resolves to the snapshot before every exec.

Run steps (setup, verify, evidence) must test the worktree's code, so
``without_snapshot`` removes the snapshot entry and the markers from their
environment.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Collection, Mapping
from pathlib import Path

SNAPSHOT_ENV = "HELMRAISER_CODE_SNAPSHOT"
SOURCE_REPO_ENV = "HELMRAISER_SOURCE_REPO"
PACKAGE_PATH = "src/helmraiser"
CODE_DIR = "code"
# Snapshots kept besides the one in use, newest first, for a quick look back.
KEEP_OTHERS = 2


class SnapshotError(RuntimeError):
    """The commit's ``src/helmraiser`` could not be exported."""


def package_dir() -> Path:
    """The directory of the loaded ``helmraiser`` package."""
    import helmraiser

    return Path(helmraiser.__file__).resolve().parent


def source_repo(environ: Mapping[str, str] | None = None) -> Path | None:
    """The Helmraiser checkout: ``HELMRAISER_SOURCE_REPO`` when set (a worker on a
    snapshot), else the git checkout whose ``src/helmraiser`` is the loaded package;
    ``None`` for a package installed outside a checkout."""
    env = os.environ if environ is None else environ
    if env.get(SOURCE_REPO_ENV):
        return Path(env[SOURCE_REPO_ENV])
    here = package_dir()
    try:
        result = subprocess.run(
            ["git", "-C", str(here), "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    top = Path(result.stdout.strip())
    return top if (top / PACKAGE_PATH).resolve() == here else None


def snapshot_path(base: Path, commit: str) -> Path:
    return base / CODE_DIR / commit


def export(repo: Path, commit: str, base: Path) -> Path:
    """``<base>/code/<commit>`` holding the commit's ``src/helmraiser``: reused when it
    exists, else extracted into a temporary directory and renamed into place, so a
    snapshot directory is always complete."""
    final = snapshot_path(base, commit)
    if final.is_dir():
        return final
    final.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{commit[:12]}-", dir=final.parent))
    try:
        try:
            archive = subprocess.run(
                ["git", "-C", str(repo), "archive", "--format=tar", commit, PACKAGE_PATH],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SnapshotError(f"git archive failed: {exc}") from exc
        if archive.returncode != 0:
            lines = archive.stderr.decode(errors="replace").strip().splitlines()
            raise SnapshotError(lines[-1] if lines else f"git archive exit {archive.returncode}")
        try:
            with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
                tar.extractall(temp, filter="data")
        except (tarfile.TarError, OSError) as exc:
            raise SnapshotError(f"cannot extract the archive: {exc}") from exc
        if not (temp / PACKAGE_PATH / "__init__.py").is_file():
            raise SnapshotError(f"{commit[:8]} has no {PACKAGE_PATH}/__init__.py")
        try:
            temp.rename(final)
        except OSError:
            if not final.is_dir():  # not a concurrent export of the same commit
                raise
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    return final


def without_snapshot(environ: Mapping[str, str]) -> dict[str, str]:
    """``environ`` without the snapshot markers and the snapshot's ``PYTHONPATH``
    entry: the environment the worker was started with."""
    env = dict(environ)
    commit = env.pop(SNAPSHOT_ENV, None)
    env.pop(SOURCE_REPO_ENV, None)
    if commit and "PYTHONPATH" in env:
        tail = os.sep.join(("", CODE_DIR, commit, "src"))
        kept = [p for p in env["PYTHONPATH"].split(os.pathsep) if not p.endswith(tail)]
        if kept:
            env["PYTHONPATH"] = os.pathsep.join(kept)
        else:
            del env["PYTHONPATH"]
    return env


def snapshot_env(
    snapshot: Path, commit: str, repo: Path, environ: Mapping[str, str]
) -> dict[str, str]:
    """``environ`` running ``snapshot``: its ``src`` first on ``PYTHONPATH`` (replacing
    a previous snapshot's entry) and the markers set."""
    env = without_snapshot(environ)
    entries = [str(snapshot / "src"), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    env["PYTHONPATH"] = os.pathsep.join(entries)
    env[SNAPSHOT_ENV] = commit
    env[SOURCE_REPO_ENV] = str(repo)
    return env


def loaded_from(snapshot: Path) -> bool:
    """Whether this process imported ``helmraiser`` from ``snapshot``."""
    return package_dir().is_relative_to(snapshot.resolve())


def import_problem(env: Mapping[str, str], snapshot: Path) -> str | None:
    """Why a process with ``env`` would not run ``snapshot``: its CLI does not import,
    or ``helmraiser`` resolves elsewhere. ``None`` when it would."""
    code = "import helmraiser.cli.app, helmraiser; print(helmraiser.__file__)"
    try:
        check = subprocess.run(
            [sys.executable, "-c", code],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=dict(env),
            cwd=snapshot,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not check the new code: {exc}"
    if check.returncode != 0:
        lines = check.stderr.strip().splitlines()
        return "the new code does not import: " + (
            lines[-1] if lines else f"exit code {check.returncode}"
        )
    loaded = Path(check.stdout.strip()).resolve()
    if not loaded.is_relative_to(snapshot.resolve()):
        return f"the new code does not load from the snapshot: helmraiser comes from {loaded}"
    return None


def exec_into(commit: str, repo: Path, base: Path, announce: Callable[[], None]) -> str:
    """Replace this process with the same command (``sys.orig_argv``, so the pid is
    kept) running the snapshot of ``commit``. Returns only when the process was not
    replaced, with the reason: the export failed, or the snapshot does not import."""
    try:
        snapshot = export(repo, commit, base)
    except (SnapshotError, OSError) as exc:
        return f"could not export {commit[:8]}: {exc}"
    env = snapshot_env(snapshot, commit, repo, os.environ)
    problem = import_problem(env, snapshot)
    if problem is not None:
        return problem
    announce()
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execve(sys.executable, sys.orig_argv, env)
    except OSError as exc:
        return f"exec failed: {exc}"
    return "exec returned"  # pragma: no cover - execve does not return


def prune(base: Path, current: str, *, protected_commits: Collection[str] = ()) -> list[Path]:
    """Keep current, child-protected snapshots and the ``KEEP_OTHERS`` newest
    unprotected others (by mtime). Delete older snapshots and interrupted exports.
    Returns what was deleted."""
    code = base / CODE_DIR
    if not code.is_dir():
        return []
    others = [p for p in code.iterdir() if p.is_dir() and p.name != current]
    temps = [p for p in others if p.name.startswith(".")]
    snapshots = sorted(
        (p for p in others if not p.name.startswith(".") and p.name not in protected_commits),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = temps + snapshots[KEEP_OTHERS:]
    for path in removed:
        shutil.rmtree(path, ignore_errors=True)
    return removed
