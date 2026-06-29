# slskd ⇄ Lidarr bridge — design spec

Date: 2026-06-29

## Goal

A small, single-process container that makes **slskd** (headless Soulseek client) appear to
**Lidarr** as both:

1. a **Newznab indexer** (so Lidarr can search Soulseek), and
2. a **SABnzbd download client** (so Lidarr can grab a result, track it to completion, and import the files).

No web UI. It is a pure protocol adapter / proxy. Configuration is **only** via environment
variables. Persistence (when needed) is SQLite. Python + uv. Dockerized.

## Why usenet (Newznab + SABnzbd) and not torrent

In Lidarr the indexer protocol and the download-client protocol must be the same family
(usenet↔usenet, torrent↔torrent). The torrent path forces Lidarr to derive an **infohash** from
the indexer's enclosure and correlate the download client item by that exact hash — impossible to
satisfy cleanly for a Soulseek source without fabricating coordinated fake infohashes across two
components. The usenet/SABnzbd path lets the bridge **mint its own opaque download id** (`nzo_id`),
uses a flat HTTP/JSON API, and matches Soulseek's "fire-and-forget, no seeding" semantics. Decided.

## External protocol facts (verified against current sources)

### slskd REST API (base `/api/v0`)
- Auth: header `X-API-Key: <key>` (16–255 chars).
- Search is **start-then-poll**:
  - `POST /api/v0/searches` body `{ "searchText": "<terms>" }` → returns `Search` with `id` (Guid).
  - Poll `GET /api/v0/searches/{id}` until `isComplete == true` (~`searchTimeout`≈15s).
  - `GET /api/v0/searches/{id}/responses` → array of responses:
    `{ username, hasFreeUploadSlot, queueLength, uploadSpeed, files: [ { filename, size, extension,
    bitRate, bitDepth, sampleRate, length, ... } ] }`.
    `filename` is the full remote path with **backslash** separators.
- Download:
  - `POST /api/v0/downloads/{username}` body `[ { "filename": "<remote path>", "size": <bytes> } ]`.
    Does **not** return the new transfer id.
  - `GET /api/v0/downloads/{username}` → user's transfers; match by `filename` to learn each `id`.
  - `GET /api/v0/downloads/{username}/{id}` → `Transfer`: `state` (comma-joined flags),
    `bytesTransferred`, `bytesRemaining`, `size`, `percentComplete`, `averageSpeed`, `exception`, ...
  - Done = `state` contains `Completed`; success = also contains `Succeeded`.
  - Cancel/remove: `DELETE /api/v0/downloads/{username}/{id}?remove=true`.
- Completed files land under slskd's downloads directory, preserving the remote album subfolder.

### Lidarr → Newznab indexer
- `GET {base}/api?t=caps` → must return `<caps>` advertising:
  - `<search available="yes" supportedParams="q"/>`
  - `<audio-search available="yes" supportedParams="q,artist,album"/>`
  - `<categories>` containing music categories (3000 Audio, 3010 MP3, 3030 Audiobook, 3040 Lossless).
- Album search arrives as `GET {base}/api?t=music&cat=...&extended=1&offset=0&limit=100&q=&artist=<A>&album=<B>`
  (note: caps element is `audio-search` but the request mode is `t=music`).
  Basic fallback: `t=search&q=<A B>`. RSS sync: same mode with no query terms.
- Response = RSS 2.0 with `xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"`.
  Each `<item>` needs: `<title>` (scene-parseable), `<guid>`, `<pubDate>` (mandatory),
  `<enclosure url=".../nzb/<id>" length="<bytes>" type="application/x-nzb"/>`, and
  `<newznab:attr name="size" value="<bytes>"/>` plus `name="category"`.
- **Lidarr matches by parsing `<title>`** (artist/album/quality), not by attrs. Titles must look like
  `Artist - Album (Year) [FLAC]` / `Artist - Album [MP3-320]`.
- API key (optional) arrives as `&apikey=<key>`.

### Lidarr → SABnzbd download client (api at `{base}/api`, `apikey` + `output=json`)
Only these `mode` values are used:
- `mode=version` → `{ "version": "<x.y.z>" }` (no apikey required).
- `mode=get_config` → `{ "config": { "misc": { "complete_dir": "...", ... },
  "categories": [ "<cat>", ... ] } }`. Lidarr validates its configured category is present.
