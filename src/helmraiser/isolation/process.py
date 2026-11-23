"""``isolation: process`` — anthropics/sandbox-runtime (``srt``) over bubblewrap (spec §7).

``wrap`` writes ``<run_dir>/srt-settings.json`` and returns
``[<absolute env path>, "CLAUDE_CODE_TMPDIR=<tmpdir>", <absolute srt path>,
"--settings", <file>, "--", *argv]``. The ``env`` prefix sets srt's own temporary
directory even when callers strip ``CLAUDE_*`` from their environment. Unlike the
spec's ``["srt", "--settings", <file>, *argv]``, this also includes ``--``. srt
(``@anthropic-ai/sandbox-runtime`` 0.0.77) parses its options with commander
and accepts unknown options, so without ``--`` it would try to read pi's flags
as its own. After ``--`` it takes the command as separate argv (a second
``--``, pi's own before the prompt, is passed through), shell-quotes it and runs
it with ``bash -c`` inside bwrap. That puts the whole command in one argument,
which Linux caps at 128 KiB; pi prompts above 60 KiB already go through an
``@file``.

srt 0.0.77 rejects a settings file without ``network.deniedDomains`` and
``filesystem.denyWrite``. All paths in the file are absolute (``~`` expanded
here, not left to srt).

Sandbox settings: reads are allowed everywhere except

- ``DENY_READ``: credential and private locations, listed whether or not they
  exist (srt accepts a missing path and mounts nothing for it);
- the data dir that holds the run dir (the ledger and every other run);
- ``isolation.deny_read`` from the Helmraiser config, then the project's
  ``[sandbox] deny_read`` (relative entries beneath both the primary checkout
  and the worktree);
- other projects: every top-level directory of each ``isolation.projects_root``
  except the one that holds the task's repository.

A denied directory that holds a path the run needs (the worktree, the git common
dir, the run dir, the primary checkout) is denied entry by entry around it
instead: srt does not bind an ``allowWrite`` path back when an ``allowRead``
carve-out above it re-opens a denied directory, so the run dir cannot be a
carve-out inside a denied ``~/.helmraiser`` and still take pi's session writes.
``allowRead`` re-opens what pi itself loads from another project: local
paths in ``~/.pi/agent/settings.json`` (``packages`` and the resource lists) and
the targets of symlinks in ``~/.pi/agent`` and its ``skills``, ``prompts``,
``themes`` and ``extensions`` directories (the global ``AGENTS.md``, linked
skills). Ordinary (not readonly) pi sessions also re-open the resolved ``lb``
package root when another-project filtering hides it. Neither carve-out re-opens
a credential or configured denial.

Writes go only to the worktree, the run's ``sessions/`` directory, the configured
temporary directory, ``/tmp`` (unless it contains the git common dir, which must not
be opened through a broader root),
the existing Leeboard store (``LEEBOARD_HOME``, else ``~/.leeboard``), and two more
places pi needs, found by running pi under srt. The store's existing ``.git`` is
denied writes so a worker cannot plant hooks for the owner's later commits:


- The git common dir's ``objects/``, the linked worktree's own admin dir, and
  its branch's loose-ref and reflog directories. The admin dir's ``config.worktree``
  is denied so it cannot plant a ``core.hooksPath`` for later unconfined commits.
  Sessions can still write other refs in their own branch directory (other runs
  of the same task). ``git stash`` cannot write ``refs/stash``; automatic ``git gc``
  that packs refs fails with a warning only.
- ``~/.pi/agent``. pi takes ``auth.json.lock`` / ``settings.json.lock``
  directories there on every start and rewrites ``auth.json`` on an OAuth
  refresh. Every other entry (lock dirs aside) that exists when the settings are written
  (``settings.json`` with its ``packages``, ``skills``, ``npm``, ...) is
  denied, so a sandboxed pi cannot change what a later unconfined pi loads.

``readonly`` (intake) drops every write root except the session directory,
the configured temporary directory and ``~/.pi/agent``: no worktree, no git common
dir, no ``/tmp`` root.

Setup's sandbox settings allow writes only to the worktree, ``/tmp``, the configured
temporary directory, the owned per-project
npm/pip cache, and project-declared write roots. It uses setup-specific registry domains and has no pi resource
carve-outs, Git writes, run-directory writes, or Unix socket access. Its settings
are ``<run_dir>/srt-settings-setup.json``.

The command sandbox settings (``settings(..., command=True)``, ``wrap_command``) are for the
verify and evidence steps of a run. It has the same read denials, without the
``allowRead`` carve-outs for what pi loads, and writes only to the worktree, the
run's ``evidence/`` directory, ``/tmp`` and the configured temporary directory:
not the rest of the run dir (the diff,
events and summary that the reviewer and the owner read), not the git common dir,
not ``~/.pi/agent``. Its settings go to ``<run_dir>/srt-settings-<kind>.json``.

Network: only ``allowed_domains``, and for the command sandbox settings the literal
``127.0.0.1:<port>`` of each project service the run started (S4b; the step reaches
them through the relays of ``isolation/relay.py``). Unix sockets stay blocked (srt's
default; ``allowAllUnixSockets`` is never set).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import helmraiser.config as helmraiser_config
from helmraiser.isolation.relay import relay_argv
from helmraiser.isolation.retry import RetryObserver, SandboxLaunch
from helmraiser.leeboard import lb_install_root, leeboard_home

DEFAULT_ALLOWED_DOMAINS: tuple[str, ...] = (
    "api.openai.com",
    "chatgpt.com",
    "*.openai.com",
    "opencode.ai",
    "*.opencode.ai",
    "github.com",
)
DENY_READ: tuple[str, ...] = (
    "~/.ssh",
    # Conventional home for credentials interactive agents may use.
    "~/.secrets",
    "~/.gnupg",
    "~/.aws",
    "~/.config/gh",
    "~/.config/.wrangler",
    "~/.config/rustdesk",
    "~/.claude",
    "~/.codex",
    "~/.gemini",
    "~/.config/opencode",
    "~/.local/share/opencode",
    "~/.config/kilo",
    "~/.local/share/kilo",
    "~/.local/share/keyrings",
    "~/.password-store",
    "~/.config/chromium",
    "~/.config/google-chrome",
    "~/.config/BraveSoftware",
    "~/.mozilla",
    "~/.docker",
    "~/.npmrc",
    "~/.pypirc",
    "~/.netrc",
    "~/.git-credentials",
    "~/.kube",
    "~/.config/tailscale",
    "~/.local/share/tailscale",
    # Helmraiser's ledger and config; the run's own dir stays readable (see above).
    "~/.helmraiser",
    # pi's interactive sessions; a run's sessions live in its run dir.
    "~/.pi/agent/sessions",
)
SETTINGS_FILENAME = "srt-settings.json"
# The command sandbox settings: ``srt-settings-verify.json``, ``srt-settings-evidence.json``.
COMMAND_SETTINGS_FILENAME = "srt-settings-{kind}.json"
SETUP_SETTINGS_FILENAME = "srt-settings-setup.json"
PI_AGENT_DIR = "~/.pi/agent"
# The one ~/.pi/agent entry pi writes (OAuth refresh). ``*.lock`` entries stay
# writable too, so pi can clear a stale lock left by a killed session.
PI_AGENT_WRITABLE: tuple[str, ...] = ("auth.json",)
# ~/.pi/agent directories whose symlinked entries pi loads (skills linked from a repo).
PI_RESOURCE_DIRS: tuple[str, ...] = ("skills", "prompts", "themes", "extensions")
PI_SETTINGS_LISTS: tuple[str, ...] = ("packages", "extensions", "skills", "prompts", "themes")
REMOTE_SOURCE_PREFIXES: tuple[str, ...] = ("npm:", "git:", "git@", "http://", "https://", "ssh://")
# srt refuses to start on Linux unless these are on PATH as well (no --version check).
SRT_LINUX_DEPS: tuple[str, ...] = ("socat", "rg")
# srt's generateProxyEnvVars (sandbox-utils.js) defaults TMPDIR here.
DEFAULT_SRT_TMPDIR = Path("/tmp/claude")


class SrtNotFoundError(RuntimeError):
    """The configured or discovered srt executable could not be resolved."""


_srt_cache: dict[str | None, str] = {}


def reset_srt_cache() -> None:
    """Forget successful resolutions (for example after changing configuration)."""
    _srt_cache.clear()


def _executable_file(path: str | Path) -> bool:
    return Path(path).is_file() and os.access(path, os.X_OK)


def _mise_shim(path: str, mise: str | None) -> bool:
    real = Path(os.path.realpath(path))
    return (real.name == "mise" and real.is_file()) or (
        Path(path).parent.name == "shims" and mise is not None
    )


def resolve_srt(configured: str | None = None) -> str:
    """Resolve srt lazily, independently of a project's mise configuration.

    A mise shim either resolves to a file named ``mise``, or has a parent named
    ``shims`` with mise on PATH. The latter covers script shims and MISE_DATA_DIR,
    while leaving an asdf-only shims directory alone. Mise runs from home, not the
    project's cwd. Node resolution is deliberately unchanged.
    """
    cached = _srt_cache.get(configured)
    if cached is not None and _executable_file(cached):
        return cached
    _srt_cache.pop(configured, None)
    fix = f"set isolation.srt to an absolute executable path in {helmraiser_config.CONFIG_FILE}"
    if configured is not None:
        try:
            path = Path(configured).expanduser()
        except RuntimeError as exc:
            raise SrtNotFoundError(f"invalid isolation.srt {configured!r}: {exc}; {fix}") from exc
        if not path.is_absolute() or not _executable_file(path):
            raise SrtNotFoundError(f"invalid isolation.srt {configured!r}; {fix}")
        resolved = str(path)
    else:
        candidate = shutil.which("srt")
        if candidate is None:
            raise SrtNotFoundError(f"srt not found on PATH; {fix}")
        candidate = os.path.abspath(candidate)
        mise = shutil.which("mise")
        if not _mise_shim(candidate, mise):
            resolved = candidate
        else:
            detail = "mise not found on PATH"
            if mise is not None:
                try:
                    home = Path.home()
                    proc = subprocess.run(
                        [os.path.abspath(mise), "which", "srt"],
                        cwd=home,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=15,
                    )
                    output = proc.stdout.strip()
                    path = home / output
                    if (
                        proc.returncode == 0
                        and output
                        and _executable_file(path)
                        and not _mise_shim(str(path), mise)
                    ):
                        resolved = os.path.abspath(path)
                        _srt_cache[configured] = resolved
                        return resolved
                    detail = (proc.stderr or proc.stdout).strip() or (
                        f"mise which srt returned no executable (exit status {proc.returncode})"
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    detail = str(exc)
            raise SrtNotFoundError(f"srt not found on PATH: {detail}; {fix}")
    _srt_cache[configured] = resolved
    return resolved


def _tool_ok(binary: str, *, label: str | None = None) -> str | None:
    """None when ``<binary> --version`` runs and exits 0, else the reason."""
    label = label or binary
    if not Path(binary).is_absolute() and shutil.which(binary) is None:
        return f"{binary} not found on PATH"
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, check=False, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{label} --version failed: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"exit status {proc.returncode}"
        return f"{label} --version failed: {detail}"
    return None


def _abs(path: str | Path) -> str:
    return str(Path(path).expanduser().absolute())


def _srt_argv(
    srt: str,
    argv: list[str],
    settings: Path,
    tmpdir: Path,
    kind: str,
    observer: RetryObserver | None,
) -> SandboxLaunch:
    tmpdir.mkdir(parents=True, exist_ok=True)
    env = shutil.which("env")
    if env is None:
        raise RuntimeError("env not found on PATH; required to set srt's temporary directory")
    return SandboxLaunch(
        [
            os.path.abspath(env),
            f"CLAUDE_CODE_TMPDIR={tmpdir}",
            srt,
            "--settings",
            _abs(settings),
            "--",
            *argv,
        ],
        settings,
        tmpdir,
        kind,
        observer,
    )


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _deny_around(denied: Path, keep: list[Path]) -> list[str]:
    """``denied``, or when it holds a kept path, each entry beside the way down to it."""
    real = denied.resolve()
    inside = [path for path in keep if _within(path, real)]
    if not inside:
        return [str(denied)]
    if real in inside or not real.is_dir():
        return []
    out: list[str] = []
    for entry in sorted(real.iterdir()):
        out += _deny_around(entry, inside)
    return out


def _local_sources(settings_file: Path) -> list[Path]:
    """Local paths configured in a pi ``settings.json`` (packages and resource lists)."""
    try:
        data = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    paths: list[Path] = []
    for key in PI_SETTINGS_LISTS:
        entries = data.get(key)
        for entry in entries if isinstance(entries, list) else []:
            source = entry.get("source") if isinstance(entry, dict) else entry
            if not isinstance(source, str) or source.startswith(("!", "-")):
                continue
            source = source.removeprefix("+")
            if not source or source.startswith(REMOTE_SOURCE_PREFIXES):
                continue
            path = Path(source).expanduser()
            paths.append(path if path.is_absolute() else settings_file.parent / path)
    return paths


def pi_read_needs(agent_dir: Path) -> list[Path]:
    """What pi reads from outside ``agent_dir``: configured local packages and resources,
    and the targets of symlinks in it and its resource directories."""
    if not agent_dir.is_dir():
        return []
    needs = _local_sources(agent_dir / "settings.json")
    links = [entry for entry in agent_dir.iterdir() if entry.is_symlink()]
    for name in PI_RESOURCE_DIRS:
        folder = agent_dir / name
        if folder.is_dir() and not folder.is_symlink():
            links += [entry for entry in folder.iterdir() if entry.is_symlink()]
    needs += [link.resolve() for link in sorted(links)]
    return [path.resolve() for path in needs if path.exists()]


def git_common_dir(worktree: Path) -> Path | None:
    """The git common dir of ``worktree`` (the primary ``.git``), or None if not a repo."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = proc.stdout.strip()
    return Path(out) if proc.returncode == 0 and out else None


