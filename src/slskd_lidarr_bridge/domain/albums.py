"""Grouping remote Soulseek directory layouts into albums and discs.

slskd reports a flat list of files per peer, each with its full remote path.
Multi-disc albums appear as sibling subfolders (``Album/CD1``, ``Album/CD2``)
that belong to one release; this module recognises them and merges them under
their shared album root.
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from slskd_lidarr_bridge.domain.models import AudioFile, Disc
from slskd_lidarr_bridge.domain.paths import sanitize_segment

_DISC_WORDS = "one|two|three|four|five|six|seven|eight|nine|ten"

# A disc folder is a token followed by a quantifier: digits ("CD1"), a single
# letter ("Side A") or a spelled-out number ("Disc Two"). Requiring the
# quantifier is what keeps "Disco", "Discovery", "CDs" and "CD Audio" out, and
# the trailing \b keeps three-digit runs ("CD100") out.
#
# "vol"/"volume" are deliberately absent: compilation series are organised by
# volume ("Cafe del Mar/Volumen 1", "Volumen 2"), where the parent is a series
# name and each child is a distinct album. Merging those would fabricate a
# release claiming to hold every volume.
_DISC_FOLDER_RE = re.compile(
    rf"^[\[(]?\s*(?:cd|disc|disk|part|side)[\s._\-]*"
    rf"(?:\d{{1,2}}|[a-e]|{_DISC_WORDS})\b",
    re.IGNORECASE,
)

# Soulseek Qt hides a peer's real share root behind an "@@" + hash prefix.
# It is never an album, so a disc folder sitting directly under one is not
# promoted (its "album" would be the share root itself).
_SHARE_ROOT_PREFIX = "@@"


def is_disc_folder(name: str) -> bool:
    """True if *name* looks like a disc subfolder of an album."""
    return _DISC_FOLDER_RE.match(name) is not None


@dataclass(frozen=True)
class AlbumGroup:
    """The files of one album from one peer, split into discs."""

    album_path: str  # full remote directory of the album root, "/"-separated
    discs: tuple[Disc, ...]  # ordered by subpath

    @property
    def name(self) -> str:
        """The album root's own folder name."""
        return posixpath.basename(self.album_path)

    @property
    def parent_name(self) -> str:
        """The folder above the album root, usually the artist.

        Empty when there is no folder above it, or when that folder is the
        peer's share root.
        """
        parent = posixpath.basename(posixpath.dirname(self.album_path))
        if parent.startswith(_SHARE_ROOT_PREFIX):
            return ""
        return parent

    @property
    def files(self) -> tuple[AudioFile, ...]:
        """Every file of the album, discs flattened in order."""
        return tuple(f for disc in self.discs for f in disc.files)


def _directory(file: AudioFile) -> str:
    """The remote directory holding *file*, normalized to forward slashes."""
    return posixpath.dirname(file.filename.replace("\\", "/"))


def _album_root(directory: str) -> str:
    """The album root for *directory*: its parent if it is a disc folder."""
    parent = posixpath.dirname(directory)
    if not parent:
        return directory
    if posixpath.basename(parent).startswith(_SHARE_ROOT_PREFIX):
        return directory
    if not is_disc_folder(posixpath.basename(directory)):
        return directory
    return parent


def _subpath(root: str, directory: str) -> str:
    """The sanitized, album-root-relative folder for *directory*."""
    if directory == root:
        return ""
    relative = directory[len(root) :].strip("/")
    return "/".join(sanitize_segment(s) for s in relative.split("/"))


def group_files_by_album(files: Iterable[AudioFile]) -> list[AlbumGroup]:
    """Group one peer's files into albums, merging disc subfolders.

    Grouping is keyed on the full remote directory path, not just its name, so
    two albums of one peer whose folders happen to share a name stay apart.
    Ordering is deterministic: groups by album path, discs by subpath, files by
    filename.
    """
    by_root: defaultdict[str, defaultdict[str, list[AudioFile]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for file in files:
        directory = _directory(file)
        root = _album_root(directory)
        by_root[root][_subpath(root, directory)].append(file)

    return [
        AlbumGroup(
            album_path=root,
            discs=tuple(
                Disc(
                    subpath=subpath,
                    files=tuple(sorted(by_subpath[subpath], key=lambda f: f.filename)),
                )
                for subpath in sorted(by_subpath)
            ),
        )
        for root, by_subpath in sorted(by_root.items())
    ]
