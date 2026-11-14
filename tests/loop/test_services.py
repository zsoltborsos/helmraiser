"""Project services (``loop/services.py``) against the fake docker: the ``docker run``
argv, the port read-back, readiness, cleanup by label and orphan detection."""

from __future__ import annotations

import shlex
import socket
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from helmraiser.ledger.db import Ledger
from helmraiser.loop import services
from helmraiser.loop.reconcile import orphaned_services, remove_orphaned_services
from helmraiser.loop.services import (
    RunningService,
    ServiceError,
    _last_line,
    docker_ready,
    labelled_containers,
    port_ready,
    start_service,
    stop_services,
    wait_ready,
)
from helmraiser.task.project import ServiceSpec
from tests.conftest import FakeDocker
from tests.ledger.test_db import make_spec, run_row

RUN_ID = "1a2b3c4d-0000-4000-8000-000000000001"
POSTGRES = ServiceSpec(
    name="postgres",
    image="postgres:16",
    port=5432,
    env={"POSTGRES_PASSWORD": "helmraiser"},
    command=("postgres", "-c", "fsync=off"),
    provides={"DATABASE_URL": "postgresql://postgres:helmraiser@{host}:{port}/app"},
)


class Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        self.items.append((kind, payload))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.items]


def ready(port: int) -> bool:
    return True


def test_start_publishes_on_loopback_only_with_labels_and_env_names(fake_docker: FakeDocker):
    events = Events()

    service = start_service(
        RUN_ID, POSTGRES, {"POSTGRES_PASSWORD": "helmraiser"}, events, probe=ready
    )

    run_call, port_call = fake_docker.calls()
    argv = run_call["argv"]
    publish = argv[argv.index("-p") + 1]
    host, host_port, port = publish.split(":")
    assert argv == [
        "run",
        "-d",
        "--name",
        "helmraiser-1a2b3c4d-postgres",
        "--label",
        f"helmraiser.run={RUN_ID}",
        "--label",
        "helmraiser.service=postgres",
        "-p",
        publish,
        "-e",
        "POSTGRES_PASSWORD",
        "postgres:16",
        "postgres",
        "-c",
        "fsync=off",
    ]
    assert (host, port) == ("127.0.0.1", "5432") and 1024 <= int(host_port) <= 65535
    assert run_call["env"] == {"POSTGRES_PASSWORD": "helmraiser"}  # from the CLI's env
    assert not any(arg.endswith("=helmraiser") for arg in argv)  # the value is not in argv
    assert port_call["argv"] == ["port", "helmraiser-1a2b3c4d-postgres", "5432/tcp"]
    assert service == RunningService(
        "postgres",
        "postgres:16",
        "helmraiser-1a2b3c4d-postgres",
        5432,
        int(host_port),
        {"DATABASE_URL": f"postgresql://postgres:helmraiser@127.0.0.1:{host_port}/app"},
    )
    assert events.kinds() == ["service_start", "service_ready"]
    start = events.items[0][1]
    assert start["host_port"] == int(host_port) and start["env"] == ["POSTGRES_PASSWORD"]
    assert start["provides"] == ["DATABASE_URL"]  # names only
    assert "helmraiser@" not in str(events.items)


def test_a_lost_port_is_retried_and_the_created_container_removed(fake_docker: FakeDocker):
    fake_docker.write_script(
        {"run_fail": {"postgres": ["Bind for 127.0.0.1:1: failed: port is already allocated"]}}
    )

    service = start_service(RUN_ID, POSTGRES, {}, Events(), probe=ready)

    commands = [call["argv"][0] for call in fake_docker.calls()]
    assert commands == ["run", "rm", "run", "port"]
    assert list(fake_docker.containers().values())[0]["name"] == service.container
    assert len(fake_docker.containers()) == 1


def test_a_fixed_host_port_is_published_as_given(fake_docker: FakeDocker):
    spec = replace(POSTGRES, host_port=55440)

    service = start_service(RUN_ID, spec, {}, Events(), probe=ready)

    run = fake_docker.calls()[0]["argv"]
    assert "127.0.0.1:55440:5432" in run
    assert service.host_port == 55440
    assert service.provides["DATABASE_URL"].endswith("@127.0.0.1:55440/app")


def test_a_taken_fixed_host_port_fails_without_retry(fake_docker: FakeDocker):
    fake_docker.write_script(
        {"run_fail": {"postgres": ["Bind for 127.0.0.1:55440: failed: port is already allocated"]}}
    )

    with pytest.raises(ServiceError) as info:
        start_service(RUN_ID, replace(POSTGRES, host_port=55440), {}, Events(), probe=ready)

    assert "host_port 55440 is in use; stop whatever holds it" in str(info.value)
    assert [call["argv"][0] for call in fake_docker.calls()] == ["run", "rm", "ps"]


