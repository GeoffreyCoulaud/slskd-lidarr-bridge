"""Tests for SearchService (Task 7)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from slskd_lidarr_bridge.domain.models import (
    AudioFile,
    Release,
    SearchQuery,
    SearchResponse,
    Transfer,
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
    def transfers(self, username: str) -> list[Transfer]:
        return []

    def cancel(self, username: str, transfer_id: str) -> None: ...
    def downloads_directory(self) -> str:
        return "/downloads"


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


class FakeStorePurge(FakeStore):
    """FakeStore that records purge_older_than calls for assertion."""

    def __init__(self) -> None:
        super().__init__()
        self.purge_calls: list[datetime] = []

    def purge_older_than(self, cutoff: datetime) -> None:
        self.purge_calls.append(cutoff)


class FakeClock:
    """Clock that records sleeps and advances now() by advance_per_sleep each time."""

    def __init__(
        self,
        start: datetime | None = None,
        advance_per_sleep: float = 1.0,
    ) -> None:
        self._now = start or datetime(2024, 1, 1, tzinfo=UTC)
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
    # Artist comes from the real folder layout (grandparent), not the query.
    assert r.artist == "Artist"
    assert r.album == "My Album"
    assert r.quality == "FLAC"
    assert r.size == sum(f.size for f in files)
    assert r.id is not None
    assert len(r.files) == 3
    assert all(f.is_audio for f in r.files)


def test_same_album_quality_distinct_titles_per_uploader():
    """Two uploaders offering the same folder+quality must get distinct titles.

    Artist/album are derived from the (identical) real folder, so without the
    uploader tag both results would be named identically and Lidarr's interactive
    search would be impossible to disambiguate. The uploader is appended
    scene-style to keep each release distinguishable.
    """
    files_a = [make_flac("My Album", i) for i in range(1, 3)]
    files_b = [make_flac("My Album", i) for i in range(1, 3)]
    resp_a = make_response("alice", files_a)
    resp_b = make_response("bob", files_b)
    gateway = FakeGateway(completes_on=1, responses=[resp_a, resp_b])
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock)

    releases = service.search(SearchQuery(artist="My Artist", album="My Album"))

    assert len(releases) == 2
    by_user = {r.username: r.title for r in releases}
    # Artist "Artist" + album "My Album" come from the make_flac folder layout.
    assert by_user["alice"] == "Artist - My Album [FLAC]-alice"
    assert by_user["bob"] == "Artist - My Album [FLAC]-bob"


def test_same_album_name_different_artist_keeps_real_artist():
    """A same-named album by a *different* artist must keep its real artist.

    Soulseek's text search is fuzzy: searching Bob/HelloWorld also surfaces
    Alice/HelloWorld. Each result must carry the real folder artist so Lidarr
    rejects the non-matching one (Alice ≠ Bob) instead of showing both as the
    searched artist. Reproduces the reported false-positive/mislabel bug.
    """
    alice_file = AudioFile(
        filename=r"@@p1\Music\Alice\HelloWorld\01.flac",
        size=10_000_000,
        extension=".flac",
    )
    bob_file = AudioFile(
        filename=r"@@p2\Music\Bob\HelloWorld\01.flac",
        size=10_000_000,
        extension=".flac",
    )
    # Two distinct Soulseek peers (separate responses).
    resp_alice = make_response("peer_one", [alice_file])
    resp_bob = make_response("peer_two", [bob_file])
    gateway = FakeGateway(completes_on=1, responses=[resp_alice, resp_bob])
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock)

    releases = service.search(SearchQuery(artist="Bob", album="HelloWorld"))

    by_artist = {r.artist: r for r in releases}
    assert set(by_artist) == {"Alice", "Bob"}
    assert by_artist["Alice"].album == "HelloWorld"
    assert by_artist["Alice"].title.startswith("Alice - HelloWorld [FLAC]")
    assert by_artist["Bob"].title.startswith("Bob - HelloWorld [FLAC]")


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

    assert result == []
    assert (
        len(clock.sleeps) == 1
    )  # exactly one sleep: elapsed hits timeout after first advance


def test_timeout_logs_warning(caplog):
    """A search hitting the timeout logs a WARNING (distinct from 'no results')."""
    gateway = FakeGateway(completes_on=9999, responses=[])
    store = FakeStore()
    clock = FakeClock(advance_per_sleep=5.0)
    service = SearchService(gateway, store, clock, search_timeout=5, poll_interval=1.0)

    with caplog.at_level(logging.WARNING):
        service.search(SearchQuery(artist="A", album="B"))

    assert any(
        r.levelno == logging.WARNING and "timed out" in r.getMessage().lower()
        for r in caplog.records
        if "search_service" in r.name
    )


def test_ordering_free_slot_before_no_slot():
    """Response with free upload slot sorts before faster-but-no-slot response."""
    file1 = make_flac("AlbumA", 1)
    file2 = make_flac("AlbumB", 1)
    resp_free = make_response(
        "alice", [file1], has_free_upload_slot=True, upload_speed=500_000
    )
    resp_fast = make_response(
        "bob", [file2], has_free_upload_slot=False, upload_speed=2_000_000
    )
    gateway = FakeGateway(completes_on=1, responses=[resp_fast, resp_free])
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock)

    releases = service.search(SearchQuery(artist="X", album="Y"))

    assert len(releases) == 2
    assert releases[0].username == "alice"  # free slot first
    assert releases[1].username == "bob"


def test_min_bitrate_filters_low_bitrate_files():
    high = AudioFile(
        filename=r"@@a\Artist\Album\01.mp3",
        size=5_000_000,
        extension=".mp3",
        bitrate=320,
    )
    low = AudioFile(
        filename=r"@@a\Artist\Album\02.mp3",
        size=5_000_000,
        extension=".mp3",
        bitrate=128,
    )
    response = make_response("alice", [high, low])
    gateway = FakeGateway(completes_on=1, responses=[response])
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock, min_bitrate=192)

    releases = service.search(SearchQuery(artist="A", album="B"))

    assert len(releases) == 1
    assert len(releases[0].files) == 1
    assert releases[0].files[0].bitrate == 320


def test_min_bitrate_keeps_unknown_bitrate_files():
    """Files with bitrate=None are kept when min_bitrate is set (conservative)."""
    unknown_bitrate = AudioFile(
        filename=r"@@a\Artist\Album\01.flac",
        size=10_000_000,
        extension=".flac",
        bitrate=None,
    )
    response = make_response("alice", [unknown_bitrate])
    gateway = FakeGateway(completes_on=1, responses=[response])
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock, min_bitrate=192)

    releases = service.search(SearchQuery(artist="A", album="B"))

    assert len(releases) == 1
    assert len(releases[0].files) == 1
    assert releases[0].files[0].bitrate is None


def test_non_audio_files_excluded():
    audio = make_flac("Album", 1)
    cover = AudioFile(
        filename=r"@@a\Artist\Album\cover.jpg", size=100_000, extension=".jpg"
    )
    response = make_response("alice", [audio, cover])
    gateway = FakeGateway(completes_on=1, responses=[response])
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock)

    releases = service.search(SearchQuery(artist="A", album="B"))

    assert len(releases) == 1
    assert all(f.is_audio for f in releases[0].files)
    assert len(releases[0].files) == 1


def test_response_with_no_audio_files_is_skipped():
    """A response whose files are all non-audio yields no release; a sibling
    response with audio still produces one."""
    only_non_audio = make_response(
        "noaudio",
        [
            AudioFile(
                filename=r"@@a\Artist\Album\cover.jpg",
                size=100_000,
                extension=".jpg",
            ),
            AudioFile(
                filename=r"@@a\Artist\Album\notes.txt",
                size=2_000,
                extension=".txt",
            ),
        ],
    )
    with_audio = make_response("hasaudio", [make_flac("Album", 1)])
    gateway = FakeGateway(completes_on=1, responses=[only_non_audio, with_audio])
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock)

    releases = service.search(SearchQuery(artist="A", album="B"))

    assert len(releases) == 1
    assert releases[0].username == "hasaudio"


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
    """No ' - ' in the folder: album=folder, artist=grandparent folder."""
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
    assert r.artist == "Artist"
    assert r.album == "RandomAlbum"


def test_flat_folder_yields_empty_artist():
    """A too-flat layout (no folder above the album) yields an empty artist, so
    Lidarr cannot attribute it to the searched artist."""
    f = AudioFile(filename=r"HelloWorld\01.flac", size=10_000_000, extension=".flac")
    response = make_response("alice", [f])
    gateway = FakeGateway(completes_on=1, responses=[response])
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock)

    releases = service.search(SearchQuery(artist="Bob", album="HelloWorld"))

    assert len(releases) == 1
    assert releases[0].artist == ""
    assert releases[0].album == "HelloWorld"


def test_flat_combined_artist_album_folder_splits_on_dash():
    """A flat layout whose only folder is 'Artist - Album' (no separate artist
    folder above it) still yields the real artist via the ' - ' split, so Lidarr
    can match it. The split takes precedence over the (here useless) grandparent.
    """
    f = AudioFile(
        filename=r"@@peer\Bob - HelloWorld\01.flac",
        size=10_000_000,
        extension=".flac",
    )
    response = make_response("peer", [f])
    gateway = FakeGateway(completes_on=1, responses=[response])
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock)

    releases = service.search(SearchQuery(artist="Bob", album="HelloWorld"))

    assert len(releases) == 1
    r = releases[0]
    assert r.artist == "Bob"
    assert r.album == "HelloWorld"
    assert r.title.startswith("Bob - HelloWorld [FLAC]")


# ---------------------------------------------------------------------------
# Tests — release retention (purge_older_than)
# ---------------------------------------------------------------------------


def test_real_search_triggers_purge_with_correct_cutoff():
    """A non-empty search calls purge_older_than with cutoff = now - ttl_days."""
    files = [make_flac("Album", 1)]
    response = make_response("alice", files)
    gateway = FakeGateway(completes_on=1, responses=[response])
    store = FakeStorePurge()
    clock = FakeClock()
    service = SearchService(gateway, store, clock, release_ttl_days=7)

    service.search(SearchQuery(artist="A", album="B"))

    assert len(store.purge_calls) == 1
    expected_cutoff = clock.now() - timedelta(days=7)
    assert store.purge_calls[0] == expected_cutoff


def test_empty_query_does_not_purge_and_no_gateway_call():
    """An empty query returns early before purging or touching the gateway."""
    gateway = FakeGateway()
    store = FakeStorePurge()
    clock = FakeClock()
    service = SearchService(gateway, store, clock, release_ttl_days=7)

    result = service.search(SearchQuery())

    assert result == []
    assert gateway.started_searches == []
    assert store.purge_calls == []
