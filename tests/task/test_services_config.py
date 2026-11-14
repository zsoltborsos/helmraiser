"""``[[services]]`` in ``.helmraiser/project.toml``: parsing and strict validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from helmraiser.task.project import ServiceSpec, load_project_defaults

POSTGRES = """
[[services]]
name = "postgres"
image = "postgres:16"
env = { POSTGRES_PASSWORD = "helmraiser", POSTGRES_DB = "$DB_NAME" }
port = 5432
command = ["postgres", "-c", "fsync=off"]
ready_timeout = 90
ready = ["pg_isready", "-U", "postgres"]
provides = { DATABASE_URL = "postgresql://postgres:helmraiser@{host}:{port}/app" }

[[services]]
name = "qdrant"
image = "qdrant/qdrant:latest"
port = 6333
provides = { QDRANT_URL = "http://{host}:{port}" }
"""


def write(project: Path, text: str) -> None:
    path = project / ".helmraiser" / "project.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_services_are_parsed_in_order(tmp_path: Path):
    write(tmp_path, POSTGRES)

    services = load_project_defaults(tmp_path).services

    assert services == (
        ServiceSpec(
            name="postgres",
            image="postgres:16",
            port=5432,
            env={"POSTGRES_PASSWORD": "helmraiser", "POSTGRES_DB": "$DB_NAME"},
            command=("postgres", "-c", "fsync=off"),
            ready_timeout=90,
            provides={"DATABASE_URL": "postgresql://postgres:helmraiser@{host}:{port}/app"},
            ready=("pg_isready", "-U", "postgres"),
        ),
        ServiceSpec(
            name="qdrant",
            image="qdrant/qdrant:latest",
            port=6333,
            provides={"QDRANT_URL": "http://{host}:{port}"},
        ),
    )
    assert services[1].ready_timeout == 60 and services[1].command == ()
    assert services[1].ready == ()


def test_no_services_by_default(tmp_path: Path):
    write(tmp_path, "[defaults]\nsetup = 'true'\n")
    assert load_project_defaults(tmp_path).services == ()


BASE = 'name = "db"\nimage = "postgres:16"\nport = 5432\n'


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (BASE + "volume = 'x'\n", "unknown key(s) volume"),
        ('image = "postgres:16"\nport = 5432\n', "name must be a slug"),
        ('name = "Postgres"\nimage = "x"\nport = 1\n', "name must be a slug"),
        ('name = "db-"\nimage = "x"\nport = 1\n', "name must be a slug"),
        ('name = "' + "a" * 33 + '"\nimage = "x"\nport = 1\n', "name must be a slug"),
        ('name = "db"\nimage = " "\nport = 1\n', "services.db.image must be a non-empty string"),
        ('name = "db"\nport = 1\n', "services.db.image must be a non-empty string"),
        ('name = "db"\nimage = "x"\nport = 0\n', "port must be an integer from 1 to 65535"),
        ('name = "db"\nimage = "x"\nport = 65536\n', "port must be an integer from 1 to 65535"),
        ('name = "db"\nimage = "x"\nport = "5432"\n', "port must be an integer from 1 to 65535"),
        ('name = "db"\nimage = "x"\nport = true\n', "port must be an integer from 1 to 65535"),
        ('name = "db"\nimage = "x"\n', "port must be an integer from 1 to 65535"),
        (BASE + "host_port = 0\n", "host_port must be an integer from 1 to 65535"),
        (BASE + 'host_port = "55440"\n', "host_port must be an integer from 1 to 65535"),
        (BASE + 'command = "postgres"\n', "command must be a list of strings"),
        (BASE + "command = [1]\n", "command must be a list of strings"),
        (BASE + "ready_timeout = 0\n", "ready_timeout must be a positive integer"),
        (BASE + "ready_timeout = 1.5\n", "ready_timeout must be a positive integer"),
        (BASE + 'ready = "pg_isready"\n', "ready must be a non-empty list of strings"),
        (BASE + "ready = []\n", "ready must be a non-empty list of strings"),
        (BASE + "ready = [1]\n", "ready must be a non-empty list of strings"),
        (BASE + 'ready = [" "]\n', "ready must be a non-empty list of strings"),
        (BASE + 'env = "X=1"\n', "services.db.env must be a table"),
        (BASE + "env = { X = 1 }\n", "services.db.env.X must be a string"),
        (BASE + 'env = { "1X" = "1" }\n', "'1X' is not a valid environment variable name"),
        (BASE + 'provides = { URL = "{port" }\n', "services.db.provides.URL"),
        (BASE + 'provides = { URL = "{user}:{port}" }\n', "unknown placeholder 'user'"),
        (BASE + 'provides = { URL = "{0}" }\n', "unknown placeholder '0'"),
        (BASE + "provides = { URL = 5 }\n", "services.db.provides.URL must be a string"),
    ],
)
def test_invalid_service_is_rejected(tmp_path: Path, body: str, message: str):
    write(tmp_path, "[[services]]\n" + body)
    with pytest.raises(ValueError, match="project.toml") as info:
        load_project_defaults(tmp_path)
    assert message in str(info.value)


def test_services_must_be_an_array_of_tables(tmp_path: Path):
    write(tmp_path, "[services]\nname = 'db'\n")
    with pytest.raises(ValueError, match=r"array of tables \(\[\[services\]\]\)"):
        load_project_defaults(tmp_path)


def test_names_are_unique(tmp_path: Path):
    write(tmp_path, f"[[services]]\n{BASE}\n[[services]]\n{BASE}")
    with pytest.raises(ValueError, match="duplicate service name db"):
        load_project_defaults(tmp_path)


def test_a_fixed_host_port_is_parsed_and_unique(tmp_path: Path):
    write(tmp_path, f"[[services]]\n{BASE}host_port = 55440\n")
    (service,) = load_project_defaults(tmp_path).services
    assert service.host_port == 55440
    write(
        tmp_path,
        f"[[services]]\n{BASE}host_port = 55440\n"
        "[[services]]\nname = 'cache'\nimage = 'redis:7'\nport = 6379\nhost_port = 55440\n",
    )
    with pytest.raises(ValueError, match="duplicate host_port 55440"):
        load_project_defaults(tmp_path)


def test_a_variable_is_provided_by_one_service_only(tmp_path: Path):
    write(
        tmp_path,
        f"[[services]]\n{BASE}provides = {{ URL = '{{port}}' }}\n"
        "[[services]]\nname = 'cache'\nimage = 'redis:7'\nport = 6379\n"
        "provides = { URL = '{port}' }\n",
    )
    with pytest.raises(ValueError, match="duplicate provided variable URL"):
        load_project_defaults(tmp_path)
