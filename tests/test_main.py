"""Tests for the application entrypoint (Task 18)."""

from __future__ import annotations

import pytest

from slskd_lidarr_bridge.main import build_app


class TestBuildApp:
    def test_returns_flask_app_answering_health(self, tmp_path):
        db_path = str(tmp_path / "bridge.db")
        env = {
            "SLSKD_URL": "http://localhost:5030",
            "SLSKD_API_KEY": "test-key",
            "SLSKD_DOWNLOADS_DIR": "/downloads",
            "BRIDGE_DB_PATH": db_path,
        }
        app = build_app(env)
        client = app.test_client()
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

    def test_build_app_wires_indexer(self, tmp_path):
        db_path = str(tmp_path / "bridge.db")
        env = {
            "SLSKD_URL": "http://localhost:5030",
            "SLSKD_API_KEY": "test-key",
            "SLSKD_DOWNLOADS_DIR": "/downloads",
            "BRIDGE_DB_PATH": db_path,
        }
        app = build_app(env)
        client = app.test_client()
        resp = client.get("/indexer/api?t=caps")
        assert resp.status_code == 200
        assert "xml" in resp.content_type

    def test_build_app_wires_sabnzbd(self, tmp_path):
        db_path = str(tmp_path / "bridge.db")
        env = {
            "SLSKD_URL": "http://localhost:5030",
            "SLSKD_API_KEY": "test-key",
            "SLSKD_DOWNLOADS_DIR": "/downloads",
            "BRIDGE_DB_PATH": db_path,
        }
        app = build_app(env)
        client = app.test_client()
        resp = client.get("/sabnzbd/api?mode=version")
        assert resp.status_code == 200
        assert "version" in resp.get_json()

    def test_build_app_raises_on_missing_env(self):
        with pytest.raises(ValueError, match="SLSKD_URL"):
            build_app({})
