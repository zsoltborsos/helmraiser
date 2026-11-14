"""The in-sandbox relay bootstrap (``isolation/relay.py``), run by the real bash with a
fake ``socat`` and a sample srt ``HTTP_PROXY``: the proxy parse, the relay argv, the
step's exit status and quoting, relay cleanup and the redaction of the credentials."""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from helmraiser.isolation import relay
from helmraiser.isolation.relay import RELAY_FAILED, relay_argv, relay_script

AUTH = "srt-user:s3cr%2Et.tok"  # the shape srt uses: user:token, token percent-encoded
PROXY = f"http://{AUTH}@localhost:3128"

pytestmark = pytest.mark.skipif(
    not Path("/proc/net/tcp").is_file(), reason="needs Linux /proc/net/tcp"
)

FAKE_SOCAT = """import json, os, signal, socket, sys, time
log = os.environ["FAKE_SOCAT_LOG"]
listen, target = sys.argv[1], sys.argv[2]
port = int(listen.split(",")[0].split(":")[1])
with open(log, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
auth = target.rpartition("proxyauth=")[2]
print(f"socat[1] E proxy auth {auth} rejected", file=sys.stderr, flush=True)
print("socat[1] W CONNECT 127.0.0.1:1: Forbidden", file=sys.stderr, flush=True)

def stop(*_):
    with open(log + ".stopped", "a") as f:
        f.write(f"{port}\\n")
    print("socat[1] W exiting on signal 15", file=sys.stderr, flush=True)
    sys.exit(0)

signal.signal(signal.SIGTERM, stop)
if not os.environ.get("FAKE_SOCAT_NO_LISTEN"):
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen()
while True:
    time.sleep(1)
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def socat(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "socat-bin"
    bin_dir.mkdir()
    source = tmp_path / "fake_socat.py"
    source.write_text(FAKE_SOCAT)
    wrapper = bin_dir / "socat"
    wrapper.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(source))} "$@"\n'
    )
    wrapper.chmod(0o755)
    return tmp_path / "socat.log"


def run(
    argv: list[str], log: Path, *, proxy: str | None = PROXY, **env: str
) -> subprocess.CompletedProcess[str]:
    base = {
        "PATH": f"{log.parent / 'socat-bin'}{os.pathsep}{os.environ['PATH']}",
        "FAKE_SOCAT_LOG": str(log),
        **env,
    }
    if proxy is not None:
        base["HTTP_PROXY"] = proxy
    return subprocess.run(
        argv, capture_output=True, text=True, env=base, timeout=60, stdin=subprocess.DEVNULL
    )


def test_relays_start_on_the_same_port_the_step_runs_and_its_status_is_kept(socat: Path):
    ports = (free_port(), free_port())
    step = ["/bin/bash", "-o", "pipefail", "-c", "echo step-output; false | true; exit 3"]

    proc = run(relay_argv(step, ports), socat)

    assert proc.returncode == 3, proc.stderr
    assert "step-output" in proc.stdout
    calls = [json.loads(line) for line in socat.read_text().splitlines()]
    assert sorted(calls) == sorted(
        [
            f"TCP-LISTEN:{port},bind=127.0.0.1,fork,reuseaddr",
            f"PROXY:localhost:127.0.0.1:{port},proxyport=3128,proxyauth={AUTH}",
        ]
        for port in ports
    )
    stopped = sorted(int(line) for line in Path(f"{socat}.stopped").read_text().split())
    assert stopped == sorted(ports)  # both relays were stopped after the step
    # Relay messages follow the step's output, never with the credentials.
    assert "[helmraiser relay] socat[1] W CONNECT 127.0.0.1:1: Forbidden" in proc.stderr
    assert "s3cr" not in proc.stdout + proc.stderr
    assert "exiting on signal" not in proc.stderr


def test_the_step_command_is_quoted_and_keeps_pipefail(socat: Path, tmp_path: Path):
    marker = tmp_path / "it's here.txt"
    command = f"echo \"$HOME\" '$HOME' `echo tick` > {shlex.quote(str(marker))}"
    step = ["/bin/bash", "-o", "pipefail", "-c", command]

    proc = run(relay_argv(step, [free_port()]), socat, HOME="/fake/home")

    assert proc.returncode == 0, proc.stderr
    assert marker.read_text() == "/fake/home $HOME tick\n"
    pipefail = ["/bin/bash", "-o", "pipefail", "-c", "false | true"]
    assert run(relay_argv(pipefail, [free_port()]), socat).returncode == 1


@pytest.mark.parametrize("proxy", [None, "http://localhost:3128", "socks5://u:p@localhost:1"])
def test_without_srts_authenticated_proxy_the_step_does_not_run(socat: Path, proxy: str | None):
    proc = run(relay_argv(["/bin/echo", "ran"], [free_port()]), socat, proxy=proxy)

    assert proc.returncode == RELAY_FAILED
    assert "ran" not in proc.stdout
    assert "[helmraiser] service relay: HTTP_PROXY is not srt's authenticated proxy" in proc.stderr
    assert not socat.exists()


def test_a_relay_that_never_listens_fails_the_step(socat: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(relay, "RELAY_WAIT_TRIES", 3)
    port = free_port()

    proc = run(relay_argv(["/bin/echo", "ran"], [port]), socat, FAKE_SOCAT_NO_LISTEN="1")

    assert proc.returncode == RELAY_FAILED
    assert f"relay for 127.0.0.1:{port} did not start" in proc.stderr
    assert "ran" not in proc.stdout and "s3cr" not in proc.stderr
    assert Path(f"{socat}.stopped").read_text().split() == [str(port)]


def test_missing_socat_fails_the_step(tmp_path: Path):
    env = {"PATH": "/nonexistent", "HTTP_PROXY": PROXY}
    proc = subprocess.run(
        relay_argv(["/bin/echo", "ran"], [5432]), capture_output=True, text=True, env=env
    )
    assert proc.returncode == RELAY_FAILED
    assert "socat not found inside the sandbox" in proc.stderr


def test_the_script_names_only_ports_and_the_quoted_step():
    script = relay_script(["/bin/bash", "-o", "pipefail", "-c", "pytest -q 'a b'"], [55432, 6333])

    assert "for __sq_port in 55432 6333; do" in script
    assert script.count("for __sq_port in 55432 6333; do") == 2
    assert "/bin/bash -o pipefail -c 'pytest -q '\"'\"'a b'\"'\"''" in script
    assert "HTTP_PROXY" in script and "3128" not in script  # parsed at runtime


@pytest.mark.parametrize("ports", [[], [0], [65536], ["5432"], [True]])
def test_bad_ports_are_refused(ports: list[object]):
    with pytest.raises(ValueError):
        relay_script(["true"], ports)  # type: ignore[arg-type]
