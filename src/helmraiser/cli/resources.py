"""Resource-limit helpers for the CLI commands."""

from __future__ import annotations

import logging

try:
    import resource
except ImportError:  # pragma: no cover - Windows has no resource module
    resource = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def disable_core_dumps() -> bool:
    """Lower this process's soft ``RLIMIT_CORE`` to 0, keeping the hard limit.

    ``helmraiser worker start`` runs ``helmraiser work`` as a child, so this covers the
    background worker too. Returns True when the limit was applied; a platform
    without the ``resource`` module or a failing ``setrlimit`` is logged at debug
    level and ignored, so it never stops the worker.
    """
    if resource is None:
        logger.debug("resource module unavailable; not disabling core dumps")
        return False
    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        resource.setrlimit(resource.RLIMIT_CORE, (0, hard))
    except (OSError, ValueError):
        logger.debug("could not disable core dumps", exc_info=True)
        return False
    return True