- `mode=fullstatus&skip_dashboard=1` → `{ "status": { ... } }`.
- `mode=addfile` (**multipart POST**, field `name` = NZB bytes, plus `nzbname`, `cat`, `priority`)
  → `{ "status": true, "nzo_ids": ["<id>"] }`. Lidarr stores `nzo_ids[0]` as DownloadId.
- `mode=queue&start=0&limit=...[&category=<cat>]` → `{ "queue": { "slots": [ { nzo_id, filename,
  status, mb, mbleft, percentage, cat, timeleft, ... } ] } }` (in-progress).
- `mode=history&start=0&limit=...[&category=<cat>]` → `{ "history": { "slots": [ { nzo_id, name,
  nzb_name, status, storage, category, fail_message, bytes, ... } ] } }` (completed).
  **`storage` = absolute final folder of the files — Lidarr imports from here.**
- Remove: queue `mode=queue&name=delete&value=<nzo_id>&del_files=1`;
  history `mode=history&name=delete&value=<nzo_id>`.
- Status mapping: `Downloading/Queued/Paused → in-progress`; `Completed → done`; `Failed → failed`.

## Architecture (clean / ports & adapters)

```
slskd_lidarr_bridge/
  config.py            # Config dataclass loaded from env (the ONLY config source)
  domain/
    models.py          # SearchQuery, AudioFile, Quality, Release, DownloadJob, JobStatus
    quality.py         # detect Quality + format label from a set of AudioFile
    titles.py          # build scene-parseable release titles
    ports.py           # Protocols: SoulseekGateway, ReleaseStore, JobStore, Clock
    search_service.py  # use case: query -> [Release]   (orchestrates SoulseekGateway)
    download_service.py# use cases: start / list / remove downloads
  adapters/
    slskd_gateway.py   # SoulseekGateway over slskd REST (httpx)
    sqlite_store.py    # ReleaseStore + JobStore over sqlite3
    system_clock.py    # Clock
  web/
    app.py             # Flask app factory; wires config -> adapters -> services -> blueprints
    newznab.py         # /indexer/api (caps, search), /indexer/nzb/<id>
    sabnzbd.py         # /sabnzbd/api (mode dispatch)
    nzb.py             # build_nzb(payload) / parse_nzb(bytes) -> payload (self-describing NZB)
    xml.py             # caps XML + results RSS builders (stdlib ElementTree)
  main.py              # entrypoint: build app, serve with waitress
```

**Dependency rule:** `domain/` imports nothing from `adapters/` or `web/`. Services depend on the
Protocols in `ports.py`. Adapters implement them. `web/app.py` and `main.py` are the composition
root.

### Ports (domain-owned interfaces)
- `SoulseekGateway`: `search(text) -> [SearchResponse]`, `enqueue(username, files) -> None`,
  `transfers(username) -> [Transfer]`, `all_transfers() -> [Transfer]`,
  `cancel(username, transfer_id) -> None`.
- `ReleaseStore`: `put(release) -> id`, `get(id) -> Release | None`, `purge_older_than(dt)`.
- `JobStore`: `add(job)`, `get(nzo_id) -> Job | None`, `list() -> [Job]`, `remove(nzo_id)`.
- `Clock`: `now() -> datetime`.

### Library choices (popular, active, maintained)
- Web: **Flask** (sync; simple proxy) served by **waitress** (pure-python WSGI, threaded).
- HTTP client to slskd: **httpx** (sync client).
- XML / NZB / SQLite: Python **stdlib** (`xml.etree.ElementTree`, `sqlite3`) — no extra deps.
- Tests: **pytest**; **respx** to mock httpx in the slskd adapter tests.

## End-to-end flow

1. **caps**: Lidarr `t=caps` → static caps XML advertising audio-search + music categories.
2. **search**: Lidarr `t=music&artist&album` (or `t=search&q`) →
   `search_service` issues `POST /searches`, polls to completion, fetches responses, groups each
   user's audio files by album folder into `Release` candidates. Quality detected from
   extensions/bitrate; title built scene-style. Each Release is stored in `ReleaseStore`
   (→ short id) and rendered as an RSS `<item>` whose enclosure points at `/indexer/nzb/<id>`.
   RSS-sync (no query) returns an empty channel.
3. **grab**: Lidarr GETs `/indexer/nzb/<id>` → bridge loads the Release and returns a valid,
   self-describing NZB embedding a base64 payload (username + file list + title + size + category-less).
