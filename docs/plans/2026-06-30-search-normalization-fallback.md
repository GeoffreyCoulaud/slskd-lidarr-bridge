# Search normalization + multi-tier fallback — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise search hit-rate by trying an ordered list of progressively looser query candidates against slskd, in series, until enough results accumulate or a wall-clock budget elapses.

**Architecture:** A new pure module `domain/query_candidates.py` turns a `SearchQuery` into an ordered, deduplicated list of query strings (raw → edition-stripped → folded → album-only). `SearchService.search()` loops over them serially, gating on cumulative distinct results and a wall-clock budget; the primary candidate always runs. Ranking, dedup-by-folder, and Lidarr's title re-parse stay as they are.

**Tech Stack:** Python 3.12+, stdlib `re` + `unicodedata`, pytest + respx, ruff, strict mypy. Dependency-free (no new packages).

Design spec: `docs/specs/2026-06-30-search-normalization-fallback-design.md`.

## Global Constraints

- Python 3.12+; `from __future__ import annotations` at the top of every module.
- Strict mypy: production code fully typed; no `# type: ignore` to dodge real errors.
- Ruff (`E, F, I, B, UP`), line length 88; imports sorted by ruff.
- **100% line + branch coverage** (`fail_under = 100`, `branch = true`). Every branch needs a test.
- Domain depends only on ports/models — no Flask/SQLite/httpx imports in `domain/`.
- All env parsing lives in `config.py`; new env vars also go in the README table.
- Tests use fakes for ports; **no network in pytest** (the corpus is a committed fixture).
- Run the full gate before considering any task done: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`.
- Work on the current branch `feat/search-normalization-fallback`; commit per task.

## File Structure

- **Create** `src/slskd_lidarr_bridge/domain/query_candidates.py` — pure candidate generation (`generate_candidates`, `_strip_editions`, `_fold`).
- **Create** `tests/domain/test_query_candidates.py` — synthetic + golden unit tests, then corpus invariants.
- **Create** `tests/fixtures/real_albums.json` — committed MusicBrainz-sourced corpus (~80 balanced entries).
- **Modify** `src/slskd_lidarr_bridge/config.py` — add `min_results`, `search_budget`.
- **Modify** `src/slskd_lidarr_bridge/domain/search_service.py` — candidate loop + `_run_search` / `_collect` helpers + new ctor params.
- **Modify** `src/slskd_lidarr_bridge/adapters/inbound/app.py` — wire the two config values into `SearchService`.
- **Modify** `tests/domain/test_search_service.py` — upgrade `FakeGateway`, migrate 3 single-search tests, add orchestration tests.
- **Modify** `tests/test_config.py` — assert new vars (defaults + parsing).
- **Modify** `tests/adapters/inbound/test_app.py` — add the two fields to `_make_config` defaults.
- **Modify** `README.md` — document `BRIDGE_MIN_RESULTS`, `BRIDGE_SEARCH_BUDGET`.

---

### Task 1: Pure candidate generation module

**Files:**
- Create: `src/slskd_lidarr_bridge/domain/query_candidates.py`
- Test: `tests/domain/test_query_candidates.py`

**Interfaces:**
- Consumes: `SearchQuery` from `domain/models.py` (fields `artist: str | None`, `album: str | None`, `term: str | None`; method `to_search_text() -> str`).
- Produces: `generate_candidates(query: SearchQuery) -> list[str]` — ordered, string-deduplicated, no blanks. Later consumed by `SearchService` (Task 4).

- [ ] **Step 1: Write the failing test — raw candidate is first**

Create `tests/domain/test_query_candidates.py`:

```python
"""Tests for query candidate generation."""

from __future__ import annotations

import unicodedata

from slskd_lidarr_bridge.domain.models import SearchQuery
from slskd_lidarr_bridge.domain.query_candidates import generate_candidates


def test_raw_candidate_is_first_and_unchanged():
    cands = generate_candidates(SearchQuery(artist="Daft Punk", album="Discovery"))
    assert cands[0] == "Daft Punk Discovery"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/domain/test_query_candidates.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: ...query_candidates`.

- [ ] **Step 3: Write the minimal module**

Create `src/slskd_lidarr_bridge/domain/query_candidates.py`:

```python
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


def generate_candidates(query: SearchQuery) -> list[str]:
    candidates = [query.to_search_text()]
    seen: set[str] = set()
    result: list[str] = []
    for c in candidates:
        c = _MULTISPACE.sub(" ", c).strip()
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    return result
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/domain/test_query_candidates.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Write failing tests — edition stripping**

