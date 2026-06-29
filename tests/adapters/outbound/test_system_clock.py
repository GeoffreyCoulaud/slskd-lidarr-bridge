"""Tests for SystemClock adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from slskd_lidarr_bridge.adapters.outbound.system_clock import SystemClock


def test_now_returns_aware_datetime():
    clock = SystemClock()
    result = clock.now()
    assert isinstance(result, datetime)
    assert result.tzinfo is not None
    assert result.tzinfo == UTC


def test_sleep_calls_time_sleep():
    clock = SystemClock()
    with patch("time.sleep") as mock_sleep:
        clock.sleep(1.5)
    mock_sleep.assert_called_once_with(1.5)


def test_sleep_zero_returns_without_error():
    clock = SystemClock()
    with patch("time.sleep") as mock_sleep:
        clock.sleep(0)
    mock_sleep.assert_called_once_with(0)
