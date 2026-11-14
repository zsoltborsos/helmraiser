"""Per-project defaults (``<repo>/.helmraiser/project.toml``) and the worktree environment.

```toml
[defaults]
setup = "python3 -m venv .venv && .venv/bin/pip install -q -e '.[dev]'"

[defaults.env]
PYTHONPATH = "$HELMRAISER_WORKTREE/src"

[models]
implement = "opencode-go/deepseek-v4.1-flash:medium"
fallback = "openai-codex/gpt-6.1-sol:high"
```

The ``[models]`` table sets model profile defaults and overrides the config's
model profiles for this project's tasks; a task's own ``models:`` block still
wins. A task's own ``setup`` / ``env`` win over the project defaults (``env``
merges key by key). Values may reference ``$HELMRAISER_WORKTREE`` and the other
``HELMRAISER_*`` variables Helmraiser exports; ``expand_env`` resolves them.

``[[services]]`` declares containers that Helmraiser starts on the host for verify
and evidence (``loop/services.py``)::

    [[services]]
    name = "postgres"                          # slug: container helmraiser-<run8>-postgres
    image = "postgres:16"
    env = { POSTGRES_PASSWORD = "helmraiser" }   # expanded like [defaults.env]
    port = 5432                                # the container's TCP port
    command = ["postgres", "-c", "fsync=off"]  # optional
    ready_timeout = 60                         # seconds for the port to answer (default)
    ready = ["pg_isready", "-h", "127.0.0.1"]  # optional: must also exit 0 (docker exec)
    provides = { DATABASE_URL = "postgresql://postgres:helmraiser@{host}:{port}/postgres" }

``{port}`` is the host port Helmraiser chose and ``{host}`` is ``127.0.0.1``.
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from string import Formatter, Template
from typing import Any

from helmraiser.config import load_config, validate_run_limit
from helmraiser.pipeline.worktree import WorktreeManager
from helmraiser.task.model import PROFILE_NAMES, TaskSpec

PROJECT_FILE = Path(".helmraiser") / "project.toml"
SERVICE_KEYS = frozenset(
    {"name", "image", "env", "port", "host_port", "command", "ready_timeout", "ready", "provides"}
)
SERVICE_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROVIDES_FIELDS = frozenset({"port", "host"})
DEFAULT_READY_TIMEOUT = 60
DEFAULT_SETUP_ALLOWED_DOMAINS: tuple[str, ...] = (
    "registry.npmjs.org",
    "pypi.org",
    "files.pythonhosted.org",
)


@dataclass(frozen=True)
class SetupConfig:
    """Filesystem and network allowances for a project's setup command."""

    write: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = DEFAULT_SETUP_ALLOWED_DOMAINS
    sandbox: str | None = None


@dataclass(frozen=True)
class ServiceSpec:
    """One ``[[services]]`` entry of ``project.toml``."""

    name: str
    image: str
    port: int  # the container's TCP port
    env: dict[str, str] = field(default_factory=dict)
    command: tuple[str, ...] = ()
    ready_timeout: int = DEFAULT_READY_TIMEOUT
    provides: dict[str, str] = field(default_factory=dict)  # NAME -> "{host}:{port}" template
    ready: tuple[str, ...] = ()  # argv run with ``docker exec``; empty: the TCP check alone
    # A fixed 127.0.0.1 port, for projects whose tests hard-code one; None: a free port.
    host_port: int | None = None


@dataclass(frozen=True)
class ProjectDefaults:
    setup: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    worktree_dir: str | None = None
    delivery: str | None = None
    base: str | None = None
    deny_read: tuple[str, ...] = ()
    require_approval: bool = False
    setup_config: SetupConfig = field(default_factory=SetupConfig)
    services: tuple[ServiceSpec, ...] = ()
    scout_exclude: tuple[str, ...] = ()
    max_runs_per_project: int | None = None
    models: dict[str, str] = field(default_factory=dict)


