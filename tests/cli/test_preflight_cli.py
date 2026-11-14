"""``helmraiser preflight`` from the CLI, with the fake srt (tests/loop/test_preflight.py)."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from helmraiser.cli import app, preflight_commands
from helmraiser.config import DEFAULT_CONFIG
from helmraiser.loop.hints import SETUP_READONLY_HINT, SOCKET_HINT
from tests.loop.test_preflight import project_toml, srt_log  # noqa: F401 (fixture)

runner = CliRunner()
pytestmark = pytest.mark.usefixtures("srt_log")


@pytest.fixture(autouse=True)
def cli_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight_commands, "load_config", lambda: copy.deepcopy(DEFAULT_CONFIG))


def preflight(*args: str, data_dir: Path):
    return runner.invoke(app, ["preflight", "--data-dir", str(data_dir), *args])


def test_json_reports_each_command_and_touches_no_ledger(tmp_project: Path, tmp_path: Path):
    data_dir = tmp_path / "data"

    result = preflight(
        str(tmp_project),
        "--json",
        "--run",
        "echo first",
        "--",
        "sh",
        "-c",
        "echo 'listen EPERM: operation not permitted /tmp/tsx/1.pipe'; exit 4",
        data_dir=data_dir,
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert [c["command"] for c in payload["commands"]] == [
        "echo first",
        "sh -c 'echo '\"'\"'listen EPERM: operation not permitted /tmp/tsx/1.pipe'\"'\"'; exit 4'",
    ]
    first, second = payload["commands"]
    assert first["exit_code"] == 0 and first["tail"] == "first"
    assert second["exit_code"] == 4 and second["hints"] == [SOCKET_HINT]
    assert not Path(payload["worktree"]).exists()
    assert not (data_dir / "helmraiser.db").exists()


def test_a_single_trailing_argument_is_a_shell_command(tmp_project: Path, tmp_path: Path):
    result = preflight(
        str(tmp_project), "--json", "--", "echo a && echo b", data_dir=tmp_path / "d"
    )

    assert result.exit_code == 0, result.output
    (command,) = json.loads(result.output)["commands"]
    assert command["command"] == "echo a && echo b" and command["tail"] == "a\nb"


def test_human_output(tmp_project: Path, tmp_path: Path):
    result = preflight(
        str(tmp_project),
        "--",
        "echo 'listen EPERM /tmp/x.pipe'; exit 1",
        data_dir=tmp_path / "d",
    )

    assert result.exit_code == 1, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith(f"preflight {tmp_project} at ")
    assert "$ echo 'listen EPERM /tmp/x.pipe'; exit 1" in lines
    assert any(line.startswith("  exit 1 in ") and line.endswith("s (srt)") for line in lines)
    assert "  | listen EPERM /tmp/x.pipe" in lines
    assert f"  hint: {SOCKET_HINT}" in lines
    assert lines[-1] == "1 of 1 commands failed; worktree removed"


def test_without_a_command_exits_2(tmp_project: Path, tmp_path: Path):
    result = preflight(str(tmp_project), "--json", data_dir=tmp_path / "d")

    assert result.exit_code == 2
    assert "no command" in json.loads(result.output)["error"]


def test_not_a_repository_exits_1(tmp_path: Path):
    result = preflight(str(tmp_path), "--json", "--", "true", data_dir=tmp_path / "d")

    assert result.exit_code == 1
    assert "not a git repository" in json.loads(result.output)["error"]


def _linked_worktree(project: Path, tmp_path: Path) -> Path:
    linked = tmp_path / "run-worktree"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(linked), "HEAD"],
        cwd=project,
        capture_output=True,
        check=True,
    )
    return linked


def test_a_linked_worktree_is_refused_naming_the_primary_checkout(
    tmp_project: Path, tmp_path: Path
):
    linked = _linked_worktree(tmp_project, tmp_path)
    data_dir = tmp_path / "data"

    result = preflight(str(linked), "--", "true", data_dir=data_dir)

    assert result.exit_code == 1, result.output
    assert result.output.startswith("Error: ")
    message = result.output.removeprefix("Error: ")
    assert str(linked) in message and "linked worktree" in message
    assert "primary checkout's HEAD" in message
    assert f"helmraiser preflight {tmp_project.resolve()} " in message
    assert not data_dir.exists()


def test_a_linked_worktree_is_refused_in_json_and_from_a_subdirectory(
    tmp_project: Path, tmp_path: Path
):
    linked = _linked_worktree(tmp_project, tmp_path)
    (linked / "sub").mkdir()

    for target in (linked, linked / "sub"):
        result = preflight(str(target), "--json", "--", "true", data_dir=tmp_path / "data")

        assert result.exit_code == 1, result.output
        error = json.loads(result.output)["error"]
        assert "linked worktree" in error
        assert f"helmraiser preflight {tmp_project.resolve()} " in error


def test_the_main_checkout_still_runs_while_a_linked_worktree_exists(
    tmp_project: Path, tmp_path: Path
):
    _linked_worktree(tmp_project, tmp_path)

    for target in (tmp_project, tmp_project / ".git" / ".."):
        result = preflight(str(target), "--json", "--", "echo hi", data_dir=tmp_path / "data")

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["ok"] and payload["commands"][0]["tail"] == "hi"


def test_an_interrupt_exits_130_after_cleaning_up(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from helmraiser.loop import preflight as module

    seen: list[Path] = []

    def interrupted(step, cwd, *args, **kwargs):
        seen.append(cwd)
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "run_step", interrupted)

    result = preflight(str(tmp_project), "--", "true", data_dir=tmp_path / "d")

    assert result.exit_code == 130
    assert "interrupted" in result.output
    assert seen and not seen[0].exists()


@pytest.mark.parametrize("signature", ["EROFS", "Read-only file system"])
@pytest.mark.parametrize("machine", [False, True])
def test_setup_readonly_hint_with_hidden_signature(tmp_project, tmp_path, signature, machine):
    project_toml(tmp_project, f'[defaults]\nsetup = "echo {signature}; echo final; exit 3"\n')
    result = preflight(
        str(tmp_project),
        *(["--json"] if machine else []),
        "--tail",
        "1",
        "--",
        "echo should-not-run",
        data_dir=tmp_path / "data",
    )
    assert result.exit_code == 1
    if machine:
        payload = json.loads(result.output)
        assert payload["setup"]["hints"] == [SETUP_READONLY_HINT]
        assert payload["setup"]["tail"] == "final"
        assert payload["commands"] == []
    else:
        assert f"  hint: {SETUP_READONLY_HINT}" in result.output
        assert "  | final" in result.output
        assert f"  | {signature}" not in result.output
        assert "$ echo should-not-run" not in result.output


@pytest.mark.parametrize("case", ["host", "timeout", "unrelated"])
@pytest.mark.parametrize("machine", [False, True])
def test_setup_failure_without_hint(tmp_project, tmp_path, monkeypatch, case, machine):
    from helmraiser.loop import preflight as module
    from helmraiser.loop.shell import ShellResult

    project_toml(
        tmp_project,
        '[defaults]\nsetup = "echo EROFS; exit 3"\nrequire_approval = true\n'
        + ('[setup]\nsandbox = "host"\n' if case == "host" else ""),
    )
    if case != "host":
        result = (
            ShellResult(None, 0, "EROFS", timed_out=True)
            if case == "timeout"
            else ShellResult(3, 0, "unrelated")
        )
        monkeypatch.setattr(module, "run_shell", lambda *args: result)
    result = preflight(
        str(tmp_project), *(["--json"] if machine else []), "--", "true", data_dir=tmp_path / "data"
    )
    assert result.exit_code == 1
    if machine:
        assert "hints" not in json.loads(result.output)["setup"]
    else:
        assert "hint:" not in result.output
