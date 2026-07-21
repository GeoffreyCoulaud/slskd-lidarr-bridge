"""Tests for disc detection and album grouping."""

from __future__ import annotations

import pytest

from slskd_lidarr_bridge.domain.albums import (
    AlbumGroup,
    group_files_by_album,
    is_disc_folder,
)
from slskd_lidarr_bridge.domain.models import AudioFile, Disc


def _f(path: str, size: int = 10) -> AudioFile:
    return AudioFile(filename=path, size=size, extension=".flac")


@pytest.mark.parametrize(
    "name",
    [
        "CD1",
        "CD 1",
        "cd_2",
        "CD-03",
        "Disc 2",
        "disk3",
        "Disc Two",
        "[CD2]",
        "(cd 2)",
        "CD1 - Bonus",
        "Side A",
        "Part 1",
    ],
)
def test_is_disc_folder_accepts_disc_layouts(name) -> None:
    assert is_disc_folder(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "Disco",
        "Discovery",
        "CDs",
        "CD Audio",
        "Part Of The Album",
        "Vol 1",
        "Volume 2",
        "CD100",
        "Album",
        "",
    ],
)
def test_is_disc_folder_rejects_everything_else(name) -> None:
    assert is_disc_folder(name) is False


def test_single_directory_yields_one_group_with_one_unnamed_disc() -> None:
    files = [_f(r"@@ab\Artist\Album\01.flac"), _f(r"@@ab\Artist\Album\02.flac")]
    groups = group_files_by_album(files)
    assert groups == [
        AlbumGroup(
            album_path="@@ab/Artist/Album",
            discs=(Disc(subpath="", files=(files[0], files[1])),),
        )
    ]


def test_disc_subfolders_merge_into_one_group() -> None:
    a = _f(r"@@ab\Artist\Album\CD1\01.flac")
    b = _f(r"@@ab\Artist\Album\CD2\01.flac")
    groups = group_files_by_album([b, a])
    assert len(groups) == 1
    assert groups[0].album_path == "@@ab/Artist/Album"
    assert groups[0].discs == (
        Disc(subpath="CD1", files=(a,)),
        Disc(subpath="CD2", files=(b,)),
    )


def test_a_lone_disc_of_a_multi_disc_album_is_still_rooted_at_the_album() -> None:
    b = _f(r"@@ab\Artist\Album\CD2\01.flac")
    groups = group_files_by_album([b])
    assert groups[0].album_path == "@@ab/Artist/Album"
    assert groups[0].discs == (Disc(subpath="CD2", files=(b,)),)


def test_same_folder_name_under_different_parents_stays_separate() -> None:
    a = _f(r"@@ab\Live\Album\01.flac")
    b = _f(r"@@ab\Studio\Album\01.flac")
    groups = group_files_by_album([a, b])
    assert [g.album_path for g in groups] == [
        "@@ab/Live/Album",
        "@@ab/Studio/Album",
    ]


def test_disc_folder_directly_under_a_share_root_is_not_promoted() -> None:
    a = _f(r"@@ab\CD1\01.flac")
    groups = group_files_by_album([a])
    assert groups[0].album_path == "@@ab/CD1"


def test_disc_folder_without_any_parent_is_not_promoted() -> None:
    a = _f(r"CD1\01.flac")
    groups = group_files_by_album([a])
    assert groups[0].album_path == "CD1"


def test_disc_subpath_is_sanitized() -> None:
    # The ":" is legal on a Unix share and hostile on Windows, so the subpath
    # stored is already the on-disk form.
    a = _f(r"@@ab\Artist\Album\CD1: Bonus\01.flac")
    groups = group_files_by_album([a])
    assert groups[0].album_path == "@@ab/Artist/Album"
    assert groups[0].discs == (Disc(subpath="CD1_ Bonus", files=(a,)),)


def test_group_name_and_parent_name() -> None:
    group = AlbumGroup(album_path="@@ab/Artist/Album", discs=())
    assert group.name == "Album"
    assert group.parent_name == "Artist"


def test_parent_name_is_empty_at_a_share_root_or_without_a_parent() -> None:
    assert AlbumGroup(album_path="@@ab/Album", discs=()).parent_name == ""
    assert AlbumGroup(album_path="Album", discs=()).parent_name == ""


def test_group_files_flattens_every_disc() -> None:
    a = _f(r"@@ab\Artist\Album\CD1\01.flac")
    b = _f(r"@@ab\Artist\Album\CD2\01.flac")
    assert group_files_by_album([a, b])[0].files == (a, b)
