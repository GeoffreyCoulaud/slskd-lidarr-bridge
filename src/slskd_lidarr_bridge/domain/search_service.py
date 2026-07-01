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

# The bridge polls each candidate for ``isComplete`` up to the wall-clock time
# still available in the budget (see ``search_budget``); slskd's own *idle*
# ``searchTimeout`` (forwarded per call) plus ``responseLimit`` decide when a
# search actually finishes. These two timers are independent: the budget is the
# bridge's patience, not a value forwarded to slskd.

# Floor for a single candidate's poll deadline. A shorter wait is not worth a
# round-trip (it can barely reach slskd's 5 s minimum idle window), so we do not
# start a candidate with less budget than this — except the primary, which always
# runs floored to it so a misconfigured tiny budget cannot starve it.
_MIN_SEARCH_WINDOW = 5.0


class SearchService:
    def __init__(
        self,
        gateway: SoulseekGateway,
        store: ReleaseStore,
        clock: Clock,
        *,
        search_timeout: int = 15,
        poll_interval: float = 1.0,
        min_bitrate: int | None = None,
        release_ttl_days: int = 7,
        enough_results: int = 3,
        search_budget: int = 75,
    ) -> None:
        self._gateway = gateway
        self._store = store
        self._clock = clock
        self._search_timeout = search_timeout
        self._poll_interval = poll_interval
        self._min_bitrate = min_bitrate
        self._release_ttl_days = release_ttl_days
        self._enough_results = enough_results
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
            remaining = self._search_budget - elapsed
            if index == 0:
                # The primary always runs; a tight budget must not starve it.
                deadline = max(remaining, _MIN_SEARCH_WINDOW)
            else:
                if len(seen) >= self._enough_results:
                    break
                # Only start another candidate if enough budget remains to let a
                # search complete; a shorter wait just hammers Soulseek.
                if remaining < _MIN_SEARCH_WINDOW:
                    break
                deadline = remaining

            try:
                responses = self._run_search(text, deadline)
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

    def _run_search(self, text: str, deadline: float) -> list[SearchResponse]:
        # Forward a small, bounded idle window (``search_timeout``) to slskd, NOT
        # the budget: ``searchTimeout`` is an inactivity timer that resets on each
        # response, so a large value never completes on a busy query. slskd's
        # ``responseLimit`` (gateway) plus this idle window decide completion; we
        # poll for it up to the caller's wall-clock ``deadline``.
        sid = self._gateway.start_search(text, float(self._search_timeout))
        start = self._clock.now()
        while not self._gateway.search_is_complete(sid):
            elapsed = (self._clock.now() - start).total_seconds()
            if elapsed >= deadline:
                # slskd exposes /responses only once a search is complete, so an
                # abandoned search has nothing to read — return empty rather than
                # fetch a guaranteed-empty snapshot. Non-fatal: the loop moves on.
                logger.warning(
                    "Search %r did not complete within its %.0fs budget; "
                    "no results (slskd populates /responses only once complete)",
                    text,
                    deadline,
                )
                return []
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
