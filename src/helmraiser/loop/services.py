"""Project services for verify and evidence (S4b): Docker containers on the host.

For each ``[[services]]`` entry of ``project.toml`` (``task/project.py``) Helmraiser
picks a free port it owns on ``127.0.0.1`` (binds port 0, reads the port, closes)
and runs, outside srt:

    docker run -d --name helmraiser-<run8>-<name> --label helmraiser.run=<run id>
        --label helmraiser.service=<name> -p 127.0.0.1:<host port>:<port>
        -e NAME ... <image> <command ...>

``-e NAME`` takes each value from the docker CLI's environment, so values stay out
of argv and events. The mapping is read back with ``docker port``; the service is
ready once its host port holds a TCP connection open (a closed or reset connection,
as Docker's port proxy gives before the container listens, is not ready) and, when
the spec has ``ready``, ``docker exec <container> <ready ...>`` exits 0 (Postgres
accepts TCP before it accepts queries).
``provides`` is then expanded with ``{port}`` (the host port) and ``{host}``
(``127.0.0.1``). Cleanup goes by the ``helmraiser.run`` label: ``stop_services``
removes a run's containers with their anonymous volumes (``docker rm -f -v``), and
``orphaned_containers`` finds labelled containers of runs that are over or unknown.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from helmraiser.task.project import ServiceSpec

HOST = "127.0.0.1"
RUN_LABEL = "helmraiser.run"
SERVICE_LABEL = "helmraiser.service"
RUN_TIMEOUT = 600  # docker run may pull the image
DOCKER_TIMEOUT = 60
READY_POLL_SECONDS = 0.25
PORT_ATTEMPTS = 3  # another process may take the chosen port before docker binds it
PORT_TAKEN = ("port is already allocated", "address already in use")
SS_TIMEOUT = 5
SS_PROCESS = re.compile(r'users:\(\("([^"]+)",pid=(\d+)')
USAGE_FOOTER = re.compile(r"^Run '.+ --help' for more information$")  # ends docker's errors

Event = Callable[[str, dict[str, Any]], None]
Probe = Callable[[int], bool]


class ServiceError(Exception):
    """A service did not start or become ready; the message is the run's failure."""


@dataclass(frozen=True)
class RunningService:
    name: str
    image: str
    container: str
    port: int  # the container's port
    host_port: int
    provides: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """For events and the outcome: the provided names, never their values."""
        return {
            "name": self.name,
            "image": self.image,
            "container": self.container,
            "port": self.port,
            "host_port": self.host_port,
            "provides": sorted(self.provides),
        }


def container_name(run_id: str, service: str) -> str:
    return f"helmraiser-{run_id[:8]}-{service}"


def free_port() -> int:
    """A port on 127.0.0.1 that nothing listens on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def port_ready(port: int) -> bool:
    """True when ``127.0.0.1:<port>`` accepts a connection and keeps it open."""
    try:
        sock = socket.create_connection((HOST, port), timeout=1.0)
    except OSError:
        return False
    with sock:
        sock.settimeout(0.3)
        try:
            return bool(sock.recv(1))  # the server spoke first
        except TimeoutError:
            return True  # open and silent: a server waiting for its client
        except OSError:
            return False


def _docker(
    docker: str, *args: str, env: Mapping[str, str] | None = None, timeout: int = DOCKER_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    """``docker`` with the worker's environment, plus ``env`` (a service's variables)."""
    try:
        return subprocess.run(
            [docker, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env={**os.environ, **(env or {})},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess([docker, *args], 125, "", str(exc))


def _last_line(proc: subprocess.CompletedProcess[str]) -> str:
    lines = [
        line
        for line in (proc.stderr or proc.stdout).strip().splitlines()
        if line.strip() and not USAGE_FOOTER.match(line.strip())
    ]
    return lines[-1] if lines else f"exit status {proc.returncode}"


def _port_holder(port: int, docker: str = "docker") -> str | None:
    """What holds a host port, best effort: a running container, else a listening process."""
    try:
        names = _docker(docker, "ps", "--filter", f"publish={port}", "--format", "{{.Names}}")
        if names.returncode == 0 and names.stdout.strip():
            return f"container {names.stdout.split()[0]}"
        listening = _docker("ss", "-ltnpH", "sport", "=", f":{port}", timeout=SS_TIMEOUT)
        process = SS_PROCESS.search(listening.stdout) if listening.returncode == 0 else None
    except Exception:  # only a hint: it must not replace the run's own failure
        return None
    return f"process {process[1]} (pid {process[2]})" if process else None


def run_argv(run_id: str, spec: ServiceSpec, host_port: int, docker: str = "docker") -> list[str]:
    """``docker run`` for ``spec``, published on ``127.0.0.1:<host_port>`` only."""
    argv = [
        docker,
        "run",
        "-d",
        "--name",
        container_name(run_id, spec.name),
        "--label",
        f"{RUN_LABEL}={run_id}",
        "--label",
        f"{SERVICE_LABEL}={spec.name}",
        "-p",
        f"{HOST}:{host_port}:{spec.port}",
    ]
    for name in spec.env:
        argv += ["-e", name]
    return [*argv, spec.image, *spec.command]


def _published_port(docker: str, container: str, port: int) -> int | None:
    """The host port Docker bound for ``port`` on 127.0.0.1, read back."""
    proc = _docker(docker, "port", container, f"{port}/tcp")
    for line in proc.stdout.splitlines() if proc.returncode == 0 else []:
        host, _, bound = line.strip().rpartition(":")
        if host == HOST and bound.isdigit():
            return int(bound)
    return None


def wait_ready(
    port: int,
    timeout: float,
    probe: Probe | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Poll ``probe`` (default ``port_ready``) until it holds or ``timeout`` passes."""
    probe = probe or port_ready
    deadline = clock() + timeout
    while True:
        if probe(port):
            return True
        if clock() >= deadline:
            return False
        sleep(READY_POLL_SECONDS)


