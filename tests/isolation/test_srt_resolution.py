"""Resolve only fake tools: never execute the host's mise or srt."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

import helmraiser.config as config
from helmraiser.isolation import process
from helmraiser.isolation.process import SrtNotFoundError, reset_srt_cache, resolve_srt


def executable(path: Path, body: str = "exit 0") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def tools(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{tmp_path / 'shims'}:{tmp_path / 'bin'}")
    real = executable(tmp_path / "real" / "srt")
    log = tmp_path / "calls"
    mise = executable(
        tmp_path / "bin" / "mise",
        f'pwd -P >> {shlex.quote(str(log))}\nprintf "%s\\n" {shlex.quote(str(real))}',
    )
    shim = executable(tmp_path / "shims" / "srt", "exit 97")
    return real, mise, shim, log


def test_configured_path_wins(tmp_path, monkeypatch):
    custom = executable(tmp_path / "custom")
    monkeypatch.setattr(process.shutil, "which", lambda _: pytest.fail("discovery ran"))
    assert resolve_srt(str(custom)) == str(custom)


def test_configured_path_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    custom = executable(tmp_path / "custom")
    assert resolve_srt("~/custom") == str(custom)


@pytest.mark.parametrize("kind", ["relative", "missing", "nonexec", "directory", "empty"])
def test_invalid_configured_path(kind, tmp_path):
    path = tmp_path / kind
    if kind == "nonexec":
        path.write_text("not executable")
    if kind == "directory":
        path.mkdir()
    value = "relative" if kind == "relative" else "" if kind == "empty" else str(path)
    with pytest.raises(SrtNotFoundError) as exc:
        resolve_srt(value)
    assert "isolation.srt" in str(exc.value)
    assert repr(value) in str(exc.value)
    assert str(config.CONFIG_FILE) in str(exc.value)


def test_plain_path_binary_is_absolute(tools, monkeypatch):
    real, _, _, log = tools
    monkeypatch.setenv("PATH", f"{real.parent}:{os.environ['PATH']}")
    assert resolve_srt() == str(real)
    assert not log.exists()


def test_relative_path_entry_becomes_absolute(tmp_path, monkeypatch):
    real = executable(tmp_path / "bin" / "srt")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "bin")
    assert resolve_srt() == str(real)


def test_mocked_plain_path_need_not_exist(monkeypatch):
    monkeypatch.setattr(process.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert resolve_srt() == "/usr/bin/srt"


@pytest.mark.parametrize("symlink", [False, True])
def test_mise_shim_resolves_from_home(tools, monkeypatch, tmp_path, symlink):
    real, mise, _, log = tools
    if symlink:
        link = tmp_path / "links" / "srt"
        link.parent.mkdir()
        link.symlink_to(mise)
        monkeypatch.setenv("PATH", f"{link.parent}:{mise.parent}")
    assert resolve_srt() == str(real)
    assert log.read_text().splitlines() == [str(Path.home())]


def test_asdf_only_shims_path_is_plain(tools, monkeypatch):
    _, _, shim, log = tools
    monkeypatch.setenv("PATH", str(shim.parent))
    assert resolve_srt() == str(shim)
    assert not log.exists()


@pytest.mark.parametrize("kind", ["missing-mise", "failed", "empty", "missing", "nonexec", "shim"])
def test_mise_resolution_failure(tools, kind, tmp_path):
    _, mise, shim, _ = tools
    if kind == "missing-mise":
        shim.unlink()
        shim.symlink_to(mise)
        mise.rename(mise.with_name("hidden"))
        # A symlink target named mise must exist for shim detection.
        target = executable(tmp_path / "elsewhere" / "mise")
        shim.unlink()
        shim.symlink_to(target)
    else:
        output = tmp_path / "missing"
        if kind == "nonexec":
            output.write_text("not executable")
        if kind == "shim":
            output = shim
        body = (
            "echo diagnostic >&2; exit 1"
            if kind == "failed"
            else ("exit 0" if kind == "empty" else f"echo {shlex.quote(str(output))}")
        )
        executable(mise, body)
    with pytest.raises(SrtNotFoundError) as exc:
        resolve_srt()
    assert str(exc.value).startswith("srt not found on PATH")
    assert "isolation.srt" in str(exc.value)
    assert str(config.CONFIG_FILE) in str(exc.value)
    if kind == "failed":
        assert "diagnostic" in str(exc.value)


@pytest.mark.parametrize("error", [OSError("failed"), subprocess.TimeoutExpired("mise", 15)])
def test_mise_execution_error(tools, monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(process.subprocess, "run", fail)
    with pytest.raises(SrtNotFoundError, match="isolation.srt"):
        resolve_srt()


def test_missing_srt_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(SrtNotFoundError, match="srt not found on PATH.*isolation.srt"):
        resolve_srt()


def test_success_is_cached_and_reset_clears_cache(tools):
    real, _, _, log = tools
    assert resolve_srt() == resolve_srt() == str(real)
    assert len(log.read_text().splitlines()) == 1
    reset_srt_cache()
    assert resolve_srt() == str(real)
    assert len(log.read_text().splitlines()) == 2


def test_cache_is_keyed_by_configured(tools, tmp_path):
    real, _, _, log = tools
    first = executable(tmp_path / "first")
    second = executable(tmp_path / "second")
    for _ in range(2):
        assert resolve_srt(str(first)) == str(first)
        assert resolve_srt(str(second)) == str(second)
        assert resolve_srt() == str(real)
    assert len(log.read_text().splitlines()) == 1


def test_failed_resolution_is_not_cached(tools):
    real, mise, _, _ = tools
    original = mise.read_text()
    executable(mise, "exit 1")
    with pytest.raises(SrtNotFoundError):
        resolve_srt()
    mise.write_text(original)
    assert resolve_srt() == str(real)


@pytest.mark.parametrize("deleted", [True, False])
def test_invalid_cached_executable_is_resolved_again(tools, tmp_path, deleted):
    real, mise, _, log = tools
    assert resolve_srt() == str(real)
    if deleted:
        real.unlink()
    else:
        real.chmod(0o644)
    replacement = executable(tmp_path / "new" / "srt")
    executable(mise, f"pwd -P >> {shlex.quote(str(log))}; echo {shlex.quote(str(replacement))}")
    assert resolve_srt() == str(replacement)
    assert len(log.read_text().splitlines()) == 2
