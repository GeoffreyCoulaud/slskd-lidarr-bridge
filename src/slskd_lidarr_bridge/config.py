"""Application configuration loaded from environment variables."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

# Fixed Newznab indexer category IDs for audio content.
_DEFAULT_CATEGORIES: list[tuple[int, str]] = [
    (3000, "Audio"),
    (3010, "Audio/MP3"),
    (3030, "Audio/Audiobook"),
    (3040, "Audio/Lossless"),
]

_DEFAULT_SAB_CATEGORIES: list[str] = ["music"]

_REQUIRED_VARS = ("SLSKD_URL", "SLSKD_API_KEY", "SLSKD_DOWNLOADS_DIR")


@dataclass(frozen=True)
class Config:
    """Immutable application configuration.

    Attributes
    ----------
    slskd_url:
        Base URL of the slskd instance (required).
    slskd_api_key:
        API key for slskd authentication (required).
    slskd_downloads_dir:
        Absolute path to slskd's downloads directory on disk (required).
    bridge_api_key:
        Optional API key protecting the bridge's own HTTP endpoints.
    categories:
        Newznab indexer category tuples ``(id, name)`` exposed by the bridge.
        Fixed set — not user-configurable.
    sab_categories:
        SABnzbd category *names* returned by the bridge's SABnzbd shim.
        Sourced from ``BRIDGE_CATEGORIES`` (comma-separated), default
        ``["music"]``.
    bridge_host:
        Host address to bind waitress to (default ``0.0.0.0``).
    bridge_port:
        TCP port to bind waitress to (default 8765).
    search_timeout:
        Seconds to wait for a slskd search to complete (default 30).
    db_path:
        Path to the SQLite database file (default ``/data/bridge.db``).
    min_bitrate:
        Minimum acceptable bitrate in kbps; ``None`` means no filter.
    """

    slskd_url: str
    slskd_api_key: str
    slskd_downloads_dir: str
    bridge_api_key: str | None
    categories: list[tuple[int, str]]
    sab_categories: list[str]
    bridge_host: str
    bridge_port: int
    search_timeout: int
    db_path: str
    min_bitrate: int | None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        """Build a Config from a mapping of environment variables.

        Raises
        ------
        ValueError
            If a required variable is missing. The message names the var.
        """
        for var in _REQUIRED_VARS:
            if var not in env or not env[var]:
                raise ValueError(f"Required environment variable {var!r} is not set")

        raw_sab = env.get("BRIDGE_CATEGORIES", "")
        if raw_sab.strip():
            sab_categories = [c.strip() for c in raw_sab.split(",") if c.strip()]
        else:
            sab_categories = list(_DEFAULT_SAB_CATEGORIES)

        raw_min_bitrate = env.get("BRIDGE_MIN_BITRATE", "")
        min_bitrate = int(raw_min_bitrate) if raw_min_bitrate.strip() else None

        return cls(
            slskd_url=env["SLSKD_URL"],
            slskd_api_key=env["SLSKD_API_KEY"],
            slskd_downloads_dir=env["SLSKD_DOWNLOADS_DIR"],
            bridge_api_key=env.get("BRIDGE_API_KEY") or None,
            categories=list(_DEFAULT_CATEGORIES),
            sab_categories=sab_categories,
            bridge_host=env.get("BRIDGE_HOST", "0.0.0.0"),
            bridge_port=int(env.get("BRIDGE_PORT", "8765")),
            search_timeout=int(env.get("SLSKD_SEARCH_TIMEOUT", "30")),
            db_path=env.get("BRIDGE_DB_PATH", "/data/bridge.db"),
            min_bitrate=min_bitrate,
        )
