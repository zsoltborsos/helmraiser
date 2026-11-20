"""``work --watch`` (the background worker) serves from a snapshot of committed code.

Started off a snapshot (``worker start``, or an older worker re-execing itself with
the same argv and environment), it exports HEAD's ``src/helmraiser`` to
``<data_dir>/code/<commit>`` and re-execs onto it. On its snapshot it records the
commit in ``worker.json`` and prunes old snapshots. Interactive commands keep running
from the checkout.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, commands, supervisor
from helmraiser.cli.supervisor import SupervisorResult
from helmraiser.code_snapshot import SNAPSHOT_ENV, SOURCE_REPO_ENV
from tests.conftest import FakePi, configured_test_config
from tests.test_code_snapshot import PACKAGE, _git, commit

runner = CliRunner()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def repo(tmp_path: Path, git_init: Any) -> Path:
    path = tmp_path / "checkout"
    path.mkdir()
    git_init(path)
    commit(path, PACKAGE, "package")
    return path


@pytest.fixture(autouse=True)
def config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    config = configured_test_config()
    monkeypatch.setattr(commands, "load_config", lambda: config)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv(supervisor.RESTART_CHILDREN_ENV, raising=False)
    return config


@pytest.fixture
def supervisors(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class Recording:
        def __init__(self, **kwargs: Any):
            calls.append(kwargs)

        def run(self) -> SupervisorResult:
            return SupervisorResult()

    monkeypatch.setattr(commands, "Supervisor", Recording)
    return calls


@pytest.fixture
def execs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str], dict[str, str]]]:
    calls: list[tuple[str, list[str], dict[str, str]]] = []

    def execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        calls.append((path, argv, env))
        raise OSError("exec refused in the test")

    monkeypatch.setattr(os, "execve", execve)
    return calls


def work(*args: str, data_dir: Path) -> Any:
    return runner.invoke(app, ["work", *args, "--data-dir", str(data_dir)])


def record(data_dir: Path) -> dict[str, Any]:
    return json.loads((data_dir / "worker.json").read_text())


def test_an_older_worker_re_execing_without_markers_moves_onto_a_snapshot(
    data_dir: Path,
    repo: Path,
    fake_pi: FakePi,
    supervisors: list,
    execs: list,
    monkeypatch: pytest.MonkeyPatch,
):
    """The transition: the old ``_reexec`` keeps argv and environment, so the new code
    starts without the markers and must take the snapshot path itself."""
    monkeypatch.setattr(commands, "source_repo", lambda: repo)
    (data_dir / "worker.json").write_text(json.dumps({"pid": os.getpid(), "argv": []}))
    (repo / "src/helmraiser/__init__.py").write_text("MARK = 'uncommitted'\n")
    head = _git(repo, "rev-parse", "HEAD")

    result = work("--watch", "--json", data_dir=data_dir)

    ((path, argv, env),) = execs
    snapshot = data_dir / "code" / head
    assert (path, argv) == (sys.executable, sys.orig_argv)
    assert env[SNAPSHOT_ENV] == head and env[SOURCE_REPO_ENV] == str(repo)
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(snapshot / "src")
    assert (snapshot / "src/helmraiser/__init__.py").read_text() == "MARK = 'one'\n"
    # The exec was refused here: the worker fails loudly instead of serving the checkout.
    assert result.exit_code == 1 and supervisors == []
    assert f"cannot serve from a code snapshot of {head[:8]}" in result.output
    assert "exec failed: exec refused in the test" in result.output
    assert record(data_dir)["exit"]["error"] == "exit code 1; see the log"


def test_a_head_that_does_not_import_is_not_served(
    data_dir: Path,
    repo: Path,
    fake_pi: FakePi,
    supervisors: list,
    execs: list,
    monkeypatch: pytest.MonkeyPatch,
):
    commit(repo, {"src/helmraiser/cli/app.py": "def broken(:\n"}, "broken")
    monkeypatch.setattr(commands, "source_repo", lambda: repo)

    result = work("--watch", "--json", data_dir=data_dir)

    assert result.exit_code == 1 and execs == [] and supervisors == []
    assert "the new code does not import" in result.output


def test_on_its_snapshot_it_records_the_code_and_prunes(
    data_dir: Path,
    repo: Path,
    fake_pi: FakePi,
    supervisors: list,
    execs: list,
    monkeypatch: pytest.MonkeyPatch,
):
    head = _git(repo, "rev-parse", "HEAD")
    code = data_dir / "code"
    for age, name in enumerate(["old1", "old2", "old3"]):
        (code / name).mkdir(parents=True)
        os.utime(code / name, (1000 + age, 1000 + age))
    (code / head).mkdir()
    monkeypatch.setenv(SNAPSHOT_ENV, head)
    monkeypatch.setenv(SOURCE_REPO_ENV, str(repo))
    monkeypatch.setattr(commands, "loaded_from", lambda snapshot: snapshot == code / head)
    (data_dir / "worker.json").write_text(json.dumps({"pid": os.getpid(), "argv": []}))
    commit(repo, {"src/helmraiser/__init__.py": "MARK = 'two'\n"}, "newer")  # HEAD moved on

    result = work("--watch", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    assert execs == []
    (call,) = supervisors
    assert call["running_code"] == supervisor.code_version(repo, head)
    assert record(data_dir)["code"] == {"commit": head, "snapshot": str(code / head)}
    assert sorted(p.name for p in code.iterdir()) == sorted([head, "old2", "old3"])
    assert {"code_snapshot": {"commit": head, "path": str(code / head)}} in [
        json.loads(line) for line in result.output.splitlines() if line.startswith("{")
    ]


@pytest.mark.parametrize("transport", ["record", "environment", "foreign"])
def test_adopts_before_startup_reconcile_and_protects_snapshots(
    data_dir, repo, fake_pi, supervisors, monkeypatch, tmp_path, transport
):
    from helmraiser.ledger.db import Ledger
    from tests.ledger.test_db import make_spec, run_row

    head = _git(repo, "rev-parse", "HEAD")
    code = data_dir / "code"
    for age, name in enumerate(["child-old", "old2", "old3", head]):
        (code / name).mkdir(parents=True)
        os.utime(code / name, (1000 + age, 1000 + age))
    monkeypatch.setenv(SNAPSHOT_ENV, head)
    monkeypatch.setenv(SOURCE_REPO_ENV, str(repo))
    monkeypatch.setattr(commands, "loaded_from", lambda snapshot: True)
    children = []
    ledger = Ledger(data_dir / "helmraiser.db")
    for index, name in enumerate(["run", "scout", "orphan"]):
        project = tmp_path / name
        task_id = ledger.upsert_task(make_spec(project, "task"), "")
        ledger.create_run(run_row(name, task_id, str(project)))
        if name == "orphan":
            continue
        child = supervisor.Child(
            str(project),
            supervisor.AdoptedProcess(123 + index, "token"),
            data_dir / "worker-children" / f"{name}.jsonl",
            42.0,
            kind=name,
            scout_id="scout-id" if name == "scout" else None,
            lens="docs" if name == "scout" else "state",
            start_token="token",
            code_commit="child-old",
        )
        children.append(child.restart_dict())
    owner = 1 if transport == "foreign" else os.getpid()
    record_payload = {"pid": owner, "argv": []}
    if transport == "record":
        record_payload["restart_children"] = children
    else:
        monkeypatch.setenv(
            supervisor.RESTART_CHILDREN_ENV, json.dumps({"pid": owner, "children": children})
        )
    (data_dir / "worker.json").write_text(json.dumps(record_payload))

    result = work("--watch", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    adopted = supervisors[0]["children"]
    assert [child.restart_dict() for child in adopted] == (
        children if transport != "foreign" else []
    )
    assert ledger.get_run("orphan")["status"] == "failed"
    for name in ["run", "scout"]:
        assert ledger.get_run(name)["status"] == ("running" if transport != "foreign" else "failed")
    assert (code / "child-old").exists() is (transport != "foreign")
    assert supervisor.RESTART_CHILDREN_ENV not in os.environ
    assert not record(data_dir).get("restart_children")


def test_markers_without_the_snapshot_on_the_import_path_fail(
    data_dir: Path,
    repo: Path,
    fake_pi: FakePi,
    supervisors: list,
    monkeypatch: pytest.MonkeyPatch,
):
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv(SNAPSHOT_ENV, head)
    monkeypatch.setenv(SOURCE_REPO_ENV, str(repo))

    result = work("--watch", "--json", data_dir=data_dir)

    assert result.exit_code == 1 and supervisors == []
    error = json.loads(result.output.splitlines()[-1])["error"]
    assert error.startswith(f"the worker should run the code snapshot {data_dir / 'code' / head}")
    assert "but imports helmraiser from" in error


def test_an_installed_package_outside_a_checkout_serves_as_is(
    data_dir: Path, fake_pi: FakePi, supervisors: list, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(commands, "source_repo", lambda: None)

    result = work("--watch", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    (call,) = supervisors
    assert call["running_code"] is None
    assert not (data_dir / "code").exists()


def test_only_the_watching_worker_uses_a_snapshot(
    data_dir: Path,
    repo: Path,
    fake_pi: FakePi,
    supervisors: list,
    execs: list,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(commands, "source_repo", lambda: repo)

    result = work("--max-parallel", "2", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    assert execs == [] and not (data_dir / "code").exists()
    assert supervisors[0]["running_code"] is None