Append to the test file:

```python
def test_edition_suffix_is_stripped_in_a_later_candidate():
    cands = generate_candidates(
        SearchQuery(artist="Taylor Swift", album="1989 (Deluxe Edition)")
    )
    assert cands[0] == "Taylor Swift 1989 (Deluxe Edition)"  # raw preserved
    assert "Taylor Swift 1989" in cands  # edition removed


def test_trailing_single_qualifier_is_stripped():
    cands = generate_candidates(SearchQuery(artist="Adele", album="Hello - Single"))
    assert "Adele Hello" in cands


def test_non_edition_parenthetical_is_preserved_in_every_candidate():
    cands = generate_candidates(
        SearchQuery(artist="Oasis", album="(What's the Story) Morning Glory?")
    )
    # The raw candidate keeps the parenthetical (it is part of the real title).
    assert cands[0] == "Oasis (What's the Story) Morning Glory?"
```

- [ ] **Step 6: Run to verify failure**

Run: `uv run pytest tests/domain/test_query_candidates.py -v --no-cov`
Expected: FAIL — `test_edition_suffix_is_stripped...` and `test_trailing_single...` (only one candidate so far).

- [ ] **Step 7: Implement `_strip_editions` and add the edition-stripped candidate**

Edit `query_candidates.py` — add the constants after `_MULTISPACE`, the helper, and extend `generate_candidates`:

```python
_EDITION_KEYWORDS = (
    "deluxe", "remaster", "expanded", "anniversary", "bonus", "edition",
    "version", "explicit", "clean", "mono", "stereo", "reissue", "special",
    "collector", "limited", "remix", "instrumental",
)
_EDITION_GROUP = re.compile(
    r"\s*[(\[][^)\]]*(?:" + "|".join(_EDITION_KEYWORDS) + r")[^)\]]*[)\]]",
    re.IGNORECASE,
)
_TRAILING_QUALIFIER = re.compile(r"\s*-\s*(?:single|ep)\s*$", re.IGNORECASE)


def _strip_editions(album: str) -> str:
    out = _EDITION_GROUP.sub("", album)
    out = _TRAILING_QUALIFIER.sub("", out)
    return _MULTISPACE.sub(" ", out).strip()
```

Replace the body of `generate_candidates` so it branches on term vs music and builds the edition-stripped candidate:

```python
def generate_candidates(query: SearchQuery) -> list[str]:
    candidates = [query.to_search_text()]
    if query.term is None:
        artist = query.artist or ""
        album = query.album or ""
        stripped = _strip_editions(album)
        candidates.append(f"{artist} {stripped}".strip())
    seen: set[str] = set()
    result: list[str] = []
    for c in candidates:
        c = _MULTISPACE.sub(" ", c).strip()
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    return result
```

- [ ] **Step 8: Run to verify pass**

Run: `uv run pytest tests/domain/test_query_candidates.py -v --no-cov`
Expected: PASS (all 5).

- [ ] **Step 9: Write failing tests — folding (diacritics, `&`/`+`, punctuation, fillers)**

Append:

```python
def test_folded_candidate_strips_diacritics():
    cands = generate_candidates(SearchQuery(artist="Björk", album="Homogenic"))
    assert "Bjork Homogenic" in cands


def test_folded_candidate_drops_ampersand_and_articles():
    cands = generate_candidates(
        SearchQuery(artist="Florence + the Machine", album="Lungs")
    )
    assert "Florence Machine Lungs" in cands


def test_album_only_candidate_excludes_artist():
    cands = generate_candidates(SearchQuery(artist="Various Artists", album="Trainspotting"))
    assert "Trainspotting" in cands
    assert cands[-1] == "Trainspotting"  # album-only is the loosest, last
```

- [ ] **Step 10: Run to verify failure**

Run: `uv run pytest tests/domain/test_query_candidates.py -v --no-cov`
Expected: FAIL — folded and album-only candidates not generated yet.

- [ ] **Step 11: Implement `_fold` and the folded + album-only candidates**

Edit `query_candidates.py` — add the fold constants and helper:

```python
_FILLER = frozenset({"the", "a", "an", "feat", "ft", "featuring", "with", "vs"})
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    no_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    no_amp = no_marks.replace("&", " ")
    no_punct = _PUNCT.sub(" ", no_amp)
    tokens = [t for t in no_punct.split() if t.lower() not in _FILLER]
    return " ".join(tokens)
```

