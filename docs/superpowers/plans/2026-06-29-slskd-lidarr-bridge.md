# slskd ⇄ Lidarr bridge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A single-process container exposing slskd to Lidarr as a Newznab indexer + SABnzbd download client.

**Architecture:** Clean ports/adapters. A pure `domain/` (models, quality, titles, services) depends only on Protocol ports. Adapters implement those ports over slskd's REST API (httpx), SQLite (stdlib), and the system clock. Two Flask blueprints (`web/newznab.py`, `web/sabnzbd.py`) are inbound adapters translating HTTP ↔ services. `main.py` is the composition root served by waitress.

**Tech Stack:** Python 3.12+, uv, Flask, waitress, httpx, pytest, respx. XML/NZB/SQLite via stdlib.

**Spec:** `docs/superpowers/specs/2026-06-29-slskd-lidarr-bridge-design.md` (read it; it carries every external protocol fact).

## Global Constraints

- Python **3.12+**. Package/project management with **uv** only. Run everything via `uv run ...`.
- Configuration comes **only** from environment variables. No CLI flags, no config files.
- Persistence is **SQLite** (stdlib `sqlite3`). No ORM.
- External deps allowed only if active/reliable/maintained/popular: **flask, waitress, httpx** (runtime); **pytest, respx** (dev). Nothing else without justification. Everything else uses the stdlib.
- `domain/` must import nothing from `adapters/` or `web/`. Services depend on Protocols in `domain/ports.py`.
- TDD strict: write the failing test, watch it fail, minimal code, watch it pass, commit. Tests are the spec.
- Package name: `slskd_lidarr_bridge`. Tests under `tests/`.
- Every commit message ends with a trailer line: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## Canonical interfaces (shared contract — every task uses these exact names/types)

`domain/models.py` (frozen dataclasses unless noted):
```python
@dataclass(frozen=True)
class AudioFile:
    filename: str            # full remote path, backslash-separated, e.g. r"@@a\Music\Artist\Album\01.flac"
    size: int                # bytes
    extension: str | None = None     # ".flac", ".mp3" (lowercased, with dot) or None
    bitrate: int | None = None       # kbps if known
    length: int | None = None        # seconds if known
    @property
    def album_folder(self) -> str: ...   # last directory component of filename (split on "\" and "/")
    @property
    def is_audio(self) -> bool: ...      # extension in AUDIO_EXTENSIONS

@dataclass(frozen=True)
class SearchResponse:
    username: str
    has_free_upload_slot: bool
    upload_speed: int
    queue_length: int
    files: tuple[AudioFile, ...]

@dataclass(frozen=True)
class Transfer:
    username: str
    id: str
    filename: str
    size: int
    state: str               # comma-joined slskd flags, e.g. "Completed, Succeeded"
    bytes_transferred: int
    bytes_remaining: int
    percent_complete: float
    exception: str | None = None
    local_path: str | None = None    # set if slskd exposes the on-disk path; else None
    @property
    def is_complete(self) -> bool: ...   # "Completed" in state
    @property
    def is_succeeded(self) -> bool: ...  # is_complete and "Succeeded" in state
    @property
    def is_failed(self) -> bool: ...     # is_complete and not is_succeeded

@dataclass(frozen=True)
class SearchQuery:
    artist: str | None = None
    album: str | None = None
    term: str | None = None              # basic t=search q=
    @property
    def is_empty(self) -> bool: ...      # no artist, album, or term -> rss sync
    def to_search_text(self) -> str: ... # "Artist Album" / "Artist" / term

@dataclass(frozen=True)
class Release:
    artist: str
    album: str
    title: str
    username: str
    files: tuple[AudioFile, ...]
    size: int
    album_folder: str
    quality: str                          # "FLAC", "MP3-320", ...
    created_at: datetime
    id: str | None = None                 # set by ReleaseStore.put

@dataclass(frozen=True)
class DownloadJob:
    nzo_id: str
    title: str
    username: str
    files: tuple[AudioFile, ...]
    category: str
    album_folder: str
    total_size: int
    created_at: datetime

@dataclass(frozen=True)
class JobStatusView:
    nzo_id: str
    title: str
    category: str
    total_bytes: int
    transferred_bytes: int
    percent: float                        # 0..100
    state: str                            # "downloading" | "completed" | "failed"
    storage: str | None = None            # absolute final folder when completed
    fail_message: str | None = None
```

