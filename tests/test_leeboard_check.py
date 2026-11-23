import os
import subprocess
from pathlib import Path

import pytest

from helmraiser import leeboard


def test_leeboard_fake_defaults_and_environment(fake_lb):
    result = subprocess.run([str(fake_lb.bin), "brief"], capture_output=True)
    assert result.returncode == 0
    assert result.stdout == result.stderr == b""
    assert fake_lb.calls()[0]["env"] == {
        "LEEBOARD_HOME": os.environ["LEEBOARD_HOME"],
        "LEEBOARD_HARNESS": None,
        "LEEBOARD_SESSION": None,
    }


def test_leeboard_fake_project_override_and_output(fake_lb, tmp_path):
    fake_lb.write_script(
        {
            "check": {"exit": 1},
            f"check@{tmp_path}": {"exit": 3, "stdout": "held", "stderr": "detail"},
        }
    )
    result = subprocess.run(
        [str(fake_lb.bin), "check", "--cwd", str(tmp_path)], capture_output=True, text=True
    )
    assert (result.returncode, result.stdout, result.stderr) == (3, "held", "detail")
    assert fake_lb.calls()[0]["cwd"] == os.getcwd()


@pytest.mark.parametrize(
    "code,output,status",
    [
        (0, "", "clear"),
        (0, "[]", "clear"),
        (0, "oops", "unavailable"),
        (0, "{}", "unavailable"),
        (3, "oops", "blocked"),
        (3, "{}", "blocked"),
        (3, "", "blocked"),
        (1, "", "unavailable"),
        (6, "", "unavailable"),
    ],
)
def test_leeboard_exit_matrix(fake_lb, tmp_path, code, output, status):
    fake_lb.write_script(
        {"check": {"exit": code, "stdout": output, "stderr": "lb: broken\nsecret"}}
    )
    check = leeboard.check_hold(tmp_path)
    assert check.status == status
    call = fake_lb.calls()[0]
    assert call["argv"] == ["check", "--blocks", "helmraiser-run", "--cwd", str(tmp_path), "--json"]
    assert call["env"] == {
        "LEEBOARD_HOME": os.environ["LEEBOARD_HOME"],
        "LEEBOARD_HARNESS": "helmraiser",
        "LEEBOARD_SESSION": None,
    }
    assert "secret" not in check.detail
    if status == "blocked":
        assert check.skip_text().startswith("leeboard hold: ")
        assert check.skip_payload()["text"] == output


def test_leeboard_holds_and_formatting(fake_lb, tmp_path):
    import json

    holds = [
        {
            "id": "one",
            "title": "No runs",
            "release": "tunnel closed",
            "author": {"harness": "user"},
        },
        {"id": "two", "title": "Wait", "until": "tomorrow"},
    ]
    fake_lb.write_script({"check": {"exit": 3, "stdout": json.dumps(holds)}})
    check = leeboard.check_hold(tmp_path)
    assert check.holds == holds
    assert check.skip_text() == "leeboard hold: [one] No runs (release: tunnel closed); [two] Wait"
    assert check.skip_payload() == {
        "leeboard_hold": [
            {"id": "one", "title": "No runs", "release": "tunnel closed"},
            {"id": "two", "title": "Wait", "until": "tomorrow"},
        ]
    }
    long = leeboard.HoldCheck("blocked", text="\n" + "x" * 400)
    assert len(long.skip_text()) == 300
    assert long.skip_text().endswith("…")
    assert len(long.skip_payload()["text"]) == 300


def test_explicit_hold_tags(fake_lb, tmp_path):
    fake_lb.write_script(
        {
            "check": {"exit": 1},
            "check#push": {"exit": 0},
            "check#merge": {"exit": 3, "stdout": "global"},
            f"check@{tmp_path}#merge": {"exit": 3, "stdout": "project"},
        }
    )
    assert leeboard.check_hold(tmp_path, blocks="push").status == "clear"
    held = leeboard.check_hold(tmp_path, blocks="merge")
    assert held.status == "blocked" and held.text == "project"
    assert [call["argv"][2] for call in fake_lb.calls()] == ["push", "merge"]
    hold = leeboard.HoldCheck(
        "blocked", holds=[{"id": "one", "title": "x" * 400, "release": "close", "until": "soon"}]
    )
    assert len(hold.full_text()) > 400
    assert "release: close" in hold.full_text() and "until: soon" in hold.full_text()