Extend the `if query.term is None:` block (after the edition-stripped append) with the folded and album-only candidates:

```python
        candidates.append(f"{_fold(artist)} {_fold(stripped)}".strip())
        candidates.append(_fold(stripped))
```

- [ ] **Step 12: Run to verify pass**

Run: `uv run pytest tests/domain/test_query_candidates.py -v --no-cov`
Expected: PASS (all 8).

- [ ] **Step 13: Write failing tests — term-only path, dedup, and edge cases**

Append:

```python
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
```

- [ ] **Step 14: Run to verify failure**

Run: `uv run pytest tests/domain/test_query_candidates.py -v --no-cov`
Expected: FAIL — `test_term_only...` (term path adds no normalized candidate yet).

- [ ] **Step 15: Implement the term-only normalized candidate**

In `generate_candidates`, add an `else` branch to the `if query.term is None:` check:

```python
    if query.term is None:
        artist = query.artist or ""
        album = query.album or ""
        stripped = _strip_editions(album)
        candidates.append(f"{artist} {stripped}".strip())
        candidates.append(f"{_fold(artist)} {_fold(stripped)}".strip())
        candidates.append(_fold(stripped))
    else:
        candidates.append(_fold(_strip_editions(query.term)))
```

- [ ] **Step 16: Run to verify pass**

Run: `uv run pytest tests/domain/test_query_candidates.py -v --no-cov`
Expected: PASS (all 12).

- [ ] **Step 17: Add the golden-subset test**

Append (each expected list computed by hand from the rules above):

```python
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
```

- [ ] **Step 18: Run the golden test and the full module gate**

Run: `uv run pytest tests/domain/test_query_candidates.py -v --no-cov`
Expected: PASS. If any golden list mismatches, fix the *expected* value to match the documented rules (do not weaken the rules) and note it.
Then: `uv run ruff check src/slskd_lidarr_bridge/domain/query_candidates.py && uv run mypy`
Expected: clean.

- [ ] **Step 19: Commit**

```bash
git add src/slskd_lidarr_bridge/domain/query_candidates.py tests/domain/test_query_candidates.py
git commit -m "feat(search): pure query-candidate generation (normalize + fallback variants)"
```

---

### Task 2: Real-album corpus fixture + invariant tests

**Files:**
- Create: `tests/fixtures/real_albums.json`
- Modify: `tests/domain/test_query_candidates.py` (append corpus invariants)

**Interfaces:**
- Consumes: `generate_candidates` (Task 1).
- Produces: a committed fixture other tests may reuse; schema is a JSON array of `{"artist": str, "album": str, "mbid": str, "dimension": str}`.

- [ ] **Step 1: Build the corpus fixture from MusicBrainz**

Create `tests/fixtures/real_albums.json` — a JSON array of ~80 objects, **balanced**:
- ~70% tricky, spread across dimensions (target counts): `edition` ×12, `non_edition_paren` ×6, `diacritics` ×10, `ampersand` ×6, `compilation` ×8 (Various-Artists / soundtracks), `self_titled` ×6, `long_title` ×6, `punctuation` ×6.
- ~30% `clean` (popular albums, plain titles) ×20.
- ~4 `non_latin` (e.g. Japanese/Cyrillic) for fold pass-through.

Procedure (one-shot; **not** shipped as code): for each chosen artist, query the MusicBrainz web service for real release-group titles and copy the exact strings, e.g.
`https://musicbrainz.org/ws/2/release-group?query=artist:%22Björk%22&fmt=json&limit=25`
(send a descriptive `User-Agent`; respect 1 req/s). Use the real `title` and the release-group `id` as `mbid`. The album titles must be the **authentic** MusicBrainz strings (real edition suffixes), not invented. Each object's `dimension` is one of the tags above.

Seed entries to include verbatim (extend to the target counts with MusicBrainz lookups):

