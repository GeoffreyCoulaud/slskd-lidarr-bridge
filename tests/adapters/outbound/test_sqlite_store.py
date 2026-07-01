"""Tests for SqliteReleaseStore + SqliteJobStore — typed wrappers over SqliteStore."""

from __future__ import annotations

import dataclasses
import gc
import hashlib
import sqlite3
import warnings
from datetime import UTC, datetime

import pytest

from slskd_lidarr_bridge.adapters.outbound.sqlite_store import (
    SqliteStore,
    open_stores,
)
from slskd_lidarr_bridge.domain.models import AudioFile, DownloadJob, Release
from slskd_lidarr_bridge.domain.ports import JobStore, ReleaseStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DT_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
DT_OLD = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)

FILE1 = AudioFile(
    filename=r"@@peer\Music\Artist\Album\01.flac",
    size=30_000_000,
    extension=".flac",
    bitrate=1000,
    length=240,
)
FILE2 = AudioFile(
    filename=r"@@peer\Music\Artist\Album\02.flac",
    size=25_000_000,
    extension=".flac",
    bitrate=900,
    length=180,
)


def make_release(
    created_at: datetime = DT_NOW,
    *,
    username: str = "peer1",
    album_folder: str = "Album",
) -> Release:
    return Release(
        artist="Test Artist",
        album="Test Album",
        title="Test Artist - Test Album [FLAC]",
        username=username,
        files=(FILE1, FILE2),
        size=55_000_000,
        album_folder=album_folder,
        quality="FLAC",
        created_at=created_at,
    )


def make_job() -> DownloadJob:
    return DownloadJob(
        nzo_id="nzo-abc",
        title="Test Artist - Test Album [FLAC]",
        username="peer1",
        files=(FILE1,),
        category="music",
        album_folder="Album",
        total_size=30_000_000,
        created_at=DT_NOW,
    )


@pytest.fixture
def stores():
    """Factory for ``open_stores`` that closes every connection at teardown.

    Without this, each opened (and never-closed) SQLite connection is reported
    as an ``unclosed database`` ResourceWarning when garbage-collected.
    """
    opened = []

    def factory(db_path):
        rs, js = open_stores(db_path)
        opened.append(rs)
        return rs, js

    yield factory
    for rs in opened:
        rs.close()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_sqlite_release_store_satisfies_protocol(stores, tmp_path):
    rs, _ = stores(str(tmp_path / "db.sqlite"))
    assert isinstance(rs, ReleaseStore)


def test_sqlite_job_store_satisfies_protocol(stores, tmp_path):
    _, js = stores(str(tmp_path / "db.sqlite"))
    assert isinstance(js, JobStore)


# ---------------------------------------------------------------------------
# ReleaseStore tests
# ---------------------------------------------------------------------------


def test_release_put_returns_id_and_get_roundtrips(stores, tmp_path):
    rs, _ = stores(str(tmp_path / "db.sqlite"))
    rel = make_release()
    rid = rs.put(rel)

    assert isinstance(rid, str)
    assert len(rid) == 64
    assert all(c in "0123456789abcdef" for c in rid)

    fetched = rs.get(rid)
    assert fetched is not None
    assert fetched.id == rid
    assert fetched.artist == rel.artist
    assert fetched.album == rel.album
    assert fetched.title == rel.title
    assert fetched.username == rel.username
    assert fetched.size == rel.size
    assert fetched.album_folder == rel.album_folder
    assert fetched.quality == rel.quality
    assert fetched.created_at == rel.created_at
    assert len(fetched.files) == 2
    assert fetched.files[0].filename == FILE1.filename
    assert fetched.files[0].size == FILE1.size
    assert fetched.files[0].extension == FILE1.extension
    assert fetched.files[0].bitrate == FILE1.bitrate
    assert fetched.files[0].length == FILE1.length


def test_release_get_unknown_returns_none(stores, tmp_path):
    rs, _ = stores(str(tmp_path / "db.sqlite"))
    assert rs.get("doesnotexist") is None


def test_release_get_does_not_return_jobs(stores, tmp_path):
    """SqliteReleaseStore.get must only look up releases, not jobs."""
    rs, js = stores(str(tmp_path / "db.sqlite"))
    js.add(make_job())
    # nzo_id must NOT be found via the release store's get
    assert rs.get("nzo-abc") is None


