"""Tests for SlskdGateway — respx mocks httpx transport."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from slskd_lidarr_bridge.adapters.outbound.slskd_gateway import (
    EnqueueError,
    SlskdGateway,
)
from slskd_lidarr_bridge.domain.models import AudioFile

BASE_URL = "http://slskd:5030"
API_KEY = "test-api-key"


def make_gateway() -> tuple[SlskdGateway, httpx.Client]:
    """Return a gateway backed by a plain client; respx will intercept."""
    client = httpx.Client(base_url=BASE_URL)
    gw = SlskdGateway(BASE_URL, API_KEY, client=client)
    return gw, client


def _gateway(**kwargs):
    return SlskdGateway(
        "http://slskd", "key", client=httpx.Client(base_url="http://slskd"), **kwargs
    )


# ---------------------------------------------------------------------------
# start_search
# ---------------------------------------------------------------------------


@respx.mock
def test_start_search_posts_correct_body_and_header():
    route = respx.post(f"{BASE_URL}/api/v0/searches").mock(
        return_value=httpx.Response(
            200, json={"id": "abc123", "searchText": "artist album"}
        )
    )
    gw, _ = make_gateway()
    result = gw.start_search("artist album", 30)

    assert result == "abc123"
    assert route.called
    request = route.calls.last.request
    import json

    body = json.loads(request.content)
    # The idle window is forwarded to slskd in MILLISECONDS (30s → 30000ms), and
    # responseLimit (gateway default 100) bounds gathering so a busy query — whose
    # idle timer keeps resetting — still completes promptly.
    assert body == {
        "searchText": "artist album",
        "searchTimeout": 30000,
        "responseLimit": 100,
    }
    assert request.headers["x-api-key"] == API_KEY


@respx.mock
def test_start_search_forwards_response_limit():
    """responseLimit from the gateway is sent on every search POST."""
    route = respx.post(f"{BASE_URL}/api/v0/searches").mock(
        return_value=httpx.Response(200, json={"id": "abc"})
    )
    client = httpx.Client(base_url=BASE_URL)
    gw = SlskdGateway(BASE_URL, API_KEY, client=client, response_limit=42)
    gw.start_search("q", 15)

    import json

    body = json.loads(route.calls.last.request.content)
    assert body["responseLimit"] == 42
    assert body["searchTimeout"] == 15000


@respx.mock
def test_start_search_omits_response_limit_when_non_positive():
    """response_limit <= 0 omits the field, falling back to slskd's own default."""
    route = respx.post(f"{BASE_URL}/api/v0/searches").mock(
        return_value=httpx.Response(200, json={"id": "abc"})
    )
    client = httpx.Client(base_url=BASE_URL)
    gw = SlskdGateway(BASE_URL, API_KEY, client=client, response_limit=0)
    gw.start_search("q", 15)

    import json

    body = json.loads(route.calls.last.request.content)
    assert "responseLimit" not in body


@respx.mock
def test_start_search_forwards_timeout_in_milliseconds():
    route = respx.post(f"{BASE_URL}/api/v0/searches").mock(
        return_value=httpx.Response(200, json={"id": "abc"})
    )
    gw, _ = make_gateway()
    gw.start_search("q", 45)

    import json

    body = json.loads(route.calls.last.request.content)
    assert body["searchTimeout"] == 45000


@respx.mock
def test_start_search_omits_timeout_when_non_positive():
    """A non-positive window omits searchTimeout (slskd's [Range(5,..)] would 400)."""
    route = respx.post(f"{BASE_URL}/api/v0/searches").mock(
        return_value=httpx.Response(200, json={"id": "abc"})
    )
    gw, _ = make_gateway()
    gw.start_search("q", 0)

    import json

    body = json.loads(route.calls.last.request.content)
    assert "searchTimeout" not in body


# ---------------------------------------------------------------------------
# search_is_complete
# ---------------------------------------------------------------------------


