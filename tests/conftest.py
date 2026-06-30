"""Shared pytest fixtures."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    """Snapshot and restore the root logger around every test.

    ``configure_logging`` (via ``main()`` or its own tests) reconfigures the
    root logger process-wide with ``force=True``. Without this, that handler
    would leak into later tests and emit stray records. Snapshotting before the
    test and restoring after keeps each test hermetic.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
