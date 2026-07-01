# slskd-lidarr-bridge

A lightweight bridge that makes [slskd](https://github.com/slsknet/slskd) (a Soulseek daemon) look like Usenet to [Lidarr](https://lidarr.audio/). It impersonates a **Newznab indexer** and a **SABnzbd download client**, so Lidarr searches, downloads, and tracks music from Soulseek using its normal Usenet workflow.

> **A note on authorship:** Almost all of the code in this repository was written by AI coding agents, under human direction and review. The conventions and architectural guardrails those agents follow are documented in [AGENTS.md](AGENTS.md).

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SLSKD_URL` | yes | - | Base URL of the slskd instance (e.g. `http://slskd:5030`) |
| `SLSKD_API_KEY` | yes | - | API key for slskd authentication |
| `BRIDGE_PORT` | no | `8765` | TCP port for the bridge HTTP server |
| `SLSKD_SEARCH_TIMEOUT` | no | `15` | slskd's **idle** search window in seconds, forwarded as `searchTimeout` (in ms). slskd completes a search after this many seconds with **no new response** — the timer *resets on every response*, so it is NOT a wall-clock cap and must stay small (never the whole budget, or a busy query never completes). `0` omits it so slskd uses its own default (15 s); positive values must be ≥ slskd's 5 s minimum |
| `SLSKD_RESPONSE_LIMIT` | no | `100` | `responseLimit` sent on every search so a popular query — whose idle timer keeps resetting as peers reply — completes once this many peers have responded, instead of running until `BRIDGE_SEARCH_BUDGET`. `0` omits it (slskd default 250) |
| `BRIDGE_DB_PATH` | no | `/data/bridge.db` | Path to the SQLite database file |
| `BRIDGE_MIN_BITRATE` | no | _(none)_ | Minimum acceptable bitrate in kbps; only files with a **known** bitrate below this threshold are filtered out. Lossless and unknown-bitrate files always pass (slskd's bitrate field is often absent, especially for FLAC) |
| `BRIDGE_MIN_RESULTS` | no | `3` | Stop issuing further (looser) fallback search queries once this many distinct releases have accumulated. The primary query always runs |
| `BRIDGE_SEARCH_BUDGET` | no | `75` | Total wall-clock budget for the whole search across all candidates. It is the bridge's **poll cap** — how long it waits for each candidate's `isComplete` (completion itself is driven by `SLSKD_SEARCH_TIMEOUT` + `SLSKD_RESPONSE_LIMIT`, not by this value). Keeps total search latency under Lidarr's ~100 s indexer-request abort, so keep it well under 100 |
| `BRIDGE_STALL_TIMEOUT` | no | `1800` | Seconds a download may make **no progress** before the bridge reports it failed, so Lidarr stops waiting on a dead/offline peer and can try another release. `0` disables the check |
| `BRIDGE_MAX_RETRIES` | no | `1` | Times a failed transfer is re-enqueued on slskd before the download is reported failed to Lidarr (Soulseek transfers fail transiently). `0` fails on the first error |
| `BRIDGE_API_KEY` | no | _(none)_ | Shared API key for the Newznab indexer and SABnzbd surfaces. Blank or unset = no authentication required. When set, configure the **same value** in both Lidarr's Newznab and SABnzbd API-key fields |
| `LOG_LEVEL` | no | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`); applies to the bridge and its dependencies (e.g. httpx) |

> **slskd API key role:** create the key in slskd with the **`readwrite`** role — the bridge issues reads (search, status) and writes (enqueue/cancel downloads). As of slskd `0.25.x` the endpoints the bridge calls only require an authenticated key (the `Any` policy — no specific role is enforced yet), so any role technically works, but `readwrite` is the correct, future-proof choice for a client that writes.

## Docker Compose

The bridge discovers slskd's completed-downloads directory from slskd's API, so it needs no downloads volume of its own — it only reports paths to Lidarr, it never reads the files. The shared volume is between **slskd and Lidarr** (see [Completed-downloads path](#3-completed-downloads-path) below).

```yaml
services:
  slskd:
    image: slsknet/slskd:latest
    volumes:
      - downloads:/downloads
    environment:
      SLSKD_DOWNLOADS_DIR: /downloads

  bridge:
    image: ghcr.io/geoffreycoulaud/slskd-lidarr-bridge:latest
    volumes:
      - bridge-data:/data
    environment:
      SLSKD_URL: http://slskd:5030
      SLSKD_API_KEY: your-slskd-api-key
      BRIDGE_PORT: 8765
    depends_on:
      - slskd

volumes:
  downloads:
  bridge-data:
```

> **Bind-mount users:** the bridge runs as `nobody` (UID/GID 65534). If you
> mount a host directory instead of a named volume, the process won't be able
> to write the database unless you set the ownership first:
>
> ```sh
> sudo chown 65534:65534 /your/host/data/dir
> ```
>
> Named volumes (as shown above) inherit the image's `/data` ownership
> automatically — no manual `chown` needed.

## Lidarr setup

Add the **download client first**, then the indexer — so you can pin the indexer to this client. That pin lets the bridge coexist with real Usenet: releases grabbed from the bridge's indexer go only to the bridge's client, and your Usenet indexers keep using your Usenet client, with no cross-wiring.

### 1. Add a SABnzbd download client

In Lidarr: **Settings → Download Clients → Add → SABnzbd**

| Field | Value |
|---|---|
| Name | slskd-bridge (or any label) |
| Host | `bridge` |
| Port | `8765` |
| URL Base | `/sabnzbd` — under the **Show Advanced** toggle |
| API Key | set to the value of `BRIDGE_API_KEY` if configured; otherwise any non-blank string (e.g. `-`) since Lidarr rejects a blank key |
| Category | `music` |

### 2. Add a Newznab indexer

In Lidarr: **Settings → Indexers → Add → Newznab**

| Field | Value |
|---|---|
| Name | slskd (or any label) |
| URL | `http://bridge:8765/indexer` |
| API Key | set to the value of `BRIDGE_API_KEY` if configured; otherwise leave blank |
| Download Client | the **slskd-bridge** client from step 1 — under the **Show Advanced** toggle |

Both **URL Base** (on the download client) and **Download Client** (on the indexer) live behind each form's **Show Advanced** toggle, so enable that first or you won't see them. Pinning the indexer's **Download Client** sends every grab from it to the bridge's client (and keeps bridge grabs off your real Usenet client); leave your Usenet indexers on their own client and the two run side by side without interfering.

### 3. Completed-downloads path

The bridge reads slskd's completed-downloads directory (slskd's `directories.downloads`) from slskd's API and reports `<that dir>/<album folder>` to Lidarr as the import path — no path configuration on the bridge.

Lidarr must be able to read that path. If slskd and Lidarr see the downloads on the **same** filesystem path (shared volume), it works as-is. If they differ (e.g. slskd writes to `/downloads` but Lidarr sees `/media/music/downloads`), add a **Remote Path Mapping** in Lidarr under **Settings → Download Clients → Remote Path Mappings**: set *Host* to the bridge's host and *Remote Path* to slskd's path, *Local Path* to Lidarr's path. This is the standard download-client mechanism — slskd's path is the source of truth, Lidarr translates it.

## Limitations

- **Multi-disc albums are not supported.** Through the latest slskd release (`0.25.1`, as of June 2026), slskd always writes a download to `<downloads>/<immediate parent folder>/` and exposes no way to change this. A multi-disc album laid out remotely as `…/Album/CD1/…` and `…/Album/CD2/…` therefore lands in **separate sibling folders** (`/downloads/CD1/`, `/downloads/CD2/`) with no shared album root on disk, and the bridge reports the per-disc folder, so Lidarr sees each disc as a separate, incomplete release. This **cannot be worked around in the bridge, nor with a Remote Path Mapping**, because no single folder contains every disc. Single-disc albums are unaffected. A real fix is blocked on an unreleased slskd capability; the prerequisite and the planned approach are documented in the `compute_storage_path` docstring (`src/slskd_lidarr_bridge/domain/paths.py`).
- **Lidarr must reach slskd's downloads path:** the bridge reports slskd's own completed-downloads path to Lidarr (discovered from slskd's API). Lidarr must see that path — via a shared volume at the same path, or a Remote Path Mapping when the mounts differ (see [Completed-downloads path](#3-completed-downloads-path)). Otherwise Lidarr fails to import completed downloads.
