"""Helmraiser-owned package caches, prepared only for setup running inside srt."""

import os
from contextlib import ExitStack
from pathlib import Path

from helmraiser.task.model import project_slug
from helmraiser.task.project import SetupConfig, setup_write_paths


def prepare_setup(
    project: Path, data_dir: Path, setup: SetupConfig, environment: dict[str, str]
) -> tuple[dict[str, str], tuple[Path, ...], Path]:
    """Copy setup's environment and prepare private cache roots on the host.

    Callers must gate this to sandboxed setup, never host setup or other stages.
    Existing environment values (including empty values) take precedence.
    """
    cache = data_dir.expanduser().resolve() / "setup-cache" / project_slug(project)
    # The cache is writable by setup, so its entries may be replaced between runs.
    # Walk from / with directory fds: never follow a replaced symlink when
    # opening or changing permissions on the host.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        parent_fd = os.open("/", flags)
        stack.callback(os.close, parent_fd)

        def enter(name: str, *, private: bool) -> int:
            nonlocal parent_fd
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                created = False
            fd = os.open(name, flags, dir_fd=parent_fd)
            stack.callback(os.close, fd)
            if created or private:
                os.fchmod(fd, 0o700)
            parent_fd = fd
            return fd

        for index, part in enumerate(cache.parts[1:], start=1):
            enter(part, private=index >= len(cache.parts) - 2)
        cache_fd = parent_fd
        for name in ("npm", "pip"):
            parent_fd = cache_fd
            enter(name, private=True)
    env = environment.copy()
    env.setdefault("npm_config_cache", str(cache / "npm"))
    env.setdefault("PIP_CACHE_DIR", str(cache / "pip"))
    write = tuple(dict.fromkeys((*setup_write_paths(setup, project, env), cache)))
    return env, write, cache