@respx.mock
def test_search_is_complete_returns_bool():
    search_id = "abc123"
    respx.get(f"{BASE_URL}/api/v0/searches/{search_id}").mock(
        return_value=httpx.Response(200, json={"id": search_id, "isComplete": True})
    )
    gw, _ = make_gateway()
    assert gw.search_is_complete(search_id) is True


@respx.mock
def test_search_is_complete_false():
    search_id = "xyz"
    respx.get(f"{BASE_URL}/api/v0/searches/{search_id}").mock(
        return_value=httpx.Response(200, json={"id": search_id, "isComplete": False})
    )
    gw, _ = make_gateway()
    assert gw.search_is_complete(search_id) is False


# ---------------------------------------------------------------------------
# search_responses
# ---------------------------------------------------------------------------

# Extension field is unreliable: peer2's file has extension="" / "MP3" (wrong),
# but the filename carries the ground truth. Also includes an extensionless name.
SEARCH_RESPONSES_PAYLOAD = [
    {
        "username": "peer1",
        "hasFreeUploadSlot": True,
        "uploadSpeed": 1024000,
        "queueLength": 0,
        "files": [
            {
                "filename": r"@@peer1\Music\Artist\Album\01 - Track.flac",
                "size": 30000000,
                "extension": "flac",  # field may be present but we derive from filename
                "bitRate": 1000,
                "length": 240,
            },
            {
                "filename": r"@@peer1\Music\Artist\Album\02 - Track.FLAC",
                "size": 25000000,
                "extension": ".FLAC",  # unreliable casing — we derive from filename
                "bitRate": 900,
                "length": 180,
            },
        ],
    },
    {
        "username": "peer2",
        "hasFreeUploadSlot": False,
        "uploadSpeed": 512000,
        "queueLength": 3,
        "files": [
            {
                # extension is "" (wrong), but filename ends .mp3 — must use filename
                "filename": r"@@peer2\Music\Artist\Album\01.mp3",
                "size": 8000000,
                "extension": "",
                "bitRate": 320,
                "length": 200,
            },
            {
                # extension field is "MP3" (wrong/unreliable), but filename ends .mp3
                "filename": r"@@peer2\Music\Artist\Album\02.mp3",
                "size": 7500000,
                "extension": "MP3",
                "bitRate": 256,
                "length": 195,
            },
            {
                # no dot in basename — extension must be None
                "filename": r"@@peer2\Music\Artist\Album.2020\readme",
                "size": 1024,
                "extension": "",
                "bitRate": None,
                "length": None,
            },
        ],
    },
]


@respx.mock
def test_search_responses_parses_responses():
    search_id = "sid1"
    respx.get(f"{BASE_URL}/api/v0/searches/{search_id}/responses").mock(
        return_value=httpx.Response(200, json=SEARCH_RESPONSES_PAYLOAD)
    )
    gw, _ = make_gateway()
    responses = gw.search_responses(search_id)

    assert len(responses) == 2

    r1 = responses[0]
    assert r1.username == "peer1"
    assert r1.has_free_upload_slot is True
    assert r1.upload_speed == 1024000
    assert r1.queue_length == 0
    assert len(r1.files) == 2

    f1 = r1.files[0]
    assert f1.filename == r"@@peer1\Music\Artist\Album\01 - Track.flac"
    assert f1.size == 30000000
    assert f1.extension == ".flac"  # derived from filename (lowercase, leading dot)
    assert f1.bitrate == 1000
    assert f1.length == 240

    f2 = r1.files[1]
    assert f2.extension == ".flac"  # filename ends .FLAC → lowercased to .flac

    r2 = responses[1]
    assert len(r2.files) == 3

    f3 = r2.files[0]
    # extension field is "" but filename is 01.mp3 → must derive ".mp3" from filename
    assert f3.extension == ".mp3"

    f4 = r2.files[1]
    # extension field is "MP3" (wrong/unreliable) but filename is 02.mp3 → ".mp3"
    assert f4.extension == ".mp3"

    f5 = r2.files[2]
    # basename is "readme" (no dot) → None, even though directory has "Album.2020"
    assert f5.extension is None


