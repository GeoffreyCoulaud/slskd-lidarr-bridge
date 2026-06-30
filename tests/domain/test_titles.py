"""Tests for domain.titles.build_title."""

from slskd_lidarr_bridge.domain.titles import build_title


def test_basic_title_with_quality() -> None:
    assert (
        build_title("Radiohead", "In Rainbows", "FLAC")
        == "Radiohead - In Rainbows [FLAC]"
    )


def test_path_hostile_chars_stripped() -> None:
    # "/" in artist, ":" in album — both path-hostile, must be replaced with space
    result = build_title("A/B", "C: D", "MP3-320")
    assert result == "A B - C D [MP3-320]"


def test_empty_quality_no_brackets() -> None:
    assert build_title("A", "B", "") == "A - B"


def test_collapse_multiple_spaces() -> None:
    # Double slash would leave two spaces; they must collapse to one
    result = build_title("A//B", "Album", "FLAC")
    assert result == "A B - Album [FLAC]"


def test_no_leading_trailing_spaces_in_artist_album() -> None:
    # Leading/trailing path-hostile char → no leading/trailing spaces
    result = build_title("/Artist", "Album/", "FLAC")
    assert result == "Artist - Album [FLAC]"


def test_all_path_hostile_chars_replaced() -> None:
    # Chars: \ / : * ? " < > |
    result = build_title('A\\B:C*D?E"F<G>H|I', "Album", "FLAC")
    assert result == "A B C D E F G H I - Album [FLAC]"


def test_uploader_appended_as_release_group() -> None:
    # The Soulseek uploader is appended scene-style ("-group") so that
    # otherwise-identical releases from different uploaders are distinguishable.
    assert (
        build_title("Radiohead", "In Rainbows", "FLAC", "alice")
        == "Radiohead - In Rainbows [FLAC]-alice"
    )


def test_uploader_appended_without_quality() -> None:
    assert build_title("A", "B", "", "bob") == "A - B-bob"


def test_uploader_sanitized() -> None:
    # Path-hostile chars in the uploader are replaced and collapsed too.
    assert build_title("A", "B", "FLAC", "dj/cool") == "A - B [FLAC]-dj cool"


def test_empty_uploader_no_suffix() -> None:
    assert build_title("A", "B", "FLAC", "") == "A - B [FLAC]"
