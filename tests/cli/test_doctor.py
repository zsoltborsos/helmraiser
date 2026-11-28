"""Readiness checks for ``helmraiser doctor`` without real pi or isolation tools."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from helmraiser.cli import app, monitor
from helmraiser.config import DEFAULT_CONFIG
from helmraiser.isolation.environment import is_stripped
from helmraiser.isolation.exposure import COMPONENTS, PROBES, ExposureReport, ExposureResult
from helmraiser.isolation.none import NoIsolation
from helmraiser.isolation.process import ProcessIsolation
from helmraiser.ledger.db import Ledger
from helmraiser.pi.adapter import PiVersionError
from helmraiser.task.project import worktree_manager
from tests.conftest import configured_test_config
from tests.ledger.test_db import make_spec, run_row

runner = CliRunner()


@pytest.fixture(autouse=True)
def ready_checks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # doctor reads the current directory's project: never the checkout the suite runs from.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(monitor, "load_config", configured_test_config)
    monkeypatch.setattr(monitor, "pi_version", lambda pi_bin="pi": "0.87.1")
    monkeypatch.setattr(monitor, "check_auth", lambda model, pi_bin="pi": None)
    monkeypatch.setattr(NoIsolation, "check_available", lambda self: None)
    monkeypatch.setattr(ProcessIsolation, "check_available", lambda self: None)


@pytest.mark.parametrize("as_json", [False, True])
def test_unconfigured_profiles_not_auth_checked(tmp_path, monkeypatch, as_json):
    monkeypatch.setenv("COLUMNS", "140")
    monkeypatch.setattr(monitor, "load_config", lambda: copy.deepcopy(DEFAULT_CONFIG))
    monkeypatch.setattr(monitor, "check_auth", lambda *a: pytest.fail("auth called"))
    result = invoke(tmp_path, *(["--json"] if as_json else []))
    assert result.exit_code == 1, result.output
    if as_json:
        assert json.loads(result.output)["pi"]["auth"] == dict.fromkeys(
            DEFAULT_CONFIG["profiles"], "not_configured"
        )
    else:
        assert "pi auth: implement" in result.output
        assert "not configured" in result.output
        assert "not ready" in result.output


def invoke(data_dir: Path, *args: str):
    return runner.invoke(app, ["doctor", *args, "--data-dir", str(data_dir)])


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def exposure_report(
    repo: Path, status: str = "blocked", *, required: bool = True
) -> ExposureReport:
    return ExposureReport(
        str(repo),
        tuple(
            ExposureResult(
                component,
                probe,
                status,
                f"{status} reason",
                required,
                "ok" if status == "blocked" else "n/a",
            )
            for component in COMPONENTS
            for probe in PROBES
        ),
    )


def test_exposure_text_and_json(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[Path, Path]] = []

    def fake(repo: Path, config: dict, *, data_dir: Path) -> ExposureReport:
        seen.append((repo, data_dir))
        return exposure_report(repo)

    monkeypatch.setattr(monitor, "run_exposure", fake)
    monkeypatch.chdir(tmp_project)

    text = invoke(tmp_path / "unused", "--exposure")
    machine = invoke(tmp_path / "unused", "--exposure", str(tmp_project), "--json")

    assert text.exit_code == 0
    assert len(text.output.strip().splitlines()) == 12
    assert text.output.splitlines()[0].startswith("pi-session loopback blocked [control ok]:")
    assert machine.exit_code == 0
    payload = json.loads(machine.output)
    assert payload["ok"] is True and payload["complete"] is True
    assert {row["control"] for row in payload["results"]} == {"ok"}
    assert [(row["component"], row["probe"]) for row in payload["results"]] == [
        (component, probe) for component in COMPONENTS for probe in PROBES
    ]
    assert seen == [(tmp_project.resolve(), tmp_path / "unused")] * 2


@pytest.mark.parametrize(("status", "code"), [("exposed", 1), ("error", 1), ("skipped", 2)])
def test_exposure_failure_exit_codes(
    tmp_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    code: int,
) -> None:
    monkeypatch.setattr(
        monitor, "run_exposure", lambda repo, config, **kwargs: exposure_report(repo, status)
    )

    result = invoke(tmp_path / "unused", "--exposure", str(tmp_project), "--json")

    assert result.exit_code == code
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["complete"] is (status != "skipped")


def test_absent_agent_skip_is_neutral(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = exposure_report(tmp_project)
    rows = tuple(
        (
            ExposureResult(row.component, row.probe, "skipped", "host SSH agent is absent", False)
            if row.probe == "ssh-agent"
            else row
        )
        for row in report.results
    )
    monkeypatch.setattr(
        monitor, "run_exposure", lambda repo, config, **kwargs: ExposureReport(str(repo), rows)
    )

    result = invoke(tmp_path / "unused", "--exposure", str(tmp_project), "--json")

    assert result.exit_code == 0
    assert json.loads(result.output)["complete"] is True


def test_exposure_rejects_invalid_or_unrequested_repository(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    assert invoke(tmp_path / "d", str(outside)).exit_code == 2
    result = invoke(tmp_path / "d", "--exposure", str(outside), "--json")
    assert result.exit_code == 2
    assert "not a git repository" in json.loads(result.output)["error"]


@pytest.mark.parametrize("value", [None, "true", "false"])
@pytest.mark.parametrize("explicit", [True, False])
def test_git_maintenance_json(tmp_project, tmp_path, monkeypatch, value, explicit):
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")
    if value is not None:
        git(tmp_project, "config", "--local", "maintenance.auto", value)
    before = git(tmp_project, "config", "--local", "--list")
    monkeypatch.chdir(tmp_project)
    result = invoke(data_dir, *([str(tmp_project)] if explicit else []), "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["git_maintenance"] == {
        "auto": value,
        "managed": value == "false",
    }
    assert git(tmp_project, "config", "--local", "--list") == before


@pytest.mark.parametrize("value", [None, "true", "false"])
def test_git_maintenance_human_warning(tmp_project, tmp_path, monkeypatch, value):
    monkeypatch.setattr(monitor, "console", Console(width=1000))
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")
    if value is not None:
        git(tmp_project, "config", "--local", "maintenance.auto", value)
    result = invoke(data_dir, str(tmp_project))
    assert result.exit_code == 0, result.output
    for text in (
        "can delete live sessions' ref directories",
        "next run",
        "git config maintenance.auto false",
    ):
        assert (text in result.output) is (value != "false")


@pytest.mark.parametrize(
    "failure", [OSError("missing git"), subprocess.TimeoutExpired("git", 60), 3]
)
def test_git_maintenance_diagnostic_failure(tmp_project, tmp_path, monkeypatch, failure):
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")
    original = subprocess.run

    def failing(argv, **kwargs):
        if argv == ["git", "config", "--local", "--get", "maintenance.auto"]:
            if isinstance(failure, Exception):
                raise failure
            return subprocess.CompletedProcess(argv, failure, "", "broken")
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", failing)
    result = invoke(data_dir, str(tmp_project), "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["git_maintenance"] == {"auto": None, "managed": False}


def test_project_notes_line_for_explicit_and_cwd_repository(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")
    notes = "Sandbox constraints.\n"
    notes_path = tmp_project / ".helmraiser" / "notes.md"
    notes_path.parent.mkdir(exist_ok=True)
    notes_path.write_text(notes, encoding="utf-8")

    explicit = invoke(data_dir, str(tmp_project))
    explicit_json = invoke(data_dir, str(tmp_project), "--json")
    monkeypatch.chdir(tmp_project)
    cwd = invoke(data_dir)
    cwd_json = invoke(data_dir, "--json")

    expected = {
        "repo": str(tmp_project.resolve()),
        "exists": True,
        "size_bytes": len(notes.encode()),
    }
    assert explicit.exit_code == 0 and "project notes" in explicit.output
    assert f"{len(notes.encode())} bytes" in explicit.output
    assert cwd.exit_code == 0 and f"{len(notes.encode())} bytes" in cwd.output
    assert json.loads(explicit_json.output)["project_notes"] == expected
    assert json.loads(cwd_json.output)["project_notes"] == expected

    notes_path.unlink()
    absent = invoke(data_dir)
    absent_json = json.loads(invoke(data_dir, "--json").output)
    assert absent.exit_code == 0 and "not found" in absent.output
    assert absent_json["project_notes"] == {
        "repo": str(tmp_project.resolve()),
        "exists": False,
        "size_bytes": None,
    }


@pytest.mark.parametrize(
    "scenario", ["unconfigured", "unnamed", "ready", "missing-write", "different-filesystem"]
)
def test_pnpm_store_text_and_json(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    import helmraiser.task.project as project_module

    for name in list(os.environ):
        if is_stripped(name):
            monkeypatch.delenv(name)
    monkeypatch.setattr(monitor, "console", Console(width=1000))
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")
    worktrees = tmp_path / "configured-worktrees"
    store = tmp_project / "cache" / "[red]store"
    config = f'[defaults]\nworktree_dir = "{worktrees}"\n'
    if scenario != "unconfigured":
        config += 'setup = "pnpm install"\n'
    if scenario not in {"unconfigured", "unnamed"}:
        config += '[defaults.env]\nnpm_config_store_dir = "cache/[red]store"\n'
        if scenario != "missing-write":
            config += '[setup]\nwrite = ["$npm_config_store_dir"]\n'
    project_file = tmp_project / ".helmraiser" / "project.toml"
    project_file.parent.mkdir(exist_ok=True)
    project_file.write_text(config, encoding="utf-8")
    if scenario == "different-filesystem":
        monkeypatch.setattr(
            project_module, "_device", lambda path: 2 if path == worktrees.resolve() else 1
        )

    text = invoke(data_dir, str(tmp_project))
    machine = invoke(data_dir, str(tmp_project), "--json")

    assert text.exit_code == machine.exit_code == 0, text.output + machine.output
    payload = json.loads(machine.output)
    assert "pnpm_store" in payload
    result = payload["pnpm_store"]
    if scenario == "unconfigured":
        assert result is None
        assert "pnpm store" not in text.output
    else:
        row = next(line for line in text.output.splitlines() if "│ pnpm store" in line)
        if scenario == "unnamed":
            assert result["store"] is result["same_filesystem"] is result["in_setup_write"] is None
            assert len(result["warnings"]) == 1
            assert "npm_config_store_dir" in result["warnings"][0]
            assert "filesystem root" in result["warnings"][0]
        else:
            assert result["store"] == str(store.resolve())
            assert result["same_filesystem"] is (scenario != "different-filesystem")
            assert result["in_setup_write"] is (scenario != "missing-write")
            assert str(store.resolve()) in row
            assert "[red]store" in row
        if scenario == "ready":
            assert result == {
                "store": str(store.resolve()),
                "same_filesystem": True,
                "in_setup_write": True,
                "warnings": [],
            }
            assert "ready" in row
        else:
            assert "warn" in row
            assert "; ".join(result["warnings"]) in row
            if scenario == "missing-write":
                assert "setup under srt cannot write" in row
                assert "[setup].write" in row
            elif scenario == "different-filesystem":
                assert str(worktrees.resolve()) in row
                assert "pnpm copies instead of hard-linking" in row
    assert not store.exists()
    assert not worktrees.exists()


@pytest.mark.parametrize("data_dir_exists", [False, True])
def test_missing_ledger_is_informational(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data_dir_exists: bool
) -> None:
    data_dir = tmp_path / "data"
    if data_dir_exists:
        data_dir.mkdir()
    db_path = data_dir / "helmraiser.db"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(monitor, "console", Console(width=1000))

    text = invoke(data_dir)
    assert not db_path.exists()
    assert data_dir.exists() is data_dir_exists
    machine = invoke(data_dir, "--json")
    assert not db_path.exists()
    assert data_dir.exists() is data_dir_exists

    assert text.exit_code == machine.exit_code == 0, text.output + machine.output
    assert json.loads(machine.output)["ledger"] == {
        "exists": False,
        "opens": False,
        "paused": None,
    }
    row = next(line for line in text.output.splitlines() if "│ ledger exists" in line)
    assert "info" in row
    assert str(db_path) in row
    assert "not created yet; created on first use" in row
    assert "not ready" not in row
    assert "ledger opens" not in text.output


def test_existing_unopenable_ledger_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "helmraiser.db").write_bytes(b"not a SQLite database")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(monitor, "console", Console(width=1000))

    text = invoke(data_dir)
    machine = invoke(data_dir, "--json")

    assert text.exit_code == machine.exit_code == 1, text.output + machine.output
    assert json.loads(machine.output)["ledger"] == {
        "exists": True,
        "opens": False,
        "paused": None,
    }
    exists_row = next(line for line in text.output.splitlines() if "│ ledger exists" in line)
    opens_row = next(line for line in text.output.splitlines() if "│ ledger opens" in line)
    assert "ready" in exists_row and "not ready" not in exists_row
    assert "not ready" in opens_row


def test_all_ready_exits_zero_and_prints_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitor, "console", Console(width=1000))
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")

    result = invoke(data_dir)

    assert result.exit_code == 0, result.output
    assert "Helmraiser doctor" in result.output
    assert "pi auth: implement" in result.output
    assert "isolation: process" in result.output
    assert "srt path" in result.output
    for name in ("ledger exists", "ledger opens"):
        row = next(line for line in result.output.splitlines() if f"│ {name}" in line)
        assert "ready" in row and "not ready" not in row
    assert "orphaned run state" in result.output


def test_reserved_vm_is_informational(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(monitor, "console", Console(width=1000))

    text = invoke(data_dir)
    machine = invoke(data_dir, "--json")

    assert text.exit_code == machine.exit_code == 0, text.output + machine.output
    row = next(line for line in text.output.splitlines() if "│ isolation: vm" in line)
    assert "info" in row
    assert "reserved; not implemented" in row
    assert "not ready" not in row
    for level in ("none", "process"):
        row = next(line for line in text.output.splitlines() if f"│ isolation: {level}" in line)
        assert "ready" in row and "not ready" not in row
    assert json.loads(machine.output)["isolation"] == {
        "none": {"available": True, "reason": None},
        "process": {"available": True, "reason": None},
        "vm": {"available": False, "reason": "vm isolation not implemented"},
    }


def test_srt_path_reports_configured_executable(tmp_path: Path, monkeypatch) -> None:
    custom = tmp_path / "custom-srt"
    custom.write_text("#!/bin/sh\nexit 0\n")
    custom.chmod(0o755)
    config = configured_test_config()
    config["isolation"] = {"srt": str(custom)}
    monkeypatch.setattr(monitor, "load_config", lambda: config)
    monkeypatch.setattr(monitor, "console", Console(width=240))
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")

    machine = invoke(data_dir, "--json")
    assert machine.exit_code == 0
    assert json.loads(machine.output)["srt"] == {"path": str(custom), "error": None}
    text = invoke(data_dir)
    assert text.exit_code == 0
    assert "srt path" in text.output
    assert str(custom) in text.output


def test_srt_resolution_error_is_informational(tmp_path: Path, monkeypatch) -> None:
    config = configured_test_config()
    config["isolation"] = {"srt": str(tmp_path / "missing-srt")}
    monkeypatch.setattr(monitor, "load_config", lambda: config)
    monkeypatch.setattr(monitor, "console", Console(width=500))
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")

    machine = invoke(data_dir, "--json")
    assert machine.exit_code == 0
    srt = json.loads(machine.output)["srt"]
    assert srt["path"] is None
    assert "isolation.srt" in srt["error"]
    text = invoke(data_dir)
    assert text.exit_code == 0
    assert "srt path" in text.output
    assert srt["error"] in text.output


def _no_pi_version(pi_bin: str = "pi") -> str:
    raise PiVersionError(f"{pi_bin} --version gave no version: exit status 1")


@pytest.mark.parametrize("failed_check", ["pi", "process"])
def test_pi_without_version_or_unavailable_process_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_check: str
) -> None:
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")
    if failed_check == "pi":
        monkeypatch.setattr(monitor, "pi_version", _no_pi_version)
    else:
        monkeypatch.setattr(
            ProcessIsolation, "check_available", lambda self: "srt not found on PATH"
        )

    result = invoke(data_dir, "--json")

    assert result.exit_code == 1
    output = json.loads(result.output)
    if failed_check == "pi":
        assert output["pi"]["version"] is None
    else:
        assert output["isolation"]["process"] == {
            "available": False,
            "reason": "srt not found on PATH",
        }


def test_json_shape_and_paused_state(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    ledger = Ledger(data_dir / "helmraiser.db")
    ledger.pause_queue("maintenance")

    result = invoke(data_dir, "--json")

    assert result.exit_code == 1
    output = json.loads(result.output)
    assert set(output) == {
        "pi",
        "isolation",
        "srt",
        "data_dir",
        "ledger",
        "phantom_runs",
        "worktrees",
        "project_notes",
        "pnpm_store",
        "git_maintenance",
        "environment",
        "sandbox",
        "leeboard",
    }
    assert output["pi"] == {
        "version": "0.87.1",
        "auth": dict.fromkeys(DEFAULT_CONFIG["profiles"]),
    }
    assert output["isolation"]["none"] == {"available": True, "reason": None}
    assert output["isolation"]["process"] == {"available": True, "reason": None}
    # vm is reserved: reported as unavailable, but it does not gate readiness.
    assert output["isolation"]["vm"]["available"] is False
    assert output["data_dir"] == str(data_dir)
    assert output["ledger"] == {
        "exists": True,
        "opens": True,
        "paused": {"reason": "maintenance", "until": None},
    }


def test_phantom_runs_row_appears_only_when_needed(tmp_project: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    ledger = Ledger(data_dir / "helmraiser.db")

    clean = invoke(data_dir)
    assert clean.exit_code == 0
    assert "phantom runs" not in clean.output

    task_id = ledger.upsert_task(make_spec(tmp_project), "")
    run_id = "11111111-1111-4111-8111-111111111111"
    ledger.create_run(run_row(run_id, task_id, str(tmp_project)))

    result = invoke(data_dir)

    assert result.exit_code == 0, result.output
    assert "phantom runs" in result.output
    assert run_id[:8] in result.output
    assert "helmraiser work" in result.output
    payload = json.loads(invoke(data_dir, "--json").output)
    assert payload["phantom_runs"][0]["run_id"] == run_id


@pytest.mark.parametrize("location", ["explicit", "default"])
def test_json_reports_orphaned_worktree_without_affecting_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "trunk")
    run_id = "11111111-1111-4111-8111-111111111111"
    branch_name = f"feature/demo/{run_id}"
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    git(repo, "branch", branch_name)
    (repo / ".helmraiser").mkdir()
    if location == "explicit":
        metadata_dir = tmp_path / "external-worktrees"
        (repo / ".helmraiser" / "project.toml").write_text(
            f'[defaults]\nworktree_dir = "{metadata_dir}"\n', encoding="utf-8"
        )
    else:
        metadata_dir = worktree_manager(repo).path_for("demo", run_id).parent
    metadata_dir.mkdir(parents=True)
    worktree_path = metadata_dir / f"worktree-demo-{run_id}"
    (metadata_dir / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "topic_id": "demo",
                "worktree_path": str(worktree_path),
                "branch_name": branch_name,
                "base_branch": "trunk",
                "base_commit": git(repo, "rev-parse", "HEAD"),
            }
        ),
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")
    monkeypatch.chdir(repo)

    result = invoke(data_dir, "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["worktrees"] == {
        "repo": str(repo),
        "orphaned": [
            {
                "run_id": run_id,
                "topic_id": "demo",
                "branch_name": branch_name,
                "worktree_path": str(worktree_path),
                "metadata_path": str(metadata_dir / f"{run_id}.json"),
                "branch_exists": True,
            }
        ],
    }
    monkeypatch.setattr(monitor, "console", Console(width=1000))
    text = invoke(data_dir)
    assert text.exit_code == 0
    assert f"delete {metadata_dir / f'{run_id}.json'}" in text.output


def test_sandbox_deny_read_info_row_only_when_configured(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")
    monkeypatch.chdir(tmp_project)

    without = invoke(data_dir)
    assert without.exit_code == 0
    assert "sandbox deny_read" not in without.output

    project_file = tmp_project / ".helmraiser" / "project.toml"
    project_file.parent.mkdir(exist_ok=True)
    project_file.write_text('[sandbox]\ndeny_read = ["secrets", "~/private"]\n')

    result = invoke(data_dir)

    assert result.exit_code == 0, result.output
    assert "sandbox deny_read" in result.output
    assert "secrets, ~/private" in result.output


def test_deny_read_matching_tracked_files_is_a_warning_not_a_failure(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")
    monkeypatch.chdir(tmp_project)
    config = configured_test_config()
    config["isolation"] = {"deny_read": ["README.md"]}
    monkeypatch.setattr(monitor, "load_config", lambda: config)
    project_file = tmp_project / ".helmraiser" / "project.toml"
    project_file.parent.mkdir(exist_ok=True)
    project_file.write_text('[sandbox]\ndeny_read = ["untracked-secrets", "~/private"]\n')

    text = invoke(data_dir)
    output = json.loads(invoke(data_dir, "--json").output)

    assert text.exit_code == 0, text.output
    assert "sandbox tracked files" in text.output
    assert output["sandbox"] == {"tracked_deny_read": {"README.md": ["README.md"]}}


def test_stripped_environment_names_are_reported_without_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")
    for name in list(os.environ):
        if name.startswith(("SSH_", "CLAUDE", "GH_", "DBUS_", "WAYLAND_", "DISPLAY")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/value-never-printed.sock")
    monkeypatch.setenv("GH_TOKEN", "value-never-printed")

    text = invoke(data_dir)
    output = json.loads(invoke(data_dir, "--json").output)

    assert text.exit_code == 0, text.output
    assert "stripped from runs:" in text.output
    assert "GH_TOKEN" in text.output and "SSH_AUTH_SOCK" in text.output
    assert "value-never-printed" not in text.output
    stripped = output["environment"]["stripped_from_runs"]
    assert "GH_TOKEN" in stripped and "SSH_AUTH_SOCK" in stripped
    assert "value-never-printed" not in json.dumps(output)


def test_stripped_interpreter_names_and_paths_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")
    for name in list(os.environ):
        if is_stripped(name):
            monkeypatch.delenv(name)
    monkeypatch.setattr(monitor, "console", Console(width=500))
    monkeypatch.setattr(sys, "prefix", "/i")
    monkeypatch.setattr(sys, "base_prefix", "/system")
    monkeypatch.setattr(sys, "executable", "/i/bin/python")
    monkeypatch.setenv("VIRTUAL_ENV", "/w")
    monkeypatch.setenv("VIRTUAL_ENV_PROMPT", "prompt-never-printed")
    monkeypatch.setenv("PYTHONHOME", "home-never-printed")
    monkeypatch.setenv("GH_TOKEN", "secret-never-printed")
    monkeypatch.setenv("PATH", f"/w/bin:/i/bin:/w/bin:{os.environ['PATH']}")

    text = invoke(data_dir)
    machine = invoke(data_dir, "--json")

    assert text.exit_code == machine.exit_code == 0
    output = json.loads(machine.output)
    for name in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME"):
        assert name in text.output
        assert name in output["environment"]["stripped_from_runs"]
    assert "stripped from PATH:" in text.output
    assert "/w/bin" in text.output and "/i/bin" in text.output
    assert output["environment"]["stripped_path_entries"] == ["/i/bin", "/w/bin"]
    for value in ("prompt-never-printed", "home-never-printed", "secret-never-printed"):
        assert value not in text.output + machine.output


def test_interpreter_path_alone_still_shows_environment_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")
    for name in list(os.environ):
        if is_stripped(name):
            monkeypatch.delenv(name)
    monkeypatch.setattr(sys, "prefix", "/i")
    monkeypatch.setattr(sys, "base_prefix", "/system")
    monkeypatch.setattr(sys, "executable", "/i/bin/python")
    monkeypatch.setenv("PATH", f"/i/bin:{os.environ['PATH']}")

    text = invoke(data_dir)
    machine = invoke(data_dir, "--json")

    assert text.exit_code == machine.exit_code == 0
    assert "environment" in text.output and "stripped from PATH: /i/bin" in text.output
    assert "stripped from runs:" not in text.output
    assert json.loads(machine.output)["environment"] == {
        "stripped_from_runs": [],
        "stripped_path_entries": ["/i/bin"],
    }


def test_no_worker_venv_reports_no_stripped_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")
    for name in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "prefix", sys.base_prefix)

    machine = invoke(data_dir, "--json")

    assert machine.exit_code == 0
    assert json.loads(machine.output)["environment"]["stripped_path_entries"] == []


def test_gh_row_only_for_pr_delivery(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")
    monkeypatch.chdir(tmp_project)

    without = invoke(data_dir)
    assert without.exit_code == 0 and "│ gh " not in without.output

    project_file = tmp_project / ".helmraiser" / "project.toml"
    project_file.parent.mkdir(exist_ok=True)
    project_file.write_text('[defaults]\ndelivery = "pr"\n', encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\necho 'logged in to github.test'\n", encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    ready = invoke(data_dir, "--json")
    assert ready.exit_code == 0, ready.output
    assert json.loads(ready.output)["gh"] == {
        "ready": True,
        "detail": "logged in to github.test",
    }

    git_path = shutil.which("git")
    assert git_path is not None
    gh.unlink()
    (bin_dir / "git").symlink_to(git_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    missing = invoke(data_dir, "--json")
    assert missing.exit_code == 1
    assert json.loads(missing.output)["gh"] == {
        "ready": False,
        "detail": "gh not found on PATH",
    }


def test_pnpm_store_outside_repository_is_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")

    text = invoke(data_dir)
    machine = invoke(data_dir, "--json")

    assert text.exit_code == machine.exit_code == 0
    assert json.loads(machine.output)["pnpm_store"] is None
    assert "pnpm store" not in text.output


def test_json_outside_git_repository_has_no_worktree_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    Ledger(data_dir / "helmraiser.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    result = invoke(data_dir, "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["worktrees"] == {"repo": None, "orphaned": []}
    assert json.loads(result.output)["git_maintenance"] is None
    assert "Automatic git maintenance" not in invoke(data_dir).output
