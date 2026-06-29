"""Storage path computation for completed slskd downloads."""

from __future__ import annotations

import posixpath
from pathlib import PurePosixPath


def compute_storage_path(downloads_dir: str, remote_filename: str) -> str:
    """Return the local folder where slskd stores a completed download.

    slskd layout assumption: slskd preserves the remote album folder (the
    immediate parent directory of the remote file) directly under its
    configured ``directories.downloads`` path — no username nesting.

    Args:
        downloads_dir: Local path to slskd's downloads directory.
        remote_filename: Full remote path as reported by slskd (may use
            backslashes, forward slashes, or a mix).

    Returns:
        Absolute local path to the album folder, e.g.
        ``"/downloads/Album Name"``.

    Note — multi-disc albums:
        Storage is derived from the file's *immediate* parent folder.
        Multi-disc albums laid out as ``…/Album/CD1/track.flac`` will
        report ``CD1`` (per-disc subfolder) rather than the album root —
        this is the folder slskd actually wrote to, which is acceptable
        for single-disc sets.  Lidarr import of multi-disc releases may
        need the album root; revisit this logic if that becomes an issue.
    """
    # Normalize Windows-style separators so PurePosixPath handles both.
    normalized_remote = remote_filename.replace("\\", "/")
    album_folder = PurePosixPath(normalized_remote).parent.name
    if not album_folder:
        raise ValueError(f"remote_filename has no album folder: {remote_filename!r}")
    # Strip any trailing slash; guard against "/" → "" which would produce a
    # relative path.  If stripping empties the string, treat base as "/".
    base = downloads_dir.rstrip("/") or "/"
    return posixpath.join(base, album_folder)
