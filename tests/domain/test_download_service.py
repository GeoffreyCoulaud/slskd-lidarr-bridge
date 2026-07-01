"""Tests for DownloadService (Task 8)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

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
        # Optional service reference — when set, I/O methods assert not locked.
        self.service: DownloadService | None = None
        # Count calls per username for batching assertions.
        self.transfers_call_count: dict[str, int] = {}

    def _assert_not_locked(self) -> None:
        if self.service is not None:
            assert not self.service._lock.locked(), (
                "Gateway I/O called while DownloadService._lock is held"
            )

    # Unused search methods — satisfy Protocol
    def start_search(self, text: str, timeout_seconds: float) -> str:
        return ""

    def search_is_complete(self, search_id: str) -> bool:
        return True

    def search_responses(self, search_id: str) -> list[SearchResponse]:
        return []

    def enqueue(self, username: str, files: list[AudioFile]) -> None:
        self._assert_not_locked()
        self.enqueued.append((username, list(files)))

    def transfers(self, username: str) -> list[Transfer]:
        self._assert_not_locked()
        self.transfers_call_count[username] = (
            self.transfers_call_count.get(username, 0) + 1
        )
        return list(self._transfers.get(username, []))

    def set_transfers(self, username: str, transfers: list[Transfer]) -> None:
        """Replace a user's transfers — used to script state changes across polls."""
        self._transfers[username] = transfers

    def cancel(self, username: str, transfer_id: str) -> None:
        self._assert_not_locked()
        self.cancelled.append((username, transfer_id))

    def downloads_directory(self) -> str:
        return "/downloads"


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

    def advance(self, seconds: float) -> None:
        """Move the clock forward — used to exercise time-based behaviour."""
        self._now = self._now + timedelta(seconds=seconds)


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


