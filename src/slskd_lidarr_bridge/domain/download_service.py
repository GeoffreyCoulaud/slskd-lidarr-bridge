"""Domain use-case: manage slskd download jobs."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from slskd_lidarr_bridge.domain.models import AudioFile, DownloadJob, JobStatusView
from slskd_lidarr_bridge.domain.paths import compute_storage_path
from slskd_lidarr_bridge.domain.ports import Clock, JobStore, SoulseekGateway

logger = logging.getLogger(__name__)


class DownloadService:
    def __init__(
        self,
        gateway: SoulseekGateway,
        jobs: JobStore,
        clock: Clock,
        *,
        stall_timeout: int = 1800,
        max_retries: int = 1,
        _failed_purge_seconds: int = 86400,
    ) -> None:
        self._gateway = gateway
        self._jobs = jobs
        self._clock = clock
        self._stall_timeout = stall_timeout
        self._max_retries = max_retries
        self._failed_purge_seconds = _failed_purge_seconds
        # Guards _retries, _progress, and _terminal_since. Never hold while
        # calling gateway I/O (transfers / enqueue / cancel).
        self._lock: threading.Lock = threading.Lock()
        # nzo_id → first instant the job became terminal (completed or failed).
        # Presence drives log-once; for failed jobs it also drives the purge clock.
        self._terminal_since: dict[str, datetime] = {}
        # Retry rounds already spent per job (nzo_id → count). In-memory like
        # _terminal_since; a restart resets a job's retry budget, which only
        # means a few extra retries in a rare case.
        self._retries: dict[str, int] = {}
        # Per-job progress watermark for stall detection:
        # nzo_id → (max transferred bytes seen, when that maximum was first seen).
        # In-memory by design (mirrors _terminal_since): a restart simply restarts
        # each in-flight job's stall clock, which only delays detection by one
        # stall_timeout window — acceptable for a rare event.
        self._progress: dict[str, tuple[int, datetime]] = {}

    def completed_dir(self) -> str:
        """slskd's completed-downloads directory (reported to Lidarr as-is)."""
        return self._gateway.downloads_directory()

    def start(self, payload: dict[str, Any], category: str) -> str:
        """Enqueue files on slskd, persist a DownloadJob, return its nzo_id."""
        username: str = payload["username"]
        title: str = payload["title"]
        album_folder: str = payload["album_folder"]
        total_size: int = payload["total_size"]

        files = tuple(
            AudioFile(filename=f["filename"], size=f["size"]) for f in payload["files"]
        )

        self._gateway.enqueue(username, list(files))

        nzo_id = "SABnzbd_nzo_" + uuid4().hex[:12]

        job = DownloadJob(
            nzo_id=nzo_id,
            title=title,
            username=username,
            files=files,
            category=category,
            album_folder=album_folder,
            total_size=total_size,
            created_at=self._clock.now(),
        )
        self._jobs.add(job)
        logger.info("Enqueued download %s: %r (cat=%r)", nzo_id, title, category)
        return nzo_id

    def statuses(self) -> list[JobStatusView]:
        """Return a JobStatusView for every tracked job."""
        now = self._clock.now()
        jobs = self._jobs.list()

        # #5b — batch transfers() by username: one call per distinct user,
        # outside the lock (no slskd I/O while holding _lock).
        transfers_by_user = {
            u: self._gateway.transfers(u) for u in {j.username for j in jobs}
        }

        views: list[JobStatusView] = []
        purge_ids: list[str] = []

        for job in jobs:
            all_transfers = transfers_by_user.get(job.username, [])
            job_filenames = {f.filename for f in job.files}
            matched = [t for t in all_transfers if t.filename in job_filenames]

            total_bytes = job.total_size
            transferred_bytes = sum(t.bytes_transferred for t in matched)
            percent = (transferred_bytes / total_bytes * 100) if total_bytes else 0.0

            state = "downloading"
            storage: str | None = None
            fail_message: str | None = None

            if len(matched) == len(job.files) and all(t.is_succeeded for t in matched):
                state = "completed"
                lp = next((t.local_path for t in matched if t.local_path), None)
                if lp:
                    storage = str(PurePosixPath(lp).parent)
                else:
                    storage = compute_storage_path(
                        self._gateway.downloads_directory(), job.files[0].filename
                    )
            elif any(t.is_failed for t in matched):
                # #4 — atomic check-and-increment under the lock so two concurrent
                # threads cannot both read the same used count and both retry.
                # The gateway.enqueue() call happens OUTSIDE the lock.
                with self._lock:
                    used = self._retries.get(job.nzo_id, 0)
                    do_retry = used < self._max_retries
                    if do_retry:
                        self._retries[job.nzo_id] = used + 1
                        # A retry is activity — reset the stall clock.
                        self._progress[job.nzo_id] = (transferred_bytes, now)
                if do_retry:
                    failed_names = {t.filename for t in matched if t.is_failed}
                    retry_files = [f for f in job.files if f.filename in failed_names]
                    self._gateway.enqueue(job.username, retry_files)  # I/O outside lock
                    logger.info(
                        "Retrying download %s after failure (attempt %d/%d): %r",
                        job.nzo_id,
                        used + 1,
                        self._max_retries,
                        job.title,
                    )
                else:
                    state = "failed"
                    failed = next(t for t in matched if t.is_failed)
                    fail_message = failed.exception

            # Stall detection: a job still "downloading" but making no progress
            # for stall_timeout seconds is reported failed so Lidarr stops waiting
            # on a dead peer and can try another release. stall_timeout <= 0
            # disables it.
            if state == "downloading" and self._stall_timeout > 0:
                # #4 — RMW on _progress is under the lock.
                with self._lock:
                    prev = self._progress.get(job.nzo_id)
                    if prev is None or transferred_bytes > prev[0]:
                        # First sighting or fresh progress → (re)start the stall clock.
                        self._progress[job.nzo_id] = (transferred_bytes, now)
                    elif (now - prev[1]).total_seconds() >= self._stall_timeout:
                        state = "failed"
                        fail_message = (
                            f"stalled: no progress for {self._stall_timeout}s"
                        )

            # Log each terminal state once and check for purge eligibility.
            # logger calls stay outside the lock (logging is already thread-safe).
            log_action: str | None = None
            should_purge = False
            if state in ("completed", "failed"):
                with self._lock:
                    if job.nzo_id not in self._terminal_since:
                        # #5a — first terminal instant: drives log-once + purge.
                        self._terminal_since[job.nzo_id] = now
                        log_action = state
                    elif state == "failed":
                        # #5a — purge stale failed jobs (never purge completed).
                        terminal_age = (
                            now - self._terminal_since[job.nzo_id]
                        ).total_seconds()
                        if terminal_age >= self._failed_purge_seconds:
                            should_purge = True

            if log_action == "completed":
                logger.info("Download completed: %r → %s", job.title, storage)
            elif log_action == "failed":
                logger.warning("Download failed: %r (%s)", job.title, fail_message)

            if should_purge:
                purge_ids.append(job.nzo_id)
                continue  # exclude from returned views

            views.append(
                JobStatusView(
                    nzo_id=job.nzo_id,
                    title=job.title,
                    category=job.category,
                    total_bytes=total_bytes,
                    transferred_bytes=transferred_bytes,
                    percent=percent,
                    state=state,
                    storage=storage,
                    fail_message=fail_message,
                )
            )

        # #5a — remove stale failed jobs after the loop (don't mutate while iterating).
        for nzo_id in purge_ids:
            self._jobs.remove(nzo_id)
            with self._lock:
                self._terminal_since.pop(nzo_id, None)
                self._progress.pop(nzo_id, None)
                self._retries.pop(nzo_id, None)

        return views

    def remove(self, nzo_id: str) -> None:
        """Cancel in-progress transfers and remove the job. Unknown id is a no-op."""
        job = self._jobs.get(nzo_id)
        if job is None:
            return

        # I/O outside the lock.
        all_transfers = self._gateway.transfers(job.username)
        job_filenames = {f.filename for f in job.files}
        matched = [t for t in all_transfers if t.filename in job_filenames]

        for transfer in matched:
            if not transfer.is_complete:
                self._gateway.cancel(job.username, transfer.id)

        self._jobs.remove(nzo_id)
        # #4 — in-memory cleanup under the lock.
        with self._lock:
            self._terminal_since.pop(nzo_id, None)
            self._progress.pop(nzo_id, None)
            self._retries.pop(nzo_id, None)
        logger.info("Removed download %s: %r", nzo_id, job.title)
