"""Application configuration loaded from environment variables."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

# Fixed Newznab indexer category IDs for audio content.
_DEFAULT_CATEGORIES: list[tuple[int, str]] = [
    (3000, "Audio"),
    (3010, "Audio/MP3"),
    (3030, "Audio/Audiobook"),
    (3040, "Audio/Lossless"),
]

_REQUIRED_VARS = ("SLSKD_URL", "SLSKD_API_KEY")


@dataclass(frozen=True)
class Config:
    """Immutable application configuration.

    Attributes
    ----------
    slskd_url:
        Base URL of the slskd instance (required).
    slskd_api_key:
        API key for slskd authentication (required).
    categories:
        Newznab indexer category tuples ``(id, name)`` exposed by the bridge.
        Fixed set — not user-configurable.
    bridge_port:
        TCP port to bind waitress to (default 8765).
    search_timeout:
        Seconds to wait for a slskd search to complete (default 30).
    db_path:
        Path to the SQLite database file (default ``/data/bridge.db``).
    min_bitrate:
        Minimum acceptable bitrate in kbps; ``None`` means no filter.
    log_level:
        Root logging level name (e.g. ``INFO``, ``DEBUG``). Always a valid,
        upper-cased ``logging`` level name; defaults to ``INFO``.
    """

    slskd_url: str
    slskd_api_key: str
    categories: list[tuple[int, str]]
    bridge_port: int
    search_timeout: int
    db_path: str
    min_bitrate: int | None
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        """Build a Config from a mapping of environment variables.

        Raises
        ------
        ValueError
            If a required variable is missing. The message names the var.
        """
        for var in _REQUIRED_VARS:
            if var not in env or not env[var]:
                raise ValueError(f"Required environment variable {var!r} is not set")

        raw_min_bitrate = env.get("BRIDGE_MIN_BITRATE", "")
        min_bitrate = int(raw_min_bitrate) if raw_min_bitrate.strip() else None

        log_level = env.get("LOG_LEVEL", "").strip().upper() or "INFO"
        if log_level not in logging.getLevelNamesMapping():
            valid = ", ".join(sorted(logging.getLevelNamesMapping()))
            raise ValueError(
                f"Invalid LOG_LEVEL {log_level!r}; expected one of {valid}"
            )

        return cls(
            slskd_url=env["SLSKD_URL"],
            slskd_api_key=env["SLSKD_API_KEY"],
            categories=list(_DEFAULT_CATEGORIES),
            bridge_port=int(env.get("BRIDGE_PORT", "8765")),
            search_timeout=int(env.get("SLSKD_SEARCH_TIMEOUT", "30")),
            db_path=env.get("BRIDGE_DB_PATH", "/data/bridge.db"),
            min_bitrate=min_bitrate,
            log_level=log_level,
        )
