"""Domain use-case: manage slskd download jobs."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from slskd_lidarr_bridge.domain.models import AudioFile, DownloadJob, JobStatusView
from slskd_lidarr_bridge.domain.paths import compute_storage_path
from slskd_lidarr_bridge.domain.ports import Clock, JobStore, SoulseekGateway


class DownloadService:
    def __init__(
        self,
        gateway: SoulseekGateway,
        jobs: JobStore,
        clock: Clock,
        *,
        downloads_dir: str,
    ) -> None:
        self._gateway = gateway
        self._jobs = jobs
        self._clock = clock
        self._downloads_dir = downloads_dir

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
        return nzo_id

    def statuses(self) -> list[JobStatusView]:
        """Return a JobStatusView for every tracked job."""
        views: list[JobStatusView] = []

        for job in self._jobs.list():
            all_transfers = self._gateway.transfers(job.username)
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
                        self._downloads_dir, job.files[0].filename
                    )
            elif any(t.is_failed for t in matched):
                state = "failed"
                failed = next(t for t in matched if t.is_failed)
                fail_message = failed.exception

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

        return views

    def remove(self, nzo_id: str) -> None:
        """Cancel in-progress transfers and remove the job. Unknown id is a no-op."""
        job = self._jobs.get(nzo_id)
        if job is None:
            return

        all_transfers = self._gateway.transfers(job.username)
        job_filenames = {f.filename for f in job.files}
        matched = [t for t in all_transfers if t.filename in job_filenames]

        for transfer in matched:
            if not transfer.is_complete:
                self._gateway.cancel(job.username, transfer.id)

        self._jobs.remove(nzo_id)