`domain/ports.py` (typing.Protocol, all `@runtime_checkable`):
```python
class SoulseekGateway(Protocol):
    def start_search(self, text: str) -> str: ...
    def search_is_complete(self, search_id: str) -> bool: ...
    def search_responses(self, search_id: str) -> list[SearchResponse]: ...
    def enqueue(self, username: str, files: list[AudioFile]) -> None: ...
    def transfers(self, username: str) -> list[Transfer]: ...
    def cancel(self, username: str, transfer_id: str) -> None: ...

class ReleaseStore(Protocol):
    def put(self, release: Release) -> str: ...           # returns assigned id
    def get(self, release_id: str) -> Release | None: ...
    def purge_older_than(self, cutoff: datetime) -> None: ...

class JobStore(Protocol):
    def add(self, job: DownloadJob) -> None: ...
    def get(self, nzo_id: str) -> DownloadJob | None: ...
    def list(self) -> list[DownloadJob]: ...
    def remove(self, nzo_id: str) -> None: ...

class Clock(Protocol):
    def now(self) -> datetime: ...
    def sleep(self, seconds: float) -> None: ...
```

NZB payload (carried in the self-describing NZB; `web/nzb.py`):
```python
# build_nzb(payload: dict) -> bytes ; parse_nzb(data: bytes) -> dict
# payload keys: "username": str, "title": str, "album_folder": str,
#               "total_size": int, "files": [ {"filename": str, "size": int} ]
```

---

### Task 1: Project scaffold + tooling

**Files:**
- Create: `pyproject.toml`, `src/slskd_lidarr_bridge/__init__.py`, `tests/__init__.py`, `tests/test_smoke.py`
- Create: package subdirs `src/slskd_lidarr_bridge/{domain,adapters,web}/__init__.py`

**Interfaces:**
- Produces: importable package `slskd_lidarr_bridge`; `uv run pytest` working.

- [ ] **Step 1:** Write `pyproject.toml` using uv/hatchling: project name `slskd-lidarr-bridge`, `requires-python = ">=3.12"`, dependencies `["flask>=3.0", "waitress>=3.0", "httpx>=0.27"]`, dev group `["pytest>=8", "respx>=0.21"]`. Configure `[tool.pytest.ini_options]` with `pythonpath = ["src"]` and `testpaths = ["tests"]`. Set `[tool.hatch.build.targets.wheel] packages = ["src/slskd_lidarr_bridge"]`.
- [ ] **Step 2:** Create the package dirs and empty `__init__.py` files (set `__version__` in the top one).
- [ ] **Step 3:** Write `tests/test_smoke.py`:
```python
def test_package_imports():
    import slskd_lidarr_bridge
    assert slskd_lidarr_bridge.__version__
```
- [ ] **Step 4:** Run `uv run pytest -q`. Expected: 1 passed. (uv resolves/creates the venv.)
- [ ] **Step 5:** Commit `feat: project scaffold (uv, package layout, pytest)`.

---

### Task 2: Domain models

**Files:**
- Create: `src/slskd_lidarr_bridge/domain/models.py`
- Test: `tests/domain/test_models.py`

**Interfaces:**
- Produces: every dataclass/property in the Canonical interfaces section. Define `AUDIO_EXTENSIONS = {".flac",".mp3",".m4a",".aac",".ogg",".opus",".wav",".alac",".wma",".ape"}`.

- [ ] **Step 1:** Write failing tests covering:
  - `AudioFile(filename=r"@@a\Music\Artist\Album Name\01 - x.flac", size=100).album_folder == "Album Name"`; also a `/`-separated path; a file at root returns `""`.
  - `AudioFile(..., extension=".flac").is_audio is True`; `".txt"` and `None` → False; extension match is case-insensitive (`".FLAC"`).
  - `Transfer(state="Completed, Succeeded", ...)`: `is_complete and is_succeeded and not is_failed`.
  - `Transfer(state="Completed, Errored")`: `is_complete and is_failed and not is_succeeded`.
  - `Transfer(state="InProgress")`: none of complete/succeeded/failed.
  - `SearchQuery(artist="A", album="B").is_empty is False` and `.to_search_text() == "A B"`; `SearchQuery().is_empty is True`; `SearchQuery(term="x").to_search_text() == "x"`.
- [ ] **Step 2:** Run `uv run pytest tests/domain/test_models.py -v`. Expected: import/attribute errors (FAIL).
- [ ] **Step 3:** Implement `models.py` minimally to satisfy the asserts. `album_folder`: normalize `\`→`/`, split, return parent dir name or `""`.
- [ ] **Step 4:** Run tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: domain models`.