# What ``docker run`` really prints: the error, a blank line, then a usage footer.
PORT_TAKEN_STDERR = (
    "docker: Error response from daemon: failed to set up container networking: driver "
    "failed programming external connectivity on endpoint x: Bind for 127.0.0.1:55440 "
    "failed: port is already allocated\n"
    "\n"
    "Run 'docker run --help' for more information"
)


def fake_ss(fake_docker: FakeDocker, output: str = "", *, exit_code: int = 0) -> Path:
    """A scripted ``ss`` beside the fake docker; returns the file logging its arguments."""
    log = fake_docker.bin.parent / "ss.log"
    ss = fake_docker.bin.parent / "ss"
    ss.write_text(
        f'#!/bin/sh\necho "$*" >> {shlex.quote(str(log))}\n'
        f"printf '%s\\n' {shlex.quote(output)}\nexit {exit_code}\n"
    )
    ss.chmod(0o755)
    return log


def test_docker_usage_footer_does_not_hide_the_real_error(fake_docker: FakeDocker):
    fake_docker.write_script(
        {
            "run_fail": {
                "postgres": "docker: Error response from daemon: pull access denied\n"
                "\n"
                "Run 'docker run --help' for more information"
            }
        }
    )

    with pytest.raises(ServiceError) as info:
        start_service(RUN_ID, POSTGRES, {}, Events(), probe=ready)

    assert str(info.value) == (
        "services: postgres: docker run failed: "
        "docker: Error response from daemon: pull access denied"
    )
    assert [call["argv"][0] for call in fake_docker.calls()] == ["run", "rm"]


def test_docker_usage_footer_alone_falls_back_to_the_exit_status():
    proc = subprocess.CompletedProcess(
        ["docker"], 125, "", "\nRun 'docker run --help' for more information\n"
    )

    assert _last_line(proc) == "exit status 125"


def test_real_port_taken_stderr_retries_on_a_free_port(fake_docker: FakeDocker):
    fake_docker.write_script({"run_fail": {"postgres": [PORT_TAKEN_STDERR]}})

    service = start_service(RUN_ID, POSTGRES, {}, Events(), probe=ready)

    assert [call["argv"][0] for call in fake_docker.calls()] == ["run", "rm", "run", "port"]
    assert list(fake_docker.containers().values())[0]["name"] == service.container


def test_real_port_taken_stderr_exhausts_the_free_port_attempts(fake_docker: FakeDocker):
    fake_docker.write_script({"run_fail": {"postgres": [PORT_TAKEN_STDERR] * 3}})

    with pytest.raises(ServiceError) as info:
        start_service(RUN_ID, POSTGRES, {}, Events(), probe=ready)

    assert str(info.value).endswith("failed: port is already allocated")
    assert [call["argv"][0] for call in fake_docker.calls()].count("run") == 3


def test_a_taken_fixed_host_port_names_the_container_holding_it(fake_docker: FakeDocker):
    log = fake_ss(fake_docker, "unused")
    fake_docker.write_script(
        {
            "run_fail": {"postgres": PORT_TAKEN_STDERR},
            "published": {"55440": ["example-test-postgres"]},
        }
    )

    with pytest.raises(ServiceError) as info:
        start_service(RUN_ID, replace(POSTGRES, host_port=55440), {}, Events(), probe=ready)

    assert str(info.value).endswith(
        "failed: port is already allocated "
        "(host_port 55440 is in use by container example-test-postgres; stop it)"
    )
    assert fake_docker.calls()[-1]["argv"] == [
        "ps",
        "--filter",
        "publish=55440",
        "--format",
        "{{.Names}}",
    ]
    assert not log.exists()


@pytest.mark.parametrize("docker_answers", [True, False])
def test_a_taken_fixed_host_port_names_the_process_listening_on_it(
    fake_docker: FakeDocker, docker_answers: bool
):
    log = fake_ss(
        fake_docker,
        'LISTEN 0 4096 127.0.0.1:55440 0.0.0.0:* users:(("postgres",pid=4242,fd=7))',
    )
    fake_docker.write_script(
        {
            "run_fail": {"postgres": PORT_TAKEN_STDERR},
            **({} if docker_answers else {"ps_publish_fail": "Cannot connect"}),
        }
    )

    with pytest.raises(ServiceError) as info:
        start_service(RUN_ID, replace(POSTGRES, host_port=55440), {}, Events(), probe=ready)

    assert str(info.value).endswith(
        "(host_port 55440 is in use by process postgres (pid 4242); stop it)"
    )
    assert log.read_text() == "-ltnpH sport = :55440\n"


