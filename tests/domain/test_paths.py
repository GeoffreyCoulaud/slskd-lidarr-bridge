"""Tests for domain.paths.compute_storage_path."""

from slskd_lidarr_bridge.domain.paths import (
    batch_destination,
    build_album_destination,
    compute_storage_path,
    sanitize_segment,
)


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


def test_sanitize_segment_replaces_separators_and_hostile_characters():
    assert sanitize_segment("AC/DC") == "AC_DC"
    assert sanitize_segment("Album: The Return") == "Album_ The Return"
    assert sanitize_segment("a\\b") == "a_b"
    assert sanitize_segment('q?w*e|r<t>y"u') == "q_w_e_r_t_y_u"


def test_sanitize_segment_replaces_control_characters():
    assert sanitize_segment("a\x00b\x1fc") == "a_b_c"


def test_sanitize_segment_strips_windows_hostile_trailing_characters():
    assert sanitize_segment("Album.") == "Album"
    assert sanitize_segment("Album ") == "Album"


def test_sanitize_segment_falls_back_when_nothing_survives():
    assert sanitize_segment("") == "_"
    assert sanitize_segment("...") == "_"
    assert sanitize_segment("   ") == "_"


def test_build_album_destination_appends_the_peer_name():
    assert build_album_destination("Greatest Hits", "bob") == "Greatest Hits [bob]"


def test_build_album_destination_sanitizes_the_whole_segment():
    assert build_album_destination("AC/DC Live", "bo/b") == "AC_DC Live [bo_b]"


def test_batch_destination_without_subpath_is_the_album_destination():
    assert batch_destination("Album [bob]", "") == "Album [bob]"


def test_batch_destination_appends_the_disc_subpath():
    assert batch_destination("Album [bob]", "CD1") == "Album [bob]/CD1"
