"""Redaction of secret values from command output before a model reads it."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

from helmraiser.loop.services import HOST
from helmraiser.task.project import ServiceSpec, expand_env

MIN_SECRET_LENGTH = 6
# scheme://user:password@host: the password runs to the last "@" before a "/" or a space.
URL_CREDENTIALS = re.compile(r"(?P<head>[A-Za-z][A-Za-z0-9+.-]*://[^\s:/@]*):[^\s/]+@")


def redact_output(text: str, env: Mapping[str, str]) -> str:
    """Hide secrets in ``text``.

    Each value of ``env`` that is long enough becomes ``<redacted:NAME>`` (longer values
    first, so a value that contains another is replaced whole), then the password of any
    ``scheme://user:password@host`` URL becomes ``<redacted>``.
    """
    for name, value in sorted(env.items(), key=lambda item: (-len(item[1]), item[0])):
        if len(value) >= MIN_SECRET_LENGTH:
            text = text.replace(value, f"<redacted:{name}>")
    return URL_CREDENTIALS.sub(r"\g<head>:<redacted>@", text)


def step_environment_values(
    env: Mapping[str, str], services: Iterable[ServiceSpec], running: Iterable[Mapping[str, Any]]
) -> dict[str, str]:
    """The variables Helmraiser sets for verify and evidence steps, with the values the
    steps see: ``env`` expanded from the worker's environment, then what the ``running``
    services (``RunningService.as_dict()`` entries) provide, which win as in ``step_env``."""
    values = expand_env(dict(env), dict(os.environ))
    ports = {str(item["name"]): item["host_port"] for item in running}
    for service in services:
        if service.name in ports:
            for name, template in service.provides.items():
                values[name] = template.format(port=ports[service.name], host=HOST)
    return values