@pytest.mark.parametrize(
    ("output", "exit_code"),
    [
        ("", 0),  # nothing is listening any more
        ("LISTEN 0 4096 127.0.0.1:55440 0.0.0.0:*", 0),  # another user's process
        ("", 1),
    ],
)
def test_a_port_holder_that_cannot_be_named_keeps_the_plain_hint(
    fake_docker: FakeDocker, output: str, exit_code: int
):
    fake_ss(fake_docker, output, exit_code=exit_code)
    fake_docker.write_script({"run_fail": {"postgres": PORT_TAKEN_STDERR}})

    with pytest.raises(ServiceError) as info:
        start_service(RUN_ID, replace(POSTGRES, host_port=55440), {}, Events(), probe=ready)

    assert str(info.value).endswith("(host_port 55440 is in use; stop whatever holds it)")


def test_naming_the_port_holder_never_raises(
    fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
):
    fake_docker.write_script({"run_fail": {"postgres": PORT_TAKEN_STDERR}})
    real = services._docker

    def broken_ps(docker: str, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[0] == "ps":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return real(docker, *args, **kwargs)

    monkeypatch.setattr(services, "_docker", broken_ps)

    with pytest.raises(ServiceError) as info:
        start_service(RUN_ID, replace(POSTGRES, host_port=55440), {}, Events(), probe=ready)

    assert str(info.value).endswith("(host_port 55440 is in use; stop whatever holds it)")


def test_a_missing_ss_keeps_the_plain_hint(
    fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PATH", str(fake_docker.bin.parent))
    fake_docker.write_script({"run_fail": {"postgres": PORT_TAKEN_STDERR}})

    with pytest.raises(ServiceError) as info:
        start_service(RUN_ID, replace(POSTGRES, host_port=55440), {}, Events(), probe=ready)

    assert str(info.value).endswith("(host_port 55440 is in use; stop whatever holds it)")


def test_docker_run_failure_fails_without_retry(fake_docker: FakeDocker):
    fake_docker.write_script({"run_fail": {"postgres": "Unable to find image 'postgres:16'"}})
    events = Events()

    with pytest.raises(ServiceError) as info:
        start_service(RUN_ID, POSTGRES, {}, events, probe=ready)

    assert str(info.value) == (
        "services: postgres: docker run failed: Unable to find image 'postgres:16'"
    )
    assert [call["argv"][0] for call in fake_docker.calls()] == ["run", "rm"]
    assert events.items == [("service_failed", {"name": "postgres", "reason": str(info.value)})]


def test_a_port_published_beyond_loopback_is_refused(fake_docker: FakeDocker):
    fake_docker.write_script({"port_host": "0.0.0.0"})
    with pytest.raises(ServiceError, match="no 127.0.0.1 port published for 5432/tcp"):
        start_service(RUN_ID, POSTGRES, {}, Events(), probe=ready)


def test_not_ready_in_time_fails_with_the_container_logs(fake_docker: FakeDocker):
    fake_docker.write_script({"logs": "initdb: starting\ninitdb: still going"})
    events = Events()
    spec = ServiceSpec("postgres", "postgres:16", 5432, ready_timeout=1)

    with pytest.raises(ServiceError, match=r"^services: postgres not ready after 1s$"):
        start_service(RUN_ID, spec, {}, events, probe=lambda port: False)

    assert events.kinds() == ["service_start", "service_failed"]
    assert events.items[1][1]["logs"] == ["initdb: starting", "initdb: still going"]


def test_ready_command_runs_in_the_container_once_the_port_answers(
    fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(services, "READY_POLL_SECONDS", 0)
    fake_docker.write_script({"exec_fail": 1})  # accepts TCP before it accepts queries
    spec = replace(POSTGRES, ready=("pg_isready", "-U", "postgres"))
    tcp = iter([False, True, True])
    events = Events()

    start_service(RUN_ID, spec, {}, events, probe=lambda port: next(tcp))

    execs = [call["argv"] for call in fake_docker.calls() if call["argv"][0] == "exec"]
    assert execs == [["exec", "helmraiser-1a2b3c4d-postgres", "pg_isready", "-U", "postgres"]] * 2
    assert events.kinds() == ["service_start", "service_ready"]


def test_a_ready_command_that_never_passes_fails_after_ready_timeout(fake_docker: FakeDocker):
    fake_docker.write_script({"exec_fail": 1000})
    spec = ServiceSpec("redis", "redis:7", 6379, ready_timeout=1, ready=("redis-cli", "ping"))
    events = Events()

    with pytest.raises(ServiceError, match=r"^services: redis not ready after 1s$"):
        start_service(RUN_ID, spec, {}, events, probe=ready)

    assert "exec" in [call["argv"][0] for call in fake_docker.calls()]
    assert events.kinds() == ["service_start", "service_failed"]


def test_wait_ready_polls_until_the_deadline():
    now = [0.0]
    probes: list[int] = []

    def probe(port: int) -> bool:
        probes.append(port)
        return len(probes) == 3

    def sleep(seconds: float) -> None:
        now[0] += seconds

    assert wait_ready(9, 10, probe, sleep, lambda: now[0]) is True
    assert probes == [9, 9, 9] and now[0] == 0.5
    assert wait_ready(9, 1, lambda port: False, sleep, lambda: now[0]) is False


@pytest.fixture
def listener() -> Iterator[tuple[socket.socket, int]]:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    yield server, server.getsockname()[1]
    server.close()


def _serve_once(server: socket.socket, action: str) -> threading.Thread:
    def handle() -> None:
        conn, _ = server.accept()
        if action == "banner":
            conn.sendall(b"+OK\r\n")
        if action == "close":
            conn.close()
            return
        try:
            conn.recv(1)  # hold the connection until the probe closes it
        except OSError:
            pass
        conn.close()

    thread = threading.Thread(target=handle, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize(
    ("action", "expected"), [("hold", True), ("banner", True), ("close", False)]
)
def test_port_ready_needs_a_connection_that_stays_open(
    listener: tuple[socket.socket, int], action: str, expected: bool
):
    server, port = listener
    thread = _serve_once(server, action)
    assert port_ready(port) is expected
    thread.join(timeout=5)


def test_port_ready_is_false_when_nothing_listens():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert port_ready(port) is False


def test_stop_removes_only_the_runs_containers_by_label(fake_docker: FakeDocker):
    start_service(RUN_ID, POSTGRES, {}, Events(), probe=ready)
    other = fake_docker.add_container("helmraiser-99999999-db", {"helmraiser.run": "other-run"})
    unlabelled = fake_docker.add_container("my-own-postgres", {})

    removed, error = stop_services(RUN_ID)

    assert error is None and len(removed) == 1
    assert sorted(fake_docker.containers()) == sorted([other, unlabelled])
    rm = [call["argv"] for call in fake_docker.calls() if call["argv"][0] == "rm"]
    assert rm == [["rm", "-f", "-v", *removed]]
    assert stop_services(RUN_ID) == ([], None)  # nothing left: no rm call
    assert [call["argv"][0] for call in fake_docker.calls()][-1] == "ps"


def test_stop_reports_a_docker_failure(fake_docker: FakeDocker):
    start_service(RUN_ID, POSTGRES, {}, Events(), probe=ready)
    fake_docker.write_script({"rm_fail": "Error response from daemon: busy"})
    assert stop_services(RUN_ID) == ([], "Error response from daemon: busy")


def test_orphans_are_containers_of_runs_that_are_over_or_unknown(
    fake_docker: FakeDocker, tmp_path: Path
):
    ledger = Ledger(tmp_path / "helmraiser.db")
    key = ledger.upsert_task(make_spec(tmp_path), "snapshot")
    for run_id, status in [
        ("r-landed", "landed"),
        ("r-live", "running"),
        ("r-wait", "awaiting_approval"),
    ]:
        ledger.create_run(run_row(run_id, key, str(tmp_path), status=status))
    for run_id in ("r-landed", "r-live", "r-wait", "r-gone"):
        fake_docker.add_container(f"helmraiser-{run_id}-db", {"helmraiser.run": run_id})
    fake_docker.add_container("unrelated", {})

    containers = labelled_containers()
    assert containers is not None and len(containers) == 4
    orphans = orphaned_services(ledger, containers)
    assert [(o["run_id"], o["run_status"]) for o in orphans] == [
        ("r-landed", "landed"),
        ("r-wait", "awaiting_approval"),
        ("r-gone", "unknown"),
    ]
    assert len(orphaned_services(None, containers)) == 4

    removed = remove_orphaned_services(ledger)

    # r-gone is unknown to this ledger: another data directory's run, left alone.
    assert [(item["run_id"], item["removed"]) for item in removed] == [
        ("r-landed", True),
        ("r-wait", True),
    ]
    assert sorted(c["name"] for c in fake_docker.containers().values()) == [
        "helmraiser-r-gone-db",
        "helmraiser-r-live-db",
        "unrelated",
    ]


def test_orphan_cleanup_is_a_no_op_without_a_working_docker(tmp_path: Path):
    ledger = Ledger(tmp_path / "helmraiser.db")
    # The suite's guard docker fails every call.
    assert remove_orphaned_services(ledger) == []
    assert remove_orphaned_services(ledger, docker="no-such-docker-binary") == []
    assert labelled_containers() is None


def test_docker_ready(fake_docker: FakeDocker):
    assert docker_ready() is None
    fake_docker.write_script({"info_fail": "Cannot connect to the Docker daemon"})
    assert docker_ready() == "Cannot connect to the Docker daemon"
    assert services.docker_ready("no-such-docker-binary") is not None
