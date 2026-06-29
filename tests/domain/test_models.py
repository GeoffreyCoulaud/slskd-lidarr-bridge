import pytest
from slskd_lidarr_bridge.domain.models import (
    AudioFile,
    SearchResponse,
    Transfer,
    SearchQuery,
    Release,
    DownloadJob,
    JobStatusView,
    AUDIO_EXTENSIONS,
)
from datetime import datetime


# ── AudioFile.album_folder ────────────────────────────────────────────────────

def test_audio_file_album_folder_backslash():
    f = AudioFile(filename=r"@@a\Music\Artist\Album Name\01 - x.flac", size=100)
    assert f.album_folder == "Album Name"


def test_audio_file_album_folder_forward_slash():
    f = AudioFile(filename="Music/Artist/Album Name/01 - x.flac", size=100)
    assert f.album_folder == "Album Name"


def test_audio_file_album_folder_at_root():
    f = AudioFile(filename="track.flac", size=100)
    assert f.album_folder == ""


# ── AudioFile.is_audio ────────────────────────────────────────────────────────

def test_audio_file_is_audio_flac():
    f = AudioFile(filename="x.flac", size=100, extension=".flac")
    assert f.is_audio is True


def test_audio_file_is_audio_txt():
    f = AudioFile(filename="x.txt", size=100, extension=".txt")
    assert f.is_audio is False


def test_audio_file_is_audio_none_extension():
    f = AudioFile(filename="x", size=100, extension=None)
    assert f.is_audio is False


def test_audio_file_is_audio_case_insensitive():
    # Extension stored uppercase should still match
    f = AudioFile(filename="x.FLAC", size=100, extension=".FLAC")
    assert f.is_audio is True


# ── Transfer properties ───────────────────────────────────────────────────────

def _transfer(**kwargs):
    defaults = dict(
        username="user",
        id="abc",
        filename="file.flac",
        size=1000,
        bytes_transferred=0,
        bytes_remaining=1000,
        percent_complete=0.0,
    )
    defaults.update(kwargs)
    return Transfer(**defaults)


def test_transfer_completed_succeeded():
    t = _transfer(state="Completed, Succeeded")
    assert t.is_complete is True
    assert t.is_succeeded is True
    assert t.is_failed is False


def test_transfer_completed_errored():
    t = _transfer(state="Completed, Errored")
    assert t.is_complete is True
    assert t.is_failed is True
    assert t.is_succeeded is False


def test_transfer_in_progress():
    t = _transfer(state="InProgress")
    assert t.is_complete is False
    assert t.is_succeeded is False
    assert t.is_failed is False


# ── SearchQuery ───────────────────────────────────────────────────────────────

def test_search_query_artist_album_not_empty():
    q = SearchQuery(artist="A", album="B")
    assert q.is_empty is False


def test_search_query_artist_album_to_search_text():
    q = SearchQuery(artist="A", album="B")
    assert q.to_search_text() == "A B"


def test_search_query_empty():
    q = SearchQuery()
    assert q.is_empty is True


def test_search_query_term_only():
    q = SearchQuery(term="x")
    assert q.to_search_text() == "x"


def test_search_query_artist_only():
    q = SearchQuery(artist="A")
    assert q.to_search_text() == "A"


# ── AUDIO_EXTENSIONS ─────────────────────────────────────────────────────────

def test_audio_extensions_contains_expected():
    for ext in (".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".alac", ".wma", ".ape"):
        assert ext in AUDIO_EXTENSIONS
