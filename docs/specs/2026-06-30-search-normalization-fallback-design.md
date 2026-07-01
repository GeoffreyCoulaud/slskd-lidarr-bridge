# Search normalization + multi-tier fallback — design spec

Date: 2026-06-30

## Goal

Raise the search hit-rate of the bridge when Lidarr's exact `"artist album"`
query does not match how the album is named on Soulseek (edition suffixes,
diacritics, punctuation, compilations). Today `SearchService.search()` issues a
**single** slskd search built from `SearchQuery.to_search_text()`; if that misses,
Lidarr gets nothing.

The fix: try an **ordered list of progressively looser query candidates**,
**in series**, stopping as soon as we have enough results or run out of a
wall-clock budget.

This is item 4 of the Tubifarry-borrowing roadmap (see the
`bridge-tubifarry-roadmap` memory). Items 1–3 (stall timeout, retries, slskd
search-timeout forwarding, queue-length ranking) already shipped.

## Why this is the realistic ceiling (no scope creep)

We are a **Newznab indexer**, so Lidarr only sends us `artist` + `album` strings
(`adapters/inbound/newznab.py`, `t=music`) — or a single `q` term (`t=search`).
We do **not** receive the track count, track titles, MusicBrainz aliases, year,
or release type that Tubifarry (a native Lidarr plugin) reads from
`AlbumSearchCriteria`. So this design deliberately does **not** attempt
track-count matching/filtering, track-title fallback, or alias fallback — we
lack the inputs. We only transform the two strings we have. Decided.

## Verified external facts

- **slskd serializes search submission.** `POST /api/v0/searches` is guarded by a
  process-wide `SemaphoreSlim(1,1)`; an overlapping POST gets an immediate
  **HTTP 429** (slskd `SearchesController.cs`, non-blocking `Wait(0)`). slskd also
  caps actual concurrent search *execution* at 2 (hardcoded in Soulseek.NET, not
  configurable) and queues the rest. The semaphore exists explicitly to avoid
  bombarding Soulseek's central server (which throttles/bans chatty users).
  ⇒ Running candidates **in series** is not just polite, it is required. Decided.
- **Lidarr aborts an indexer search HTTP call after ~100 s**, hardcoded and
  **not configurable** (Lidarr `ManagedHttpDispatcher.cs:78` — the `else` branch
  applies `TimeSpan.FromSeconds(100)` because Newznab requests leave
  `RequestTimeout == Zero`). Same for RSS sync and interactive/automatic search.
  There is no aggregate multi-indexer budget. ⇒ Our whole `search()` call must
  finish comfortably under 100 s; this sets the wall-clock budget default.

## Design

### Control flow (`SearchService.search`)

Replace the single search with a serial loop over candidate query strings, with
two combined stop conditions:

```
candidates = generate_candidates(query)        # ordered, string-deduplicated
results: dict[(username, album_folder), Release] = {}
start = clock.now()
for index, cand in enumerate(candidates):
    elapsed = (clock.now() - start).total_seconds()
    window = search_window(search_budget - elapsed)   # remaining − margin, opt. capped
    if index == 0:
        window = max(window, MIN_SEARCH_WINDOW)  # the primary always runs
    else:
        if len(results) >= min_results: break    # enough distinct results
        if window < MIN_SEARCH_WINDOW: break      # not enough budget for a real search
    try:
        responses = run_search(cand, window)     # forwards `window` to slskd; polls it
    except Exception:
        if not results: raise                    # total failure → Lidarr error envelope
        break                                    # keep earlier results, stop walking
    for release parsed from responses:
        key = (username, album_folder)
        if key in results: continue              # dedup BEFORE store.put()
        store.put(release); results[key] = release
return sort(results.values())                    # existing (free slot, speed, queue) order

search_window(remaining) = min(remaining − INTER_SEARCH_MARGIN, search_timeout or ∞)
run_search(cand, window):  start_search(cand, window)   # slskd's searchTimeout = window
                           poll until isComplete, cap = window + COMPLETION_GRACE
```

- **Each search takes the wall-clock time still available**, sized as
  `min(remaining_budget − margin, search_timeout)`. The window is forwarded to
  slskd *per call* as its `searchTimeout`, so slskd stops gathering exactly when
  the bridge stops polling — no lingering search to 429 the next candidate.
  **Correction (root cause of the end-to-end miss):** the original draft used a
  single fixed `search_timeout` both as slskd's window *and* the bridge's poll
  cap, so the bridge abandoned the primary at the instant slskd finalised, fetched
  an empty `/responses`, and fired a premature fallback that slskd refused (429).
