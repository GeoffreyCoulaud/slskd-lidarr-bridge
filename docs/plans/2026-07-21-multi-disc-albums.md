# Multi-disc album support — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the bridge offer a multi-disc album to Lidarr as one release, and
download it into one folder Lidarr can import, using slskd 0.26's per-batch
download destinations.

**Architecture:** Search-side grouping merges disc subfolders (`Album/CD1`,
`Album/CD2`) into a single release rooted at the album, and decides the on-disk
destination there and then. Download-side enqueues one slskd batch per disc with
destination `<album> [<peer>]/<disc>`, so every disc lands under a shared album
root that the bridge reports to Lidarr verbatim instead of inferring it.

**Tech Stack:** Python 3.12+, Flask/waitress, SQLite, httpx, pytest + respx,
ruff, strict mypy. Dependencies via `uv`.

## Global Constraints

- Design spec: `docs/specs/2026-07-21-multi-disc-albums-design.md`. It is the
  authority; this plan implements it.
- Branch: `feat/multi-disc-albums`. Never commit on `main`.
- Every commit must pass all four gates: `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy`, `uv run pytest`.
- **100% line and branch coverage** is enforced by `fail_under = 100`. Every new
  branch needs a test.
- `from __future__ import annotations` at the top of every module.
- Strict mypy: production code fully typed, no `# type: ignore` to dodge errors.
- Line length 88 (ruff).
- Minimum supported slskd version: **0.26.0**.
- Domain modules import only from `domain/`; never from `adapters/` or Flask.
- Never use em-dashes (`—`) or en-dashes (`–`) in README text or log messages
  that users read. Use a colon, parentheses, or a short hyphen.

---

### Task 1: Destination path helpers

**Files:**
- Modify: `src/slskd_lidarr_bridge/domain/paths.py`
- Test: `tests/domain/test_paths.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `sanitize_segment(segment: str) -> str`
  - `build_album_destination(album_name: str, username: str) -> str`
  - `batch_destination(destination: str, subpath: str) -> str`

`compute_storage_path` is left untouched in this task; Task 5 replaces it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/domain/test_paths.py`:

```python
from slskd_lidarr_bridge.domain.paths import (
    batch_destination,
    build_album_destination,
    sanitize_segment,
)


def test_sanitize_segment_replaces_separators_and_hostile_characters():
    assert sanitize_segment("AC/DC") == "AC_DC"
    assert sanitize_segment("Album: The Return") == "Album_ The Return"
    assert sanitize_segment("a\\b") == "a_b"
    assert sanitize_segment("q?w*e|r<t>y\"u") == "q_w_e_r_t_y_u"


def test_sanitize_segment_replaces_control_characters():
    assert sanitize_segment("a\x00b\x1fc") == "a_b_c"


def test_sanitize_segment_strips_windows_hostile_trailing_characters():
    assert sanitize_segment("Album.") == "Album"
    assert sanitize_segment("Album ") == "Album"


def test_sanitize_segment_falls_back_when_nothing_survives():
    assert sanitize_segment("") == "_"
    assert sanitize_segment("...") == "_"
    assert sanitize_segment("   ") == "_"


def test_build_album_destination_appends_the_peer_name():
    assert build_album_destination("Greatest Hits", "bob") == "Greatest Hits [bob]"


def test_build_album_destination_sanitizes_the_whole_segment():
    assert build_album_destination("AC/DC Live", "bo/b") == "AC_DC Live [bo_b]"


def test_batch_destination_without_subpath_is_the_album_destination():
    assert batch_destination("Album [bob]", "") == "Album [bob]"


def test_batch_destination_appends_the_disc_subpath():
    assert batch_destination("Album [bob]", "CD1") == "Album [bob]/CD1"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/domain/test_paths.py -v`
Expected: FAIL with `ImportError: cannot import name 'batch_destination'`.

- [ ] **Step 3: Implement the helpers**

Append to `src/slskd_lidarr_bridge/domain/paths.py`:

```python
# Characters that are invalid in a path segment on *any* supported host.
# The union of Windows' and Unix's invalid sets is deliberate: slskd sanitizes
# with its own host's set, so normalizing against the strictest one here makes
# slskd's sanitization a no-op and keeps the path we report to Lidarr exactly
# equal to the path on disk, whatever slskd runs on.
_INVALID_SEGMENT_CHARS = frozenset('"<>|:*?\\/') | {chr(c) for c in range(32)}


def sanitize_segment(segment: str) -> str:
    """Return *segment* made safe for use as a single path component.

    Invalid characters become ``_``; trailing dots and spaces are dropped
    (Windows silently truncates them, which would desynchronize the path we
    report from the path on disk); a segment left empty falls back to ``_``.

    Stripping trailing dots also disposes of the ``.`` and ``..`` traversal
    segments that slskd's ``NonTraversingPath`` validator rejects.
    """
    cleaned = "".join("_" if c in _INVALID_SEGMENT_CHARS else c for c in segment)
    cleaned = cleaned.rstrip(". ")
    return cleaned or "_"


def build_album_destination(album_name: str, username: str) -> str:
    """Return the album's download folder, relative to slskd's downloads dir.

    The peer name is appended so that two peers offering identically named
    album folders cannot write into the same directory.
    """
    return sanitize_segment(f"{album_name} [{username}]")


def batch_destination(destination: str, subpath: str) -> str:
    """Return the destination for one disc: the album folder, plus its subpath."""
    if not subpath:
        return destination
    return posixpath.join(destination, subpath)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/domain/test_paths.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full gate**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`
Expected: all pass, coverage 100%.

- [ ] **Step 6: Commit**

```bash
git add src/slskd_lidarr_bridge/domain/paths.py tests/domain/test_paths.py
git commit -m "feat(paths): add destination helpers for bridge-chosen download folders"
```

---

### Task 2: Disc detection and album grouping

**Files:**
- Create: `src/slskd_lidarr_bridge/domain/albums.py`
- Modify: `src/slskd_lidarr_bridge/domain/models.py`
- Test: `tests/domain/test_albums.py` (create)

**Interfaces:**
- Consumes: `sanitize_segment` (Task 1).
- Produces:
  - `Disc(subpath: str, files: tuple[AudioFile, ...])` in `domain/models.py`
  - `is_disc_folder(name: str) -> bool`
  - `AlbumGroup(album_path: str, discs: tuple[Disc, ...])` with properties
    `name: str`, `parent_name: str`, `files: tuple[AudioFile, ...]`
  - `group_files_by_album(files: Iterable[AudioFile]) -> list[AlbumGroup]`

- [ ] **Step 1: Write the failing tests**

Create `tests/domain/test_albums.py`:

