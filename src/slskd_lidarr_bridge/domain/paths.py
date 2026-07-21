"""Storage path computation for completed slskd downloads."""

from __future__ import annotations

import posixpath


def compute_storage_path(downloads_dir: str, destination: str) -> str:
    """Return the local folder holding a completed download.

    ``destination`` is what the bridge told slskd to use when enqueueing the
    batches (see ``build_album_destination``), so this is an exact path rather
    than an inference about slskd's layout: slskd gives a batch destination
    precedence over its own ``transfers.download.destination`` pattern, and the
    segments were sanitized to survive slskd's own sanitization unchanged.
    """
    # Strip any trailing slash; guard against "/" → "" which would produce a
    # relative path.  If stripping empties the string, treat base as "/".
    base = downloads_dir.rstrip("/") or "/"
    return posixpath.join(base, destination)


# Characters that are invalid in a path segment on *any* supported host.
# The union of Windows' and Unix's invalid sets is deliberate: slskd sanitizes
# with its own host's set, so normalizing against the strictest one here makes
# slskd's sanitization a no-op and keeps the path we report to Lidarr exactly
# equal to the path on disk, whatever slskd runs on.
_INVALID_SEGMENT_CHARS = frozenset('"<>|:*?\\/') | {chr(c) for c in range(32)}


def sanitize_segment(segment: str) -> str:
    """Return *segment* made safe for use as a single path component.

    Invalid characters become ``_``; trailing dots and spaces are dropped
    (Windows silently truncates them, which would desynchronize the path we
    report from the path on disk); a segment left empty falls back to ``_``.

    Stripping trailing dots also disposes of the ``.`` and ``..`` traversal
    segments that slskd's ``NonTraversingPath`` validator rejects.
    """
    cleaned = "".join("_" if c in _INVALID_SEGMENT_CHARS else c for c in segment)
    cleaned = cleaned.rstrip(". ")
    return cleaned or "_"


def build_album_destination(album_name: str, username: str) -> str:
    """Return the album's download folder, relative to slskd's downloads dir.

    The peer name is appended so that two peers offering identically named
    album folders cannot write into the same directory.
    """
    return sanitize_segment(f"{album_name} [{username}]")


def batch_destination(destination: str, subpath: str) -> str:
    """Return the destination for one disc: the album folder, plus its subpath."""
    if not subpath:
        return destination
    return posixpath.join(destination, subpath)