# ---------------------------------------------------------------------------
# enqueue
# ---------------------------------------------------------------------------


def test_enqueue_posts_a_batch_with_its_destination(respx_mock) -> None:
    route = respx_mock.post("http://slskd/api/v0/transfers/downloads/batches").mock(
        return_value=httpx.Response(201, json={"batch": {}, "failures": []})
    )
    gateway = _gateway()

    gateway.enqueue(
        "bob",
        [AudioFile(filename=r"@@a\Album\01.flac", size=5)],
        "Album [bob]/CD1",
    )

    assert route.called
    assert json.loads(route.calls[0].request.content) == {
        "username": "bob",
        "files": [{"filename": r"@@a\Album\01.flac", "size": 5}],
        "options": {"destination": "Album [bob]/CD1"},
    }


def test_enqueue_raises_when_slskd_enqueued_nothing(respx_mock) -> None:
    # 200 means every file failed, not success. A bare raise_for_status would
    # read it as a win.
    respx_mock.post("http://slskd/api/v0/transfers/downloads/batches").mock(
        return_value=httpx.Response(
            200,
            json={"batch": {}, "failures": [{"filename": "x.flac", "message": "nope"}]},
        )
    )

    with pytest.raises(EnqueueError, match="nope"):
        _gateway().enqueue("bob", [AudioFile(filename="x.flac", size=1)], "Album")


def test_enqueue_raises_on_a_partial_batch(respx_mock) -> None:
    respx_mock.post("http://slskd/api/v0/transfers/downloads/batches").mock(
        return_value=httpx.Response(
            207,
            json={"batch": {}, "failures": [{"filename": "b.flac", "message": "busy"}]},
        )
    )

    with pytest.raises(EnqueueError, match="busy"):
        _gateway().enqueue("bob", [AudioFile(filename="b.flac", size=1)], "Album")


def test_enqueue_raises_when_the_peer_is_offline(respx_mock) -> None:
    respx_mock.post("http://slskd/api/v0/transfers/downloads/batches").mock(
        return_value=httpx.Response(404, text="User bob appears to be offline")
    )

    with pytest.raises(httpx.HTTPStatusError):
        _gateway().enqueue("bob", [AudioFile(filename="x.flac", size=1)], "Album")


def test_enqueue_retries_when_slskd_throttles(respx_mock) -> None:
    slept: list[float] = []
    respx_mock.post("http://slskd/api/v0/transfers/downloads/batches").mock(
        side_effect=[
            httpx.Response(429, text="Only one concurrent operation is permitted"),
            httpx.Response(201, json={"batch": {}, "failures": []}),
        ]
    )
    gateway = _gateway(enqueue_retry_delays=(0.01, 0.02), sleep=slept.append)

    gateway.enqueue("bob", [AudioFile(filename="x.flac", size=1)], "Album")

    assert slept == [0.01]


def test_enqueue_gives_up_after_exhausting_its_retries(respx_mock) -> None:
    slept: list[float] = []
    respx_mock.post("http://slskd/api/v0/transfers/downloads/batches").mock(
        return_value=httpx.Response(429, text="throttled")
    )
    gateway = _gateway(enqueue_retry_delays=(0.01, 0.02), sleep=slept.append)

    with pytest.raises(httpx.HTTPStatusError):
        gateway.enqueue("bob", [AudioFile(filename="x.flac", size=1)], "Album")

    assert slept == [0.01, 0.02]


def test_version_reads_the_application_version(respx_mock) -> None:
    respx_mock.get("http://slskd/api/v0/application/version").mock(
        return_value=httpx.Response(200, json="0.26.0")
    )

    assert _gateway().version() == "0.26.0"


# ---------------------------------------------------------------------------
# transfers
# ---------------------------------------------------------------------------

