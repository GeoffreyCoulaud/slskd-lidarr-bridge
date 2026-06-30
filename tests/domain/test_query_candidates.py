"""Tests for query candidate generation."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

from slskd_lidarr_bridge.domain.models import SearchQuery
from slskd_lidarr_bridge.domain.query_candidates import (
    _strip_editions,
    generate_candidates,
)


def test_raw_candidate_is_first_and_unchanged():
    cands = generate_candidates(SearchQuery(artist="Daft Punk", album="Discovery"))
    assert cands[0] == "Daft Punk Discovery"


def test_edition_suffix_is_stripped_in_a_later_candidate():
    cands = generate_candidates(
        SearchQuery(artist="Taylor Swift", album="1989 (Deluxe Edition)")
    )
    assert cands[0] == "Taylor Swift 1989 (Deluxe Edition)"  # raw preserved
    assert "Taylor Swift 1989" in cands  # edition removed


def test_trailing_single_qualifier_is_stripped():
    cands = generate_candidates(SearchQuery(artist="Adele", album="Hello - Single"))
    assert "Adele Hello" in cands


def test_non_edition_parenthetical_preserved_in_raw_only():
    # A parenthetical with no edition keyword stays intact in the raw (and
    # edition-stripped) candidate; the folded/album-only candidates legitimately
    # transform it. Asserting the full list makes both facts unambiguous.
    cands = generate_candidates(
        SearchQuery(artist="Oasis", album="(What's the Story) Morning Glory?")
    )
    assert cands == [
        "Oasis (What's the Story) Morning Glory?",
        "Oasis What s Story Morning Glory",
        "What s Story Morning Glory",
    ]


def test_square_bracket_edition_form_is_stripped():
    # _EDITION_GROUP handles [] as well as (); exercise the bracket branch.
    cands = generate_candidates(SearchQuery(artist="A", album="Album [Remastered]"))
    assert "A Album" in cands


def test_folded_candidate_strips_diacritics():
    cands = generate_candidates(SearchQuery(artist="Björk", album="Homogenic"))
    assert "Bjork Homogenic" in cands


def test_folded_candidate_drops_ampersand_and_articles():
    cands = generate_candidates(
        SearchQuery(artist="Florence + the Machine", album="Lungs")
    )
    assert "Florence Machine Lungs" in cands


def test_album_only_candidate_excludes_artist():
    cands = generate_candidates(
        SearchQuery(artist="Various Artists", album="Trainspotting")
    )
    assert "Trainspotting" in cands
    assert cands[-1] == "Trainspotting"  # album-only is the loosest, last


def test_term_only_query_yields_raw_plus_normalized():
    cands = generate_candidates(SearchQuery(term="Björk Homogenic (Remastered)"))
    assert cands[0] == "Björk Homogenic (Remastered)"  # raw term preserved
    assert "Bjork Homogenic" in cands  # edition stripped + folded


def test_candidates_are_string_deduplicated():
    # A clean ASCII title with no edition tag collapses raw == stripped == folded.
    cands = generate_candidates(SearchQuery(artist="Pixies", album="Doolittle"))
    assert cands == ["Pixies Doolittle", "Doolittle"]


def test_blank_album_only_candidates_are_dropped():
    cands = generate_candidates(SearchQuery(artist="Metallica", album=None))
    assert cands == ["Metallica"]


def test_all_stopword_title_does_not_blow_up():
    cands = generate_candidates(SearchQuery(artist="The The", album="The"))
    assert cands  # non-empty, no crash
    assert cands[0] == "The The The"


def test_golden_examples():
    golden: dict[tuple[str, str], list[str]] = {
        ("Daft Punk", "Random Access Memories"): [
            "Daft Punk Random Access Memories",
            "Random Access Memories",
        ],
        ("Taylor Swift", "1989 (Deluxe Edition)"): [
            "Taylor Swift 1989 (Deluxe Edition)",
            "Taylor Swift 1989",
            "1989",
        ],
        ("Björk", "Homogenic"): [
            "Björk Homogenic",
            "Bjork Homogenic",
            "Homogenic",
        ],
        ("Florence + the Machine", "Lungs"): [
            "Florence + the Machine Lungs",
            "Florence Machine Lungs",
            "Lungs",
        ],
        ("Oasis", "(What's the Story) Morning Glory?"): [
            "Oasis (What's the Story) Morning Glory?",
            "Oasis What s Story Morning Glory",
            "What s Story Morning Glory",
        ],
        ("Adele", "25"): ["Adele 25", "25"],
    }
    for (artist, album), expected in golden.items():
        assert generate_candidates(SearchQuery(artist=artist, album=album)) == expected


# ---------------------------------------------------------------------------
# Corpus invariant tests (real MusicBrainz data)
# ---------------------------------------------------------------------------

_CORPUS_DIR = Path(__file__).parent.parent / "fixtures" / "corpus"


def _load(name: str) -> list[dict[str, str]]:
    path = _CORPUS_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _load_all() -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for path in sorted(_CORPUS_DIR.glob("*.json")):
        entries.extend(json.loads(path.read_text(encoding="utf-8")))
    return entries


_ALL = _load_all()
_EDITION = _load("edition")
_DIACRITICS = _load("diacritics")
_COMPILATION = _load("compilation")


def _has_combining(text: str) -> bool:
    return any(unicodedata.combining(c) for c in unicodedata.normalize("NFKD", text))


@pytest.mark.parametrize(
    "entry", _ALL, ids=[f"{e['artist']} - {e['album']}" for e in _ALL]
)
def test_generic_invariants(entry: dict[str, str]) -> None:
    cands = generate_candidates(
        SearchQuery(artist=entry["artist"], album=entry["album"])
    )
    assert cands, "expected at least one candidate"
    assert 1 <= len(cands) <= 4
    assert cands[0] == f"{entry['artist']} {entry['album']}".strip()
    assert len(cands) == len(set(cands)), "candidates must be deduplicated"
    assert all(c.strip() for c in cands), "no blank candidates"


@pytest.mark.parametrize("entry", _EDITION, ids=[e["album"] for e in _EDITION])
def test_edition_strips_to_canonical(entry: dict[str, str]) -> None:
    # Canonical ground truth from MusicBrainz (release-group title), independent
    # of our functions: stripping the edition must recover it.
    assert _strip_editions(entry["album"]) == entry["canonical"]


@pytest.mark.parametrize(
    "entry", _DIACRITICS, ids=[f"{e['artist']} - {e['album']}" for e in _DIACRITICS]
)
def test_diacritics_folds(entry: dict[str, str]) -> None:
    # The canonical title keeps accents; _fold deliberately removes them, so
    # there is no canonical target — assert the output property.
    cands = generate_candidates(
        SearchQuery(artist=entry["artist"], album=entry["album"])
    )
    assert any(not _has_combining(c) and c != cands[0] for c in cands)


@pytest.mark.parametrize("entry", _COMPILATION, ids=[e["album"] for e in _COMPILATION])
def test_compilation_drops_artist(entry: dict[str, str]) -> None:
    cands = generate_candidates(
        SearchQuery(artist=entry["artist"], album=entry["album"])
    )
    assert entry["artist"].casefold() not in cands[-1].casefold()