---

### Task 3: Quality detection

**Files:**
- Create: `src/slskd_lidarr_bridge/domain/quality.py`
- Test: `tests/domain/test_quality.py`

**Interfaces:**
- Produces: `detect_quality(files: Sequence[AudioFile]) -> str`. Returns a Lidarr-parseable label.
- Consumes: `AudioFile`.

- [ ] **Step 1:** Write failing tests:
  - All `.flac` → `"FLAC"`.
  - `.mp3` with bitrate 320 → `"MP3-320"`; 256 → `"MP3-256"`; 192 → `"MP3-192"`; bitrate unknown → `"MP3"`.
  - Mixed formats → pick the predominant by file count; tie → prefer lossless (`FLAC` > `MP3`).
  - Empty list → `"Unknown"`.
  - `.m4a`/`.aac` → `"AAC"`; `.ogg`/`.opus` → `"OGG"`; `.wav` → `"WAV"`; `.alac` → `"ALAC"`.
- [ ] **Step 2:** Run tests → FAIL.
- [ ] **Step 3:** Implement: tally by extension among `is_audio` files, map to family, append bitrate bucket for MP3. Bitrate buckets: round down to nearest of {320,256,192,128} when within ±16; else bare `"MP3"`.
- [ ] **Step 4:** Run tests → PASS.
- [ ] **Step 5:** Commit `feat: quality detection`.

---

### Task 4: Release title building

**Files:**
- Create: `src/slskd_lidarr_bridge/domain/titles.py`
- Test: `tests/domain/test_titles.py`

**Interfaces:**
- Produces: `build_title(artist: str, album: str, quality: str) -> str`.

- [ ] **Step 1:** Write failing tests:
  - `build_title("Radiohead", "In Rainbows", "FLAC") == "Radiohead - In Rainbows [FLAC]"`.
  - `build_title("A/B", "C: D", "MP3-320")` strips path-hostile chars to `"A B - C D [MP3-320]"` (collapse whitespace, no leading/trailing spaces).
  - Empty quality → no trailing brackets: `build_title("A","B","") == "A - B"`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement: sanitize artist/album (replace `[\\/:*?"<>|]` with space, collapse spaces), assemble `f"{artist} - {album}"` + (f" [{quality}]" if quality else "").
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: scene-style title building`.

---

### Task 5: Storage path computation

**Files:**
- Create: `src/slskd_lidarr_bridge/domain/paths.py`
- Test: `tests/domain/test_paths.py`

**Interfaces:**
- Produces: `compute_storage_path(downloads_dir: str, remote_filename: str) -> str`.

**Note for implementer:** Verify slskd's on-disk layout from slskd docs/source (config.md "directories.downloads"; search/transfer handling). slskd preserves the **remote album folder** under the downloads dir. If slskd nests under username too, adjust the function AND its tests to match reality, and record the finding in a code comment. The contract below is the default assumption.

- [ ] **Step 1:** Write failing tests:
  - `compute_storage_path("/downloads", r"@@abc\Music\Artist\Album Name\01.flac") == "/downloads/Album Name"`.
  - `/`-separated remote path works too.
  - Trailing slash on downloads_dir is normalized (no `//`).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement using `posixpath`/`PurePosixPath`: normalize `\`→`/`, album folder = parent dir name of the file, join to downloads_dir.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: storage path computation` (mention the verified slskd layout in the commit body).

---

### Task 6: Ports (Protocols)

**Files:**
- Create: `src/slskd_lidarr_bridge/domain/ports.py`
- Test: `tests/domain/test_ports.py`

**Interfaces:**
- Produces: `SoulseekGateway`, `ReleaseStore`, `JobStore`, `Clock` exactly as in Canonical interfaces.