# Real slskd shape: a single JSON OBJECT with "username" and "directories" keys.
TRANSFERS_PAYLOAD = {
    "username": "peer1",
    "directories": [
        {
            "directory": r"@@peer1\Music\Artist\Album",
            "fileCount": 2,
            "files": [
                {
                    "id": "transfer-id-1",
                    "username": "peer1",
                    "direction": "Download",
                    "filename": r"@@peer1\Music\Artist\Album\01.flac",
                    "size": 30000000,
                    "state": "Completed, Succeeded",
                    "bytesTransferred": 30000000,
                    "bytesRemaining": 0,
                    "percentComplete": 100.0,
                    "exception": None,
                },
                {
                    "id": "transfer-id-2",
                    "username": "peer1",
                    "direction": "Download",
                    "filename": r"@@peer1\Music\Artist\Album\02.flac",
                    "size": 25000000,
                    "state": "InProgress",
                    "bytesTransferred": 12500000,
                    "bytesRemaining": 12500000,
                    "percentComplete": 50.0,
                    "exception": None,
                },
            ],
        },
        {
            "directory": r"@@peer1\Music\Other",
            "fileCount": 1,
            "files": [
                {
                    "id": "transfer-id-3",
                    "username": "peer1",
                    "direction": "Download",
                    "filename": r"@@peer1\Music\Other\cover.jpg",
                    "size": 100000,
                    "state": "Completed, TimedOut",
                    "bytesTransferred": 0,
                    "bytesRemaining": 100000,
                    "percentComplete": 0.0,
                    "exception": "Connection timed out",
                }
            ],
        },
    ],
}


@respx.mock
def test_transfers_flattens_directories():
    username = "peer1"
    respx.get(f"{BASE_URL}/api/v0/transfers/downloads/{username}").mock(
        return_value=httpx.Response(200, json=TRANSFERS_PAYLOAD)
    )
    gw, _ = make_gateway()
    transfers = gw.transfers(username)

    assert len(transfers) == 3

    t1 = transfers[0]
    assert t1.id == "transfer-id-1"
    assert t1.username == username
    assert t1.filename == r"@@peer1\Music\Artist\Album\01.flac"
    assert t1.size == 30000000
    assert t1.state == "Completed, Succeeded"
    assert t1.bytes_transferred == 30000000
    assert t1.bytes_remaining == 0
    assert t1.percent_complete == 100.0
    assert t1.exception is None
    assert t1.is_succeeded is True

    t2 = transfers[1]
    assert t2.id == "transfer-id-2"
    assert t2.percent_complete == 50.0
    assert t2.is_complete is False

    t3 = transfers[2]
    assert t3.exception == "Connection timed out"
    assert t3.is_failed is True


@respx.mock
def test_transfers_returns_empty_list_on_404():
    """A 404 from slskd means no downloads for this user — return [], do not raise."""
    username = "peer-with-no-downloads"
    respx.get(f"{BASE_URL}/api/v0/transfers/downloads/{username}").mock(
        return_value=httpx.Response(404)
    )
    gw, _ = make_gateway()
    result = gw.transfers(username)
    assert result == []


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


@respx.mock
def test_cancel_issues_delete_with_remove_true():
    username = "peer1"
    transfer_id = "transfer-id-1"
    route = respx.delete(
        f"{BASE_URL}/api/v0/transfers/downloads/{username}/{transfer_id}",
    ).mock(return_value=httpx.Response(204))
    gw, _ = make_gateway()
    gw.cancel(username, transfer_id)

    assert route.called
    request = route.calls.last.request
    assert "remove=true" in str(request.url)
    assert request.headers["x-api-key"] == API_KEY


# ---------------------------------------------------------------------------
# Negative-path: HTTP errors propagate as HTTPStatusError
# ---------------------------------------------------------------------------


@respx.mock
def test_search_is_complete_raises_on_404():
    """A 404 from slskd must propagate as httpx.HTTPStatusError (not be swallowed)."""
    search_id = "no-such-search"
    respx.get(f"{BASE_URL}/api/v0/searches/{search_id}").mock(
        return_value=httpx.Response(404)
    )
    gw, _ = make_gateway()
    with pytest.raises(httpx.HTTPStatusError):
        gw.search_is_complete(search_id)