```python
"""Tests for disc detection and album grouping."""

from __future__ import annotations

import pytest

from slskd_lidarr_bridge.domain.albums import (
    AlbumGroup,
    group_files_by_album,
    is_disc_folder,
)
from slskd_lidarr_bridge.domain.models import AudioFile, Disc


def _f(path: str, size: int = 10) -> AudioFile:
    return AudioFile(filename=path, size=size, extension=".flac")


@pytest.mark.parametrize(
    "name",
    [
        "CD1",
        "CD 1",
        "cd_2",
        "CD-03",
        "Disc 2",
        "disk3",
        "Disc Two",
        "[CD2]",
        "(cd 2)",
        "CD1 - Bonus",
        "Side A",
        "Part 1",
    ],
)
def test_is_disc_folder_accepts_disc_layouts(name):
    assert is_disc_folder(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "Disco",
        "Discovery",
        "CDs",
        "CD Audio",
        "Part Of The Album",
        "Vol 1",
        "Volume 2",
        "CD100",
        "Album",
        "",
    ],
)
def test_is_disc_folder_rejects_everything_else(name):
    assert is_disc_folder(name) is False


def test_single_directory_yields_one_group_with_one_unnamed_disc():
    files = [_f(r"@@ab\Artist\Album\01.flac"), _f(r"@@ab\Artist\Album\02.flac")]
    groups = group_files_by_album(files)
    assert groups == [
        AlbumGroup(
            album_path="@@ab/Artist/Album",
            discs=(Disc(subpath="", files=(files[0], files[1])),),
        )
    ]


def test_disc_subfolders_merge_into_one_group():
    a = _f(r"@@ab\Artist\Album\CD1\01.flac")
    b = _f(r"@@ab\Artist\Album\CD2\01.flac")
    groups = group_files_by_album([b, a])
    assert len(groups) == 1
    assert groups[0].album_path == "@@ab/Artist/Album"
    assert groups[0].discs == (
        Disc(subpath="CD1", files=(a,)),
        Disc(subpath="CD2", files=(b,)),
    )


def test_a_lone_disc_of_a_multi_disc_album_is_still_rooted_at_the_album():
    b = _f(r"@@ab\Artist\Album\CD2\01.flac")
    groups = group_files_by_album([b])
    assert groups[0].album_path == "@@ab/Artist/Album"
    assert groups[0].discs == (Disc(subpath="CD2", files=(b,)),)


def test_same_folder_name_under_different_parents_stays_separate():
    a = _f(r"@@ab\Live\Album\01.flac")
    b = _f(r"@@ab\Studio\Album\01.flac")
    groups = group_files_by_album([a, b])
    assert [g.album_path for g in groups] == [
        "@@ab/Live/Album",
        "@@ab/Studio/Album",
    ]


def test_disc_folder_directly_under_a_share_root_is_not_promoted():
    a = _f(r"@@ab\CD1\01.flac")
    groups = group_files_by_album([a])
    assert groups[0].album_path == "@@ab/CD1"


def test_disc_folder_without_any_parent_is_not_promoted():
    a = _f(r"CD1\01.flac")
    groups = group_files_by_album([a])
    assert groups[0].album_path == "CD1"


def test_disc_subpath_is_sanitized():
    # The ":" is legal on a Unix share and hostile on Windows, so the subpath
    # stored is already the on-disk form.
    a = _f(r"@@ab\Artist\Album\CD1: Bonus\01.flac")
    groups = group_files_by_album([a])
    assert groups[0].album_path == "@@ab/Artist/Album"
    assert groups[0].discs == (Disc(subpath="CD1_ Bonus", files=(a,)),)


def test_group_name_and_parent_name():
    group = AlbumGroup(album_path="@@ab/Artist/Album", discs=())
    assert group.name == "Album"
    assert group.parent_name == "Artist"


def test_parent_name_is_empty_at_a_share_root_or_without_a_parent():
    assert AlbumGroup(album_path="@@ab/Album", discs=()).parent_name == ""
    assert AlbumGroup(album_path="Album", discs=()).parent_name == ""


def test_group_files_flattens_every_disc():
    a = _f(r"@@ab\Artist\Album\CD1\01.flac")
    b = _f(r"@@ab\Artist\Album\CD2\01.flac")
    assert group_files_by_album([a, b])[0].files == (a, b)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/domain/test_albums.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'slskd_lidarr_bridge.domain.albums'`.

- [ ] **Step 3: Add the `Disc` model**

In `src/slskd_lidarr_bridge/domain/models.py`, insert after the `AudioFile`
class:

```python
@dataclass(frozen=True)
class Disc:
    """One disc of an album: a placement decision plus the files it holds.

    ``subpath`` is the album-root-relative folder the files belong in, already
    sanitized for the local filesystem (e.g. ``"CD1"``). It is empty for a
    single-disc album, whose files sit directly in the album folder.
    """

    subpath: str
    files: tuple[AudioFile, ...]
```

- [ ] **Step 4: Implement the grouping module**

Create `src/slskd_lidarr_bridge/domain/albums.py`:

```python
"""Grouping remote Soulseek directory layouts into albums and discs.

slskd reports a flat list of files per peer, each with its full remote path.
Multi-disc albums appear as sibling subfolders (``Album/CD1``, ``Album/CD2``)
that belong to one release; this module recognises them and merges them under
their shared album root.
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from slskd_lidarr_bridge.domain.models import AudioFile, Disc
from slskd_lidarr_bridge.domain.paths import sanitize_segment

_DISC_WORDS = "one|two|three|four|five|six|seven|eight|nine|ten"

# A disc folder is a token followed by a quantifier: digits ("CD1"), a single
# letter ("Side A") or a spelled-out number ("Disc Two"). Requiring the
# quantifier is what keeps "Disco", "Discovery", "CDs" and "CD Audio" out, and
# the trailing \b keeps three-digit runs ("CD100") out.
#
# "vol"/"volume" are deliberately absent: compilation series are organised by
# volume ("Cafe del Mar/Volumen 1", "Volumen 2"), where the parent is a series
# name and each child is a distinct album. Merging those would fabricate a
# release claiming to hold every volume.
_DISC_FOLDER_RE = re.compile(
    rf"^[\[(]?\s*(?:cd|disc|disk|part|side)[\s._\-]*"
    rf"(?:\d{{1,2}}|[a-e]|{_DISC_WORDS})\b",
    re.IGNORECASE,
)

# Soulseek Qt hides a peer's real share root behind an "@@" + hash prefix.
# It is never an album, so a disc folder sitting directly under one is not
# promoted (its "album" would be the share root itself).
_SHARE_ROOT_PREFIX = "@@"


def is_disc_folder(name: str) -> bool:
    """True if *name* looks like a disc subfolder of an album."""
    return _DISC_FOLDER_RE.match(name) is not None


@dataclass(frozen=True)
class AlbumGroup:
    """The files of one album from one peer, split into discs."""

    album_path: str  # full remote directory of the album root, "/"-separated
    discs: tuple[Disc, ...]  # ordered by subpath

    @property
    def name(self) -> str:
        """The album root's own folder name."""
        return posixpath.basename(self.album_path)

    @property
    def parent_name(self) -> str:
        """The folder above the album root, usually the artist.

        Empty when there is no folder above it, or when that folder is the
        peer's share root.
        """
        parent = posixpath.basename(posixpath.dirname(self.album_path))
        if parent.startswith(_SHARE_ROOT_PREFIX):
            return ""
        return parent

    @property
    def files(self) -> tuple[AudioFile, ...]:
        """Every file of the album, discs flattened in order."""
        return tuple(f for disc in self.discs for f in disc.files)


def _directory(file: AudioFile) -> str:
    """The remote directory holding *file*, normalized to forward slashes."""
    return posixpath.dirname(file.filename.replace("\\", "/"))


def _album_root(directory: str) -> str:
    """The album root for *directory*: its parent if it is a disc folder."""
    parent = posixpath.dirname(directory)
    if not parent:
        return directory
    if posixpath.basename(parent).startswith(_SHARE_ROOT_PREFIX):
        return directory
    if not is_disc_folder(posixpath.basename(directory)):
        return directory
    return parent


def _subpath(root: str, directory: str) -> str:
    """The sanitized, album-root-relative folder for *directory*."""
    if directory == root:
        return ""
    relative = directory[len(root) :].strip("/")
    return "/".join(sanitize_segment(s) for s in relative.split("/"))


def group_files_by_album(files: Iterable[AudioFile]) -> list[AlbumGroup]:
    """Group one peer's files into albums, merging disc subfolders.

    Grouping is keyed on the full remote directory path, not just its name, so
    two albums of one peer whose folders happen to share a name stay apart.
    Ordering is deterministic: groups by album path, discs by subpath, files by
    filename.
    """
    by_root: defaultdict[str, defaultdict[str, list[AudioFile]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for file in files:
        directory = _directory(file)
        root = _album_root(directory)
        by_root[root][_subpath(root, directory)].append(file)

    return [
        AlbumGroup(
            album_path=root,
            discs=tuple(
                Disc(
                    subpath=subpath,
                    files=tuple(sorted(by_subpath[subpath], key=lambda f: f.filename)),
                )
                for subpath in sorted(by_subpath)
            ),
        )
        for root, by_subpath in sorted(by_root.items())
    ]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/domain/test_albums.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full gate**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`
