"""System clock adapter — implements the Clock protocol using real time."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from slskd_lidarr_bridge.domain.ports import Clock


class SystemClock:
    """Clock implementation backed by the system wall clock."""

    def now(self) -> datetime:
        """Return the current UTC time as a timezone-aware datetime."""
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        """Block for the given number of seconds."""
        time.sleep(seconds)