def _string_table(value: object, where: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a table")
    for key, item in value.items():
        if not ENV_NAME.match(key):
            raise ValueError(f"{where}: {key!r} is not a valid environment variable name")
        if not isinstance(item, str):
            raise ValueError(f"{where}.{key} must be a string")
    return dict(value)


def _models(raw: object, path: Path) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: [models] must be a table")
    unknown = sorted(set(raw) - set(PROFILE_NAMES))
    if unknown:
        raise ValueError(f"{path}: models: unknown key(s) {', '.join(unknown)}")
    models: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{path}: models.{key} must be a non-empty string")
        models[key] = value.strip()
    return models


def _service(raw: object, path: Path, index: int) -> ServiceSpec:
    where = f"{path}: services[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be a table")
    unknown = sorted(set(raw) - SERVICE_KEYS)
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}")
    name = raw.get("name")
    if not isinstance(name, str) or not SERVICE_NAME.match(name):
        raise ValueError(f"{where}.name must be a slug (a-z, 0-9 and '-', at most 32 characters)")
    where = f"{path}: services.{name}"
    image = raw.get("image")
    if not isinstance(image, str) or not image.strip():
        raise ValueError(f"{where}.image must be a non-empty string")
    port = raw.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"{where}.port must be an integer from 1 to 65535")
    host_port = raw.get("host_port")
    if host_port is not None and (
        isinstance(host_port, bool) or not isinstance(host_port, int) or not 1 <= host_port <= 65535
    ):
        raise ValueError(f"{where}.host_port must be an integer from 1 to 65535")
    command = raw.get("command", [])
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ValueError(f"{where}.command must be a list of strings")
    ready_timeout = raw.get("ready_timeout", DEFAULT_READY_TIMEOUT)
    if isinstance(ready_timeout, bool) or not isinstance(ready_timeout, int) or ready_timeout < 1:
        raise ValueError(f"{where}.ready_timeout must be a positive integer (seconds)")
    ready = raw.get("ready")
    if ready is not None and not (
        isinstance(ready, list)
        and ready
        and all(isinstance(item, str) for item in ready)
        and ready[0].strip()
    ):
        raise ValueError(f"{where}.ready must be a non-empty list of strings (argv)")
    provides = _string_table(raw.get("provides", {}), f"{where}.provides")
    for key, template in provides.items():
        try:
            fields = {parsed[1] for parsed in Formatter().parse(template) if parsed[1] is not None}
        except ValueError as exc:
            raise ValueError(f"{where}.provides.{key}: {exc}") from exc
        if not fields <= PROVIDES_FIELDS:
            bad = ", ".join(repr(f) for f in sorted(fields - PROVIDES_FIELDS))
            raise ValueError(
                f"{where}.provides.{key}: unknown placeholder {bad}; use {{port}} or {{host}}"
            )
    return ServiceSpec(
        name=name,
        image=image.strip(),
        port=port,
        env=_string_table(raw.get("env", {}), f"{where}.env"),
        command=tuple(command),
        ready_timeout=ready_timeout,
        provides=provides,
        ready=tuple(ready or ()),
        host_port=host_port,
    )


def _services(raw: object, path: Path) -> tuple[ServiceSpec, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"{path}: services must be an array of tables ([[services]])")
    services = tuple(_service(item, path, index) for index, item in enumerate(raw))
    for what, values in (
        ("service name", [service.name for service in services]),
        ("provided variable", [key for service in services for key in service.provides]),
        ("host_port", [str(s.host_port) for s in services if s.host_port is not None]),
    ):
        duplicates = sorted({value for value in values if values.count(value) > 1})
        if duplicates:
            raise ValueError(f"{path}: duplicate {what} {', '.join(duplicates)}")
    return services


def _non_empty_strings(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and bool(item.strip()) for item in value
    ):
        raise ValueError(f"{where} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _check_keys(table: dict[str, Any], known: set[str], where: str) -> None:
    unknown = sorted(set(table) - known)
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}")


