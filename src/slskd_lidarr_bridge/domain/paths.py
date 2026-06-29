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

    Note — multi-disc albums (known limitation, not yet fixable):
        slskd through the current stable release (``0.25.1``, Apr 2026)
        hardcodes download placement to
        ``<downloads>/<immediate parent folder>/<basename>`` with no
        configuration knob.  A multi-disc album (``…/Album/CD1/x``,
        ``…/Album/CD2/y``) is written to separate sibling folders
        (``/downloads/CD1``, ``/downloads/CD2``) with no shared album root
        on disk, so this function — and the bridge — cannot point Lidarr at
        a single folder holding every disc.  This is a slskd-layout
        constraint, not a path-computation bug: a Remote Path Mapping does
        not help either, since no folder contains all discs.

        Fixing it requires slskd to let the *caller* choose the download
        destination.  That capability exists on slskd ``master`` but is
        unreleased as of ``0.25.1``: the batch download endpoint accepts
        ``Options.Destination`` — a path relative to the downloads dir that
        takes precedence over slskd's global subdirectory pattern.  Planned
        approach once it ships:

        * enqueue via the batch endpoint instead of
          ``POST /api/v0/transfers/downloads/{username}`` (see
          ``SlskdGateway.enqueue``);
        * issue one batch per disc with destination ``<album>/<disc>`` so
          each disc keeps its own folder under a common album root (a single
          flat destination would collide same-named tracks across discs);
        * report that album root as the storage path.

        This removes the per-disc problem with no user-side slskd config.
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
