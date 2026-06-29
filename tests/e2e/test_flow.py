"""End-to-end lifecycle test: Lidarr → bridge → (fake) slskd.

Drives the full flow:
  1. caps         – indexer advertises audio-search
  2. music search – RSS feed with ≥1 item; capture nzb url
  3. GET nzb url  – application/x-nzb; parse_nzb round-trips
  4. addfile       – POST multipart; capture nzo_id
  5. in-progress   – queue shows slot; history empty
  6. completed     – history shows slot with storage; queue empty
  7. delete        – history entry removed
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import pytest

from slskd_lidarr_bridge.adapters.inbound.app import create_app
from slskd_lidarr_bridge.adapters.inbound.nzb import parse_nzb
from slskd_lidarr_bridge.adapters.outbound.sqlite_store import open_stores
from slskd_lidarr_bridge.config import Config
from slskd_lidarr_bridge.domain.models import AudioFile, SearchResponse, Transfer
from slskd_lidarr_bridge.domain.paths import compute_storage_path

# ---------------------------------------------------------------------------
# Fake SoulseekGateway
# ---------------------------------------------------------------------------

_ALICE = "alice"
_FILES = [
    AudioFile(
        filename=r"@@alice\Music\Radiohead\In Rainbows\01 - 15 Step.flac",
        size=30_000_000,
        extension=".flac",
        bitrate=None,
        length=237,
    ),
    AudioFile(
        filename=r"@@alice\Music\Radiohead\In Rainbows\02 - Bodysnatchers.flac",
        size=25_000_000,
        extension=".flac",
        bitrate=None,
        length=242,
    ),
    AudioFile(
        filename=r"@@alice\Music\Radiohead\In Rainbows\03 - Nude.flac",
        size=28_000_000,
        extension=".flac",
        bitrate=None,
        length=255,
    ),
]


class FakeGateway:
    """Stateful fake SoulseekGateway for e2e tests."""

    def __init__(self) -> None:
        self._enqueued: list[tuple[str, list[AudioFile]]] = []
        self._cancelled: list[tuple[str, str]] = []
        # Start in-progress; flip to True to simulate completion.
        self.transfers_complete: bool = False

    def start_search(self, text: str) -> str:
        return "fake-search-id-001"

    def search_is_complete(self, search_id: str) -> bool:
        return True  # Always done immediately — no sleep needed.

    def search_responses(self, search_id: str) -> list[SearchResponse]:
        return [
            SearchResponse(
                username=_ALICE,
                has_free_upload_slot=True,
                upload_speed=10_000_000,
                queue_length=0,
                files=tuple(_FILES),
            )
        ]

    def enqueue(self, username: str, files: list[AudioFile]) -> None:
        self._enqueued.append((username, files))

    def transfers(self, username: str) -> list[Transfer]:
        if username != _ALICE:
            return []
        if self.transfers_complete:
            return [
                Transfer(
                    username=_ALICE,
                    id=f"xfer-{i}",
                    filename=f.filename,
                    size=f.size,
                    state="Completed, Succeeded",
                    bytes_transferred=f.size,
                    bytes_remaining=0,
                    percent_complete=100.0,
                    local_path=None,
                )
                for i, f in enumerate(_FILES)
            ]
        else:
            return [
                Transfer(
                    username=_ALICE,
                    id=f"xfer-{i}",
                    filename=f.filename,
                    size=f.size,
                    state="InProgress",
                    bytes_transferred=f.size // 2,
                    bytes_remaining=f.size // 2,
                    percent_complete=50.0,
                )
                for i, f in enumerate(_FILES)
            ]

    def cancel(self, username: str, transfer_id: str) -> None:
        self._cancelled.append((username, transfer_id))


# ---------------------------------------------------------------------------
# Fake Clock (no-op sleep so the search loop exits instantly)
# ---------------------------------------------------------------------------


class FakeClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        pass  # no-op


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def downloads_dir(tmp_path):
    d = tmp_path / "downloads"
    d.mkdir()
    return str(d)


@pytest.fixture()
def gateway():
    return FakeGateway()


@pytest.fixture()
def client(tmp_path, downloads_dir, gateway):
    db_path = str(tmp_path / "bridge.db")
    release_store, job_store = open_stores(db_path)
    config = Config.from_env(
        {
            "SLSKD_URL": "http://slskd:5030",
            "SLSKD_API_KEY": "test-slskd-key",
            "SLSKD_DOWNLOADS_DIR": downloads_dir,
        }
    )
    app = create_app(config, gateway, release_store, job_store, FakeClock())
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# The end-to-end test
# ---------------------------------------------------------------------------


def test_full_lidarr_lifecycle(client, gateway, downloads_dir):
    # ------------------------------------------------------------------
    # Step 1: caps advertises audio-search
    # ------------------------------------------------------------------
    resp = client.get("/indexer/api?t=caps")
    assert resp.status_code == 200
    assert resp.content_type.startswith("application/xml")
    caps_root = ET.fromstring(resp.data)
    searching = caps_root.find("searching")
    assert searching is not None, "Missing <searching> element in caps"
    audio_search = searching.find("audio-search")
    assert audio_search is not None, "Missing <audio-search> element"
    assert audio_search.get("available") == "yes"

    # ------------------------------------------------------------------
    # Step 2: music search returns RSS with ≥1 item; capture nzb url + guid
    # ------------------------------------------------------------------
    resp = client.get("/indexer/api?t=music&artist=Radiohead&album=In+Rainbows")
    assert resp.status_code == 200
    rss_root = ET.fromstring(resp.data)
    channel = rss_root.find("channel")
    assert channel is not None
    items = channel.findall("item")
    assert len(items) >= 1, "Expected at least one RSS item"

    first_item = items[0]
    enclosure = first_item.find("enclosure")
    assert enclosure is not None
    nzb_url = enclosure.get("url")
    assert nzb_url and "/indexer/nzb/" in nzb_url

    guid_el = first_item.find("guid")
    assert guid_el is not None
    guid = guid_el.text
    assert guid  # non-empty

    # ------------------------------------------------------------------
    # Step 3: GET nzb url → application/x-nzb; parse_nzb round-trips
    # ------------------------------------------------------------------
    # The URL is absolute (http://localhost/indexer/nzb/<id>); extract the path.
    from urllib.parse import urlparse

    nzb_path = urlparse(nzb_url).path
    resp = client.get(nzb_path)
    assert resp.status_code == 200
    assert resp.content_type.startswith("application/x-nzb")

    nzb_bytes = resp.data
    payload = parse_nzb(nzb_bytes)
    assert payload["username"] == _ALICE
    assert len(payload["files"]) == len(_FILES)

    # ------------------------------------------------------------------
    # Step 4: POST addfile multipart → capture nzo_id
    # ------------------------------------------------------------------
    resp = client.post(
        "/sabnzbd/api?mode=addfile",
        data={"cat": "music", "name": (io.BytesIO(nzb_bytes), "release.nzb")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] is True
    assert data["nzo_ids"]
    nzo_id = data["nzo_ids"][0]
    assert nzo_id.startswith("SABnzbd_nzo_")

    # ------------------------------------------------------------------
    # Step 5: In-progress state — queue shows slot; history empty
    # ------------------------------------------------------------------
    gateway.transfers_complete = False

    resp = client.get("/sabnzbd/api?mode=queue")
    assert resp.status_code == 200
    q_data = resp.get_json()
    queue_slots = q_data["queue"]["slots"]
    assert any(s["nzo_id"] == nzo_id for s in queue_slots), (
        f"nzo_id {nzo_id!r} not found in queue slots: {queue_slots}"
    )

    resp = client.get("/sabnzbd/api?mode=history")
    assert resp.status_code == 200
    h_data = resp.get_json()
    history_slots = h_data["history"]["slots"]
    assert not any(s["nzo_id"] == nzo_id for s in history_slots), (
        "nzo_id should NOT appear in history while still in-progress"
    )

    # ------------------------------------------------------------------
    # Step 6: Completed — history shows slot; queue empty for this id
    # ------------------------------------------------------------------
    gateway.transfers_complete = True

    resp = client.get("/sabnzbd/api?mode=history")
    assert resp.status_code == 200
    h_data = resp.get_json()
    history_slots = h_data["history"]["slots"]
    completed_slot = next((s for s in history_slots if s["nzo_id"] == nzo_id), None)
    assert completed_slot is not None, (
        f"nzo_id {nzo_id!r} not found in history after completion: {history_slots}"
    )
    assert completed_slot["status"] == "Completed"
    assert completed_slot["storage"], "storage path must be non-empty when completed"
    expected_storage = compute_storage_path(downloads_dir, _FILES[0].filename)
    assert completed_slot["storage"] == expected_storage, (
        f"storage {completed_slot['storage']!r} != expected {expected_storage!r}"
    )

    resp = client.get("/sabnzbd/api?mode=queue")
    assert resp.status_code == 200
    q_data = resp.get_json()
    queue_slots = q_data["queue"]["slots"]
    assert not any(s["nzo_id"] == nzo_id for s in queue_slots), (
        "nzo_id should NOT appear in queue after completion"
    )

    # ------------------------------------------------------------------
    # Step 7: delete → history empty; job removed
    # ------------------------------------------------------------------
    resp = client.get(f"/sabnzbd/api?mode=history&name=delete&value={nzo_id}")
    assert resp.status_code == 200
    del_data = resp.get_json()
    assert del_data["status"] is True

    resp = client.get("/sabnzbd/api?mode=history")
    assert resp.status_code == 200
    h_data = resp.get_json()
    remaining = h_data["history"]["slots"]
    assert not any(s["nzo_id"] == nzo_id for s in remaining), (
        "Deleted job should no longer appear in history"
    )
