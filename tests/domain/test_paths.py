"""Tests for domain.paths.compute_storage_path."""

from slskd_lidarr_bridge.domain.paths import (
    batch_destination,
    build_album_destination,
    compute_storage_path,
    sanitize_segment,
)


def test_compute_storage_path_joins_the_destination():
    assert compute_storage_path("/downloads", "Album [bob]") == "/downloads/Album [bob]"


def test_compute_storage_path_tolerates_a_trailing_slash():
    assert compute_storage_path("/downloads/", "Album") == "/downloads/Album"


def test_compute_storage_path_tolerates_a_root_downloads_dir():
    assert compute_storage_path("/", "Album") == "/Album"


def test_compute_storage_path_strips_a_leading_slash_from_destination():
    # posixpath.join treats a leading "/" as absolute and silently drops
    # downloads_dir; destination is read straight from the NZB payload in
    # start(), one hop removed from build_album_destination's guarantee that
    # it never has one, so this must be defended against locally too.
    assert compute_storage_path("/downloads", "/Album") == "/downloads/Album"


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
