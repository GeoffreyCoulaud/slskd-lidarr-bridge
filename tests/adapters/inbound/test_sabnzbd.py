"""Tests for the SABnzbd download-client blueprint (Task 16)."""

from __future__ import annotations

import io

import flask

from slskd_lidarr_bridge.adapters.inbound.nzb import build_nzb
from slskd_lidarr_bridge.adapters.inbound.sabnzbd import create_sabnzbd_blueprint
from slskd_lidarr_bridge.domain.models import JobStatusView

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeDownloadService:
    def __init__(
        self,
        statuses: list[JobStatusView] | None = None,
        complete_dir: str = "/downloads",
    ):
        self._statuses = statuses or []
        self._complete_dir = complete_dir
        self.started: list[tuple[dict, str]] = []
        self.removed: list[str] = []

    def start(self, payload: dict, category: str) -> str:
        self.started.append((payload, category))
        return "SABnzbd_nzo_test001"

    def statuses(self) -> list[JobStatusView]:
        return self._statuses

    def remove(self, nzo_id: str) -> None:
        self.removed.append(nzo_id)

    def completed_dir(self) -> str:
        return self._complete_dir


def _make_status(
    nzo_id: str = "nzo1",
    title: str = "Artist - Album [FLAC]",
    category: str = "music",
    total_bytes: int = 10_485_760,
    transferred_bytes: int = 5_242_880,
    percent: float = 50.0,
    state: str = "downloading",
    storage: str | None = None,
    fail_message: str | None = None,
) -> JobStatusView:
    return JobStatusView(
        nzo_id=nzo_id,
        title=title,
        category=category,
        total_bytes=total_bytes,
        transferred_bytes=transferred_bytes,
        percent=percent,
        state=state,
        storage=storage,
        fail_message=fail_message,
    )


def _make_app(
    download_service=None,
    complete_dir: str = "/downloads",
    api_key=None,
) -> flask.Flask:
    if download_service is None:
        download_service = FakeDownloadService(complete_dir=complete_dir)

    app = flask.Flask(__name__)
    bp = create_sabnzbd_blueprint(download_service, api_key=api_key)
    app.register_blueprint(bp)
    return app


def _sample_nzb() -> bytes:
    return build_nzb(
        {
            "username": "user1",
            "title": "Artist - Album [FLAC]",
            "album_folder": "Album",
            "total_size": 10_000_000,
            "files": [
                {
                    "filename": r"user1\Music\Artist\Album\01.flac",
                    "size": 10_000_000,
                }
            ],
        }
    )


# ---------------------------------------------------------------------------
# Tests: version
# ---------------------------------------------------------------------------


class TestVersion:
    def test_version_returns_version_key(self):
        client = _make_app().test_client()
        resp = client.get("/sabnzbd/api?mode=version")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "version" in data
        assert data["version"] == "4.3.0"


# ---------------------------------------------------------------------------
# Tests: get_config
# ---------------------------------------------------------------------------


