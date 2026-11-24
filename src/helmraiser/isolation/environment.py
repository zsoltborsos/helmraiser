"""The environment a run's child processes see.

The worker is often started from an interactive (agent) session, so its own
environment carries an SSH agent socket, session tokens and the like. None of
that is passed on as is:

- pi sessions (and the ``srt`` that wraps them) get an allowlist,
  :func:`session_environment`.
- ``setup``, ``verify`` and ``evidence`` commands get the full environment minus
  secret-shaped names, :func:`command_environment`.

Both remove the worker's Python environment variables and venv bin directories
from PATH so bare Python does not use the worker's editable install.

Both take ``extra`` last: Helmraiser's ``HELMRAISER_*`` variables and the task's and
project's ``env`` (``[defaults.env]``). Naming a variable there is the explicit
opt-in that passes a stripped or non-allowlisted name through, for example
``GH_TOKEN = "$GH_TOKEN"``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping
from fnmatch import fnmatchcase
from pathlib import Path

from helmraiser.code_snapshot import without_snapshot

# What pi sessions inherit from the worker. Found by running the real srt with
# ``env -i`` and adding back names (``pi --version``, ``node --version`` and the
# mise ``node``/``gh`` shims under srt): PATH alone is enough on the owner's
# machine; srt resolves bwrap/socat/rg through it and itself sets TMPDIR, the proxy
# variables and SANDBOX_RUNTIME inside the sandbox. pi and mise fall back to the
# passwd entry without HOME, and mise's shims need no MISE_* variable. The rest are
# kept so tools behave as they do for the user: identity and shell (HOME, USER,
# LOGNAME, SHELL), locale and terminal (LANG, LANGUAGE, LC_*, TERM, TZ), the XDG
# base directories (pi, node and mise read config and caches through them), TLS
# trust and node flags (NODE_OPTIONS, NODE_EXTRA_CA_CERTS, SSL_CERT_FILE,
# SSL_CERT_DIR), proxies (pi honours HTTP(S)_PROXY) and the PI_* variables pi
# documents as its own configuration (docs/environment-variables.md). Provider API
# keys are not passed: pi reads its credentials from ~/.pi/agent/auth.json.
SESSION_ENV_NAMES: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "LEEBOARD_HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "TERM",
        "TZ",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "NODE_OPTIONS",
        "NODE_EXTRA_CA_CERTS",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "PI_CODING_AGENT_DIR",
        "PI_PACKAGE_DIR",
        "PI_OFFLINE",
        "PI_SKIP_VERSION_CHECK",
        "PI_TELEMETRY",
        "PI_CACHE_RETENTION",
    }
)
SESSION_ENV_PREFIXES: tuple[str, ...] = ("LC_",)

# The worker's interpreter settings must not select its editable install in runs.
WORKER_PYTHON_NAMES: frozenset[str] = frozenset({"VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME"})

# Removed from setup / verify / evidence (and never allowlisted for pi). Names are
# exact; patterns match case-insensitively. The agent sockets hand out keys, and
# DBUS_SESSION_BUS_ADDRESS reaches the desktop keyring (Secret Service).
# DISPLAY and WAYLAND_DISPLAY go too: an unattended run has no business on the
# desktop (a Wayland or X client can capture the screen), and headless browsers
# do not need them. A project that runs a headed browser re-adds them in
# ``[defaults.env]``.
STRIPPED_NAMES: frozenset[str] = (
    frozenset(
        {
            "SSH_AUTH_SOCK",
            "SSH_AGENT_PID",
            "GPG_AGENT_INFO",
            "CLAUDECODE",
            "LEEBOARD_HARNESS",
            "LEEBOARD_SESSION",
            "DBUS_SESSION_BUS_ADDRESS",
            "DISPLAY",
            "WAYLAND_DISPLAY",
        }
    )
    | WORKER_PYTHON_NAMES
)
STRIPPED_PATTERNS: tuple[str, ...] = (
    "*TOKEN*",
    "*SECRET*",
    "*PASSWORD*",
    "*PASSWD*",
    "*_KEY",
    "*APIKEY*",
    "*API_KEY*",
    "*CREDENTIAL*",
    "CLAUDE_*",
    "ANTHROPIC_*",
    "OPENAI_*",
    "GH_*",
    "GITHUB_*",
    "AWS_*",
    "GOOGLE_*",
    "AZURE_*",
    "CLOUDFLARE_*",
    "CF_*",
)


def is_stripped(name: str) -> bool:
    """True for a name runs never inherit (secrets, desktop or interpreter settings)."""
    upper = name.upper()
    return name in STRIPPED_NAMES or any(fnmatchcase(upper, p) for p in STRIPPED_PATTERNS)


def _allowlisted(name: str) -> bool:
    return name in SESSION_ENV_NAMES or name.startswith(SESSION_ENV_PREFIXES)


def _worker_venv_bins(source: Mapping[str, str]) -> set[str]:
    bins: set[str] = set()
    virtual_env = source.get("VIRTUAL_ENV")
    if virtual_env:
        bins.add(str(Path(virtual_env) / "bin"))
    if sys.prefix != sys.base_prefix:
        bins.add(str(Path(sys.executable).parent))
    return bins


def _without_worker_venv(source: Mapping[str, str]) -> dict[str, str]:
    bins = _worker_venv_bins(source)
    env = dict(source)
    for name in WORKER_PYTHON_NAMES:
        env.pop(name, None)
    if "PATH" in env:
        env["PATH"] = os.pathsep.join(
            entry for entry in env["PATH"].split(os.pathsep) if entry not in bins
        )
    return env


def session_environment(
    extra: Mapping[str, str], base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The complete environment for a pi session: the allowlist of ``base``
    (default ``os.environ``), without the worker's Python settings and venv PATH
    entries, plus ``extra``."""
    source = _without_worker_venv(os.environ if base is None else base)
    return {**{k: v for k, v in source.items() if _allowlisted(k)}, **extra}


def command_environment(
    extra: Mapping[str, str], base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The complete environment for setup / verify / evidence: ``base`` (default
    ``os.environ``) without secret-shaped names, the worker's Python settings and
    venv PATH entries, and its code snapshot (``PYTHONPATH`` entry and markers:
    the steps test the worktree's code), plus ``extra``."""
    source = _without_worker_venv(without_snapshot(os.environ if base is None else base))
    return {**{k: v for k, v in source.items() if not is_stripped(k)}, **extra}


def stripped_names(base: Iterable[str] | None = None) -> list[str]:
    """The names in ``base`` (default ``os.environ``) that runs do not inherit."""
    return sorted(name for name in (os.environ if base is None else base) if is_stripped(name))


def stripped_path_entries(base: Mapping[str, str] | None = None) -> list[str]:
    """The sorted, unique PATH entries actually removed from the worker environment."""
    source = os.environ if base is None else base
    if "PATH" not in source:
        return []
    return sorted(set(source["PATH"].split(os.pathsep)) & _worker_venv_bins(source))
