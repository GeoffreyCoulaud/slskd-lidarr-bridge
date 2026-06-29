"""Tests for the Newznab indexer blueprint (Task 15)."""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import flask
import pytest

from slskd_lidarr_bridge.domain.models import AudioFile, Release, SearchQuery
from slskd_lidarr_bridge.web.newznab import create_newznab_blueprint
from slskd_lidarr_bridge.web.nzb import build_nzb, parse_nzb

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

CREATED_AT = datetime(2024, 1, 1, tzinfo=timezone.utc)

NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"


class FakeSearchService:
    def __init__(self, results: list[Release] | None = None):
        self.called_with: list[SearchQuery] = []
        self._results = results or []

    def search(self, query: SearchQuery) -> list[Release]:
        self.called_with.append(query)
        return self._results


class FakeReleaseStore:
    def __init__(self):
        self._releases: dict[str, Release] = {}

    def put(self, release: Release) -> str:
        rid = "test-id-001"
        self._releases[rid] = release
        return rid

    def get(self, release_id: str) -> Release | None:
        return self._releases.get(release_id)

    def purge_older_than(self, cutoff: datetime) -> None:
        pass


def _make_release(release_id: str = "test-id-001", quality: str = "FLAC") -> Release:
    return Release(
        id=release_id,
        artist="Artist",
        album="Album",
        title="Artist - Album [FLAC]",
        username="user1",
        files=(
            AudioFile(
                filename=r"user1\Music\Artist\Album\01.flac",
                size=5_000_000,
                extension=".flac",
            ),
        ),
        size=5_000_000,
        album_folder="Album",
        quality=quality,
        created_at=CREATED_AT,
    )


def _make_app(
    search_service=None,
    release_store=None,
    api_key=None,
    categories=None,
) -> flask.Flask:
    if search_service is None:
        search_service = FakeSearchService()
    if release_store is None:
        release_store = FakeReleaseStore()
    if categories is None:
        categories = [(3000, "Audio"), (3010, "Audio/MP3"), (3040, "Audio/Lossless")]

    app = flask.Flask(__name__)
    bp = create_newznab_blueprint(
        search_service, release_store, api_key=api_key, categories=categories
    )
    app.register_blueprint(bp)
    return app


# ---------------------------------------------------------------------------
# Tests: caps
# ---------------------------------------------------------------------------


class TestCaps:
    def test_caps_200_xml(self):
        client = _make_app().test_client()
        resp = client.get("/indexer/api?t=caps")
        assert resp.status_code == 200
        assert "xml" in resp.content_type

    def test_caps_has_audio_search_available(self):
        client = _make_app().test_client()
        resp = client.get("/indexer/api?t=caps")
        root = ET.fromstring(resp.data)
        audio_search = root.find(".//audio-search")
        assert audio_search is not None
        assert audio_search.get("available") == "yes"

    def test_caps_has_search_available(self):
        client = _make_app().test_client()
        resp = client.get("/indexer/api?t=caps")
        root = ET.fromstring(resp.data)
        search_el = root.find(".//search")
        assert search_el is not None
        assert search_el.get("available") == "yes"


# ---------------------------------------------------------------------------
# Tests: music search
# ---------------------------------------------------------------------------


class TestMusicSearch:
    def test_music_search_calls_service_with_artist_and_album(self):
        release = _make_release()
        svc = FakeSearchService(results=[release])
        store = FakeReleaseStore()
        store._releases["test-id-001"] = release

        client = _make_app(search_service=svc, release_store=store).test_client()
        resp = client.get("/indexer/api?t=music&artist=A&album=B")
        assert resp.status_code == 200
        assert len(svc.called_with) == 1
        q = svc.called_with[0]
        assert q.artist == "A"
        assert q.album == "B"

    def test_music_search_rss_item_enclosure_url_ends_with_release_id(self):
        release = _make_release()
        svc = FakeSearchService(results=[release])
        store = FakeReleaseStore()
        store._releases["test-id-001"] = release

        client = _make_app(search_service=svc, release_store=store).test_client()
        resp = client.get("/indexer/api?t=music&artist=A&album=B")
        root = ET.fromstring(resp.data)
        enclosures = root.findall(".//enclosure")
        assert len(enclosures) == 1
        url = enclosures[0].get("url")
        assert url.endswith("/indexer/nzb/test-id-001")
        assert enclosures[0].get("type") == "application/x-nzb"

    def test_music_search_empty_terms_returns_empty_channel_no_service_call(self):
        svc = FakeSearchService()
        client = _make_app(search_service=svc).test_client()
        resp = client.get("/indexer/api?t=music")
        assert resp.status_code == 200
        root = ET.fromstring(resp.data)
        items = root.findall(".//item")
        assert items == []
        assert svc.called_with == []

    def test_music_search_empty_string_q_returns_empty_channel(self):
        """Lidarr sends q= (empty) alongside artist/album in rss-sync mode."""
        svc = FakeSearchService()
        client = _make_app(search_service=svc).test_client()
        # q= empty, no artist, no album → should be treated as empty query
        resp = client.get("/indexer/api?t=music&q=")
        root = ET.fromstring(resp.data)
        items = root.findall(".//item")
        assert items == []
        assert svc.called_with == []

    def test_music_search_category_lossless_flac(self):
        release = _make_release(quality="FLAC")
        svc = FakeSearchService(results=[release])
        store = FakeReleaseStore()
        store._releases["test-id-001"] = release

        client = _make_app(search_service=svc, release_store=store).test_client()
        resp = client.get("/indexer/api?t=music&artist=A&album=B")
        root = ET.fromstring(resp.data)
        attrs = root.findall(f".//{{{NEWZNAB_NS}}}attr[@name='category']")
        assert len(attrs) == 1
        assert attrs[0].get("value") == "3040"

    def test_music_search_category_mp3(self):
        release = _make_release(quality="MP3-320")
        svc = FakeSearchService(results=[release])
        store = FakeReleaseStore()
        store._releases["test-id-001"] = release

        client = _make_app(search_service=svc, release_store=store).test_client()
        resp = client.get("/indexer/api?t=music&artist=A&album=B")
        root = ET.fromstring(resp.data)
        attrs = root.findall(f".//{{{NEWZNAB_NS}}}attr[@name='category']")
        assert len(attrs) == 1
        assert attrs[0].get("value") == "3010"

    def test_music_search_category_default(self):
        release = _make_release(quality="OGG-320")
        svc = FakeSearchService(results=[release])
        store = FakeReleaseStore()
        store._releases["test-id-001"] = release

        client = _make_app(search_service=svc, release_store=store).test_client()
        resp = client.get("/indexer/api?t=music&artist=A&album=B")
        root = ET.fromstring(resp.data)
        attrs = root.findall(f".//{{{NEWZNAB_NS}}}attr[@name='category']")
        assert len(attrs) == 1
        assert attrs[0].get("value") == "3000"