def test_release_purge_older_than(stores, tmp_path):
    rs, _ = stores(str(tmp_path / "db.sqlite"))
    # Distinct folders → distinct ids (the id is derived from the folder), so the
    # two rows are independent and only the older one is purged.
    old_id = rs.put(make_release(created_at=DT_OLD, album_folder="OldAlbum"))
    new_id = rs.put(make_release(created_at=DT_NOW, album_folder="NewAlbum"))
    assert old_id != new_id

    cutoff = datetime(2024, 3, 1, tzinfo=UTC)
    rs.purge_older_than(cutoff)

    assert rs.get(old_id) is None
    assert rs.get(new_id) is not None


def test_release_persists_across_reopen(stores, tmp_path):
    db_path = str(tmp_path / "db.sqlite")
    rs1, _ = stores(db_path)
    rel = make_release()
    rid = rs1.put(rel)

    # Open NEW stores on the same file — must see the row.
    rs2, _ = stores(db_path)
    fetched = rs2.get(rid)
    assert fetched is not None
    assert fetched.artist == rel.artist


def test_release_put_on_memory_db(stores):
    rs, _ = stores(":memory:")
    rel = make_release()
    rid = rs.put(rel)
    fetched = rs.get(rid)
    assert fetched is not None
    assert fetched.album == rel.album


def test_release_naive_created_at_is_read_back_as_utc(stores, tmp_path):
    """A release stored with a tz-naive created_at is read back as UTC-aware.

    The DB column is plain ISO-8601 text, so a naive timestamp round-trips
    without an offset; on read it must be interpreted as UTC (not left naive),
    otherwise downstream tz-aware comparisons would raise TypeError.
    """
    rs, _ = stores(str(tmp_path / "db.sqlite"))
    naive = datetime(2024, 6, 1, 12, 0, 0)  # no tzinfo
    assert naive.tzinfo is None
    rid = rs.put(make_release(created_at=naive))

    fetched = rs.get(rid)
    assert fetched is not None
    assert fetched.created_at.tzinfo is not None
    assert fetched.created_at == datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Deterministic release id (SHA-256 of username + album_folder)
# ---------------------------------------------------------------------------


def test_release_id_is_sha256_of_username_and_folder(stores, tmp_path):
    rs, _ = stores(str(tmp_path / "db.sqlite"))
    rel = make_release(username="alice", album_folder="Cool Album (Deluxe)")
    rid = rs.put(rel)

    expected = hashlib.sha256(b"alice\x00Cool Album (Deluxe)").hexdigest()
    assert rid == expected


def test_release_put_same_folder_is_idempotent_and_replaces(stores, tmp_path):
    """Re-putting the same (username, folder) reuses the id and refreshes the row.

    With the old random uuid this created a second row and a fresh guid; now it
    must return the same id, not raise on the primary-key clash, and overwrite
    the stored fields (so created_at tracks the latest sighting for TTL purging).
    """
    rs, _ = stores(str(tmp_path / "db.sqlite"))
    first = rs.put(make_release(created_at=DT_OLD))
    updated = dataclasses.replace(make_release(created_at=DT_NOW), quality="MP3-320")
    second = rs.put(updated)

    assert first == second
    fetched = rs.get(second)
    assert fetched is not None
    assert fetched.created_at == DT_NOW
    assert fetched.quality == "MP3-320"


def test_release_id_differs_by_username_and_by_folder(stores, tmp_path):
    rs, _ = stores(str(tmp_path / "db.sqlite"))
    base = rs.put(make_release(username="u1", album_folder="A"))
    other_user = rs.put(make_release(username="u2", album_folder="A"))
    other_folder = rs.put(make_release(username="u1", album_folder="B"))

    assert len({base, other_user, other_folder}) == 3


# ---------------------------------------------------------------------------
# JobStore tests
# ---------------------------------------------------------------------------


