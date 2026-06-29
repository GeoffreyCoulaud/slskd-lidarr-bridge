"""Tests for SearchService (Task 7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from slskd_lidarr_bridge.domain.models import (
    AudioFile,
    Release,
    SearchQuery,
    SearchResponse,
)
from slskd_lidarr_bridge.domain.search_service import SearchService


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeGateway:
    """Scriptable SoulseekGateway: completes after `completes_on` checks."""

    def __init__(
        self,
        *,
        completes_on: int = 1,
        responses: list[SearchResponse] | None = None,
    ) -> None:
        self._completes_on = completes_on
        self._check_count = 0
        self._responses: list[SearchResponse] = responses or []
        self.started_searches: list[str] = []
        self._sid_counter = 0

    def start_search(self, text: str) -> str:
        self.started_searches.append(text)
        self._sid_counter += 1
        return f"search-{self._sid_counter}"

    def search_is_complete(self, search_id: str) -> bool:
        self._check_count += 1
        return self._check_count >= self._completes_on

    def search_responses(self, search_id: str) -> list[SearchResponse]:
        return list(self._responses)

    # Unused in search service — satisfy Protocol
    def enqueue(self, username: str, files: list[AudioFile]) -> None: ...
    def transfers(self, username: str) -> list:  # type: ignore[override]
        return []
    def cancel(self, username: str, transfer_id: str) -> None: ...


class FakeStore:
    """In-memory ReleaseStore with incrementing string ids."""

    def __init__(self) -> None:
        self._counter = 0
        self._store: dict[str, Release] = {}

    def put(self, release: Release) -> str:
        self._counter += 1
        rid = str(self._counter)
        self._store[rid] = release
        return rid

    def get(self, release_id: str) -> Release | None:
        return self._store.get(release_id)

    def purge_older_than(self, cutoff: datetime) -> None:
        pass


class FakeClock:
    """Clock that records sleeps and advances now() by advance_per_sleep on each sleep."""

    def __init__(
        self,
        start: datetime | None = None,
        advance_per_sleep: float = 1.0,
    ) -> None:
        self._now = start or datetime(2024, 1, 1, tzinfo=timezone.utc)
        self._advance = advance_per_sleep
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now = self._now + timedelta(seconds=self._advance)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_flac(album_folder: str, track: int, bitrate: int | None = None) -> AudioFile:
    return AudioFile(
        filename=rf"@@a\Music\Artist\{album_folder}\{track:02d}.flac",
        size=10_000_000,
        extension=".flac",
        bitrate=bitrate,
    )


def make_response(
    username: str,
    files: list[AudioFile],
    *,
    has_free_upload_slot: bool = False,
    upload_speed: int = 1_000_000,
) -> SearchResponse:
    return SearchResponse(
        username=username,
        has_free_upload_slot=has_free_upload_slot,
        upload_speed=upload_speed,
        queue_length=0,
        files=tuple(files),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_query_returns_empty_no_gateway_call():
    gateway = FakeGateway()
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock)

    result = service.search(SearchQuery())

    assert result == []
    assert gateway.started_searches == []


def test_one_response_flac_album_one_folder():
    files = [make_flac("My Album", i) for i in range(1, 4)]
    response = make_response("alice", files)
    gateway = FakeGateway(completes_on=1, responses=[response])
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock)

    releases = service.search(SearchQuery(artist="My Artist", album="My Album"))

    assert len(releases) == 1
    r = releases[0]
    assert r.username == "alice"
    assert r.artist == "My Artist"
    assert r.album == "My Album"
    assert r.quality == "FLAC"
    assert r.size == sum(f.size for f in files)
    assert r.id is not None
    assert len(r.files) == 3
    assert all(f.is_audio for f in r.files)


def test_two_folders_produce_two_releases():
    files_a = [make_flac("Album A", i) for i in range(1, 3)]
    files_b = [make_flac("Album B", i) for i in range(1, 3)]
    response = make_response("bob", files_a + files_b)
    gateway = FakeGateway(completes_on=1, responses=[response])
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock)

    releases = service.search(SearchQuery(artist="Bob", album="Test"))

    assert len(releases) == 2
    folders = {r.album_folder for r in releases}
    assert folders == {"Album A", "Album B"}


def test_polling_completes_on_third_check():
    """gateway.search_is_complete returns True on the 3rd call → 2 sleeps."""
    files = [make_flac("Album", 1)]
    response = make_response("alice", files)
    # completes_on=3: first two calls return False, third returns True
    gateway = FakeGateway(completes_on=3, responses=[response])
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock, search_timeout=30, poll_interval=1.0)

    service.search(SearchQuery(artist="A", album="B"))

    assert clock.sleeps == [1.0, 1.0]


def test_timeout_stops_polling_without_infinite_loop():
    """When gateway never completes, service breaks after search_timeout."""
    gateway = FakeGateway(completes_on=9999, responses=[])
    store = FakeStore()
    # advance 5 s per sleep so elapsed >= search_timeout=5 after the first sleep
    clock = FakeClock(advance_per_sleep=5.0)
    service = SearchService(gateway, store, clock, search_timeout=5, poll_interval=1.0)

    result = service.search(SearchQuery(artist="A", album="B"))

    assert isinstance(result, list)
    assert len(clock.sleeps) >= 1  # at least one sleep before timeout triggered


def test_ordering_free_slot_before_no_slot():
    """Response with free upload slot sorts before faster-but-no-slot response."""
    file1 = make_flac("AlbumA", 1)
    file2 = make_flac("AlbumB", 1)
    resp_free = make_response("alice", [file1], has_free_upload_slot=True, upload_speed=500_000)
    resp_fast = make_response("bob", [file2], has_free_upload_slot=False, upload_speed=2_000_000)
    gateway = FakeGateway(completes_on=1, responses=[resp_fast, resp_free])
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock)

    releases = service.search(SearchQuery(artist="X", album="Y"))

    assert len(releases) == 2
    assert releases[0].username == "alice"  # free slot first
    assert releases[1].username == "bob"


def test_min_bitrate_filters_low_bitrate_files():
    high = AudioFile(filename=r"@@a\Artist\Album\01.mp3", size=5_000_000, extension=".mp3", bitrate=320)
    low = AudioFile(filename=r"@@a\Artist\Album\02.mp3", size=5_000_000, extension=".mp3", bitrate=128)
    response = make_response("alice", [high, low])
    gateway = FakeGateway(completes_on=1, responses=[response])
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock, min_bitrate=192)

    releases = service.search(SearchQuery(artist="A", album="B"))

    assert len(releases) == 1
    assert len(releases[0].files) == 1
    assert releases[0].files[0].bitrate == 320


def test_non_audio_files_excluded():
    audio = make_flac("Album", 1)
    cover = AudioFile(filename=r"@@a\Artist\Album\cover.jpg", size=100_000, extension=".jpg")
    response = make_response("alice", [audio, cover])
    gateway = FakeGateway(completes_on=1, responses=[response])
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock)

    releases = service.search(SearchQuery(artist="A", album="B"))

    assert len(releases) == 1
    assert all(f.is_audio for f in releases[0].files)
    assert len(releases[0].files) == 1


def test_term_only_query_splits_folder_on_dash():
    """When query has only term and folder contains ' - ', derive artist and album."""
    f = AudioFile(
        filename=r"@@a\Artist\Daft Punk - Random Access Memories\01.flac",
        size=10_000_000,
        extension=".flac",
    )
    response = make_response("alice", [f])
    gateway = FakeGateway(completes_on=1, responses=[response])
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock)

    releases = service.search(SearchQuery(term="Daft Punk Random Access Memories"))

    assert len(releases) == 1
    r = releases[0]
    assert r.artist == "Daft Punk"
    assert r.album == "Random Access Memories"


def test_term_only_no_dash_uses_folder_as_album():
    """When query has only term and folder has no ' - ', album=folder, artist=''."""
    f = AudioFile(
        filename=r"@@a\Artist\RandomAlbum\01.flac",
        size=10_000_000,
        extension=".flac",
    )
    response = make_response("alice", [f])
    gateway = FakeGateway(completes_on=1, responses=[response])
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock)

    releases = service.search(SearchQuery(term="something"))

    assert len(releases) == 1
    r = releases[0]
    assert r.artist == ""
    assert r.album == "RandomAlbum"
