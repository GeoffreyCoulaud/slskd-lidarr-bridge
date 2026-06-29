import xml.etree.ElementTree as ET

import pytest

from slskd_lidarr_bridge.web.nzb import build_nzb, parse_nzb

NZB_NS = "http://www.newzbin.com/DTD/2003/nzb"

PAYLOAD = {
    "username": "some_user",
    "title": "Björk – Homogenic (1997) [FLAC]",
    "album_folder": "Homogenic (1997) [FLAC]",
    "total_size": 999_999_999_999,
    "files": [
        {"filename": r"@@some_user\Music\Björk\Homogenic\01.flac", "size": 499_999_999_999},
        {"filename": r"@@some_user\Music\Björk\Homogenic\02.flac", "size": 500_000_000_000},
    ],
}


def test_round_trip_identity():
    """parse_nzb(build_nzb(p)) == p for payload with two files, unicode, large sizes."""
    assert parse_nzb(build_nzb(PAYLOAD)) == PAYLOAD


def test_build_produces_valid_xml_with_nzb_root():
    """build_nzb returns bytes parseable as XML whose root localname is 'nzb'."""
    data = build_nzb(PAYLOAD)
    root = ET.fromstring(data)
    # localname (strip namespace)
    tag = root.tag
    local = tag.split("}")[-1] if "}" in tag else tag
    assert local == "nzb"


def test_build_nzb_has_correct_namespace():
    """Root element uses the NZB 1.1 namespace."""
    data = build_nzb(PAYLOAD)
    root = ET.fromstring(data)
    assert root.tag == f"{{{NZB_NS}}}nzb"


def test_build_nzb_has_dummy_file_element():
    """Produced NZB contains at least one <file> element so generic parsers don't choke."""
    data = build_nzb(PAYLOAD)
    root = ET.fromstring(data)
    files = root.findall(f"{{{NZB_NS}}}file")
    assert len(files) >= 1


def test_parse_nzb_raises_value_error_on_missing_meta():
    """parse_nzb on bytes lacking the x-slskd-payload meta raises ValueError."""
    # Valid NZB XML but no payload meta
    bare = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
        b"<head></head>"
        b'<file poster="" date="0" subject="">'
        b'<groups><group>alt.binaries.test</group></groups>'
        b"<segments></segments>"
        b"</file>"
        b"</nzb>"
    )
    with pytest.raises(ValueError):
        parse_nzb(bare)
