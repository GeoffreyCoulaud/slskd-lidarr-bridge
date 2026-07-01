"""Generate ordered, deduplicated slskd query candidates from a SearchQuery.

Pure string logic — no I/O. Lidarr gives the bridge only artist+album (or a raw
term) over Newznab, so these candidates transform exactly those strings. See
docs/specs/2026-06-30-search-normalization-fallback-design.md.
"""

from __future__ import annotations

import re
import unicodedata

from slskd_lidarr_bridge.domain.models import SearchQuery

_MULTISPACE = re.compile(r"\s+")

_EDITION_KEYWORDS = (
    "deluxe",
    "remaster",
    "expanded",
    "anniversary",
    "bonus",
    "edition",
    "version",
    "explicit",
    "clean",
    "mono",
    "stereo",
    "reissue",
    "special",
    "collector",
    "limited",
    "remix",
    "instrumental",
)
# Keywords match as substrings (no \b anchors) on purpose: "remaster" must also
# catch "Remastered"/"Remasters". Trade-off: a contrived "(Remasterable)" would
# match too — acceptable, as such titles do not occur in real metadata.
_EDITION_GROUP = re.compile(
    r"\s*[(\[][^)\]]*(?:" + "|".join(_EDITION_KEYWORDS) + r")[^)\]]*[)\]]",
    re.IGNORECASE,
)
_TRAILING_QUALIFIER = re.compile(r"\s*-\s*(?:single|ep)\s*$", re.IGNORECASE)

_FILLER = frozenset({"the", "a", "an", "feat", "ft", "featuring", "with", "vs"})
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def _strip_editions(album: str) -> str:
    out = _EDITION_GROUP.sub("", album)
    out = _TRAILING_QUALIFIER.sub("", out)
    return _MULTISPACE.sub(" ", out).strip()


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    no_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    no_amp = no_marks.replace("&", " ")
    no_punct = _PUNCT.sub(" ", no_amp)
    tokens = [t for t in no_punct.split() if t.lower() not in _FILLER]
    return " ".join(tokens)


def generate_candidates(query: SearchQuery) -> list[str]:
    # Build in precision order (raw first, loosest last). A plain list — not a
    # set — because ordering is contractual: the raw candidate must always run
    # first as the primary. The final loop below deduplicates while preserving
    # this order, so a set here would only trade the ordering guarantee for
    # nothing.
    candidates = [query.to_search_text()]
    if query.term is None:
        artist = query.artist or ""
        album = query.album or ""
        stripped = _strip_editions(album)
        candidates.append(f"{artist} {stripped}".strip())
        candidates.append(f"{_fold(artist)} {_fold(stripped)}".strip())
        candidates.append(_fold(stripped))
    else:
        candidates.append(_fold(_strip_editions(query.term)))

    seen: set[str] = set()
    result: list[str] = []
    for c in candidates:
        c = _MULTISPACE.sub(" ", c).strip()
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    return result
