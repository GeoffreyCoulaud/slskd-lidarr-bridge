"""Quality detection for a collection of audio files."""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Sequence

from slskd_lidarr_bridge.domain.models import AudioFile

# Map lowercase extensions to format family labels.
# Note: .ape and .wma are valid audio extensions (models.AUDIO_EXTENSIONS) but
# are outside the Lidarr quality-label spec, so they are intentionally omitted.
_EXT_TO_FAMILY: dict[str, str] = {
    ".flac": "FLAC",
    ".alac": "ALAC",
    ".wav": "WAV",
    ".mp3": "MP3",
    ".m4a": "AAC",
    ".aac": "AAC",
    ".ogg": "OGG",
    ".opus": "OGG",
}

# Lossless families (prefer these on tie).
_LOSSLESS: frozenset[str] = frozenset({"FLAC", "ALAC", "WAV"})

# Explicit priority order for lossless tie-breaks: FLAC > ALAC > WAV.
_LOSSLESS_PRIORITY: tuple[str, ...] = ("FLAC", "ALAC", "WAV")

# Ordered bitrate buckets for MP3; snap when within ±16 kbps.
_MP3_BUCKETS: tuple[int, ...] = (320, 256, 192, 128)
_MP3_TOLERANCE: int = 16


def _mp3_suffix(bitrates: list[int | None]) -> str:
    """Return the bitrate suffix for a collection of MP3 files, e.g. '-320'."""
    known = [b for b in bitrates if b is not None]
    if not known:
        return ""
    median_br = int(statistics.median(known))
    # Snap to the nearest standard bucket within ±16 kbps (brief-mandated
    # examples: 312→320, 272→256, 208→192).  Values outside tolerance yield
    # a bare "MP3" label (no suffix).
    for bucket in _MP3_BUCKETS:
        if abs(median_br - bucket) <= _MP3_TOLERANCE:
            return f"-{bucket}"
    return ""


def detect_quality(files: Sequence[AudioFile]) -> str:
    """Return a Lidarr-parseable quality label for a collection of audio files.

    Rules:
    - Only ``is_audio`` files are considered; empty result → "Unknown".
    - Tally by format family (extension-mapped).
    - Predominant family wins; on tie, lossless beats lossy.
    - MP3 gets an optional bitrate suffix (-320/-256/-192/-128) when the
      median bitrate snaps within ±16 kbps of a known bucket.
    """
    audio = [f for f in files if f.is_audio]
    if not audio:
        return "Unknown"

    # Count files per family.
    family_counts: Counter[str] = Counter()
    family_bitrates: dict[str, list[int | None]] = {}
    for f in audio:
        ext = (f.extension or "").lower()
        family = _EXT_TO_FAMILY.get(ext, "Unknown")
        family_counts[family] += 1
        if family == "MP3":
            family_bitrates.setdefault("MP3", []).append(f.bitrate)

    # Find the winning family: highest count, with lossless preferred on tie.
    top_count = family_counts.most_common(1)[0][1]
    candidates = [fam for fam, cnt in family_counts.items() if cnt == top_count]

    # Prefer lossless on tie; among lossless candidates, use explicit priority.
    lossless_candidates = [c for c in candidates if c in _LOSSLESS]
    if lossless_candidates:
        winner = next(
            (p for p in _LOSSLESS_PRIORITY if p in lossless_candidates),
            lossless_candidates[0],
        )
    else:
        winner = candidates[0]

    if winner == "MP3":
        suffix = _mp3_suffix(family_bitrates.get("MP3", []))
        return f"MP3{suffix}"

    return winner
