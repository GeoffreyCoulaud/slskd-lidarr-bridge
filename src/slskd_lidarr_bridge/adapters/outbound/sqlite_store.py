"""SQLite-backed store adapter — typed wrappers satisfy the store Protocols.

Public API
----------
- ``SqliteReleaseStore``  — implements ``ReleaseStore``
- ``SqliteJobStore``      — implements ``JobStore``
- ``open_stores(db_path) -> tuple[SqliteReleaseStore, SqliteJobStore]``
  Build both typed wrappers sharing one underlying ``SqliteStore``.

Internal
--------
- ``SqliteStore`` holds the DB connection and both tables; not meant to be
  imported by application code directly.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from types import TracebackType
from uuid import uuid4

from slskd_lidarr_bridge.domain.models import AudioFile, DownloadJob, Release

# Fixed location of the SQLite file inside the container's ``/data`` volume.
# Not user-configurable; tests override it by passing ``db_path`` to
# ``open_stores`` (or by patching this constant).
DEFAULT_DB_PATH = "/data/bridge.db"

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
        dt = dt.replace(tzinfo=UTC)
    return dt


class SqliteStore:
    """Internal DB adapter holding both tables.

    Thread-safe for Flask+waitress: ``check_same_thread=False`` is set on the
    connection, and all mutating operations are serialised with a *per-instance*
    lock (avoids false cross-instance serialisation when multiple in-memory
    databases are used, e.g. in tests).

    Prefer the typed wrappers ``SqliteReleaseStore`` / ``SqliteJobStore`` or
    the convenience factory ``open_stores`` over this class directly.
    """

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._write_lock = threading.Lock()
        with self._write_lock:
            self._conn.executescript(_DDL)
            self._conn.commit()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying SQLite connection.

        Idempotent: closing an already-closed connection is a no-op, so this
        is safe to call from shutdown handlers or test teardown. After closing,
        any store operation raises ``sqlite3.ProgrammingError``.
        """
        self._conn.close()

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Release table
    # ------------------------------------------------------------------

    def put(self, release: Release) -> str:
        """Persist a Release; returns the newly assigned 16-char hex id."""
        new_id = uuid4().hex[:16]
        with self._write_lock:
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

    def purge_older_than(self, cutoff: datetime) -> None:
        """Delete releases whose created_at is strictly before cutoff."""
        with self._write_lock:
            self._conn.execute(
                "DELETE FROM releases WHERE created_at < ?",
                (cutoff.isoformat(),),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Job table
    # ------------------------------------------------------------------

    def add(self, job: DownloadJob) -> None:
        with self._write_lock:
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
        with self._write_lock:
            self._conn.execute("DELETE FROM jobs WHERE nzo_id = ?", (nzo_id,))
            self._conn.commit()


class SqliteReleaseStore:
    """``ReleaseStore`` Protocol implementation backed by a shared ``SqliteStore``.

    Build via ``open_stores(db_path)`` to share a single SQLite connection with
    ``SqliteJobStore``.
    """

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def put(self, release: Release) -> str:
        return self._store.put(release)

    def get(self, release_id: str) -> Release | None:
        return self._store.get_release(release_id)

    def purge_older_than(self, cutoff: datetime) -> None:
        self._store.purge_older_than(cutoff)

    def close(self) -> None:
        """Close the shared SQLite connection (also closes the paired job store)."""
        self._store.close()


class SqliteJobStore:
    """``JobStore`` Protocol implementation backed by a shared ``SqliteStore``.

    Build via ``open_stores(db_path)`` to share a single SQLite connection with
    ``SqliteReleaseStore``.
    """

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def add(self, job: DownloadJob) -> None:
        self._store.add(job)

    def get(self, nzo_id: str) -> DownloadJob | None:
        return self._store.get_job(nzo_id)

    def list(self) -> list[DownloadJob]:
        return self._store.list()

    def remove(self, nzo_id: str) -> None:
        self._store.remove(nzo_id)

    def close(self) -> None:
        """Close the shared SQLite connection (also closes the paired release store)."""
        self._store.close()


def open_stores(
    db_path: str | None = None,
) -> tuple[SqliteReleaseStore, SqliteJobStore]:
    """Build both typed wrappers sharing one ``SqliteStore`` (one DB connection).

    Usage::

        release_store, job_store = open_stores()          # DEFAULT_DB_PATH
        release_store, job_store = open_stores("/tmp/x.db")  # explicit override

    ``db_path`` defaults to ``DEFAULT_DB_PATH`` (resolved at call time). Both
    wrappers share the same underlying connection and per-instance write lock,
    so they are safe to use from multiple threads simultaneously.
    """
    store = SqliteStore(db_path if db_path is not None else DEFAULT_DB_PATH)
    return SqliteReleaseStore(store), SqliteJobStore(store)
