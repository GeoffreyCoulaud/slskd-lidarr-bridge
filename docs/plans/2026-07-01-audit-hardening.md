# Audit hardening — implementation plan

Executes the fixes agreed for the security/quality audit (findings #1–#10).
Branch `fix/audit-hardening` off `main`. Sequential tasks, one feature branch.

## Context

`slskd-lidarr-bridge` impersonates a Newznab indexer + SABnzbd download client
in front of slskd so Lidarr drives Soulseek. Hexagonal architecture; see
[AGENTS.md](../../AGENTS.md). This plan applies ten audited fixes.

## Global Constraints (bind every task)

- **Gate, per commit:** `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`, `uv run pytest` — the last enforces **100% line+branch
  coverage** (`fail_under = 100`, `branch = true`). Every commit stays green.
- `from __future__ import annotations` at the top of every Python module.
- **All env parsing lives in `config.py`** — no `os.environ` elsewhere. New env
  vars are documented in **both** `config.py` and the README table.
- Ports are `@runtime_checkable` `Protocol`s. Adapters satisfy them structurally.
- **Error envelopes return HTTP 200** (Newznab XML `<error>` / SABnzbd
  `{"status": false}`), never an HTTP error status.
- Tests use fakes for the ports and `respx` to stub slskd HTTP; mirror the
  existing patterns under `tests/`.
- Strict mypy, no `# type: ignore` to dodge real errors. Ruff `E,F,I,B,UP`,
  line length 88.
- TDD: write the failing test first, then implement. Keep test output pristine.
- Conventional-commit messages, small focused commits.

## Out of scope (do not touch)

Multi-disc limitation; the reactive/no-background-worker architecture;
migrating in-memory download state to the DB; a second API key; PUID/PGID
remap; any `min_bitrate` behaviour change; payload type-validation.

---

### Task 1: Encode slskd URL path segments via a single chokepoint (#1)

**Files:** Modify `src/slskd_lidarr_bridge/adapters/outbound/slskd_gateway.py`;
extend `tests/adapters/outbound/test_slskd_gateway.py`.

**Problem.** `username`, `search_id`, `transfer_id` are interpolated raw into
slskd URL paths. `username` is fully attacker-controlled (it comes from the NZB
payload submitted to the unauthenticated `addfile` endpoint). httpx normalises
`..` segments, so a crafted username traverses to other slskd paths, and `?`/`#`
inject query/fragment — using the bridge's privileged `X-API-Key`. It is also a
plain robustness bug: a real Soulseek username with a space/`?`/`#` breaks the
request today.

**Approach — systemic, not per-call-site.** Add one private request helper that
percent-encodes every path segment, and route ALL requests through it. No call
site builds a URL string any more; they pass segments.

- [ ] **Step 1 (RED).** Add tests asserting that path segments are encoded.
  Use `respx` (existing pattern) to assert the request URL path. Cover:
  - `enqueue("../../api/v0/options", …)` → request path is
    `/api/v0/transfers/downloads/..%2F..%2Fapi%2Fv0%2Foptions` (the `..` and
    `/` are percent-encoded, NOT traversed — assert the path does not equal the
    traversed `/api/v0/api/v0/options`).
  - `transfers("user?remove=true")` → `?`/`=` encoded, single GET to the
    downloads path, no injected query param.
  - A normal username/transfer_id/search_id still round-trips (e.g.
    `start_search` posts to `/api/v0/searches`, `search_is_complete("abc")`
    GETs `/api/v0/searches/abc`).
  Run the focused test, confirm it fails against current code.

- [ ] **Step 2 (GREEN).** Implement the chokepoint and convert call sites:

  ```python
  from urllib.parse import quote

  def _req(self, method: str, *segments: str, **kwargs: object) -> httpx.Response:
      path = "/".join(quote(s, safe="") for s in segments)
      return self._client.request(method, f"/{path}", **kwargs)
  ```

  Convert every call: `start_search`, `search_is_complete`, `search_responses`,
  `enqueue`, `transfers`, `cancel`, `downloads_directory` to pass segments
  (`"api", "v0", "transfers", "downloads", username`, …) instead of building
  the URL string. Static segments are encoded too (harmless — no reserved
  chars). Keep `params=`/`json=` passing through `**kwargs`. Preserve existing
  behaviour: `transfers` still treats 404 as `[]`; `cancel` still passes
  `params={"remove": "true"}`. `request()` does not raise on 404, so the
  existing `r.status_code == 404` check still works.

- [ ] **Step 3.** Full gate green, commit:
  `fix(gateway): percent-encode slskd URL path segments via single chokepoint`.

---

### Task 2: DownloadService thread-safety + terminal-age purge + transfers batching (#4, #5a, #5b)

**Files:** Modify `src/slskd_lidarr_bridge/domain/download_service.py`; extend
`tests/domain/test_download_service.py`.

This task changes shared-state handling, adds a purge, and de-duplicates slskd
calls. All three live in `statuses()`/`remove()`.

**#4 — thread-safety (atomic check-and-increment, I/O outside the lock).**
waitress is multi-threaded; Lidarr polls `queue` and `history` concurrently, so
two threads run `statuses()` at once and race on `_retries`, `_progress`,
`_logged_terminal`. Add `self._lock = threading.Lock()`. The retry decision must
be atomic WITHOUT holding the lock across the `enqueue()` network call:

```python
with self._lock:
    used = self._retries.get(job.nzo_id, 0)
    do_retry = used < self._max_retries
    if do_retry:
        self._retries[job.nzo_id] = used + 1          # reserve immediately
        self._progress[job.nzo_id] = (transferred_bytes, now)
if do_retry:
    self._gateway.enqueue(job.username, retry_files)   # I/O OUTSIDE the lock
```

The stall-watermark read-modify-write on `_progress` and the `_logged_terminal`
check-and-add are likewise done under the lock; the actual `logger` call stays
outside it (logging is already thread-safe). `remove()`'s in-memory cleanup
(`_logged_terminal`/`_progress`/`_retries`) runs under the same lock; its
`transfers()`/`cancel()` I/O stays outside. **No slskd I/O while holding the
lock.**

**#5a — terminal-age purge.** Change `_logged_terminal: set[str]` to
`_terminal_since: dict[str, datetime]` (nzo_id → first instant seen terminal).
It still drives the log-once behaviour (presence = already logged) AND the
purge. In `statuses()`:
- `failed` jobs whose terminal age (`now - _terminal_since[nzo_id]`) exceeds a
  `_failed_purge_seconds` threshold (default **86400** = 24h) are purged:
  `self._jobs.remove(nzo_id)` plus the in-memory cleanup, and they are **not**
  included in the returned views.
- `completed` jobs are **never** auto-purged (rely on Lidarr's explicit delete).
- `downloading` jobs are never purged.
Record the terminal instant under the lock when a job first becomes terminal
(both completed and failed get a timestamp for log-once; only `failed` is
purged by age). The timestamp is in-memory — it resets on restart, which only
restarts a terminal job's purge clock; acceptable, mirrors existing in-memory
state. Add `_failed_purge_seconds` as a constructor kwarg defaulting to 86400
(no env var, no config change — keep it an internal default).

**#5b — batch transfers() by username.** `transfers()` is per-user and returns
the same data regardless of which job asks. Fetch once per distinct username at
the start of `statuses()` (outside the lock) and reuse:

```python
jobs = self._jobs.list()
transfers_by_user = {u: self._gateway.transfers(u) for u in {j.username for j in jobs}}
```

Pure dedup, behaviour identical.

- [ ] **Step 1 (RED).** Add tests, each failing first:
  - Concurrency/atomicity: a job with a failed transfer and `max_retries=1`
    re-enqueues **exactly once** even when `statuses()` is invoked twice in a
    way that simulates the race (e.g. assert the reserve-then-enqueue ordering:
    after one `statuses()` the retry count is reserved so a second concurrent
    pass would not re-enqueue). At minimum assert single enqueue and that no
    `enqueue`/`transfers`/`cancel` is called while the lock is held (a fake
    gateway whose methods assert `not self._lock.locked()` is a clean way).
  - Purge: a `failed` job older than 24h terminal is dropped from the next
    `statuses()` and removed from the job store; a `failed` job younger than
    24h is retained; a `completed` job of any age is retained.
  - Batching: with two jobs sharing one username, `transfers()` is called once
    per `statuses()` (spy/count on the fake gateway).
  - Existing retry/stall/completion tests still pass unchanged.

- [ ] **Step 2 (GREEN).** Implement the lock, the purge, and the batching.
  Keep `statuses()` readable; extract small private helpers if it clarifies.

- [ ] **Step 3.** Full gate green, commit (one or a few focused commits, e.g.
  `fix(download): lock retry state, purge stale failed jobs, batch transfers`).

---

### Task 3: SABnzbd dedup + generic error envelopes with correlation id (#10a, #8)

**Files:** Modify `src/slskd_lidarr_bridge/adapters/inbound/sabnzbd.py` and
`src/slskd_lidarr_bridge/adapters/inbound/app.py`; extend
`tests/adapters/inbound/test_sabnzbd.py` and `tests/adapters/inbound/test_app.py`.

**#10a — dedup.** The `queue` and `history` modes repeat verbatim the
`name=delete` handling (read `value` → `download_service.remove(value)` →
`{"status": True}`) and the `cat_filter` read. Extract a small private helper
for the delete sub-action and one for the category filter; call both from
`queue` and `history`. Do not change behaviour or response shapes.

**#8 — generic error envelopes + correlation id.** In `app.py`'s
`handle_exception`, stop returning `str(e)` to the client. Generate a short
correlation id (`uuid4().hex[:8]`), log it WITH the existing
`logger.exception(...)` (so the traceback and id are together server-side), and
return a generic message carrying only the id:
- `/indexer/*` → `build_error(900, f"Internal error (ref: {eid})")` at 200.
- everything else → `{"status": False, "error": f"internal error (ref: {eid})"}`
  at 200.
HTTPExceptions (404/405) still re-raise unchanged. Intentional errors that do
not flow through this handler (e.g. `build_error(202, …)`) are untouched.

- [ ] **Step 1 (RED).** Tests first:
  - dedup: deleting via `mode=queue&name=delete` and
    `mode=history&name=delete` both call `remove` and return `{"status": True}`
    (behaviour preserved); category filtering still works for both modes.
  - error envelope: force an unhandled exception (e.g. a fake service that
    raises) on an `/indexer/*` path and a `/sabnzbd/*` path; assert HTTP 200,
    that the body does NOT contain the original exception text, and that it
    contains a `ref:` id. Assert the server log record contains the same id.
- [ ] **Step 2 (GREEN).** Implement.
- [ ] **Step 3.** Full gate green, commit(s), e.g.
  `refactor(sabnzbd): extract delete + category-filter helpers` and
  `fix(app): generic error envelope with correlation id`.

---

### Task 4: Optional API-key auth + compose port hygiene (#2)

**Files:** Modify `src/slskd_lidarr_bridge/config.py`,
`src/slskd_lidarr_bridge/adapters/inbound/app.py`,
`src/slskd_lidarr_bridge/adapters/inbound/newznab.py`,
`src/slskd_lidarr_bridge/adapters/inbound/sabnzbd.py`, `README.md`; extend the
matching tests under `tests/`.

**Single optional key.** Add `BRIDGE_API_KEY` to `config.py`
(`api_key: str | None`, value from `env.get("BRIDGE_API_KEY")` normalised so
empty/whitespace → `None`). **Blank/unset → no auth (current behaviour
unchanged).**

**Enforcement.** When a key is configured, require it on the Newznab and
SABnzbd surfaces. Lidarr sends it as the `apikey` query parameter on both; for
the SABnzbd `addfile` POST read it from query and form. Implement a
`before_request` check on each blueprint (the `/health` route is on the app, so
it stays exempt). Compare with `hmac.compare_digest` (constant time).
- Newznab failure → `build_error(100, "Incorrect API key")` (Newznab code 100 =
  bad credentials), `content_type="application/xml"`, HTTP 200.
- SABnzbd failure → `{"status": False, "error": "API Key Incorrect"}`, HTTP 200.

**NZB download URL.** The `/indexer/nzb/<id>` route is under the Newznab
blueprint, so the `before_request` guards it too. The enclosure/sentinel URLs
are generated by the bridge via `url_for(...)`; when a key is configured,
append `apikey=<key>` to those generated URLs so Lidarr's grab carries it. Pass
the configured key into `create_newznab_blueprint` for this.

**Compose hygiene.** In the README compose example, remove the host port
publish (`ports: - "8765:8765"`) and instead set `BRIDGE_PORT` explicitly in the
`environment:` block to document the port without exposing it. Add a
`BRIDGE_API_KEY` row to the env-var table (optional; blank = no auth; set the
same value in Lidarr's SABnzbd and Newznab API-key fields).

Wire the key from `config` through `create_app` to both blueprints.

- [ ] **Step 1 (RED).** Tests first:
  - No key configured → all current requests still succeed (no `apikey`
    needed) — regression guard.
  - Key configured → Newznab `caps`/`search` without/with wrong `apikey` →
    `build_error(100,…)` XML at 200; with correct `apikey` → normal response.
  - Key configured → SABnzbd `version`/`addfile`/`queue` without/with wrong
    key → `{"status": False, "error": "API Key Incorrect"}`; addfile reads the
    key from query AND form.
  - Key configured → generated enclosure/sentinel URLs contain `apikey=`; the
    `/indexer/nzb/<id>` route accepts the key and serves the NZB.
  - `/health` works without a key even when one is configured.
  - `Config.from_env`: `BRIDGE_API_KEY` present/absent/whitespace → expected
    `api_key`.
- [ ] **Step 2 (GREEN).** Implement config, wiring, both `before_request`
  guards, URL key embedding, README.
- [ ] **Step 3.** Full gate green, commit(s), e.g.
  `feat(auth): optional BRIDGE_API_KEY on both Lidarr surfaces` and
  `docs(readme): document BRIDGE_API_KEY, drop host port publish`.

---

### Task 5: Documentation/comment fixes (#3, #9)

**Files:** Modify `src/slskd_lidarr_bridge/adapters/inbound/nzb.py` (comment
only) and `README.md`.

**#3 — comment only.** In `parse_nzb`, add a short comment documenting that the
bridge relies on the stdlib/Expat built-in entity-amplification protection
(active by default in CPython; the deployment image `python:3.13-slim` ships
Expat ≥ 2.8) against "billion laughs", and that the stdlib refuses external
entities (no XXE file read). No code/behaviour change.

**#9 — README `min_bitrate`.** Clarify the `BRIDGE_MIN_BITRATE` row: only files
with a **known** bitrate below the threshold are filtered out; lossless and
unknown-bitrate files always pass (slskd's bitrate field is often absent,
especially for FLAC). No behaviour change.

- [ ] **Step 1.** Edit the comment and the README row.
- [ ] **Step 2.** Gate green (docs/comment only; coverage unaffected). Verify
  the README text changed (`grep`).
- [ ] **Step 3.** Commit: `docs: clarify min_bitrate filtering and NZB XML safety`.

---

### Task 6: Dockerfile non-root user + healthcheck (#6)

**Files:** Modify `Dockerfile`; update `README.md` (bind-mount note).

**Non-root.** Run as the existing `nobody` user (UID/GID 65534). After creating
`/data`, `chown` it to `nobody:nogroup` (65534:65534), then add `USER nobody`
before `CMD`. The venv at `/app` is world-readable from the `COPY`, so `nobody`
can execute it. Document in the README that **bind-mount** users must
`chown 65534:65534` their host data dir (named volumes inherit the image
ownership automatically).

**Healthcheck.** Add a `HEALTHCHECK` using the venv python (no curl/wget in
slim), reading `BRIDGE_PORT` at runtime:

```dockerfile
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["/app/.venv/bin/python","-c","import os,urllib.request; urllib.request.urlopen('http://localhost:%s/health' % os.environ.get('BRIDGE_PORT','8765'))"]
```

- [ ] **Step 1.** Edit the Dockerfile (user creation/chown/USER + HEALTHCHECK)
  and the README bind-mount note.
- [ ] **Step 2.** Verify the image builds and runs as non-root and the
  healthcheck endpoint responds. If Docker is available:
  `docker build -t bridge-test .` then run with required env and
  `docker inspect` / `id` inside the container; otherwise lint the Dockerfile
  and state what could not be executed. The Python gate is unaffected.
- [ ] **Step 3.** Commit: `build(docker): run as nobody and add healthcheck`.

---

### Task 7: Supply-chain hardening — pinning, Dependabot, Syft/cosign SBOM, Grype + VEX (#7)

**Files:** Modify `Dockerfile`, `.github/workflows/checks.yml`,
`.github/workflows/release.yml`, `.github/workflows/pr.yml`; add
`.github/dependabot.yml`, `.github/workflows/grype-scan.yml`, `.grype.yaml`,
`security/vex.openvex.json`, `SECURITY.md`.

**7.1 Pinning.** Digest-pin base images: `python:3.13-slim` and
`ghcr.io/astral-sh/uv:0.8.11` get `@sha256:<digest>` appended (resolve the
current digests; record them). SHA-pin third-party GitHub Actions (the
`docker/*`, `astral-sh/*`, `anchore/*`, `sigstore/*` ones) to commit SHAs with a
trailing version comment. First-party `actions/*` may stay on major tags.

**7.2 Dependabot.** Add `.github/dependabot.yml` covering ecosystems: `uv`
(Python deps / `uv.lock`), `docker` (base-image digests), and `github-actions`
(action SHAs). Weekly schedule. Confirm the exact `uv` package-ecosystem key
against current Dependabot docs.

**7.3 Anchore chain.**
- *Build (release.yml, docker job, after push):* generate a single SBOM by
  scanning the **final pushed image** with Syft (`anchore/sbom-action`, pinned)
  — it catalogues both base-OS (dpkg) and Python deps (dist-info); **no
  lockfile/base merge**. Attach the SBOM to the image as a signed attestation
  with cosign **keyless (OIDC)** (`sigstore/cosign-installer` +
  `cosign attest --type cyclonedx`/spdx). Needs job `permissions:` with
  `id-token: write` and `packages: write`.
- *Daily scan (new `grype-scan.yml`, `schedule:` cron each morning + manual
  `workflow_dispatch`):* pull the SBOM attestation for the latest image and run
  Grype against the SBOM (`anchore/scan-action`), applying the VEX document, and
  upload SARIF to GitHub code scanning (`github/codeql-action/upload-sarif`).
  Needs `security-events: write`. Do not fail the job on findings (SARIF is the
  signal).
- *VEX system:* `security/vex.openvex.json` (a valid empty-but-well-formed
  OpenVEX doc to start), `.grype.yaml` referencing it so CI and local Grype
  apply it identically, and `SECURITY.md` documenting the triage process (a
  non-exploitable CVE → add an OpenVEX `not_affected` statement with a vocab
  justification via `vexctl`, in a PR, rather than ignoring).

**Validation.** These are CI/config files; the Python gate (`pytest`/coverage)
is unaffected and must still pass. Validate YAML/JSON syntax (e.g.
`python -c "import yaml,json; …"`). Pin action SHAs and image digests to real
current values; if a digest/SHA cannot be resolved in the environment, state so
in the report rather than inventing one.

- [ ] **Step 1.** Pinning (Dockerfile digests + Actions SHAs across all
  workflows).
- [ ] **Step 2.** `.github/dependabot.yml`.
- [ ] **Step 3.** release.yml Syft+cosign attest steps.
- [ ] **Step 4.** `grype-scan.yml`, `.grype.yaml`, `security/vex.openvex.json`,
  `SECURITY.md`.
- [ ] **Step 5.** Validate syntax; full Python gate green; commit(s), e.g.
  `ci(supply-chain): pin digests/SHAs, Dependabot, Syft+cosign SBOM, Grype+VEX`.
