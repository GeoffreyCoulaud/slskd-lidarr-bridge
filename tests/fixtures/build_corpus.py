"""Build tests/fixtures/real_albums.json from the MusicBrainz API.

Run once:
    uv run python tests/fixtures/build_corpus.py

Requires internet access.  All metadata is CC0 (MusicBrainz).
"""

from __future__ import annotations

import json
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_UA = (
    "slskd-lidarr-bridge-corpus/1.0"
    " ( https://github.com/GeoffreyCoulaud/slskd-lidarr-bridge )"
)
_SLEEP = 1.1
_CAP = 80

_FIXTURES_DIR = Path(__file__).parent
_CORPUS_DIR = _FIXTURES_DIR / "corpus"


def _tag_dimension(artist: str, album: str) -> str:
    """Objective feature-based dimension tag — no production-code imports."""
    if artist.casefold() in {"various artists", "va"}:
        return "compilation"
    if artist.casefold() == album.casefold():
        return "self_titled"
    combined = artist + album
    if any(unicodedata.combining(c) for c in unicodedata.normalize("NFKD", combined)):
        return "diacritics"
    if "&" in artist or "&" in album:
        return "ampersand"
    if any(c.isalpha() and ord(c) > 0x024F for c in combined):
        return "non_latin"
    if len(album.split()) > 6:
        return "long_title"
    return "clean"


def _mb_get(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


_EDITION_CAP = 25


def _fetch_edition_slice() -> list[dict[str, str]]:
    """Fetch releases with a known disambiguation phrase.

    For each release:
    - canonical = release["release-group"]["title"]  (independent MB ground truth)
    - album = f"{canonical} ({disambiguation})"      (Soulseek folder form)

    Hygiene skips:
    - canonical is empty/missing
    - disambiguation contains (, ), [, ] (malformed nested-paren reconstruction)

    dimension is pre-set to "edition" here; _tag_dimension is not called for
    these entries.
    """
    phrases = [
        "deluxe edition",
        "remastered",
        "expanded edition",
        "anniversary edition",
        "special edition",
    ]
    entries: list[dict[str, str]] = []
    for phrase in phrases:
        raw_q = f'comment:"{phrase}" AND status:official'
        url = (
            "https://musicbrainz.org/ws/2/release"
            f"?query={urllib.parse.quote(raw_q)}&fmt=json&limit=10"
        )
        print(f"  edition query: {phrase!r}")
        try:
            data = _mb_get(url)
        except Exception as exc:
            print(f"    WARN: {exc}", flush=True)
            time.sleep(_SLEEP)
            continue
        for release in data.get("releases", []):
            disambig: str = release.get("disambiguation", "").strip()
            credits = release.get("artist-credit", [])
            if not disambig or not credits:
                continue
            # Hygiene: skip if disambiguation contains parentheses or brackets
            if any(ch in disambig for ch in ("(", ")", "[", "]")):
                continue
            artist: str = credits[0].get("name", "").strip()
            if not artist:
                continue
            rg = release.get("release-group", {})
            canonical: str = rg.get("title", "").strip()
            if not canonical:
                continue
            album = f"{canonical} ({disambig})"
            mbid: str = release.get("id", "")
            entries.append(
                {
                    "artist": artist,
                    "album": album,
                    "canonical": canonical,
                    "mbid": mbid,
                    "dimension": "edition",
                }
            )
        time.sleep(_SLEEP)
    # Dedup within the edition slice, then cap at EDITION_CAP
    return _dedup(entries)[:_EDITION_CAP]


def _fetch_representative_slice() -> list[dict[str, str]]:
    queries = [
        "primarytype:album AND tag:rock",
        "primarytype:album AND tag:pop",
        'primarytype:album AND tag:"hip hop"',
        "primarytype:album AND tag:electronic",
        "primarytype:album AND tag:jazz",
        "primarytype:album AND tag:metal",
        'primarytype:album AND artist:"Various Artists"',
        'primarytype:album AND tag:"j-pop"',
        'primarytype:album AND tag:"k-pop"',
        "primarytype:album AND tag:chanson",
        'primarytype:album AND tag:"música popular brasileira"',
    ]
    entries: list[dict[str, str]] = []
    for q in queries:
        url = (
            "https://musicbrainz.org/ws/2/release-group"
            f"?query={urllib.parse.quote(q)}&fmt=json&limit=15"
        )
        print(f"  representative query: {q!r}")
        try:
            data = _mb_get(url)
        except Exception as exc:
            print(f"    WARN: {exc}", flush=True)
            time.sleep(_SLEEP)
            continue
        groups = data.get("release-groups", [])
        if not groups:
            print(f"    WARN: no results for {q!r}", file=sys.stderr)
            time.sleep(_SLEEP)
            continue
        for rg in groups:
            album: str = rg.get("title", "").strip()
            credits = rg.get("artist-credit", [])
            if not album or not credits:
                continue
            artist: str = credits[0].get("name", "").strip()
            if not artist:
                continue
            mbid: str = rg.get("id", "")
            # canonical is not applicable for representative slice entries
            entries.append(
                {"artist": artist, "album": album, "canonical": "", "mbid": mbid}
            )
        time.sleep(_SLEEP)
    return entries


def _dedup(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for e in entries:
        key = (e["artist"].casefold(), e["album"].casefold())
        if key not in seen:
            seen.add(key)
            result.append(e)
    return result


def _assemble() -> list[dict[str, str]]:
    print("Fetching edition slice …")
    edition = _fetch_edition_slice()
    print(f"  → {len(edition)} entries after edition dedup+cap")

    print("Fetching representative slice …")
    representative = _fetch_representative_slice()
    print(f"  → {len(representative)} raw entries")

    all_entries = edition + representative
    deduped = _dedup(all_entries)
    capped = deduped[:_CAP]

    print(f"After dedup+cap: {len(capped)} entries")

    tagged: list[dict[str, str]] = []
    for e in capped:
        # Edition entries are pre-tagged; representative entries need tagging.
        dim = e.get("dimension") or _tag_dimension(e["artist"], e["album"])
        tagged.append(
            {
                "artist": e["artist"],
                "album": e["album"],
                "canonical": e.get("canonical", ""),
                "mbid": e["mbid"],
                "dimension": dim,
            }
        )

    tagged.sort(key=lambda x: (x["dimension"], x["artist"], x["album"]))
    return tagged


if __name__ == "__main__":
    from collections import defaultdict

    corpus = _assemble()

    # Group by dimension
    by_dim: dict[str, list[dict[str, str]]] = defaultdict(list)
    for entry in corpus:
        by_dim[entry["dimension"]].append(entry)

    # Print dimension counts
    print("\nDimension counts:")
    for dim in sorted(by_dim):
        print(f"  {dim}: {len(by_dim[dim])}")
    print(f"Total: {len(corpus)}")

    # Write one file per dimension (skip empty dimensions)
    _CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    for dim, entries in sorted(by_dim.items()):
        if not entries:
            continue
        shaped = []
        for e in entries:
            r: dict[str, str] = {
                "artist": e["artist"],
                "album": e["album"],
                "mbid": e["mbid"],
            }
            if dim == "edition":
                r["canonical"] = e.get("canonical", "")
            shaped.append(r)
        out_path = _CORPUS_DIR / f"{dim}.json"
        out_path.write_text(
            json.dumps(shaped, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {out_path} ({len(shaped)} entries)")
