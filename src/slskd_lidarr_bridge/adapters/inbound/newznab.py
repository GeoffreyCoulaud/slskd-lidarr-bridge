"""Newznab indexer blueprint (Task 15).

Exposes:
  GET /indexer/api   – dispatch on t=caps|search|music
  GET /indexer/nzb/<release_id>  – download NZB for a stored release
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from flask import Blueprint, Response, abort, request, url_for

from slskd_lidarr_bridge.adapters.inbound.nzb import build_nzb
from slskd_lidarr_bridge.adapters.inbound.xml import (
    build_caps,
    build_error,
    build_results_rss,
)
from slskd_lidarr_bridge.domain.models import Release, SearchQuery
from slskd_lidarr_bridge.domain.ports import ReleaseStore
from slskd_lidarr_bridge.domain.search_service import SearchService

logger = logging.getLogger(__name__)

# Newznab category IDs for audio quality tiers
_LOSSLESS_QUALITY_PREFIXES = ("FLAC", "ALAC", "WAV")
_MP3_QUALITY_PREFIXES = ("MP3",)

# Sentinel item returned for empty/recent feed requests.
#
# When an indexer is added/tested, Lidarr runs TestConnection(), which fetches
# the *recent* feed — a query with no search terms — and rejects the indexer
# unless it returns at least one result in a configured category ("Query
# successful, but no results in the configured categories were returned from
# your indexer"). This happens regardless of the RSS toggle. slskd has no notion
# of "recent uploads", so we answer the empty query with a single placeholder.
#
# The title is deliberately unparseable as an artist/album so that, even if RSS
# sync is enabled, Lidarr's parser cannot match it to a monitored album and will
# never attempt to grab it. The pubDate is intentionally old for the same reason.
_SENTINEL_TITLE = "slskd-bridge indexer online (connection-test placeholder)"
_SENTINEL_GUID = "slskd-bridge-sentinel"
_SENTINEL_PUBDATE = datetime(2020, 1, 1, tzinfo=UTC)


def _quality_to_category(quality: str) -> int:
    """Map a quality string to a Newznab category id."""
    q = quality.upper()
    if any(q.startswith(p) for p in _LOSSLESS_QUALITY_PREFIXES):
        return 3040
    if any(q.startswith(p) for p in _MP3_QUALITY_PREFIXES):
        return 3010
    return 3000


def create_newznab_blueprint(
    search_service: SearchService,
    release_store: ReleaseStore,
    *,
    categories: list[tuple[int, str]],
) -> Blueprint:
    """Build and return the Newznab indexer Blueprint.

    Args:
        search_service: implements SearchService.search(SearchQuery) -> list[Release].
        release_store: implements ReleaseStore.get(release_id) -> Release | None.
        categories: list of (id, name) tuples forwarded to build_caps.

    Returns:
        A Flask Blueprint registered at url_prefix="/indexer".
    """
    bp = Blueprint("newznab", __name__, url_prefix="/indexer")

    # Category advertised by the connection-test sentinel. The first configured
    # category is always one Lidarr knows about (it is in the caps), so the test
    # accepts it as "in the configured categories".
    sentinel_category = categories[0][0]

    @bp.route("/api")
    def api() -> Response:
        t = request.args.get("t", "")

        if t == "caps":
            return Response(build_caps(categories), content_type="application/xml")

        if t == "search":
            # Normalise empty string → None so is_empty works correctly.
            term = request.args.get("q") or None
            query = SearchQuery(term=term)
            if query.is_empty:
                return Response(_build_recent_feed(), content_type="application/xml")
            releases = search_service.search(query)
            logger.info(
                "Indexer search %r → %d releases",
                query.to_search_text(),
                len(releases),
            )
            return Response(_build_rss(releases), content_type="application/xml")

        if t == "music":
            artist = request.args.get("artist") or None
            album = request.args.get("album") or None
            term = request.args.get("q") or None
            query = SearchQuery(artist=artist, album=album, term=term)
            if query.is_empty:
                return Response(_build_recent_feed(), content_type="application/xml")
            releases = search_service.search(query)
            logger.info(
                "Indexer search %r → %d releases",
                query.to_search_text(),
                len(releases),
            )
            return Response(_build_rss(releases), content_type="application/xml")

        return Response(
            build_error(202, f"No such function: {t}"),
            content_type="application/xml",
        )

    def _build_recent_feed() -> bytes:
        """Single-item feed for empty/recent queries (Lidarr connection test).

        Returns a placeholder item instead of an empty channel so Lidarr's
        TestConnection accepts the indexer. See ``_SENTINEL_*`` for why this is
        safe even when RSS sync is enabled. The search backend is never called.
        """
        nzb_url = url_for("newznab.nzb", release_id=_SENTINEL_GUID, _external=True)
        item: dict[str, Any] = {
            "title": _SENTINEL_TITLE,
            "guid": _SENTINEL_GUID,
            "link": nzb_url,
            "pubDate": _SENTINEL_PUBDATE,
            "size": 1,
            "category": sentinel_category,
        }
        return build_results_rss([item])

    def _build_rss(releases: list[Release]) -> bytes:
        """Convert a list of Release objects to RSS bytes."""
        items: list[dict[str, Any]] = []
        for release in releases:
            nzb_url = url_for("newznab.nzb", release_id=release.id, _external=True)
            category = _quality_to_category(release.quality)
            items.append(
                {
                    "title": release.title,
                    "guid": release.id,
                    "link": nzb_url,
                    "pubDate": release.created_at,
                    "size": release.size,
                    "category": category,
                }
            )
        return build_results_rss(items)

    @bp.route("/nzb/<release_id>")
    def nzb(release_id: str) -> Response:
        release = release_store.get(release_id)
        if release is None:
            abort(404)

        payload = {
            "username": release.username,
            "title": release.title,
            "album_folder": release.album_folder,
            "total_size": release.size,
            "files": [{"filename": f.filename, "size": f.size} for f in release.files],
        }
        nzb_bytes = build_nzb(payload)
        return Response(
            nzb_bytes,
            status=200,
            content_type="application/x-nzb",
            headers={
                "Content-Disposition": f'attachment; filename="{release_id}.nzb"',
            },
        )

    return bp