4. **addfile**: Lidarr POSTs that NZB to `/sabnzbd/api?mode=addfile` → bridge parses payload,
   `enqueue`s the download on slskd, creates a `DownloadJob` (mint `nzo_id`, store username + remote
   file paths + album folder + category + title + total size), returns `nzo_ids:[nzo_id]`.
5. **queue/history**: Lidarr polls. For each job, bridge reads slskd transfers for that user, matches
   the job's files, aggregates progress/state. Not-yet-complete → a `queue` slot; complete+succeeded →
   a `history` slot with `storage` = computed album folder under the downloads dir. Failed → history
   slot with failed status + `fail_message`.
6. **delete**: Lidarr `mode=...&name=delete&value=<nzo_id>` → bridge cancels matching slskd transfers
   (queue delete) and/or removes the job record.

### Storage path computation (the one fragile spot — isolate + test)
`storage` must be the real directory where slskd wrote the album, as Lidarr will see it (shared
volume). Implemented as a pure function `compute_storage_path(downloads_dir, remote_filename)` that
derives the album folder from the remote path's last directory component; verified against slskd's
actual on-disk layout during implementation of the slskd adapter. Reported path is absolute; Lidarr
Remote Path Mapping handles any mount divergence.

## Configuration (environment variables only)

| Var | Required | Default | Purpose |
|---|---|---|---|
| `SLSKD_URL` | yes | — | Base URL of slskd, e.g. `http://slskd:5030` |
| `SLSKD_API_KEY` | yes | — | slskd `X-API-Key` |
| `SLSKD_DOWNLOADS_DIR` | yes | — | Path of slskd's completed-downloads dir, **as the bridge/Lidarr see it** (shared volume); used for `storage` |
| `BRIDGE_API_KEY` | no | unset | If set, Lidarr must present it as `apikey` to indexer and SABnzbd; else not enforced |
| `BRIDGE_CATEGORIES` | no | `music` | Comma-separated categories advertised by `get_config` |
| `BRIDGE_HOST` | no | `0.0.0.0` | Bind host |
| `BRIDGE_PORT` | no | `8765` | Bind port |
| `SLSKD_SEARCH_TIMEOUT` | no | `30` | Seconds to wait for a slskd search to complete |
| `BRIDGE_DB_PATH` | no | `/data/bridge.db` | SQLite file |
| `BRIDGE_MIN_BITRATE` | no | unset | Optional filter on candidate files |

Enclosure base URL is derived from the incoming request host (no config needed).

## Error handling
- slskd unreachable / 5xx: indexer returns `<error code="900" description="..."/>`; SABnzbd returns
  `{"status": false, "error": "..."}`; never 500 with a stack trace.
- Search timeout: return whatever responses exist (possibly empty channel).
- Unknown `nzo_id` on delete: no-op success.
- Transfer in `Errored/TimedOut/Rejected/Cancelled`: surfaced as a failed `history` slot so Lidarr can
  fail the grab and try the next release.
- Missing required env var: fail fast at startup with a clear message.

## Testing strategy (TDD strict — tests are the spec)
- **domain/** unit tests with fakes for the ports: quality detection, title building, release grouping,
  search orchestration (poll loop), job lifecycle/state aggregation, storage-path computation.
- **adapters/slskd_gateway**: httpx mocked with respx — request shapes and response parsing.
- **adapters/sqlite_store**: real sqlite on a tmp file — round-trips, purge.
- **web/newznab**: Flask test client — caps XML correctness, search→RSS, nzb round-trip, apikey, errors.
- **web/sabnzbd**: Flask test client — every `mode`, addfile multipart, queue/history slot shapes,
  delete, apikey, category filtering.
- **web/nzb**: build→parse round-trip is identity.
- One **e2e** test wiring real services with a fake `SoulseekGateway`: caps → search → grab(nzb) →
  addfile → queue → (transfer completes) → history-with-storage → delete.

## Out of scope (YAGNI)
No web UI, no auth UI, no multi-user, no metrics/dashboards, no retries-with-backoff beyond surfacing
failures to Lidarr (Lidarr already retries by grabbing the next release), no torrent path, no CLI flags,
no config files.

## Docker
Multi-stage: build layer installs deps with uv into a venv; runtime layer is slim Python running
`waitress` via `main.py`. `BRIDGE_DB_PATH` under a `/data` volume. `SLSKD_DOWNLOADS_DIR` mounted from
the same volume slskd writes to (shared with Lidarr for import).