# ---------------------------------------------------------------------------
# Tests: term search
# ---------------------------------------------------------------------------


class TestTermSearch:
    def test_search_term_calls_service(self):
        svc = FakeSearchService()
        client = _make_app(search_service=svc).test_client()
        client.get("/indexer/api?t=search&q=pink+floyd")
        assert len(svc.called_with) == 1
        assert svc.called_with[0].term == "pink floyd"

    def test_search_empty_term_returns_empty_channel(self):
        svc = FakeSearchService()
        client = _make_app(search_service=svc).test_client()
        client.get("/indexer/api?t=search&q=")
        assert svc.called_with == []


# ---------------------------------------------------------------------------
# Tests: NZB route
# ---------------------------------------------------------------------------


class TestNzbRoute:
    def test_known_release_returns_200_nzb(self):
        release = _make_release()
        store = FakeReleaseStore()
        store._releases["test-id-001"] = release

        client = _make_app(release_store=store).test_client()
        resp = client.get("/indexer/nzb/test-id-001")
        assert resp.status_code == 200
        assert resp.content_type == "application/x-nzb"

    def test_nzb_body_parses_back_to_payload(self):
        release = _make_release()
        store = FakeReleaseStore()
        store._releases["test-id-001"] = release

        client = _make_app(release_store=store).test_client()
        resp = client.get("/indexer/nzb/test-id-001")
        payload = parse_nzb(resp.data)
        assert payload["username"] == release.username
        assert payload["title"] == release.title
        assert payload["album_folder"] == release.album_folder
        assert payload["total_size"] == release.size
        assert len(payload["files"]) == len(release.files)

    def test_unknown_release_returns_404(self):
        client = _make_app().test_client()
        resp = client.get("/indexer/nzb/does-not-exist")
        assert resp.status_code == 404

    def test_nzb_content_disposition_attachment(self):
        release = _make_release()
        store = FakeReleaseStore()
        store._releases["test-id-001"] = release

        client = _make_app(release_store=store).test_client()
        resp = client.get("/indexer/nzb/test-id-001")
        cd = resp.headers.get("Content-Disposition", "")
        assert "attachment" in cd
        assert "test-id-001.nzb" in cd


# ---------------------------------------------------------------------------
# Tests: API key enforcement
# ---------------------------------------------------------------------------


class TestApiKey:
    def test_missing_apikey_returns_error_100(self):
        client = _make_app(api_key="secret").test_client()
        resp = client.get("/indexer/api?t=caps")
        assert resp.status_code == 200
        root = ET.fromstring(resp.data)
        assert root.tag == "error"
        assert root.get("code") == "100"

    def test_wrong_apikey_returns_error_100(self):
        client = _make_app(api_key="secret").test_client()
        resp = client.get("/indexer/api?t=caps&apikey=wrong")
        assert resp.status_code == 200
        root = ET.fromstring(resp.data)
        assert root.tag == "error"
        assert root.get("code") == "100"

    def test_correct_apikey_passes_caps(self):
        client = _make_app(api_key="secret").test_client()
        resp = client.get("/indexer/api?t=caps&apikey=secret")
        assert resp.status_code == 200
        root = ET.fromstring(resp.data)
        assert root.tag == "caps"

    def test_no_api_key_configured_always_passes(self):
        client = _make_app(api_key=None).test_client()
        resp = client.get("/indexer/api?t=caps")
        assert resp.status_code == 200
        root = ET.fromstring(resp.data)
        assert root.tag == "caps"