class TestGetConfig:
    def test_get_config_has_complete_dir(self):
        client = _make_app(complete_dir="/data/downloads").test_client()
        resp = client.get("/sabnzbd/api?mode=get_config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["config"]["misc"]["complete_dir"] == "/data/downloads"

    def test_get_config_has_categories(self):
        client = _make_app().test_client()
        resp = client.get("/sabnzbd/api?mode=get_config")
        data = resp.get_json()
        names = [c["name"] for c in data["config"]["categories"]]
        assert "music" in names

    def test_get_config_categories_are_objects_with_name(self):
        """Lidarr deserializes categories into SabnzbdCategory objects.

        Returning bare strings makes the download-client test abort with a
        cast error, so each category must be a JSON object carrying a name.
        """
        client = _make_app().test_client()
        resp = client.get("/sabnzbd/api?mode=get_config")
        cats = resp.get_json()["config"]["categories"]
        assert isinstance(cats, list)
        assert all(isinstance(c, dict) and "name" in c for c in cats)


# ---------------------------------------------------------------------------
# Tests: fullstatus
# ---------------------------------------------------------------------------


class TestFullStatus:
    def test_fullstatus_returns_status_key(self):
        client = _make_app().test_client()
        resp = client.get("/sabnzbd/api?mode=fullstatus")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "status" in data


# ---------------------------------------------------------------------------
# Tests: addfile
# ---------------------------------------------------------------------------


class TestAddFile:
    def test_addfile_calls_service_start(self):
        svc = FakeDownloadService()
        client = _make_app(download_service=svc).test_client()
        nzb_bytes = _sample_nzb()
        resp = client.post(
            "/sabnzbd/api",
            data={
                "mode": "addfile",
                "cat": "music",
                "name": (io.BytesIO(nzb_bytes), "test.nzb"),
            },
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] is True
        assert data["nzo_ids"] == ["SABnzbd_nzo_test001"]
        assert len(svc.started) == 1

    def test_addfile_passes_category_to_service(self):
        svc = FakeDownloadService()
        client = _make_app(download_service=svc).test_client()
        nzb_bytes = _sample_nzb()
        client.post(
            "/sabnzbd/api",
            data={
                "mode": "addfile",
                "cat": "music",
                "name": (io.BytesIO(nzb_bytes), "test.nzb"),
            },
            content_type="multipart/form-data",
        )
        _, cat = svc.started[0]
        assert cat == "music"

    def test_addfile_reads_category_from_query_param(self):
        """Lidarr sends `cat` as a URL query parameter, not a form field.

        The NZB is the only multipart part; `mode`, `cat`, and `priority` ride
        in the query string (see Lidarr's SabnzbdProxy.DownloadNzb, which calls
        ``request.AddQueryParam("cat", ...)``). If the handler reads `cat` from
        the form only, the job's category is empty, and Lidarr — which tracks
        and imports only downloads whose category matches its configured one —
        silently drops the download (empty queue, no import).
        """
        svc = FakeDownloadService()
        client = _make_app(download_service=svc).test_client()
        nzb_bytes = _sample_nzb()
        resp = client.post(
            "/sabnzbd/api?mode=addfile&cat=music",
            data={"name": (io.BytesIO(nzb_bytes), "test.nzb")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] is True
        _, cat = svc.started[0]
        assert cat == "music"

    def test_addfile_parses_nzb_payload(self):
        svc = FakeDownloadService()
        client = _make_app(download_service=svc).test_client()
        nzb_bytes = _sample_nzb()
        client.post(
            "/sabnzbd/api",
            data={
                "mode": "addfile",
                "cat": "music",
                "name": (io.BytesIO(nzb_bytes), "test.nzb"),
            },
            content_type="multipart/form-data",
        )
        payload, _ = svc.started[0]
        assert payload["username"] == "user1"
        assert payload["title"] == "Artist - Album [FLAC]"
        assert payload["album_folder"] == "Album"

    def test_addfile_no_file_returns_status_false(self):
        """addfile without a 'name' file field returns status:false (HTTP 200)."""
        client = _make_app().test_client()
        resp = client.post(
            "/sabnzbd/api",
            data={"mode": "addfile", "cat": "music"},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] is False
        assert data["error"] == "no nzb file provided"


# ---------------------------------------------------------------------------
# Tests: queue
# ---------------------------------------------------------------------------


class TestQueue:
    def test_queue_contains_downloading_slots(self):
        status = _make_status(state="downloading")
        client = _make_app(
            download_service=FakeDownloadService(statuses=[status])
        ).test_client()
        resp = client.get("/sabnzbd/api?mode=queue")
        data = resp.get_json()
        slots = data["queue"]["slots"]
        assert len(slots) == 1
        s = slots[0]
        assert s["nzo_id"] == "nzo1"
        assert s["filename"] == "Artist - Album [FLAC]"
        assert s["status"] == "Downloading"
        assert s["cat"] == "music"

    def test_queue_excludes_completed(self):
        completed = _make_status(state="completed")
        client = _make_app(
            download_service=FakeDownloadService(statuses=[completed])
        ).test_client()
        resp = client.get("/sabnzbd/api?mode=queue")
        slots = resp.get_json()["queue"]["slots"]
        assert slots == []

    def test_queue_slot_mb_and_percentage(self):
        # total_bytes=10 MiB, transferred=5 MiB → mb=10, mbleft=5, percentage=50
        status = _make_status(
            state="downloading",
            total_bytes=10 * 1024 * 1024,
            transferred_bytes=5 * 1024 * 1024,
            percent=50.0,
        )
        client = _make_app(
            download_service=FakeDownloadService(statuses=[status])
        ).test_client()
        resp = client.get("/sabnzbd/api?mode=queue")
        s = resp.get_json()["queue"]["slots"][0]
        assert abs(s["mb"] - 10.0) < 0.01
        assert abs(s["mbleft"] - 5.0) < 0.01
        assert s["percentage"] == 50

    def test_queue_filter_by_category(self):
        s1 = _make_status(nzo_id="j1", category="music", state="downloading")
        s2 = _make_status(nzo_id="j2", category="ebooks", state="downloading")
        client = _make_app(
            download_service=FakeDownloadService(statuses=[s1, s2])
        ).test_client()
        resp = client.get("/sabnzbd/api?mode=queue&category=music")
        slots = resp.get_json()["queue"]["slots"]
        assert len(slots) == 1
        assert slots[0]["cat"] == "music"

    def test_queue_slot_has_timeleft_and_index(self):
        status = _make_status(state="downloading")
        client = _make_app(
            download_service=FakeDownloadService(statuses=[status])
        ).test_client()
        resp = client.get("/sabnzbd/api?mode=queue")
        s = resp.get_json()["queue"]["slots"][0]
        assert "timeleft" in s
        assert "index" in s

    def test_queue_filter_by_cat_alias(self):
        """`cat=` query param filters queue the same way `category=` does."""
        s1 = _make_status(nzo_id="j1", category="music", state="downloading")
        s2 = _make_status(nzo_id="j2", category="ebooks", state="downloading")
        client = _make_app(
            download_service=FakeDownloadService(statuses=[s1, s2])
        ).test_client()
        resp = client.get("/sabnzbd/api?mode=queue&cat=music")
        slots = resp.get_json()["queue"]["slots"]
        assert len(slots) == 1
        assert slots[0]["cat"] == "music"


# ---------------------------------------------------------------------------
# Tests: history
# ---------------------------------------------------------------------------


class TestHistory:
    def test_history_completed_slot(self):
        status = _make_status(state="completed", storage="/downloads/Album")
        client = _make_app(
            download_service=FakeDownloadService(statuses=[status])
        ).test_client()
        resp = client.get("/sabnzbd/api?mode=history")
        data = resp.get_json()
        slots = data["history"]["slots"]
        assert len(slots) == 1
        s = slots[0]
        assert s["nzo_id"] == "nzo1"
        assert s["status"] == "Completed"
        assert s["storage"] == "/downloads/Album"
        assert s["category"] == "music"

    def test_history_failed_slot(self):
        status = _make_status(state="failed", fail_message="Connection refused")
        client = _make_app(
            download_service=FakeDownloadService(statuses=[status])
        ).test_client()
        resp = client.get("/sabnzbd/api?mode=history")
        slots = resp.get_json()["history"]["slots"]
        assert len(slots) == 1
        s = slots[0]
        assert s["status"] == "Failed"
        assert s["fail_message"] == "Connection refused"

    def test_history_excludes_downloading(self):
        status = _make_status(state="downloading")
        client = _make_app(
            download_service=FakeDownloadService(statuses=[status])
        ).test_client()
        resp = client.get("/sabnzbd/api?mode=history")
        slots = resp.get_json()["history"]["slots"]
        assert slots == []

    def test_history_filter_by_category(self):
        s1 = _make_status(nzo_id="j1", category="music", state="completed")
        s2 = _make_status(nzo_id="j2", category="ebooks", state="completed")
        client = _make_app(
            download_service=FakeDownloadService(statuses=[s1, s2])
        ).test_client()
        resp = client.get("/sabnzbd/api?mode=history&category=music")
        slots = resp.get_json()["history"]["slots"]
        assert len(slots) == 1
        assert slots[0]["category"] == "music"

    def test_history_slot_has_nzb_name_and_bytes(self):
        status = _make_status(state="completed", total_bytes=10_000_000)
        client = _make_app(
            download_service=FakeDownloadService(statuses=[status])
        ).test_client()
        resp = client.get("/sabnzbd/api?mode=history")
        s = resp.get_json()["history"]["slots"][0]
        assert s["nzb_name"] == status.title
        assert s["bytes"] == 10_000_000

    def test_history_filter_by_cat_alias(self):
        """`cat=` query param filters history the same way `category=` does."""
        s1 = _make_status(nzo_id="j1", category="music", state="completed")
        s2 = _make_status(nzo_id="j2", category="ebooks", state="completed")
        client = _make_app(
            download_service=FakeDownloadService(statuses=[s1, s2])
        ).test_client()
        resp = client.get("/sabnzbd/api?mode=history&cat=music")
        slots = resp.get_json()["history"]["slots"]
        assert len(slots) == 1
        assert slots[0]["category"] == "music"


# ---------------------------------------------------------------------------
# Tests: delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_queue_calls_remove(self):
        svc = FakeDownloadService()
        client = _make_app(download_service=svc).test_client()
        resp = client.get("/sabnzbd/api?mode=queue&name=delete&value=nzo123")
        assert resp.status_code == 200
        assert resp.get_json()["status"] is True
        assert "nzo123" in svc.removed

    def test_delete_history_calls_remove(self):
        svc = FakeDownloadService()
        client = _make_app(download_service=svc).test_client()
        resp = client.get("/sabnzbd/api?mode=history&name=delete&value=nzo456")
        assert resp.status_code == 200
        assert "nzo456" in svc.removed


# ---------------------------------------------------------------------------
# Tests: unknown mode
# ---------------------------------------------------------------------------


class TestUnknownMode:
    def test_unknown_mode_returns_status_false_with_error(self):
        svc = FakeDownloadService()
        client = _make_app(download_service=svc).test_client()
        resp = client.get("/sabnzbd/api?mode=frobnicate")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] is False
        assert data["error"] == "Unknown mode: frobnicate"
        # An unknown mode must not start or remove anything.
        assert svc.started == []
        assert svc.removed == []


# ---------------------------------------------------------------------------
# Tests: API key authentication
# ---------------------------------------------------------------------------


class TestApiKeyAuth:
    KEY = "s3cr3t"

    # -- No-key regression: all requests pass without apikey --

    def test_no_key_version_needs_no_apikey(self):
        client = _make_app().test_client()
        resp = client.get("/sabnzbd/api?mode=version")
        assert resp.status_code == 200
        assert "version" in resp.get_json()

    def test_no_key_addfile_needs_no_apikey(self):
        svc = FakeDownloadService()
        client = _make_app(download_service=svc).test_client()
        nzb_bytes = _sample_nzb()
        resp = client.post(
            "/sabnzbd/api?mode=addfile&cat=music",
            data={"name": (io.BytesIO(nzb_bytes), "test.nzb")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] is True

    # -- Key set: missing apikey → error payload at HTTP 200 --

    def test_key_set_missing_apikey_version_returns_error(self):
        client = _make_app(api_key=self.KEY).test_client()
        resp = client.get("/sabnzbd/api?mode=version")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] is False
        assert data["error"] == "API Key Incorrect"

    def test_key_set_missing_apikey_queue_returns_error(self):
        client = _make_app(api_key=self.KEY).test_client()
        resp = client.get("/sabnzbd/api?mode=queue")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] is False
        assert data["error"] == "API Key Incorrect"

    def test_key_set_missing_apikey_addfile_returns_error(self):
        client = _make_app(api_key=self.KEY).test_client()
        nzb_bytes = _sample_nzb()
        resp = client.post(
            "/sabnzbd/api?mode=addfile&cat=music",
            data={"name": (io.BytesIO(nzb_bytes), "test.nzb")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] is False
        assert data["error"] == "API Key Incorrect"

    # -- Key set: wrong apikey → error --

    def test_key_set_wrong_apikey_returns_error(self):
        client = _make_app(api_key=self.KEY).test_client()
        resp = client.get("/sabnzbd/api?mode=version&apikey=wrongkey")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] is False

    # -- Key set: correct apikey → normal response --

    def test_key_set_correct_apikey_version_succeeds(self):
        client = _make_app(api_key=self.KEY).test_client()
        resp = client.get(f"/sabnzbd/api?mode=version&apikey={self.KEY}")
        assert resp.status_code == 200
        assert "version" in resp.get_json()

    def test_key_set_correct_apikey_queue_succeeds(self):
        client = _make_app(api_key=self.KEY).test_client()
        resp = client.get(f"/sabnzbd/api?mode=queue&apikey={self.KEY}")
        assert resp.status_code == 200
        assert "queue" in resp.get_json()

    # -- addfile: key read from query param --

    def test_key_set_addfile_accepts_key_from_query(self):
        svc = FakeDownloadService()
        client = _make_app(download_service=svc, api_key=self.KEY).test_client()
        nzb_bytes = _sample_nzb()
        resp = client.post(
            f"/sabnzbd/api?mode=addfile&cat=music&apikey={self.KEY}",
            data={"name": (io.BytesIO(nzb_bytes), "test.nzb")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] is True

    # -- addfile: key read from form body --

    def test_key_set_addfile_accepts_key_from_form(self):
        svc = FakeDownloadService()
        client = _make_app(download_service=svc, api_key=self.KEY).test_client()
        nzb_bytes = _sample_nzb()
        resp = client.post(
            "/sabnzbd/api?mode=addfile&cat=music",
            data={
                "apikey": self.KEY,
                "name": (io.BytesIO(nzb_bytes), "test.nzb"),
            },
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] is True
