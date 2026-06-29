"""Tests for DownloadService (Task 8)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from slskd_lidarr_bridge.domain.download_service import DownloadService
from slskd_lidarr_bridge.domain.models import (
    AudioFile,
    DownloadJob,
    SearchResponse,
    Transfer,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeGateway:
    def __init__(
        self,
        transfers_by_username: dict[str, list[Transfer]] | None = None,
    ) -> None:
        self._transfers = transfers_by_username or {}
        self.enqueued: list[tuple[str, list[AudioFile]]] = []
        self.cancelled: list[tuple[str, str]] = []

    # Unused search methods — satisfy Protocol
    def start_search(self, text: str) -> str:
        return ""

    def search_is_complete(self, search_id: str) -> bool:
        return True

    def search_responses(self, search_id: str) -> list[SearchResponse]:
        return []

    def enqueue(self, username: str, files: list[AudioFile]) -> None:
        self.enqueued.append((username, list(files)))

    def transfers(self, username: str) -> list[Transfer]:
        return list(self._transfers.get(username, []))

    def cancel(self, username: str, transfer_id: str) -> None:
        self.cancelled.append((username, transfer_id))


class FakeJobStore:
    def __init__(self) -> None:
        self._store: dict[str, DownloadJob] = {}

    def add(self, job: DownloadJob) -> None:
        self._store[job.nzo_id] = job

    def get(self, nzo_id: str) -> DownloadJob | None:
        return self._store.get(nzo_id)

    def list(self) -> list[DownloadJob]:
        return list(self._store.values())

    def remove(self, nzo_id: str) -> None:
        self._store.pop(nzo_id, None)


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2024, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_PAYLOAD: dict = {
    "username": "alice",
    "title": "Artist - Album [FLAC]",
    "album_folder": "Album",
    "total_size": 20_000_000,
    "files": [
        {"filename": r"@@a\Artist\Album\01.flac", "size": 10_000_000},
        {"filename": r"@@a\Artist\Album\02.flac", "size": 10_000_000},
    ],
}


def make_transfer(
    filename: str,
    *,
    username: str = "alice",
    transfer_id: str = "t1",
    state: str = "InProgress",
    bytes_transferred: int = 0,
    size: int = 10_000_000,
    exception: str | None = None,
    local_path: str | None = None,
) -> Transfer:
    return Transfer(
        username=username,
        id=transfer_id,
        filename=filename,
        size=size,
        state=state,
        bytes_transferred=bytes_transferred,
        bytes_remaining=size - bytes_transferred,
        percent_complete=bytes_transferred / size * 100 if size else 0.0,
        exception=exception,
        local_path=local_path,
    )


# ---------------------------------------------------------------------------
# Tests — start
# ---------------------------------------------------------------------------


def test_start_returns_nzo_id_with_correct_prefix():
    service = DownloadService(
        FakeGateway(), FakeJobStore(), FakeClock(), downloads_dir="/downloads"
    )
    nzo_id = service.start(SAMPLE_PAYLOAD, "music")
    assert nzo_id.startswith("SABnzbd_nzo_")


def test_start_enqueues_files_on_gateway():
    gateway = FakeGateway()
    service = DownloadService(
        gateway, FakeJobStore(), FakeClock(), downloads_dir="/downloads"
    )

    service.start(SAMPLE_PAYLOAD, "music")

    assert len(gateway.enqueued) == 1
    username, files = gateway.enqueued[0]
    assert username == "alice"
    assert len(files) == 2
    assert all(isinstance(f, AudioFile) for f in files)
    assert files[0].filename == r"@@a\Artist\Album\01.flac"
    assert files[1].filename == r"@@a\Artist\Album\02.flac"


def test_start_persists_download_job():
    jobs = FakeJobStore()
    service = DownloadService(
        FakeGateway(), jobs, FakeClock(), downloads_dir="/downloads"
    )

    nzo_id = service.start(SAMPLE_PAYLOAD, "music")

    job = jobs.get(nzo_id)
    assert job is not None
    assert job.nzo_id == nzo_id
    assert job.username == "alice"
    assert job.title == "Artist - Album [FLAC]"
    assert job.category == "music"
    assert job.total_size == 20_000_000
    assert job.album_folder == "Album"
    assert len(job.files) == 2


# ---------------------------------------------------------------------------
# Tests — statuses: downloading
# ---------------------------------------------------------------------------


def test_statuses_no_transfers_yet_reports_job_total_size():
    """No transfers from gateway → total_bytes == job.total_size and percent == 0."""
    gateway = FakeGateway(transfers_by_username={})
    service = DownloadService(
        gateway, FakeJobStore(), FakeClock(), downloads_dir="/downloads"
    )
    service.start(SAMPLE_PAYLOAD, "music")

    views = service.statuses()

    assert len(views) == 1
    v = views[0]
    assert v.state == "downloading"
    assert v.total_bytes == 20_000_000
    assert v.transferred_bytes == 0
    assert v.percent == pytest.approx(0.0)
    assert v.storage is None


def test_statuses_downloading_half_done():
    gateway = FakeGateway(
        transfers_by_username={
            "alice": [
                make_transfer(
                    r"@@a\Artist\Album\01.flac",
                    transfer_id="t1",
                    bytes_transferred=5_000_000,
                ),
                make_transfer(
                    r"@@a\Artist\Album\02.flac",
                    transfer_id="t2",
                    bytes_transferred=5_000_000,
                ),
            ]
        }
    )
    service = DownloadService(
        gateway, FakeJobStore(), FakeClock(), downloads_dir="/downloads"
    )
    service.start(SAMPLE_PAYLOAD, "music")

    views = service.statuses()

    assert len(views) == 1
    v = views[0]
    assert v.state == "downloading"
    assert v.storage is None
    assert v.fail_message is None
    assert v.percent == pytest.approx(50.0)
    assert v.transferred_bytes == 10_000_000
    assert v.total_bytes == 20_000_000


# ---------------------------------------------------------------------------
# Tests — statuses: completed
# ---------------------------------------------------------------------------


def test_statuses_completed_uses_compute_storage_path():
    gateway = FakeGateway(
        transfers_by_username={
            "alice": [
                make_transfer(
                    r"@@a\Artist\Album\01.flac",
                    transfer_id="t1",
                    state="Completed, Succeeded",
                    bytes_transferred=10_000_000,
                ),
                make_transfer(
                    r"@@a\Artist\Album\02.flac",
                    transfer_id="t2",
                    state="Completed, Succeeded",
                    bytes_transferred=10_000_000,
                ),
            ]
        }
    )
    service = DownloadService(
        gateway, FakeJobStore(), FakeClock(), downloads_dir="/downloads"
    )
    service.start(SAMPLE_PAYLOAD, "music")

    views = service.statuses()

    assert len(views) == 1
    v = views[0]
    assert v.state == "completed"
    assert v.storage == "/downloads/Album"
    assert v.fail_message is None


def test_statuses_completed_uses_local_path_parent_when_set():
    gateway = FakeGateway(
        transfers_by_username={
            "alice": [
                make_transfer(
                    r"@@a\Artist\Album\01.flac",
                    transfer_id="t1",
                    state="Completed, Succeeded",
                    bytes_transferred=10_000_000,
                    local_path="/actual/path/to/Album/01.flac",
                ),
                make_transfer(
                    r"@@a\Artist\Album\02.flac",
                    transfer_id="t2",
                    state="Completed, Succeeded",
                    bytes_transferred=10_000_000,
                    local_path="/actual/path/to/Album/02.flac",
                ),
            ]
        }
    )
    service = DownloadService(
        gateway, FakeJobStore(), FakeClock(), downloads_dir="/downloads"
    )
    service.start(SAMPLE_PAYLOAD, "music")

    views = service.statuses()

    assert len(views) == 1
    assert views[0].state == "completed"
    assert views[0].storage == "/actual/path/to/Album"


def test_statuses_partial_match_is_downloading():
    """3-file job where only 2 transfers are returned (both Succeeded) → downloading."""
    payload_3: dict = {
        "username": "alice",
        "title": "Artist - Album [FLAC]",
        "album_folder": "Album",
        "total_size": 30_000_000,
        "files": [
            {"filename": r"@@a\Artist\Album\01.flac", "size": 10_000_000},
            {"filename": r"@@a\Artist\Album\02.flac", "size": 10_000_000},
            {"filename": r"@@a\Artist\Album\03.flac", "size": 10_000_000},
        ],
    }
    # Only 2 of 3 transfers returned, both succeeded
    gateway = FakeGateway(
        transfers_by_username={
            "alice": [
                make_transfer(
                    r"@@a\Artist\Album\01.flac",
                    transfer_id="t1",
                    state="Completed, Succeeded",
                    bytes_transferred=10_000_000,
                ),
                make_transfer(
                    r"@@a\Artist\Album\02.flac",
                    transfer_id="t2",
                    state="Completed, Succeeded",
                    bytes_transferred=10_000_000,
                ),
            ]
        }
    )
    service = DownloadService(
        gateway, FakeJobStore(), FakeClock(), downloads_dir="/downloads"
    )
    service.start(payload_3, "music")

    views = service.statuses()

    assert len(views) == 1
    v = views[0]
    assert v.state == "downloading"
    assert v.storage is None


# ---------------------------------------------------------------------------
# Tests — statuses: failed
# ---------------------------------------------------------------------------


def test_statuses_failed_sets_fail_message():
    gateway = FakeGateway(
        transfers_by_username={
            "alice": [
                make_transfer(
                    r"@@a\Artist\Album\01.flac",
                    transfer_id="t1",
                    state="Completed, Succeeded",
                    bytes_transferred=10_000_000,
                ),
                make_transfer(
                    r"@@a\Artist\Album\02.flac",
                    transfer_id="t2",
                    state="Completed, Errored",
                    bytes_transferred=0,
                    exception="no slots",
                ),
            ]
        }
    )
    service = DownloadService(
        gateway, FakeJobStore(), FakeClock(), downloads_dir="/downloads"
    )
    service.start(SAMPLE_PAYLOAD, "music")

    views = service.statuses()

    assert len(views) == 1
    v = views[0]
    assert v.state == "failed"
    assert v.fail_message == "no slots"
    assert v.storage is None


# ---------------------------------------------------------------------------
# Tests — remove
# ---------------------------------------------------------------------------


def test_remove_cancels_in_progress_transfers_and_removes_job():
    in_progress = make_transfer(
        r"@@a\Artist\Album\01.flac", transfer_id="t1", state="InProgress"
    )
    completed = make_transfer(
        r"@@a\Artist\Album\02.flac",
        transfer_id="t2",
        state="Completed, Succeeded",
        bytes_transferred=10_000_000,
    )
    gateway = FakeGateway(transfers_by_username={"alice": [in_progress, completed]})
    jobs = FakeJobStore()
    service = DownloadService(gateway, jobs, FakeClock(), downloads_dir="/downloads")
    nzo_id = service.start(SAMPLE_PAYLOAD, "music")

    service.remove(nzo_id)

    # Only in-progress transfer cancelled
    assert ("alice", "t1") in gateway.cancelled
    assert ("alice", "t2") not in gateway.cancelled
    # Job removed
    assert jobs.get(nzo_id) is None


def test_remove_unknown_id_is_noop():
    gateway = FakeGateway()
    service = DownloadService(
        gateway, FakeJobStore(), FakeClock(), downloads_dir="/downloads"
    )

    # Must not raise
    service.remove("SABnzbd_nzo_doesnotexist")

    assert gateway.cancelled == []
