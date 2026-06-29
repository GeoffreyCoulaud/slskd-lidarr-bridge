"""Tests for Config.from_env — env-var driven configuration."""
from __future__ import annotations

import pytest

from slskd_lidarr_bridge.config import Config

REQUIRED = {
    "SLSKD_URL": "http://slskd:5030",
    "SLSKD_API_KEY": "secret-key",
    "SLSKD_DOWNLOADS_DIR": "/downloads",
}


# ---------------------------------------------------------------------------
# Full env — all fields parsed
# ---------------------------------------------------------------------------


def test_full_env_parses_all_fields():
    env = {
        **REQUIRED,
        "BRIDGE_API_KEY": "bridge-key",
        "BRIDGE_HOST": "127.0.0.1",
        "BRIDGE_PORT": "9000",
        "SLSKD_SEARCH_TIMEOUT": "60",
        "BRIDGE_DB_PATH": "/tmp/test.db",
        "BRIDGE_MIN_BITRATE": "192",
        "BRIDGE_CATEGORIES": "music, podcasts",
    }
    cfg = Config.from_env(env)

    assert cfg.slskd_url == "http://slskd:5030"
    assert cfg.slskd_api_key == "secret-key"
    assert cfg.slskd_downloads_dir == "/downloads"
    assert cfg.bridge_api_key == "bridge-key"
    assert cfg.bridge_host == "127.0.0.1"
    assert cfg.bridge_port == 9000
    assert isinstance(cfg.bridge_port, int)
    assert cfg.search_timeout == 60
    assert cfg.db_path == "/tmp/test.db"
    assert cfg.min_bitrate == 192
    assert cfg.sab_categories == ["music", "podcasts"]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults_when_optional_vars_absent():
    cfg = Config.from_env(REQUIRED)

    assert cfg.bridge_api_key is None
    assert cfg.bridge_host == "0.0.0.0"
    assert cfg.bridge_port == 8765
    assert cfg.search_timeout == 30
    assert cfg.db_path == "/data/bridge.db"
    assert cfg.min_bitrate is None
    assert cfg.sab_categories == ["music"]


def test_default_newznab_categories():
    cfg = Config.from_env(REQUIRED)
    expected = [
        (3000, "Audio"),
        (3010, "Audio/MP3"),
        (3030, "Audio/Audiobook"),
        (3040, "Audio/Lossless"),
    ]
    assert cfg.categories == expected


# ---------------------------------------------------------------------------
# sab_categories parsing (comma-separated, trimmed)
# ---------------------------------------------------------------------------


def test_sab_categories_splits_and_trims():
    cfg = Config.from_env({**REQUIRED, "BRIDGE_CATEGORIES": " jazz ,  rock , pop "})
    assert cfg.sab_categories == ["jazz", "rock", "pop"]


def test_sab_categories_single_entry():
    cfg = Config.from_env({**REQUIRED, "BRIDGE_CATEGORIES": "audiobooks"})
    assert cfg.sab_categories == ["audiobooks"]


# ---------------------------------------------------------------------------
# Missing required vars → error
# ---------------------------------------------------------------------------


def test_missing_slskd_url_raises():
    env = {k: v for k, v in REQUIRED.items() if k != "SLSKD_URL"}
    with pytest.raises((SystemExit, ValueError)) as exc_info:
        Config.from_env(env)
    assert "SLSKD_URL" in str(exc_info.value)


def test_missing_slskd_api_key_raises():
    env = {k: v for k, v in REQUIRED.items() if k != "SLSKD_API_KEY"}
    with pytest.raises((SystemExit, ValueError)) as exc_info:
        Config.from_env(env)
    assert "SLSKD_API_KEY" in str(exc_info.value)


def test_missing_slskd_downloads_dir_raises():
    env = {k: v for k, v in REQUIRED.items() if k != "SLSKD_DOWNLOADS_DIR"}
    with pytest.raises((SystemExit, ValueError)) as exc_info:
        Config.from_env(env)
    assert "SLSKD_DOWNLOADS_DIR" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_config_is_frozen():
    cfg = Config.from_env(REQUIRED)
    with pytest.raises(Exception):
        cfg.bridge_port = 1234  # type: ignore[misc]
