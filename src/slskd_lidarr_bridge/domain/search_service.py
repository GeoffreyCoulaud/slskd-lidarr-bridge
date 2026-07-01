"""Domain use-case: search slskd for releases."""

from __future__ import annotations

import dataclasses
import datetime
import logging
from collections import defaultdict

from slskd_lidarr_bridge.domain.models import (
    AudioFile,
    Release,
    SearchQuery,
    SearchResponse,
)
from slskd_lidarr_bridge.domain.ports import Clock, ReleaseStore, SoulseekGateway
from slskd_lidarr_bridge.domain.quality import detect_quality
from slskd_lidarr_bridge.domain.query_candidates import generate_candidates
from slskd_lidarr_bridge.domain.titles import build_title

logger = logging.getLogger(__name__)

# Each candidate search takes the wall-clock time still available in the budget
# (see ``search_budget``). These constants bound that window.

# Reserved between one search finishing and the next candidate being submitted.
# slskd serialises search submissions (an overlap gets an immediate HTTP 429) and
# needs a beat to finish its current search and free the slot; this margin also
# keeps the total comfortably under Lidarr's ~100 s request abort.
_INTER_SEARCH_MARGIN = 5.0
# A search shorter than this is not worth a round-trip and would fall below
# slskd's minimum ``searchTimeout`` (5 s), so we stop rather than start one.
_MIN_SEARCH_WINDOW = 5.0
# Poll a little past the window we forwarded to slskd: it flips ``isComplete``
# just after its search window ends, and we want the finished result set.
_COMPLETION_GRACE = 3.0


class SearchService:
    def __init__(
        self,
        gateway: SoulseekGateway,
        store: ReleaseStore,
        clock: Clock,
        *,
        search_timeout: int = 0,
        poll_interval: float = 1.0,
        min_bitrate: int | None = None,
        release_ttl_days: int = 7,
        min_results: int = 3,
        search_budget: int = 75,
    ) -> None:
        self._gateway = gateway
        self._store = store
        self._clock = clock
        self._search_timeout = search_timeout
        self._poll_interval = poll_interval
        self._min_bitrate = min_bitrate
        self._release_ttl_days = release_ttl_days
        self._min_results = min_results
        self._search_budget = search_budget

    def search(self, query: SearchQuery) -> list[Release]:
        if query.is_empty:
            return []

        self._store.purge_older_than(
            self._clock.now() - datetime.timedelta(days=self._release_ttl_days)
        )

        candidates = generate_candidates(query)
        seen: set[tuple[str, str]] = set()
        # (has_free_upload_slot, upload_speed, queue_length, release) — for sorting.
        tagged: list[tuple[bool, int, int, Release]] = []
        start = self._clock.now()

        for index, text in enumerate(candidates):
            elapsed = (self._clock.now() - start).total_seconds()
            window = self._search_window(self._search_budget - elapsed)
            if index == 0:
                # The primary always runs; a tight budget must not starve it.
                window = max(window, _MIN_SEARCH_WINDOW)
            else:
                if len(seen) >= self._min_results:
                    break
                # Only start another candidate if enough budget remains for a
                # worthwhile search; a shorter one just hammers Soulseek.
                if window < _MIN_SEARCH_WINDOW:
                    break

            try:
                responses = self._run_search(text, window)
            except Exception:
                if not tagged:
                    # Nothing collected yet: a failing search is a genuine error
                    # (e.g. slskd is down). Surface it so Lidarr gets an error
                    # envelope rather than a false "no results".
                    raise
                # A later, looser candidate failing (429/network) must not discard
                # results already collected from an earlier, higher-precision
                # candidate. Keep them and stop walking.
                logger.warning(
                    "Candidate search %r failed; keeping earlier results",
                    text,
                    exc_info=True,
                )
                break
            self._collect(responses, seen, tagged)

        # Order by (free slot desc, upload speed desc, queue length asc).
        tagged.sort(key=lambda x: (x[0], x[1], -x[2]), reverse=True)
        return [r for *_, r in tagged]

    def _search_window(self, remaining: float) -> float:
        """Seconds a single search may run: the remaining wall-clock budget minus
        the inter-search margin, optionally capped by the per-query maximum.

        With ``search_timeout == 0`` (default) a search takes the whole remaining
        budget — slskd's native "one search per query" behaviour. A positive
        ``search_timeout`` caps each search so several looser candidates fit inside
        the budget instead.
        """
        window = remaining - _INTER_SEARCH_MARGIN
        if self._search_timeout > 0:
            window = min(window, float(self._search_timeout))
        return window

    def _run_search(self, text: str, window: float) -> list[SearchResponse]:
        # Forward the window to slskd as its own searchTimeout so it stops
        # gathering when we stop polling — no lingering search to 429 the next
        # candidate. Poll a little past it to catch isComplete.
        sid = self._gateway.start_search(text, window)
        poll_cap = window + _COMPLETION_GRACE
        start = self._clock.now()
        while not self._gateway.search_is_complete(sid):
            elapsed = (self._clock.now() - start).total_seconds()
            if elapsed >= poll_cap:
                logger.warning(
                    "Search %r timed out after %ss; returning partial results",
                    text,
                    poll_cap,
                )
                break
            self._clock.sleep(self._poll_interval)
        return self._gateway.search_responses(sid)

    def _collect(
        self,
        responses: list[SearchResponse],
        seen: set[tuple[str, str]],
        tagged: list[tuple[bool, int, int, Release]],
    ) -> None:
        for response in responses:
            audio: list[AudioFile] = [
                f
                for f in response.files
                if f.is_audio
                and (
                    self._min_bitrate is None
                    or f.bitrate is None
                    or f.bitrate >= self._min_bitrate
                )
            ]
            if not audio:
                continue

            groups: dict[str, list[AudioFile]] = defaultdict(list)
            for f in audio:
                groups[f.album_folder].append(f)

            for folder, files in groups.items():
                key = (response.username, folder)
                if key in seen:
                    continue

                if " - " in folder:
                    left, right = folder.split(" - ", 1)
                    artist, album = left.strip(), right.strip()
                else:
                    album = folder
                    artist = files[0].artist_folder

                size = sum(f.size for f in files)
                quality = detect_quality(files)
                title = build_title(artist, album, quality, response.username)
                release = Release(
                    artist=artist,
                    album=album,
                    title=title,
                    username=response.username,
                    files=tuple(files),
                    size=size,
                    album_folder=folder,
                    quality=quality,
                    created_at=self._clock.now(),
                )
                release_id = self._store.put(release)
                release = dataclasses.replace(release, id=release_id)
                seen.add(key)
                tagged.append(
                    (
                        response.has_free_upload_slot,
                        response.upload_speed,
                        response.queue_length,
                        release,
                    )
                )
