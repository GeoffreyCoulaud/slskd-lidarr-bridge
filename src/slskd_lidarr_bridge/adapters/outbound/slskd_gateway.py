"""slskd REST gateway adapter — implements SoulseekGateway over httpx."""

from __future__ import annotations

import httpx

from slskd_lidarr_bridge.domain.models import AudioFile, SearchResponse, Transfer


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
        Per-request timeout in seconds (used only when the client is
        default-constructed here).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
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
        self._downloads_directory: str | None = None

    # ------------------------------------------------------------------
    # SoulseekGateway implementation
    # ------------------------------------------------------------------

    def start_search(self, text: str) -> str:
        """POST /api/v0/searches → return the new search id."""
        r = self._client.post("/api/v0/searches", json={"searchText": text})
        r.raise_for_status()
        search_id: str = r.json()["id"]
        return search_id

    def search_is_complete(self, search_id: str) -> bool:
        """GET /api/v0/searches/{id} → read isComplete."""
        r = self._client.get(f"/api/v0/searches/{search_id}")
        r.raise_for_status()
        return bool(r.json()["isComplete"])

    def search_responses(self, search_id: str) -> list[SearchResponse]:
        """GET /api/v0/searches/{id}/responses → list[SearchResponse]."""
        r = self._client.get(f"/api/v0/searches/{search_id}/responses")
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

    def enqueue(self, username: str, files: list[AudioFile]) -> None:
        """POST /api/v0/transfers/downloads/{username} with [{filename, size}, ...]."""
        payload = [{"filename": f.filename, "size": f.size} for f in files]
        r = self._client.post(f"/api/v0/transfers/downloads/{username}", json=payload)
        r.raise_for_status()

    def transfers(self, username: str) -> list[Transfer]:
        """GET transfers for a user, flattening directories→files to list[Transfer].

        slskd groups transfers by directory; we flatten all files.
        Note: slskd does not expose a local on-disk path in this payload,
        so local_path is always None.
        """
        r = self._client.get(f"/api/v0/transfers/downloads/{username}")
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
                        local_path=None,  # slskd does not expose an on-disk path here
                    )
                )
        return result

    def cancel(self, username: str, transfer_id: str) -> None:
        """DELETE /api/v0/transfers/downloads/{username}/{id}?remove=true."""
        r = self._client.delete(
            f"/api/v0/transfers/downloads/{username}/{transfer_id}",
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
            r = self._client.get("/api/v0/options")
            r.raise_for_status()
            self._downloads_directory = str(r.json()["directories"]["downloads"])
        return self._downloads_directory
