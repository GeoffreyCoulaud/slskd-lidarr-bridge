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
    stall_timeout:
        Seconds a download may make no progress before it is reported failed;
        ``<= 0`` disables stall detection. Defaults to 1800 (30 min).
    max_retries:
        Times a failed transfer is re-enqueued before the download is reported
        failed to Lidarr; ``0`` fails on the first error. Defaults to 1.
    log_level:
        Root logging level name (e.g. ``INFO``, ``DEBUG``). Always a valid,
        upper-cased ``logging`` level name; defaults to ``INFO``.
    min_results:
        Stop issuing fallback candidates once this many distinct releases have
        accumulated. Defaults to 3.
    search_budget:
        Wall-clock seconds gating fallback candidates (the primary always runs);
        kept under Lidarr's hardcoded 100 s indexer-request abort. Defaults to 75.
    api_key:
        Optional shared key for the Newznab and SABnzbd surfaces
        (``BRIDGE_API_KEY``). ``None`` means no authentication required.
        Empty / whitespace values are normalised to ``None``.
    """

    slskd_url: str
    slskd_api_key: str
    categories: list[tuple[int, str]]
    bridge_port: int
    search_timeout: int
    db_path: str
    min_bitrate: int | None
    stall_timeout: int
    max_retries: int
    log_level: str
    min_results: int
    search_budget: int
    api_key: str | None = None

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

        raw_api_key = env.get("BRIDGE_API_KEY", "")
        api_key = raw_api_key.strip() or None

        return cls(
            slskd_url=env["SLSKD_URL"],
            slskd_api_key=env["SLSKD_API_KEY"],
            categories=list(_DEFAULT_CATEGORIES),
            bridge_port=int(env.get("BRIDGE_PORT", "8765")),
            search_timeout=int(env.get("SLSKD_SEARCH_TIMEOUT", "30")),
            db_path=env.get("BRIDGE_DB_PATH", "/data/bridge.db"),
            min_bitrate=min_bitrate,
            stall_timeout=int(env.get("BRIDGE_STALL_TIMEOUT", "1800")),
            max_retries=int(env.get("BRIDGE_MAX_RETRIES", "1")),
            log_level=log_level,
            min_results=int(env.get("BRIDGE_MIN_RESULTS", "3")),
            search_budget=int(env.get("BRIDGE_SEARCH_BUDGET", "75")),
            api_key=api_key,
        )
