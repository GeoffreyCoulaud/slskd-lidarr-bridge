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
        slskd's *idle* search window in seconds (its ``searchTimeout``): slskd
        completes a search after this many seconds pass with **no new response**
        (the timer resets on every response), so it must stay small — never the
        whole budget, or a busy query never completes. Default 15 (slskd's own
        default); ``0`` omits the field so slskd uses that default; positive values
        must be >= slskd's 5 s minimum.
    response_limit:
        ``responseLimit`` sent on every search POST so a popular query — whose idle
        timer keeps resetting — completes once this many peers have responded,
        instead of running until the wall-clock budget. Default 100 (aligned with
        the Newznab caps limit); ``<= 0`` omits it (slskd default 250).
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
    enough_results:
        Stop issuing broader fallback candidates once this many distinct releases
        have accumulated. The primary query always runs. Defaults to 3.
    search_budget:
        Total wall-clock seconds for the whole search across all candidates; kept
        under Lidarr's hardcoded 100 s indexer-request abort. Each candidate takes
        the remaining budget (minus a small inter-search margin), optionally capped
        by ``search_timeout``. Defaults to 75.
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
    min_bitrate: int | None
    stall_timeout: int
    max_retries: int
    log_level: str
    enough_results: int
    search_budget: int
    api_key: str | None = None
    response_limit: int = 100

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
            search_timeout=int(env.get("SLSKD_SEARCH_TIMEOUT", "15")),
            min_bitrate=min_bitrate,
            stall_timeout=int(env.get("BRIDGE_STALL_TIMEOUT", "1800")),
            max_retries=int(env.get("BRIDGE_MAX_RETRIES", "1")),
            log_level=log_level,
            enough_results=int(env.get("BRIDGE_ENOUGH_RESULTS", "3")),
            search_budget=int(env.get("BRIDGE_SEARCH_BUDGET", "75")),
            api_key=api_key,
            response_limit=int(env.get("SLSKD_RESPONSE_LIMIT", "100")),
        )
