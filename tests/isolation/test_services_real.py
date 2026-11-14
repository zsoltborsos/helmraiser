"""Services end to end with the real docker and the real srt (S4b): Postgres, Redis and
Qdrant start on Helmraiser-chosen loopback ports, and verify steps under the command
sandbox settings reach them through the in-sandbox relays. A port that is not declared stays
unreachable, Unix sockets stay blocked, and the containers and their anonymous volumes
are gone afterwards.

Skipped unless ``docker info`` works, the three images are already pulled (the test
never pulls), the real srt is available and the suite is not itself inside srt.
"""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from helmraiser.isolation.process import ProcessIsolation
from helmraiser.loop.services import RunningService, run_containers, start_service, stop_services
from helmraiser.loop.verify import run_verify
from helmraiser.task.model import VerifyStep
from helmraiser.task.project import ServiceSpec
from tests.isolation.test_process_settings import REAL_SRT, _git, _repo, needs_real_srt

# Resolved at import, before the suite's guard stub shadows docker on PATH.
REAL_DOCKER = shutil.which("docker")
IMAGES = ("postgres:16", "redis:7", "qdrant/qdrant:latest")


def _docker_ok() -> bool:
    if REAL_DOCKER is None:
        return False
    try:
        info = subprocess.run([REAL_DOCKER, "info"], capture_output=True, timeout=20)
        images = subprocess.run(
            [REAL_DOCKER, "image", "inspect", *IMAGES], capture_output=True, timeout=20
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return info.returncode == 0 and images.returncode == 0


# Opt-in: this starts three real containers, which a routine suite run should not do.
_OPT_IN = os.environ.get("HELMRAISER_DOCKER_TESTS") == "1"

needs_real_docker = pytest.mark.skipif(
    not _OPT_IN or not _docker_ok(),
    reason="set HELMRAISER_DOCKER_TESTS=1 (needs docker and " f"{', '.join(IMAGES)} pulled)",
)

SPECS = (
    ServiceSpec(
        "postgres",
        "postgres:16",
        5432,
        env={"POSTGRES_PASSWORD": "helmraiser", "POSTGRES_DB": "app"},
        provides={"DATABASE_URL": "postgresql://postgres:helmraiser@{host}:{port}/app"},
        ready=("pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "app"),
    ),
    ServiceSpec(
        "redis", "redis:7", 6379, provides={"REDIS_HOST": "{host}", "REDIS_PORT": "{port}"}
    ),
    ServiceSpec(
        "qdrant", "qdrant/qdrant:latest", 6333, provides={"QDRANT_URL": "http://{host}:{port}"}
    ),
)

PROBE = r"""
import base64, json, os, socket, sys, urllib.parse, urllib.request

mode = sys.argv[1]
if mode == "redis":
    with socket.create_connection((os.environ["REDIS_HOST"], int(os.environ["REDIS_PORT"])), 5) as s:
        s.sendall(b"PING\r\n")
        reply = s.recv(64)
    print(reply)
    sys.exit(0 if reply.startswith(b"+PONG") else 1)
if mode == "postgres":  # SSLRequest: a live Postgres answers with one byte, S or N
    url = urllib.parse.urlsplit(os.environ["DATABASE_URL"])
    with socket.create_connection((url.hostname, url.port), 5) as s:
        s.sendall((8).to_bytes(4, "big") + (80877103).to_bytes(4, "big"))
        reply = s.recv(1)
    print(reply)
    sys.exit(0 if reply in (b"S", b"N") else 1)
if mode == "qdrant":  # urllib honours srt's NO_PROXY: loopback goes to the relay
    body = urllib.request.urlopen(os.environ["QDRANT_URL"] + "/collections", timeout=10).read()
    print(body)
    sys.exit(0 if json.loads(body)["status"] == "ok" else 1)
if mode == "direct":
    try:
        socket.create_connection(("127.0.0.1", int(sys.argv[2])), 5).close()
    except OSError as exc:
        print("refused:", exc)
        sys.exit(1)
    sys.exit(0)
if mode == "proxy":  # CONNECT through srt's proxy to a port that is not declared
    proxy = urllib.parse.urlsplit(os.environ["HTTP_PROXY"])
    auth = base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode()
    with socket.create_connection((proxy.hostname, proxy.port), 5) as s:
        s.sendall(
            f"CONNECT 127.0.0.1:{sys.argv[2]} HTTP/1.1\r\nHost: 127.0.0.1:{sys.argv[2]}\r\n"
            f"Proxy-Authorization: Basic {auth}\r\n\r\n".encode()
        )
        status = s.recv(128).split(b"\r\n")[0]
    print(status.decode())
    sys.exit(0 if b" 200" in status else 1)
if mode == "unix":
    try:
        s = socket.socket(socket.AF_UNIX)
        s.connect(sys.argv[2])
    except OSError as exc:
        print("unix:", exc)
        sys.exit(1)
    sys.exit(0)
"""


@pytest.fixture
def host_listener() -> Iterator[int]:
    """A TCP service on the host that no step may reach (it is not declared)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    stop = threading.Event()

    def serve() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
                conn.sendall(b"host-secret\n")
                conn.close()
            except OSError:
                continue

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield int(server.getsockname()[1])
    stop.set()
    thread.join(timeout=5)
    server.close()


def _volumes(container: str) -> list[str]:
    assert REAL_DOCKER is not None
    out = subprocess.run(
        [REAL_DOCKER, "inspect", "-f", "{{range .Mounts}}{{.Name}} {{end}}", container],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.split()


@needs_real_docker
@needs_real_srt
def test_real_services_are_reached_through_the_relays_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_listener: int
):
    assert REAL_DOCKER is not None and REAL_SRT is not None
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no real ~/.pi/agent in the settings
    (tmp_path / "home").mkdir()
    repo = _repo(tmp_path / "projects" / "repo")
    worktree = repo / ".helmraiser" / "worktrees" / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "sq/wt", str(worktree))
    run_dir = tmp_path / "data" / "runs" / "r"
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE)
    run_id = str(uuid.uuid4())
    events: list[tuple[str, dict[str, Any]]] = []
    started: list[RunningService] = []
    volumes: list[str] = []
    try:
        for spec in SPECS:
            service = start_service(
                run_id, spec, spec.env, lambda k, p: events.append((k, p)), docker=REAL_DOCKER
            )
            started.append(service)
            volumes += _volumes(service.container)
        ports = tuple(service.host_port for service in started)
        iso = ProcessIsolation({}, primary_checkout=repo)

        def wrap(argv: list[str]) -> list[str]:
            wrapped = iso.wrap_command(argv, worktree, run_dir, "verify", ports)
            wrapped[wrapped.index("--settings") - 1] = REAL_SRT
            return wrapped

        python = f"{shlex.quote(sys.executable)} {shlex.quote(str(probe))}"
        psql = shutil.which("psql")
        steps = [
            VerifyStep(
                "postgres",
                (
                    f"{psql} \"$DATABASE_URL?connect_timeout=5\" -Atc 'select 40 + 2'"
                    if psql
                    else f"{python} postgres"
                ),
            ),
            VerifyStep("redis", f"{python} redis"),
            VerifyStep("qdrant", f"{python} qdrant"),
            VerifyStep("undeclared-direct", f"{python} direct {host_listener}"),
            VerifyStep("undeclared-proxy", f"{python} proxy {host_listener}"),
            VerifyStep("unix-socket", f"{python} unix /run/docker.sock"),
            VerifyStep("inside", 'test "$SANDBOX_RUNTIME" = 1'),
        ]
        env = {key: value for service in started for key, value in service.provides.items()}
        results = {r.name: r for r in run_verify(steps, worktree, run_id, 40, env, wrap)}

        for name in ("postgres", "redis", "qdrant", "inside"):
            assert results[name].passed, f"{name}: {results[name].tail}"
            assert results[name].sandbox == "srt"
        if psql:
            assert results["postgres"].tail.splitlines()[0] == "42"
        for name in ("undeclared-direct", "undeclared-proxy", "unix-socket"):
            assert not results[name].passed, f"{name}: {results[name].tail}"
            assert "host-secret" not in results[name].tail
        assert "403" in results["undeclared-proxy"].tail
        assert "Operation not permitted" in results["unix-socket"].tail
        tails = "\n".join(r.tail for r in results.values())
        assert "proxyauth=" not in tails  # the relay argv never reaches the output
        assert [kind for kind, _ in events].count("service_ready") == 3
    finally:
        removed, error = stop_services(run_id, REAL_DOCKER)
    assert error is None and len(removed) == len(started) == 3
    assert run_containers(run_id, REAL_DOCKER) == []
    for volume in volumes:  # anonymous volumes went with their containers (rm -v)
        gone = subprocess.run([REAL_DOCKER, "volume", "inspect", volume], capture_output=True)
        assert gone.returncode != 0, volume