def start_service(
    run_id: str,
    spec: ServiceSpec,
    env: Mapping[str, str],
    on_event: Event,
    docker: str = "docker",
    probe: Probe | None = None,
) -> RunningService:
    """Start ``spec`` and wait for it. ``env`` holds the service's expanded ``env``
    values, which the docker CLI passes on for each ``-e NAME``. Raises
    ``ServiceError``; the caller removes whatever was created with ``stop_services``."""
    container = container_name(run_id, spec.name)
    started = time.monotonic()
    # A fixed host_port cannot move to another port, so a taken one is not retried.
    attempts = 1 if spec.host_port is not None else PORT_ATTEMPTS
    for attempt in range(1, attempts + 1):
        argv = run_argv(run_id, spec, spec.host_port or free_port(), docker)
        proc = _docker(docker, *argv[1:], env=env, timeout=RUN_TIMEOUT)
        if proc.returncode == 0:
            break
        detail = _last_line(proc)
        _docker(docker, "rm", "-f", "-v", container)  # a created container keeps the name
        if attempt == attempts or not any(taken in detail.lower() for taken in PORT_TAKEN):
            reason = f"services: {spec.name}: docker run failed: {detail}"
            if spec.host_port is not None and any(t in detail.lower() for t in PORT_TAKEN):
                holder = _port_holder(spec.host_port, docker)
                reason += (
                    f" (host_port {spec.host_port} is in use by {holder}; stop it)"
                    if holder
                    else f" (host_port {spec.host_port} is in use; stop whatever holds it)"
                )
            on_event("service_failed", {"name": spec.name, "reason": reason})
            raise ServiceError(reason)
    host_port = _published_port(docker, container, spec.port)
    if host_port is None:
        reason = f"services: {spec.name}: no 127.0.0.1 port published for {spec.port}/tcp"
        on_event("service_failed", {"name": spec.name, "reason": reason})
        raise ServiceError(reason)
    provides = {
        key: template.format(port=host_port, host=HOST) for key, template in spec.provides.items()
    }
    service = RunningService(spec.name, spec.image, container, spec.port, host_port, provides)
    on_event("service_start", {**service.as_dict(), "env": sorted(spec.env)})
    tcp_ready = probe or port_ready

    def ready(port: int) -> bool:
        if not tcp_ready(port):
            return False
        if not spec.ready:
            return True
        timeout = min(DOCKER_TIMEOUT, spec.ready_timeout)
        return _docker(docker, "exec", container, *spec.ready, timeout=timeout).returncode == 0

    if not wait_ready(host_port, spec.ready_timeout, ready):
        reason = f"services: {spec.name} not ready after {spec.ready_timeout}s"
        logs = _docker(docker, "logs", "--tail", "20", container)
        tail = (logs.stdout + logs.stderr).strip().splitlines()[-20:]
        on_event("service_failed", {"name": spec.name, "reason": reason, "logs": tail})
        raise ServiceError(reason)
    on_event("service_ready", {"name": spec.name, "seconds": round(time.monotonic() - started, 3)})
    return service


def run_containers(run_id: str, docker: str = "docker") -> list[str] | None:
    """IDs of the containers labelled with ``run_id``; None when docker fails."""
    proc = _docker(docker, "ps", "-a", "-q", "--filter", f"label={RUN_LABEL}={run_id}")
    if proc.returncode != 0:
        return None
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def remove_containers(ids: list[str], docker: str = "docker") -> str | None:
    """``docker rm -f -v``: None on success, else the reason."""
    if not ids:
        return None
    proc = _docker(docker, "rm", "-f", "-v", *ids)
    return None if proc.returncode == 0 else _last_line(proc)


def stop_services(run_id: str, docker: str = "docker") -> tuple[list[str], str | None]:
    """Remove every container labelled with ``run_id``: (removed IDs, error)."""
    ids = run_containers(run_id, docker)
    if ids is None:
        return [], "docker ps failed"
    error = remove_containers(ids, docker)
    return ([] if error else ids), error


def labelled_containers(docker: str = "docker") -> list[dict[str, str]] | None:
    """Every container with a ``helmraiser.run`` label; None when docker fails."""
    proc = _docker(
        docker,
        "ps",
        "-a",
        "--filter",
        f"label={RUN_LABEL}",
        "--format",
        f'{{{{.ID}}}}\t{{{{.Label "{RUN_LABEL}"}}}}\t{{{{.Names}}}}\t{{{{.Status}}}}',
    )
    if proc.returncode != 0:
        return None
    found: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and parts[0]:
            found.append({"id": parts[0], "run_id": parts[1], "name": parts[2], "status": parts[3]})
    return found


def docker_ready(docker: str = "docker") -> str | None:
    """None when the Docker daemon answers, else the reason."""
    proc = _docker(docker, "info", "--format", "{{.ServerVersion}}", timeout=15)
    return None if proc.returncode == 0 else _last_line(proc)