def tracked_denials(repo: Path, entries: tuple[str, ...]) -> dict[str, list[str]]:
    """Relative ``deny_read`` entries that match files tracked in ``repo`` (``git
    ls-files``), with the files. Inside srt such a file reads as missing or empty, so
    pi could commit its deletion."""
    found: dict[str, list[str]] = {}
    for entry in entries:
        if Path(entry).is_absolute() or entry.startswith("~"):
            continue
        try:
            proc = subprocess.run(
                ["git", "ls-files", "-z", "--", entry],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        files = [name for name in proc.stdout.split("\0") if name] if proc.returncode == 0 else []
        if files:
            found[entry] = files
    return found


def tracked_denial_warnings(found: dict[str, list[str]]) -> list[str]:
    return [
        f"sandbox deny_read {entry!r} matches {len(files)} tracked file(s) ({files[0]}"
        f"{', ...' if len(files) > 1 else ''}); inside srt they read as missing, so pi "
        "could commit their deletion"
        for entry, files in found.items()
    ]


class ProcessIsolation:
    level = "process"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        deny_read: tuple[str, ...] = (),
        primary_checkout: Path | None = None,
        readonly: bool = False,
        data_dir: Path | None = None,
    ):
        config = config or {}
        self.readonly = readonly
        self.retry_observer: RetryObserver | None = None
        isolation_cfg = config.get("isolation") or {}
        tmpdir = isolation_cfg.get("tmpdir", DEFAULT_SRT_TMPDIR)
        self.tmpdir: Path = Path(tmpdir).expanduser()
        if not self.tmpdir.is_absolute():
            raise ValueError(
                f"isolation.tmpdir must be an absolute path after expansion: {tmpdir!r}"
            )
        self._configured_srt: str | None = isolation_cfg.get("srt")
        domains = isolation_cfg.get("allowed_domains") or list(DEFAULT_ALLOWED_DOMAINS)
        self.allowed_domains: tuple[str, ...] = tuple(str(d) for d in domains)
        roots = isolation_cfg.get("projects_root") or ()
        self.projects_roots: tuple[str, ...] = (
            (str(roots),) if isinstance(roots, str) else tuple(str(r) for r in roots)
        )
        self.global_deny_read: tuple[str, ...] = tuple(
            str(p) for p in isolation_cfg.get("deny_read") or ()
        )
        self.project_deny_read = deny_read
        self.primary_checkout = primary_checkout
        # Diagnostics can put their run directory outside the real data dir.
        # Keep its denial independent of whether setup prepares a cache.
        self.data_dir = data_dir.expanduser().resolve() if data_dir is not None else None

    def srt_path(self) -> str:
        return resolve_srt(self._configured_srt)

    def set_project_deny_read(self, deny_read: tuple[str, ...], primary_checkout: Path) -> None:
        """Apply read denials loaded from the primary checkout's project settings."""
        self.project_deny_read = deny_read
        self.primary_checkout = primary_checkout

    def configured_deny_read(self) -> tuple[str, ...]:
        """The config's ``isolation.deny_read`` followed by the project's ``deny_read``."""
        return (*self.global_deny_read, *self.project_deny_read)

    def _denied_paths(self, worktree: Path) -> list[Path]:
        denied: list[Path] = []
        for configured in self.configured_deny_read():
            path = Path(configured)
            if path.is_absolute() or configured.startswith("~"):
                denied.append(Path(_abs(configured)))
            else:
                if self.primary_checkout is not None:
                    denied.append(Path(_abs(self.primary_checkout / path)))
                denied.append(Path(_abs(worktree / path)))
        return denied

    def _other_projects(self, keep: list[Path]) -> list[Path]:
        """Top-level directories of each projects root that hold nothing the run needs."""
        others: list[Path] = []
        for configured in self.projects_roots:
            root = Path(_abs(configured)).resolve()
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                real = entry.resolve()
                if entry.is_dir() and not any(_within(p, real) or _within(real, p) for p in keep):
                    others.append(entry)
        return others

    def _read_denials(
        self, worktree: Path, run_dir: Path, *, setup_cache: Path | None = None
    ) -> tuple[list[str], list[Path], list[Path]]:
        """Return shared read denials, fixed denials, and other-project denials."""
        common = git_common_dir(worktree)
        keep = [
            path.resolve()
            for path in (worktree, run_dir, common, self.primary_checkout)
            if path is not None
        ]
        fixed = [Path(_abs(path)) for path in DENY_READ]
        if setup_cache is not None:
            setup_cache = setup_cache.resolve()
            keep.append(setup_cache)
            fixed.append(setup_cache.parent.parent)
        if run_dir.parent.name == "runs":
            fixed.append(run_dir.parent.parent)
        if self.data_dir is not None:
            fixed.append(self.data_dir)
        fixed += self._denied_paths(worktree)
        others = self._other_projects(keep)
        deny_read: list[str] = []
        for path in dict.fromkeys([*fixed, *others]):
            deny_read += _deny_around(path, keep)
        return deny_read, fixed, others

    def _allow_tmpdir(self, allow_write: list[str], common: Path | None) -> None:
        """Grant temporary storage without opening the protected Git common directory."""
        tmpdir = self.tmpdir.resolve()
        if common is not None and _within(common.resolve(), tmpdir):
            raise RuntimeError(
                f"isolation.tmpdir {self.tmpdir} contains git common directory {common}"
            )
        # Keep custom temporary storage explicit even beneath the generic /tmp
        # grant; the default remains covered by /tmp without a redundant entry.
        if not any(
            _within(tmpdir, Path(root).resolve())
            for root in allow_write
            if root != "/tmp" or self.tmpdir == DEFAULT_SRT_TMPDIR
        ):
            allow_write.append(str(self.tmpdir))

    def settings(
        self,
        worktree: Path,
        run_dir: Path,
        *,
        command: bool = False,
        service_ports: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        """Settings: deny credentials, the ledger and other projects; write the worktree,
        sessions, /tmp, configured temporary storage and what pi needs. ``command``:
        the verify / evidence settings, which writes only the worktree, the run's
        ``evidence/``, /tmp and configured temporary storage, and may also
        reach ``127.0.0.1:<port>`` for each of ``service_ports``."""
        if service_ports and not command:
            raise ValueError("service ports belong to the command sandbox settings only")
        worktree = Path(_abs(worktree))
        run_dir = Path(_abs(run_dir))
        common = git_common_dir(worktree)
        deny_read, fixed, others = self._read_denials(worktree, run_dir)
        network = {"allowedDomains": list(self.allowed_domains), "deniedDomains": []}
        if command:
            network["allowedDomains"] += [f"127.0.0.1:{port}" for port in service_ports]
            allow_write = [str(worktree), str(run_dir / "evidence"), "/tmp"]
            self._allow_tmpdir(allow_write, common)
            return {
                "filesystem": {
                    "allowRead": [],
                    "allowWrite": allow_write,
                    "denyRead": deny_read,
                    "denyWrite": [],
                },
                "network": network,
            }
        agent_dir = Path(_abs(PI_AGENT_DIR))
        read_needs = pi_read_needs(agent_dir)
        if not self.readonly:
            install_root = lb_install_root()
            if install_root is not None:
                read_needs.append(install_root)
        allow_read = [
            str(need)
            for need in dict.fromkeys(read_needs)
            if any(_within(need, other.resolve()) for other in others)
            and not any(_within(need, path.resolve()) for path in fixed)
        ]
        allow_write = [str(run_dir / "sessions")]
        if not self.readonly:
            allow_write.insert(0, str(worktree))
            if common is None or not _within(common, Path("/tmp")):
                allow_write.insert(1, "/tmp")
        deny_write: list[str] = []
        if common is not None and not self.readonly:
            admin = Path(
                subprocess.run(
                    ["git", "rev-parse", "--absolute-git-dir"],
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                ).stdout.strip()
            )
            # Exposure probes also use session settings in the primary checkout.
            # There, granting the admin dir would open the entire common dir:
            # leave Git protected by srt's default instead of adding write roots.
            if admin.resolve() != common.resolve():
                branch = subprocess.run(
                    ["git", "symbolic-ref", "-q", "HEAD"],
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                if branch.returncode not in (0, 1):
                    raise RuntimeError(f"cannot read worktree branch: {branch.stderr.strip()}")
                ref = branch.stdout.strip()
                if branch.returncode == 0 and (
                    not ref.startswith("refs/heads/") or "/" not in ref.removeprefix("refs/heads/")
                ):
                    raise RuntimeError(
                        f"sandbox branch must have a directory (e.g. feature/topic/run): {ref}"
                    )
                allow_write += [_abs(common / "objects"), _abs(admin)]
                if branch.returncode == 0:
                    for directory in ((common / ref).parent, (common / "logs" / ref).parent):
                        directory.mkdir(parents=True, exist_ok=True)
                        allow_write.append(_abs(directory))
                # srt's denyWrite bind mount needs an existing file. Do not overwrite
                # an existing per-worktree configuration when preparing settings.
                config = admin / "config.worktree"
                if not config.exists():
                    config.touch()
                deny_write.append(_abs(config))
        self._allow_tmpdir(allow_write, common)
        if not self.readonly:
            store = leeboard_home()
            if store.is_dir():
                allow_write.append(str(store))
                if (store / ".git").exists():
                    deny_write.append(str(store / ".git"))
        if agent_dir.is_dir():
            allow_write.append(str(agent_dir))
            deny_write += [
                str(entry)
                for entry in sorted(agent_dir.iterdir())
                if entry.name not in PI_AGENT_WRITABLE and not entry.name.endswith(".lock")
            ]
        return {
            "filesystem": {
                "allowRead": allow_read,
                "allowWrite": allow_write,
                "denyRead": deny_read,
                "denyWrite": deny_write,
            },
            "network": network,
        }

    def setup_settings(
        self,
        worktree: Path,
        run_dir: Path,
        write: tuple[Path, ...],
        allowed_domains: tuple[str, ...],
        *,
        setup_cache: Path | None = None,
    ) -> dict[str, Any]:
        """Setup's own sandbox settings: project/cache writes and registry network only."""
        worktree = Path(_abs(worktree))
        run_dir = Path(_abs(run_dir))
        deny_read, _, _ = self._read_denials(worktree, run_dir, setup_cache=setup_cache)
        allow_write = list(dict.fromkeys([str(worktree), "/tmp", *map(_abs, write)]))
        self._allow_tmpdir(allow_write, git_common_dir(worktree))
        return {
            "filesystem": {
                "allowRead": [],
                "allowWrite": allow_write,
                "denyRead": deny_read,
                "denyWrite": [],
            },
            "network": {
                "allowedDomains": list(allowed_domains),
                "deniedDomains": [],
            },
        }

    def write_setup_settings(
        self,
        worktree: Path,
        run_dir: Path,
        write: tuple[Path, ...],
        allowed_domains: tuple[str, ...],
        *,
        setup_cache: Path | None = None,
    ) -> Path:
        path = run_dir / SETUP_SETTINGS_FILENAME
        settings = self.setup_settings(
            worktree, run_dir, write, allowed_domains, setup_cache=setup_cache
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n")
        return path

    def write_settings(
        self,
        worktree: Path,
        run_dir: Path,
        *,
        command_kind: str | None = None,
        service_ports: tuple[int, ...] = (),
    ) -> Path:
        """``<run_dir>/srt-settings.json``, or with ``command_kind`` the command sandbox settings in
        ``<run_dir>/srt-settings-<kind>.json``."""
        if command_kind is None:
            path = run_dir / SETTINGS_FILENAME
        else:
            path = run_dir / COMMAND_SETTINGS_FILENAME.format(kind=command_kind)
        settings = self.settings(
            worktree, run_dir, command=command_kind is not None, service_ports=service_ports
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n")
        return path

    def wrap(self, argv: list[str], cwd: Path, run_dir: Path) -> list[str]:
        (run_dir / "sessions").mkdir(parents=True, exist_ok=True)
        settings = self.write_settings(cwd, run_dir)
        return _srt_argv(
            self.srt_path(), argv, settings, self.tmpdir, "session", self.retry_observer
        )

    def wrap_setup(
        self,
        argv: list[str],
        cwd: Path,
        run_dir: Path,
        write: tuple[Path, ...],
        allowed_domains: tuple[str, ...],
        *,
        setup_cache: Path | None = None,
    ) -> list[str]:
        settings = self.write_setup_settings(
            cwd, run_dir, write, allowed_domains, setup_cache=setup_cache
        )
        return _srt_argv(self.srt_path(), argv, settings, self.tmpdir, "setup", self.retry_observer)

    def wrap_command(
        self,
        argv: list[str],
        cwd: Path,
        run_dir: Path,
        kind: str,
        service_ports: tuple[int, ...] = (),
    ) -> list[str]:
        """``argv``, a verify or evidence step (``kind``), under the command sandbox settings. With
        ``service_ports`` the settings allow each ``127.0.0.1:<port>`` and ``argv`` runs
        behind the in-sandbox relays (``isolation/relay.py``)."""
        (run_dir / "evidence").mkdir(parents=True, exist_ok=True)
        settings = self.write_settings(cwd, run_dir, command_kind=kind, service_ports=service_ports)
        if service_ports:
            argv = relay_argv(argv, service_ports)
        return _srt_argv(self.srt_path(), argv, settings, self.tmpdir, kind, self.retry_observer)

    def check_available(self) -> str | None:
        try:
            srt = self.srt_path()
        except SrtNotFoundError as exc:
            return str(exc)
        reason = _tool_ok(srt, label="srt")
        if reason is not None:
            return reason
        reason = _tool_ok("bwrap")
        if reason is not None:
            return reason
        for binary in SRT_LINUX_DEPS:
            if shutil.which(binary) is None:
                return f"{binary} not found on PATH (srt needs it)"
        return None