def test_leeboard_missing_and_oserror(monkeypatch, tmp_path):
    monkeypatch.setattr(leeboard, "find_lb", lambda: None)
    assert leeboard.check_hold(tmp_path) == leeboard.HoldCheck("clear")
    monkeypatch.setattr(leeboard, "find_lb", lambda: "/nonexistent/lb")
    assert leeboard.check_hold(tmp_path).status == "unavailable"


def test_leeboard_timeout(fake_lb, tmp_path, monkeypatch):
    fake_lb.write_script({"check": {"sleep": 1}})
    monkeypatch.setattr(leeboard, "CHECK_TIMEOUT_S", 0.2)
    assert leeboard.check_hold(tmp_path).detail == "timed out after 0.2s"


def test_leeboard_install_root_resolves_symlink(monkeypatch, tmp_path):
    package = tmp_path / "package"
    (package / "dist").mkdir(parents=True)
    (package / "package.json").write_text("{}")
    executable = package / "dist" / "cli.js"
    executable.touch()
    link = tmp_path / "lb"
    link.symlink_to(executable)
    monkeypatch.setattr(leeboard, "find_lb", lambda: str(link))
    assert leeboard.lb_install_root() == package


def test_leeboard_install_root_uses_nearest_package(monkeypatch, tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    (tmp_path / "package.json").write_text("{}")
    (nested / "package.json").write_text("{}")
    executable = nested / "cli.js"
    executable.touch()
    monkeypatch.setattr(leeboard, "find_lb", lambda: str(executable))
    assert leeboard.lb_install_root() == nested


def test_leeboard_install_root_missing_lb(monkeypatch):
    monkeypatch.setattr(leeboard, "find_lb", lambda: None)
    assert leeboard.lb_install_root() is None


def test_leeboard_install_root_without_package(monkeypatch, tmp_path):
    executable = tmp_path / "lb"
    executable.touch()
    monkeypatch.setattr(leeboard, "find_lb", lambda: str(executable))
    assert leeboard.lb_install_root() is None


def test_leeboard_identity_home_and_subprocess(monkeypatch, tmp_path):
    assert leeboard.leeboard_identity(None) == {"LEEBOARD_HARNESS": "helmraiser"}
    assert leeboard.leeboard_identity("run")["LEEBOARD_SESSION"] == "run"
    assert leeboard.leeboard_home({}) == Path.home() / ".leeboard"
    assert leeboard.leeboard_home({"LEEBOARD_HOME": ""}) == Path.home() / ".leeboard"
    assert leeboard.leeboard_home({"LEEBOARD_HOME": "relative"}) == Path.cwd() / "relative"
    assert leeboard.leeboard_home({"LEEBOARD_HOME": "~/notes"}) == Path.home() / "notes"
    monkeypatch.setattr(leeboard, "find_lb", lambda: "lb")

    def run(argv, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] == 5.0
        assert kwargs["shell"] is False
        assert kwargs["env"]["LEEBOARD_SESSION"] == "run"
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(leeboard.subprocess, "run", run)
    assert leeboard.check_hold(tmp_path, leeboard.leeboard_identity("run")).status == "clear"
    assert "LEEBOARD_SESSION" not in os.environ


@pytest.mark.parametrize("recovery", ["clear", "blocked"])
def test_leeboard_gate_cache_and_warning_streak(monkeypatch, recovery):
    results = iter(
        [
            leeboard.HoldCheck("unavailable", detail="missing"),
            leeboard.HoldCheck("unavailable", detail="missing"),
            leeboard.HoldCheck("unavailable", detail="broken"),
            leeboard.HoldCheck(recovery),
            leeboard.HoldCheck("unavailable", detail="broken"),
        ]
    )
    calls = []

    def check(project):
        calls.append(project)
        return next(results)

    monkeypatch.setattr(leeboard, "check_hold", check)
    warnings = []
    gate = leeboard.HoldGate(warnings.append)
    for expected in [1, 1, 2, 2, 3]:
        gate.check("/a")
        gate.check("/a")
        assert len(warnings) == expected
        gate.clear()
    assert len(calls) == 5
    assert all("tasks are scheduled without it" in w for w in warnings)


def test_leeboard_gate_project_cache(fake_lb, tmp_path):
    gate = leeboard.HoldGate(lambda text: pytest.fail(text))
    gate.check(str(tmp_path))
    gate.check(str(tmp_path))
    gate.check(str(tmp_path / "other"))
    assert len(fake_lb.calls()) == 2
    gate.clear()
    gate.check(str(tmp_path))
    assert len(fake_lb.calls()) == 3
