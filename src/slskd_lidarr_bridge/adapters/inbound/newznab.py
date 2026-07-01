"""Newznab indexer blueprint (Task 15).

Exposes:
  GET /indexer/api   – dispatch on t=caps|search|music
  GET /indexer/nzb/<release_id>  – download NZB for a stored release
"""

from __future__ import annotations

import hmac
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
    api_key: str | None = None,
) -> Blueprint:
    """Build and return the Newznab indexer Blueprint.

    Args:
        search_service: implements SearchService.search(SearchQuery) -> list[Release].
        release_store: implements ReleaseStore.get(release_id) -> Release | None.
        categories: list of (id, name) tuples forwarded to build_caps.
        api_key: optional shared key; when set, every request must supply a
            matching ``apikey`` query parameter. ``None`` disables auth.

    Returns:
        A Flask Blueprint registered at url_prefix="/indexer".
    """
    bp = Blueprint("newznab", __name__, url_prefix="/indexer")

    # Category advertised by the connection-test sentinel. The first configured
    # category is always one Lidarr knows about (it is in the caps), so the test
    # accepts it as "in the configured categories".
    sentinel_category = categories[0][0]

    @bp.before_request
    def check_api_key() -> Response | None:
        if api_key is None:
            return None
        provided = request.args.get("apikey")
        if not hmac.compare_digest((provided or "").encode(), api_key.encode()):
            return Response(
                build_error(100, "Incorrect API key"),
                status=200,
                content_type="application/xml",
            )
        return None

    @bp.route("/api")
    def api() -> Response:
        t = request.args.get("t", "")

        if t == "caps":
            return Response(build_caps(categories), content_type="application/xml")

        if t == "search":
            # Normalise empty string → None so is_empty works correctly.
            term = request.args.get("q") or None
            return _run_query(SearchQuery(term=term))

        if t == "music":
            artist = request.args.get("artist") or None
            album = request.args.get("album") or None
            term = request.args.get("q") or None
            return _run_query(SearchQuery(artist=artist, album=album, term=term))

        return Response(
            build_error(202, f"No such function: {t}"),
            content_type="application/xml",
        )

    def _run_query(query: SearchQuery) -> Response:
        """Answer a t=search / t=music request, honouring Newznab pagination.

        Lidarr paginates results in fixed pages of 100 (its
        ``NewznabRequestGenerator`` hardcodes ``PageSize=100`` / ``MaxPages=30``),
        advancing ``offset`` = 0, 100, 200, … and stopping a tier only once a page
        comes back with fewer than 100 items (``HttpIndexerBase.IsFullPage``). We
        cannot page through a live Soulseek swarm: each request re-runs a fresh
        slskd search and mints a new guid per release, so a naive follow-up page
        would re-trigger an *identical* Soulseek search and never look "short" to
        Lidarr — hammering slskd until a live search happens to yield <100 folders
        (observed as 4 identical searches for one album). We are therefore a
        single-page indexer: a follow-up page (``offset`` > 0) returns an empty
        feed *without* searching, so Lidarr stops after the first page.
        """
        if query.is_empty:
            return Response(_build_recent_feed(), content_type="application/xml")
        if (request.args.get("offset", type=int) or 0) > 0:
            return Response(_build_rss([]), content_type="application/xml")
        releases = search_service.search(query)
        logger.info(
            "Indexer search %r → %d releases",
            query.to_search_text(),
            len(releases),
        )
        return Response(_build_rss(releases), content_type="application/xml")

    def _nzb_url(release_id: str) -> str:
        """Build the NZB download URL, embedding the API key when configured."""
        key_kwargs: dict[str, str] = {"apikey": api_key} if api_key is not None else {}
        return url_for(
            "newznab.nzb", release_id=release_id, _external=True, **key_kwargs
        )

    def _build_recent_feed() -> bytes:
        """Single-item feed for empty/recent queries (Lidarr connection test).

        Returns a placeholder item instead of an empty channel so Lidarr's
        TestConnection accepts the indexer. See ``_SENTINEL_*`` for why this is
        safe even when RSS sync is enabled. The search backend is never called.
        """
        item: dict[str, Any] = {
            "title": _SENTINEL_TITLE,
            "guid": _SENTINEL_GUID,
            "link": _nzb_url(_SENTINEL_GUID),
            "pubDate": _SENTINEL_PUBDATE,
            "size": 1,
            "category": sentinel_category,
        }
        return build_results_rss([item])

    def _build_rss(releases: list[Release]) -> bytes:
        """Convert a list of Release objects to RSS bytes."""
        items: list[dict[str, Any]] = []
        for release in releases:
            # Releases returned by search_service are always stored first, so
            # their id is always set. The Optional only exists to support the
            # pre-storage construction pattern on the domain model.
            assert release.id is not None, "stored releases must have an id"
            category = _quality_to_category(release.quality)
            items.append(
                {
                    "title": release.title,
                    "guid": release.id,
                    "link": _nzb_url(release.id),
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
