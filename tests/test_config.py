"""Tests for Config.from_env — env-var driven configuration."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from slskd_lidarr_bridge.config import Config

REQUIRED = {
    "SLSKD_URL": "http://slskd:5030",
    "SLSKD_API_KEY": "secret-key",
}


# ---------------------------------------------------------------------------
# Full env — all fields parsed
# ---------------------------------------------------------------------------


def test_full_env_parses_all_fields():
    env = {
        **REQUIRED,
        "BRIDGE_PORT": "9000",
        "SLSKD_SEARCH_TIMEOUT": "60",
        "BRIDGE_MIN_BITRATE": "192",
        "BRIDGE_STALL_TIMEOUT": "600",
        "BRIDGE_MAX_RETRIES": "3",
        "BRIDGE_ENOUGH_RESULTS": "5",
        "BRIDGE_SEARCH_BUDGET": "120",
        "SLSKD_RESPONSE_LIMIT": "250",
    }
    cfg = Config.from_env(env)

    assert cfg.slskd_url == "http://slskd:5030"
    assert cfg.slskd_api_key == "secret-key"
    assert cfg.bridge_port == 9000
    assert isinstance(cfg.bridge_port, int)
    assert cfg.search_timeout == 60
    assert cfg.min_bitrate == 192
    assert cfg.stall_timeout == 600
    assert cfg.max_retries == 3
    assert cfg.enough_results == 5
    assert cfg.search_budget == 120
    assert cfg.response_limit == 250


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults_when_optional_vars_absent():
    cfg = Config.from_env(REQUIRED)

    assert cfg.bridge_port == 8765
    assert cfg.search_timeout == 15
    assert cfg.min_bitrate is None
    assert cfg.stall_timeout == 1800
    assert cfg.max_retries == 1
    assert cfg.enough_results == 3
    assert cfg.search_budget == 75
    assert cfg.response_limit == 100


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


# ---------------------------------------------------------------------------
# LOG_LEVEL
# ---------------------------------------------------------------------------


def test_log_level_defaults_to_info():
    cfg = Config.from_env(REQUIRED)
    assert cfg.log_level == "INFO"


def test_log_level_from_env_is_normalised_to_upper():
    cfg = Config.from_env({**REQUIRED, "LOG_LEVEL": "debug"})
    assert cfg.log_level == "DEBUG"


def test_blank_log_level_falls_back_to_info():
    cfg = Config.from_env({**REQUIRED, "LOG_LEVEL": "   "})
    assert cfg.log_level == "INFO"


def test_invalid_log_level_raises():
    with pytest.raises(ValueError, match="LOG_LEVEL"):
        Config.from_env({**REQUIRED, "LOG_LEVEL": "verbose"})


# ---------------------------------------------------------------------------
# BRIDGE_API_KEY
# ---------------------------------------------------------------------------


def test_api_key_absent_defaults_to_none():
    cfg = Config.from_env(REQUIRED)
    assert cfg.api_key is None


def test_api_key_set():
    cfg = Config.from_env({**REQUIRED, "BRIDGE_API_KEY": "mysecret"})
    assert cfg.api_key == "mysecret"


def test_api_key_empty_string_is_none():
    cfg = Config.from_env({**REQUIRED, "BRIDGE_API_KEY": ""})
    assert cfg.api_key is None


def test_api_key_whitespace_is_none():
    cfg = Config.from_env({**REQUIRED, "BRIDGE_API_KEY": "   "})
    assert cfg.api_key is None


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_config_is_frozen():
    cfg = Config.from_env(REQUIRED)
    with pytest.raises(FrozenInstanceError):
        cfg.bridge_port = 1234  # type: ignore[misc]