- **`search_timeout` is now an *optional per-query maximum*, default `0` (no
  cap).** With no cap, a search uses the whole remaining budget — slskd's native
  one-search-per-query behaviour, which already works most of the time. A positive
  value caps each search so several looser candidates fit inside the budget.
- **The primary candidate (index 0) always runs**, floored at `MIN_SEARCH_WINDOW`
  (slskd's 5 s minimum) so a tight budget can't starve it.
- **Gating counts distinct releases**: once `len(results) >= min_results`, no
  further candidate starts.
- **A candidate's search failing is non-fatal *unless nothing is collected yet*.**
  A later, looser candidate that 429s / errors must not discard the earlier,
  higher-precision results — log and stop. But if the *first* search fails with an
  empty result set, the error propagates so Lidarr gets an error envelope rather
  than a false "no results".
- **Dedup key is `(username, album_folder)`**, applied *before* `store.put()` — a
  re-surfaced offer must not create duplicate DB rows / NZBs. First occurrence
  wins (it comes from the higher-precision earlier candidate). Decided.
- **A fallback only starts if its window is worthwhile** (`window >=
  MIN_SEARCH_WINDOW`), and every window is bounded by the remaining budget minus a
  margin, so total wall-clock stays under `search_budget` (kept under Lidarr's
  100 s abort).
- **Final ranking is unchanged** — the existing `(has_free_upload_slot,
  upload_speed, -queue_length)` sort applied to the deduplicated set.
- `purge_older_than` runs once at the top, as today (not per candidate).

### Candidate generation (`domain/query_candidates.py`, new, pure)

`generate_candidates(query: SearchQuery) -> list[str]` returns an ordered list,
de-duplicated by string (drop a candidate equal to an earlier one or blank).

For a `t=music` query (artist + album both present), in order:

1. **Raw** — `"{artist} {album}"`, exactly as Lidarr sent. Highest precision;
   always first; preserves today's behaviour as candidate #1.
2. **Edition-stripped** — `"{artist} {strip_editions(album)}"`. Remove
   parenthetical/bracketed qualifiers whose content contains an edition/format
   keyword, and trailing `- Single` / `- EP`. Keyword set (case-insensitive,
   starting point, locked by the golden corpus): `deluxe, remaster(ed),
   expanded, anniversary, bonus, edition, version, explicit, clean, mono,
   stereo, reissue, special, collector('?s), limited, remix(es), instrumental`.
   Parentheticals **without** an edition keyword are left intact (e.g.
   `(What's the Story) Morning Glory?` is preserved).
3. **Folded** — `"{fold(artist)} {fold(strip_editions(album))}"`. `fold` =
   Unicode NFKD then drop combining marks (é→e, ö→o); replace `&`→space; strip
   punctuation/apostrophes to space; drop filler tokens (`the, a, an, feat, ft,
   featuring, with, vs`); collapse whitespace. Case preserved.
4. **Album-only** — `fold(strip_editions(album))` alone, **unconditionally**
   (not gated on Various-Artists detection). Broadens when the artist token is
   the thing that mismatches (featured artists, name variants, compilations).

For a `t=search` term-only query (no artist/album split): **Raw** + a single
**normalized** variant (`fold(strip_editions(term))`). Steps 2/4 need the
artist/album structure and are skipped.

Robust to missing fields: any candidate that resolves to a blank string after
transformation is dropped.

### Configuration (`config.py`)

| Var | Default | Meaning |
|---|---|---|
| `BRIDGE_MIN_RESULTS` | `3` | Stop issuing further candidates once this many distinct releases have accumulated. |
| `BRIDGE_SEARCH_BUDGET` | `75` | Total wall-clock seconds for the whole search across all candidates. Each candidate takes the remaining budget (minus a margin). Chosen for ~25 s headroom under Lidarr's hardcoded 100 s indexer-request abort. |
| `SLSKD_SEARCH_TIMEOUT` | `0` | Optional per-query **maximum** seconds. `0` = no cap → each search uses the whole remaining budget (slskd's native single-search behaviour). A positive value (≥ slskd's 5 s minimum) caps each search so several looser fallback candidates fit within the budget. |

Wired through `create_app` into
`SearchService(min_results=…, search_budget=…, search_timeout=…)`. All documented
in the README env-var table.

### Code structure

- **New** `domain/query_candidates.py` — pure, no I/O: `generate_candidates` plus
  private `_strip_editions`, `_fold`. Testable in isolation.
- `SearchService`:
  - `__init__` gains `min_results: int = 3`, `search_budget: int = 75`,
    `search_timeout: int = 0` (the optional per-query cap).
  - `search()` is a candidate loop. Private helpers keep each unit focused:
    `_search_window(remaining) -> float` (window sizing), `_run_search(text,
    window) -> list[SearchResponse]` (forward the window to slskd + bounded poll),
    and `_collect(responses, seen, tagged)` (per-response audio filter / folder
    grouping / artist-album derivation / `min_bitrate` / dedup).
- `SlskdGateway.start_search(text, timeout_seconds)` forwards the per-call window
  as slskd's `searchTimeout` (ms); the gateway no longer holds a fixed timeout.
- `SearchQuery` stays a passive dataclass; the candidate module reads its fields.

### Safety: why loose candidates are not dangerous

Looser candidates surface more (sometimes wrong) folders, but they cannot cause
**wrong grabs**: artist/album are derived from the real remote folder, and Lidarr
re-parses every release title and only grabs releases whose artist/album resolve
to the searched album (existing behaviour, covered by current tests). So the only
costs of a loose candidate are latency (bounded by `search_budget`) and slskd load
(bounded by the result gating) — correctness stays Lidarr's call.

## Testing strategy (TDD, 100 % coverage)

### `tests/domain/test_query_candidates.py` (new)

Pure-function tests for the generator. Three layers:

1. **Real corpus** — a committed static fixture `tests/fixtures/real_albums.json`,
   generated from MusicBrainz (authoritative; Lidarr's metadata source; data is
   CC0) by a committed build script (`tests/fixtures/build_corpus.py`) — the
   script is the provenance proof that the titles are real, not invented. ~80
   entries. Composition follows what real data yields rather than a fixed ratio:
   real album titles are **mostly plain**, so the corpus is clean-majority, with
   a capped edition slice (reconstructed from real release `title (disambiguation)`
   pairs — the form Soulseek folders use) plus naturally-occurring coverage of
   the other dimensions (diacritics, compilations, long titles, non-Latin). The
   asserted dimensions (edition, diacritics, compilation, self-titled) are each
   present so every transform is exercised. Entries are tagged by **objective
   feature detection**, not human judgement.
   - Format: list of `{artist, album, mbid, dimension}` with real MusicBrainz
     MBIDs; a `real_albums.README.md` records source, queries, rules, and fetch
     date. Committed so tests stay hermetic — **no network in pytest** (the
     script runs once at build time, never in the suite).
   - NOTE: the earlier "~70% tricky / ~30% clean" target was a pre-data estimate;
     observing real MusicBrainz titles showed plain titles dominate, so a fixed
     ratio was dropped in favour of guaranteeing each transform is exercised.
   - **Invariants over every entry** (no hand-written expectations): raw is
     present and first and equals `"{artist} {album}"`; the list is
     string-deduplicated and non-empty; no candidate is blank; the folded
     candidate contains no combining diacritics and no parentheses; the
     album-only candidate never contains the artist token.
2. **Golden subset** — ~15–25 hand-verified entries with the **exact** expected
   candidate list, as living documentation and a precision check.
3. **Synthetic edge cases** — for branch coverage: empty/blank album, album that
   is only an edition tag, parenthetical-without-keyword preserved, all-stopword
   title, `t=search` term-only path, fields that collapse so steps dedupe away.

### `tests/domain/test_search_service.py` (extend)

Enhance the test `FakeGateway` to map **query text → responses** and reset its
completion counter per search, enabling orchestration scenarios:
- primary alone yields ≥ `min_results` → exactly one search issued (fast path);
- primary < `min_results` → walks to fallback candidates and accumulates;
- budget exhausted mid-loop → no further candidate is issued;
- `search_budget <= 0` → only the primary runs (fallbacks disabled);
- dedup across candidates → same `(user, folder)` from two candidates counted once;
- per-search timeout still bounds an individual candidate.

### `tests/test_config.py` + `tests/adapters/inbound/test_app.py`

New env vars (defaults + parsing); `_make_config` defaults updated.

## Out of scope (explicitly)

Track-count matching/filtering, track-title fallback, MusicBrainz-alias fallback,
per-user grab caps (all need inputs we don't get over Newznab); parallel/fan-out
search submission (slskd forbids it); any new source, import list, tagging, or
metadata feature. A future gateway-level 429/concurrency guard for *concurrent
Lidarr requests* is noted but separate from this work.

## Open questions

- Final tuning of the edition-keyword list and filler-word list — to be pinned by
  the golden corpus during implementation.
- The exact list of MusicBrainz artists/release-groups sampled — chosen during
  implementation to hit each dimension; recorded via the `mbid` field in the
  committed fixture for provenance.