```json
[
  {"artist": "Taylor Swift", "album": "1989 (Deluxe Edition)", "mbid": "", "dimension": "edition"},
  {"artist": "Radiohead", "album": "OK Computer OKNOTOK 1997 2017", "mbid": "", "dimension": "edition"},
  {"artist": "Nirvana", "album": "Nevermind (Remastered)", "mbid": "", "dimension": "edition"},
  {"artist": "The Beatles", "album": "Abbey Road (Super Deluxe Edition)", "mbid": "", "dimension": "edition"},
  {"artist": "Oasis", "album": "(What's the Story) Morning Glory?", "mbid": "", "dimension": "non_edition_paren"},
  {"artist": "Alicia Keys", "album": "Songs in A Minor", "mbid": "", "dimension": "non_edition_paren"},
  {"artist": "Björk", "album": "Homogenic", "mbid": "", "dimension": "diacritics"},
  {"artist": "Sigur Rós", "album": "Ágætis byrjun", "mbid": "", "dimension": "diacritics"},
  {"artist": "Mötley Crüe", "album": "Dr. Feelgood", "mbid": "", "dimension": "diacritics"},
  {"artist": "Beyoncé", "album": "Lemonade", "mbid": "", "dimension": "diacritics"},
  {"artist": "Simon & Garfunkel", "album": "Bridge over Troubled Water", "mbid": "", "dimension": "ampersand"},
  {"artist": "Florence + the Machine", "album": "Lungs", "mbid": "", "dimension": "ampersand"},
  {"artist": "Earth, Wind & Fire", "album": "That's the Way of the World", "mbid": "", "dimension": "ampersand"},
  {"artist": "Various Artists", "album": "Trainspotting", "mbid": "", "dimension": "compilation"},
  {"artist": "Various Artists", "album": "Guardians of the Galaxy: Awesome Mix, Vol. 1", "mbid": "", "dimension": "compilation"},
  {"artist": "Metallica", "album": "Metallica", "mbid": "", "dimension": "self_titled"},
  {"artist": "Weezer", "album": "Weezer", "mbid": "", "dimension": "self_titled"},
  {"artist": "Sufjan Stevens", "album": "Illinois", "mbid": "", "dimension": "long_title"},
  {"artist": "David Bowie", "album": "The Rise and Fall of Ziggy Stardust and the Spiders from Mars", "mbid": "", "dimension": "long_title"},
  {"artist": "The Beatles", "album": "Sgt. Pepper's Lonely Hearts Club Band", "mbid": "", "dimension": "punctuation"},
  {"artist": "Fleetwood Mac", "album": "Rumours", "mbid": "", "dimension": "clean"},
  {"artist": "Pixies", "album": "Doolittle", "mbid": "", "dimension": "clean"},
  {"artist": "Daft Punk", "album": "Discovery", "mbid": "", "dimension": "clean"},
  {"artist": "宇多田ヒカル", "album": "First Love", "mbid": "", "dimension": "non_latin"}
]
```

Add a top-of-array provenance is not possible in pure JSON; instead create a sibling `tests/fixtures/real_albums.README.md` with one line: `Source: MusicBrainz (CC0). Sampled <date>. Curated for query-candidate tests.`

- [ ] **Step 2: Write the corpus invariant test**

Append to `tests/domain/test_query_candidates.py`:

```python
import json
from pathlib import Path

import pytest

_CORPUS = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "real_albums.json").read_text(
        encoding="utf-8"
    )
)


def _has_combining(text: str) -> bool:
    return any(unicodedata.combining(c) for c in unicodedata.normalize("NFKD", text))


@pytest.mark.parametrize(
    "entry", _CORPUS, ids=[f"{e['artist']} - {e['album']}" for e in _CORPUS]
)
def test_corpus_invariants(entry):
    q = SearchQuery(artist=entry["artist"], album=entry["album"])
    cands = generate_candidates(q)

    assert cands, "expected at least one candidate"
    assert 1 <= len(cands) <= 4
    assert cands[0] == f"{entry['artist']} {entry['album']}".strip()  # raw first
    assert len(cands) == len(set(cands)), "candidates must be deduplicated"
    assert all(c.strip() for c in cands), "no blank candidates"

    dim = entry["dimension"]
    if dim == "edition":
        assert any(len(c) < len(cands[0]) for c in cands[1:]), "edition not stripped"
    elif dim == "diacritics":
        assert any(not _has_combining(c) and c != cands[0] for c in cands)
    elif dim in ("compilation", "self_titled"):
        # The album-only candidate (loosest, last) drops the artist.
        assert cands[-1] == _fold_for_test(entry["album"])


def _fold_for_test(album: str) -> str:
    from slskd_lidarr_bridge.domain.query_candidates import _fold, _strip_editions

    return _fold(_strip_editions(album))
```

- [ ] **Step 3: Run the corpus test**

