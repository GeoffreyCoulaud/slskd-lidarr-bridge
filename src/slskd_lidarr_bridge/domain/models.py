from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".alac", ".wma", ".ape"}
)


@dataclass(frozen=True)
class AudioFile:
    filename: str  # full remote path, backslash-separated (e.g. r"@@a\Artist\01.flac")
    size: int  # bytes
    extension: str | None = None  # ".flac", ".mp3" (lowercased, with dot) or None
    bitrate: int | None = None  # kbps if known
    length: int | None = None  # seconds if known

    @property
    def is_audio(self) -> bool:
        """True if extension is in AUDIO_EXTENSIONS (case-insensitive)."""
        if self.extension is None:
            return False
        return self.extension.lower() in AUDIO_EXTENSIONS


@dataclass(frozen=True)
class Disc:
    """One disc of an album: a placement decision plus the files it holds.

    ``subpath`` is the album-root-relative folder the files belong in, already
    sanitized for the local filesystem (e.g. ``"CD1"``). It is empty for a
    single-disc album, whose files sit directly in the album folder.
    """

    subpath: str
    files: tuple[AudioFile, ...]


@dataclass(frozen=True)
class SearchResponse:
    username: str
    has_free_upload_slot: bool
    upload_speed: int
    queue_length: int
    files: tuple[AudioFile, ...]


@dataclass(frozen=True)
class Transfer:
    username: str
    id: str
    filename: str
    size: int
    state: str  # comma-joined slskd flags, e.g. "Completed, Succeeded"
    bytes_transferred: int
    bytes_remaining: int
    percent_complete: float
    exception: str | None = None

    @property
    def is_complete(self) -> bool:
        """True if 'Completed' is in state."""
        return "Completed" in self.state

    @property
    def is_succeeded(self) -> bool:
        """True if is_complete and 'Succeeded' is in state."""
        return self.is_complete and "Succeeded" in self.state

    @property
    def is_failed(self) -> bool:
        """True if is_complete and not is_succeeded."""
        return self.is_complete and not self.is_succeeded


@dataclass(frozen=True)
class SearchQuery:
    artist: str | None = None
    album: str | None = None
    term: str | None = None  # basic t=search q=

    @property
    def is_empty(self) -> bool:
        """True if no artist, album, or term — RSS sync."""
        return self.artist is None and self.album is None and self.term is None

    def to_search_text(self) -> str:
        """Build a search string: 'Artist Album' / 'Artist' / term."""
        if self.term is not None:
            return self.term
        parts = [p for p in (self.artist, self.album) if p is not None]
        return " ".join(parts)


@dataclass(frozen=True)
class Release:
    artist: str
    album: str
    title: str
    username: str
    discs: tuple[Disc, ...]
    size: int
    album_path: str  # full remote directory of the album root, "/"-separated
    destination: str  # local folder, relative to slskd's downloads directory
    quality: str  # "FLAC", "MP3-320", ...
    created_at: datetime
    id: str | None = None  # set by ReleaseStore.put

    @property
    def files(self) -> tuple[AudioFile, ...]:
        """Every file of the release, discs flattened in order."""
        return tuple(f for disc in self.discs for f in disc.files)


@dataclass(frozen=True)
class DownloadJob:
    nzo_id: str
    title: str
    username: str
    discs: tuple[Disc, ...]
    category: str
    destination: str  # local folder, relative to slskd's downloads directory
    total_size: int
    created_at: datetime

    @property
    def files(self) -> tuple[AudioFile, ...]:
        """Every file of the job, discs flattened in order."""
        return tuple(f for disc in self.discs for f in disc.files)


@dataclass(frozen=True)
class JobStatusView:
    nzo_id: str
    title: str
    category: str
    total_bytes: int
    transferred_bytes: int
    percent: float  # 0..100
    state: str  # "downloading" | "completed" | "failed"
    storage: str | None = None  # absolute final folder when completed
    fail_message: str | None = None