- [ ] **Step 1:** Write a failing test asserting the Protocols exist and are `runtime_checkable` (a trivial in-test class implementing the methods `isinstance`-matches the Protocol).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement the four Protocols.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: domain ports`.

---

### Task 7: Search service

**Files:**
- Create: `src/slskd_lidarr_bridge/domain/search_service.py`
- Test: `tests/domain/test_search_service.py`

**Interfaces:**
- Produces: `class SearchService` with `__init__(self, gateway: SoulseekGateway, store: ReleaseStore, clock: Clock, *, search_timeout: int = 30, poll_interval: float = 1.0, min_bitrate: int | None = None)` and `search(self, query: SearchQuery) -> list[Release]`.
- Consumes: `SoulseekGateway`, `ReleaseStore`, `Clock`, `detect_quality`, `build_title`, models.

**Behavior:** empty query → `[]` (no slskd call). Else: `sid = gateway.start_search(query.to_search_text())`; poll `search_is_complete(sid)` sleeping `poll_interval` via `clock.sleep`, advancing using `clock.now()` until complete or `search_timeout` elapsed; fetch `search_responses(sid)`; per response, keep `is_audio` files (and `bitrate>=min_bitrate` if set), group by `album_folder`; per group build a `Release` (artist/album from query — fall back to parsing the folder name when query has only `term`; size=sum; quality=`detect_quality`; title=`build_title`; created_at=`clock.now()`), `store.put` to get id, set on release; return releases ordered by `(has_free_upload_slot desc, upload_speed desc)`.

- [ ] **Step 1:** Write failing tests with a `FakeGateway` (scriptable: completes after N polls, returns canned responses), `FakeStore` (assigns incrementing ids), `FakeClock` (records sleeps, advances now). Cases:
  - empty query → `[]`, gateway never called.
  - one response with a full FLAC album in one folder → one Release: correct username, size=sum, quality `"FLAC"`, title via `build_title`, id assigned, files only the audio ones.
  - response with files across two folders → two Releases.
  - polling: gateway completes on 3rd check → `clock.sleep` called twice; responses fetched once.
  - timeout: never completes → after timeout, fetches whatever responses exist (assert it still returns/empties without infinite loop).
  - ordering: two responses, the free-slot/faster one first.
  - `min_bitrate` filters low-bitrate files out of the group.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `SearchService`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: search service (slskd search -> releases)`.

---

### Task 8: Download service

**Files:**
- Create: `src/slskd_lidarr_bridge/domain/download_service.py`
- Test: `tests/domain/test_download_service.py`

**Interfaces:**
- Produces: `class DownloadService` with `__init__(self, gateway: SoulseekGateway, jobs: JobStore, clock: Clock, *, downloads_dir: str)` and:
  - `start(self, payload: dict, category: str) -> str` — `payload` is a parsed NZB payload (see nzb keys). Enqueues files on slskd (`gateway.enqueue(username, files)`), mints `nzo_id = "SABnzbd_nzo_" + uuid4().hex[:12]`, stores a `DownloadJob`, returns `nzo_id`.
  - `statuses(self) -> list[JobStatusView]` — for each job: `transfers = gateway.transfers(job.username)`; match the job's files by `filename`; aggregate `total_bytes`, `transferred_bytes`, `percent`; if all matched transfers `is_succeeded` → state `"completed"`, `storage` = matched transfer `local_path` parent if present else `compute_storage_path(downloads_dir, files[0].filename)`; if any `is_failed` → `"failed"` with `fail_message` from the transfer `exception`; else `"downloading"`.
  - `remove(self, nzo_id: str) -> None` — if job exists: for each matching in-progress transfer, `gateway.cancel(username, transfer.id)`; then `jobs.remove(nzo_id)`. Unknown id → no-op.
- Consumes: `SoulseekGateway`, `JobStore`, `Clock`, `compute_storage_path`, models.

- [ ] **Step 1:** Write failing tests with fakes:
  - `start`: enqueues exact files on gateway, returns an `nzo_id` starting `"SABnzbd_nzo_"`, persists a `DownloadJob` with the category/title/username/total_size.
  - `statuses` downloading: transfers half-done → one view, state `"downloading"`, percent ~50, storage None.
  - `statuses` completed: all transfers `Completed, Succeeded` → state `"completed"`, storage equals computed path (and equals `local_path` parent when provided).
  - `statuses` failed: a transfer `Completed, Errored` with `exception="no slots"` → state `"failed"`, fail_message set.
  - `remove`: cancels the in-progress transfer ids then removes the job; unknown id is a no-op (no exception).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `DownloadService`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: download service (slskd transfers <-> jobs)`.

---

### Task 9: NZB build/parse (self-describing carrier)

**Files:**
- Create: `src/slskd_lidarr_bridge/web/nzb.py`
- Test: `tests/web/test_nzb.py`

**Interfaces:**
- Produces: `build_nzb(payload: dict) -> bytes`, `parse_nzb(data: bytes) -> dict`. Output is a valid NZB 1.1 XML document (root `<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">`) carrying the payload as base64-JSON inside `<head><meta type="x-slskd-payload">...</meta></head>`, plus one dummy `<file>` so generic NZB parsers don't choke.