Expected: all pass, coverage 100%.

- [ ] **Step 7: Commit**

```bash
git add src/slskd_lidarr_bridge/domain/albums.py \
        src/slskd_lidarr_bridge/domain/models.py \
        tests/domain/test_albums.py
git commit -m "feat(albums): group disc subfolders into a single album"
```

---

### Task 3: Batch enqueue in the slskd gateway

**Files:**
- Modify: `src/slskd_lidarr_bridge/adapters/outbound/slskd_gateway.py`
- Modify: `src/slskd_lidarr_bridge/domain/ports.py:20`
- Modify: `tests/adapters/outbound/test_slskd_gateway.py` (replace the enqueue tests)
- Modify the `FakeGateway.enqueue` signature in: `tests/domain/test_download_service.py:51`,
  `tests/domain/test_search_service.py`, `tests/adapters/inbound/test_app.py`,
  `tests/e2e/test_flow.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `SoulseekGateway.enqueue(username: str, files: list[AudioFile], destination: str | None = None) -> None`
  - `EnqueueError(RuntimeError)` in `adapters/outbound/slskd_gateway.py`
  - `SlskdGateway.version() -> str`
  - `SlskdGateway.__init__` gains `enqueue_retry_delays: tuple[float, ...] = (0.5, 1.5)`
    and `sleep: Callable[[float], None] = time.sleep`

`destination` keeps a `None` default here so this task lands with behaviour
unchanged (slskd falls back to its own `subdirectory` pattern, which is what it
does today). Task 5 makes it required.

- [ ] **Step 1: Write the failing tests**

In `tests/adapters/outbound/test_slskd_gateway.py`, replace
`test_enqueue_posts_file_list` and `test_enqueue_encodes_traversal_username`
with:

```python
def test_enqueue_posts_a_batch_with_its_destination(respx_mock):
    route = respx_mock.post("http://slskd/api/v0/transfers/downloads/batches").mock(
        return_value=httpx.Response(201, json={"batch": {}, "failures": []})
    )
    gateway = _gateway()

    gateway.enqueue(
        "bob",
        [AudioFile(filename=r"@@a\Album\01.flac", size=5)],
        "Album [bob]/CD1",
    )

    assert route.called
    assert json.loads(route.calls[0].request.content) == {
        "username": "bob",
        "files": [{"filename": r"@@a\Album\01.flac", "size": 5}],
        "options": {"destination": "Album [bob]/CD1"},
    }


def test_enqueue_omits_options_without_a_destination(respx_mock):
    route = respx_mock.post("http://slskd/api/v0/transfers/downloads/batches").mock(
        return_value=httpx.Response(201, json={"batch": {}, "failures": []})
    )

    _gateway().enqueue("bob", [AudioFile(filename="x.flac", size=1)])

    assert "options" not in json.loads(route.calls[0].request.content)


def test_enqueue_raises_when_slskd_enqueued_nothing(respx_mock):
    # 200 means every file failed, not success. A bare raise_for_status would
    # read it as a win.
    respx_mock.post("http://slskd/api/v0/transfers/downloads/batches").mock(
        return_value=httpx.Response(
            200,
            json={"batch": {}, "failures": [{"filename": "x.flac", "message": "nope"}]},
        )
    )

    with pytest.raises(EnqueueError, match="nope"):
        _gateway().enqueue("bob", [AudioFile(filename="x.flac", size=1)], "Album")


def test_enqueue_raises_on_a_partial_batch(respx_mock):
    respx_mock.post("http://slskd/api/v0/transfers/downloads/batches").mock(
        return_value=httpx.Response(
            207,
            json={"batch": {}, "failures": [{"filename": "b.flac", "message": "busy"}]},
        )
    )

    with pytest.raises(EnqueueError, match="busy"):
        _gateway().enqueue("bob", [AudioFile(filename="b.flac", size=1)], "Album")


def test_enqueue_raises_when_the_peer_is_offline(respx_mock):
    respx_mock.post("http://slskd/api/v0/transfers/downloads/batches").mock(
        return_value=httpx.Response(404, text="User bob appears to be offline")
    )

    with pytest.raises(httpx.HTTPStatusError):
        _gateway().enqueue("bob", [AudioFile(filename="x.flac", size=1)], "Album")


def test_enqueue_retries_when_slskd_throttles(respx_mock):
    slept: list[float] = []
    respx_mock.post("http://slskd/api/v0/transfers/downloads/batches").mock(
        side_effect=[
            httpx.Response(429, text="Only one concurrent operation is permitted"),
            httpx.Response(201, json={"batch": {}, "failures": []}),
        ]
    )
    gateway = _gateway(enqueue_retry_delays=(0.01, 0.02), sleep=slept.append)

    gateway.enqueue("bob", [AudioFile(filename="x.flac", size=1)], "Album")

    assert slept == [0.01]


def test_enqueue_gives_up_after_exhausting_its_retries(respx_mock):
    slept: list[float] = []
    respx_mock.post("http://slskd/api/v0/transfers/downloads/batches").mock(
        return_value=httpx.Response(429, text="throttled")
    )
    gateway = _gateway(enqueue_retry_delays=(0.01, 0.02), sleep=slept.append)

    with pytest.raises(httpx.HTTPStatusError):
        gateway.enqueue("bob", [AudioFile(filename="x.flac", size=1)], "Album")

    assert slept == [0.01, 0.02]


def test_version_reads_the_application_version(respx_mock):
    respx_mock.get("http://slskd/api/v0/application/version").mock(
        return_value=httpx.Response(200, json="0.26.0")
    )

    assert _gateway().version() == "0.26.0"
```

Add a `_gateway()` helper near the top of the module if one does not already
exist, matching the module's existing construction style:

```python
def _gateway(**kwargs):
    return SlskdGateway("http://slskd", "key", client=httpx.Client(base_url="http://slskd"), **kwargs)
```

Make sure `json`, `pytest`, `httpx`, `AudioFile`, `EnqueueError` and
`SlskdGateway` are imported at the top of the test module.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/adapters/outbound/test_slskd_gateway.py -v`
Expected: FAIL with `ImportError: cannot import name 'EnqueueError'`.

- [ ] **Step 3: Implement the gateway changes**

In `src/slskd_lidarr_bridge/adapters/outbound/slskd_gateway.py`, add to the
imports:

```python
import logging
import time
from collections.abc import Callable
```

and, above the `SlskdGateway` class:

```python
logger = logging.getLogger(__name__)


class EnqueueError(RuntimeError):
    """slskd accepted the batch request but did not enqueue every file.

    The batch endpoint answers 200 when *every* file failed and 207 when some
    did, so neither is an HTTP error and neither is a success.
    """
```

Extend `__init__` with two parameters (keep the existing ones unchanged):

```python
        enqueue_retry_delays: tuple[float, ...] = (0.5, 1.5),
        sleep: Callable[[float], None] = time.sleep,
```

and store them:

```python
        self._enqueue_retry_delays = enqueue_retry_delays
        self._sleep = sleep
```

Document them in the class docstring's parameter list:

```
    enqueue_retry_delays:
        Delays, in seconds, between retries when slskd answers 429 to a batch
        enqueue. slskd guards enqueueing with a two-permit semaphore acquired
        without waiting, so a concurrent grab is throttled immediately rather
        than queued. One album now costs one request per disc, so retrying
        briefly is what keeps a multi-disc grab from failing on contention.
    sleep:
        Sleep function used between those retries; injectable for tests.
```

Replace the `enqueue` method with:

