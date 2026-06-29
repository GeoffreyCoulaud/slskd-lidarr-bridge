"""Tests for newznab XML builders: build_caps, build_results_rss, build_error."""

import email.utils
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest

from slskd_lidarr_bridge.web.xml import build_caps, build_error, build_results_rss

NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"


# ---------------------------------------------------------------------------
# build_caps
# ---------------------------------------------------------------------------

CATEGORIES = [(3000, "Audio"), (3010, "Audio/MP3"), (3040, "Audio/Lossless")]


def test_caps_root_tag():
    data = build_caps(CATEGORIES)
    root = ET.fromstring(data)
    local = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    assert local == "caps"


def test_caps_searching_element_exists():
    data = build_caps(CATEGORIES)
    root = ET.fromstring(data)
    assert root.find("searching") is not None


def test_caps_search_not_direct_child():
    """Guards against regression to flat (un-nested) structure."""
    data = build_caps(CATEGORIES)
    root = ET.fromstring(data)
    assert root.find("search") is None


def test_caps_audio_search_available():
    data = build_caps(CATEGORIES)
    root = ET.fromstring(data)
    audio_search = root.find("searching/audio-search")
    assert audio_search is not None
    assert audio_search.get("available") == "yes"


def test_caps_audio_search_supported_params_contains_required():
    data = build_caps(CATEGORIES)
    root = ET.fromstring(data)
    audio_search = root.find("searching/audio-search")
    params = audio_search.get("supportedParams", "")
    param_list = [p.strip() for p in params.split(",")]
    for required in ("q", "artist", "album"):
        assert required in param_list, f"supportedParams missing '{required}'"


def test_caps_search_available():
    data = build_caps(CATEGORIES)
    root = ET.fromstring(data)
    search = root.find("searching/search")
    assert search is not None
    assert search.get("available") == "yes"


def test_caps_search_supported_params_contains_q():
    data = build_caps(CATEGORIES)
    root = ET.fromstring(data)
    search = root.find("searching/search")
    params = search.get("supportedParams", "")
    assert "q" in [p.strip() for p in params.split(",")]


def test_caps_tv_search_not_available():
    data = build_caps(CATEGORIES)
    root = ET.fromstring(data)
    tv_search = root.find("searching/tv-search")
    assert tv_search is not None
    assert tv_search.get("available") == "no"


def test_caps_limits():
    data = build_caps(CATEGORIES)
    root = ET.fromstring(data)
    limits = root.find("limits")
    assert limits is not None
    assert limits.get("max") == "100"
    assert limits.get("default") == "100"


def test_caps_all_categories_present():
    data = build_caps(CATEGORIES)
    root = ET.fromstring(data)
    cats_el = root.find("categories")
    assert cats_el is not None
    found = {
        (int(c.get("id")), c.get("name"))
        for c in cats_el.findall("category")
    }
    expected = {(cat_id, name) for cat_id, name in CATEGORIES}
    assert expected == found


# ---------------------------------------------------------------------------
# build_results_rss
# ---------------------------------------------------------------------------

PUB_DATE = datetime(2024, 3, 15, 12, 0, 0, tzinfo=timezone.utc)

ITEM = {
    "title": "Björk – Homogenic (1997) [FLAC]",
    "guid": "release-abc-123",
    "link": "http://localhost:8080/api/nzb/release-abc-123",
    "pubDate": PUB_DATE,
    "size": 999_999_999,
    "category": 3040,
}


def _parse_rss(data: bytes):
    ET.register_namespace("newznab", NEWZNAB_NS)
    return ET.fromstring(data)


def test_rss_root_is_rss():
    root = _parse_rss(build_results_rss([ITEM]))
    local = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    assert local == "rss"


def test_rss_channel_title():
    root = _parse_rss(build_results_rss([ITEM], channel_title="my-bridge"))
    channel = root.find("channel")
    assert channel is not None
    title_el = channel.find("title")
    assert title_el is not None and title_el.text == "my-bridge"


def test_rss_default_channel_title():
    root = _parse_rss(build_results_rss([ITEM]))
    title_el = root.find("channel/title")
    assert title_el is not None and title_el.text == "slskd-bridge"


