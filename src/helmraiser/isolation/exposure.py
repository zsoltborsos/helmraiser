"""Host-exposure diagnostics for the policies used by a Helmraiser run.

The model of the thing to protect is a TCP listener bound to the host's loopback only:
``ssh -L <port>:<host>:<port>`` binds ``localhost`` (127.0.0.1 and ::1) unless
``GatewayPorts`` or an explicit bind address says otherwise, so a production tunnel is
reachable by anything that can open a connection on the host's loopback, and by nothing
else. The diagnostic opens such a listener on a random port (127.0.0.1, plus ::1 on the
same port when the host has IPv6 loopback) and asks each run component to reach it.

Every route a process in a component could use is tried, in parallel:

- a direct connect to 127.0.0.1, ``localhost`` and ::1 (inside srt and a container these
  are the component's own network namespace);
- for each proxy in the component's environment (every ``*proxy*`` variable except
  ``NO_PROXY``, read inside the sandbox; srt sets an HTTP proxy and a SOCKS5 proxy, with
  credentials): HTTP ``CONNECT``, an absolute-form HTTP ``GET`` and SOCKS5 ``CONNECT`` to
  each of those hosts;
- in a service container, also ``host.docker.internal`` and the container's default
  gateway (the bridge address of the host).

The listener is the ground truth: a connection waiting in its backlog after a
component's probe is exposure, whatever the probe itself concluded (a proxy may connect
after the probe gave up). A probe's own successful connect also counts.

A ``blocked`` row is only as good as the probe's ability to see access, so every probe
has a positive control that must succeed, or the row is ``error`` ("probe could not
demonstrate detection"), never ``blocked``:

- loopback, srt components: a second loopback listener that the component's settings
  explicitly allows (an ``allowedDomains`` entry; for the command sandbox settings a service port
  behind ``isolation/relay.py``, as verify uses one) must receive a connection.
- loopback, service container: host firewalls commonly drop container traffic to host
  ports, so no host listener is reliably reachable from a container; the control is the
  same probe run on the host, outside any sandbox, which must reach the target listener.
- ``~/.ssh``: the probe must read a token file the component may read (a file in the
  diagnostic's temporary directory; in a container, ``/etc/hostname``, set with
  ``--hostname``).
- SSH agent: srt refuses every Unix socket, so the control is the probe run on the host,
  which must connect to a test Unix socket.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from helmraiser.isolation.environment import command_environment, session_environment
from helmraiser.isolation.process import ProcessIsolation
from helmraiser.isolation.setup import prepare_setup
from helmraiser.ledger.db import default_data_dir
from helmraiser.loop.services import free_port
from helmraiser.task.project import expand_env, load_project_defaults

COMPONENTS: tuple[str, ...] = ("pi-session", "command", "setup", "service-container")
PROBES: tuple[str, ...] = ("loopback", "ssh", "ssh-agent")
CONTAINER_IMAGE = "python:3.12.7-alpine3.20"
PROBE_TIMEOUT = 30
CONTAINER_TIMEOUT = 300  # docker run may pull the image first
# The probe's instructions (JSON); passed through srt, the relays and ``docker run -e``.
PROBE_ENV = "HELMRAISER_EXPOSURE_PROBE"
LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
NOT_DEMONSTRATED = "probe could not demonstrate detection"

Executor = Callable[[list[str], Path, Mapping[str, str], int], subprocess.CompletedProcess[str]]

# An expected denial is an OSError (False). Anything else means the diagnostic itself
# broke (None), which the host reports as an error, never as blocked.
PROBE_PROGRAM = r"""
import base64, json, os, socket
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlsplit

SPEC = json.loads(os.environ["HELMRAISER_EXPOSURE_PROBE"])
WAIT = 2.0
TOKEN = b"helmraiser-exposure\n"

def attempt(fn, *args):
    try:
        return bool(fn(*args))
    except OSError:
        return False
    except Exception:
        return None

def combine(values):
    return None if None in values else any(values)

def proxies():
    found = {}
    for name, value in os.environ.items():
        if "proxy" not in name.lower() or name.lower().endswith("no_proxy"):
            continue
        for item in value.split(","):
            item = item.strip()
            try:
                parts = urlsplit(item if "://" in item else "//" + item)
                host, port = parts.hostname, parts.port
            except ValueError:
                continue
            if not host or not port:
                continue
            creds = None
            if parts.username is not None:
                creds = (unquote(parts.username), unquote(parts.password or ""))
            found[(host, port, creds)] = None
    return list(found)

