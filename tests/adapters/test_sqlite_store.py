"""Tests for SqliteStore — ReleaseStore + JobStore over SQLite."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from slskd_lidarr_bridge.adapters.sqlite_store import SqliteStore
from slskd_lidarr_bridge.domain.models import AudioFile, DownloadJob, Release

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DT_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
DT_OLD = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

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


def make_release(created_at: datetime = DT_NOW) -> Release:
    return Release(
        artist="Test Artist",
        album="Test Album",
        title="Test Artist - Test Album [FLAC]",
        username="peer1",
        files=(FILE1, FILE2),
        size=55_000_000,
        album_folder="Album",
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


# ---------------------------------------------------------------------------
# ReleaseStore tests
# ---------------------------------------------------------------------------


def test_release_put_returns_id_and_get_roundtrips(tmp_path):
    store = SqliteStore(str(tmp_path / "db.sqlite"))
    rel = make_release()
    rid = store.put(rel)

    assert isinstance(rid, str)
    assert len(rid) == 16

    fetched = store.get(rid)
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


def test_release_get_unknown_returns_none(tmp_path):
    store = SqliteStore(str(tmp_path / "db.sqlite"))
    assert store.get("doesnotexist") is None


def test_release_purge_older_than(tmp_path):
    store = SqliteStore(str(tmp_path / "db.sqlite"))
    old_id = store.put(make_release(created_at=DT_OLD))
    new_id = store.put(make_release(created_at=DT_NOW))

    cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
    store.purge_older_than(cutoff)

    assert store.get(old_id) is None
    assert store.get(new_id) is not None


def test_release_persists_across_reopen(tmp_path):
    db_path = str(tmp_path / "db.sqlite")
    store1 = SqliteStore(db_path)
    rel = make_release()
    rid = store1.put(rel)

    # Open a NEW store on the same file — must see the row
    store2 = SqliteStore(db_path)
    fetched = store2.get(rid)
    assert fetched is not None
    assert fetched.artist == rel.artist


def test_release_put_on_memory_db():
    store = SqliteStore(":memory:")
    rel = make_release()
    rid = store.put(rel)
    fetched = store.get(rid)
    assert fetched is not None
    assert fetched.album == rel.album


# ---------------------------------------------------------------------------
# JobStore tests
# ---------------------------------------------------------------------------


def test_job_add_get_roundtrip(tmp_path):
    store = SqliteStore(str(tmp_path / "db.sqlite"))
    job = make_job()
    store.add(job)

    fetched = store.get(job.nzo_id)
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


def test_job_get_unknown_returns_none(tmp_path):
    store = SqliteStore(str(tmp_path / "db.sqlite"))
    assert store.get("nzo-unknown") is None


def test_job_list_returns_all(tmp_path):
    store = SqliteStore(str(tmp_path / "db.sqlite"))
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
    store.add(j1)
    store.add(j2)

    jobs = store.list()
    nzo_ids = {j.nzo_id for j in jobs}
    assert nzo_ids == {"nzo-abc", "nzo-def"}


def test_job_remove(tmp_path):
    store = SqliteStore(str(tmp_path / "db.sqlite"))
    job = make_job()
    store.add(job)
    store.remove(job.nzo_id)
    assert store.get(job.nzo_id) is None


def test_job_remove_unknown_no_error(tmp_path):
    store = SqliteStore(str(tmp_path / "db.sqlite"))
    store.remove("nzo-unknown")  # must not raise


def test_job_persists_across_reopen(tmp_path):
    db_path = str(tmp_path / "db.sqlite")
    SqliteStore(db_path).add(make_job())
    fetched = SqliteStore(db_path).get("nzo-abc")
    assert fetched is not None
    assert fetched.title == make_job().title