SAMPLE_PAYLOAD_1: dict = {
    "username": "alice",
    "title": "Artist - Album [FLAC]",
    "album_folder": "Album",
    "total_size": 10_000_000,
    "files": [
        {"filename": r"@@a\Artist\Album\01.flac", "size": 10_000_000},
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


def test_completed_dir_delegates_to_gateway():
    """completed_dir() returns slskd's downloads directory from the gateway."""
    service = DownloadService(FakeGateway(), FakeJobStore(), FakeClock())
    assert service.completed_dir() == "/downloads"


def test_start_returns_nzo_id_with_correct_prefix():
    service = DownloadService(FakeGateway(), FakeJobStore(), FakeClock())
    nzo_id = service.start(SAMPLE_PAYLOAD, "music")
    assert nzo_id.startswith("SABnzbd_nzo_")


def test_start_enqueues_files_on_gateway():
    gateway = FakeGateway()
    service = DownloadService(gateway, FakeJobStore(), FakeClock())

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
    service = DownloadService(FakeGateway(), jobs, FakeClock())

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
    service = DownloadService(gateway, FakeJobStore(), FakeClock())
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
    service = DownloadService(gateway, FakeJobStore(), FakeClock())
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
    service = DownloadService(gateway, FakeJobStore(), FakeClock())
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
    service = DownloadService(gateway, FakeJobStore(), FakeClock())
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
    service = DownloadService(gateway, FakeJobStore(), FakeClock())
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
    # max_retries=0 isolates the terminal-failure mapping (no re-enqueue).
    service = DownloadService(gateway, FakeJobStore(), FakeClock(), max_retries=0)
    service.start(SAMPLE_PAYLOAD, "music")

    views = service.statuses()

    assert len(views) == 1
    v = views[0]
    assert v.state == "failed"
    assert v.fail_message == "no slots"
    assert v.storage is None


# ---------------------------------------------------------------------------
# Tests — statuses: stalled (no progress past stall_timeout)
# ---------------------------------------------------------------------------


def test_statuses_stalled_download_becomes_failed():
    """A download making zero progress past stall_timeout is reported failed.

    A peer that queues us remotely but never serves (or goes offline) leaves the
    transfer making no progress indefinitely; without this it would stay
    'downloading' forever and pin Lidarr's queue slot.
    """
    clock = FakeClock()
    gateway = FakeGateway(
        transfers_by_username={
            "alice": [
                make_transfer(
                    r"@@a\Artist\Album\01.flac",
                    transfer_id="t1",
                    state="Queued, Remotely",
                    bytes_transferred=0,
                ),
                make_transfer(
                    r"@@a\Artist\Album\02.flac",
                    transfer_id="t2",
                    state="Queued, Remotely",
                    bytes_transferred=0,
                ),
            ]
        }
    )
    service = DownloadService(gateway, FakeJobStore(), clock, stall_timeout=1800)
    service.start(SAMPLE_PAYLOAD, "music")

    # First poll establishes the progress baseline; still downloading.
    assert service.statuses()[0].state == "downloading"

    # No progress for longer than the stall timeout → failed.
    clock.advance(1801)
    v = service.statuses()[0]
    assert v.state == "failed"
    assert "stall" in (v.fail_message or "").lower()


def test_statuses_progress_resets_stall_clock():
    """Byte progress between polls resets the stall timer."""
    clock = FakeClock()
    gateway = FakeGateway(
        transfers_by_username={
            "alice": [
                make_transfer(
                    r"@@a\Artist\Album\01.flac",
                    transfer_id="t1",
                    state="InProgress",
                    bytes_transferred=0,
                )
            ]
        }
    )
    service = DownloadService(gateway, FakeJobStore(), clock, stall_timeout=1800)
    service.start(SAMPLE_PAYLOAD_1, "music")

    assert service.statuses()[0].state == "downloading"  # baseline at t=0

    clock.advance(1000)
    gateway.set_transfers(
        "alice",
        [
            make_transfer(
                r"@@a\Artist\Album\01.flac",
                transfer_id="t1",
                state="InProgress",
                bytes_transferred=5_000_000,
            )
        ],
    )
    assert service.statuses()[0].state == "downloading"  # progress resets clock

    clock.advance(1000)  # 2000s elapsed total, but only 1000s since last progress
    assert service.statuses()[0].state == "downloading"


def test_statuses_stall_disabled_never_fails():
    """stall_timeout=0 disables the check: a stuck download stays 'downloading'."""
    clock = FakeClock()
    gateway = FakeGateway(
        transfers_by_username={
            "alice": [
                make_transfer(
                    r"@@a\Artist\Album\01.flac",
                    transfer_id="t1",
                    state="Queued, Remotely",
                    bytes_transferred=0,
                )
            ]
        }
    )
    service = DownloadService(gateway, FakeJobStore(), clock, stall_timeout=0)
    service.start(SAMPLE_PAYLOAD_1, "music")

    service.statuses()
    clock.advance(100_000)
    assert service.statuses()[0].state == "downloading"


# ---------------------------------------------------------------------------
# Tests — statuses: retries (re-enqueue failed transfers before giving up)
# ---------------------------------------------------------------------------


def _errored_transfer() -> Transfer:
    return make_transfer(
        r"@@a\Artist\Album\01.flac",
        transfer_id="t1",
        state="Completed, Errored",
        bytes_transferred=0,
        exception="no slots",
    )


def test_statuses_failed_transfer_is_retried_and_reported_downloading():
    """A failed transfer with retries remaining is re-enqueued, kept downloading.

    Soulseek transfers fail transiently; reporting 'failed' on the first error
    would make Lidarr blocklist the release immediately. Instead we re-enqueue.
    """
    gateway = FakeGateway(transfers_by_username={"alice": [_errored_transfer()]})
    service = DownloadService(gateway, FakeJobStore(), FakeClock(), max_retries=1)
    service.start(SAMPLE_PAYLOAD_1, "music")  # initial enqueue

    v = service.statuses()[0]

    assert v.state == "downloading"
    # start() enqueued once; the retry re-enqueues the failed file (2 total).
    assert len(gateway.enqueued) == 2
    username, files = gateway.enqueued[1]
    assert username == "alice"
    assert [f.filename for f in files] == [r"@@a\Artist\Album\01.flac"]


def test_statuses_retries_exhausted_then_failed():
    """Once retries are exhausted, a still-failing transfer is reported failed."""
    gateway = FakeGateway(transfers_by_username={"alice": [_errored_transfer()]})
    service = DownloadService(gateway, FakeJobStore(), FakeClock(), max_retries=1)
    service.start(SAMPLE_PAYLOAD_1, "music")

    # First poll consumes the single retry → downloading.
    assert service.statuses()[0].state == "downloading"
    # Still failing on the next poll, no retries left → failed.
    v = service.statuses()[0]
    assert v.state == "failed"
    assert v.fail_message == "no slots"


def test_statuses_retries_disabled_fails_immediately():
    """max_retries=0 preserves the original behaviour: fail on first error."""
    gateway = FakeGateway(transfers_by_username={"alice": [_errored_transfer()]})
    service = DownloadService(gateway, FakeJobStore(), FakeClock(), max_retries=0)
    service.start(SAMPLE_PAYLOAD_1, "music")

    v = service.statuses()[0]

    assert v.state == "failed"
    assert len(gateway.enqueued) == 1  # no retry enqueue


def test_statuses_retried_transfer_then_succeeds_completes():
    """A re-enqueued transfer that subsequently succeeds completes normally."""
    gateway = FakeGateway(transfers_by_username={"alice": [_errored_transfer()]})
    service = DownloadService(gateway, FakeJobStore(), FakeClock(), max_retries=1)
    service.start(SAMPLE_PAYLOAD_1, "music")

    assert service.statuses()[0].state == "downloading"  # retry issued
    gateway.set_transfers(
        "alice",
        [
            make_transfer(
                r"@@a\Artist\Album\01.flac",
                transfer_id="t1",
                state="Completed, Succeeded",
                bytes_transferred=10_000_000,
            )
        ],
    )
    assert service.statuses()[0].state == "completed"


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
    service = DownloadService(gateway, jobs, FakeClock())
    nzo_id = service.start(SAMPLE_PAYLOAD, "music")

    service.remove(nzo_id)

    # Only in-progress transfer cancelled
    assert ("alice", "t1") in gateway.cancelled
    assert ("alice", "t2") not in gateway.cancelled
    # Job removed
    assert jobs.get(nzo_id) is None


def test_remove_unknown_id_is_noop():
    gateway = FakeGateway()
    service = DownloadService(gateway, FakeJobStore(), FakeClock())

    # Must not raise
    service.remove("SABnzbd_nzo_doesnotexist")

    assert gateway.cancelled == []


# ---------------------------------------------------------------------------
# Tests — lifecycle logging
# ---------------------------------------------------------------------------


def test_start_logs_enqueue(caplog):
    service = DownloadService(FakeGateway(), FakeJobStore(), FakeClock())
    with caplog.at_level(logging.INFO):
        nzo_id = service.start(SAMPLE_PAYLOAD, "music")
    assert any(
        r.levelno == logging.INFO and nzo_id in r.getMessage()
        for r in caplog.records
        if "download_service" in r.name
    )


def test_statuses_completed_logs_once(caplog):
    """A completed download logs INFO exactly once, not on every poll."""
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
    service = DownloadService(gateway, FakeJobStore(), FakeClock())
    service.start(SAMPLE_PAYLOAD, "music")

    with caplog.at_level(logging.INFO):
        service.statuses()
        service.statuses()  # polled again — must not log a second time

    completed = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO
        and "completed" in r.getMessage().lower()
        and "download_service" in r.name
    ]
    assert len(completed) == 1


def test_statuses_failed_logs_warning(caplog):
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
    service = DownloadService(gateway, FakeJobStore(), FakeClock(), max_retries=0)
    service.start(SAMPLE_PAYLOAD, "music")

    with caplog.at_level(logging.WARNING):
        service.statuses()

    assert any(
        r.levelno == logging.WARNING and "failed" in r.getMessage().lower()
        for r in caplog.records
        if "download_service" in r.name
    )


def test_remove_logs(caplog):
    in_progress = make_transfer(
        r"@@a\Artist\Album\01.flac", transfer_id="t1", state="InProgress"
    )
    gateway = FakeGateway(transfers_by_username={"alice": [in_progress]})
    service = DownloadService(gateway, FakeJobStore(), FakeClock())
    nzo_id = service.start(SAMPLE_PAYLOAD, "music")

    with caplog.at_level(logging.INFO):
        service.remove(nzo_id)

    assert any(
        r.levelno == logging.INFO
        and nzo_id in r.getMessage()
        and "remov" in r.getMessage().lower()
        for r in caplog.records
        if "download_service" in r.name
    )


# ---------------------------------------------------------------------------
# Tests — thread-safety: no I/O while lock held + atomic retry
# ---------------------------------------------------------------------------


def test_no_io_while_lock_held_during_statuses():
    """Gateway I/O (transfers, enqueue, cancel) must not happen while the lock is held.

    FakeGateway.service is wired so each I/O method asserts not locked.
    A failing job triggers both a transfers() call and a retry enqueue(),
    exercising both paths.
    """
    gateway = FakeGateway(transfers_by_username={"alice": [_errored_transfer()]})
    service = DownloadService(gateway, FakeJobStore(), FakeClock(), max_retries=1)
    gateway.service = service
    service.start(SAMPLE_PAYLOAD_1, "music")

    # Must not raise AssertionError from _assert_not_locked.
    service.statuses()


def test_no_io_while_lock_held_during_remove():
    """transfers() and cancel() in remove() must not happen while the lock is held."""
    in_progress = make_transfer(
        r"@@a\Artist\Album\01.flac", transfer_id="t1", state="InProgress"
    )
    gateway = FakeGateway(transfers_by_username={"alice": [in_progress]})
    service = DownloadService(gateway, FakeJobStore(), FakeClock())
    gateway.service = service
    nzo_id = service.start(SAMPLE_PAYLOAD_1, "music")

    # Must not raise AssertionError from _assert_not_locked.
    service.remove(nzo_id)


def test_retry_reserved_atomically_no_double_enqueue():
    """The retry count is reserved under the lock so two sequential statuses() calls
    with max_retries=1 produce exactly one retry enqueue (not two).

    Simulates the race: the first call reserves the retry slot; the second call
    sees retries exhausted and reports failed — no extra enqueue.
    """
    gateway = FakeGateway(transfers_by_username={"alice": [_errored_transfer()]})
    service = DownloadService(gateway, FakeJobStore(), FakeClock(), max_retries=1)
    service.start(SAMPLE_PAYLOAD_1, "music")

    first = service.statuses()[0]
    assert first.state == "downloading"

    # Transfer is still errored on the second poll — retries exhausted.
    second = service.statuses()[0]
    assert second.state == "failed"

    # start() enqueued once; exactly one retry enqueue — total = 2.
    assert len(gateway.enqueued) == 2


# ---------------------------------------------------------------------------
# Tests — terminal-age purge of stale failed jobs
# ---------------------------------------------------------------------------


def test_failed_job_purged_after_threshold():
    """A failed job whose terminal age exceeds _failed_purge_seconds is dropped
    from the next statuses() and removed from the job store.
    """
    clock = FakeClock()
    gateway = FakeGateway(transfers_by_username={"alice": [_errored_transfer()]})
    jobs = FakeJobStore()
    service = DownloadService(
        gateway, jobs, clock, max_retries=0, _failed_purge_seconds=1
    )
    nzo_id = service.start(SAMPLE_PAYLOAD_1, "music")

    # First poll: job transitions to failed, records terminal_since.
    views = service.statuses()
    assert len(views) == 1
    assert views[0].state == "failed"

    # Advance clock past threshold.
    clock.advance(2)

    # Second poll: terminal age > 1s → purged.
    views = service.statuses()
    assert len(views) == 0
    assert jobs.get(nzo_id) is None


def test_failed_job_retained_within_threshold():
    """A failed job younger than the threshold is still included in statuses()."""
    clock = FakeClock()
    gateway = FakeGateway(transfers_by_username={"alice": [_errored_transfer()]})
    jobs = FakeJobStore()
    service = DownloadService(
        gateway, jobs, clock, max_retries=0, _failed_purge_seconds=86400
    )
    nzo_id = service.start(SAMPLE_PAYLOAD_1, "music")

    service.statuses()  # record terminal_since
    clock.advance(3600)  # only 1 h — well within 24 h

    views = service.statuses()
    assert len(views) == 1
    assert views[0].state == "failed"
    assert jobs.get(nzo_id) is not None


def test_completed_job_never_auto_purged():
    """completed jobs are never auto-purged regardless of age."""
    clock = FakeClock()
    gateway = FakeGateway(
        transfers_by_username={
            "alice": [
                make_transfer(
                    r"@@a\Artist\Album\01.flac",
                    transfer_id="t1",
                    state="Completed, Succeeded",
                    bytes_transferred=10_000_000,
                ),
            ]
        }
    )
    jobs = FakeJobStore()
    # Very short purge threshold — completed jobs must still survive.
    service = DownloadService(gateway, jobs, clock, _failed_purge_seconds=1)
    nzo_id = service.start(SAMPLE_PAYLOAD_1, "music")

    service.statuses()  # record terminal_since as completed
    clock.advance(100_000)  # way past any threshold

    views = service.statuses()
    assert len(views) == 1
    assert views[0].state == "completed"
    assert jobs.get(nzo_id) is not None


# ---------------------------------------------------------------------------
# Tests — batching: transfers() called once per username per statuses()
# ---------------------------------------------------------------------------


SAMPLE_PAYLOAD_2: dict = {
    "username": "alice",
    "title": "Artist - Album2 [FLAC]",
    "album_folder": "Album2",
    "total_size": 10_000_000,
    "files": [
        {"filename": r"@@a\Artist\Album2\01.flac", "size": 10_000_000},
    ],
}


def test_transfers_called_once_per_username():
    """With two jobs sharing one username, transfers() is called exactly once
    per statuses() invocation — not once per job.
    """
    gateway = FakeGateway(transfers_by_username={"alice": []})
    service = DownloadService(gateway, FakeJobStore(), FakeClock())
    service.start(SAMPLE_PAYLOAD_1, "music")
    service.start(SAMPLE_PAYLOAD_2, "music")

    service.statuses()

    assert gateway.transfers_call_count.get("alice", 0) == 1


def test_transfers_called_once_per_username_multiple_polls():
    """Batching holds across multiple polls: each statuses() call issues exactly
    one transfers() call per distinct username.
    """
    gateway = FakeGateway(transfers_by_username={"alice": []})
    service = DownloadService(gateway, FakeJobStore(), FakeClock())
    service.start(SAMPLE_PAYLOAD_1, "music")
    service.start(SAMPLE_PAYLOAD_2, "music")

    service.statuses()
    service.statuses()

    # Two polls × one username = exactly 2 total calls.
    assert gateway.transfers_call_count.get("alice", 0) == 2
