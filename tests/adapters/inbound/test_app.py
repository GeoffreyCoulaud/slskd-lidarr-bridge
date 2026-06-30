"""Tests for the Flask app factory (Task 17)."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from slskd_lidarr_bridge.adapters.inbound.app import create_app
from slskd_lidarr_bridge.config import Config
from slskd_lidarr_bridge.domain.models import AudioFile, DownloadJob

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

CREATED_AT = datetime(2024, 1, 1, tzinfo=UTC)


class FakeGateway:
    def start_search(self, text: str) -> str:
        return "sid-fake"

    def search_is_complete(self, sid: str) -> bool:
        return True

    def search_responses(self, sid: str) -> list:
        return []

    def enqueue(self, username: str, files: list) -> None:
        pass

    def transfers(self, username: str) -> list:
        return []

    def cancel(self, username: str, transfer_id: str) -> None:
        pass

    def downloads_directory(self) -> str:
        return "/downloads"


class FakeReleaseStore:
    def put(self, release) -> str:
        return "fake-release-id"

    def get(self, release_id: str):
        return None

    def purge_older_than(self, cutoff: datetime) -> None:
        pass


class FakeJobStore:
    def add(self, job) -> None:
        pass

    def get(self, nzo_id: str):
        return None

    def list(self) -> list:
        return []

    def remove(self, nzo_id: str) -> None:
        pass


class FakeClock:
    def now(self) -> datetime:
        return CREATED_AT

    def sleep(self, seconds: float) -> None:
        pass


class ExplodingGateway:
    """Gateway that raises on search and transfers — used for error handler tests."""

    def start_search(self, text: str) -> str:
        raise RuntimeError("gateway search boom")

    def search_is_complete(self, sid: str) -> bool:
        return True

    def search_responses(self, sid: str) -> list:
        return []

    def enqueue(self, username: str, files: list) -> None:
        pass

    def transfers(self, username: str) -> list:
        raise RuntimeError("gateway transfers boom")

    def cancel(self, username: str, transfer_id: str) -> None:
        pass

    def downloads_directory(self) -> str:
        return "/downloads"


class FakeJobStoreWithOneJob:
    """Job store that returns a single job so statuses() calls transfers()."""

    def add(self, job) -> None:
        pass

    def get(self, nzo_id: str):
        return None

    def list(self) -> list:
        return [
            DownloadJob(
                nzo_id="nzo1",
                title="Title",
                username="user1",
                files=(AudioFile(filename=r"user1\Music\01.flac", size=1000),),
                category="music",
                album_folder="Album",
                total_size=1000,
                created_at=CREATED_AT,
            )
        ]

    def remove(self, nzo_id: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> Config:
    defaults: dict = dict(
        slskd_url="http://slskd:5030",
        slskd_api_key="key",
        categories=[(3000, "Audio"), (3040, "Audio/Lossless")],
        bridge_port=8765,
        search_timeout=30,
        db_path=":memory:",
        min_bitrate=None,
        log_level="INFO",
    )
    defaults.update(overrides)
    return Config(**defaults)


# ---------------------------------------------------------------------------
# Tests: basic wiring
# ---------------------------------------------------------------------------


class TestCreateApp:
    def test_health_returns_ok(self):
        config = _make_config()
        app = create_app(
            config, FakeGateway(), FakeReleaseStore(), FakeJobStore(), FakeClock()
        )
        resp = app.test_client().get("/health")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

    def test_indexer_caps_endpoint_200(self):
        config = _make_config()
        app = create_app(
            config, FakeGateway(), FakeReleaseStore(), FakeJobStore(), FakeClock()
        )
        resp = app.test_client().get("/indexer/api?t=caps")
        assert resp.status_code == 200
        assert "xml" in resp.content_type

    def test_sabnzbd_version_endpoint_200(self):
        config = _make_config()
        app = create_app(
            config, FakeGateway(), FakeReleaseStore(), FakeJobStore(), FakeClock()
        )
        resp = app.test_client().get("/sabnzbd/api?mode=version")
        assert resp.status_code == 200
        assert "version" in resp.get_json()


# ---------------------------------------------------------------------------
# Tests: error handler — no stack trace leakage
# ---------------------------------------------------------------------------


class TestErrorHandlers:
    def test_indexer_service_error_returns_error_xml(self):
        """An exception from the search service returns error XML at HTTP 200."""
        config = _make_config()
        app = create_app(
            config,
            ExplodingGateway(),  # start_search raises
            FakeReleaseStore(),
            FakeJobStore(),
            FakeClock(),
        )
        resp = app.test_client().get("/indexer/api?t=music&artist=A&album=B")
        assert resp.status_code == 200
        assert "xml" in resp.content_type
        # Must be parseable XML with tag <error>
        root = ET.fromstring(resp.data)
        assert root.tag == "error"
        assert root.get("code") == "900"
        # Must not be a raw Python traceback in the response body
        assert b"Traceback" not in resp.data

    def test_sabnzbd_service_error_returns_json_status_false(self):
        """An exception from statuses() returns JSON status:false at HTTP 200."""
        config = _make_config()
        app = create_app(
            config,
            ExplodingGateway(),  # transfers() raises
            FakeReleaseStore(),
            FakeJobStoreWithOneJob(),  # one job → statuses() calls transfers()
            FakeClock(),
        )
        resp = app.test_client().get("/sabnzbd/api?mode=queue")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] is False
        assert "error" in data
        # Must not be an HTML error page
        assert b"Traceback" not in resp.data

    def test_unhandled_error_is_logged_with_traceback(self, caplog):
        """Errors swallowed into a 200 envelope must still be logged."""
        config = _make_config()
        app = create_app(
            config,
            ExplodingGateway(),  # start_search raises
            FakeReleaseStore(),
            FakeJobStore(),
            FakeClock(),
        )
        with caplog.at_level(logging.ERROR):
            app.test_client().get("/indexer/api?t=music&artist=A&album=B")
        assert any(
            r.levelno == logging.ERROR and r.exc_info is not None
            for r in caplog.records
            if r.name == "slskd_lidarr_bridge.adapters.inbound.app"
        )

    def test_404_not_swallowed_by_error_handler(self):
        """The error handler must not interfere with legitimate 404s."""
        config = _make_config()
        app = create_app(
            config, FakeGateway(), FakeReleaseStore(), FakeJobStore(), FakeClock()
        )
        resp = app.test_client().get("/indexer/nzb/nonexistent-id")
        assert resp.status_code == 404
