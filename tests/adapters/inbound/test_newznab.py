"""Tests for the Newznab indexer blueprint (Task 15)."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import flask

from slskd_lidarr_bridge.adapters.inbound.newznab import create_newznab_blueprint
from slskd_lidarr_bridge.adapters.inbound.nzb import parse_nzb
from slskd_lidarr_bridge.domain.models import AudioFile, Release, SearchQuery

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

CREATED_AT = datetime(2024, 1, 1, tzinfo=UTC)

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
    categories=None,
    api_key=None,
) -> flask.Flask:
    if search_service is None:
        search_service = FakeSearchService()
    if release_store is None:
        release_store = FakeReleaseStore()
    if categories is None:
        categories = [(3000, "Audio"), (3010, "Audio/MP3"), (3040, "Audio/Lossless")]

    app = flask.Flask(__name__)
    bp = create_newznab_blueprint(
        search_service, release_store, categories=categories, api_key=api_key
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
        assert url is not None
        assert url.endswith("/indexer/nzb/test-id-001")
        assert enclosures[0].get("type") == "application/x-nzb"

    def test_music_search_empty_terms_returns_sentinel_no_service_call(self):
        """Lidarr's connection test fetches the recent feed (no search terms).

        We must answer with at least one item so TestConnection passes, without
        ever hitting the search backend (slskd has no "recent uploads" concept).
        """
        svc = FakeSearchService()
        client = _make_app(search_service=svc).test_client()
        resp = client.get("/indexer/api?t=music")
        assert resp.status_code == 200
        root = ET.fromstring(resp.data)
        items = root.findall(".//item")
        assert len(items) == 1
        assert svc.called_with == []

    def test_music_search_empty_string_q_returns_sentinel(self):
        """Lidarr sends q= (empty) alongside artist/album in rss-sync mode."""
        svc = FakeSearchService()
        client = _make_app(search_service=svc).test_client()
        # q= empty, no artist, no album → treated as the empty/recent query
        resp = client.get("/indexer/api?t=music&q=")
        root = ET.fromstring(resp.data)
        items = root.findall(".//item")
        assert len(items) == 1
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

    def test_music_search_logs_result_count(self, caplog):
        release = _make_release()
        svc = FakeSearchService(results=[release])
        store = FakeReleaseStore()
        store._releases["test-id-001"] = release

        client = _make_app(search_service=svc, release_store=store).test_client()
        with caplog.at_level(logging.INFO):
            client.get("/indexer/api?t=music&artist=A&album=B")

        assert any(
            r.levelno == logging.INFO and "1 releases" in r.getMessage()
            for r in caplog.records
            if "newznab" in r.name
        )

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
# Tests: recent/test feed sentinel
# ---------------------------------------------------------------------------


class TestRecentFeedSentinel:
    """The empty/recent feed must satisfy Lidarr's TestConnection."""

    def test_sentinel_category_is_a_configured_category(self):
        configured = [(3000, "Audio"), (3010, "Audio/MP3"), (3040, "Audio/Lossless")]
        client = _make_app(categories=configured).test_client()
        resp = client.get("/indexer/api?t=music")
        root = ET.fromstring(resp.data)
        attrs = root.findall(f".//{{{NEWZNAB_NS}}}attr[@name='category']")
        assert len(attrs) == 1
        configured_ids = {str(cid) for cid, _ in configured}
        assert attrs[0].get("value") in configured_ids

    def test_sentinel_item_has_nzb_enclosure(self):
        client = _make_app().test_client()
        resp = client.get("/indexer/api?t=music")
        assert "xml" in resp.content_type
        root = ET.fromstring(resp.data)
        enclosures = root.findall(".//enclosure")
        assert len(enclosures) == 1
        assert enclosures[0].get("type") == "application/x-nzb"


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

    def test_search_empty_term_returns_sentinel(self):
        svc = FakeSearchService()
        client = _make_app(search_service=svc).test_client()
        resp = client.get("/indexer/api?t=search&q=")
        root = ET.fromstring(resp.data)
        items = root.findall(".//item")
        assert len(items) == 1
        # The recent/test feed must never reach the search backend.
        assert svc.called_with == []


# ---------------------------------------------------------------------------
# Tests: unknown function
# ---------------------------------------------------------------------------


class TestUnknownFunction:
    def test_unknown_t_returns_newznab_error_202(self):
        svc = FakeSearchService()
        client = _make_app(search_service=svc).test_client()
        resp = client.get("/indexer/api?t=bogus")

        assert resp.status_code == 200
        assert "xml" in resp.content_type
        root = ET.fromstring(resp.data)
        assert root.tag == "error"
        assert root.get("code") == "202"
        assert root.get("description") == "No such function: bogus"
        # An unknown function must never reach the search backend.
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
# Tests: API key authentication
# ---------------------------------------------------------------------------