- [ ] **Step 1:** Write failing tests:
  - round-trip identity: `parse_nzb(build_nzb(p)) == p` for a payload with two files, unicode in title, large sizes.
  - `build_nzb(p)` is parseable XML whose root localname is `nzb`.
  - `parse_nzb` on bytes missing the meta → raises `ValueError`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement with `xml.etree.ElementTree` + `base64` + `json`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: self-describing NZB carrier`.

---

### Task 10: Newznab XML builders

**Files:**
- Create: `src/slskd_lidarr_bridge/web/xml.py`
- Test: `tests/web/test_xml.py`

**Interfaces:**
- Produces:
  - `build_caps(categories: list[tuple[int, str]]) -> bytes` — `<caps>` advertising `search`(q) + `audio-search`(q,artist,album), `<limits max=100 default=100>`, and the given categories.
  - `build_results_rss(items: list[dict], channel_title: str = "slskd-bridge") -> bytes` — RSS 2.0 with `xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"`; each item dict has keys `title, guid, link, pubDate(datetime), size(int), category(int)`; renders `<title><guid><pubDate><enclosure url=link length=size type="application/x-nzb"/>` + `<newznab:attr name="size">` + `<newznab:attr name="category">`.
  - `build_error(code: int, description: str) -> bytes` — `<error code= description=/>`.

- [ ] **Step 1:** Write failing tests (parse output back with ElementTree and assert):
  - caps: root `caps`; `audio-search` has `available="yes"` and `supportedParams` containing `q,artist,album`; each category present with int id + name.
  - rss: one item → `enclosure@type == "application/x-nzb"`, `enclosure@url == link`, newznab size attr equals size, pubDate is RFC-822 formatted, guid present.
  - error: code/description attributes correct.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Format pubDate with `email.utils.format_datetime`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: newznab caps/results/error XML builders`.

---

### Task 11: slskd gateway adapter

**Files:**
- Create: `src/slskd_lidarr_bridge/adapters/slskd_gateway.py`
- Test: `tests/adapters/test_slskd_gateway.py`

**Interfaces:**
- Produces: `class SlskdGateway(SoulseekGateway)` with `__init__(self, base_url: str, api_key: str, *, client: httpx.Client | None = None, timeout: float = 30.0)`. Implements every `SoulseekGateway` method against `/api/v0/...` with header `X-API-Key`.
- Consumes: models, ports, httpx.

**Mapping (from spec):** `start_search` → `POST /api/v0/searches {searchText}` returns `id`. `search_is_complete` → `GET /api/v0/searches/{id}` reads `isComplete`. `search_responses` → `GET /api/v0/searches/{id}/responses` → map each user + `files[]`→`AudioFile` (extension lowercased with leading dot; `bitRate`→bitrate; `length`→length). `enqueue` → `POST /api/v0/downloads/{username}` body `[{filename,size}]`. `transfers` → `GET /api/v0/downloads/{username}` → flatten directories→`Transfer` (read `state, bytesTransferred, bytesRemaining, percentComplete, exception`; `local_path` from whichever field slskd exposes, else None). `cancel` → `DELETE /api/v0/downloads/{username}/{id}?remove=true`.

**Note for implementer:** Confirm the responses JSON shape and the `GET /downloads/{username}` grouping against slskd source (`Response.cs`, `File.cs`, `Transfer.cs`) before finalizing the parsing. Record any field name that differs from the spec in a comment.

- [ ] **Step 1:** Write failing tests with `respx` mocking httpx:
  - `start_search` posts `{"searchText":"x"}` with `X-API-Key` header to `/api/v0/searches`, returns the `id` from the response.
  - `search_is_complete` returns the `isComplete` bool.
  - `search_responses` parses two users with files into `SearchResponse`/`AudioFile` (extension `.flac`, size, bitrate, length).
  - `enqueue` posts the `[{filename,size}]` array to `/api/v0/downloads/<user>`.
  - `transfers` flattens the nested downloads payload into `Transfer` objects with correct state/bytes.
  - `cancel` issues DELETE with `remove=true`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `SlskdGateway`. Default-construct an `httpx.Client(base_url=base_url, headers={"X-API-Key": api_key})` when none injected.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: slskd REST gateway adapter`.

---

### Task 12: SQLite store adapter

**Files:**
- Create: `src/slskd_lidarr_bridge/adapters/sqlite_store.py`
- Test: `tests/adapters/test_sqlite_store.py`