```python
    def enqueue(
        self,
        username: str,
        files: list[AudioFile],
        destination: str | None = None,
    ) -> None:
        """POST a download batch, optionally pinning its destination folder.

        ``destination`` is a path relative to slskd's downloads directory. slskd
        gives it precedence over the user's ``transfers.download.destination``
        pattern, which is what lets the bridge decide where an album lands and
        report that exact path to Lidarr. ``None`` leaves the choice to slskd.

        The batch endpoint's status codes are not conventional: 201 means every
        file was enqueued, 207 that some failed, and **200 that all of them
        did**. Only 201 is a success.
        """
        body: dict[str, object] = {
            "username": username,
            "files": [{"filename": f.filename, "size": f.size} for f in files],
        }
        if destination is not None:
            body["options"] = {"destination": destination}

        response = self._post_batch(body)

        if response.status_code in (200, 207):
            failures = response.json().get("failures", [])
            detail = "; ".join(
                f"{f.get('filename')}: {f.get('message')}" for f in failures
            )
            raise EnqueueError(
                f"slskd enqueued {len(files) - len(failures)} of {len(files)} "
                f"files for {username!r}: {detail}"
            )
        response.raise_for_status()

    def _post_batch(self, body: dict[str, object]) -> httpx.Response:
        """POST the batch, retrying while slskd's enqueue limiter answers 429."""
        response = self._req(
            "POST", "api", "v0", "transfers", "downloads", "batches", json=body
        )
        for delay in self._enqueue_retry_delays:
            if response.status_code != 429:
                return response
            logger.warning(
                "slskd throttled the enqueue (429); retrying in %.2fs", delay
            )
            self._sleep(delay)
            response = self._req(
                "POST", "api", "v0", "transfers", "downloads", "batches", json=body
            )
        return response
```

Add the version reader at the end of the class:

```python
    def version(self) -> str:
        """Return slskd's semantic version (``GET /api/v0/application/version``).

        Deliberately not part of the ``SoulseekGateway`` port: only the
        composition root reads it, to refuse to start against a slskd too old
        for batch destinations.
        """
        r = self._req("GET", "api", "v0", "application", "version")
        r.raise_for_status()
        return str(r.json())
```

- [ ] **Step 4: Update the port**

In `src/slskd_lidarr_bridge/domain/ports.py`, replace the `enqueue` line:

```python
    def enqueue(
        self,
        username: str,
        files: list[AudioFile],
        destination: str | None = None,
    ) -> None: ...
```

- [ ] **Step 5: Update every test fake**

In each of `tests/domain/test_download_service.py`,
`tests/domain/test_search_service.py`, `tests/adapters/inbound/test_app.py` and
`tests/e2e/test_flow.py`, change the fake's `enqueue` to record the destination:

```python
    def enqueue(
        self,
        username: str,
        files: list[AudioFile],
        destination: str | None = None,
    ) -> None:
        self._assert_not_locked()
        self.enqueued.append((username, list(files), destination))
```

Drop the `_assert_not_locked()` call in fakes that do not define it. Where the
fake annotates the list, widen it to match, otherwise mypy fails on the extra
element:

```python
        self.enqueued: list[tuple[str, list[AudioFile], str | None]] = []
```

Update the existing assertions on `gateway.enqueued` in those modules to expect
the extra tuple element (`(username, files, None)` for now).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS.

- [ ] **Step 7: Run the full gate**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`
Expected: all pass, coverage 100%.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(slskd): enqueue through the batch endpoint with a destination"
```

---

### Task 4: Disc-aware releases

**Files:**
- Modify: `src/slskd_lidarr_bridge/domain/models.py` (`Release`)
- Modify: `src/slskd_lidarr_bridge/domain/search_service.py:136-196` (`_collect`)
- Modify: `src/slskd_lidarr_bridge/adapters/outbound/sqlite_store.py`
- Modify: `src/slskd_lidarr_bridge/adapters/inbound/newznab.py:200-206`
- Test: `tests/domain/test_search_service.py`, `tests/domain/test_models.py`,
  `tests/adapters/outbound/test_sqlite_store.py`,
  `tests/adapters/inbound/test_newznab.py`

**Interfaces:**
- Consumes: `Disc`, `group_files_by_album`, `AlbumGroup` (Task 2);
  `build_album_destination` (Task 1).
- Produces:
  - `Release(artist, album, title, username, discs, size, album_path,
    destination, quality, created_at, id=None)` with a `files` property
  - `SCHEMA_VERSION = 2` in `adapters/outbound/sqlite_store.py`
  - NZB payload gains `destination: str` and
    `discs: list[{"subpath": str, "files": [{"filename": str, "size": int}]}]`

The payload keeps its `album_folder` and `files` keys in this task so the
download side, still reading them, keeps working. Task 5 removes them.

- [ ] **Step 1: Write the failing tests**

In `tests/domain/test_search_service.py`, add:

```python
def test_multi_disc_album_becomes_one_release():
    files = (
        AudioFile(filename=r"@@a\Artist\Album\CD1\01.flac", size=10, extension=".flac"),
        AudioFile(filename=r"@@a\Artist\Album\CD2\01.flac", size=20, extension=".flac"),
    )
    gateway = FakeGateway(responses=[SearchResponse("bob", True, 100, 0, files)])
    service = _service(gateway)

    releases = service.search(SearchQuery(artist="Artist", album="Album"))

    assert len(releases) == 1
    release = releases[0]
    assert release.album == "Album"
    assert release.artist == "Artist"
    assert release.size == 30
    assert release.album_path == "@@a/Artist/Album"
    assert release.destination == "Album [bob]"
    assert [d.subpath for d in release.discs] == ["CD1", "CD2"]


def test_single_disc_release_keeps_one_unnamed_disc():
    files = (
        AudioFile(filename=r"@@a\Artist\Album\01.flac", size=10, extension=".flac"),
    )
    gateway = FakeGateway(responses=[SearchResponse("bob", True, 100, 0, files)])

    releases = _service(gateway).search(SearchQuery(artist="Artist", album="Album"))

    assert [d.subpath for d in releases[0].discs] == [""]
    assert releases[0].destination == "Album [bob]"


def test_artist_and_album_split_on_the_album_folder_name():
    files = (
        AudioFile(
            filename=r"@@a\Music\Pink Floyd - Animals\01.flac",
            size=10,
            extension=".flac",
        ),
    )
    gateway = FakeGateway(responses=[SearchResponse("bob", True, 100, 0, files)])

    release = _service(gateway).search(SearchQuery(term="animals"))[0]

    assert (release.artist, release.album) == ("Pink Floyd", "Animals")
```

Use the module's existing helper for building the service; if there is none,
build `SearchService(gateway, FakeStore(), FakeClock())` inline as the
neighbouring tests do, and match the existing `FakeGateway` constructor.

In `tests/adapters/outbound/test_sqlite_store.py`, add:

```python
def test_schema_is_recreated_when_the_version_predates_this_build(tmp_path, caplog):
    db = tmp_path / "bridge.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE releases (id TEXT PRIMARY KEY);"
        "CREATE TABLE jobs (nzo_id TEXT PRIMARY KEY);"
        "PRAGMA user_version = 1;"
    )
    conn.execute("INSERT INTO releases (id) VALUES ('old')")
    conn.commit()
    conn.close()

    with caplog.at_level(logging.WARNING):
        store = SqliteStore(str(db))

    assert "dropping" in caplog.text.lower()
    assert store.get_release("old") is None
    store.close()


def test_a_fresh_database_is_stamped_without_a_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        store = SqliteStore(str(tmp_path / "bridge.db"))

    assert caplog.text == ""
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    store.close()


def test_reopening_a_current_database_keeps_its_rows(tmp_path, sample_release):
    path = str(tmp_path / "bridge.db")
    first = SqliteStore(path)
    release_id = first.put(sample_release)
    first.close()

    second = SqliteStore(path)
    assert second.get_release(release_id) is not None
    second.close()
```

