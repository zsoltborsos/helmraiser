"""Hints for known sandbox and environment failure signatures in a command's output.

``helmraiser preflight`` shows them per command; a real run's failed verify or evidence
step carries them (``VerifyResult.hints``, ``EvidenceResult.hints``), computed from the
step's full output, so an environment gap (a service no ``[[services]]`` entry provides,
a path srt denies) does not read as a model failure.
"""

from __future__ import annotations

import re
from collections.abc import Collection

SETUP_READONLY_HINT = (
    "setup tried to write a read-only path: inside the sandbox it writes only the worktree, "
    "/tmp, the per-project npm/pip cache and [setup] write in .helmraiser/project.toml; add the "
    "directory there or point the tool's cache at /tmp"
)
_SETUP_READONLY = re.compile(r"EROFS|read-only file system", re.IGNORECASE)


def setup_readonly_line(output: str) -> str | None:
    """The first read-only diagnostic in full setup output, not its displayed tail."""
    return next((line for line in output.splitlines() if _SETUP_READONLY.search(line)), None)


SOCKET_HINT = (
    "Unix socket blocked by the sandbox (srt allows none): run tsx as `node --import tsx`, "
    "and keep tools from opening their own IPC socket"
)
DENIED_HINT = (
    "a path is denied in the sandbox: verify writes only the worktree, "
    "$HELMRAISER_EVIDENCE_DIR and /tmp, and cannot read credential directories, the data dir "
    "or other projects; point caches at the worktree or /tmp from setup or [defaults.env]"
)
NOT_PERMITTED_HINT = (
    "the sandbox refused an operation (srt's seccomp filter or bubblewrap): the command may "
    "need a Unix socket, a namespace or a device srt does not allow"
)
# What srt 0.0.77 masks inside writable roots when absent (sandbox-utils.js DANGEROUS_FILES
# and the dangerous directories): such a path reads as /dev/null, a character device.
SRT_MASKED = (
    ".gitconfig",
    ".gitmodules",
    ".bashrc",
    ".bash_profile",
    ".zshrc",
    ".zprofile",
    ".profile",
    ".ripgreprc",
    ".mcp.json",
    ".vscode",
    ".idea",
)

_SOCKET = re.compile(r"\bEPERM\b.*(\blisten\b|\bconnect\b|\.sock\b|\.pipe\b|\bsocket\b)")
_SOCKET_BEFORE = re.compile(r"\b(listen|connect)\s+EPERM\b")
_DENIED = re.compile(r"\bEACCES\b|permission denied", re.IGNORECASE)
_NOT_REGULAR = re.compile(r"not a regular file", re.IGNORECASE)
_NOT_PERMITTED = re.compile(r"operation not permitted", re.IGNORECASE)
_REFUSED = re.compile(r"ECONNREFUSED|connection refused", re.IGNORECASE)
_LOOPBACK = re.compile(r"(127\.0\.0\.1|localhost|::1)\]?(?::(\d+)|\"?, port (\d+))?")
_MASKED = re.compile(
    r"(?:^|[\s/'\"(])(" + "|".join(re.escape(name) for name in SRT_MASKED) + r")(?=$|[\s'\":,)/])"
)


def loopback_hint(target: str, *, sandboxed: bool) -> str:
    if sandboxed:
        return (
            f"nothing listens on {target} inside the sandbox: only the ports of "
            "[[services]] in .helmraiser/project.toml are reachable; declare the service "
            "and use the variable it provides"
        )
    return (
        f"nothing listens on {target}: no [[services]] entry in .helmraiser/project.toml "
        "provides it; declare the service and use the variable it provides"
    )


def sandbox_hints(
    output: str, provided_ports: Collection[int] = (), *, sandboxed: bool = True
) -> list[str]:
    """One hint per known signature in ``output``, in order, without repeats.

    A refused loopback connection gets a hint unless its port is one of
    ``provided_ports`` (the run's services). Outside the sandbox (``sandboxed`` false)
    only that hint applies: the others describe srt's settings."""
    hints: list[str] = []
    previous = ""  # the last non-blank line: a diagnostic header may name the path
    for line in output.splitlines():
        found: str | None = None
        masked = _MASKED.search(line) or _MASKED.search(previous)
        if sandboxed and (_SOCKET.search(line) or _SOCKET_BEFORE.search(line)):
            found = SOCKET_HINT
        elif _REFUSED.search(line) and (loopback := _LOOPBACK.search(line)):
            port = loopback.group(2) or loopback.group(3)
            if port is None or int(port) not in provided_ports:
                target = f"{loopback.group(1)}:{port}" if port else loopback.group(1)
                found = loopback_hint(target, sandboxed=sandboxed)
        elif not sandboxed:
            pass
        elif masked and (_DENIED.search(line) or _NOT_REGULAR.search(line)):
            name = masked.group(1)
            found = (
                f"srt masks {name} in writable roots (it reads as /dev/null there): "
                f"scope the command to the changed paths or have the tool ignore {name}; "
                "a tool that only warns about it and exits 0 did not fail because of it"
            )
        elif _DENIED.search(line):
            found = DENIED_HINT
        elif _NOT_PERMITTED.search(line):
            found = NOT_PERMITTED_HINT
        if found and found not in hints:
            hints.append(found)
        if line.strip():
            previous = line
    return hints