Run: `uv run pytest tests/domain/test_query_candidates.py -k corpus -v --no-cov`
Expected: PASS for every parametrized entry. If a dimension assertion fails for a real title, that reveals a real generator gap — fix the generator (Task 1 module) under TDD with a synthetic test first, then re-run.

- [ ] **Step 4: Full gate for the module**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest tests/domain/test_query_candidates.py`
Expected: all pass; `query_candidates.py` at 100% coverage.

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/real_albums.json tests/fixtures/real_albums.README.md tests/domain/test_query_candidates.py
git commit -m "test(search): real-album corpus fixture + candidate invariants"
```

---

### Task 3: Config — `BRIDGE_MIN_RESULTS` and `BRIDGE_SEARCH_BUDGET`

**Files:**
- Modify: `src/slskd_lidarr_bridge/config.py`
- Test: `tests/test_config.py`
- Modify: `tests/adapters/inbound/test_app.py:133-145` (`_make_config` defaults)

**Interfaces:**
- Produces: `Config.min_results: int` (default 3), `Config.search_budget: int` (default 75). Consumed by `create_app` in Task 4.

- [ ] **Step 1: Write the failing config tests**

In `tests/test_config.py`, add to the env dict and asserts in `test_full_env_parses_all_fields`:

```python
        "BRIDGE_MIN_RESULTS": "5",
        "BRIDGE_SEARCH_BUDGET": "120",
```
```python
    assert cfg.min_results == 5
    assert cfg.search_budget == 120
```

And in `test_defaults_when_optional_vars_absent`:

```python
    assert cfg.min_results == 3
    assert cfg.search_budget == 75
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_config.py -v --no-cov`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'min_results'`.

- [ ] **Step 3: Add the fields and parsing**

In `config.py`, add to the `Config` dataclass (after `min_bitrate: int | None`):

```python
    min_results: int
    search_budget: int
```

Add to the docstring Attributes section:

```
    min_results:
        Stop issuing fallback candidates once this many distinct releases have
        accumulated. Defaults to 3.
    search_budget:
        Wall-clock seconds gating fallback candidates (the primary always runs);
        kept under Lidarr's hardcoded 100 s indexer-request abort. Defaults to 75.
```

In `from_env`, add to the returned `cls(...)` (after `min_bitrate=min_bitrate,`):

```python
            min_results=int(env.get("BRIDGE_MIN_RESULTS", "3")),
            search_budget=int(env.get("BRIDGE_SEARCH_BUDGET", "75")),
```

- [ ] **Step 4: Update `_make_config` in the app test helper**

In `tests/adapters/inbound/test_app.py`, add to the `defaults` dict in `_make_config` (after `min_bitrate=None,`):

```python
        min_results=3,
        search_budget=75,
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_config.py tests/adapters/inbound/test_app.py -v --no-cov`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/slskd_lidarr_bridge/config.py tests/test_config.py tests/adapters/inbound/test_app.py
git commit -m "feat(config): BRIDGE_MIN_RESULTS and BRIDGE_SEARCH_BUDGET"
```

---

### Task 4: SearchService candidate loop

**Files:**
- Modify: `src/slskd_lidarr_bridge/domain/search_service.py`
- Modify: `src/slskd_lidarr_bridge/adapters/inbound/app.py:60-64`
- Test: `tests/domain/test_search_service.py`

**Interfaces:**
- Consumes: `generate_candidates` (Task 1); `Config.min_results`, `Config.search_budget` (Task 3).
- Produces: `SearchService(__init__(..., min_results: int = 3, search_budget: int = 75))`; behaviour unchanged for a single sufficient primary search.

- [ ] **Step 1: Upgrade the test `FakeGateway` (per-search polling + text-mapped responses)**

In `tests/domain/test_search_service.py`, replace the `FakeGateway` class with:

