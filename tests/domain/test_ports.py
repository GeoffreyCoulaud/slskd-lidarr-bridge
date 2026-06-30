"""
Tests confirming the four domain Protocols exist and are runtime_checkable.
Each test builds a minimal in-test class that implements all required methods
and verifies isinstance() matches against the Protocol.
"""

from datetime import datetime

from slskd_lidarr_bridge.domain.models import (
    AudioFile,
    DownloadJob,
    Release,
    SearchResponse,
    Transfer,
)
from slskd_lidarr_bridge.domain.ports import (
    Clock,
    JobStore,
    ReleaseStore,
    SoulseekGateway,
)

# ── SoulseekGateway ──────────────────────────────────────────────────────────


class _StubSoulseekGateway:
    def start_search(self, text: str) -> str:
        return "search-id"

    def search_is_complete(self, search_id: str) -> bool:
        return False

    def search_responses(self, search_id: str) -> list[SearchResponse]:
        return []

    def enqueue(self, username: str, files: list[AudioFile]) -> None:
        pass

    def transfers(self, username: str) -> list[Transfer]:
        return []

    def cancel(self, username: str, transfer_id: str) -> None:
        pass

    def downloads_directory(self) -> str:
        return "/downloads"


def test_soulseek_gateway_is_runtime_checkable():
    assert isinstance(_StubSoulseekGateway(), SoulseekGateway)


def test_soulseek_gateway_non_conforming_class_fails():
    class _Bad:
        pass

    assert not isinstance(_Bad(), SoulseekGateway)


# ── ReleaseStore ─────────────────────────────────────────────────────────────


class _StubReleaseStore:
    def put(self, release: Release) -> str:
        return "some-id"

    def get(self, release_id: str) -> Release | None:
        return None

    def purge_older_than(self, cutoff: datetime) -> None:
        pass


def test_release_store_is_runtime_checkable():
    assert isinstance(_StubReleaseStore(), ReleaseStore)


def test_release_store_non_conforming_fails():
    class _Bad:
        pass

    assert not isinstance(_Bad(), ReleaseStore)


# ── JobStore ─────────────────────────────────────────────────────────────────


class _StubJobStore:
    def add(self, job: DownloadJob) -> None:
        pass

    def get(self, nzo_id: str) -> DownloadJob | None:
        return None

    def list(self) -> list[DownloadJob]:
        return []

    def remove(self, nzo_id: str) -> None:
        pass


def test_job_store_is_runtime_checkable():
    assert isinstance(_StubJobStore(), JobStore)


def test_job_store_non_conforming_fails():
    class _Bad:
        pass

    assert not isinstance(_Bad(), JobStore)


# ── Clock ────────────────────────────────────────────────────────────────────


class _StubClock:
    def now(self) -> datetime:
        return datetime.now()

    def sleep(self, seconds: float) -> None:
        pass


def test_clock_is_runtime_checkable():
    assert isinstance(_StubClock(), Clock)


def test_clock_non_conforming_fails():
    class _Bad:
        pass

    assert not isinstance(_Bad(), Clock)
