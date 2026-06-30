# slskd-lidarr-bridge

A lightweight bridge that makes [slskd](https://github.com/slsknet/slskd) (a Soulseek daemon) look like Usenet to [Lidarr](https://lidarr.audio/). It impersonates a **Newznab indexer** and a **SABnzbd download client**, so Lidarr searches, downloads, and tracks music from Soulseek using its normal Usenet workflow.

> **A note on authorship:** Almost all of the code in this repository was written by AI coding agents, under human direction and review. The conventions and architectural guardrails those agents follow are documented in [AGENTS.md](AGENTS.md).

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SLSKD_URL` | yes | - | Base URL of the slskd instance (e.g. `http://slskd:5030`) |
| `SLSKD_API_KEY` | yes | - | API key for slskd authentication |
| `SLSKD_DOWNLOADS_DIR` | yes | - | Absolute path to slskd's downloads directory (must match Lidarr's view) |
| `BRIDGE_PORT` | no | `8765` | TCP port for the bridge HTTP server |
| `SLSKD_SEARCH_TIMEOUT` | no | `30` | Seconds to wait for a slskd search to complete |
| `BRIDGE_DB_PATH` | no | `/data/bridge.db` | Path to the SQLite database file |
| `BRIDGE_MIN_BITRATE` | no | _(none)_ | Minimum acceptable bitrate in kbps; results below this are filtered out |

## Docker Compose

The bridge and slskd must share the same downloads volume so that file paths reported by slskd are accessible to Lidarr.

```yaml
services:
  slskd:
    image: slsknet/slskd:latest
    volumes:
      - downloads:/downloads
    environment:
      SLSKD_DOWNLOADS_DIR: /downloads

  bridge:
    image: slskd-lidarr-bridge:latest
    ports:
      - "8765:8765"
    volumes:
      - downloads:/downloads
      - bridge-data:/data
    environment:
      SLSKD_URL: http://slskd:5030
      SLSKD_API_KEY: your-slskd-api-key
      SLSKD_DOWNLOADS_DIR: /downloads
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

### 3. Shared downloads volume

`SLSKD_DOWNLOADS_DIR` must be the **same filesystem path** that Lidarr uses when it inspects completed downloads. If Lidarr and the bridge run on different mounts (e.g. Lidarr sees `/media/music/downloads` but the bridge sees `/downloads`), configure a **Remote Path Mapping** in Lidarr under **Settings → Download Clients → Remote Path Mappings** to translate between the two paths.

## Limitations

- **Multi-disc albums are not supported.** Through the latest slskd release (`0.25.1`, as of June 2026), slskd always writes a download to `<downloads>/<immediate parent folder>/` and exposes no way to change this. A multi-disc album laid out remotely as `…/Album/CD1/…` and `…/Album/CD2/…` therefore lands in **separate sibling folders** (`/downloads/CD1/`, `/downloads/CD2/`) with no shared album root on disk, and the bridge reports the per-disc folder, so Lidarr sees each disc as a separate, incomplete release. This **cannot be worked around in the bridge, nor with a Remote Path Mapping**, because no single folder contains every disc. Single-disc albums are unaffected. A real fix is blocked on an unreleased slskd capability; the prerequisite and the planned approach are documented in the `compute_storage_path` docstring (`src/slskd_lidarr_bridge/domain/paths.py`).
- **Shared volume required:** `SLSKD_DOWNLOADS_DIR` must be the path that Lidarr also sees (direct shared volume or a Remote Path Mapping configured in Lidarr). Mismatched paths will cause Lidarr to fail to import completed downloads.