**Interfaces:**
- Produces: `class SqliteStore(ReleaseStore, JobStore)` with `__init__(self, db_path: str)` (creates tables if absent; `db_path` may be `:memory:`). Implements both port method-sets. `put` assigns `id = uuid4().hex[:16]`. Serializes `files` as JSON. `created_at` stored ISO-8601.
- Consumes: models, ports, stdlib `sqlite3`, `json`, `uuid`.

- [ ] **Step 1:** Write failing tests against a `tmp_path` db (and one `:memory:`):
  - release `put` returns id; `get(id)` round-trips all fields incl. files tuple; `get("nope")` → None.
  - `purge_older_than(cutoff)` deletes older, keeps newer.
  - job `add`/`get`/`list`/`remove` round-trip; `get` unknown → None; `remove` unknown → no error; `list` returns all.
  - reopening a new `SqliteStore` on the same file path sees persisted rows.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Use `check_same_thread=False` and a module-level lock around writes (Flask+waitress is threaded). Two tables: `releases`, `jobs`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: sqlite store (releases + jobs)`.

---

### Task 13: System clock adapter

**Files:**
- Create: `src/slskd_lidarr_bridge/adapters/system_clock.py`
- Test: `tests/adapters/test_system_clock.py`

**Interfaces:**
- Produces: `class SystemClock(Clock)`: `now()` → `datetime.now(timezone.utc)`; `sleep(seconds)` → `time.sleep`.

- [ ] **Step 1:** Write failing test: `now()` returns a tz-aware datetime; `sleep(0)` returns without error. (Patch `time.sleep` to assert it's called with the arg.)
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: system clock adapter`.

---

### Task 14: Config

**Files:**
- Create: `src/slskd_lidarr_bridge/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class Config` with fields: `slskd_url: str`, `slskd_api_key: str`, `slskd_downloads_dir: str`, `bridge_api_key: str | None`, `categories: list[tuple[int,str]]`, `bridge_host: str`, `bridge_port: int`, `search_timeout: int`, `db_path: str`, `min_bitrate: int | None`. Classmethod `from_env(env: Mapping[str,str]) -> Config`.
- Default categories (when `BRIDGE_CATEGORIES` unset): `[(3000,"Audio"),(3010,"Audio/MP3"),(3030,"Audio/Audiobook"),(3040,"Audio/Lossless")]`. The SABnzbd `get_config` category **names** come from `BRIDGE_CATEGORIES` (default `["music"]`) — store these separately as `sab_categories: list[str]`.

**Correction to keep consistent:** add `sab_categories: list[str]` to `Config` (default `["music"]`, from `BRIDGE_CATEGORIES`). Indexer categories stay the fixed Newznab music set above. (Two different concepts: Newznab category IDs vs SABnzbd category names.)

- [ ] **Step 1:** Write failing tests:
  - full env → all fields parsed; `bridge_port` is int; `sab_categories` split on comma and trimmed.
  - missing `SLSKD_URL` (or API key / downloads dir) → `raise SystemExit`/`ValueError` with a message naming the missing var.
  - defaults: unset `BRIDGE_PORT`→8765, `SEARCH_TIMEOUT`→30, `DB_PATH`→`/data/bridge.db`, `BRIDGE_API_KEY`→None, `MIN_BITRATE`→None, `BRIDGE_HOST`→`0.0.0.0`, `sab_categories`→`["music"]`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `from_env`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: env config`.

---

### Task 15: Newznab blueprint

**Files:**
- Create: `src/slskd_lidarr_bridge/web/newznab.py`
- Test: `tests/web/test_newznab.py`

**Interfaces:**
- Produces: `def create_newznab_blueprint(search_service: SearchService, release_store: ReleaseStore, *, api_key: str | None, categories: list[tuple[int,str]]) -> flask.Blueprint`. Routes under `/indexer`:
  - `GET /indexer/api`: dispatch on `t`. `t=caps`→caps XML. `t=search`→`SearchQuery(term=q)`. `t=music`→`SearchQuery(artist=..., album=..., term=q or None)`. Build releases via `search_service.search`; render RSS where each item's `link`/enclosure url = `url_for nzb route` (absolute, from request host) and guid = release id, pubDate = release.created_at, size, category 3040/3010 by quality. Missing/empty query terms with `t=music`/`t=search` → empty channel (rss sync).
  - `GET /indexer/nzb/<release_id>`: load release; 404 if unknown; else `build_nzb(payload)` with `Content-Type: application/x-nzb` and a `Content-Disposition` filename.
  - If `api_key` set, requests missing/!= `apikey` query param → `build_error(100,"Invalid API key")`.
- Consumes: `SearchService`, `ReleaseStore`, `build_caps/build_results_rss/build_error`, `build_nzb`.

- [ ] **Step 1:** Write failing tests with Flask test client and a fake `SearchService`/store:
  - `t=caps` → 200, `Content-Type` xml, body has `audio-search available="yes"`.
  - `t=music&artist=A&album=B` → calls service with `SearchQuery(artist="A",album="B")`; response RSS contains one item whose enclosure url ends `/indexer/nzb/<id>` and type `application/x-nzb`.
  - `t=music` with no terms → empty channel (no `<item>`), service not called.
  - `/indexer/nzb/<id>` for a known release → 200 `application/x-nzb`, body parses back to the payload; unknown id → 404.
  - api_key set: missing `apikey` → error XML with code 100; correct `apikey` → normal.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement the blueprint.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: newznab indexer blueprint`.