Adapt `sample_release` to whatever release factory the module already uses, and
import `logging`, `sqlite3`, `SqliteStore` and `SCHEMA_VERSION`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/domain/test_search_service.py tests/adapters/outbound/test_sqlite_store.py -v`
Expected: FAIL with `TypeError: Release.__init__() got an unexpected keyword argument 'discs'`
and `ImportError: cannot import name 'SCHEMA_VERSION'`.

- [ ] **Step 3: Update the `Release` model**

In `src/slskd_lidarr_bridge/domain/models.py`, replace the `Release` class:

```python
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
```

- [ ] **Step 4: Rewrite `SearchService._collect`**

In `src/slskd_lidarr_bridge/domain/search_service.py`, add the imports:

```python
from slskd_lidarr_bridge.domain.albums import group_files_by_album
from slskd_lidarr_bridge.domain.paths import build_album_destination
```

and replace the body of the `for response in responses:` loop after the `audio`
filter with:

```python
            for group in group_files_by_album(audio):
                key = (response.username, group.album_path)
                if key in seen:
                    continue

                folder = group.name
                if " - " in folder:
                    left, right = folder.split(" - ", 1)
                    artist, album = left.strip(), right.strip()
                else:
                    album = folder
                    artist = group.parent_name

                quality = detect_quality(group.files)
                title = build_title(artist, album, quality, response.username)
                release = Release(
                    artist=artist,
                    album=album,
                    title=title,
                    username=response.username,
                    discs=group.discs,
                    size=sum(f.size for f in group.files),
                    album_path=group.album_path,
                    destination=build_album_destination(folder, response.username),
                    quality=quality,
                    created_at=self._clock.now(),
                )
                release_id = self._store.put(release)
                release = dataclasses.replace(release, id=release_id)
                seen.add(key)
                tagged.append(
                    (
                        response.has_free_upload_slot,
                        response.upload_speed,
                        response.queue_length,
                        release,
                    )
                )
```

Delete the now-unused `defaultdict` import and the `groups` dict it fed.

- [ ] **Step 5: Migrate the store**

In `src/slskd_lidarr_bridge/adapters/outbound/sqlite_store.py`, add
`import logging`, `logger = logging.getLogger(__name__)`, import `Disc`, and:

```python
# Bumped whenever the table layout changes. The database holds a 7-day release
# cache and in-flight jobs, both cheap to lose, so a mismatch recreates the
# tables rather than migrating them.
SCHEMA_VERSION = 2
```

Change the `releases` DDL to:

```sql
CREATE TABLE IF NOT EXISTS releases (
    id          TEXT PRIMARY KEY,
    artist      TEXT NOT NULL,
    album       TEXT NOT NULL,
    title       TEXT NOT NULL,
    username    TEXT NOT NULL,
    discs       TEXT NOT NULL,   -- JSON
    size        INTEGER NOT NULL,
    album_path  TEXT NOT NULL,
    destination TEXT NOT NULL,
    quality     TEXT NOT NULL,
    created_at  TEXT NOT NULL    -- ISO-8601
);
```

Add disc serialisation next to `_files_to_json`:

```python
def _discs_to_json(discs: tuple[Disc, ...]) -> str:
    return json.dumps(
        [
            {"subpath": d.subpath, "files": json.loads(_files_to_json(d.files))}
            for d in discs
        ]
    )


def _json_to_discs(raw: str) -> tuple[Disc, ...]:
    return tuple(
        Disc(subpath=d["subpath"], files=_json_to_files(json.dumps(d["files"])))
        for d in json.loads(raw)
    )
```

Replace `__init__`'s schema block and add the migration:

```python
        with self._write_lock:
            self._migrate()
            self._conn.executescript(_DDL)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()

    def _migrate(self) -> None:
        """Drop the tables when they predate this build's schema.

        There is nothing worth migrating: releases are a 7-day cache. Jobs in
        flight are lost, which Lidarr recovers from by re-grabbing, so it is
        worth a warning but not a migration path.
        """
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        existing = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('releases', 'jobs')"
        ).fetchall()
        if existing:
            logger.warning(
                "Database schema is v%s but this build needs v%s: dropping the "
                "cached releases and any in-flight downloads. Lidarr will "
                "re-grab anything that was still downloading.",
                version,
                SCHEMA_VERSION,
            )
        self._conn.executescript(
            "DROP TABLE IF EXISTS releases; DROP TABLE IF EXISTS jobs;"
        )
```

Update `put` to hash `album_path` and write the new columns:

```python
        new_id = hashlib.sha256(
            f"{release.username}\x00{release.album_path}".encode()
        ).hexdigest()
        with self._write_lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO releases
                    (id, artist, album, title, username, discs, size,
                     album_path, destination, quality, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id,
                    release.artist,
                    release.album,
                    release.title,
                    release.username,
                    _discs_to_json(release.discs),
                    release.size,
                    release.album_path,
                    release.destination,
                    release.quality,
                    release.created_at.isoformat(),
                ),
            )
            self._conn.commit()
        return new_id
```

In `put`'s docstring, replace `album_folder` with `album_path` and note that
hashing the full remote path (not just the folder name) keeps two albums of one
peer whose folders share a name apart.

Update `get_release` to build the new `Release`:

```python
        return Release(
            id=row["id"],
            artist=row["artist"],
            album=row["album"],
            title=row["title"],
            username=row["username"],
            discs=_json_to_discs(row["discs"]),
            size=row["size"],
            album_path=row["album_path"],
            destination=row["destination"],
            quality=row["quality"],
            created_at=_parse_dt(row["created_at"]),
        )
```

- [ ] **Step 6: Extend the NZB payload**

In `src/slskd_lidarr_bridge/adapters/inbound/newznab.py`, replace the payload
dict:

```python
        payload = {
            "username": release.username,
            "title": release.title,
            # Legacy keys, still read by DownloadService until it is disc-aware.
            "album_folder": release.destination,
            "files": [{"filename": f.filename, "size": f.size} for f in release.files],
            "total_size": release.size,
            "destination": release.destination,
            "discs": [
                {
                    "subpath": disc.subpath,
                    "files": [
                        {"filename": f.filename, "size": f.size} for f in disc.files
                    ],
                }
                for disc in release.discs
            ],
        }
```

- [ ] **Step 7: Fix every remaining `Release(...)` construction in tests**

Run `uv run pytest -x` repeatedly and update each failing construction: replace
`files=(...)` with `discs=(Disc(subpath="", files=(...)),)`, replace
`album_folder="X"` with `album_path="@@a/X"` and `destination="X [user]"`.
Import `Disc` where needed.

- [ ] **Step 8: Run the full gate**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`
Expected: all pass, coverage 100%.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat(search): merge disc subfolders into one release with a chosen destination"
```

---

### Task 5: Disc-aware downloads

**Files:**
- Modify: `src/slskd_lidarr_bridge/domain/models.py` (`DownloadJob`, `Transfer`)
- Modify: `src/slskd_lidarr_bridge/domain/paths.py` (`compute_storage_path`)
- Modify: `src/slskd_lidarr_bridge/domain/download_service.py`
- Modify: `src/slskd_lidarr_bridge/domain/ports.py` (drop the `destination` default)
- Modify: `src/slskd_lidarr_bridge/adapters/outbound/slskd_gateway.py` (same)
- Modify: `src/slskd_lidarr_bridge/adapters/outbound/sqlite_store.py` (jobs table)
- Modify: `src/slskd_lidarr_bridge/adapters/inbound/newznab.py` (drop legacy keys)
- Test: `tests/domain/test_download_service.py`, `tests/domain/test_paths.py`,
  `tests/e2e/test_flow.py`

**Interfaces:**
- Consumes: `Disc` (Task 2), `batch_destination` (Task 1),
  `SoulseekGateway.enqueue(..., destination)` (Task 3), the payload's
  `destination` and `discs` keys (Task 4).
- Produces:
  - `DownloadJob(nzo_id, title, username, discs, category, destination,
    total_size, created_at)` with a `files` property
  - `compute_storage_path(downloads_dir: str, destination: str) -> str`
  - `SoulseekGateway.enqueue(username, files, destination: str)` (no default)

- [ ] **Step 1: Write the failing tests**

In `tests/domain/test_download_service.py`, add:

```python
def _payload(discs, destination="Album [bob]"):
    return {
        "username": "bob",
        "title": "Artist - Album [FLAC]-bob",
        "destination": destination,
        "total_size": sum(f["size"] for d in discs for f in d["files"]),
        "discs": discs,
    }


