"""Scene-style release title builder."""

from __future__ import annotations

import re

# Characters that are illegal or hostile in filesystem paths and release names.
_PATH_HOSTILE = re.compile(r'[\\/:*?"<>|]')
_MULTI_SPACE = re.compile(r" {2,}")


def _sanitize(text: str) -> str:
    """Replace path-hostile chars with spaces; collapse and strip whitespace."""
    text = _PATH_HOSTILE.sub(" ", text)
    text = _MULTI_SPACE.sub(" ", text)
    return text.strip()


def build_title(artist: str, album: str, quality: str) -> str:
    """Build a Lidarr-compatible scene-style release title.

    Format: ``"Artist - Album [QUALITY]"`` (brackets omitted if quality is empty).
    Path-hostile characters (``\\/:*?"<>|``) in artist/album are replaced with
    a space; consecutive spaces are collapsed.
    """
    safe_artist = _sanitize(artist)
    safe_album = _sanitize(album)
    base = f"{safe_artist} - {safe_album}"
    if quality:
        return f"{base} [{quality}]"
    return base