---

### Task 16: SABnzbd blueprint

**Files:**
- Create: `src/slskd_lidarr_bridge/web/sabnzbd.py`
- Test: `tests/web/test_sabnzbd.py`

**Interfaces:**
- Produces: `def create_sabnzbd_blueprint(download_service: DownloadService, *, api_key: str | None, categories: list[str], complete_dir: str) -> flask.Blueprint`. Route `/<any>/api` mounted at `/sabnzbd` (so `GET|POST /sabnzbd/api`). Dispatch on `mode`:
  - `version` → `{"version":"4.3.0"}` (no api_key required).
  - `get_config` → `{"config":{"misc":{"complete_dir":complete_dir,...},"categories":categories}}`.
  - `fullstatus` → `{"status":{...}}` (minimal).
  - `addfile` (POST multipart, file field `name`) → `parse_nzb(file bytes)` → `download_service.start(payload, category=request.form.get("cat",""))` → `{"status":true,"nzo_ids":[nzo_id]}`.
  - `queue` → slots from `download_service.statuses()` where state == `"downloading"`: `{nzo_id, filename=title, status:"Downloading", mb, mbleft, percentage, cat, timeleft:"0:00:00", index}` filtered by `category` query param if present.
  - `history` → slots where state in `{"completed","failed"}`: `{nzo_id, name=title, nzb_name, status:"Completed"|"Failed", storage, category, fail_message, bytes}` filtered by `category` if present.
  - delete: `mode=queue&name=delete&value=<id>` or `mode=history&name=delete&value=<id>` → `download_service.remove(id)` → `{"status":true}`.
  - If `api_key` set, any mode except `version` with missing/wrong `apikey` → `{"status":false,"error":"API Key Incorrect"}`.
- Consumes: `DownloadService`, `parse_nzb`, `JobStatusView`.

- [ ] **Step 1:** Write failing tests with Flask test client + fake `DownloadService`:
  - `mode=version` → `{"version": ...}`.
  - `mode=get_config` → categories list includes the configured ones; `complete_dir` present.
  - `mode=addfile` with a multipart NZB (built by `build_nzb`) and `cat=music` → service.start called with parsed payload + category, response `nzo_ids=[...]`.
  - `mode=queue` → only downloading jobs become slots with `mb`/`mbleft`/`percentage`/`cat`.
  - `mode=history` → completed job slot has `storage` and `status:"Completed"`; failed job has `status:"Failed"` + `fail_message`.
  - `mode=queue&name=delete&value=X` → service.remove("X") called, `{"status":true}`.
  - api_key set: wrong key on `mode=queue` → `{"status":false,...}`; `mode=version` still works without key.
  - category filter: `&category=music` returns only matching slots.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. `mb = bytes/1024/1024`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: sabnzbd download-client blueprint`.

---

### Task 17: App factory + composition

**Files:**
- Create: `src/slskd_lidarr_bridge/web/app.py`
- Test: `tests/web/test_app.py`

**Interfaces:**
- Produces: `def create_app(config: Config, gateway: SoulseekGateway, store, clock: Clock) -> flask.Flask` where `store` implements both `ReleaseStore`+`JobStore`. Builds `SearchService` and `DownloadService`, registers both blueprints, adds a JSON/XML-safe error handler (no stack traces; indexer-side returns `build_error(900,...)`, sab-side returns `{"status":false}`), and a `GET /health` → `{"status":"ok"}`.
- Consumes: everything above.

- [ ] **Step 1:** Write failing tests:
  - `create_app(...)` with fakes → test client: `/health` 200; `/indexer/api?t=caps` 200 xml; `/sabnzbd/api?mode=version` 200 json.
  - an exception raised inside a service is converted to an error response, not a 500 stack trace (force a fake to raise; assert indexer returns error XML / sab returns `status:false`).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `create_app`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: flask app factory + wiring`.

