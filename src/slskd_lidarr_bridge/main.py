"""Application entrypoint (Task 18).

``build_app(env)`` — composition root: loads Config, opens SQLite stores,
builds the gateway, constructs the Flask app via create_app.

``main()`` — starts the waitress WSGI server.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping

from flask import Flask

from slskd_lidarr_bridge.adapters.inbound.app import create_app
from slskd_lidarr_bridge.adapters.outbound.slskd_gateway import SlskdGateway
from slskd_lidarr_bridge.adapters.outbound.sqlite_store import open_stores
from slskd_lidarr_bridge.adapters.outbound.system_clock import SystemClock
from slskd_lidarr_bridge.config import Config
from slskd_lidarr_bridge.logging_setup import configure_logging

logger = logging.getLogger(__name__)

# Batch download destinations, which the bridge relies on to place every disc of
# an album under one folder, landed in slskd 0.26.0.
MINIMUM_SLSKD_VERSION = (0, 26, 0)
_MINIMUM_TEXT = ".".join(str(part) for part in MINIMUM_SLSKD_VERSION)


def _check_slskd_version(gateway: SlskdGateway) -> None:
    """Refuse to start against a slskd too old for batch download destinations.

    Two cases deliberately warn instead of failing: an unreadable version (slskd
    not up yet is transient, and crash-looping the container helps nobody), and
    a version that does not parse, which is what development builds report
    (``0.0.0``).
    """
    try:
        reported = gateway.version()
    except Exception:
        logger.warning(
            "Could not read slskd's version; continuing. This bridge needs "
            "slskd %s or newer.",
            _MINIMUM_TEXT,
            exc_info=True,
        )
        return

    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", reported)
    if match is None or reported.startswith("0.0.0"):
        logger.warning(
            "slskd reports version %r, which is not a release version "
            "(development build?). Assuming it is at least %s.",
            reported,
            _MINIMUM_TEXT,
        )
        return

    if tuple(int(part) for part in match.groups()) < MINIMUM_SLSKD_VERSION:
        raise RuntimeError(
            f"slskd {reported} is too old: this bridge requires slskd "
            f"{_MINIMUM_TEXT} or newer, which introduced per-batch download "
            f"destinations (multi-disc album support)."
        )


def build_app(env: Mapping[str, str], *, db_path: str | None = None) -> Flask:
    """Build the Flask application from environment variables.

    Args:
        env: Mapping of environment variable names to values
             (typically ``os.environ``).
        db_path: SQLite database path. ``None`` uses the store's
             ``DEFAULT_DB_PATH`` (the fixed ``/data`` location); tests pass a
             temporary path here.

    Returns:
        A fully wired Flask application.

    Raises:
        ValueError: if a required environment variable is missing.
        RuntimeError: if slskd is reachable and older than the minimum version.
    """
    config = Config.from_env(env)
    release_store, job_store = open_stores(db_path)
    gateway = SlskdGateway(
        config.slskd_url,
        config.slskd_api_key,
        response_limit=config.response_limit,
    )
    _check_slskd_version(gateway)
    clock = SystemClock()
    app = create_app(config, gateway, release_store, job_store, clock)
    app.config["BRIDGE_CONFIG"] = config
    # Keep a handle on the store so the server can release the shared SQLite
    # connection at shutdown (see main()); the two wrappers share one connection.
    app.config["BRIDGE_STORE"] = release_store
    return app


def main() -> None:
    """Start the waitress WSGI server using environment configuration."""
    import waitress

    app = build_app(os.environ)
    config = app.config["BRIDGE_CONFIG"]
    configure_logging(config.log_level)
    logger.info(
        "slskd-lidarr-bridge listening on 0.0.0.0:%s (log level %s)",
        config.bridge_port,
        config.log_level,
    )
    # Bind on all interfaces: the bridge runs in a container, reachable only
    # via the Docker network / published port.
    try:
        waitress.serve(app, host="0.0.0.0", port=config.bridge_port)
    finally:
        # Release the SQLite connection on shutdown so it isn't left dangling.
        app.config["BRIDGE_STORE"].close()


if __name__ == "__main__":
    main()
