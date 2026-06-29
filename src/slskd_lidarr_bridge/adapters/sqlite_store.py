"""SQLite-backed store adapter — implements both ReleaseStore and JobStore."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from uuid import uuid4

from slskd_lidarr_bridge.domain.models import AudioFile, DownloadJob, Release

# Module-level write lock; shared across all SqliteStore instances so that
# Flask+waitress (threaded) cannot corrupt concurrent writes.
_WRITE_LOCK = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS releases (
    id          TEXT PRIMARY KEY,
    artist      TEXT NOT NULL,
    album       TEXT NOT NULL,
    title       TEXT NOT NULL,
    username    TEXT NOT NULL,
    files       TEXT NOT NULL,   -- JSON
    size        INTEGER NOT NULL,
    album_folder TEXT NOT NULL,
    quality     TEXT NOT NULL,
    created_at  TEXT NOT NULL    -- ISO-8601
);

CREATE TABLE IF NOT EXISTS jobs (
    nzo_id      TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    username    TEXT NOT NULL,
    files       TEXT NOT NULL,   -- JSON
    category    TEXT NOT NULL,
    album_folder TEXT NOT NULL,
    total_size  INTEGER NOT NULL,
    created_at  TEXT NOT NULL    -- ISO-8601
);
"""


def _files_to_json(files: tuple[AudioFile, ...]) -> str:
    return json.dumps(
        [
            {
                "filename": f.filename,
                "size": f.size,
                "extension": f.extension,
                "bitrate": f.bitrate,
                "length": f.length,
            }
            for f in files
        ]
    )


def _json_to_files(raw: str) -> tuple[AudioFile, ...]:
    return tuple(
        AudioFile(
            filename=d["filename"],
            size=d["size"],
            extension=d.get("extension"),
            bitrate=d.get("bitrate"),
            length=d.get("length"),
        )
        for d in json.loads(raw)
    )


def _parse_dt(s: str) -> datetime:
    """Parse an ISO-8601 string (UTC) into an aware datetime."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class SqliteStore:
    """Combined ReleaseStore + JobStore backed by SQLite.

    Thread-safe for Flask+waitress: ``check_same_thread=False`` is set on
    the connection, and all mutating operations are serialised with a
    module-level lock.
    """

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with _WRITE_LOCK:
            self._conn.executescript(_DDL)
            self._conn.commit()

    # ------------------------------------------------------------------
    # ReleaseStore
    # ------------------------------------------------------------------

    def put(self, release: Release) -> str:
        """Persist a Release; returns the newly assigned 16-char hex id."""
        new_id = uuid4().hex[:16]
        with _WRITE_LOCK:
            self._conn.execute(
                """
                INSERT INTO releases
                    (id, artist, album, title, username, files, size,
                     album_folder, quality, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id,
                    release.artist,
                    release.album,
                    release.title,
                    release.username,
                    _files_to_json(release.files),
                    release.size,
                    release.album_folder,
                    release.quality,
                    release.created_at.isoformat(),
                ),
            )
            self._conn.commit()
        return new_id

    def get_release(self, release_id: str) -> Release | None:
        """Fetch a Release by its id (as returned by put)."""
        row = self._conn.execute(
            "SELECT * FROM releases WHERE id = ?", (release_id,)
        ).fetchone()
        if row is None:
            return None
        return Release(
            id=row["id"],
            artist=row["artist"],
            album=row["album"],
            title=row["title"],
            username=row["username"],
            files=_json_to_files(row["files"]),
            size=row["size"],
            album_folder=row["album_folder"],
            quality=row["quality"],
            created_at=_parse_dt(row["created_at"]),
        )

    def get(self, id_: str) -> Release | DownloadJob | None:  # type: ignore[override]
        """Satisfy both ReleaseStore.get and JobStore.get.

        Searches releases first (by release id), then jobs (by nzo_id).
        In practice release IDs (uuid4 hex[:16]) and nzo_ids are distinct
        namespaces so there is no ambiguity.
        """
        rel = self.get_release(id_)
        if rel is not None:
            return rel
        return self.get_job(id_)

    def purge_older_than(self, cutoff: datetime) -> None:
        """Delete releases whose created_at is strictly before cutoff."""
        with _WRITE_LOCK:
            self._conn.execute(
                "DELETE FROM releases WHERE created_at < ?",
                (cutoff.isoformat(),),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # JobStore
    # ------------------------------------------------------------------

    def add(self, job: DownloadJob) -> None:
        with _WRITE_LOCK:
            self._conn.execute(
                """
                INSERT INTO jobs
                    (nzo_id, title, username, files, category, album_folder,
                     total_size, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.nzo_id,
                    job.title,
                    job.username,
                    _files_to_json(job.files),
                    job.category,
                    job.album_folder,
                    job.total_size,
                    job.created_at.isoformat(),
                ),
            )
            self._conn.commit()

    def get_job(self, nzo_id: str) -> DownloadJob | None:
        """Fetch a DownloadJob by nzo_id."""
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE nzo_id = ?", (nzo_id,)
        ).fetchone()
        if row is None:
            return None
        return DownloadJob(
            nzo_id=row["nzo_id"],
            title=row["title"],
            username=row["username"],
            files=_json_to_files(row["files"]),
            category=row["category"],
            album_folder=row["album_folder"],
            total_size=row["total_size"],
            created_at=_parse_dt(row["created_at"]),
        )

    def list(self) -> list[DownloadJob]:
        rows = self._conn.execute("SELECT * FROM jobs").fetchall()
        return [
            DownloadJob(
                nzo_id=row["nzo_id"],
                title=row["title"],
                username=row["username"],
                files=_json_to_files(row["files"]),
                category=row["category"],
                album_folder=row["album_folder"],
                total_size=row["total_size"],
                created_at=_parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    def remove(self, nzo_id: str) -> None:
        with _WRITE_LOCK:
            self._conn.execute("DELETE FROM jobs WHERE nzo_id = ?", (nzo_id,))
            self._conn.commit()