def load_project_defaults(project: Path) -> ProjectDefaults:
    path = project / PROJECT_FILE
    if not path.is_file():
        return ProjectDefaults()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    top_keys = {
        "max_runs_per_project",
        "defaults",
        "sandbox",
        "scout",
        "setup",
        "services",
        "models",
    }
    defaults_keys = {"setup", "worktree_dir", "delivery", "base", "env", "require_approval"}
    unknown = sorted(set(data) - top_keys)
    if unknown:
        hints = "; ".join(
            f"{key} belongs under [defaults]" for key in unknown if key in defaults_keys
        )
        message = f"{path}: unknown key(s) {', '.join(unknown)}"
        raise ValueError(f"{message}; {hints}" if hints else message)
    run_limit = None
    if "max_runs_per_project" in data:
        try:
            run_limit = validate_run_limit(data["max_runs_per_project"])
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from exc
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ValueError(f"{path}: [defaults] must be a table")
    _check_keys(defaults, defaults_keys, f"{path}: defaults")
    sandbox = data.get("sandbox", {})
    if not isinstance(sandbox, dict):
        raise ValueError(f"{path}: [sandbox] must be a table")
    _check_keys(sandbox, {"deny_read"}, f"{path}: sandbox")
    scout = data.get("scout", {})
    if not isinstance(scout, dict):
        raise ValueError(f"{path}: [scout] must be a table")
    _check_keys(scout, {"exclude"}, f"{path}: scout")
    setup_table = data.get("setup", {})
    if not isinstance(setup_table, dict):
        raise ValueError(
            f"{path}: [setup] must be a table; the setup command belongs under [defaults]"
        )
    _check_keys(setup_table, {"sandbox", "write", "allowed_domains"}, f"{path}: setup")
    exclude = scout.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(item, str) for item in exclude):
        raise ValueError(f"{path}: scout.exclude must be a list of strings")
    scout_exclude: list[str] = []
    for raw in exclude:
        normalized = raw.strip().replace("\\", "/").rstrip("/")
        parts = normalized.split("/")
        if (
            not normalized
            or normalized.startswith("/")
            or re.match(r"^[A-Za-z]:/", normalized)
            or ".." in parts
        ):
            raise ValueError(f"{path}: scout.exclude entries must be safe relative paths")
        canonical = PurePosixPath(normalized).as_posix()
        if canonical == ".":
            raise ValueError(f"{path}: scout.exclude entries must be safe relative paths")
        scout_exclude.append(canonical)
    require_approval = defaults.get("require_approval", False)
    if not isinstance(require_approval, bool):
        raise ValueError(f"{path}: defaults.require_approval must be a boolean")
    setup_sandbox = setup_table.get("sandbox")
    if setup_sandbox is not None and setup_sandbox != "host":
        raise ValueError(f'{path}: setup.sandbox must be "host" when set')
    if setup_sandbox == "host" and not require_approval:
        raise ValueError(
            f'{path}: setup.sandbox = "host" requires defaults.require_approval = true'
        )
    setup_write = _non_empty_strings(setup_table.get("write", []), f"{path}: setup.write")
    setup_domains = _non_empty_strings(
        setup_table.get("allowed_domains", list(DEFAULT_SETUP_ALLOWED_DOMAINS)),
        f"{path}: setup.allowed_domains",
    )
    deny_read = sandbox.get("deny_read", [])
    if not isinstance(deny_read, list) or not all(isinstance(item, str) for item in deny_read):
        raise ValueError(f"{path}: sandbox.deny_read must be a list of strings")
    setup = defaults.get("setup")
    worktree_dir = defaults.get("worktree_dir")
    delivery = defaults.get("delivery")
    base = defaults.get("base")
    env = defaults.get("env") or {}
    if not isinstance(env, dict):
        raise ValueError(f"{path}: [defaults.env] must be a table")
    services = _services(data.get("services", []), path)
    models = _models(data.get("models", {}), path)
    return ProjectDefaults(
        setup=str(setup).strip() if setup else None,
        env={str(k): str(v) for k, v in env.items()},
        worktree_dir=str(worktree_dir).strip() if worktree_dir else None,
        delivery=str(delivery).strip() if delivery else None,
        base=str(base).strip() if base is not None else None,
        deny_read=tuple(deny_read),
        require_approval=require_approval,
        setup_config=SetupConfig(
            write=setup_write,
            allowed_domains=setup_domains,
            sandbox=setup_sandbox,
        ),
        services=services,
        scout_exclude=tuple(scout_exclude),
        max_runs_per_project=run_limit,
        models=models,
    )


def effective_run_limit(project: Path, config: dict[str, Any]) -> int:
    override = load_project_defaults(project).max_runs_per_project
    return (
        override
        if override is not None
        else validate_run_limit(config.get("max_runs_per_project", 1))
    )


