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


def build_title(artist: str, album: str, quality: str, uploader: str = "") -> str:
    """Build a Lidarr-compatible scene-style release title.

    Format: ``"Artist - Album [QUALITY]-uploader"`` — the quality brackets are
    omitted when *quality* is empty, and the ``-uploader`` suffix when *uploader*
    is empty. Path-hostile characters (``\\/:*?"<>|``) in every field are
    replaced with a space and consecutive spaces collapsed.

    The uploader is appended scene-style (in the release-group position) so that
    otherwise-identical releases from different Soulseek peers are
    distinguishable: a music search derives artist/album from the query, so
    without this every result of one search would share an identical title.
    """
    safe_artist = _sanitize(artist)
    safe_album = _sanitize(album)
    base = f"{safe_artist} - {safe_album}"
    if quality:
        base = f"{base} [{quality}]"
    safe_uploader = _sanitize(uploader)
    if safe_uploader:
        base = f"{base}-{safe_uploader}"
    return base
