"""pi adapter package — Helmraiser's only executor (spec §3)."""

from helmraiser.pi.adapter import (
    PiJsonAdapter,
    PiVersionError,
    check_auth,
    pi_version,
    session_id_for,
)
from helmraiser.pi.protocol import (
    OnEvent,
    PiAdapter,
    PiEvent,
    PiRequest,
    PiSessionResult,
    Usage,
)

__all__ = [
    "OnEvent",
    "PiAdapter",
    "PiEvent",
    "PiJsonAdapter",
    "PiRequest",
    "PiSessionResult",
    "PiVersionError",
    "Usage",
    "check_auth",
    "pi_version",
    "session_id_for",
]