@respx.mock
def test_start_search_raises_on_500():
    """A 500 from slskd must propagate as httpx.HTTPStatusError."""
    respx.post(f"{BASE_URL}/api/v0/searches").mock(return_value=httpx.Response(500))
    gw, _ = make_gateway()
    with pytest.raises(httpx.HTTPStatusError):
        gw.start_search("some query", 30)


# ---------------------------------------------------------------------------
# downloads_directory
# ---------------------------------------------------------------------------


@respx.mock
def test_downloads_directory_reads_options_and_caches():
    """Reads directories.downloads from GET /options, then caches the result."""
    route = respx.get(f"{BASE_URL}/api/v0/options").mock(
        return_value=httpx.Response(
            200,
            json={
                "directories": {
                    "downloads": "/data/downloads/complete",
                    "incomplete": "/data/downloads/incomplete",
                },
            },
        )
    )
    gw, _ = make_gateway()

    assert gw.downloads_directory() == "/data/downloads/complete"
    # Cached — a second call must not hit slskd again.
    assert gw.downloads_directory() == "/data/downloads/complete"
    assert route.call_count == 1
    assert route.calls.last.request.headers["x-api-key"] == API_KEY


@respx.mock
def test_downloads_directory_raises_on_500():
    """A 500 from slskd must propagate as httpx.HTTPStatusError."""
    respx.get(f"{BASE_URL}/api/v0/options").mock(return_value=httpx.Response(500))
    gw, _ = make_gateway()
    with pytest.raises(httpx.HTTPStatusError):
        gw.downloads_directory()


# ---------------------------------------------------------------------------
# Path-segment encoding — security / robustness hardening
# ---------------------------------------------------------------------------


@respx.mock
def test_transfers_encodes_special_chars_in_username():
    """A username containing ``?`` and ``=`` must not inject query parameters.

    ``transfers("user?remove=true")`` must issue a single GET with the
    ``?`` encoded — the query string of the actual request must be empty.
    """
    malicious_username = "user?remove=true"
    encoded_username = "user%3Fremove%3Dtrue"
    expected_path = f"/api/v0/transfers/downloads/{encoded_username}"
    route = respx.get(f"{BASE_URL}{expected_path}").mock(
        return_value=httpx.Response(200, json={"directories": []})
    )
    gw, _ = make_gateway()
    result = gw.transfers(malicious_username)

    assert route.called, "transfers must GET the percent-encoded path"
    actual_url = route.calls.last.request.url
    # raw_path preserves percent-encoding; url.path decodes it (misleading but correct)
    assert actual_url.raw_path == expected_path.encode(), (
        "transfers must send the path with %3F intact, not injecting a query param"
    )
    assert actual_url.query == b"", "No query parameters must be injected"
    assert result == []


@respx.mock
def test_start_search_posts_to_static_searches_path():
    """Static path segments must still reach the correct endpoint after encoding.

    Encoding ``api``, ``v0``, ``searches`` (no reserved chars) must produce
    the identical path ``/api/v0/searches``.
    """
    route = respx.post(f"{BASE_URL}/api/v0/searches").mock(
        return_value=httpx.Response(200, json={"id": "enc-test-id"})
    )
    gw, _ = make_gateway()
    result = gw.start_search("test query", 30)

    assert route.called
    assert result == "enc-test-id"


@respx.mock
def test_search_is_complete_encodes_search_id():
    """A plain alphanumeric search_id must survive encoding unchanged."""
    search_id = "abc123"
    route = respx.get(f"{BASE_URL}/api/v0/searches/{search_id}").mock(
        return_value=httpx.Response(200, json={"isComplete": False})
    )
    gw, _ = make_gateway()
    gw.search_is_complete(search_id)

    assert route.called
    assert route.calls.last.request.url.path == f"/api/v0/searches/{search_id}"