def test_start_enqueues_one_batch_per_disc():
    gateway = FakeGateway()
    service = DownloadService(gateway, FakeJobStore(), FakeClock())

    service.start(
        _payload(
            [
                {"subpath": "CD1", "files": [{"filename": r"@@a\Al\CD1\1.flac", "size": 5}]},
                {"subpath": "CD2", "files": [{"filename": r"@@a\Al\CD2\1.flac", "size": 7}]},
            ]
        ),
        "music",
    )

    assert [(u, d) for u, _, d in gateway.enqueued] == [
        ("bob", "Album [bob]/CD1"),
        ("bob", "Album [bob]/CD2"),
    ]


def test_start_of_a_single_disc_album_uses_the_album_folder_itself():
    gateway = FakeGateway()
    service = DownloadService(gateway, FakeJobStore(), FakeClock())

    service.start(
        _payload([{"subpath": "", "files": [{"filename": r"@@a\Al\1.flac", "size": 5}]}]),
        "music",
    )

    assert gateway.enqueued[0][2] == "Album [bob]"


def test_start_cancels_what_it_queued_when_a_later_disc_fails():
    # Without this, disc 1 keeps downloading into a folder Lidarr will never be
    # told about: silent disk fill.
    gateway = FakeGateway(
        transfers_by_username={
            "bob": [
                Transfer(
                    username="bob",
                    id="t1",
                    filename=r"@@a\Al\CD1\1.flac",
                    size=5,
                    state="InProgress",
                    bytes_transferred=1,
                    bytes_remaining=4,
                    percent_complete=20.0,
                )
            ]
        }
    )
    gateway.fail_enqueue_after = 1
    service = DownloadService(gateway, FakeJobStore(), FakeClock())

    with pytest.raises(RuntimeError):
        service.start(
            _payload(
                [
                    {"subpath": "CD1", "files": [{"filename": r"@@a\Al\CD1\1.flac", "size": 5}]},
                    {"subpath": "CD2", "files": [{"filename": r"@@a\Al\CD2\1.flac", "size": 7}]},
                ]
            ),
            "music",
        )

    assert gateway.cancelled == [("bob", "t1")]


def test_start_does_not_cancel_when_the_very_first_disc_fails():
    gateway = FakeGateway()
    gateway.fail_enqueue_after = 0
    service = DownloadService(gateway, FakeJobStore(), FakeClock())

    with pytest.raises(RuntimeError):
        service.start(
            _payload([{"subpath": "CD1", "files": [{"filename": "a.flac", "size": 5}]}]),
            "music",
        )

    assert gateway.cancelled == []


