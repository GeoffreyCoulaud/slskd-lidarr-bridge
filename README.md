# slskd-lidarr-bridge

A lightweight bridge that makes [slskd](https://github.com/slsknet/slskd) (a Soulseek daemon) look like Usenet to [Lidarr](https://lidarr.audio/). It impersonates a **Newznab indexer** and a **SABnzbd download client**, so Lidarr searches, downloads, and tracks music from Soulseek using its normal Usenet workflow.

> **A note on authorship:** Almost all of the code in this repository was written by AI coding agents, under human direction and review. The conventions and architectural guardrails those agents follow are documented in [AGENTS.md](AGENTS.md).

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SLSKD_URL` | yes | - | Base URL of the slskd instance (e.g. `http://slskd:5030`) |
| `SLSKD_API_KEY` | yes | - | API key for slskd authentication |
| `BRIDGE_PORT` | no | `8765` | TCP port for the bridge HTTP server |
| `SLSKD_SEARCH_TIMEOUT` | no | `30` | Seconds to wait for a slskd search to complete |
| `BRIDGE_DB_PATH` | no | `/data/bridge.db` | Path to the SQLite database file |
| `BRIDGE_MIN_BITRATE` | no | _(none)_ | Minimum acceptable bitrate in kbps; results below this are filtered out |

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
    ports:
      - "8765:8765"
    volumes:
      - bridge-data:/data
    environment:
      SLSKD_URL: http://slskd:5030
      SLSKD_API_KEY: your-slskd-api-key
    depends_on:
      - slskd

volumes:
  downloads:
  bridge-data:
```

## Lidarr setup

### 1. Add a Newznab indexer

In Lidarr: **Settings → Indexers → Add → Newznab**

| Field | Value |
|---|---|
| Name | slskd (or any label) |
| URL | `http://bridge:8765/indexer` |
| API Path | `/api` |
| API Key | _(leave blank, the bridge does not require one)_ |

### 2. Add a SABnzbd download client

In Lidarr: **Settings → Download Clients → Add → SABnzbd**

| Field | Value |
|---|---|
| Name | slskd-bridge (or any label) |
| Host | `bridge` |
| Port | `8765` |
| URL Base | `/sabnzbd` |
| API Key | _(leave blank, the bridge does not require one)_ |
| Category | `music` |

### 3. Completed-downloads path

The bridge reads slskd's completed-downloads directory (slskd's `directories.downloads`) from slskd's API and reports `<that dir>/<album folder>` to Lidarr as the import path — no path configuration on the bridge.

Lidarr must be able to read that path. If slskd and Lidarr see the downloads on the **same** filesystem path (shared volume), it works as-is. If they differ (e.g. slskd writes to `/downloads` but Lidarr sees `/media/music/downloads`), add a **Remote Path Mapping** in Lidarr under **Settings → Download Clients → Remote Path Mappings**: set *Host* to the bridge's host and *Remote Path* to slskd's path, *Local Path* to Lidarr's path. This is the standard download-client mechanism — slskd's path is the source of truth, Lidarr translates it.

## Limitations

- **Multi-disc albums are not supported.** Through the latest slskd release (`0.25.1`, as of June 2026), slskd always writes a download to `<downloads>/<immediate parent folder>/` and exposes no way to change this. A multi-disc album laid out remotely as `…/Album/CD1/…` and `…/Album/CD2/…` therefore lands in **separate sibling folders** (`/downloads/CD1/`, `/downloads/CD2/`) with no shared album root on disk, and the bridge reports the per-disc folder, so Lidarr sees each disc as a separate, incomplete release. This **cannot be worked around in the bridge, nor with a Remote Path Mapping**, because no single folder contains every disc. Single-disc albums are unaffected. A real fix is blocked on an unreleased slskd capability; the prerequisite and the planned approach are documented in the `compute_storage_path` docstring (`src/slskd_lidarr_bridge/domain/paths.py`).
- **Lidarr must reach slskd's downloads path:** the bridge reports slskd's own completed-downloads path to Lidarr (discovered from slskd's API). Lidarr must see that path — via a shared volume at the same path, or a Remote Path Mapping when the mounts differ (see [Completed-downloads path](#3-completed-downloads-path)). Otherwise Lidarr fails to import completed downloads.
