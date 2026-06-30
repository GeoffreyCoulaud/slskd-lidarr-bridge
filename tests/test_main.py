"""Tests for the application entrypoint (Task 18)."""

from __future__ import annotations

import runpy
import sqlite3

import pytest
import waitress
from flask import Flask

from slskd_lidarr_bridge import main as main_module
from slskd_lidarr_bridge.main import build_app


class TestBuildApp:
    def test_returns_flask_app_answering_health(self, tmp_path):
        db_path = str(tmp_path / "bridge.db")
        env = {
            "SLSKD_URL": "http://localhost:5030",
            "SLSKD_API_KEY": "test-key",
            "BRIDGE_DB_PATH": db_path,
        }
        app = build_app(env)
        client = app.test_client()
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
        app.config["BRIDGE_STORE"].close()

    def test_build_app_wires_indexer(self, tmp_path):
        db_path = str(tmp_path / "bridge.db")
        env = {
            "SLSKD_URL": "http://localhost:5030",
            "SLSKD_API_KEY": "test-key",
            "BRIDGE_DB_PATH": db_path,
        }
        app = build_app(env)
        client = app.test_client()
        resp = client.get("/indexer/api?t=caps")
        assert resp.status_code == 200
        assert "xml" in resp.content_type
        app.config["BRIDGE_STORE"].close()

    def test_build_app_wires_sabnzbd(self, tmp_path):
        db_path = str(tmp_path / "bridge.db")
        env = {
            "SLSKD_URL": "http://localhost:5030",
            "SLSKD_API_KEY": "test-key",
            "BRIDGE_DB_PATH": db_path,
        }
        app = build_app(env)
        client = app.test_client()
        resp = client.get("/sabnzbd/api?mode=version")
        assert resp.status_code == 200
        assert "version" in resp.get_json()
        app.config["BRIDGE_STORE"].close()

    def test_build_app_raises_on_missing_env(self):
        with pytest.raises(ValueError, match="SLSKD_URL"):
            build_app({})


class TestMain:
    """Exercise ``main()`` and the ``__main__`` script entrypoint.

    ``waitress.serve`` is stubbed so it records its call instead of blocking
    on a real server; the env is set so the composition root builds a real,
    fully-wired Flask app.
    """

    def _set_env(self, monkeypatch, tmp_path, port="9999"):
        monkeypatch.setenv("SLSKD_URL", "http://localhost:5030")
        monkeypatch.setenv("SLSKD_API_KEY", "test-key")
        monkeypatch.setenv("BRIDGE_DB_PATH", str(tmp_path / "bridge.db"))
        monkeypatch.setenv("BRIDGE_PORT", port)

    def test_main_serves_wired_app_on_configured_host_and_port(
        self, monkeypatch, tmp_path
    ):
        self._set_env(monkeypatch, tmp_path, port="9999")
        calls: list[tuple[Flask, dict]] = []
        monkeypatch.setattr(
            waitress, "serve", lambda app, **kw: calls.append((app, kw))
        )

        main_module.main()

        assert len(calls) == 1
        served_app, kwargs = calls[0]
        assert kwargs["host"] == "0.0.0.0"
        assert kwargs["port"] == 9999
        # The served object is a real, wired app — its health route answers.
        resp = served_app.test_client().get("/health")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

    def test_running_module_as_script_invokes_main(self, monkeypatch, tmp_path):
        self._set_env(monkeypatch, tmp_path, port="8123")
        calls: list[tuple[Flask, dict]] = []
        monkeypatch.setattr(
            waitress, "serve", lambda app, **kw: calls.append((app, kw))
        )

        runpy.run_module("slskd_lidarr_bridge.main", run_name="__main__")

        assert len(calls) == 1
        assert calls[0][1]["port"] == 8123

    def test_main_closes_store_on_shutdown(self, monkeypatch, tmp_path):
        """When ``serve`` returns (shutdown), main() releases the DB connection."""
        self._set_env(monkeypatch, tmp_path)
        calls: list[Flask] = []
        monkeypatch.setattr(waitress, "serve", lambda app, **kw: calls.append(app))

        main_module.main()

        served_app = calls[0]
        store = served_app.config["BRIDGE_STORE"]
        with pytest.raises(sqlite3.ProgrammingError):
            store.get("anything")  # connection closed → operating on it raises


class TestLoggingSetup:
    """``main()`` configures logging from the environment before serving."""

    def _set_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SLSKD_URL", "http://localhost:5030")
        monkeypatch.setenv("SLSKD_API_KEY", "test-key")
        monkeypatch.setenv("BRIDGE_DB_PATH", str(tmp_path / "bridge.db"))

    def test_main_configures_logging_with_default_info(self, monkeypatch, tmp_path):
        self._set_env(monkeypatch, tmp_path)
        recorded: list[str] = []
        monkeypatch.setattr(
            main_module, "configure_logging", lambda level: recorded.append(level)
        )
        monkeypatch.setattr(waitress, "serve", lambda app, **kw: None)

        main_module.main()

        assert recorded == ["INFO"]

    def test_main_configures_logging_from_log_level_env(self, monkeypatch, tmp_path):
        self._set_env(monkeypatch, tmp_path)
        monkeypatch.setenv("LOG_LEVEL", "debug")
        recorded: list[str] = []
        monkeypatch.setattr(
            main_module, "configure_logging", lambda level: recorded.append(level)
        )
        monkeypatch.setattr(waitress, "serve", lambda app, **kw: None)

        main_module.main()

        assert recorded == ["DEBUG"]
