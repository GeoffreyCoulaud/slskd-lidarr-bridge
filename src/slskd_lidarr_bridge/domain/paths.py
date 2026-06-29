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
    """
    # Normalize Windows-style separators so PurePosixPath handles both.
    normalized_remote = remote_filename.replace("\\", "/")
    album_folder = PurePosixPath(normalized_remote).parent.name
    # Strip any trailing slash from downloads_dir before joining.
    base = downloads_dir.rstrip("/")
    return posixpath.join(base, album_folder)
