"""Redaction of secret values from step output before the intake model reads it."""

from __future__ import annotations

import pytest

from helmraiser.loop.redact import redact_output, step_environment_values
from helmraiser.task.project import ServiceSpec


def test_env_values_are_replaced_by_their_names():
    env = {"API_TOKEN": "tok-abc123xyz", "REGION": "eu-west-1"}

    redacted = redact_output("auth tok-abc123xyz failed in eu-west-1", env)

    assert redacted == "auth <redacted:API_TOKEN> failed in <redacted:REGION>"


def test_every_occurrence_is_replaced():
    redacted = redact_output(
        "tok-abc123xyz and again tok-abc123xyz", {"API_TOKEN": "tok-abc123xyz"}
    )

    assert redacted == "<redacted:API_TOKEN> and again <redacted:API_TOKEN>"


def test_values_shorter_than_six_characters_are_left_alone():
    env = {"MODE": "debug", "LEVEL": "info", "FLAG": "1", "PORT": ""}

    assert redact_output("debug info 1 on", env) == "debug info 1 on"
    assert redact_output("abcdef", {"SIX": "abcdef"}) == "<redacted:SIX>"


def test_a_value_inside_a_longer_value_does_not_split_the_longer_one():
    env = {"HOST": "db.internal", "DSN": "db.internal:5432"}

    assert redact_output("connect db.internal:5432 or db.internal", env) == (
        "connect <redacted:DSN> or <redacted:HOST>"
    )


def test_output_without_secrets_is_unchanged():
    assert redact_output("3 passed in 0.1s", {}) == "3 passed in 0.1s"


def test_url_credentials_are_redacted_without_any_env():
    text = "connecting to postgresql://app:s3cretpw@127.0.0.1:5432/db failed"

    assert redact_output(text, {}) == (
        "connecting to postgresql://app:<redacted>@127.0.0.1:5432/db failed"
    )


def test_url_credentials_without_a_user_or_with_an_at_sign_in_the_password():
    assert redact_output("redis://:pw12345@cache:6379/0", {}) == "redis://:<redacted>@cache:6379/0"
    assert redact_output("amqp://u:p@ss@broker:5672", {}) == "amqp://u:<redacted>@broker:5672"


def test_every_url_on_a_line_is_redacted():
    text = "https://a:one@x.test/p https://b:two@y.test/q"

    assert redact_output(text, {}) == "https://a:<redacted>@x.test/p https://b:<redacted>@y.test/q"


def test_urls_without_a_password_are_left_alone():
    for text in (
        "https://example.com/a@b",
        "ssh://git@github.com/org/repo.git",
        "git@github.com:org/repo.git",
        "http://localhost:8080/health",
    ):
        assert redact_output(text, {}) == text


def test_step_environment_values_expand_references_to_the_workers_environment(monkeypatch):
    monkeypatch.setenv("WORKER_SECRET", "hunter2hunter2")
    env = {
        "LITERAL": "plain-value",
        "FROM_WORKER": "$WORKER_SECRET",
        "UNKNOWN": "$NOT_SET_ANYWHERE",
    }

    assert step_environment_values(env, (), ()) == {
        "LITERAL": "plain-value",
        "FROM_WORKER": "hunter2hunter2",
        "UNKNOWN": "$NOT_SET_ANYWHERE",
    }


@pytest.fixture
def postgres() -> ServiceSpec:
    return ServiceSpec(
        name="postgres",
        image="postgres:16",
        port=5432,
        provides={
            "DATABASE_URL": "postgresql://postgres:helmraiser@{host}:{port}/postgres",
            "PGPASSWORD": "helmraiser-literal",
        },
    )


def test_step_environment_values_include_what_running_services_provide(postgres):
    values = step_environment_values({}, (postgres,), [{"name": "postgres", "host_port": 54321}])

    assert values == {
        "DATABASE_URL": "postgresql://postgres:helmraiser@127.0.0.1:54321/postgres",
        "PGPASSWORD": "helmraiser-literal",
    }


def test_a_service_value_wins_over_an_env_value_of_the_same_name(postgres):
    env = {"PGPASSWORD": "from-the-project", "OTHER": "kept-as-it-is"}

    values = step_environment_values(env, (postgres,), [{"name": "postgres", "host_port": 5432}])

    assert values["PGPASSWORD"] == "helmraiser-literal"
    assert values["OTHER"] == "kept-as-it-is"


def test_a_service_that_is_not_running_provides_nothing(postgres):
    assert step_environment_values({"A": "value-a"}, (postgres,), []) == {"A": "value-a"}
