"""slskd REST gateway adapter — implements SoulseekGateway over httpx."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from slskd_lidarr_bridge.domain.models import AudioFile, SearchResponse, Transfer

logger = logging.getLogger(__name__)


class EnqueueError(RuntimeError):
    """slskd accepted the batch request but did not enqueue every file.

    The batch endpoint answers 200 when *every* file failed and 207 when some
    did, so neither is an HTTP error and neither is a success.
    """


def _extension_from_filename(filename: str) -> str | None:
    """Derive a file extension from the full filename path.

    slskd's ``extension`` field is unreliable (no leading dot, arbitrary
    case, often empty/null).  Derive from the filename instead:

    * take the last path component (split on both ``\\`` and ``/``)
    * extract text after the last ``.`` in the basename, lowercase it,
      prepend a dot
    * return ``None`` if the basename contains no dot

    Examples::

        "\\\\A\\\\Album\\\\01 Track.FLAC"  →  ".flac"
        "\\\\A\\\\Album.2020\\\\readme"    →  None   (no dot in basename)
        "\\\\A\\\\Album\\\\readme"         →  None
    """
    basename = filename.replace("\\", "/").split("/")[-1]
    if "." not in basename:
        return None
    return "." + basename.rsplit(".", 1)[-1].lower()


class SlskdGateway:
    """SoulseekGateway implementation backed by the slskd REST API.

    Parameters
    ----------
    base_url:
        Root URL of the slskd instance, e.g. ``http://slskd:5030``.
    api_key:
        Value for the ``X-API-Key`` request header.
    client:
        Optional pre-built ``httpx.Client``; one is created if omitted.
        The ``X-API-Key`` header is always merged onto the client so that
        tests that inject a plain client still exercise the auth path.
    timeout:
        Per-request HTTP timeout in seconds (used only when the client is
        default-constructed here).
    response_limit:
        ``responseLimit`` sent on every search POST — slskd completes a search
        once this many peer responses have accumulated, independently of the idle
        ``searchTimeout``. This is what makes a *popular* query finish promptly
        (a busy swarm never goes idle, so the inactivity timer alone would keep it
        ``InProgress`` for minutes). ``<= 0`` omits the field, falling back to
        slskd's own default (250).
    enqueue_retry_delays:
        Delays, in seconds, between retries when slskd answers 429 to a batch
        enqueue. slskd guards enqueueing with a two-permit semaphore acquired
        without waiting, so a concurrent grab is throttled immediately rather
        than queued. One album now costs one request per disc, so retrying
        briefly is what keeps a multi-disc grab from failing on contention.
    sleep:
        Sleep function used between those retries; injectable for tests.

    The idle search window is chosen per call by the caller (``SearchService``
    forwards a small, bounded value) and passed to :meth:`start_search`; the poll
    budget is the caller's, not slskd's.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        response_limit: int = 100,
        enqueue_retry_delays: tuple[float, ...] = (0.5, 1.5),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if client is None:
            self._client = httpx.Client(
                base_url=base_url,
                headers={"X-API-Key": api_key},
                timeout=timeout,
            )
        else:
            # Merge the auth header onto an injected client (e.g. in tests).
            client.headers["X-API-Key"] = api_key
            self._client = client
        self._response_limit = response_limit
        self._downloads_directory: str | None = None
        self._enqueue_retry_delays = enqueue_retry_delays
        self._sleep = sleep

    # ------------------------------------------------------------------
    # Internal request helper
    # ------------------------------------------------------------------

    def _req(self, method: str, *segments: str, **kwargs: Any) -> httpx.Response:
        """Issue an HTTP request with every path segment percent-encoded.

        All callers pass path segments as positional strings; this method
        encodes each one with ``quote(s, safe="")`` so no reserved character
        (``/``, ``?``, ``#``, ``..``, …) can escape its segment boundary.
        ``**kwargs`` are forwarded verbatim to ``httpx.Client.request``
        (e.g. ``json=``, ``params=``).
        """
        path = "/".join(quote(s, safe="") for s in segments)
        return self._client.request(method, f"/{path}", **kwargs)

    # ------------------------------------------------------------------
    # SoulseekGateway implementation
    # ------------------------------------------------------------------

    def start_search(self, text: str, timeout_seconds: float) -> str:
        """POST /api/v0/searches → return the new search id.

        ``timeout_seconds`` is slskd's *idle* window (``searchTimeout``): slskd
        completes the search after this many seconds elapse with **no new
        response** (the timer resets on every response), so it must be small and
        bounded — never the whole poll budget. It is sent in milliseconds. A
        non-positive value omits the field, falling back to slskd's own default
        (15 s); slskd rejects positive values below its 5 s minimum.

        ``responseLimit`` (from the gateway's ``response_limit``) is also sent so a
        busy query — whose idle timer would otherwise keep resetting — completes
        once enough peers have responded.
        """
        body: dict[str, object] = {"searchText": text}
        if timeout_seconds > 0:
            body["searchTimeout"] = int(timeout_seconds * 1000)
        if self._response_limit > 0:
            body["responseLimit"] = self._response_limit
        r = self._req("POST", "api", "v0", "searches", json=body)
        r.raise_for_status()
        search_id: str = r.json()["id"]
        return search_id

    def search_is_complete(self, search_id: str) -> bool:
        """GET /api/v0/searches/{id} → read isComplete."""
        r = self._req("GET", "api", "v0", "searches", search_id)
        r.raise_for_status()
        return bool(r.json()["isComplete"])

    def search_responses(self, search_id: str) -> list[SearchResponse]:
        """GET /api/v0/searches/{id}/responses → list[SearchResponse]."""
        r = self._req("GET", "api", "v0", "searches", search_id, "responses")
        r.raise_for_status()
        results: list[SearchResponse] = []
        for item in r.json():
            files = tuple(
                AudioFile(
                    filename=f["filename"],
                    size=f["size"],
                    extension=_extension_from_filename(f["filename"]),
                    bitrate=f.get("bitRate"),
                    length=f.get("length"),
                )
                for f in item.get("files", [])
            )
            results.append(
                SearchResponse(
                    username=item["username"],
                    has_free_upload_slot=bool(item.get("hasFreeUploadSlot", False)),
                    upload_speed=item.get("uploadSpeed", 0),
                    queue_length=item.get("queueLength", 0),
                    files=files,
                )
            )
        return results

    def enqueue(
        self,
        username: str,
        files: list[AudioFile],
        destination: str,
    ) -> None:
        """POST a download batch, pinning its destination folder.

        ``destination`` is a path relative to slskd's downloads directory. slskd
        gives it precedence over the user's ``transfers.download.destination``
        pattern, which is what lets the bridge decide where an album lands and
        report that exact path to Lidarr.

        The batch endpoint's status codes are not conventional: 201 means every
        file was enqueued, 207 that some failed, and **200 that all of them
        did**. Only 201 is a success.
        """
        body: dict[str, object] = {
            "username": username,
            "files": [{"filename": f.filename, "size": f.size} for f in files],
            "options": {"destination": destination},
        }

        response = self._post_batch(body)

        if response.status_code in (200, 207):
            failures = response.json().get("failures", [])
            detail = "; ".join(
                f"{f.get('filename')}: {f.get('message')}" for f in failures
            )
            raise EnqueueError(
                f"slskd enqueued {len(files) - len(failures)} of {len(files)} "
                f"files for {username!r}: {detail}"
            )
        response.raise_for_status()

    def _post_batch(self, body: dict[str, object]) -> httpx.Response:
        """POST the batch, retrying while slskd's enqueue limiter answers 429."""
        response = self._req(
            "POST", "api", "v0", "transfers", "downloads", "batches", json=body
        )
        for delay in self._enqueue_retry_delays:
            if response.status_code != 429:
                return response
            logger.warning(
                "slskd throttled the enqueue (429); retrying in %.2fs", delay
            )
            self._sleep(delay)
            response = self._req(
                "POST", "api", "v0", "transfers", "downloads", "batches", json=body
            )
        return response

    def transfers(self, username: str) -> list[Transfer]:
        """GET transfers for a user, flattening directories→files to list[Transfer].

        slskd groups transfers by directory; we flatten all files.
        """
        r = self._req("GET", "api", "v0", "transfers", "downloads", username)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        result: list[Transfer] = []
        for directory in r.json().get("directories", []):
            for f in directory.get("files", []):
                result.append(
                    Transfer(
                        username=username,
                        id=str(f["id"]),
                        filename=f["filename"],
                        size=f["size"],
                        state=f.get("state", ""),
                        bytes_transferred=f.get("bytesTransferred", 0),
                        bytes_remaining=f.get("bytesRemaining", 0),
                        percent_complete=float(f.get("percentComplete", 0.0)),
                        exception=f.get("exception") or None,
                    )
                )
        return result

    def cancel(self, username: str, transfer_id: str) -> None:
        """DELETE /api/v0/transfers/downloads/{username}/{id}?remove=true."""
        r = self._req(
            "DELETE",
            "api",
            "v0",
            "transfers",
            "downloads",
            username,
            transfer_id,
            params={"remove": "true"},
        )
        r.raise_for_status()

    def downloads_directory(self) -> str:
        """Return slskd's completed-downloads dir (its ``directories.downloads``).

        Read from ``GET /api/v0/options`` — slskd is the source of truth for
        where completed files land, so the bridge needs no path config of its
        own. Cached after the first successful fetch: slskd requires a restart
        to change this value, so it is stable for the process lifetime.
        """
        if self._downloads_directory is None:
            r = self._req("GET", "api", "v0", "options")
            r.raise_for_status()
            self._downloads_directory = str(r.json()["directories"]["downloads"])
        return self._downloads_directory

    def version(self) -> str:
        """Return slskd's semantic version (``GET /api/v0/application/version``).

        Deliberately not part of the ``SoulseekGateway`` port: only the
        composition root reads it, to refuse to start against a slskd too old
        for batch destinations.
        """
        r = self._req("GET", "api", "v0", "application", "version")
        r.raise_for_status()
        return str(r.json())
