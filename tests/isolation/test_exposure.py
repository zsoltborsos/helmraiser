"""Exposure diagnostics: every route, positive controls, and no credential disclosure.

``Simulated`` stands in for srt and Docker. It runs the real probe program on the host
with the component's spec pointed away from the host targets, as isolation would, so
route attempts, listener evidence and controls are exercised for real.
"""

from __future__ import annotations

import base64
import functools
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, monitor
from helmraiser.isolation import exposure
from helmraiser.isolation.exposure import COMPONENTS, PROBE_ENV, PROBES, run_exposure
from helmraiser.isolation.process import ProcessIsolation
from helmraiser.task.model import project_slug
from tests.isolation.test_process_settings import REAL_SRT, needs_real_srt

SETTINGS_COMPONENT = {
    "srt-settings.json": "pi-session",
    "srt-settings-exposure-command.json": "command",
    "srt-settings-setup.json": "setup",
}
PROXY_USER = "proxy-user"
PROXY_PASSWORD = "proxy-password-never-print"


def _closed_port() -> socket.socket:
    """A bound, non-listening socket: connecting to its port is refused."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock


def component_of(argv: list[str]) -> str | None:
    if "--settings" in argv:
        index = argv.index("--settings")
        if Path(argv[index - 1]).name == "srt" or argv[index - 1] == REAL_SRT:
            return SETTINGS_COMPONENT[Path(argv[index + 1]).name]
    if Path(argv[0]).name == "docker" and argv[1] == "run":
        return "service-container"
    return None


@pytest.mark.parametrize("settings, component", SETTINGS_COMPONENT.items())
def test_component_of_accepts_absolute_srt(settings, component):
    for prefix in ([], ["/usr/bin/env", "CLAUDE_CODE_TMPDIR=/disk/temp"]):
        assert (
            component_of([*prefix, "/fake/bin/srt", "--settings", f"/run/{settings}"]) == component
        )


class Simulated:
    """Fake srt / Docker: the real probe runs on the host, its host targets redirected
    to nothing unless ``leak[component]`` names the probe; ``broken[component]`` names
    the controls redirected to nothing. Host runs (baseline, approved host setup) and,
    with ``real_srt``, srt itself run for real."""

    def __init__(
        self,
        *,
        leak: dict[str, set[str]] | None = None,
        broken: dict[str, set[str]] | None = None,
        proxy: str | None = None,
        malformed: set[str] | None = None,
        real_srt: str | None = None,
        tmp: Path,
    ):
        self.leak = leak or {}
        self.broken = broken or {}
        self.proxy = proxy
        self.malformed = malformed or set()
        self.real_srt = real_srt
        self.tmp = tmp
        self.calls: list[list[str]] = []
        self.specs: dict[str, dict] = {}
        self.settings: dict[str, dict] = {}
        self.envs: dict[str, dict[str, str]] = {}
        self._target = _closed_port()
        self._control = _closed_port()
        self.redirected = self._target.getsockname()[1]

    def close(self) -> None:
        self._target.close()
        self._control.close()

    def __call__(self, argv, cwd, env, timeout):  # type: ignore[no-untyped-def]
        self.calls.append(argv)
        if Path(argv[0]).name == "docker" and argv[1] in {"info", "rm"}:
            return subprocess.CompletedProcess(argv, 0, "", "")
        component = component_of(argv)
        spec = json.loads(env[PROBE_ENV])
        role = component or ("baseline" if spec["tcp"]["control"] is None else "host-setup")
        self.specs[role] = spec
        self.envs[role] = dict(env)
        if role in self.malformed:
            return subprocess.CompletedProcess(argv, 0, "not-json", "")
        if component is None:
            return exposure._execute(argv, cwd, env, timeout)
        if "--settings" in argv:
            index = argv.index("--settings")
            self.settings[component] = json.loads(Path(argv[index + 1]).read_text())
            if self.real_srt is not None:
                argv = list(argv)
                argv[index - 1] = self.real_srt
                return exposure._execute(argv, cwd, env, timeout)
        leak = self.leak.get(component, set())
        broken = self.broken.get(component, set())
        tcp = dict(spec["tcp"], gateway=False)
        tcp["hosts"] = [host for host in tcp["hosts"] if host != "host.docker.internal"]
        if "loopback" not in leak:
            tcp["target"] = self.redirected
        if "loopback" in broken and tcp["control"] is not None:
            tcp["control"] = self._control.getsockname()[1]
        ssh = dict(spec["ssh"])
        if component == "service-container":
            hostname = self.tmp / f"hostname-{len(self.calls)}"
            hostname.write_text(argv[argv.index("--hostname") + 1] + "\n")
            ssh["control"] = str(hostname)
        if "ssh" not in leak:
            # srt masks a denied directory with an empty tmpfs and a file with an empty one.
            masked = self.tmp / "masked"
            (masked / "dir").mkdir(parents=True, exist_ok=True)
            (masked / "file").touch()
            ssh["dir"], ssh["file"] = str(masked / "dir"), str(masked / "file")
        if "ssh" in broken:
            ssh["control"] = str(self.tmp / "absent")
        agent = dict(spec["agent"])
        if "ssh-agent" not in leak:
            agent["path"] = str(self.tmp / "absent.sock") if agent["path"] else ""
        child = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        if self.proxy is not None:
            child["ALL_PROXY"] = self.proxy
        child[PROBE_ENV] = json.dumps({**spec, "tcp": tcp, "ssh": ssh, "agent": agent})
        return exposure._execute(
            [sys.executable, "-c", exposure.PROBE_PROGRAM], cwd, child, timeout
        )


class MappingProxy:
    """An authenticating HTTP CONNECT or SOCKS5 proxy that only reaches ``port`` when
    asked for ``mapped``: a probe gets to the listener only through the proxy."""

    def __init__(self, kind: str, mapped: int, port: int):
        self.kind = kind
        self.mapped = mapped
        self.port = port
        self.server = socket.create_server(("127.0.0.1", 0))
        self.server.settimeout(0.2)
        self.running = True
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        scheme = "http" if self.kind == "http" else "socks5h"
        address = self.server.getsockname()
        return f"{scheme}://{PROXY_USER}:{PROXY_PASSWORD}@{address[0]}:{address[1]}"

    def close(self) -> None:
        self.running = False
        self.thread.join(timeout=2)
        self.server.close()

    def _serve(self) -> None:
        while self.running:
            try:
                client, _ = self.server.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        client.settimeout(2)
        try:
            port = self._http(client) if self.kind == "http" else self._socks(client)
            if port == self.mapped:
                socket.create_connection(("127.0.0.1", self.port), timeout=2).close()
                client.sendall(
                    b"HTTP/1.1 200 Connection Established\r\n\r\n"
                    if self.kind == "http"
                    else b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00"
                )
            elif port is not None:
                client.sendall(
                    b"HTTP/1.1 403 Forbidden\r\n\r\n"
                    if self.kind == "http"
                    else b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00"
                )
        except OSError:
            pass
        finally:
            client.close()

    def _http(self, client: socket.socket) -> int | None:
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = client.recv(1024)
            if not chunk:
                return None
            request += chunk
        line, *headers = request.decode().split("\r\n")
        token = base64.b64encode(f"{PROXY_USER}:{PROXY_PASSWORD}".encode()).decode()
        if not line.startswith("CONNECT ") or f"Proxy-Authorization: Basic {token}" not in headers:
            return None
        return int(line.split()[1].rsplit(":", 1)[1])

    def _socks(self, client: socket.socket) -> int | None:
        greeting = client.recv(2)
        if len(greeting) < 2 or greeting[0] != 5:
            return None
        if 2 not in client.recv(greeting[1]):
            return None
        client.sendall(b"\x05\x02")
        auth = client.recv(512)
        user = auth[2 : 2 + auth[1]]
        password = auth[3 + auth[1] :]
        if (user, password) != (PROXY_USER.encode(), PROXY_PASSWORD.encode()):
            client.sendall(b"\x01\x01")
            return None
        client.sendall(b"\x01\x00")
        request = client.recv(512)
        return int.from_bytes(request[-2:], "big")


@pytest.fixture(autouse=True)
def cache_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELMRAISER_DATA_DIR", str(tmp_path / "default-data"))


@pytest.fixture
def host_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    try:
        unix_socket = socket.socket(socket.AF_UNIX)
    except PermissionError:
        pytest.skip("sandbox forbids Unix sockets required by exposure controls")
    else:
        unix_socket.close()
    home = tmp_path / "home"
    ssh = home / ".ssh"
    ssh.mkdir(parents=True)
    (ssh / "id_fake_secret_name").write_text("credential-content-never-print")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "agent-secret-name.sock"))
    monkeypatch.delenv("SANDBOX_RUNTIME", raising=False)
    monkeypatch.setattr(ProcessIsolation, "check_available", lambda self: None)
    monkeypatch.setattr(exposure.shutil, "which", lambda name: f"/usr/bin/{name}")
    return home


@pytest.fixture
def simulated(tmp_path: Path):  # type: ignore[no-untyped-def]
    created: list[Simulated] = []

    def make(**kwargs) -> Simulated:  # type: ignore[no-untyped-def]
        fake = Simulated(tmp=tmp_path, **kwargs)
        created.append(fake)
        return fake

    yield make
    for fake in created:
        fake.close()


def rows(report, component: str | None = None, probe: str | None = None):  # type: ignore[no-untyped-def]
    return [
        row
        for row in report.results
        if (component is None or row.component == component)
        and (probe is None or row.probe == probe)
    ]


def test_exposure_all_blocked_with_controls_and_invocation_shapes(
    tmp_path: Path, host_targets: Path, simulated
) -> None:
    fake = simulated()

    report = run_exposure(tmp_path, {}, executor=fake)

    assert [(row.component, row.probe) for row in report.results] == [
        (component, probe) for component in COMPONENTS for probe in PROBES
    ]
    assert {(row.status, row.control) for row in report.results} == {("blocked", "ok")}
    assert report.ok and report.complete
    assert [row["control"] for row in report.as_dict()["results"]] == ["ok"] * 12
    docker = next(call for call in fake.calls if component_of(call) == "service-container")
    assert docker[1:3] == ["run", "--rm"]
    assert "--network" not in docker and "-v" not in docker
    assert docker[docker.index("--add-host") + 1] == "host.docker.internal:host-gateway"
    assert docker[docker.index("-p") + 1].startswith("127.0.0.1:")
    assert docker[docker.index("-e") + 1] == PROBE_ENV
    assert exposure.CONTAINER_IMAGE in docker
    assert any(call[1:3] == ["rm", "-f"] for call in fake.calls)


def test_exposure_srt_settings_allow_only_the_control_listener(
    tmp_path: Path, host_targets: Path, simulated
) -> None:
    fake = simulated()

    run_exposure(tmp_path, {}, executor=fake)

    for component in ("pi-session", "command", "setup"):
        spec = fake.specs[component]["tcp"]
        domains = fake.settings[component]["network"]["allowedDomains"]
        assert f"127.0.0.1:{spec['control']}" in domains
        assert not any(domain.endswith(f":{spec['target']}") for domain in domains)
        assert spec["hosts"] == ["127.0.0.1", "localhost", "::1"]
    # verify / evidence reach services through relays: the command probe runs behind one.
    command = next(call for call in fake.calls if component_of(call) == "command")
    assert "socat" in command[-1]


@pytest.mark.parametrize("kind", ["http", "socks5"])
def test_exposure_detects_a_listener_reached_only_through_a_proxy(
    tmp_path: Path, host_targets: Path, simulated, kind: str
) -> None:
    fake = simulated()
    proxy_box: list[MappingProxy] = []

    def executor(argv, cwd, env, timeout):  # type: ignore[no-untyped-def]
        if not proxy_box and component_of(argv) == "pi-session":
            target = json.loads(env[PROBE_ENV])["tcp"]["target"]
            proxy_box.append(MappingProxy(kind, fake.redirected, target))
            fake.proxy = proxy_box[0].url
        return fake(argv, cwd, env, timeout)

    try:
        report = run_exposure(tmp_path, {}, executor=executor)
    finally:
        for proxy in proxy_box:
            proxy.close()

    for component in ("pi-session", "command", "setup", "service-container"):
        assert [row.status for row in rows(report, component, "loopback")] == ["exposed"]
    assert not report.safe
    rendered = json.dumps(report.as_dict())
    assert PROXY_PASSWORD not in rendered and PROXY_USER not in rendered


def test_exposure_container_tries_its_own_loopback_and_the_host_gateway(
    tmp_path: Path, host_targets: Path, simulated
) -> None:
    fake = simulated(leak={"service-container": {"loopback"}})

    report = run_exposure(tmp_path, {}, executor=fake)

    spec = fake.specs["service-container"]["tcp"]
    assert spec["hosts"] == ["127.0.0.1", "localhost", "::1", "host.docker.internal"]
    assert spec["gateway"] is True
    assert [row.status for row in rows(report, "service-container", "loopback")] == ["exposed"]
    assert [row.status for row in rows(report, "pi-session", "loopback")] == ["blocked"]


@pytest.mark.parametrize("probe", ["loopback", "ssh"])
def test_exposure_failed_control_is_an_error_not_blocked(
    tmp_path: Path, host_targets: Path, simulated, probe: str
) -> None:
    fake = simulated(broken={"command": {probe}})

    report = run_exposure(tmp_path, {}, executor=fake)

    [row] = rows(report, "command", probe)
    assert (row.status, row.control) == ("error", "failed")
    assert "could not demonstrate detection" in row.reason
    assert {row.status for row in rows(report, "pi-session")} == {"blocked"}
    assert not report.safe


def test_exposure_failed_baseline_fails_the_agent_and_container_loopback_controls(
    tmp_path: Path, host_targets: Path, simulated
) -> None:
    fake = simulated(malformed={"baseline"})

    report = run_exposure(tmp_path, {}, executor=fake)

    assert {(row.status, row.control) for row in rows(report, probe="ssh-agent")} == {
        ("error", "failed")
    }
    [container] = rows(report, "service-container", "loopback")
    assert (container.status, container.control) == ("error", "failed")
    assert {row.status for row in rows(report, "setup", "loopback")} == {"blocked"}


def test_exposure_baseline_reaches_the_target_listener_and_a_test_agent_socket(
    tmp_path: Path, host_targets: Path, simulated
) -> None:
    fake = simulated()

    run_exposure(tmp_path, {}, executor=fake)

    baseline = fake.specs["baseline"]
    assert baseline["tcp"]["target"] == fake.specs["pi-session"]["tcp"]["target"]
    assert baseline["agent"]["path"] not in {"", os.environ["SSH_AUTH_SOCK"]}
    assert baseline["ssh"]["dir"] == baseline["ssh"]["file"] == ""


def test_exposure_leaks_are_exposed(tmp_path: Path, host_targets: Path, simulated) -> None:
    fake = simulated(leak={"setup": {"ssh"}, "pi-session": {"loopback"}})

    report = run_exposure(tmp_path, {}, executor=fake)

    assert [row.status for row in rows(report, "setup", "ssh")] == ["exposed"]
    assert [row.status for row in rows(report, "pi-session", "loopback")] == ["exposed"]
    assert not report.safe


def test_approved_host_setup_is_probed_unconfined(
    tmp_path: Path, host_targets: Path, simulated
) -> None:
    project = tmp_path / ".helmraiser"
    project.mkdir()
    (project / "project.toml").write_text(
        '[defaults]\nrequire_approval = true\n[setup]\nsandbox = "host"\n'
    )
    fake = simulated()

    report = run_exposure(tmp_path, {}, executor=fake)

    setup = rows(report, "setup")
    assert [row.status for row in setup[:2]] == ["exposed", "exposed"]
    assert setup[0].control == "ok"
    assert not report.safe
    assert "host-setup" in fake.specs
    assert not any("srt-settings-setup.json" in item for call in fake.calls for item in call)


def test_exposure_malformed_probe_output_is_an_error(
    tmp_path: Path, host_targets: Path, simulated
) -> None:
    fake = simulated(malformed={"pi-session", "command", "setup", "service-container"})

    report = run_exposure(tmp_path, {}, executor=fake)

    assert {row.status for row in report.results} == {"error"}
    assert not report.ok


def test_unavailable_components_are_incomplete_and_absent_agent_is_skipped(
    tmp_path: Path, host_targets: Path, monkeypatch: pytest.MonkeyPatch, simulated
) -> None:
    monkeypatch.setattr(ProcessIsolation, "check_available", lambda self: "srt unavailable")
    monkeypatch.setattr(exposure.shutil, "which", lambda name: None)
    monkeypatch.delenv("SSH_AUTH_SOCK")

    report = run_exposure(tmp_path, {}, executor=simulated())

    assert len(report.results) == 12
    assert {row.status for row in report.results} == {"skipped"}
    assert {row.control for row in report.results} == {"n/a"}
    assert not report.complete


def test_exposure_absent_agent_is_neutral_while_everything_else_is_blocked(
    tmp_path: Path,
    host_targets: Path,
    monkeypatch: pytest.MonkeyPatch,
    simulated,
    tmp_project: Path,
) -> None:
    monkeypatch.delenv("SSH_AUTH_SOCK")
    fake = simulated()

    report = run_exposure(tmp_path, {}, executor=fake)

    agent = rows(report, probe="ssh-agent")
    assert {(row.status, row.control, row.required) for row in agent} == {("skipped", "n/a", False)}
    others = [row for row in report.results if row.probe != "ssh-agent"]
    assert {(row.status, row.control) for row in others} == {("blocked", "ok")}
    assert report.complete and report.safe and report.ok

    monkeypatch.setattr(
        monitor, "run_exposure", functools.partial(run_exposure, executor=simulated())
    )
    result = CliRunner().invoke(
        app,
        ["doctor", "--exposure", str(tmp_project), "--data-dir", str(tmp_path / "data")],
    )
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert len(lines) == 12
    assert lines[0].startswith("pi-session loopback blocked [control ok]:")
    assert lines[2].startswith("pi-session ssh-agent skipped [control n/a]:")


def test_details_do_not_disclose_ssh_names_or_contents(
    tmp_path: Path, host_targets: Path, simulated
) -> None:
    report = run_exposure(tmp_path, {}, executor=simulated(leak={"command": {"ssh"}}))
    rendered = json.dumps(report.as_dict())
    assert "id_fake_secret_name" not in rendered
    assert "credential-content-never-print" not in rendered
    assert "agent-secret-name" not in rendered


@needs_real_srt
def test_real_srt_blocks_all_three_settings_with_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, simulated
) -> None:
    assert REAL_SRT is not None
    home = tmp_path / "home"
    ssh = home / ".ssh"
    ssh.mkdir(parents=True)
    (ssh / "id_fake").write_text("fake-key")
    agent_path = tmp_path / "agent.sock"
    agent = socket.socket(socket.AF_UNIX)
    agent.bind(str(agent_path))
    agent.listen()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SSH_AUTH_SOCK", str(agent_path))
    monkeypatch.delenv("SANDBOX_RUNTIME", raising=False)
    monkeypatch.setenv("PATH", f"{Path(REAL_SRT).parent}{os.pathsep}{os.environ['PATH']}")
    # The srt resolver shares this `shutil`, so it must still find the real srt.
    monkeypatch.setattr(
        exposure.shutil,
        "which",
        lambda name: REAL_SRT if name == "srt" else f"/usr/bin/{name}",
    )
    try:
        report = run_exposure(tmp_path, {}, executor=simulated(real_srt=REAL_SRT))
    finally:
        agent.close()

    srt_rows = [row for row in report.results if row.component != "service-container"]
    assert {(row.status, row.control) for row in srt_rows} == {("blocked", "ok")}, report.as_dict()


@pytest.mark.parametrize(
    "explicit,host,unavailable",
    [(True, False, False), (False, False, False), (True, True, False), (True, False, True)],
)
def test_exposure_setup_cache_without_sockets(tmp_path, monkeypatch, explicit, host, unavailable):
    class Socket:
        def getsockname(self):
            return ("127.0.0.1", 12345)

        def bind(self, address):
            pass

        def listen(self, backlog):
            pass

        def close(self):
            pass

    monkeypatch.setattr(exposure, "_target_listeners", lambda: [Socket()])
    monkeypatch.setattr(exposure, "_listen", lambda *args: Socket())
    monkeypatch.setattr(exposure.socket, "socket", lambda *args: Socket())
    monkeypatch.setattr(exposure, "_accepted", lambda _: 0)
    guard_srt = exposure.shutil.which("srt")
    env_path = exposure.shutil.which("env")
    monkeypatch.setattr(
        exposure.shutil, "which", lambda name: {"srt": guard_srt, "env": env_path}.get(name)
    )
    monkeypatch.setattr(
        ProcessIsolation, "check_available", lambda _: "unavailable" if unavailable else None
    )
    monkeypatch.delenv("SANDBOX_RUNTIME", raising=False)
    monkeypatch.delenv("npm_config_cache", raising=False)
    monkeypatch.delenv("PIP_CACHE_DIR", raising=False)
    repo = tmp_path / "repo"
    (repo / ".helmraiser").mkdir(parents=True)
    (repo / ".helmraiser" / "project.toml").write_text(
        '[defaults]\nrequire_approval = true\n[defaults.env]\nPIP_CACHE_DIR = "/project/pip"\n'
        '[setup]\nwrite = ["$PIP_CACHE_DIR"]\nallowed_domains = ["registry.example"]\n'
        + ('sandbox = "host"\n' if host else "")
    )
    data = tmp_path / ("explicit-data" if explicit else "default-data")
    (data / "setup-cache" / "other").mkdir(parents=True)
    (data / "ledger.db").touch()
    cache = data / "setup-cache" / project_slug(repo)
    if not host and not unavailable:
        # Existing caches must already be denied when pi/command sandbox settings are
        # generated, before this diagnostic prepares its setup sandbox settings.
        (cache / "npm").mkdir(parents=True)
        (cache / "npm" / "kept").write_text("cached")
    captured = {}

    def executor(argv, cwd, env, timeout):
        role = component_of(argv) or "host"
        captured[role] = (
            dict(env),
            (
                json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
                if role != "host"
                else None
            ),
        )
        payload = {
            "loopback": {"target": False, "attempts": 1, "proxies": 0},
            "ssh": {"target": False, "control": False},
            "ssh-agent": {"target": False},
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    run_exposure(repo, {}, executor=executor, **({"data_dir": data} if explicit else {}))
    cache = data / "setup-cache" / project_slug(repo)
    assert cache.exists() == (not host and not unavailable)
    if not host and not unavailable:
        env, srt_settings = captured["setup"]
        assert env["npm_config_cache"] == str(cache / "npm")
        assert env["PIP_CACHE_DIR"] == "/project/pip"
        fs = srt_settings["filesystem"]
        assert fs["allowWrite"] == [str(repo), "/tmp", "/project/pip", str(cache)]
        assert fs["allowRead"] == []
        assert not any(cache.is_relative_to(Path(p)) for p in fs["denyRead"])
        for denied in (data / "ledger.db", data / "setup-cache" / "other"):
            assert any(denied.is_relative_to(Path(p)) for p in fs["denyRead"])
        assert srt_settings["network"]["allowedDomains"] == ["registry.example", "127.0.0.1:12345"]
    for role in ("pi-session", "command"):
        if role in captured:
            env, srt_settings = captured[role]
            assert "npm_config_cache" not in env and "PIP_CACHE_DIR" not in env
            fs = srt_settings["filesystem"]
            assert str(cache) not in fs["allowWrite"]
            for denied in (cache, data / "setup-cache" / "other", data / "ledger.db"):
                assert any(denied.is_relative_to(Path(p)) for p in fs["denyRead"])
    if not host and not unavailable:
        assert (cache / "npm" / "kept").read_text() == "cached"
    if explicit:
        assert not (tmp_path / "default-data" / "setup-cache").exists()
