"""Services from the CLI with the fake docker: ``work`` removes orphaned service
containers before dispatch and runs a task with services; ``doctor`` checks docker and
lists orphans when the current project declares services."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, monitor
from helmraiser.isolation.none import NoIsolation
from helmraiser.isolation.process import ProcessIsolation
from helmraiser.ledger.db import Ledger
from helmraiser.loop import services
from helmraiser.storage.state import StateManager
from tests.cli.test_work import cli_env, queue_task, work  # noqa: F401 (fixture)
from tests.conftest import FakeDocker, FakePi, configured_test_config
from tests.ledger.test_db import make_spec, run_row

runner = CliRunner()
SERVICE = """
[[services]]
name = "redis"
image = "redis:7"
port = 6379
provides = { REDIS_URL = "redis://{host}:{port}/0" }
"""


def declare_services(project: Path) -> None:
    path = project / ".helmraiser" / "project.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(SERVICE)


def ledger_with_runs(data_dir: Path, project: Path) -> Ledger:
    ledger = Ledger(data_dir / "helmraiser.db")
    key = ledger.upsert_task(make_spec(project, "old"), "")
    ledger.create_run(run_row("run-landed", key, str(project), status="landed"))
    return ledger


def test_work_removes_orphaned_service_containers_first(
    tmp_project: Path, tmp_path: Path, fake_docker: FakeDocker, fake_pi: FakePi
):
    data_dir = tmp_path / "data"
    ledger = ledger_with_runs(data_dir, tmp_project)
    key = ledger.upsert_task(make_spec(tmp_project, "live"), "")
    ledger.create_run(run_row("run-live", key, str(tmp_project), status="running"))
    state = StateManager(tmp_project / ".helmraiser" / "state.db")
    state.create_run("run-live", "live")
    state.acquire_lock("run-live", "live", os.getpid())  # a worker that is alive
    fake_docker.add_container("helmraiser-run-land-redis", {"helmraiser.run": "run-landed"})
    fake_docker.add_container("helmraiser-run-gone-redis", {"helmraiser.run": "run-gone"})
    live = fake_docker.add_container("helmraiser-run-live-redis", {"helmraiser.run": "run-live"})

    result = work("--once", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    removed = json.loads(result.output.splitlines()[0])["services_removed"]
    # run-gone is unknown to this ledger (another data directory's run): left alone.
    assert [(item["run_id"], item["run_status"], item["removed"]) for item in removed] == [
        ("run-landed", "landed", True),
    ]
    assert sorted(c["name"] for c in fake_docker.containers().values()) == [
        "helmraiser-run-gone-redis",
        "helmraiser-run-live-redis",
    ]
    assert live in fake_docker.containers()

    text = work("--once", data_dir=data_dir)
    assert "removed service container" not in text.output  # nothing left to remove


def test_work_runs_a_task_with_services_and_removes_them(
    tmp_project: Path,
    tmp_path: Path,
    fake_pi: FakePi,
    fake_docker: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(services, "port_ready", lambda port: True)
    declare_services(tmp_project)
    data_dir = tmp_path / "data"
    queue_task(tmp_project, data_dir)
    fake_pi.write_script({"default": {"files": [["done.txt", "ok\n"]]}})

    result = work("--once", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    outcome = json.loads(result.output.splitlines()[0])
    assert outcome["status"] == "awaiting_approval", outcome
    (service,) = outcome["services"]
    assert service["name"] == "redis" and service["provides"] == ["REDIS_URL"]
    assert fake_docker.containers() == {}


@pytest.fixture
def doctor_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monitor, "load_config", configured_test_config)
    monkeypatch.setattr(monitor, "pi_version", lambda pi_bin="pi": "0.87.1")
    monkeypatch.setattr(monitor, "check_auth", lambda model, pi_bin="pi": None)
    monkeypatch.setattr(NoIsolation, "check_available", lambda self: None)
    monkeypatch.setattr(ProcessIsolation, "check_available", lambda self: None)


def doctor(data_dir: Path, *args: str):
    return runner.invoke(app, ["doctor", *args, "--data-dir", str(data_dir)])


def test_doctor_checks_docker_and_lists_orphans_for_a_project_with_services(
    tmp_project: Path,
    tmp_path: Path,
    fake_docker: FakeDocker,
    doctor_ready: None,
    monkeypatch: pytest.MonkeyPatch,
):
    data_dir = tmp_path / "data"
    ledger_with_runs(data_dir, tmp_project)
    fake_docker.add_container("helmraiser-run-land-redis", {"helmraiser.run": "run-landed"})
    monkeypatch.chdir(tmp_project)

    without = doctor(data_dir, "--json")
    assert "services" not in json.loads(without.output)
    assert fake_docker.calls() == []  # no services declared: docker is not asked

    declare_services(tmp_project)
    result = doctor(data_dir, "--json")

    assert result.exit_code == 0, result.output
    check = json.loads(result.output)["services"]
    assert check["declared"] == ["redis"]
    assert check["docker"] == {"ready": True, "detail": None}
    assert [(o["run_id"], o["run_status"]) for o in check["orphaned"]] == [("run-landed", "landed")]
    assert len(fake_docker.containers()) == 1  # doctor only lists

    text = doctor(data_dir)
    assert "docker (services)" in text.output and "orphaned service" in text.output


def test_doctor_is_not_ready_when_docker_does_not_answer(
    tmp_project: Path,
    tmp_path: Path,
    fake_docker: FakeDocker,
    doctor_ready: None,
    monkeypatch: pytest.MonkeyPatch,
):
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")
    declare_services(tmp_project)
    fake_docker.write_script({"info_fail": "Cannot connect to the Docker daemon"})
    monkeypatch.chdir(tmp_project)

    result = doctor(data_dir, "--json")

    assert result.exit_code == 1, result.output
    check = json.loads(result.output)["services"]
    assert check["docker"] == {"ready": False, "detail": "Cannot connect to the Docker daemon"}
    assert check["orphaned"] == []
