# slskd-lidarr-bridge

A lightweight bridge that exposes [slskd](https://github.com/slsknet/slskd) (a Soulseek daemon) to [Lidarr](https://lidarr.audio/) as both a **Newznab indexer** and a **SABnzbd download client**, letting Lidarr search Soulseek for music, download NZB "tickets", and track transfer progress — all without any Usenet infrastructure.

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SLSKD_URL` | yes | — | Base URL of the slskd instance (e.g. `http://slskd:5030`) |
| `SLSKD_API_KEY` | yes | — | API key for slskd authentication |
| `SLSKD_DOWNLOADS_DIR` | yes | — | Absolute path to slskd's downloads directory (must match Lidarr's view) |
| `BRIDGE_API_KEY` | no | _(none)_ | When set, all bridge endpoints require this key as `apikey` |
| `BRIDGE_CATEGORIES` | no | `music` | Comma-separated list of SABnzbd category names reported to Lidarr |
| `BRIDGE_HOST` | no | `0.0.0.0` | Host address for the bridge HTTP server |
| `BRIDGE_PORT` | no | `8765` | TCP port for the bridge HTTP server |
| `SEARCH_TIMEOUT` | no | `30` | Seconds to wait for a slskd search to complete |
| `DB_PATH` | no | `/data/bridge.db` | Path to the SQLite database file |
| `MIN_BITRATE` | no | _(none)_ | Minimum acceptable bitrate in kbps; results below this are filtered out |

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
      # BRIDGE_API_KEY: your-bridge-key   # optional but recommended
    depends_on:
      - slskd

volumes:
  downloads:
  bridge-data:
```

## Lidarr setup

### 1 — Add a Newznab indexer

In Lidarr: **Settings → Indexers → Add → Newznab**

| Field | Value |
|---|---|
| Name | slskd (or any label) |
| URL | `http://bridge:8765/indexer` |
| API Path | `/api` |
| API Key | value of `BRIDGE_API_KEY` (leave blank if not set) |

### 2 — Add a SABnzbd download client

In Lidarr: **Settings → Download Clients → Add → SABnzbd**

| Field | Value |
|---|---|
| Name | slskd-bridge (or any label) |
| Host | `bridge` |
| Port | `8765` |
| URL Base | `/sabnzbd` |
| API Key | value of `BRIDGE_API_KEY` (leave blank if not set) |
| Category | `music` |

### 3 — Shared downloads volume

`SLSKD_DOWNLOADS_DIR` must be the **same filesystem path** that Lidarr uses when it inspects completed downloads. If Lidarr and the bridge run on different mounts (e.g. Lidarr sees `/media/music/downloads` but the bridge sees `/downloads`), configure a **Remote Path Mapping** in Lidarr under **Settings → Download Clients → Remote Path Mappings** to translate between the two paths.