---

### Task 18: Entrypoint

**Files:**
- Create: `src/slskd_lidarr_bridge/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Produces: `def build_app(env: Mapping[str,str]) -> flask.Flask` (loads `Config.from_env`, constructs `SlskdGateway`, `SqliteStore`, `SystemClock`, `create_app`) and `def main() -> None` (build from `os.environ`, `waitress.serve(app, host=config.bridge_host, port=config.bridge_port)`). `__main__` guard calls `main()`.
- Consumes: config, adapters, app factory, waitress.

- [ ] **Step 1:** Write failing test: `build_app({...valid env with db_path=tmp...})` returns a Flask app whose test client answers `/health` 200. (Do not start waitress in the test.)
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: entrypoint (build_app + waitress serve)`.

---

### Task 19: End-to-end flow test

**Files:**
- Test: `tests/e2e/test_flow.py`

**Interfaces:**
- Consumes: real `create_app` + real services + real `SqliteStore` (tmp) + `SystemClock` (or a fake clock for no sleeps) + a **fake `SoulseekGateway`** whose transfers progress over calls.

- [ ] **Step 1:** Write the e2e test:
  1. `t=caps` → 200, advertises audio-search.
  2. `t=music&artist=A&album=B` → RSS with ≥1 item; capture the nzb url + guid.
  3. GET the nzb url → `application/x-nzb` bytes.
  4. POST those bytes to `/sabnzbd/api?mode=addfile` (multipart, `cat=music`) → capture `nzo_ids[0]`.
  5. Fake gateway now reports the transfer in-progress → `mode=queue` shows the slot with that nzo_id; `mode=history` empty.
  6. Fake gateway flips transfers to `Completed, Succeeded` → `mode=history` shows the slot with `status:"Completed"` and a `storage` path; `mode=queue` empty.
  7. `mode=history&name=delete&value=<nzo_id>` → `{"status":true}`; subsequent history empty.
- [ ] **Step 2:** Run → FAIL (until wiring is right), then iterate.
- [ ] **Step 3:** Make it pass (no production code changes beyond bug fixes surfaced here).
- [ ] **Step 4:** Run full suite `uv run pytest -q`. Expected: all green.
- [ ] **Step 5:** Commit `test: end-to-end lidarr lifecycle`.

---

### Task 20: Docker + README

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `README.md`

**Interfaces:**
- Produces: a runnable image; docs for env vars + Lidarr setup.

- [ ] **Step 1:** Write `.dockerignore` (`.venv`, `.git`, `__pycache__`, `tests`, `docs`, `*.db`, `.pytest_cache`).
- [ ] **Step 2:** Write multi-stage `Dockerfile`: builder `FROM python:3.12-slim` with uv installed (`COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv`), `uv sync --no-dev --frozen` into `/app/.venv`; runtime stage copies `/app` and runs `CMD ["/app/.venv/bin/python","-m","slskd_lidarr_bridge.main"]`. Expose `8765`. Create `/data`.
- [ ] **Step 3:** Build: `docker build -t slskd-lidarr-bridge .`. Expected: success. (If docker is unavailable in the environment, validate the Dockerfile syntax with `hadolint` or a dry parse and note it.)
- [ ] **Step 4:** Write `README.md`: what it is, the env-var table (from the spec), `docker run`/compose example mounting the shared downloads volume, and **Lidarr setup**: add a **Newznab** indexer URL `http://bridge:8765/indexer` api path `/api` (+ apikey if set); add a **SABnzbd** download client host `bridge` port `8765` URL base `/sabnzbd` api key (if set) category `music`. Note the shared `SLSKD_DOWNLOADS_DIR` volume requirement and Remote Path Mapping.
- [ ] **Step 5:** Commit `feat: docker image + README (lidarr setup)`.

---

## Self-Review notes
- Spec coverage: caps/search/nzb (T10,T15,T9), SABnzbd modes incl. storage (T16,T8), slskd API (T11), persistence (T12), config/env (T14), error handling (T17), e2e (T19), docker (T20). ✓
- `sab_categories` vs Newznab category IDs reconciled in T14. ✓
- Storage-path layout flagged for real-slskd verification in T5 + T11. ✓
- NZB is the self-describing carrier (no release lookup needed at addfile time); ReleaseStore is only for the indexer→nzb GET step. Both persisted in SQLite (T12). ✓