def gateways():
    found = []
    try:
        with open("/proc/net/route") as stream:
            for line in list(stream)[1:]:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000" and fields[2] != "00000000":
                    found.append(socket.inet_ntoa(bytes.fromhex(fields[2])[::-1]))
    except OSError:
        pass
    return found

def endpoint(host, port):
    return socket.create_connection((host, port), timeout=WAIT)

def authority(host, port):
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"

def receive(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data

def status_line(sock):
    data = b""
    while not data.endswith(b"\r\n"):
        if len(data) > 4096:
            raise ConnectionError("no status line")
        data += receive(sock, 1)
    return data.split()

def basic(creds):
    if creds is None:
        return ""
    raw = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
    return f"Proxy-Authorization: Basic {raw}\r\n"

def direct(host, port):
    with endpoint(host, port) as sock:
        sock.sendall(TOKEN)
    return True

def http_connect(proxy, host, port):
    target = authority(host, port)
    with endpoint(proxy[0], proxy[1]) as sock:
        request = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n{basic(proxy[2])}\r\n"
        sock.sendall(request.encode())
        status = status_line(sock)
        if len(status) < 2 or status[1] != b"200":
            return False
        sock.sendall(TOKEN)
    return True

def http_forward(proxy, host, port):
    target = authority(host, port)
    with endpoint(proxy[0], proxy[1]) as sock:
        request = (
            f"GET http://{target}/helmraiser-exposure HTTP/1.1\r\nHost: {target}\r\n"
            f"{basic(proxy[2])}Connection: close\r\n\r\n"
        )
        sock.sendall(request.encode())
        status_line(sock)
    # The listener never answers: only its backlog shows a forwarded request.
    return False

def socks5(proxy, host, port):
    creds = proxy[2]
    with endpoint(proxy[0], proxy[1]) as sock:
        sock.sendall(b"\x05\x02\x00\x02" if creds else b"\x05\x01\x00")
        version, method = receive(sock, 2)
        if version != 5:
            return False
        if method == 2 and creds:
            # srt puts the encoded command into the user name; it accepts any suffix.
            user, password = (part.encode()[:255] for part in creds)
            sock.sendall(bytes([1, len(user)]) + user + bytes([len(password)]) + password)
            if receive(sock, 2)[1] != 0:
                return False
        elif method != 0:
            return False
        try:
            address = b"\x01" + socket.inet_pton(socket.AF_INET, host)
        except OSError:
            try:
                address = b"\x04" + socket.inet_pton(socket.AF_INET6, host)
            except OSError:
                address = b"\x03" + bytes([len(host)]) + host.encode()
        sock.sendall(b"\x05\x01\x00" + address + port.to_bytes(2, "big"))
        if receive(sock, 2)[1] != 0:
            return False
        sock.sendall(TOKEN)
    return True

def loopback():
    tcp = SPEC["tcp"]
    hosts = list(tcp["hosts"]) + (gateways() if tcp["gateway"] else [])
    found = proxies()
    ports = [port for port in (tcp["target"], tcp["control"]) if port]
    jobs = []
    for port in ports:
        for host in hosts:
            jobs.append((port, direct, host, port))
            for proxy in found:
                for route in (http_connect, http_forward, socks5):
                    jobs.append((port, route, proxy, host, port))
    with ThreadPoolExecutor(max_workers=64) as pool:
        results = list(pool.map(lambda job: (job[0], attempt(*job[1:])), jobs))
    target = combine([ok for port, ok in results if port == tcp["target"]])
    return {"target": target, "attempts": len(jobs), "proxies": len(found)}

# srt masks a denied directory with an empty tmpfs and a denied file with an empty
# one, so only entries or bytes are access.
def listing(path):
    return len(os.listdir(path)) > 0

def reading(path):
    with open(path, "rb") as stream:
        return len(stream.read(1)) > 0

def control_read(path):
    if not path:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            return stream.read(256).strip()
    except OSError:
        return False

def ssh():
    spec = SPEC["ssh"]
    listed = attempt(listing, spec["dir"]) if spec["dir"] else False
    read = attempt(reading, spec["file"]) if spec["file"] else False
    try:
        control = control_read(spec["control"])
    except Exception:
        control = None
    return {"target": combine([listed, read]), "control": control}

def connect_unix(path):
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.settimeout(WAIT)
        sock.connect(path)
    finally:
        sock.close()
    return True

def agent():
    path = SPEC["agent"]["path"]
    return {"target": attempt(connect_unix, path) if path else False}

print(json.dumps({"loopback": loopback(), "ssh": ssh(), "ssh-agent": agent()}))
""".strip()


@dataclass(frozen=True)
class ExposureResult:
    component: str
    probe: str
    status: str
    reason: str
    required: bool = True
    # The positive control: ok, failed, or n/a when no probe ran.
    control: str = "n/a"

    def as_dict(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if key != "required"}


@dataclass(frozen=True)
class ExposureReport:
    repo: str
    results: tuple[ExposureResult, ...]

    @property
    def complete(self) -> bool:
        return not any(result.status == "skipped" and result.required for result in self.results)

    @property
    def safe(self) -> bool:
        return not any(result.status in {"exposed", "error"} for result in self.results)

    @property
    def ok(self) -> bool:
        return self.complete and self.safe

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "ok": self.ok,
            "complete": self.complete,
            "results": [result.as_dict() for result in self.results],
        }


def _execute(
    argv: list[str], cwd: Path, env: Mapping[str, str], timeout: int
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=dict(env),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 125, "", str(exc))


def _ssh_target(home: Path) -> tuple[Path | None, Path | None]:
    directory = home / ".ssh"
    if not directory.is_dir():
        return None, None
    try:
        files = sorted(path for path in directory.rglob("*") if path.is_file())
        # Non-empty: inside srt a denied file reads as empty, so bytes mean access.
        readable = next(
            (path for path in files if os.access(path, os.R_OK) and path.stat().st_size > 0),
            None,
        )
    except OSError:
        readable = None
    return directory, readable


def _rows_for_failure(component: str, status: str, reason: str) -> list[ExposureResult]:
    return [ExposureResult(component, probe, status, reason) for probe in PROBES]


def _tristate(value: object) -> bool:
    return value is None or isinstance(value, bool)


def _payload(proc: subprocess.CompletedProcess[str]) -> dict[str, Any] | str:
    """The probe's findings, or why there are none."""
    if proc.returncode != 0:
        return f"probe process failed (exit {proc.returncode})"
    try:
        payload = json.loads(proc.stdout)
    except (TypeError, json.JSONDecodeError):
        return "probe returned malformed output"
    try:
        tcp, ssh, agent = payload["loopback"], payload["ssh"], payload["ssh-agent"]
        valid = (
            _tristate(tcp["target"])
            and isinstance(tcp["attempts"], int)
            and isinstance(tcp["proxies"], int)
            and _tristate(ssh["target"])
            and (_tristate(ssh["control"]) or isinstance(ssh["control"], str))
            and _tristate(agent["target"])
        )
    except (KeyError, TypeError):
        valid = False
    return payload if valid else "probe returned malformed output"


def _decide(
    component: str, probe: str, target: object, control: bool, blocked: str
) -> ExposureResult:
    """Exposure wins; otherwise a failed control turns blocked into error."""
    marker = "ok" if control else "failed"
    if target is None:
        return ExposureResult(
            component, probe, "error", "probe raised an unexpected error", True, marker
        )
    if target:
        return ExposureResult(component, probe, "exposed", "host access succeeded", True, marker)
    if not control:
        return ExposureResult(component, probe, "error", NOT_DEMONSTRATED, True, "failed")
    return ExposureResult(component, probe, "blocked", blocked, True, "ok")


def _component_rows(
    component: str,
    proc: subprocess.CompletedProcess[str],
    *,
    target_hits: int,
    loopback_control: bool,
    ssh_token: str,
    agent_control: bool,
    ssh_available: bool,
    agent_available: bool,
) -> list[ExposureResult]:
    payload = _payload(proc)
    if isinstance(payload, str):
        return _rows_for_failure(component, "error", payload)
    tcp = payload["loopback"]
    target = True if target_hits else tcp["target"]
    routes = f"{tcp['attempts']} attempts, {tcp['proxies']} proxies"
    rows = [
        _decide(
            component,
            "loopback",
            target,
            loopback_control,
            f"no route reached the host loopback listener ({routes})",
        )
    ]
    if ssh_available:
        control = payload["ssh"]["control"] == ssh_token
        rows.append(
            _decide(
                component, "ssh", payload["ssh"]["target"], control, "host ~/.ssh was unreadable"
            )
        )
    else:
        rows.append(ExposureResult(component, "ssh", "skipped", "host ~/.ssh is absent", False))
    if agent_available:
        rows.append(
            _decide(
                component,
                "ssh-agent",
                payload["ssh-agent"]["target"],
                agent_control,
                "host SSH agent was unreachable",
            )
        )
    else:
        rows.append(
            ExposureResult(component, "ssh-agent", "skipped", "host SSH agent is absent", False)
        )
    return rows


def _spec(
    target: int,
    control: int | None,
    *,
    ssh_dir: Path | None,
    ssh_file: Path | None,
    ssh_control: str,
    agent: str | None,
    hosts: tuple[str, ...] = LOOPBACK_HOSTS,
    gateway: bool = False,
) -> str:
    return json.dumps(
        {
            "tcp": {"hosts": list(hosts), "target": target, "control": control, "gateway": gateway},
            "ssh": {"dir": str(ssh_dir or ""), "file": str(ssh_file or ""), "control": ssh_control},
            "agent": {"path": agent or ""},
        }
    )


def _listen(family: socket.AddressFamily, host: str, port: int = 0) -> socket.socket:
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((host, port))
        sock.listen(128)
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    return sock


def _target_listeners() -> list[socket.socket]:
    """127.0.0.1:<random port>, and ::1 on the same port where the host has it."""
    first = _listen(socket.AF_INET, "127.0.0.1")
    try:
        return [first, _listen(socket.AF_INET6, "::1", first.getsockname()[1])]
    except OSError:
        return [first]


def _accepted(listeners: list[socket.socket]) -> int:
    """Count (and close) the connections waiting in the listeners' backlogs."""
    count = 0
    for listener in listeners:
        while True:
            try:
                connection, _ = listener.accept()
            except (BlockingIOError, InterruptedError):
                break
            connection.close()
            count += 1
    return count


def run_exposure(
    repo: Path,
    config: dict[str, Any],
    *,
    executor: Executor = _execute,
    data_dir: Path | None = None,
) -> ExposureReport:
    """Probe host loopback and credentials through every process-isolated component."""
    repo = repo.resolve()
    defaults = load_project_defaults(repo)
    data_dir = (data_dir or default_data_dir()).expanduser().resolve()
    isolation = ProcessIsolation(
        config, deny_read=defaults.deny_read, primary_checkout=repo, data_dir=data_dir
    )
    home = Path(os.environ.get("HOME", str(Path.home())))
    ssh_dir, ssh_file = _ssh_target(home)
    agent = os.environ.get("SSH_AUTH_SOCK")
    token = uuid.uuid4().hex
    targets = _target_listeners()
    port = targets[0].getsockname()[1]
    control = _listen(socket.AF_INET, "127.0.0.1")
    control_port = control.getsockname()[1]
    control_domain = f"127.0.0.1:{control_port}"
    test_agent = socket.socket(socket.AF_UNIX)
    rows: list[ExposureResult] = []
    probe_argv = [sys.executable, "-c", PROBE_PROGRAM]

    def probe(
        argv: list[str], env: Mapping[str, str], timeout: int = PROBE_TIMEOUT
    ) -> tuple[subprocess.CompletedProcess[str], int, int]:
        """Run one probe; return it with the target and control listeners' connections."""
        proc = executor(argv, repo, env, timeout)
        return proc, _accepted(targets), _accepted([control])

    try:
        with tempfile.TemporaryDirectory(prefix="helmraiser-exposure-") as temporary:
            run_dir = Path(temporary)
            control_file = run_dir / "control" / "token"
            control_file.parent.mkdir()
            control_file.write_text(token + "\n")
            agent_path = run_dir / "agent.sock"
            test_agent.bind(str(agent_path))
            test_agent.listen(4)

            # Baseline: the same probe on the host, outside any sandbox, must reach the
            # target listener and a test Unix socket.
            baseline_env = _spec(
                port, None, ssh_dir=None, ssh_file=None, ssh_control="", agent=str(agent_path)
            )
            proc, target_hits, _ = probe(probe_argv, command_environment({PROBE_ENV: baseline_env}))
            payload = _payload(proc)
            baseline_tcp = target_hits > 0
            agent_control = not isinstance(payload, str) and payload["ssh-agent"]["target"] is True

            def spec(allowed: int | None) -> dict[str, str]:
                return {
                    PROBE_ENV: _spec(
                        port,
                        allowed,
                        ssh_dir=ssh_dir,
                        ssh_file=ssh_file,
                        ssh_control=str(control_file),
                        agent=agent,
                    )
                }

            def component(name: str, argv: list[str], env: Mapping[str, str]) -> None:
                proc, target_hits, control_hits = probe(argv, env)
                rows.extend(
                    _component_rows(
                        name,
                        proc,
                        target_hits=target_hits,
                        loopback_control=control_hits > 0,
                        ssh_token=token,
                        agent_control=agent_control,
                        ssh_available=ssh_dir is not None,
                        agent_available=agent is not None,
                    )
                )

            unavailable = (
                "nested sandbox cannot establish host isolation"
                if os.environ.get("SANDBOX_RUNTIME")
                else isolation.check_available()
            )
            if unavailable is not None:
                for name in COMPONENTS[:2]:
                    rows.extend(_rows_for_failure(name, "skipped", unavailable))
            else:
                session = ProcessIsolation(
                    config, deny_read=defaults.deny_read, primary_checkout=repo, data_dir=data_dir
                )
                session.allowed_domains = (*session.allowed_domains, control_domain)
                component(
                    "pi-session",
                    session.wrap(probe_argv, repo, run_dir),
                    session_environment(spec(control_port)),
                )
                # The command sandbox settings reach their control as verify reaches a service.
                component(
                    "command",
                    isolation.wrap_command(
                        probe_argv, repo, run_dir, "exposure-command", (control_port,)
                    ),
                    command_environment(spec(control_port)),
                )

            setup_env = command_environment(spec(control_port))
            if defaults.setup_config.sandbox == "host":
                component("setup", probe_argv, setup_env)
            elif unavailable is not None:
                rows.extend(_rows_for_failure("setup", "skipped", unavailable))
            else:
                setup_env.update(expand_env(defaults.env, setup_env))
                setup_env, write, cache = prepare_setup(
                    repo, data_dir, defaults.setup_config, setup_env
                )
                setup_argv = isolation.wrap_setup(
                    probe_argv,
                    repo,
                    run_dir,
                    write,
                    (*defaults.setup_config.allowed_domains, control_domain),
                    setup_cache=cache,
                )
                component("setup", setup_argv, setup_env)

            docker = shutil.which("docker")
            if docker is None:
                rows.extend(
                    _rows_for_failure("service-container", "skipped", "docker not found on PATH")
                )
            elif executor([docker, "info"], repo, os.environ, PROBE_TIMEOUT).returncode != 0:
                rows.extend(
                    _rows_for_failure(
                        "service-container", "skipped", "docker daemon is unavailable"
                    )
                )
            else:
                container = f"helmraiser-exposure-{uuid.uuid4().hex[:12]}"
                hostname = f"sq-{token[:24]}"
                container_spec = _spec(
                    port,
                    None,
                    ssh_dir=ssh_dir,
                    ssh_file=ssh_file,
                    ssh_control="/etc/hostname",
                    agent=agent,
                    hosts=(*LOOPBACK_HOSTS, "host.docker.internal"),
                    gateway=True,
                )
                argv = [
                    docker,
                    "run",
                    "--rm",
                    "--name",
                    container,
                    "--hostname",
                    hostname,
                    "--add-host",
                    "host.docker.internal:host-gateway",
                    "-p",
                    f"127.0.0.1:{free_port()}:49152",
                    "-e",
                    PROBE_ENV,
                    CONTAINER_IMAGE,
                    "python3",
                    "-c",
                    PROBE_PROGRAM,
                ]
                try:
                    proc, target_hits, _ = probe(
                        argv, {**os.environ, PROBE_ENV: container_spec}, CONTAINER_TIMEOUT
                    )
                    rows.extend(
                        _component_rows(
                            "service-container",
                            proc,
                            target_hits=target_hits,
                            loopback_control=baseline_tcp,
                            ssh_token=hostname,
                            agent_control=agent_control,
                            ssh_available=ssh_dir is not None,
                            agent_available=agent is not None,
                        )
                    )
                finally:
                    executor([docker, "rm", "-f", container], repo, os.environ, PROBE_TIMEOUT)
    finally:
        for listener in (*targets, control, test_agent):
            listener.close()
    ordered = sorted(
        rows,
        key=lambda row: (COMPONENTS.index(row.component), PROBES.index(row.probe)),
    )
    return ExposureReport(str(repo), tuple(ordered))
