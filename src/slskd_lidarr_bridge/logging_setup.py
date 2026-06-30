"""Process-wide logging configuration.

A single ``configure_logging`` call, made once at the composition root
(``main``), wires up the standard-library ``logging`` module for the whole
process. It configures the *root* logger so that the bridge's own logs and its
dependencies' (httpx, waitress) all honour the same level — set via the
``LOG_LEVEL`` environment variable (see :class:`~slskd_lidarr_bridge.config.Config`).
"""

from __future__ import annotations

import logging

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging(level: str) -> None:
    """Configure the root logger for the process.

    Args:
        level: a ``logging`` level name (e.g. ``"INFO"``, ``"DEBUG"``). Assumed
            already validated by :meth:`Config.from_env`.

    ``force=True`` makes this authoritative: it replaces any handler a
    dependency may have installed on import, so the bridge's format and level
    always win. Intended to be called exactly once, at startup.
    """
    logging.basicConfig(level=level, format=_LOG_FORMAT, force=True)