def test_completed_job_reports_the_destination_as_its_storage_path():
    job = DownloadJob(
        nzo_id="SABnzbd_nzo_1",
        title="t",
        username="bob",
        discs=(Disc(subpath="CD1", files=(AudioFile(filename=r"@@a\Al\CD1\1.flac", size=5),)),),
        category="music",
        destination="Album [bob]",
        total_size=5,
        created_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    gateway = FakeGateway(
        transfers_by_username={
            "bob": [
                Transfer(
                    username="bob",
                    id="t1",
                    filename=r"@@a\Al\CD1\1.flac",
                    size=5,
                    state="Completed, Succeeded",
                    bytes_transferred=5,
                    bytes_remaining=0,
                    percent_complete=100.0,
                )
            ]
        }
    )
    jobs = FakeJobStore()
    jobs.add(job)
    service = DownloadService(gateway, jobs, FakeClock())

    view = service.statuses()[0]

    assert view.state == "completed"
    assert view.storage == "/downloads/Album [bob]"


def _failed_transfer(filename: str, size: int, transfer_id: str) -> Transfer:
    return Transfer(
        username="bob",
        id=transfer_id,
        filename=filename,
        size=size,
        state="Completed, Errored",
        bytes_transferred=0,
        bytes_remaining=size,
        percent_complete=0.0,
        exception="peer went away",
    )


def test_retry_reenqueues_failed_files_into_their_own_disc():
    cd1 = AudioFile(filename=r"@@a\Al\CD1\1.flac", size=5)
    cd2 = AudioFile(filename=r"@@a\Al\CD2\1.flac", size=7)
    job = DownloadJob(
        nzo_id="SABnzbd_nzo_1",
        title="t",
        username="bob",
        discs=(Disc(subpath="CD1", files=(cd1,)), Disc(subpath="CD2", files=(cd2,))),
        category="music",
        destination="Album [bob]",
        total_size=12,
        created_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    gateway = FakeGateway(
        transfers_by_username={
            "bob": [
                Transfer(
                    username="bob",
                    id="t1",
                    filename=cd1.filename,
                    size=5,
                    state="Completed, Succeeded",
                    bytes_transferred=5,
                    bytes_remaining=0,
                    percent_complete=100.0,
                ),
                _failed_transfer(cd2.filename, 7, "t2"),
            ]
        }
    )
    jobs = FakeJobStore()
    jobs.add(job)
    service = DownloadService(gateway, jobs, FakeClock())

    service.statuses()

    assert gateway.enqueued == [("bob", [cd2], "Album [bob]/CD2")]


def test_a_failing_retry_does_not_break_the_queue_listing(caplog):
    # An exception here would fail SABnzbd's whole queue endpoint, which makes
    # Lidarr treat the download client as down. The stall detector is the
    # backstop instead.
    failed = AudioFile(filename=r"@@a\Al\CD1\1.flac", size=5)
    job = DownloadJob(
        nzo_id="SABnzbd_nzo_1",
        title="t",
        username="bob",
        discs=(Disc(subpath="CD1", files=(failed,)),),
        category="music",
        destination="Album [bob]",
        total_size=5,
        created_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    gateway = FakeGateway(
        transfers_by_username={"bob": [_failed_transfer(failed.filename, 5, "t1")]}
    )
    gateway.fail_enqueue_after = 0
    jobs = FakeJobStore()
    jobs.add(job)
    service = DownloadService(gateway, jobs, FakeClock())

    with caplog.at_level(logging.WARNING):
        views = service.statuses()

    assert [v.state for v in views] == ["downloading"]
    assert "Retry enqueue failed" in caplog.text
```

Extend `FakeGateway` with the failure hook and destination recording. Keep every
existing line of `__init__` and add the two below; `enqueue` replaces the version
Task 3 left behind:

```python
    # Added at the end of the existing __init__ body:
        self.enqueued: list[tuple[str, list[AudioFile], str]] = []
        # None: never fail. N: raise on the (N+1)-th enqueue call.
        self.fail_enqueue_after: int | None = None

    def enqueue(self, username: str, files: list[AudioFile], destination: str) -> None:
        self._assert_not_locked()
        if self.fail_enqueue_after is not None and (
            len(self.enqueued) >= self.fail_enqueue_after
        ):
            raise RuntimeError("slskd said no")
        self.enqueued.append((username, list(files), destination))
```

`FakeGateway.downloads_directory` already returns `/downloads`; leave it alone.

In `tests/domain/test_paths.py`, replace the `compute_storage_path` tests with:

```python
def test_compute_storage_path_joins_the_destination():
    assert compute_storage_path("/downloads", "Album [bob]") == "/downloads/Album [bob]"


def test_compute_storage_path_tolerates_a_trailing_slash():
    assert compute_storage_path("/downloads/", "Album") == "/downloads/Album"


def test_compute_storage_path_tolerates_a_root_downloads_dir():
    assert compute_storage_path("/", "Album") == "/Album"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/domain/test_download_service.py -v`
Expected: FAIL with `KeyError: 'album_folder'` or
`TypeError: DownloadJob.__init__() got an unexpected keyword argument 'discs'`.

- [ ] **Step 3: Update the models**

In `src/slskd_lidarr_bridge/domain/models.py`, replace `DownloadJob`:

```python
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
```

and delete `local_path` from `Transfer` (both the field and the comment): slskd
exposes no on-disk path there, and the bridge now chooses the path itself.

Also delete the `AudioFile.album_folder` and `AudioFile.artist_folder`
properties, along with their tests in `tests/domain/test_models.py`. Task 4
removed their last production callers when `SearchService._collect` moved to
`group_files_by_album`, which derives both from the album root instead. Verified
before amending this plan: `grep -rn "artist_folder\|\.album_folder" src/`
returns no reader of either property outside their own definitions. Leaving them
would be dead surface area that the next reader would take for live API.

- [ ] **Step 4: Simplify `compute_storage_path`**

In `src/slskd_lidarr_bridge/domain/paths.py`, replace the whole function
(including its multi-disc note, which no longer applies) with:

```python
def compute_storage_path(downloads_dir: str, destination: str) -> str:
    """Return the local folder holding a completed download.

    ``destination`` is what the bridge told slskd to use when enqueueing the
    batches (see ``build_album_destination``), so this is an exact path rather
    than an inference about slskd's layout: slskd gives a batch destination
    precedence over its own ``transfers.download.destination`` pattern, and the
    segments were sanitized to survive slskd's own sanitization unchanged.
    """
    # Strip any trailing slash; guard against "/" → "" which would produce a
    # relative path.  If stripping empties the string, treat base as "/".
    base = downloads_dir.rstrip("/") or "/"
    return posixpath.join(base, destination)
```

Delete the now-unused `PurePosixPath` import if nothing else uses it.

- [ ] **Step 5: Rewrite the download service**

In `src/slskd_lidarr_bridge/domain/download_service.py`, import
`batch_destination` and `Disc`, drop the `PurePosixPath` import, and replace
`start`:

```python
    def start(self, payload: dict[str, Any], category: str) -> str:
        """Enqueue one slskd batch per disc, persist a DownloadJob, return its id.

        A batch destination is what puts every disc under one album folder, so
        the batches are issued per disc. They are not atomic: if a later disc
        fails, the discs already queued are cancelled, otherwise they would keep
        downloading into a folder Lidarr is never told about.
        """
        username: str = payload["username"]
        title: str = payload["title"]
        destination: str = payload["destination"]
        total_size: int = payload["total_size"]

        discs = tuple(
            Disc(
                subpath=d["subpath"],
                files=tuple(
                    AudioFile(filename=f["filename"], size=f["size"])
                    for f in d["files"]
                ),
            )
            for d in payload["discs"]
        )

        queued: list[Disc] = []
        try:
            for disc in discs:
                self._gateway.enqueue(
                    username,
                    list(disc.files),
                    batch_destination(destination, disc.subpath),
                )
                queued.append(disc)
        except Exception:
            if queued:
                logger.warning(
                    "Enqueue of %r failed partway through; cancelling %d disc(s) "
                    "already queued",
                    title,
                    len(queued),
                )
                self._cancel_files(username, [f for d in queued for f in d.files])
            raise

        nzo_id = "SABnzbd_nzo_" + uuid4().hex[:12]

        job = DownloadJob(
            nzo_id=nzo_id,
            title=title,
            username=username,
            discs=discs,
            category=category,
            destination=destination,
            total_size=total_size,
            created_at=self._clock.now(),
        )
        self._jobs.add(job)
        logger.info("Enqueued download %s: %r (cat=%r)", nzo_id, title, category)
        return nzo_id
```

Add the shared cancellation helper:

```python
    def _cancel_files(self, username: str, files: Sequence[AudioFile]) -> None:
        """Cancel any in-progress transfer among *files*.

        Best effort: a peer or slskd failing here must not stop the caller, who
        is either already handling an error or removing a job that Lidarr
        considers gone.
        """
        wanted = {f.filename for f in files}
        try:
            transfers = self._gateway.transfers(username)
        except Exception:
            logger.warning(
                "Could not list %r's transfers to cancel them", username, exc_info=True
            )
            return
        for transfer in transfers:
            if transfer.filename in wanted and not transfer.is_complete:
                try:
                    self._gateway.cancel(username, transfer.id)
                except Exception:
                    logger.warning(
                        "Could not cancel transfer %s for %r",
                        transfer.id,
                        username,
                        exc_info=True,
                    )
```

In `statuses`, replace the completed branch:

```python
            if len(matched) == len(job.files) and all(t.is_succeeded for t in matched):
                state = "completed"
                storage = compute_storage_path(
                    self._gateway.downloads_directory(), job.destination
                )
```

and the retry branch's enqueue:

```python
                if do_retry:
                    failed_names = {t.filename for t in matched if t.is_failed}
                    try:
                        for disc in job.discs:
                            retry_files = [
                                f for f in disc.files if f.filename in failed_names
                            ]
                            if retry_files:
                                self._gateway.enqueue(
                                    job.username,
                                    retry_files,
                                    batch_destination(job.destination, disc.subpath),
                                )
                    except Exception:
                        # Never let this escape: statuses() backs SABnzbd's queue
                        # endpoint, and an exception here makes Lidarr treat the
                        # whole download client as down. The stall detector is
                        # the backstop.
                        logger.warning(
                            "Retry enqueue failed for %s (%r)",
                            job.nzo_id,
                            job.title,
                            exc_info=True,
                        )
                    else:
                        logger.info(
                            "Retrying download %s after failure (attempt %d/%d): %r",
                            job.nzo_id,
                            used + 1,
                            self._max_retries,
                            job.title,
                        )
```

Replace `remove`'s cancellation loop with the helper:

```python
        self._cancel_files(job.username, job.files)
        self._jobs.remove(nzo_id)
```

Add `from collections.abc import Sequence` to the imports.

- [ ] **Step 6: Update the jobs table**

In `src/slskd_lidarr_bridge/adapters/outbound/sqlite_store.py`, change the
`jobs` DDL to use `discs TEXT NOT NULL` and `destination TEXT NOT NULL` in place
of `files` and `album_folder`, and update `add`, `get_job` and `list` to read and
write `_discs_to_json(job.discs)` / `_json_to_discs(row["discs"])` and
`row["destination"]`.

- [ ] **Step 7: Drop the legacy payload keys and the destination default**

In `src/slskd_lidarr_bridge/adapters/inbound/newznab.py`, delete the
`"album_folder"` and `"files"` entries and the comment above them.

In `src/slskd_lidarr_bridge/domain/ports.py` and
`src/slskd_lidarr_bridge/adapters/outbound/slskd_gateway.py`, make the parameter
required: `destination: str` (no `= None`), and drop the `if destination is not
None:` guard in the gateway, always sending `body["options"]`. Update the
gateway docstring accordingly and delete
`test_enqueue_omits_options_without_a_destination`.

- [ ] **Step 8: Run the full gate**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`
Expected: all pass, coverage 100%. Fix the e2e flow test's payload and fakes as
needed.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat(download): one batch per disc under a shared album folder"
```

---

### Task 6: Refuse to start against a slskd older than 0.26.0

**Files:**
- Modify: `src/slskd_lidarr_bridge/main.py:27-56`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `SlskdGateway.version()` (Task 3).
- Produces: `MINIMUM_SLSKD_VERSION: tuple[int, int, int]` in `main.py`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_main.py`, add:

```python
class _VersionGateway:
    def __init__(self, version):
        self._version = version

    def version(self):
        if isinstance(self._version, Exception):
            raise self._version
        return self._version


def test_build_app_refuses_a_slskd_older_than_the_minimum(monkeypatch, tmp_path, env):
    monkeypatch.setattr(
        main, "SlskdGateway", lambda *a, **k: _VersionGateway("0.25.1")
    )

    with pytest.raises(RuntimeError, match="0.26.0"):
        main.build_app(env, db_path=str(tmp_path / "b.db"))


@pytest.mark.parametrize("reported", ["0.0.0", "not-a-version"])
def test_build_app_accepts_an_unrecognisable_version_with_a_warning(
    monkeypatch, tmp_path, env, caplog, reported
):
    monkeypatch.setattr(
        main, "SlskdGateway", lambda *a, **k: _VersionGateway(reported)
    )

    with caplog.at_level(logging.WARNING):
        main.build_app(env, db_path=str(tmp_path / "b.db"))

    assert "version" in caplog.text.lower()


def test_build_app_continues_when_slskd_is_unreachable(
    monkeypatch, tmp_path, env, caplog
):
    # A container that starts before slskd must not crash-loop.
    monkeypatch.setattr(
        main,
        "SlskdGateway",
        lambda *a, **k: _VersionGateway(httpx.ConnectError("down")),
    )

    with caplog.at_level(logging.WARNING):
        main.build_app(env, db_path=str(tmp_path / "b.db"))

    assert "could not read" in caplog.text.lower()


def test_build_app_accepts_a_supported_version(monkeypatch, tmp_path, env, caplog):
    monkeypatch.setattr(
        main, "SlskdGateway", lambda *a, **k: _VersionGateway("0.26.1")
    )

    with caplog.at_level(logging.WARNING):
        main.build_app(env, db_path=str(tmp_path / "b.db"))

    assert caplog.text == ""
```

Reuse the module's existing environment fixture; if there is none, build the
env dict inline exactly as the neighbouring `build_app` tests do.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_main.py -v`
Expected: FAIL, `build_app` does not raise for `0.25.1`.

- [ ] **Step 3: Implement the check**

In `src/slskd_lidarr_bridge/main.py`, add `import re`, and:

```python
# Batch download destinations, which the bridge relies on to place every disc of
# an album under one folder, landed in slskd 0.26.0.
MINIMUM_SLSKD_VERSION = (0, 26, 0)
_MINIMUM_TEXT = ".".join(str(part) for part in MINIMUM_SLSKD_VERSION)


def _check_slskd_version(gateway: SlskdGateway) -> None:
    """Refuse to start against a slskd too old for batch download destinations.

    Two cases deliberately warn instead of failing: an unreadable version (slskd
    not up yet is transient, and crash-looping the container helps nobody), and
    a version that does not parse, which is what development builds report
    (``0.0.0``).
    """
    try:
        reported = gateway.version()
    except Exception:
        logger.warning(
            "Could not read slskd's version; continuing. This bridge needs "
            "slskd %s or newer.",
            _MINIMUM_TEXT,
            exc_info=True,
        )
        return

    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", reported)
    if match is None or reported.startswith("0.0.0"):
        logger.warning(
            "slskd reports version %r, which is not a release version "
            "(development build?). Assuming it is at least %s.",
            reported,
            _MINIMUM_TEXT,
        )
        return

    if tuple(int(part) for part in match.groups()) < MINIMUM_SLSKD_VERSION:
        raise RuntimeError(
            f"slskd {reported} is too old: this bridge requires slskd "
            f"{_MINIMUM_TEXT} or newer, which introduced per-batch download "
            f"destinations (multi-disc album support)."
        )
```

Call it in `build_app`, right after the gateway is built:

```python
    _check_slskd_version(gateway)
```

and add to `build_app`'s `Raises:` section:

```
        RuntimeError: if slskd is reachable and older than the minimum version.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_main.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full gate**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`
Expected: all pass, coverage 100%.

- [ ] **Step 6: Commit**

```bash
git add src/slskd_lidarr_bridge/main.py tests/test_main.py
git commit -m "feat(main): require slskd 0.26.0 or newer at startup"
```

---

### Task 7: Documentation

**Files:**
- Modify: `README.md:3`, `README.md:38`, `README.md:84-86`, `README.md:108`
- Modify: `AGENTS.md` (the "Gotchas" section)

**Interfaces:**
- Consumes: everything above.
- Produces: no code.

- [ ] **Step 1: Update the README's requirements**

Under the compose example, add a requirement line:

```markdown
> **slskd `0.26.0` or newer is required.** The bridge tells slskd where to place
> each download (a per-batch destination), which is what lets it keep every disc
> of an album under one folder. The bridge refuses to start against an older
> slskd. Note that slskd `0.26.0` also carries a breaking configuration change of
> its own: see its [release notes](https://github.com/slskd/slskd/releases/tag/0.26.0).
```

- [ ] **Step 2: Rewrite the completed-downloads path section**

Replace the paragraph at `README.md:84`:

```markdown
The bridge chooses the folder each download lands in and reports it to Lidarr:
`<slskd's downloads dir>/<album folder> [<peer>]`, with each disc of a
multi-disc album in its own subfolder underneath. slskd's downloads directory is
read from its API, so there is no path configuration on the bridge, and slskd's
own `transfers.download.destination.subdirectory` pattern does not apply to
bridge downloads (a destination sent with a batch takes precedence over it).
```

Keep the existing Remote Path Mapping paragraph that follows.

- [ ] **Step 3: Replace the multi-disc limitation bullet**

At `README.md:108`, delete the "Multi-disc albums are not supported" bullet
entirely. Add, to the same list:

```markdown
- **Upgrading resets the bridge's database.** The schema changed with multi-disc
  support; on first start the bridge drops its cached releases and any jobs it
  was tracking. Downloads in flight at that moment are forgotten by the bridge
  (slskd keeps downloading them) and Lidarr re-grabs them. Nothing else is
  affected: the database holds no configuration.
```

- [ ] **Step 4: Update the AGENTS.md gotcha**

Replace the "Multi-disc albums are unsupported" bullet with:

```markdown
- **Multi-disc albums** are grouped at search time (`domain/albums.py`) and
  downloaded one slskd batch per disc into `<album> [<peer>]/<disc>`
  (`domain/download_service.py`). This needs slskd >= 0.26.0, whose batch
  endpoint accepts a destination; the design and the verified slskd contract are
  in `docs/specs/2026-07-21-multi-disc-albums-design.md`.
```

- [ ] **Step 5: Verify no stale references remain**

Run: `grep -rn "album_folder\|multi-disc" README.md AGENTS.md src/`
Expected: no hits in `src/`, and only the new wording in the docs.

- [ ] **Step 6: Run the full gate and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add README.md AGENTS.md
git commit -m "docs: describe multi-disc support and the slskd 0.26 requirement"
```

---

## Verification before opening the PR

- [ ] `uv run ruff check .` passes
- [ ] `uv run ruff format --check .` passes
- [ ] `uv run mypy` passes
- [ ] `uv run pytest` passes with 100% line and branch coverage
- [ ] `grep -rn "local_path\|album_folder" src/` returns nothing
- [ ] The e2e flow test covers a multi-disc album end to end: search returns
      `Album/CD1` + `Album/CD2` from one peer, one release is offered, the NZB
      round-trips, two batches are enqueued with destinations
      `Album [peer]/CD1` and `Album [peer]/CD2`, and the completed job reports
      `<downloads>/Album [peer]`. Add it to `tests/e2e/test_flow.py` if the
      existing flow only covers a single disc.