def setup_write_paths(
    setup: SetupConfig, project: Path, environment: dict[str, str]
) -> tuple[Path, ...]:
    """Expand and resolve setup cache write roots, preserving declaration order."""
    paths: list[Path] = []
    home = environment.get("HOME")
    for configured in setup.write:
        expanded = Template(configured).safe_substitute(environment)
        if expanded == "~" or expanded.startswith("~/"):
            if home:
                expanded = home + expanded[1:]
            else:
                expanded = str(Path(expanded).expanduser())
        path = Path(expanded)
        if not path.is_absolute():
            path = project / path
        resolved = path.resolve()
        if resolved not in paths:
            paths.append(resolved)
    return tuple(paths)


def _device(path: Path) -> int:
    """Return the device of the nearest existing ancestor, without creating paths."""
    path = path.resolve()
    while True:
        try:
            return path.stat().st_dev
        except FileNotFoundError:
            path = path.parent


def pnpm_store_check(
    project: Path,
    defaults: ProjectDefaults,
    worktree_dir: Path,
    environ: Mapping[str, str],
) -> dict[str, Any] | None:
    """Report informational pnpm store filesystem and setup-write diagnostics."""
    store_key = next(
        (
            key
            for key in defaults.env
            if key.lower() in {"npm_config_store_dir", "pnpm_config_store_dir"}
        ),
        None,
    )
    if store_key is None:
        if not re.search(r"\bpnpm\b", defaults.setup or ""):
            return None
        return {
            "store": None,
            "same_filesystem": None,
            "in_setup_write": None,
            "warnings": [
                "Set npm_config_store_dir in [defaults.env]; "
                "pnpm under srt cannot find a store at the filesystem root."
            ],
        }

    base = dict(environ)
    expanded = expand_env(defaults.env, base)
    env = {**base, **expanded}
    store = Path(expanded[store_key])
    if not store.is_absolute():
        store = project / store
    store = store.resolve()
    worktree_dir = worktree_dir.resolve()
    same_filesystem = _device(store) == _device(worktree_dir)
    in_setup_write = (
        None
        if defaults.setup_config.sandbox == "host"
        else any(
            store.is_relative_to(root)
            for root in setup_write_paths(defaults.setup_config, project, env)
        )
    )
    warnings = []
    if not same_filesystem:
        warnings.append(
            f"pnpm store {store} is on a different filesystem from worktrees {worktree_dir}; "
            "pnpm copies instead of hard-linking."
        )
    if in_setup_write is False:
        warnings.append(
            f"setup under srt cannot write to pnpm store {store}; add it to [setup].write."
        )
    return {
        "store": str(store),
        "same_filesystem": same_filesystem,
        "in_setup_write": in_setup_write,
        "warnings": warnings,
    }


def worktree_manager(project: Path) -> WorktreeManager:
    """Use the project override or a stable per-repository directory under the global root."""
    configured = load_project_defaults(project).worktree_dir
    if configured is not None:
        expanded = expand_env({"worktree_dir": configured}, dict(os.environ))["worktree_dir"]
        path = Path(expanded)
        if not path.is_absolute():
            path = project / path
        return WorktreeManager(project, worktree_dir=path.resolve())

    configured_root = load_config().get("worktree_root")
    if configured_root is None:
        # ledger.db imports task models through task.loader, so import lazily.
        from helmraiser.ledger.db import default_data_dir

        root = default_data_dir() / "worktrees"
    else:
        if not isinstance(configured_root, str) or not configured_root.strip():
            raise ValueError("worktree_root must be a non-empty string or null")
        expanded = expand_env({"root": configured_root}, dict(os.environ))["root"]
        root = Path(expanded).expanduser().resolve()
    resolved = project.resolve()
    suffix = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    return WorktreeManager(project, worktree_dir=root / f"{resolved.name}-{suffix}")


def effective_setup(task: TaskSpec, defaults: ProjectDefaults) -> str | None:
    return task.setup or defaults.setup


def effective_env(task: TaskSpec, defaults: ProjectDefaults) -> dict[str, str]:
    return {**defaults.env, **task.env}


def expand_env(env: dict[str, str], base: dict[str, str]) -> dict[str, str]:
    """Substitute ``$VAR`` / ``${VAR}`` in each value from ``base``; unknown names stay."""
    return {key: Template(value).safe_substitute(base) for key, value in env.items()}
