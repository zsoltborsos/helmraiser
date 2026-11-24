"""Run-specific git worktree creation and safe local finalization."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from helmraiser.code_snapshot import without_snapshot


class WorktreeManager:
    """Manage isolated worktrees without touching another run's artifacts."""

    def __init__(
        self,
        codebase_dir: Path,
        worktree_dir: Path | None = None,
        command_timeout: float | None = None,
    ):
        self.command_timeout = command_timeout
        self.codebase_dir = codebase_dir.resolve()
        self._worktree_dir = worktree_dir or codebase_dir / ".helmraiser" / "worktrees"

    @property
    def worktree_dir(self) -> Path:
        """The configured worktree directory, without creating it."""
        return self._worktree_dir

    def _path(self, topic_id: str, run_id: str) -> Path:
        return self._worktree_dir / f"worktree-{topic_id}-{run_id}"

    @staticmethod
    def _branch(topic_id: str, run_id: str) -> str:
        return f"feature/{topic_id}/{run_id}"

    def _metadata_path(self, run_id: str) -> Path:
        return self._worktree_dir / f"{run_id}.json"

    def _git(
        self,
        *args: str,
        check: bool = True,
        cwd: Path | None = None,
        timeout: float | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        # Without the worker's code snapshot: the project's git hooks run its own code.
        effective_timeout = timeout if timeout is not None else self.command_timeout
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd or self.codebase_dir,
                capture_output=True,
                text=True,
                check=False,
                shell=False,
                env={
                    **without_snapshot(os.environ),
                    **({"GIT_TERMINAL_PROMPT": "0"} if effective_timeout is not None else {}),
                    **(env_overrides or {}),
                },
                timeout=effective_timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if effective_timeout is None:
                raise
            raise RuntimeError(f"git {' '.join(args)} failed: {exc}") from exc
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
        return result

    def disable_auto_maintenance(self) -> dict[str, Any]:
        """Keep automatic ref packing from removing live sessions' bind mounts."""
        timeout = self.command_timeout if self.command_timeout is not None else 60
        result = self._git(
            "config", "--local", "--get", "maintenance.auto", check=False, timeout=timeout
        )
        if result.returncode not in (0, 1):
            raise RuntimeError(
                f"git config --local --get maintenance.auto failed: {result.stderr.strip()}"
            )
        previous = result.stdout.strip() if result.returncode == 0 else None
        if previous == "false":
            return {"changed": False, "previous": previous}
        self._git("config", "--local", "maintenance.auto", "false", timeout=timeout)
        return {"changed": True, "previous": previous}

    def run_maintenance(self) -> None:
        """Run Git's bounded automatic maintenance only when the project is idle."""
        timeout = self.command_timeout if self.command_timeout is not None else 60
        self._git("maintenance", "run", "--auto", timeout=timeout)

    def path_for(self, topic_id: str, run_id: str) -> Path:
        """Where ``create`` puts this run's worktree (resolved, not created)."""
        return self._path(topic_id, run_id).resolve()

    def metadata(self, run_id: str) -> dict[str, Any]:
        """Persisted creation record: branch_name, base_branch, base_commit, worktree_path."""
        return self._read_metadata(run_id)

    def adopt_base(self, run_id: str, base_commit: str) -> None:
        """Update the persisted base after the implementer has rebased the run."""
        metadata = self._read_metadata(run_id)
        metadata["base_commit"] = base_commit
        self._metadata_path(run_id).write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    def _read_metadata(self, run_id: str) -> dict[str, Any]:
        path = self._metadata_path(run_id)
        if not path.exists():
            raise RuntimeError(f"no recoverable worktree state for run {run_id}")
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))

    @contextmanager
    def _finalization_lock(self) -> Iterator[None]:
        """Serialize base-checkout updates across Helmraiser processes."""
        lock_path = self.codebase_dir / ".helmraiser" / "finalize.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _base_checkout(self, base_ref: str) -> tuple[str | None, bool]:
        """Refuse another worktree's branch or tracked changes in our checkout."""
        path: Path | None = None
        checked_out = False
        for field in self._git("worktree", "list", "--porcelain", "-z").stdout.split("\0"):
            if field.startswith("worktree "):
                path = Path(field[9:]).resolve()
            elif field == f"branch {base_ref}":
                if path != self.codebase_dir:
                    return "other_worktree", False
                checked_out = True
        if (
            checked_out
            and self._git("status", "--porcelain", "--untracked-files=no").stdout.strip()
        ):
            return "dirty_checkout", True
        return None, checked_out

    def fast_forward_base(self, base: str, sha: str) -> dict[str, Any]:
        """Best-effort ff-only update of a clean primary base checkout; never fetch or push.

        Unchecked-out branches and branches in other worktrees are left alone.
        Untracked files do not count as dirty, but collisions can make the merge fail.
        """
        previous_timeout = self.command_timeout
        self.command_timeout = previous_timeout if previous_timeout is not None else 60
        try:
            with self._finalization_lock():
                ref = f"refs/heads/{base}"
                reason, checked_out = self._base_checkout(ref)
                if reason or not checked_out:
                    return {"status": "skipped", "reason": reason or "not_checked_out"}
                tip = self._git("rev-parse", "--verify", ref).stdout.strip()
                contains = self._git("merge-base", "--is-ancestor", sha, tip, check=False)
                if contains.returncode == 0:
                    return {"status": "skipped", "reason": "up_to_date"}
                if contains.returncode != 1:
                    raise RuntimeError(contains.stderr.strip())
                ancestor = self._git("merge-base", "--is-ancestor", tip, sha, check=False)
                if ancestor.returncode == 1:
                    return {"status": "skipped", "reason": "not_fast_forward"}
                if ancestor.returncode != 0:
                    raise RuntimeError(ancestor.stderr.strip())
                self._git("merge", "--ff-only", "--no-squash", "--no-autostash", "--no-edit", sha)
                if self._git("rev-parse", "--verify", ref).stdout.strip() != sha:
                    raise RuntimeError("base branch did not advance to the landing head")
                return {"status": "updated", "reason": "fast_forward"}
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            return {"status": "failed", "reason": "git_failed", "detail": str(exc)}
        finally:
            self.command_timeout = previous_timeout

    def sync_base(self, base: str | None = None) -> dict[str, Any]:
        """Best-effort upstream fast-forward, without changing unsafe checkouts."""
        payload: dict[str, Any] = {
            "status": "skipped",
            "reason": "no_base",
            "base_branch": base,
            "remote": None,
            "upstream": None,
            "from_commit": None,
            "to_commit": None,
            "upstream_commit": None,
        }
        temporary_ref = f"refs/helmraiser/base-sync/{uuid4()}"
        base_ref: str | None = None
        failure_reason = "git_failed"
        try:
            with self._finalization_lock():
                try:
                    if base is None:
                        head = self._git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
                        if head.returncode != 0:
                            payload["reason"] = "detached_head"
                            return payload
                        base = head.stdout.strip()
                    payload["base_branch"] = base
                    base_ref = f"refs/heads/{base}"
                    tip = self._git("rev-parse", "--verify", base_ref, check=False)
                    if tip.returncode != 0:
                        return payload
                    original = tip.stdout.strip()
                    payload["from_commit"] = payload["to_commit"] = original
                    remote = self._git(
                        "config", "--get", f"branch.{base}.remote", check=False
                    ).stdout.strip()
                    sources = self._git(
                        "config", "--get-all", f"branch.{base}.merge", check=False
                    ).stdout.splitlines()
                    payload["remote"] = remote or None
                    payload["upstream"] = sources[0] if len(sources) == 1 else None
                    if not remote or not sources:
                        payload["reason"] = "no_upstream"
                        return payload
                    if len(sources) != 1:
                        payload["reason"] = "unsupported_upstream"
                        return payload
                    reason, _ = self._base_checkout(base_ref)
                    if reason:
                        payload["reason"] = reason
                        return payload
                    failure_reason = "fetch_failed"
                    self._git(
                        "fetch",
                        "--no-tags",
                        "--no-recurse-submodules",
                        "--no-write-fetch-head",
                        "--refmap=",
                        "--",
                        remote,
                        f"{sources[0]}:{temporary_ref}",
                        timeout=60,
                        env_overrides={"GIT_TERMINAL_PROMPT": "0"},
                    )
                    failure_reason = "git_failed"
                    upstream = self._git(
                        "rev-parse", "--verify", f"{temporary_ref}^{{commit}}"
                    ).stdout.strip()
                    payload["upstream_commit"] = upstream
                    ancestry = self._git(
                        "merge-base", "--is-ancestor", original, upstream, check=False
                    )
                    if ancestry.returncode == 1:
                        payload["reason"] = "not_fast_forward"
                        return payload
                    if ancestry.returncode != 0:
                        raise RuntimeError(ancestry.stderr.strip())
                    if original == upstream:
                        payload["reason"] = "up_to_date"
                        return payload
                    reason, checked_out = self._base_checkout(base_ref)
                    if reason:
                        payload["reason"] = reason
                        return payload
                    if self._git("rev-parse", base_ref).stdout.strip() != original:
                        payload["reason"] = "base_moved"
                        return payload
                    failure_reason = "update_failed"
                    if checked_out:
                        # branch.<base>.mergeOptions may otherwise squash without moving HEAD.
                        self._git(
                            "merge",
                            "--ff-only",
                            "--no-squash",
                            "--no-autostash",
                            "--no-edit",
                            upstream,
                        )
                    else:
                        self._git("update-ref", base_ref, upstream, original)
                    if self._git("rev-parse", "--verify", base_ref).stdout.strip() != upstream:
                        raise RuntimeError("base branch did not advance to the fetched upstream")
                    payload.update(status="synced", reason="fast_forward")
                    return payload
                finally:
                    # Read actual state even if a concurrent writer or failed update moved it.
                    if base_ref is not None and payload["from_commit"] is not None:
                        try:
                            tip = self._git("rev-parse", "--verify", base_ref, check=False)
                            payload["to_commit"] = (
                                tip.stdout.strip() if tip.returncode == 0 else None
                            )
                        except (RuntimeError, OSError, subprocess.SubprocessError):
                            pass
                    try:
                        self._git("update-ref", "-d", temporary_ref)
                    except (RuntimeError, OSError, subprocess.SubprocessError):
                        pass
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            payload.update(status="failed", reason=failure_reason, detail=str(exc))
            return payload

    def require_current_base(self, base: str) -> dict[str, Any]:
        """Synchronize safely, then require the local base to contain its fetched upstream."""
        previous_timeout = self.command_timeout
        self.command_timeout = previous_timeout if previous_timeout is not None else 60
        remote = "configured remote"
        upstream = "configured upstream"
        try:
            # sync_base takes its own lock; do not nest finalization locks.
            payload = self.sync_base(base=base)
            if payload["reason"] in ("no_base", "no_upstream"):
                return payload
            remote = payload["remote"] or remote
            source = payload["upstream"]
            upstream = f"{remote}/{source}" if source else f"{remote} (ambiguous upstream)"
            with self._finalization_lock():
                if payload["reason"] == "unsupported_upstream":
                    raise RuntimeError("upstream is ambiguous; configure one upstream")
                if not payload["upstream_commit"] and payload["reason"] in (
                    "other_worktree",
                    "dirty_checkout",
                ):
                    temporary_ref = f"refs/helmraiser/base-sync/{uuid4()}"
                    try:
                        self._git(
                            "fetch",
                            "--no-tags",
                            "--no-recurse-submodules",
                            "--no-write-fetch-head",
                            "--refmap=",
                            "--",
                            remote,
                            f"{source}:{temporary_ref}",
                            timeout=60,
                            env_overrides={"GIT_TERMINAL_PROMPT": "0"},
                        )
                        payload["upstream_commit"] = self._git(
                            "rev-parse", "--verify", f"{temporary_ref}^{{commit}}"
                        ).stdout.strip()
                    finally:
                        try:
                            self._git("update-ref", "-d", temporary_ref)
                        except (RuntimeError, OSError, subprocess.SubprocessError):
                            pass
                if not payload["upstream_commit"]:
                    raise RuntimeError(payload.get("detail") or payload["reason"])
                contains = self._git(
                    "merge-base",
                    "--is-ancestor",
                    payload["upstream_commit"],
                    f"refs/heads/{base}",
                    check=False,
                )
                if contains.returncode != 0:
                    raise RuntimeError(
                        contains.stderr.strip() or "local base does not contain fetched upstream"
                    )
                return payload
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                f"cannot prove local {base} contains upstream {upstream}: {exc}; "
                f"update local {base} from {remote} (git fetch {remote}, then "
                f"fast-forward {base} where it is checked out) and approve again"
            ) from exc
        finally:
            self.command_timeout = previous_timeout

    def create(
        self, topic_id: str, run_id: str, resume: bool = False, base: str | None = None
    ) -> Path:
        """Create this run's new branch/worktree from an explicit base or checkout HEAD."""
        worktree_path = self._path(topic_id, run_id).resolve()
        branch_name = self._branch(topic_id, run_id)

        if resume:
            metadata = self._read_metadata(run_id)
            if (
                metadata["topic_id"] != topic_id
                or Path(metadata["worktree_path"]).resolve() != worktree_path
            ):
                raise RuntimeError("run worktree metadata does not match the requested topic/path")
            info = self.get_info(topic_id, run_id)
            if info is None:
                raise RuntimeError(f"registered worktree for run {run_id} is missing")
            return worktree_path

        if self.get_info(topic_id, run_id) is not None or worktree_path.exists():
            raise RuntimeError(f"worktree already exists for run {run_id}; use resume")
        if (
            self._git(
                "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}", check=False
            ).returncode
            == 0
        ):
            raise RuntimeError(f"branch already exists for run {run_id}; refusing to reuse it")

        if base is not None:
            base_ref = f"refs/heads/{base}"
            if self._git("show-ref", "--verify", "--quiet", base_ref, check=False).returncode != 0:
                raise RuntimeError(f"base branch {base} does not exist")
            base_branch = base
            base_commit = self._git("rev-parse", base_ref).stdout.strip()
        else:
            base_branch_result = self._git(
                "symbolic-ref", "--quiet", "--short", "HEAD", check=False
            )
            if base_branch_result.returncode != 0:
                raise RuntimeError(
                    "primary checkout is detached; refusing to create a run worktree"
                )
            base_branch = base_branch_result.stdout.strip()
            base_commit = self._git("rev-parse", "HEAD").stdout.strip()

        self._worktree_dir.mkdir(parents=True, exist_ok=True)
        metadata = {
            "run_id": run_id,
            "topic_id": topic_id,
            "worktree_path": str(worktree_path),
            "branch_name": branch_name,
            "base_branch": base_branch,
            "base_commit": base_commit,
        }
        # Persist intent first: failed creation remains diagnosable and recoverable.
        self._metadata_path(run_id).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        self._git("worktree", "add", "-b", branch_name, str(worktree_path), base_commit)
        return worktree_path

    def restore_pr(
        self,
        topic_id: str,
        run_id: str,
        *,
        branch_name: str,
        base_branch: str,
        worktree_path: str,
    ) -> dict[str, Any]:
        """Recover a published run at origin's head, without rewriting local history."""
        if branch_name != self._branch(topic_id, run_id):
            raise RuntimeError("run branch does not match its identity")
        path = Path(worktree_path).resolve()
        if path != self._worktree_dir.resolve() / f"worktree-{topic_id}-{run_id}":
            raise RuntimeError("recorded worktree is outside its expected path")
        previous_timeout = self.command_timeout
        self.command_timeout = previous_timeout if previous_timeout is not None else 60
        temporary_ref = f"refs/helmraiser/pr-restore/{uuid4()}"
        try:
            with self._finalization_lock():
                try:
                    metadata_path = path.parent / f"{run_id}.json"
                    if metadata_path.exists():
                        recorded = json.loads(metadata_path.read_text(encoding="utf-8"))
                        if (
                            recorded.get("run_id") != run_id
                            or recorded.get("topic_id") != topic_id
                            or recorded.get("branch_name") != branch_name
                            or recorded.get("base_branch") != base_branch
                            or Path(recorded.get("worktree_path", "")).resolve() != path
                        ):
                            raise RuntimeError("run worktree metadata does not match")
                    info = self.get_info(topic_id, run_id)
                    if info is not None and Path(info["path"]).resolve() != path:
                        raise RuntimeError("run branch is checked out outside its expected path")
                    if path.exists():
                        if info is None or not path.is_dir():
                            raise RuntimeError("existing path is not the registered run worktree")
                        if self._git("status", "--porcelain", cwd=path).stdout.strip():
                            raise RuntimeError("run worktree is dirty; refusing restoration")
                    elif info is not None:
                        raise RuntimeError("registered run worktree is missing; repair it first")
                    local_ref = f"refs/heads/{branch_name}"
                    local = self._git("rev-parse", "--verify", local_ref).stdout.strip()
                    self._git(
                        "fetch",
                        "--no-tags",
                        "--no-recurse-submodules",
                        "--no-write-fetch-head",
                        "--refmap=",
                        "--",
                        "origin",
                        f"{local_ref}:{temporary_ref}",
                    )
                    head = self._git("rev-parse", "--verify", temporary_ref).stdout.strip()
                    if (
                        self._git(
                            "merge-base", "--is-ancestor", local, head, check=False
                        ).returncode
                        != 0
                    ):
                        raise RuntimeError(
                            "local run branch is ahead of or diverges from the PR head"
                        )
                    base_commit = self._git(
                        "merge-base", f"refs/heads/{base_branch}", head
                    ).stdout.strip()
                    recreated = not path.exists()
                    if local != head:
                        if recreated:
                            self._git("update-ref", local_ref, head, local)
                        else:
                            self._git(
                                "merge",
                                "--ff-only",
                                "--no-squash",
                                "--no-autostash",
                                "--no-edit",
                                head,
                                cwd=path,
                            )
                    if recreated:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        self._git("worktree", "add", str(path), branch_name)
                    metadata = {
                        "run_id": run_id,
                        "topic_id": topic_id,
                        "worktree_path": str(path),
                        "branch_name": branch_name,
                        "base_branch": base_branch,
                        "base_commit": base_commit,
                    }
                    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
                    return {**metadata, "head_commit": head, "worktree_recreated": recreated}
                finally:
                    self._git("update-ref", "-d", temporary_ref)
        finally:
            self.command_timeout = previous_timeout

    def remove(self, topic_id: str, run_id: str) -> None:
        """Remove only this run's worktree, preserving its branch for recovery."""
        expected = self._path(topic_id, run_id).resolve()
        info = self.get_info(topic_id, run_id)
        if info is not None and Path(info["path"]).resolve() != expected:
            raise RuntimeError("registered run worktree is outside its expected path")
        if info is not None or expected.exists():
            self._git("worktree", "remove", str(expected))

    def discard(self, topic_id: str, run_id: str) -> None:
        """Remove this run's worktree and metadata while preserving its branch."""
        self.remove(topic_id, run_id)
        self._metadata_path(run_id).unlink(missing_ok=True)

    def exists(self, topic_id: str, run_id: str) -> bool:
        return self.get_info(topic_id, run_id) is not None

    def orphaned_metadata(self) -> list[dict[str, Any]]:
        """Return persisted run records whose worktree is no longer registered."""
        result = self._git("worktree", "list", "--porcelain", check=False)
        if result.returncode != 0:
            return []
        registered = {
            Path(line[9:]).resolve()
            for line in result.stdout.splitlines()
            if line.startswith("worktree ")
        }
        orphaned: list[dict[str, Any]] = []
        for metadata_path in sorted(self._worktree_dir.glob("*.json")):
            metadata = cast(dict[str, Any], json.loads(metadata_path.read_text(encoding="utf-8")))
            if Path(metadata["worktree_path"]).resolve() in registered:
                continue
            branch_name = str(metadata["branch_name"])
            branch_exists = (
                self._git(
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{branch_name}",
                    check=False,
                ).returncode
                == 0
            )
            orphaned.append(
                {
                    "run_id": str(metadata["run_id"]),
                    "topic_id": str(metadata["topic_id"]),
                    "branch_name": branch_name,
                    "worktree_path": str(metadata["worktree_path"]),
                    "metadata_path": str(metadata_path.resolve()),
                    "branch_exists": branch_exists,
                }
            )
        return orphaned

    def get_info(self, topic_id: str, run_id: str) -> dict[str, str] | None:
        branch_name = self._branch(topic_id, run_id)
        result = self._git("worktree", "list", "--porcelain", check=False)
        if result.returncode != 0:
            return None
        current: dict[str, str] | None = None
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if line.startswith("worktree "):
                current = {"path": line[9:]}
            elif line.startswith("branch "):
                if current and line[7:] == f"refs/heads/{branch_name}":
                    return current
                current = None
        return None

    def finalize_local(self, topic_id: str, run_id: str, *, rebase: bool = False) -> None:
        """Fast-forward the captured base, then clean the still-recoverable worktree.

        With ``rebase``, a primary HEAD that moved since the run started is handled by
        rebasing the run branch onto it first (inside the run worktree); a conflicting
        rebase is aborted and refused. Nothing is re-verified after a rebase, so only
        the human-driven ``helmraiser approve`` path opts in.
        """
        metadata = self._read_metadata(run_id)
        if metadata["topic_id"] != topic_id:
            raise RuntimeError("run worktree metadata belongs to another topic")

        worktree_path = Path(metadata["worktree_path"]).resolve()
        if worktree_path != self._path(topic_id, run_id).resolve():
            raise RuntimeError("run worktree metadata points outside its expected path")
        if self.get_info(topic_id, run_id) is None:
            raise RuntimeError("registered run worktree is missing")
        if self._git("status", "--porcelain", cwd=worktree_path).stdout.strip():
            raise RuntimeError("run worktree is dirty; commit its changes before finalization")

        branch_name = str(metadata["branch_name"])
        with self._finalization_lock():
            status = self._git(
                "status", "--porcelain", "--", ".", ":(exclude).helmraiser"
            ).stdout.strip()
            if status:
                raise RuntimeError("primary checkout is dirty; refusing local finalization")
            branch_result = self._git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
            if branch_result.returncode != 0:
                raise RuntimeError("primary checkout is detached; refusing local finalization")
            current_branch = branch_result.stdout.strip()
            if current_branch != metadata["base_branch"]:
                raise RuntimeError(
                    f"primary checkout is on {current_branch}, "
                    f"not the run's base {metadata['base_branch']}"
                )
            head = self._git("rev-parse", "HEAD").stdout.strip()
            if head != metadata["base_commit"]:
                if not rebase:
                    raise RuntimeError("primary checkout HEAD moved since run creation")
                result = self._git("rebase", head, cwd=worktree_path, check=False)
                if result.returncode != 0:
                    self._git("rebase", "--abort", cwd=worktree_path, check=False)
                    detail = result.stderr.strip() or result.stdout.strip()
                    raise RuntimeError(f"rebase onto moved HEAD failed: {detail}")
                metadata["base_commit"] = head
                self._metadata_path(run_id).write_text(
                    json.dumps(metadata, indent=2), encoding="utf-8"
                )
            if (
                self._git(
                    "merge-base",
                    "--is-ancestor",
                    metadata["base_commit"],
                    branch_name,
                    check=False,
                ).returncode
                != 0
            ):
                raise RuntimeError("run branch cannot fast-forward the captured base")

            # Keep the worktree until the only operation that changes the base succeeds.
            self._git("merge", "--ff-only", branch_name)
            self.remove(topic_id, run_id)
            self._git("branch", "-d", branch_name)
            self._metadata_path(run_id).unlink()

    def finalize_remote(
        self,
        topic_id: str,
        run_id: str,
        *,
        remote: str = "origin",
        rebase: bool = False,
        deliver: Callable[[str, str], str] | None = None,
        push: Callable[[Path, str, str], None] | None = None,
    ) -> dict[str, str]:
        """Push a run branch, call ``deliver(base_branch, branch_name)`` (e.g. open a
        pull request) and only then remove the worktree; the local branch stays. If
        ``deliver`` raises, the worktree is kept so ``helmraiser approve`` can retry."""
        from helmraiser.loop.delivery import push_branch

        metadata = self._read_metadata(run_id)
        if metadata["topic_id"] != topic_id:
            raise RuntimeError("run worktree metadata belongs to another topic")

        worktree_path = Path(metadata["worktree_path"]).resolve()
        if worktree_path != self._path(topic_id, run_id).resolve():
            raise RuntimeError("run worktree metadata points outside its expected path")
        if self.get_info(topic_id, run_id) is None:
            raise RuntimeError("registered run worktree is missing")
        if self._git("status", "--porcelain", cwd=worktree_path).stdout.strip():
            raise RuntimeError("run worktree is dirty; commit its changes before finalization")

        branch_name = str(metadata["branch_name"])
        base_branch = str(metadata["base_branch"])
        with self._finalization_lock():
            if rebase:
                base_head = self._git("rev-parse", f"refs/heads/{base_branch}").stdout.strip()
                result = self._git("rebase", base_head, cwd=worktree_path, check=False)
                if result.returncode != 0:
                    self._git("rebase", "--abort", cwd=worktree_path, check=False)
                    detail = result.stderr.strip() or result.stdout.strip()
                    raise RuntimeError(f"rebase onto moved HEAD failed: {detail}")
                metadata["base_commit"] = base_head
                self._metadata_path(run_id).write_text(
                    json.dumps(metadata, indent=2), encoding="utf-8"
                )
            (push or push_branch)(worktree_path, branch_name, remote)
            delivered = {"base_branch": base_branch, "branch_name": branch_name}
            if deliver is not None:
                delivered["pr_url"] = deliver(base_branch, branch_name)
            self.remove(topic_id, run_id)
            self._metadata_path(run_id).unlink()
        return delivered

    def salvage_uncommitted(self, worktree_path: Path) -> None:
        """Commit uncommitted agent work for recovery."""
        # Import lazily: isolation's package imports task/project worktree helpers.
        from helmraiser.isolation.placeholders import remove_placeholders

        if self._git("status", "--porcelain", cwd=worktree_path).stdout.strip():
            names = remove_placeholders(worktree_path)
            self._git("add", "-A", cwd=worktree_path)
            if names:
                staged = self._git("diff", "--cached", "--quiet", cwd=worktree_path, check=False)
                if staged.returncode == 0:
                    return
                if staged.returncode != 1:
                    staged.check_returncode()
            self._git(
                "commit",
                "-m",
                "salvage: recover uncommitted work from implementation agent",
                cwd=worktree_path,
            )
