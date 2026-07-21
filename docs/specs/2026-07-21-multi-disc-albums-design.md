# Multi-disc album support (slskd 0.26 batch destinations) — design spec

Date: 2026-07-21

## Goal

Remove the last documented limitation of the bridge: multi-disc albums. Today a
remote album laid out as `…/Album/CD1/…` + `…/Album/CD2/…` is:

1. **split at search time** into one release per disc (`SearchService._collect`
   groups by the file's immediate parent folder), so Lidarr is offered releases
   titled "CD1" and "CD2" that it cannot match; and
2. **scattered at download time**, because slskd through `0.25.1` hardcoded
   placement to `<downloads>/<immediate parent>/<basename>`, leaving no folder
   on disk that contains every disc.

slskd `0.26.0` (released 2026-07-19) lifts the blocking constraint: the caller
can now choose the destination directory per download batch. This spec covers
both halves of the fix, plus the robustness work that follows from taking
ownership of the destination.

## Verified external facts

Everything below was read from the tagged `0.26.0` sources, not from release
notes alone.

- **Batch enqueue endpoint.** `POST /api/v0/transfers/downloads/batches` accepts
  `{Id?, SearchId?, Username, Files:[{Filename, Size}], Options:{Destination?,
  ExternalId?}}` (`Transfers/API/DTO/EnqueueDownloadBatchRequest.cs`).
- **Destination wins over user configuration.** `DownloadService.DeriveDestination()`
  returns the batch's `Options.Destination` early, before the
  `transfers.download.destination.subdirectory` expression is evaluated. A
  destination supplied by the bridge therefore makes the user's slskd layout
  configuration irrelevant for bridge downloads. Decided: send one on **every**
  download, not just multi-disc ones.
- **Files land flat in the destination.** The completion path calls
  `Files.MoveFile(destinationDirectory: <downloads>/<derived subdirectory>)` with
  the file's basename, so a single batch per album would collide same-named
  tracks across discs. ⇒ **one batch per disc**, destination `<album>/<disc>`.
- **`Options.ExternalId` is dead.** The controller copies only `Destination` into
  `Batches.CreateAsync`. The `BATCH_EXTERNAL_ID` token can never be populated
  through the API. Unusable; we rely solely on `Destination`.
- **Status codes are not conventional.** 201 = all enqueued, 207 = partial,
  **200 = every file failed**, 404 = user offline, 409 = duplicate batch id,
  429 = throttled. A bare `raise_for_status()` would read 200 as success.
- **Enqueue is throttled.** `TransfersController.DownloadRequestLimiter` is a
  static `SemaphoreSlim(2, 2)` acquired with `Wait(0)`, shared by the legacy and
  batch endpoints: a third concurrent enqueue gets an immediate 429.
- **Destination validation.** `[RelativePath(OperatingSystem.All)]` requires the
  path to be relative on Linux **and** Windows; `[NonTraversingPath]` rejects
  `.`/`..` segments; the value is then run through `FileSafety.SanitizePath`,
  which replaces characters invalid on the **host** OS (Linux: only `/` and NUL;
  Windows: also `" < > | : * ?` and control characters) and drops empty segments.
  `LocalizePath` converts `\` to the host separator, so a backslash inside a
  segment would silently create a nested directory.
- **Version endpoint.** `GET /api/v0/application/version` returns the semantic
  version as a bare JSON string (`Program.SemanticVersion`).
- **Lidarr imports recursively.** `DownloadedTracksImportService.ProcessFolder`
  calls `DiskScanService.GetAudioFiles(path)` whose `allDirectories` parameter
  defaults to `true`. A download-client item pointing at `Album [peer]/` is
  scanned into `CD1/` and `CD2/`, so Lidarr sees one multi-disc album.

## Decisions

- **slskd `>= 0.26.0` is required.** No fallback to the legacy
  `POST /api/v0/transfers/downloads/{username}` endpoint: a second code path with
  different error semantics, in which multi-disc albums stay broken, is not worth
  maintaining for a project whose reference image is `latest`. Users must upgrade
  slskd anyway, since `0.26.0` moves `permissions.file.mode`.
- **Destinations carry the peer name**: `<album folder> [<peer>]`. We control the
  path now, so the pre-existing collision between two peers offering identically
  named album folders is eliminated at no cost. Lidarr renames on import, so the
  folder shape has no effect on the library.
- **Disc detection recognises `cd`, `disc`, `disk`, `part`, `side`; not `vol` or
  `volume`.** Compilation series are organised by volume (`Café del Mar/Volumen
  1/`, `Volumen 2/`), where the parent is a series name and the children are
  distinct albums; merging them would fabricate a release claiming to hold twenty
  albums. `part` and `side` subfolders, in contrast, are parts of one thing.
- **A release is produced even when a single disc of a multi-disc album is
  present** in a peer's response. It is rooted at the album, listing that one
  disc. Strictly better than today's "CD2" release, which Lidarr cannot match.
- **Placement is decided at search time** and carried verbatim in the NZB
  payload. The download side never re-derives it.

## Design

### Disc folder detection (`domain/albums.py`, new)

Matched case-insensitively against a directory's **name**:

```python
_DISC_WORDS = "one|two|three|four|five|six|seven|eight|nine|ten"
_DISC_FOLDER_RE = re.compile(
    rf"^[\[(]?\s*(?:cd|disc|disk|part|side)[\s._\-]*"
    rf"(?:\d{{1,2}}|[a-e]|{_DISC_WORDS})\b",
    re.IGNORECASE,
)
```

A quantifier is mandatory after the token, so "Disco", "Discovery", "CDs",
"CD Audio" and "Part Of The Album" do not match, while "CD1", "CD 01 - Bonus",
"Disc Two", "[CD2]", "Side A" and "Disk_3" do. A trailing `\b` keeps three-digit
runs ("CD100") out.

### Grouping (`SearchService._collect`)

Replaces the current group-by-parent-folder-name:

1. Group a peer's audio files by their **full** remote directory path. This also
   fixes a latent bug: today two distinct albums of one peer whose folders share
   a *name* are merged.
2. Album root of a directory = its parent when the directory's name matches
   `_DISC_FOLDER_RE`, otherwise the directory itself. Not promoted when there is
   no parent, or when the parent is a Soulseek share root (`@@`-prefixed).
3. Merge directories sharing an album root into one release; per-disc subpath =
   the album-root-relative remainder of each file's directory (empty for
   single-disc).
4. Artist and album come from the album root's own name (split on `" - "` when
   present) and its parent segment, replacing the per-file `album_folder` /
   `artist_folder` properties, which are off by one level once discs are
   grouped. The artist falls back to `""` when the parent is a share root, which
   today leaks `@@abcde` into the artist field.
5. Dedup key becomes `(username, album root path)`.

Ordering is deterministic: discs sorted by subpath, files sorted by filename.

### Destination and identity (`domain/paths.py`)

Segment sanitiser, deliberately stricter than any single host:

```python
_INVALID = frozenset('"<>|:*?\\/') | {chr(c) for c in range(32)}

def sanitize_segment(segment: str) -> str:
    cleaned = "".join("_" if c in _INVALID else c for c in segment)
    cleaned = cleaned.rstrip(". ")   # Windows truncates trailing dots/spaces
    return cleaned or "_"
```

Using the union of the Windows and Linux invalid sets makes slskd's own
`SanitizePath` a no-op on either host, so the path the bridge reports to Lidarr
is exactly the path on disk rather than an assumption. It also guarantees the
`RelativePath(All)` and `NonTraversingPath` validators pass: no segment can hold
a slash, a drive-letter prefix, or reduce to `.`/`..`.

- album destination: `sanitize_segment(f"{album_name} [{username}]")`
- disc subpath: each of its segments passed through `sanitize_segment` at search
  time, so what is stored is already the on-disk form
- batch destination: `posixpath.join(destination, subpath)`, or `destination`
  alone when the subpath is empty
- storage path reported to Lidarr: `compute_storage_path(downloads_dir,
  destination)`, reduced to a join now that no layout is inferred
- release id: `sha256(username + NUL + album_path)`, where `album_path` is the
  full remote album root, instead of the folder name alone

### Models and carriers

- New `Disc(subpath: str, files: tuple[AudioFile, ...])`.
- `Release`: drops `album_folder`; gains `album_path` (remote root, for identity),
  `destination` (local, sanitised) and `discs`. `files` becomes a flattening
  property so existing consumers keep working.
- `DownloadJob`: drops `album_folder`; gains `destination` and `discs`.
- `Transfer.local_path` is removed along with the branch preferring it in
  `DownloadService.statuses`: the adapter always set it to `None` (slskd exposes
  no on-disk path there), and our own destination is now authoritative.
- NZB payload: `{username, title, total_size, destination, discs: [{subpath,
  files: [{filename, size}]}]}`.

### Storage schema (`adapters/outbound/sqlite_store.py`)

The store has no migration mechanism (`CREATE TABLE IF NOT EXISTS`), so new
columns would break an existing `/data/bridge.db` at INSERT time. Add
`PRAGMA user_version` with `SCHEMA_VERSION = 2`:

- version matches: nothing to do;
- version differs: drop and recreate both tables, then stamp the version.

Releases are a 7-day cache, so the only real cost is losing jobs in flight at
upgrade time; that is logged as a warning (and only when tables actually existed,
so a fresh database stays quiet) and documented in the README.

### Gateway (`adapters/outbound/slskd_gateway.py`)

`enqueue(username, files, destination)` posts one batch and maps the response:
201 succeeds; 200 and 207 raise `EnqueueError` (a new exception defined in the
gateway module) carrying slskd's per-file failure messages; 429 is retried with a
bounded backoff (`enqueue_retry_delays = (0.5, 1.5)`, sleep injectable for tests)
before raising; anything else goes through `raise_for_status()`. `Id` is omitted
so slskd generates it, which also makes the 409 duplicate-id case unreachable.

A `version()` method reads `GET /api/v0/application/version`. It stays off the
`SoulseekGateway` protocol: only the composition root calls it, and adding it to
the port would force every test fake to implement it for no benefit.

### Download service

`start()` issues one batch per disc. Because the batches are not atomic, a
failure after at least one success triggers a **best-effort compensation**: the
already-enqueued files are cancelled before the error propagates. Without it,
discs would keep downloading into a folder Lidarr will never be told about,
filling the disk silently. Cancellation is factored into a helper shared with
`remove()`.

`statuses()` reports `compute_storage_path(downloads_dir, job.destination)` on
completion. Its retry path re-enqueues the failed files grouped by disc, using
the same destinations, and is now wrapped: an enqueue exception there currently
escapes `statuses()` and fails the entire SABnzbd queue listing, which makes
Lidarr treat the whole download client as down. On failure we log and leave the
job downloading; the stall detector remains the backstop.

### Startup check (composition root, `main.build_app`)

Read `version()` and refuse to start when it is below `0.26.0`, naming the
required version and the endpoint that needs it. Two deliberate exceptions:

- slskd development builds report `0.0.0`: treated as unknown, accepted with a
  warning;
- an unreachable slskd is a transient condition, not an old version: log a
  warning and continue, so a `depends_on` race does not crash-loop the container.

`create_app` stays free of the check so tests and e2e wiring are unaffected.

### Documentation

README: state the slskd `>= 0.26.0` requirement, remove the multi-disc
limitation bullet, note that `transfers.download.destination.subdirectory` is
ignored for bridge downloads, and document the one-time bridge database reset.
Since the bridge is what forces the upgrade, the requirement links to slskd's
`0.26.0` release notes and mentions that they carry a breaking configuration
change of their own; the details stay upstream rather than being copied here,
where they would drift. The
multi-disc paragraph in the `compute_storage_path` docstring is deleted, since
the constraint it describes no longer exists.

## Testing

100% line and branch coverage, as everywhere else in the project.

- **Detection**: positives (digits, words, letters, brackets, separators),
  negatives ("Disco", "Discovery", "CDs", "CD Audio", "Vol 1", "CD100"), and the
  `@@` share-root guard.
- **Grouping**: single-disc unchanged; two discs merged under one root; a single
  disc of a multi-disc album; same-named folders under different parents kept
  apart; artist derived from the root's parent and from `" - "` splitting.
- **Sanitising**: hostile characters, trailing dots and spaces, a segment that
  reduces to empty, a peer name containing a slash.
- **Gateway** under `respx`: 201, 207, 200, 404, and 429 followed by success,
  then 429 exhausting its retries.
- **Download service**: one batch per disc with the expected destinations;
  compensation cancelling the first disc when the second fails; storage path on
  completion; retry grouped by disc; retry failure swallowed.
- **Store**: schema recreated on version mismatch, warning only when tables
  existed, fresh database stamped.
- **Startup**: too old refuses, `0.0.0` warns and proceeds, unreachable warns and
  proceeds.

## Out of scope

- Albums whose discs are already flattened into one folder (`1-01 Title.flac`):
  they work today and are untouched.
- `vol` / `volume` detection, excluded by decision above.
- Nested layouts deeper than one disc level are handled generically by the
  subpath computation but are not a design target.