```python
class FakeGateway:
    """Scriptable SoulseekGateway.

    Each started search has its own poll counter (so multiple searches in one
    SearchService.search() call are independent). `responses_by_text` maps a
    query string to its responses; any unmapped text falls back to `responses`.
    """

    def __init__(
        self,
        *,
        completes_on: int = 1,
        responses: list[SearchResponse] | None = None,
        responses_by_text: dict[str, list[SearchResponse]] | None = None,
    ) -> None:
        self._completes_on = completes_on
        self._default: list[SearchResponse] = responses or []
        self._by_text = responses_by_text or {}
        self.started_searches: list[str] = []
        self._sid_counter = 0
        self._sid_text: dict[str, str] = {}
        self._poll_counts: dict[str, int] = {}

    def start_search(self, text: str) -> str:
        self._sid_counter += 1
        sid = f"search-{self._sid_counter}"
        self.started_searches.append(text)
        self._sid_text[sid] = text
        self._poll_counts[sid] = 0
        return sid

    def search_is_complete(self, search_id: str) -> bool:
        self._poll_counts[search_id] += 1
        return self._poll_counts[search_id] >= self._completes_on

    def search_responses(self, search_id: str) -> list[SearchResponse]:
        return list(self._by_text.get(self._sid_text[search_id], self._default))

    # Unused in search service — satisfy Protocol
    def enqueue(self, username: str, files: list[AudioFile]) -> None: ...
    def transfers(self, username: str) -> list[Transfer]:
        return []

    def cancel(self, username: str, transfer_id: str) -> None: ...
    def downloads_directory(self) -> str:
        return "/downloads"
```

- [ ] **Step 2: Verify existing search tests still pass against the upgraded fake**

