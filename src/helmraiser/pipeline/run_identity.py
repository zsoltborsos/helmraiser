"""Run identity generation and validation."""

from __future__ import annotations

import uuid


def resolve_run_id(run_id: str | None = None) -> str:
    """Return a canonical UUID4 run ID, generating one when omitted."""
    if run_id is None:
        return str(uuid.uuid4())

    try:
        parsed = uuid.UUID(run_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("run_id must be a canonical UUID version 4") from exc
    if parsed.version != 4 or str(parsed) != run_id:
        raise ValueError("run_id must be a canonical UUID version 4")
    return run_id