class TestApiKeyAuth:
    KEY = "s3cr3t"

    def _make_keyed_app(
        self,
        search_service=None,
        release_store=None,
    ) -> flask.Flask:
        return _make_app(
            search_service=search_service,
            release_store=release_store,
            categories=[(3000, "Audio"), (3040, "Audio/Lossless")],
            api_key=self.KEY,
        )

    # -- No-key regression: all requests pass without apikey param --

    def test_no_key_caps_needs_no_apikey(self):
        client = _make_app().test_client()
        resp = client.get("/indexer/api?t=caps")
        assert resp.status_code == 200
        root = ET.fromstring(resp.data)
        assert root.tag != "error"

    def test_no_key_nzb_needs_no_apikey(self):
        release = _make_release()
        store = FakeReleaseStore()
        store._releases["test-id-001"] = release
        client = _make_app(release_store=store).test_client()
        resp = client.get("/indexer/nzb/test-id-001")
        assert resp.status_code == 200
        assert resp.content_type == "application/x-nzb"

    # -- Key set: missing apikey → error 100 at HTTP 200 --

    def test_key_set_missing_apikey_caps_returns_error_100(self):
        client = self._make_keyed_app().test_client()
        resp = client.get("/indexer/api?t=caps")
        assert resp.status_code == 200
        assert "xml" in resp.content_type
        root = ET.fromstring(resp.data)
        assert root.tag == "error"
        assert root.get("code") == "100"

    def test_key_set_missing_apikey_search_returns_error_100(self):
        client = self._make_keyed_app().test_client()
        resp = client.get("/indexer/api?t=music&artist=A&album=B")
        assert resp.status_code == 200
        root = ET.fromstring(resp.data)
        assert root.tag == "error"
        assert root.get("code") == "100"

    # -- Key set: wrong apikey → error 100 --

    def test_key_set_wrong_apikey_returns_error_100(self):
        client = self._make_keyed_app().test_client()
        resp = client.get("/indexer/api?t=caps&apikey=wrongkey")
        assert resp.status_code == 200
        root = ET.fromstring(resp.data)
        assert root.tag == "error"
        assert root.get("code") == "100"

    # -- Key set: correct apikey → normal responses --

    def test_key_set_correct_apikey_caps_returns_caps(self):
        client = self._make_keyed_app().test_client()
        resp = client.get(f"/indexer/api?t=caps&apikey={self.KEY}")
        assert resp.status_code == 200
        root = ET.fromstring(resp.data)
        assert root.tag == "caps"

    def test_key_set_correct_apikey_search_returns_results(self):
        release = _make_release()
        svc = FakeSearchService(results=[release])
        store = FakeReleaseStore()
        store._releases["test-id-001"] = release
        client = self._make_keyed_app(
            search_service=svc, release_store=store
        ).test_client()
        resp = client.get(f"/indexer/api?t=music&artist=A&album=B&apikey={self.KEY}")
        assert resp.status_code == 200
        root = ET.fromstring(resp.data)
        assert root.tag != "error"

    # -- Enclosure URLs contain apikey when key is set --

    def test_key_set_search_enclosure_url_contains_apikey(self):
        release = _make_release()
        svc = FakeSearchService(results=[release])
        store = FakeReleaseStore()
        store._releases["test-id-001"] = release
        client = self._make_keyed_app(
            search_service=svc, release_store=store
        ).test_client()
        resp = client.get(f"/indexer/api?t=music&artist=A&album=B&apikey={self.KEY}")
        root = ET.fromstring(resp.data)
        enclosures = root.findall(".//enclosure")
        assert len(enclosures) == 1
        url = enclosures[0].get("url", "")
        assert f"apikey={self.KEY}" in url

    def test_key_set_sentinel_url_contains_apikey(self):
        client = self._make_keyed_app().test_client()
        resp = client.get(f"/indexer/api?t=music&apikey={self.KEY}")
        root = ET.fromstring(resp.data)
        enclosures = root.findall(".//enclosure")
        assert len(enclosures) == 1
        url = enclosures[0].get("url", "")
        assert f"apikey={self.KEY}" in url

    def test_no_key_enclosure_url_has_no_apikey(self):
        release = _make_release()
        svc = FakeSearchService(results=[release])
        store = FakeReleaseStore()
        store._releases["test-id-001"] = release
        client = _make_app(search_service=svc, release_store=store).test_client()
        resp = client.get("/indexer/api?t=music&artist=A&album=B")
        root = ET.fromstring(resp.data)
        enclosures = root.findall(".//enclosure")
        assert len(enclosures) == 1
        url = enclosures[0].get("url", "")
        assert "apikey=" not in url

    # -- NZB route: key set → accepts correct apikey, rejects missing/wrong --

    def test_key_set_nzb_route_accepts_correct_key(self):
        release = _make_release()
        store = FakeReleaseStore()
        store._releases["test-id-001"] = release
        client = self._make_keyed_app(release_store=store).test_client()
        resp = client.get(f"/indexer/nzb/test-id-001?apikey={self.KEY}")
        assert resp.status_code == 200
        assert resp.content_type == "application/x-nzb"

    def test_key_set_nzb_route_rejects_missing_key(self):
        release = _make_release()
        store = FakeReleaseStore()
        store._releases["test-id-001"] = release
        client = self._make_keyed_app(release_store=store).test_client()
        resp = client.get("/indexer/nzb/test-id-001")
        assert resp.status_code == 200
        root = ET.fromstring(resp.data)
        assert root.tag == "error"
        assert root.get("code") == "100"

    def test_key_set_nzb_route_rejects_wrong_key(self):
        release = _make_release()
        store = FakeReleaseStore()
        store._releases["test-id-001"] = release
        client = self._make_keyed_app(release_store=store).test_client()
        resp = client.get("/indexer/nzb/test-id-001?apikey=badkey")
        assert resp.status_code == 200
        root = ET.fromstring(resp.data)
        assert root.tag == "error"
        assert root.get("code") == "100"
