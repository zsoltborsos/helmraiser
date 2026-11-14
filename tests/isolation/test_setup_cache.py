"""Host-side setup preparation; no sandbox or package manager is started."""

import stat
from pathlib import Path

import pytest

from helmraiser.isolation.setup import prepare_setup
from helmraiser.task.model import project_slug
from helmraiser.task.project import SetupConfig


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"npm_config_cache": ""},
        {"PIP_CACHE_DIR": "/worker/pip"},
        {"npm_config_cache": "/task/npm", "PIP_CACHE_DIR": "/project/pip"},
    ],
)
def test_prepare_setup(tmp_path: Path, overrides: dict[str, str]) -> None:
    data = tmp_path / "missing" / "data"
    project = tmp_path / "repo"
    env = {"HOME": str(tmp_path), **overrides}
    original = env.copy()
    setup = SetupConfig(write=("~/extra", "$npm_config_cache", "~/extra"))
    prepared, write, cache = prepare_setup(project, data, setup, env)
    assert cache == data / "setup-cache" / project_slug(project)
    assert env == original
    assert prepared == {
        **env,
        "npm_config_cache": overrides.get("npm_config_cache", str(cache / "npm")),
        "PIP_CACHE_DIR": overrides.get("PIP_CACHE_DIR", str(cache / "pip")),
    }
    assert write[0] == tmp_path / "extra"
    assert write[-1] == cache
    assert len(write) == len(set(write))
    for path in (data.parent, data, cache.parent, cache, cache / "npm", cache / "pip"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    (cache / "npm" / "kept").write_text("cached")
    cache.chmod(0o755)
    assert prepare_setup(project, data, setup, env) == (prepared, write, cache)
    assert (cache / "npm" / "kept").read_text() == "cached"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    other = prepare_setup(tmp_path / "other", data, setup, env)[2]
    assert other != cache


def test_existing_ancestors_are_not_chmodded(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    prepare_setup(tmp_path / "repo", data, SetupConfig(), {})
    assert stat.S_IMODE(data.stat().st_mode) == 0o755


@pytest.mark.parametrize("entry", ["setup-cache", "project", "npm", "pip"])
@pytest.mark.parametrize("target_type", ["directory", "file"])
def test_cache_symlink_does_not_change_external_target(
    tmp_path: Path, entry: str, target_type: str
) -> None:
    data = tmp_path / "data"
    project = tmp_path / "repo"
    cache = data / "setup-cache" / project_slug(project)
    destination = {
        "setup-cache": cache.parent,
        "project": cache,
        "npm": cache / "npm",
        "pip": cache / "pip",
    }[entry]
    destination.parent.mkdir(parents=True)
    external = tmp_path / "external"
    if target_type == "directory":
        external.mkdir(mode=0o755)
        (external / "sentinel").write_text("unchanged")
    else:
        external.write_text("unchanged")
        external.chmod(0o644)
    original_mode = stat.S_IMODE(external.stat().st_mode)
    destination.symlink_to(external)

    with pytest.raises(OSError):
        prepare_setup(project, data, SetupConfig(), {})

    assert destination.is_symlink()
    assert stat.S_IMODE(external.stat().st_mode) == original_mode
    if target_type == "directory":
        assert (external / "sentinel").read_text() == "unchanged"
    else:
        assert external.read_text() == "unchanged"


@pytest.mark.parametrize("entry", ["project", "npm", "pip"])
def test_cache_file_is_rejected(tmp_path: Path, entry: str) -> None:
    cache = tmp_path / "data" / "setup-cache" / project_slug(tmp_path / "repo")
    destination = cache if entry == "project" else cache / entry
    destination.parent.mkdir(parents=True)
    destination.write_text("untouched")
    with pytest.raises(OSError):
        prepare_setup(tmp_path / "repo", tmp_path / "data", SetupConfig(), {})
    assert destination.read_text() == "untouched"
