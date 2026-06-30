"""Domain use-case: search slskd for releases."""

from __future__ import annotations

import dataclasses
import datetime
from collections import defaultdict

from slskd_lidarr_bridge.domain.models import AudioFile, Release, SearchQuery
from slskd_lidarr_bridge.domain.ports import Clock, ReleaseStore, SoulseekGateway
from slskd_lidarr_bridge.domain.quality import detect_quality
from slskd_lidarr_bridge.domain.titles import build_title


class SearchService:
    def __init__(
        self,
        gateway: SoulseekGateway,
        store: ReleaseStore,
        clock: Clock,
        *,
        search_timeout: int = 30,
        poll_interval: float = 1.0,
        min_bitrate: int | None = None,
        release_ttl_days: int = 7,
    ) -> None:
        self._gateway = gateway
        self._store = store
        self._clock = clock
        self._search_timeout = search_timeout
        self._poll_interval = poll_interval
        self._min_bitrate = min_bitrate
        self._release_ttl_days = release_ttl_days

    def search(self, query: SearchQuery) -> list[Release]:
        if query.is_empty:
            return []

        self._store.purge_older_than(
            self._clock.now() - datetime.timedelta(days=self._release_ttl_days)
        )

        sid = self._gateway.start_search(query.to_search_text())

        # Poll until complete or timeout.
        start = self._clock.now()
        while not self._gateway.search_is_complete(sid):
            elapsed = (self._clock.now() - start).total_seconds()
            if elapsed >= self._search_timeout:
                break
            self._clock.sleep(self._poll_interval)

        responses = self._gateway.search_responses(sid)

        # (has_free_upload_slot, upload_speed, release) — used for sorting.
        tagged: list[tuple[bool, int, Release]] = []

        for response in responses:
            # Keep only audio files, filtered by min_bitrate when set.
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

            # Group filtered files by album folder.
            groups: dict[str, list[AudioFile]] = defaultdict(list)
            for f in audio:
                groups[f.album_folder].append(f)

            for folder, files in groups.items():
                # Derive artist / album.
                if (
                    query.term is not None
                    and query.artist is None
                    and query.album is None
                ):
                    # Term-only query: parse the folder name.
                    if " - " in folder:
                        left, right = folder.split(" - ", 1)
                        artist, album = left.strip(), right.strip()
                    else:
                        artist, album = "", folder
                else:
                    artist = query.artist or ""
                    album = query.album or ""

                size = sum(f.size for f in files)
                quality = detect_quality(files)
                title = build_title(artist, album, quality, response.username)
                created_at = self._clock.now()

                release = Release(
                    artist=artist,
                    album=album,
                    title=title,
                    username=response.username,
                    files=tuple(files),
                    size=size,
                    album_folder=folder,
                    quality=quality,
                    created_at=created_at,
                )
                release_id = self._store.put(release)
                release = dataclasses.replace(release, id=release_id)
                tagged.append(
                    (response.has_free_upload_slot, response.upload_speed, release)
                )

        # Order by (free slot desc, upload speed desc).
        tagged.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [r for _, _, r in tagged]
