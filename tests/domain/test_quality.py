"""Tests for domain.quality.detect_quality."""

from slskd_lidarr_bridge.domain.models import AudioFile
from slskd_lidarr_bridge.domain.quality import detect_quality


def _mp3(bitrate: int | None = None) -> AudioFile:
    return AudioFile(
        filename=r"@@u\Artist\Album\01.mp3",
        size=1_000_000,
        extension=".mp3",
        bitrate=bitrate,
    )


def _flac() -> AudioFile:
    return AudioFile(
        filename=r"@@u\Artist\Album\01.flac",
        size=30_000_000,
        extension=".flac",
    )


def _ext(ext: str) -> AudioFile:
    return AudioFile(
        filename=f"@@u\\Artist\\Album\\01{ext}",
        size=5_000_000,
        extension=ext,
    )


# ---------------------------------------------------------------------------
# FLAC
# ---------------------------------------------------------------------------


def test_all_flac_returns_FLAC() -> None:
    files = [_flac(), _flac(), _flac()]
    assert detect_quality(files) == "FLAC"


# ---------------------------------------------------------------------------
# MP3 bitrate buckets
# ---------------------------------------------------------------------------


def test_mp3_320_returns_MP3_320() -> None:
    assert detect_quality([_mp3(320)]) == "MP3-320"


def test_mp3_256_returns_MP3_256() -> None:
    assert detect_quality([_mp3(256)]) == "MP3-256"


def test_mp3_192_returns_MP3_192() -> None:
    assert detect_quality([_mp3(192)]) == "MP3-192"


def test_mp3_unknown_bitrate_returns_MP3() -> None:
    assert detect_quality([_mp3(None)]) == "MP3"


def test_mp3_bitrate_within_tolerance_snaps_to_bucket() -> None:
    # 312 is within ±16 of 320
    assert detect_quality([_mp3(312)]) == "MP3-320"
    # 272 is within ±16 of 256
    assert detect_quality([_mp3(272)]) == "MP3-256"
    # 208 is within ±16 of 192
    assert detect_quality([_mp3(208)]) == "MP3-192"


def test_mp3_bitrate_outside_all_buckets_returns_bare_MP3() -> None:
    # 100 is not within ±16 of any bucket
    assert detect_quality([_mp3(100)]) == "MP3"


# ---------------------------------------------------------------------------
# Other formats
# ---------------------------------------------------------------------------


def test_m4a_returns_AAC() -> None:
    assert detect_quality([_ext(".m4a")]) == "AAC"


def test_aac_returns_AAC() -> None:
    assert detect_quality([_ext(".aac")]) == "AAC"


def test_ogg_returns_OGG() -> None:
    assert detect_quality([_ext(".ogg")]) == "OGG"


def test_opus_returns_OGG() -> None:
    assert detect_quality([_ext(".opus")]) == "OGG"


def test_wav_returns_WAV() -> None:
    assert detect_quality([_ext(".wav")]) == "WAV"


def test_alac_returns_ALAC() -> None:
    assert detect_quality([_ext(".alac")]) == "ALAC"


# ---------------------------------------------------------------------------
# Mixed formats
# ---------------------------------------------------------------------------


def test_mixed_majority_mp3_returns_mp3() -> None:
    # 3 mp3, 1 flac → mp3 wins
    files = [_mp3(320), _mp3(320), _mp3(320), _flac()]
    assert detect_quality(files) == "MP3-320"


def test_mixed_majority_flac_returns_flac() -> None:
    # 3 flac, 1 mp3 → flac wins
    files = [_flac(), _flac(), _flac(), _mp3(320)]
    assert detect_quality(files) == "FLAC"


def test_mixed_tie_prefers_lossless() -> None:
    # 2 flac, 2 mp3 → tie → lossless wins
    files = [_flac(), _flac(), _mp3(320), _mp3(320)]
    assert detect_quality(files) == "FLAC"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_list_returns_Unknown() -> None:
    assert detect_quality([]) == "Unknown"


def test_non_audio_files_ignored() -> None:
    non_audio = AudioFile(
        filename=r"@@u\Artist\Album\cover.jpg",
        size=50_000,
        extension=".jpg",
    )
    assert detect_quality([non_audio]) == "Unknown"
