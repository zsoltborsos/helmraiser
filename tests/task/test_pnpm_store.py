"""Read-only pnpm store diagnostics, independent of installed package managers."""

from dataclasses import replace
from pathlib import Path

import pytest

import helmraiser.task.project as project_module
from helmraiser.task.project import ProjectDefaults, SetupConfig, _device, pnpm_store_check


@pytest.mark.parametrize("setup", [None, "uv sync", "mypnpmtool"])
def test_non_pnpm_setup_without_store_is_ignored(tmp_path: Path, setup: str | None) -> None:
    assert (
        pnpm_store_check(
            tmp_path,
            ProjectDefaults(setup=setup),
            tmp_path / "worktrees",
            {"npm_config_store_dir": str(tmp_path / "inherited")},
        )
        is None
    )


@pytest.mark.parametrize("setup", ["pnpm install", "echo ready && pnpm install"])
def test_pnpm_setup_without_named_store_warns(tmp_path: Path, setup: str) -> None:
    result = pnpm_store_check(tmp_path, ProjectDefaults(setup=setup), tmp_path, {})
    assert result is not None
    assert result["store"] is result["same_filesystem"] is result["in_setup_write"] is None
    assert len(result["warnings"]) == 1
    assert "npm_config_store_dir" in result["warnings"][0]
    assert "pnpm under srt cannot find a store at the filesystem root" in result["warnings"][0]


@pytest.mark.parametrize(
    "name",
    [
        "npm_config_store_dir",
        "pnpm_config_store_dir",
        "NPM_CONFIG_STORE_DIR",
        "PNPM_CONFIG_STORE_DIR",
    ],
)
def test_named_store_is_ready(tmp_path: Path, name: str) -> None:
    store = tmp_path / "store"
    defaults = ProjectDefaults(
        env={name: str(store)}, setup_config=SetupConfig(write=(str(store),))
    )
    assert pnpm_store_check(tmp_path, defaults, tmp_path / "worktrees", {}) == {
        "store": str(store),
        "same_filesystem": True,
        "in_setup_write": True,
        "warnings": [],
    }
    assert not store.exists()


@pytest.mark.parametrize("reverse", [False, True])
def test_first_named_store_wins(tmp_path: Path, reverse: bool) -> None:
    entries = [("npm_config_store_dir", "first"), ("PNPM_CONFIG_STORE_DIR", "second")]
    if reverse:
        entries.reverse()
    defaults = ProjectDefaults(env=dict(entries), setup_config=SetupConfig(sandbox="host"))
    result = pnpm_store_check(tmp_path, defaults, tmp_path, {})
    assert result is not None
    assert result["store"] == str(tmp_path / entries[0][1])


@pytest.mark.parametrize("value", ["$HOME/store", "${HOME}/store", "store"])
def test_store_and_write_paths_expand_from_supplied_environment(tmp_path: Path, value: str) -> None:
    environ = {"HOME": str(tmp_path), "npm_config_store_dir": "/conflicting"}
    original = dict(environ)
    defaults = ProjectDefaults(
        env={"npm_config_store_dir": value},
        setup_config=SetupConfig(write=("$npm_config_store_dir",)),
    )
    result = pnpm_store_check(tmp_path, defaults, tmp_path, environ)
    assert result is not None
    assert result["store"] == str(tmp_path / "store")
    assert result["warnings"] == []
    assert environ == original
    assert defaults.env == {"npm_config_store_dir": value}
    assert defaults.setup_config.write == ("$npm_config_store_dir",)


@pytest.mark.parametrize(
    ("store", "write", "covered"),
    [
        ("store", ("store",), True),
        ("store/v10", ("store",), True),
        ("store", (), False),
        ("store-other", ("store",), False),
    ],
)
def test_store_write_coverage(
    tmp_path: Path, store: str, write: tuple[str, ...], covered: bool
) -> None:
    defaults = ProjectDefaults(
        env={"npm_config_store_dir": store}, setup_config=SetupConfig(write=write)
    )
    result = pnpm_store_check(tmp_path, defaults, tmp_path, {})
    assert result is not None
    assert result["in_setup_write"] is covered
    assert len(result["warnings"]) == (0 if covered else 1)
    if not covered:
        assert "setup under srt cannot write" in result["warnings"][0]
        assert "[setup].write" in result["warnings"][0]


def test_host_setup_does_not_require_write_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "store"
    defaults = ProjectDefaults(
        env={"npm_config_store_dir": str(store)}, setup_config=SetupConfig(sandbox="host")
    )
    result = pnpm_store_check(tmp_path, defaults, tmp_path, {})
    assert result is not None
    assert result["in_setup_write"] is None
    assert result["warnings"] == []
    monkeypatch.setattr(project_module, "_device", lambda path: 2 if path == store else 1)
    result = pnpm_store_check(tmp_path, defaults, tmp_path, {})
    assert result is not None
    assert result["in_setup_write"] is None
    assert len(result["warnings"]) == 1
    assert "filesystem" in result["warnings"][0]


@pytest.mark.parametrize("allow_write", [False, True])
def test_different_filesystem_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_write: bool
) -> None:
    store, worktrees = tmp_path / "store", tmp_path / "configured-worktrees"
    defaults = ProjectDefaults(env={"npm_config_store_dir": str(store)})
    if allow_write:
        defaults = replace(defaults, setup_config=SetupConfig(write=(str(store),)))
    monkeypatch.setattr(project_module, "_device", lambda path: 2 if path == worktrees else 1)
    result = pnpm_store_check(tmp_path, defaults, worktrees, {})
    assert result is not None
    assert result["same_filesystem"] is False
    assert str(worktrees) in result["warnings"][0]
    assert "different filesystem" in result["warnings"][0]
    assert "pnpm copies instead of hard-linking" in result["warnings"][0]
    assert len(result["warnings"]) == (1 if allow_write else 2)
    if not allow_write:
        assert "[setup].write" in result["warnings"][1]


def test_device_uses_nearest_existing_ancestor(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "nested" / "store"
    assert _device(tmp_path) == tmp_path.stat().st_dev
    assert _device(missing) == tmp_path.stat().st_dev
    assert not (tmp_path / "missing").exists()
