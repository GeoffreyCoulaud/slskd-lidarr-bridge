"""Application entrypoint (Task 18).

``build_app(env)`` — composition root: loads Config, opens SQLite stores,
builds the gateway, constructs the Flask app via create_app.

``main()`` — starts the waitress WSGI server.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from flask import Flask

from slskd_lidarr_bridge.adapters.inbound.app import create_app
from slskd_lidarr_bridge.adapters.outbound.slskd_gateway import SlskdGateway
from slskd_lidarr_bridge.adapters.outbound.sqlite_store import open_stores
from slskd_lidarr_bridge.adapters.outbound.system_clock import SystemClock
from slskd_lidarr_bridge.config import Config
from slskd_lidarr_bridge.logging_setup import configure_logging

logger = logging.getLogger(__name__)


def build_app(env: Mapping[str, str]) -> Flask:
    """Build the Flask application from environment variables.

    Args:
        env: Mapping of environment variable names to values
             (typically ``os.environ``).

    Returns:
        A fully wired Flask application.

    Raises:
        ValueError: if a required environment variable is missing.
    """
    config = Config.from_env(env)
    release_store, job_store = open_stores(config.db_path)
    gateway = SlskdGateway(
        config.slskd_url,
        config.slskd_api_key,
        response_limit=config.response_limit,
    )
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
