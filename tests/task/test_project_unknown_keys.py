"""Unknown project configuration keys fail loudly, with placement hints."""

from pathlib import Path

import pytest

from helmraiser.task.project import (
    ProjectDefaults,
    ServiceSpec,
    SetupConfig,
    load_project_defaults,
)


def write_project(project: Path, text: str) -> Path:
    path = project / ".helmraiser" / "project.toml"
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("table", ["", "defaults", "setup", "sandbox", "scout"])
@pytest.mark.parametrize("value", ['"typo"', "{}"])
def test_unknown_keys_are_named_and_sorted(tmp_path: Path, table: str, value: str):
    header = f"[{table}]\n" if table else ""
    path = write_project(tmp_path, f"{header}zebra = {value}\napple = {value}\n")

    with pytest.raises(ValueError) as exc:
        load_project_defaults(tmp_path)

    where = f"{path}: {table}" if table else str(path)
    assert str(exc.value) == f"{where}: unknown key(s) apple, zebra"


@pytest.mark.parametrize(
    "text, key",
    [
        ('worktree_dir = "/x"\n[defaults]\nsetup = "true"\n', "worktree_dir"),
        ('base = "main"\n', "base"),
        ('delivery = "pr"\n', "delivery"),
        ("require_approval = true\n", "require_approval"),
        ('[env]\nCUSTOM = "value"\n', "env"),
    ],
)
def test_misplaced_defaults_key_has_hint(tmp_path: Path, text: str, key: str):
    path = write_project(tmp_path, text)

    with pytest.raises(ValueError) as exc:
        load_project_defaults(tmp_path)

    message = str(exc.value)
    assert message.startswith(f"{path}: unknown key(s) {key}")
    assert f"{key} belongs under [defaults]" in message


def test_top_level_setup_command_has_hint(tmp_path: Path):
    write_project(tmp_path, 'setup = "make setup"\n')
    with pytest.raises(ValueError, match=r"setup.*\[defaults\]"):
        load_project_defaults(tmp_path)


def test_all_known_keys_load(tmp_path: Path):
    write_project(
        tmp_path,
        """max_runs_per_project = 2
[defaults]
setup = "true"
worktree_dir = "/x"
delivery = "pr"
base = "main"
require_approval = true
[defaults.env]
CUSTOM_VARIABLE_42 = "bar"
_private = "$HOME"
[setup]
sandbox = "host"
write = ["cache"]
allowed_domains = ["registry.npmjs.org"]
[sandbox]
deny_read = ["secrets"]
[scout]
exclude = ["vendor"]
[models]
implement = "p/m"
[[services]]
name = "postgres"
image = "postgres:16"
port = 5432
host_port = 15432
env = { POSTGRES_PASSWORD = "password" }
command = ["postgres"]
ready_timeout = 30
ready = ["pg_isready"]
provides = { DATABASE_URL = "postgres://{host}:{port}/postgres" }
""",
    )

    assert load_project_defaults(tmp_path) == ProjectDefaults(
        setup="true",
        worktree_dir="/x",
        delivery="pr",
        base="main",
        require_approval=True,
        env={"CUSTOM_VARIABLE_42": "bar", "_private": "$HOME"},
        setup_config=SetupConfig(
            sandbox="host", write=("cache",), allowed_domains=("registry.npmjs.org",)
        ),
        deny_read=("secrets",),
        scout_exclude=("vendor",),
        models={"implement": "p/m"},
        max_runs_per_project=2,
        services=(
            ServiceSpec(
                name="postgres",
                image="postgres:16",
                port=5432,
                host_port=15432,
                env={"POSTGRES_PASSWORD": "password"},
                command=("postgres",),
                ready_timeout=30,
                ready=("pg_isready",),
                provides={"DATABASE_URL": "postgres://{host}:{port}/postgres"},
            ),
        ),
    )
