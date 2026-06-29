"""Application entrypoint (Task 18).

``build_app(env)`` — composition root: loads Config, opens SQLite stores,
builds the gateway, constructs the Flask app via create_app.

``main()`` — starts the waitress WSGI server.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from flask import Flask

from slskd_lidarr_bridge.adapters.slskd_gateway import SlskdGateway
from slskd_lidarr_bridge.adapters.sqlite_store import open_stores
from slskd_lidarr_bridge.adapters.system_clock import SystemClock
from slskd_lidarr_bridge.config import Config
from slskd_lidarr_bridge.web.app import create_app


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
    gateway = SlskdGateway(config.slskd_url, config.slskd_api_key)
    clock = SystemClock()
    app = create_app(config, gateway, release_store, job_store, clock)
    app.config["BRIDGE_CONFIG"] = config
    return app


def main() -> None:
    """Start the waitress WSGI server using environment configuration."""
    import waitress

    app = build_app(os.environ)
    config = app.config["BRIDGE_CONFIG"]
    # Bind on all interfaces: the bridge runs in a container, reachable only
    # via the Docker network / published port.
    waitress.serve(app, host="0.0.0.0", port=config.bridge_port)


if __name__ == "__main__":
    main()
