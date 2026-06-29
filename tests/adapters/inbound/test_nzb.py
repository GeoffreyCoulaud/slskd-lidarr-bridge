import base64
import json
import xml.etree.ElementTree as ET

import pytest

from slskd_lidarr_bridge.adapters.inbound.nzb import build_nzb, parse_nzb

NZB_NS = "http://www.newzbin.com/DTD/2003/nzb"

PAYLOAD = {
    "username": "some_user",
    "title": "Björk – Homogenic (1997) [FLAC]",
    "album_folder": "Homogenic (1997) [FLAC]",
    "total_size": 999_999_999_999,
    "files": [
        {
            "filename": r"@@some_user\Music\Björk\Homogenic\01.flac",
            "size": 499_999_999_999,
        },
        {
            "filename": r"@@some_user\Music\Björk\Homogenic\02.flac",
            "size": 500_000_000_000,
        },
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
    """Produced NZB has at least one <file> element so generic parsers don't choke."""
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
        b"<groups><group>alt.binaries.test</group></groups>"
        b"<segments></segments>"
        b"</file>"
        b"</nzb>"
    )
    with pytest.raises(ValueError):
        parse_nzb(bare)


def test_parse_nzb_raises_when_payload_meta_is_empty():
    """A payload meta element that exists but carries no text raises a distinct
    ValueError (the 'empty' branch, not the 'not found' branch)."""
    root = ET.Element(f"{{{NZB_NS}}}nzb")
    head = ET.SubElement(root, f"{{{NZB_NS}}}head")
    meta = ET.SubElement(head, f"{{{NZB_NS}}}meta")
    meta.set("type", "x-slskd-payload")
    # Intentionally leave meta.text as None → empty element.
    data = ET.tostring(root, encoding="UTF-8")

    with pytest.raises(ValueError, match="empty"):
        parse_nzb(data)


def test_parse_nzb_skips_foreign_meta_and_finds_payload():
    """A <meta> of another type (e.g. type='title') is skipped so the
    x-slskd-payload meta is still found further down the document."""
    encoded = base64.b64encode(
        json.dumps(PAYLOAD, ensure_ascii=False).encode()
    ).decode()

    root = ET.Element(f"{{{NZB_NS}}}nzb")
    head = ET.SubElement(root, f"{{{NZB_NS}}}head")
    foreign = ET.SubElement(head, f"{{{NZB_NS}}}meta")
    foreign.set("type", "title")
    foreign.text = "Some Title"
    real = ET.SubElement(head, f"{{{NZB_NS}}}meta")
    real.set("type", "x-slskd-payload")
    real.text = encoded
    data = ET.tostring(root, encoding="UTF-8")

    assert parse_nzb(data) == PAYLOAD
