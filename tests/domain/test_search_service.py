"""Tests for SearchService (Task 7)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

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
    """Scriptable SoulseekGateway.

    Each started search has its own poll counter (so multiple searches in one
    SearchService.search() call are independent). `responses_by_text` maps a
    query string to its responses; any unmapped text falls back to `responses`.
    """

    def __init__(
        self,
        *,
        completes_on: int = 1,
        responses: list[SearchResponse] | None = None,
        responses_by_text: dict[str, list[SearchResponse]] | None = None,
    ) -> None:
        self._completes_on = completes_on
        self._default: list[SearchResponse] = responses or []
        self._by_text = responses_by_text or {}
        self.started_searches: list[str] = []
        self.started_timeouts: list[float] = []
        self._sid_counter = 0
        self._sid_text: dict[str, str] = {}
        self._poll_counts: dict[str, int] = {}

    def start_search(self, text: str, timeout_seconds: float) -> str:
        self._sid_counter += 1
        sid = f"search-{self._sid_counter}"
        self.started_searches.append(text)
        self.started_timeouts.append(timeout_seconds)
        self._sid_text[sid] = text
        self._poll_counts[sid] = 0
        return sid

    def search_is_complete(self, search_id: str) -> bool:
        self._poll_counts[search_id] += 1
        return self._poll_counts[search_id] >= self._completes_on

    def search_responses(self, search_id: str) -> list[SearchResponse]:
        return list(self._by_text.get(self._sid_text[search_id], self._default))

    # Unused in search service — satisfy Protocol
    def enqueue(self, username: str, files: list[AudioFile]) -> None: ...
    def transfers(self, username: str) -> list[Transfer]:
        return []

    def cancel(self, username: str, transfer_id: str) -> None: ...
    def downloads_directory(self) -> str:
        return "/downloads"


class RacyGateway:
    """SoulseekGateway that models slskd's real search lifecycle.

    Unlike ``FakeGateway`` (which returns responses regardless of completion),
    this fake mirrors the timing that broke the bridge end-to-end:

    * a search reports ``isComplete`` only on the ``completes_after``-th poll —
      i.e. slskd keeps gathering past its own search window;
    * ``search_responses`` yields the mapped responses **only once the search has
      completed**; a search the bridge abandons mid-flight returns ``[]``, exactly
      as slskd has not yet populated ``/responses``;
    * ``start_search`` for a 1-based index listed in ``fail_start_on`` raises,
      modelling slskd's single-submission semaphore / Soulseek refusing a search
      that arrives too soon (HTTP 429).
    """

    def __init__(
        self,
        *,
        completes_after: int = 1,
        responses_by_text: dict[str, list[SearchResponse]] | None = None,
        default_responses: list[SearchResponse] | None = None,
        fail_start_on: frozenset[int] = frozenset(),
    ) -> None:
        self._completes_after = completes_after
        self._by_text = responses_by_text or {}
        self._default = default_responses or []
        self._fail_start_on = set(fail_start_on)
        self.started_searches: list[str] = []
        self.started_timeouts: list[float] = []
        self._sid_counter = 0
        self._sid_text: dict[str, str] = {}
        self._poll_counts: dict[str, int] = {}

    def start_search(self, text: str, timeout_seconds: float) -> str:
        n = self._sid_counter + 1
        if n in self._fail_start_on:
            raise RuntimeError(f"slskd refused search #{n} (429 too soon)")
        self._sid_counter = n
        sid = f"search-{n}"
        self.started_searches.append(text)
        self.started_timeouts.append(timeout_seconds)
        self._sid_text[sid] = text
        self._poll_counts[sid] = 0
        return sid

    def search_is_complete(self, search_id: str) -> bool:
        self._poll_counts[search_id] += 1
        return self._poll_counts[search_id] >= self._completes_after

    def search_responses(self, search_id: str) -> list[SearchResponse]:
        if self._poll_counts[search_id] < self._completes_after:
            return []  # abandoned before slskd finalised → nothing gathered yet
        return list(self._by_text.get(self._sid_text[search_id], self._default))

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
    queue_length: int = 0,
) -> SearchResponse:
    return SearchResponse(
        username=username,
        has_free_upload_slot=has_free_upload_slot,
        upload_speed=upload_speed,
        queue_length=queue_length,
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
    service = SearchService(
        gateway, store, clock, search_timeout=30, poll_interval=1.0, enough_results=1
    )

    service.search(SearchQuery(artist="A", album="B"))

    assert clock.sleeps == [1.0, 1.0]


def test_timeout_stops_polling_without_infinite_loop():
    """When gateway never completes, service breaks after the completion wait."""
    gateway = FakeGateway(completes_on=9999, responses=[])
    store = FakeStore()
    # advance 100 s per sleep so elapsed exceeds the completion wait (search
    # window + grace) after a single sleep, whatever the grace constant is.
    clock = FakeClock(advance_per_sleep=100.0)
    service = SearchService(
        gateway, store, clock, search_timeout=5, poll_interval=1.0, enough_results=0
    )

    result = service.search(SearchQuery(artist="A", album="B"))

    assert result == []
    assert (
        len(clock.sleeps) == 1
    )  # exactly one sleep: elapsed hits the wait cap after first advance


def test_timeout_logs_warning(caplog):
    """A search hitting the timeout logs a WARNING (distinct from 'no results')."""
    gateway = FakeGateway(completes_on=9999, responses=[])
    store = FakeStore()
    clock = FakeClock(advance_per_sleep=5.0)
    service = SearchService(
        gateway, store, clock, search_timeout=5, poll_interval=1.0, enough_results=0
    )

    with caplog.at_level(logging.WARNING):
        service.search(SearchQuery(artist="A", album="B"))

    assert any(
        r.levelno == logging.WARNING
        and "did not complete within" in r.getMessage().lower()
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


def test_ordering_shorter_queue_first_when_slot_and_speed_equal():
    """Same free-slot and upload speed: the shorter remote queue ranks first.

    slskd reports each peer's queue length; a long queue means a long wait even
    from a fast peer, so among otherwise-equal peers we prefer the shorter queue.
    """
    resp_long = make_response(
        "longq", [make_flac("AlbumA", 1)], upload_speed=1_000_000, queue_length=10
    )
    resp_short = make_response(
        "shortq", [make_flac("AlbumB", 1)], upload_speed=1_000_000, queue_length=2
    )
    gateway = FakeGateway(completes_on=1, responses=[resp_long, resp_short])
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock)

    releases = service.search(SearchQuery(artist="X", album="Y"))

    assert [r.username for r in releases] == ["shortq", "longq"]


def test_ordering_free_slot_outranks_shorter_queue():
    """A free upload slot dominates queue length: free-but-long-queue still wins."""
    resp_free_longq = make_response(
        "free",
        [make_flac("AlbumA", 1)],
        has_free_upload_slot=True,
        upload_speed=1_000_000,
        queue_length=50,
    )
    resp_noslot_shortq = make_response(
        "noslot",
        [make_flac("AlbumB", 1)],
        has_free_upload_slot=False,
        upload_speed=1_000_000,
        queue_length=0,
    )
    gateway = FakeGateway(
        completes_on=1, responses=[resp_noslot_shortq, resp_free_longq]
    )
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock)

    releases = service.search(SearchQuery(artist="X", album="Y"))

    assert [r.username for r in releases] == ["free", "noslot"]


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


def test_walks_to_fallback_when_primary_returns_too_few():
    # Primary ("Beyonce Lemonade Deluxe") finds nothing; the edition-stripped
    # fallback ("Beyonce Lemonade") finds an album.
    primary = "Beyonce Lemonade (Deluxe)"
    fallback = "Beyonce Lemonade"
    resp = make_response("alice", [make_flac("Lemonade", 1)])
    gateway = FakeGateway(
        completes_on=1,
        responses_by_text={fallback: [resp]},  # primary text → [] (default)
    )
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock, enough_results=3)

    releases = service.search(SearchQuery(artist="Beyonce", album="Lemonade (Deluxe)"))

    assert len(releases) == 1
    assert gateway.started_searches[0] == primary
    assert fallback in gateway.started_searches


def test_stops_at_threshold_after_primary():
    # Primary alone yields 3 folders (= enough_results) → no fallback issued.
    resp = make_response(
        "alice",
        [make_flac("A", 1), make_flac("B", 1), make_flac("C", 1)],
    )
    gateway = FakeGateway(completes_on=1, responses_by_text={"X Y": [resp]})
    service = SearchService(gateway, FakeStore(), FakeClock(), enough_results=3)
    releases = service.search(SearchQuery(artist="X", album="Y"))
    assert len(releases) == 3
    assert gateway.started_searches == ["X Y"]  # exactly one search


def test_budget_zero_runs_primary_only():
    resp = make_response("alice", [make_flac("Album", 1)])
    gateway = FakeGateway(completes_on=1, responses=[resp])
    service = SearchService(
        gateway, FakeStore(), FakeClock(), enough_results=99, search_budget=0
    )
    service.search(SearchQuery(artist="A", album="B"))
    assert len(gateway.started_searches) == 1  # fallbacks disabled by budget<=0


def test_dedup_same_user_folder_across_candidates_counts_once():
    resp = make_response("alice", [make_flac("Album", 1)])
    # Same response for primary and every fallback text.
    gateway = FakeGateway(completes_on=1, responses=[resp])
    service = SearchService(gateway, FakeStore(), FakeClock(), enough_results=99)
    releases = service.search(SearchQuery(artist="A", album="B"))
    assert len(releases) == 1  # deduped on (username, album_folder)
    assert len(gateway.started_searches) >= 2  # walked candidates (never reached 99)


def test_primary_completing_after_slskd_window_is_captured_not_abandoned():
    """The bridge must wait for slskd to finish, not race its own search window.

    Regression for the end-to-end miss: ``search_timeout`` was used both as
    slskd's search window (forwarded on the POST) and as the bridge's poll cap,
    so the bridge gave up at the exact moment slskd was about to report a full
    result set — fetching an empty ``/responses`` and firing a premature fallback
    that Soulseek refuses. slskd here reports ``isComplete`` only on the 8th poll
    (~7 s), past the 5 s window; the bridge must still capture all three folders
    and issue no second search.
    """
    resp = make_response(
        "alice", [make_flac("A", 1), make_flac("B", 1), make_flac("C", 1)]
    )
    gateway = RacyGateway(completes_after=8, responses_by_text={"Artist Album": [resp]})
    service = SearchService(
        gateway,
        FakeStore(),
        FakeClock(),
        search_timeout=5,
        poll_interval=1.0,
        enough_results=3,
    )

    releases = service.search(SearchQuery(artist="Artist", album="Album"))

    assert len(releases) == 3  # full result set captured, not abandoned
    assert gateway.started_searches == ["Artist Album"]  # no premature fallback


def test_candidate_search_failure_preserves_earlier_results():
    """A refused fallback must not discard results already collected.

    The primary yields one folder (< enough_results), so the loop advances to a
    fallback whose submission slskd refuses (429). That failure must be swallowed
    and the primary's release still returned — not propagated to abort the whole
    search (which today loses even the results Lidarr should have seen).
    """
    resp = make_response("alice", [make_flac("OnlyOne", 1)])
    gateway = RacyGateway(
        completes_after=1,
        responses_by_text={"Artist Album": [resp]},
        fail_start_on=frozenset({2}),
    )
    service = SearchService(
        gateway, FakeStore(), FakeClock(), search_timeout=5, enough_results=3
    )

    releases = service.search(SearchQuery(artist="Artist", album="Album"))

    assert [r.album_folder for r in releases] == ["OnlyOne"]  # primary survives
    assert gateway.started_searches == ["Artist Album"]  # fallback #2 was refused


def test_primary_search_failure_propagates_when_nothing_collected():
    """If the very first search fails and nothing was collected, the error must
    propagate so the Lidarr surfaces return an error envelope, not a false
    'no results'."""
    gateway = RacyGateway(fail_start_on=frozenset({1}))  # primary refused
    service = SearchService(gateway, FakeStore(), FakeClock(), search_timeout=5)

    with pytest.raises(RuntimeError, match="refused search #1"):
        service.search(SearchQuery(artist="Artist", album="Album"))


# ---------------------------------------------------------------------------
# Per-search window sizing (what searchTimeout is forwarded to slskd)
# ---------------------------------------------------------------------------


def test_idle_window_not_derived_from_budget():
    """Regression for the end-to-end miss (slskd shows 100+ results, Lidarr 0).

    ``searchTimeout`` is slskd's INACTIVITY window (reset on every response), not
    a wall-clock cap. The old code forwarded the whole budget (75−5=70 s) as that
    window when ``search_timeout=0``; on a busy query the idle timer never fired
    inside the budget, so the bridge timed out and read an empty ``/responses``.
    ``0`` must now mean "use slskd's own default idle window" (field omitted),
    never "spend the whole budget idling".
    """
    resp = make_response("alice", [make_flac("Folder", 1)])
    gateway = FakeGateway(completes_on=1, responses=[resp])
    service = SearchService(
        gateway,
        FakeStore(),
        FakeClock(),
        search_timeout=0,
        search_budget=75,
        enough_results=99,
    )

    service.search(SearchQuery(artist="Artist", album="Album"))

    assert gateway.started_timeouts[0] == 0.0  # slskd default (omitted), not 70


def test_poll_cap_is_budget_not_the_idle_window():
    """The bridge must poll for ``isComplete`` up to the whole budget, not just
    the forwarded idle window.

    Old code capped polling at ``window + grace`` (≈ the idle window). Real slskd
    completes only once its inactivity timer fires — often *after* the idle window
    on a query with late stragglers — so a search that would have completed within
    the budget was abandoned and its ``/responses`` read empty. Here slskd reports
    ``isComplete`` on the 10th poll (~10 s), well past the 5 s idle window; the
    result must still be captured.
    """
    resp = make_response("alice", [make_flac("Folder", 1)])
    gateway = RacyGateway(completes_after=10, default_responses=[resp])
    clock = FakeClock(advance_per_sleep=1.0)
    service = SearchService(
        gateway,
        FakeStore(),
        clock,
        search_timeout=5,
        search_budget=75,
        poll_interval=1.0,
        enough_results=99,
    )

    releases = service.search(SearchQuery(artist="Artist", album="Album"))

    assert len(releases) == 1  # captured, not abandoned at idle-window + grace


def test_forwards_configured_idle_window_unchanged():
    # The configured idle window is forwarded to slskd verbatim, never scaled by
    # the budget (contrast the old budget−margin window). A 300 s budget still
    # forwards the small 15 s idle window.
    resp = make_response("alice", [make_flac("Folder", 1)])
    gateway = RacyGateway(completes_after=1, default_responses=[resp])
    service = SearchService(
        gateway,
        FakeStore(),
        FakeClock(),
        search_timeout=15,
        search_budget=300,
        enough_results=99,
    )

    service.search(SearchQuery(artist="Artist", album="Album"))

    assert gateway.started_timeouts[0] == 15.0  # idle window, not 300 − margin


def test_same_idle_window_forwarded_to_every_candidate():
    # Every candidate gets the same bounded idle window; it is not shrunk per
    # search to "fit" the budget (the budget is the poll cap, not the slskd timer).
    resp = make_response("alice", [make_flac("Folder", 1)])
    gateway = RacyGateway(completes_after=1, default_responses=[resp])
    service = SearchService(
        gateway,
        FakeStore(),
        FakeClock(),
        search_timeout=20,
        search_budget=75,
        enough_results=99,
    )

    service.search(SearchQuery(artist="Beyonce", album="Lemonade (Deluxe)"))

    assert gateway.started_timeouts  # candidates ran
    assert all(t == 20.0 for t in gateway.started_timeouts)


def test_fallbacks_stop_once_budget_is_exhausted():
    # Each search burns ~40 s of wall clock (completes_after=41 @ 1 s/poll). With a
    # 75 s budget: primary (~40 s) then one fallback (times out at the remaining
    # 35 s), leaving < the 5 s floor — so the third candidate is never started.
    resp = make_response("alice", [make_flac("Folder", 1)])
    gateway = RacyGateway(completes_after=41, default_responses=[resp])
    clock = FakeClock(advance_per_sleep=1.0)
    service = SearchService(
        gateway,
        FakeStore(),
        clock,
        search_timeout=15,
        search_budget=75,
        poll_interval=1.0,
        enough_results=99,
    )

    service.search(SearchQuery(artist="Beyonce", album="Lemonade (Deluxe)"))

    # Primary + one fallback started; the album-only candidate is skipped.
    assert gateway.started_searches == ["Beyonce Lemonade (Deluxe)", "Beyonce Lemonade"]


def test_primary_deadline_floored_when_budget_tiny():
    # A misconfigured tiny budget must not starve the primary: its poll deadline is
    # floored to _MIN_SEARCH_WINDOW, so a search completing within that floor is
    # captured instead of abandoned on the first poll.
    resp = make_response("alice", [make_flac("Folder", 1)])
    gateway = RacyGateway(completes_after=3, default_responses=[resp])
    clock = FakeClock(advance_per_sleep=1.0)
    service = SearchService(
        gateway,
        FakeStore(),
        clock,
        search_timeout=15,
        search_budget=0,
        poll_interval=1.0,
        enough_results=99,
    )

    releases = service.search(SearchQuery(artist="Artist", album="Album"))

    assert len(releases) == 1  # primary captured under the floored deadline
    assert len(gateway.started_searches) == 1  # budget 0 → no fallback
