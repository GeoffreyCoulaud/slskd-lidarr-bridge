"""Tests for domain.paths.compute_storage_path."""

from slskd_lidarr_bridge.domain.paths import compute_storage_path


def test_backslash_remote_path() -> None:
    result = compute_storage_path(
        "/downloads",
        r"@@abc\Music\Artist\Album Name\01.flac",
    )
    assert result == "/downloads/Album Name"


def test_forward_slash_remote_path() -> None:
    result = compute_storage_path(
        "/downloads",
        "@@abc/Music/Artist/Album Name/01.flac",
    )
    assert result == "/downloads/Album Name"


def test_trailing_slash_on_downloads_dir_normalized() -> None:
    result = compute_storage_path(
        "/downloads/",
        r"@@abc\Music\Artist\Album Name\01.flac",
    )
    assert result == "/downloads/Album Name"


def test_different_album_folder() -> None:
    result = compute_storage_path(
        "/srv/slskd/downloads",
        r"user\Artist - 2020 - Some Album\05.mp3",
    )
    assert result == "/srv/slskd/downloads/Artist - 2020 - Some Album"


def test_mixed_separators_in_remote_path() -> None:
    result = compute_storage_path(
        "/downloads",
        r"@@u\Music/Artist\Album/track.flac",
    )
    assert result == "/downloads/Album"


def test_root_level_file_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="remote_filename has no album folder"):
        compute_storage_path("/downloads", "01.flac")


def test_root_downloads_dir_stays_absolute() -> None:
    # "/" stripped → "" which would yield a relative path; guard keeps it "/"
    result = compute_storage_path("/", r"x\Album\track.flac")
    assert result == "/Album"
