# Backlog

Known work that is deliberately not done yet: deferred review findings, gaps we
accepted, and things worth doing when someone is next in the area.

Read this at the start of a session, and add to it whenever you decide *not* to
fix something you noticed. An observation that only lives in a review comment or
a chat transcript is lost.

## How to write an entry

**Anchor entries to a feature and to code, never to a branch.** Write
"`domain/paths.py`, in the storage-path helpers" and not "on
`feat/multi-disc-albums`". A branch name goes stale the moment the branch is
merged and deleted, and correcting it afterwards would mean a commit on `main`,
which this project does not do. A file path and a feature name stay true.

For the same reason, do not record status here ("done", "merged"). An entry is
either present because the work is still open, or deleted in the pull request
that does the work. Removing the entry *is* how it gets closed, in the same
commit that earns it.

Each entry states what is wrong or missing, where, and why it was left. Keep the
"why" honest: "deferred, not a defect" and "we do not know how to test this yet"
are different things and lead to different decisions later.

## Multi-disc album support

Design: [`specs/2026-07-21-multi-disc-albums-design.md`](specs/2026-07-21-multi-disc-albums-design.md)

- **Never exercised against a real slskd 0.26.0.** The entire slskd contract
  (the `transfers/downloads/batches` endpoint, `Options.Destination` taking
  precedence over the configured subdirectory pattern, the 200/207/429 status
  codes, the shape of `application/version`) was read from slskd's tagged
  `0.26.0` sources and replayed with `respx`. That is solid on form but proves
  nothing about a multi-disc album actually landing on disk. First thing to try
  on a live instance.
- **Rolling back to an older bridge build leaves an unusable database.** The
  schema is versioned and recreated when it predates the running build, but the
  reverse is not handled: an older build finds its `CREATE TABLE IF NOT EXISTS`
  satisfied by the newer tables and then fails on the first `INSERT` with
  `no column named files`. Deleting `/data/bridge.db` fixes it. The README
  documents the upgrade reset but says nothing about downgrades.
- **Two albums of one peer whose folders share a name still share one download
  folder.** `SearchService._collect` builds the destination from the album
  root's folder name alone, so a peer offering `Live/` under two different
  artists produces two distinct releases (distinct `album_path`, distinct ids)
  pointing at the same directory. Not a regression: before disc grouping those
  two merged into a single bogus release and collided anyway. But the code now
  has `album_path` in hand and could disambiguate with the parent folder name or
  a short hash.
- **`remove()` swallows a failure to list transfers and deletes the job anyway.**
  Deliberate, so that a peer or slskd being unreachable cannot leave a job
  undeletable in Lidarr, and recorded in the design. The cost is that live
  transfers can be orphaned in slskd with only a warning. Revisit if orphaned
  transfers turn out to be common in practice.

## Startup and logging

- **Startup warnings bypass the configured log format.** `main()` calls
  `build_app()` before `configure_logging()`, so the schema-reset warning and
  the slskd version warnings are emitted through `logging.lastResort`: they
  reach stderr, but with no timestamp, level or logger name. These are exactly
  the messages a user reads when a container will not start.

## Code cleanliness

None of these are defects. They are places where a future edit in the area
should tidy up rather than copy the existing shape.

- `adapters/outbound/slskd_gateway.py`: the batch POST is written out twice,
  once before the 429 retry loop and once inside it. Verified correct
  (`1 + len(delays)` attempts, and an exhausted retry still raises), but a
  single loop would not need the duplicate.
- `adapters/outbound/sqlite_store.py`: `_discs_to_json` calls `_files_to_json`
  and immediately `json.loads` its result to re-embed it in the outer dump.
  Correct and round-trip symmetric, just wasteful. A shared `_file_dict` helper
  would remove the double encode.
- `domain/paths.py`: `compute_storage_path` guards its `lstrip("/")` with
  `if destination.startswith("/")`, which is redundant around an idempotent
  call and adds a branch that then needs a test to keep coverage at 100%.
- `main.py`: the development-build carve-out tests
  `reported.startswith("0.0.0")` where the rest of the function reasons over the
  parsed version tuple. The carve-out is load-bearing (a parsed `(0, 0, 0)`
  would otherwise be rejected as too old), so tidy it rather than remove it.

## Test hygiene

- `tests/domain/test_ports.py`: `_StubSoulseekGateway.enqueue` still carries the
  two-argument signature from before `destination` became required. It cannot
  fail, because `runtime_checkable` protocols only check attribute presence, but
  it misleads the next reader about the port's actual shape.
- `tests/domain/test_download_service.py`: the `_assert_not_locked()` guard on
  `FakeGateway.downloads_directory` never runs, because no test that wires
  `gateway.service` reaches the completed branch where that method is called.
  The invariant does hold today; the guard just is not load-bearing yet.
- `tests/domain/test_paths.py`: the destination-helper tests omit the `-> None:`
  return annotation that the rest of the file carries.
- `tests/adapters/outbound/test_sqlite_store.py`:
  `test_release_id_is_sha256_of_username_and_folder` now hashes `album_path`,
  not a folder name. The assertion is right, the name is stale.
- `tests/adapters/inbound/test_nzb.py`: the sample payload still uses the
  retired `album_folder` and `files` keys. Harmless, since the NZB carrier is
  payload-agnostic by design, but it reads as a leftover.