def test_job_add_get_roundtrip(stores, tmp_path):
    _, js = stores(str(tmp_path / "db.sqlite"))
    job = make_job()
    js.add(job)

    fetched = js.get(job.nzo_id)
    assert fetched is not None
    assert fetched.nzo_id == job.nzo_id
    assert fetched.title == job.title
    assert fetched.username == job.username
    assert fetched.category == job.category
    assert fetched.album_folder == job.album_folder
    assert fetched.total_size == job.total_size
    assert fetched.created_at == job.created_at
    assert len(fetched.files) == 1
    assert fetched.files[0].filename == FILE1.filename


def test_job_get_unknown_returns_none(stores, tmp_path):
    _, js = stores(str(tmp_path / "db.sqlite"))
    assert js.get("nzo-unknown") is None


def test_job_get_does_not_return_releases(stores, tmp_path):
    """SqliteJobStore.get must only look up jobs, not releases."""
    rs, js = stores(str(tmp_path / "db.sqlite"))
    rid = rs.put(make_release())
    # release id must NOT be found via the job store's get
    assert js.get(rid) is None


def test_job_list_returns_all(stores, tmp_path):
    _, js = stores(str(tmp_path / "db.sqlite"))
    j1 = make_job()
    j2 = DownloadJob(
        nzo_id="nzo-def",
        title="Another",
        username="peer2",
        files=(FILE2,),
        category="music",
        album_folder="Other",
        total_size=25_000_000,
        created_at=DT_NOW,
    )
    js.add(j1)
    js.add(j2)

    jobs = js.list()
    nzo_ids = {j.nzo_id for j in jobs}
    assert nzo_ids == {"nzo-abc", "nzo-def"}


def test_job_remove(stores, tmp_path):
    _, js = stores(str(tmp_path / "db.sqlite"))
    job = make_job()
    js.add(job)
    js.remove(job.nzo_id)
    assert js.get(job.nzo_id) is None


def test_job_remove_unknown_no_error(stores, tmp_path):
    _, js = stores(str(tmp_path / "db.sqlite"))
    js.remove("nzo-unknown")  # must not raise


def test_job_persists_across_reopen(stores, tmp_path):
    db_path = str(tmp_path / "db.sqlite")
    _, js1 = stores(db_path)
    js1.add(make_job())

    _, js2 = stores(db_path)
    fetched = js2.get("nzo-abc")
    assert fetched is not None
    assert fetched.title == make_job().title


# ---------------------------------------------------------------------------
# Connection lifecycle: close() + context manager
# ---------------------------------------------------------------------------


def test_release_store_close_makes_further_use_raise(tmp_path):
    rs, _ = open_stores(str(tmp_path / "db.sqlite"))
    rs.close()
    with pytest.raises(sqlite3.ProgrammingError):
        rs.put(make_release())


def test_job_store_close_makes_further_use_raise(tmp_path):
    _, js = open_stores(str(tmp_path / "db.sqlite"))
    js.close()
    with pytest.raises(sqlite3.ProgrammingError):
        js.add(make_job())


def test_close_is_idempotent(tmp_path):
    rs, _ = open_stores(str(tmp_path / "db.sqlite"))
    rs.close()
    rs.close()  # second close must not raise


def test_closing_one_wrapper_closes_the_shared_connection(tmp_path):
    """Both wrappers share one connection: closing either tears it down."""
    rs, js = open_stores(str(tmp_path / "db.sqlite"))
    rs.close()
    with pytest.raises(sqlite3.ProgrammingError):
        js.add(make_job())


def test_store_is_usable_as_context_manager(tmp_path):
    db_path = str(tmp_path / "db.sqlite")
    with SqliteStore(db_path) as store:
        rid = store.put(make_release())
        assert store.get_release(rid) is not None
    # On exit the connection is closed; further use raises.
    with pytest.raises(sqlite3.ProgrammingError):
        store.put(make_release())


def test_closed_stores_emit_no_resource_warning(tmp_path):
    """Regression: a closed store is not reported as an unclosed database.

    Without an explicit close, the sqlite3 connection's finaliser emits
    ``ResourceWarning: unclosed database`` when garbage-collected.
    """
    rs, js = open_stores(str(tmp_path / "db.sqlite"))
    rs.close()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        del rs, js
        gc.collect()
    resource_warnings = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert resource_warnings == []
