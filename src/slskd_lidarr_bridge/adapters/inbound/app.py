"""Flask application factory (Task 17).

Creates the Flask app, wires SearchService + DownloadService, registers the
Newznab and SABnzbd blueprints, adds /health and error handlers.
"""

from __future__ import annotations

import logging

import flask
from flask import Flask, jsonify, request
from flask.typing import ResponseReturnValue
from werkzeug.exceptions import HTTPException

from slskd_lidarr_bridge.adapters.inbound.newznab import create_newznab_blueprint
from slskd_lidarr_bridge.adapters.inbound.sabnzbd import create_sabnzbd_blueprint
from slskd_lidarr_bridge.adapters.inbound.xml import build_error
from slskd_lidarr_bridge.config import Config
from slskd_lidarr_bridge.domain.download_service import DownloadService
from slskd_lidarr_bridge.domain.ports import (
    Clock,
    JobStore,
    ReleaseStore,
    SoulseekGateway,
)
from slskd_lidarr_bridge.domain.search_service import SearchService

logger = logging.getLogger(__name__)


def create_app(
    config: Config,
    gateway: SoulseekGateway,
    release_store: ReleaseStore,
    job_store: JobStore,
    clock: Clock,
) -> Flask:
    """Build and return the Flask application.

    Args:
        config: frozen Config instance.
        gateway: SoulseekGateway implementation.
        release_store: ReleaseStore implementation.
        job_store: JobStore implementation.
        clock: Clock implementation.

    Returns:
        A configured Flask application.
    """
    app = Flask(__name__)

    search_service = SearchService(
        gateway,
        release_store,
        clock,
        search_timeout=config.search_timeout,
        min_bitrate=config.min_bitrate,
    )
    download_service = DownloadService(
        gateway,
        job_store,
        clock,
    )

    newznab_bp = create_newznab_blueprint(
        search_service,
        release_store,
        categories=config.categories,
    )
    sabnzbd_bp = create_sabnzbd_blueprint(download_service)

    app.register_blueprint(newznab_bp)
    app.register_blueprint(sabnzbd_bp)

    @app.route("/health")
    def health() -> ResponseReturnValue:
        return jsonify({"status": "ok"})

    @app.errorhandler(Exception)
    def handle_exception(e: Exception) -> ResponseReturnValue:
        # Re-raise HTTP exceptions (404, 405, …) so Flask handles them normally.
        if isinstance(e, HTTPException):
            return e

        # The error is swallowed into a 200 envelope below (Lidarr's contract),
        # so log it here with its traceback — otherwise it would be invisible.
        logger.exception("Unhandled error handling %s %s", request.method, request.path)

        # Scope the error response by request path:
        # - /indexer/* → Newznab XML error
        # - everything else (including /sabnzbd/*) → JSON
        if request.path.startswith("/indexer"):
            return flask.Response(
                build_error(900, str(e)),
                status=200,
                content_type="application/xml",
            )
        return jsonify({"status": False, "error": str(e)}), 200

    return app