Run: `uv run pytest tests/domain/test_search_service.py -v --no-cov`
Expected: PASS (the per-search counter is transparent for today's single-search `search()`).

- [ ] **Step 3: Write a failing orchestration test — walk to a fallback**

Append to `tests/domain/test_search_service.py`:

```python
def test_walks_to_fallback_when_primary_returns_too_few():
    # Primary ("Beyonce Lemonade Deluxe") finds nothing; the edition-stripped
    # fallback ("Beyonce Lemonade") finds an album.
    primary = "Beyonce Lemonade (Deluxe)"
    fallback = "Beyonce Lemonade"
    resp = make_response("alice", [make_flac("Lemonade", 1)])
    gateway = FakeGateway(
        completes_on=1,
        responses_by_text={fallback: [resp]},  # primary text → [] (default)
    )
    store = FakeStore()
    clock = FakeClock()
    service = SearchService(gateway, store, clock, min_results=3)

    releases = service.search(SearchQuery(artist="Beyonce", album="Lemonade (Deluxe)"))

    assert len(releases) == 1
    assert gateway.started_searches[0] == primary
    assert fallback in gateway.started_searches
```

- [ ] **Step 4: Run to verify failure**

Run: `uv run pytest tests/domain/test_search_service.py::test_walks_to_fallback_when_primary_returns_too_few -v --no-cov`
Expected: FAIL — today only one search is issued, so `fallback not in started_searches`.

- [ ] **Step 5: Implement the candidate loop and helpers**

In `search_service.py`, add the import near the top:

```python
from slskd_lidarr_bridge.domain.query_candidates import generate_candidates
```

Add the two ctor params (keyword-only block) and store them — change the signature and body:

```python
        min_bitrate: int | None = None,
        release_ttl_days: int = 7,
        min_results: int = 3,
        search_budget: int = 75,
    ) -> None:
        self._gateway = gateway
        self._store = store
        self._clock = clock
        self._search_timeout = search_timeout
        self._poll_interval = poll_interval
        self._min_bitrate = min_bitrate
        self._release_ttl_days = release_ttl_days
        self._min_results = min_results
        self._search_budget = search_budget
```

Replace the whole `search` method body (everything after the empty-query guard and purge) with the candidate loop, and add the two helpers. The full replacement for `search` onward:

```python
    def search(self, query: SearchQuery) -> list[Release]:
        if query.is_empty:
            return []

        self._store.purge_older_than(
            self._clock.now() - datetime.timedelta(days=self._release_ttl_days)
        )

        candidates = generate_candidates(query)
        seen: set[tuple[str, str]] = set()
        # (has_free_upload_slot, upload_speed, queue_length, release) — for sorting.
        tagged: list[tuple[bool, int, int, Release]] = []
        start = self._clock.now()

        for index, text in enumerate(candidates):
            if index > 0:
                if len(seen) >= self._min_results:
                    break
                elapsed = (self._clock.now() - start).total_seconds()
                if elapsed >= self._search_budget:
                    break
                remaining = self._search_budget - elapsed
                max_seconds = min(float(self._search_timeout), max(remaining, 0.0))
            else:
                max_seconds = float(self._search_timeout)

            responses = self._run_search(text, max_seconds)
            self._collect(responses, seen, tagged)

        # Order by (free slot desc, upload speed desc, queue length asc).
        tagged.sort(key=lambda x: (x[0], x[1], -x[2]), reverse=True)
        return [r for *_, r in tagged]

    def _run_search(self, text: str, max_seconds: float) -> list[SearchResponse]:
        sid = self._gateway.start_search(text)
        start = self._clock.now()
        while not self._gateway.search_is_complete(sid):
            elapsed = (self._clock.now() - start).total_seconds()
            if elapsed >= max_seconds:
                logger.warning(
                    "Search %r timed out after %ss; returning partial results",
                    text,
                    max_seconds,
                )
                break
            self._clock.sleep(self._poll_interval)
        return self._gateway.search_responses(sid)

    def _collect(
        self,
        responses: list[SearchResponse],
        seen: set[tuple[str, str]],
        tagged: list[tuple[bool, int, int, Release]],
    ) -> None:
        for response in responses:
            audio: list[AudioFile] = [
                f
                for f in response.files
                if f.is_audio
                and (
                    self._min_bitrate is None
                    or f.bitrate is None
                    or f.bitrate >= self._min_bitrate
                )
            ]
            if not audio:
                continue

            groups: dict[str, list[AudioFile]] = defaultdict(list)
            for f in audio:
                groups[f.album_folder].append(f)

            for folder, files in groups.items():
                key = (response.username, folder)
                if key in seen:
                    continue

                if " - " in folder:
                    left, right = folder.split(" - ", 1)
                    artist, album = left.strip(), right.strip()
                else:
                    album = folder
                    artist = files[0].artist_folder

                size = sum(f.size for f in files)
                quality = detect_quality(files)
                title = build_title(artist, album, quality, response.username)
                release = Release(
                    artist=artist,
                    album=album,
                    title=title,
                    username=response.username,
                    files=tuple(files),
                    size=size,
                    album_folder=folder,
                    quality=quality,
                    created_at=self._clock.now(),
                )
                release_id = self._store.put(release)
                release = dataclasses.replace(release, id=release_id)
                seen.add(key)
                tagged.append(
                    (
                        response.has_free_upload_slot,
                        response.upload_speed,
                        response.queue_length,
                        release,
                    )
                )
```

Add the `SearchResponse` import to the existing models import line:

```python
from slskd_lidarr_bridge.domain.models import (
    AudioFile,
    Release,
    SearchQuery,
    SearchResponse,
)
```

- [ ] **Step 6: Run the orchestration test**

Run: `uv run pytest tests/domain/test_search_service.py::test_walks_to_fallback_when_primary_returns_too_few -v --no-cov`
Expected: PASS.

- [ ] **Step 7: Fix the three single-search tests broken by the loop**

The loop now runs fallbacks when the primary yields `< min_results`, which changes sleep/poll counts. Force primary-only on the three poll/timeout tests:

- `test_polling_completes_on_third_check`: change the constructor to
  `service = SearchService(gateway, store, clock, search_timeout=30, poll_interval=1.0, min_results=1)`.
- `test_timeout_stops_polling_without_infinite_loop`: change to
  `SearchService(gateway, store, clock, search_timeout=5, poll_interval=1.0, min_results=0)`.
- `test_timeout_logs_warning`: change to
  `SearchService(gateway, store, clock, search_timeout=5, poll_interval=1.0, min_results=0)`.

(`min_results=0` makes the `len(seen) >= min_results` check true at the first fallback, so only the primary runs; `min_results=1` stops after the primary's single result.)

- [ ] **Step 8: Run the full search-service suite**

Run: `uv run pytest tests/domain/test_search_service.py -v --no-cov`
Expected: PASS.

- [ ] **Step 9: Add the remaining orchestration tests**

Append:

```python
def test_stops_at_threshold_after_primary():
    # Primary alone yields 3 folders (= min_results) → no fallback issued.
    resp = make_response(
        "alice",
        [make_flac("A", 1), make_flac("B", 1), make_flac("C", 1)],
    )
    gateway = FakeGateway(completes_on=1, responses_by_text={"X Y": [resp]})
    service = SearchService(gateway, FakeStore(), FakeClock(), min_results=3)
    releases = service.search(SearchQuery(artist="X", album="Y"))
    assert len(releases) == 3
    assert gateway.started_searches == ["X Y"]  # exactly one search


def test_budget_zero_runs_primary_only():
    resp = make_response("alice", [make_flac("Album", 1)])
    gateway = FakeGateway(completes_on=1, responses=[resp])
    service = SearchService(gateway, FakeStore(), FakeClock(), min_results=99, search_budget=0)
    service.search(SearchQuery(artist="A", album="B"))
    assert len(gateway.started_searches) == 1  # fallbacks disabled by budget<=0


def test_dedup_same_user_folder_across_candidates_counts_once():
    resp = make_response("alice", [make_flac("Album", 1)])
    # Same response for primary and every fallback text.
    gateway = FakeGateway(completes_on=1, responses=[resp])
    service = SearchService(gateway, FakeStore(), FakeClock(), min_results=99)
    releases = service.search(SearchQuery(artist="A", album="B"))
    assert len(releases) == 1  # deduped on (username, album_folder)
    assert len(gateway.started_searches) >= 2  # walked candidates (never reached 99)
```

- [ ] **Step 10: Run to verify pass**

Run: `uv run pytest tests/domain/test_search_service.py -v --no-cov`
Expected: PASS.

- [ ] **Step 11: Wire the config values into `create_app`**

In `src/slskd_lidarr_bridge/adapters/inbound/app.py`, extend the `SearchService(...)` construction:

```python
    search_service = SearchService(
        gateway,
        release_store,
        clock,
        search_timeout=config.search_timeout,
        min_bitrate=config.min_bitrate,
        min_results=config.min_results,
        search_budget=config.search_budget,
    )
```

- [ ] **Step 12: Full gate**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`
Expected: all pass; **100% coverage**. If a branch in `search()`/`_collect()` is uncovered, add a targeted orchestration test (e.g. a per-search timeout inside a fallback, or a budget cut-off mid-loop) and re-run.

- [ ] **Step 13: Commit**

```bash
git add src/slskd_lidarr_bridge/domain/search_service.py src/slskd_lidarr_bridge/adapters/inbound/app.py tests/domain/test_search_service.py
git commit -m "feat(search): serial multi-candidate loop with result+budget gating"
```

---

### Task 5: Document the new env vars

**Files:**
- Modify: `README.md` (environment-variable table)

- [ ] **Step 1: Add the two rows**

In `README.md`, in the env-var table, add after the `BRIDGE_MIN_BITRATE` row:

```markdown
| `BRIDGE_MIN_RESULTS` | no | `3` | Stop issuing further (looser) fallback search queries once this many distinct releases have accumulated. The primary query always runs |
| `BRIDGE_SEARCH_BUDGET` | no | `75` | Wall-clock seconds across the whole search (primary + fallbacks). Bounds latency under Lidarr's ~100 s indexer-request timeout. `0` runs the primary query only (disables fallbacks) |
```

- [ ] **Step 2: Verify the table renders and is accurate**

Run: `uv run python -c "import pathlib; print('BRIDGE_SEARCH_BUDGET' in pathlib.Path('README.md').read_text())"`
Expected: `True`.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document BRIDGE_MIN_RESULTS and BRIDGE_SEARCH_BUDGET"
```

---

## Self-Review

**Spec coverage:**
- Serial candidate loop, gating on cumulative distinct results + wall-clock budget, primary always runs → Task 4.
- Candidate pipeline raw→edition-stripped→folded→album-only, term-only path, string-dedup → Task 1.
- `BRIDGE_MIN_RESULTS` (3), `BRIDGE_SEARCH_BUDGET` (75) + wiring → Tasks 3 & 4.
- Dedup `(username, album_folder)` before `store.put()`; ranking unchanged → Task 4 (`_collect`).
- Per-search cap `min(search_timeout, remaining)`, primary full timeout → Task 4 (`search` loop).
- Committed MusicBrainz+curated balanced ~80 corpus, invariants + golden subset, no network in tests → Tasks 1 & 2.
- README documentation → Task 5.

**Placeholder scan:** The `mbid: ""` values in the seed corpus are filled during Task 2 Step 1 from MusicBrainz; that step states it explicitly. No other placeholders.

**Type consistency:** `generate_candidates(query: SearchQuery) -> list[str]` (Task 1) is imported and called in Task 4. `min_results`/`search_budget` are `int` in Config (Task 3) and ctor params (Task 4). Helpers `_run_search(text: str, max_seconds: float) -> list[SearchResponse]` and `_collect(responses, seen, tagged) -> None` use the `tagged` tuple `tuple[bool, int, int, Release]` consistent with the existing sort.

## Execution Handoff

Plan complete and saved to `docs/plans/2026-06-30-search-normalization-fallback.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
