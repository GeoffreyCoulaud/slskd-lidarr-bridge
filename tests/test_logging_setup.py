"""Tests for process-wide logging configuration."""

from __future__ import annotations

import logging

from slskd_lidarr_bridge.logging_setup import configure_logging


def test_configure_logging_sets_root_level():
    configure_logging("DEBUG")
    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_installs_a_handler():
    configure_logging("WARNING")
    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert root.handlers


def test_configure_logging_format_includes_level_name_and_message():
    configure_logging("INFO")
    handler = logging.getLogger().handlers[0]
    record = logging.LogRecord(
        name="mylogger",
        level=logging.INFO,
        pathname="f.py",
        lineno=1,
        msg="hello world",
        args=None,
        exc_info=None,
    )
    formatted = handler.format(record)
    assert "mylogger" in formatted
    assert "INFO" in formatted
    assert "hello world" in formatted