def test_rss_item_title():
    root = _parse_rss(build_results_rss([ITEM]))
    title_el = root.find("channel/item/title")
    assert title_el is not None and title_el.text == ITEM["title"]


def test_rss_item_guid():
    root = _parse_rss(build_results_rss([ITEM]))
    guid_el = root.find("channel/item/guid")
    assert guid_el is not None and guid_el.text == ITEM["guid"]


def test_rss_item_enclosure_type():
    root = _parse_rss(build_results_rss([ITEM]))
    enc = root.find("channel/item/enclosure")
    assert enc is not None
    assert enc.get("type") == "application/x-nzb"


def test_rss_item_enclosure_url_equals_link():
    root = _parse_rss(build_results_rss([ITEM]))
    enc = root.find("channel/item/enclosure")
    assert enc is not None
    assert enc.get("url") == ITEM["link"]


def test_rss_item_enclosure_length_equals_size():
    root = _parse_rss(build_results_rss([ITEM]))
    enc = root.find("channel/item/enclosure")
    assert enc is not None
    assert int(enc.get("length")) == ITEM["size"]


def test_rss_item_pubdate_is_rfc822_parseable():
    root = _parse_rss(build_results_rss([ITEM]))
    pub = root.find("channel/item/pubDate")
    assert pub is not None
    # email.utils.parsedate_to_datetime raises ValueError if not RFC-822
    parsed = email.utils.parsedate_to_datetime(pub.text)
    assert parsed is not None
    # RFC-822 round-trip: parsed datetime must equal the original PUB_DATE
    assert parsed == PUB_DATE


def test_rss_item_pubdate_naive_datetime_treated_as_utc():
    """Naive datetimes (tzinfo=None) must not raise; output is valid RFC-822."""
    naive_date = datetime(2024, 3, 15, 12, 0, 0)  # no tzinfo
    naive_item = {**ITEM, "pubDate": naive_date}
    root = _parse_rss(build_results_rss([naive_item]))
    pub = root.find("channel/item/pubDate")
    assert pub is not None
    # Must parse as RFC-822 without raising
    parsed = email.utils.parsedate_to_datetime(pub.text)
    assert parsed is not None
    # Should be treated as UTC
    assert parsed.tzinfo is not None


def test_rss_item_newznab_size_attr():
    root = _parse_rss(build_results_rss([ITEM]))
    item_el = root.find("channel/item")
    assert item_el is not None
    size_attr = item_el.find(f"{{{NEWZNAB_NS}}}attr[@name='size']")
    assert size_attr is not None
    assert int(size_attr.get("value")) == ITEM["size"]


def test_rss_item_newznab_category_attr():
    root = _parse_rss(build_results_rss([ITEM]))
    item_el = root.find("channel/item")
    assert item_el is not None
    cat_attr = item_el.find(f"{{{NEWZNAB_NS}}}attr[@name='category']")
    assert cat_attr is not None
    assert int(cat_attr.get("value")) == ITEM["category"]


def test_rss_multiple_items():
    item2 = {**ITEM, "guid": "release-xyz-456", "title": "Other Album"}
    root = _parse_rss(build_results_rss([ITEM, item2]))
    items = root.findall("channel/item")
    assert len(items) == 2


def test_rss_empty_items():
    root = _parse_rss(build_results_rss([]))
    items = root.findall("channel/item")
    assert len(items) == 0


# ---------------------------------------------------------------------------
# build_error
# ---------------------------------------------------------------------------


def test_error_root_tag():
    data = build_error(100, "Incorrect user credentials")
    root = ET.fromstring(data)
    local = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    assert local == "error"


def test_error_code_attribute():
    data = build_error(100, "Incorrect user credentials")
    root = ET.fromstring(data)
    assert root.get("code") == "100"


def test_error_description_attribute():
    data = build_error(100, "Incorrect user credentials")
    root = ET.fromstring(data)
    assert root.get("description") == "Incorrect user credentials"


def test_error_different_code_and_description():
    data = build_error(300, "No such function")
    root = ET.fromstring(data)
    assert root.get("code") == "300"
    assert root.get("description") == "No such function"
