"""Newznab XML builders: caps, results RSS, and error responses.

All functions return UTF-8 encoded bytes ready to send as HTTP responses.
Uses stdlib only: xml.etree.ElementTree, email.utils, io.
"""

import email.utils
import io
import xml.etree.ElementTree as ET

NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"

ET.register_namespace("newznab", NEWZNAB_NS)


def _to_bytes(tree: ET.ElementTree) -> bytes:
    buf = io.BytesIO()
    tree.write(buf, xml_declaration=True, encoding="UTF-8")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# build_caps
# ---------------------------------------------------------------------------

def build_caps(categories: list[tuple[int, str]]) -> bytes:
    """Build a Newznab <caps> XML response.

    Advertises:
    - <search available="yes" supportedParams="q"/>
    - <audio-search available="yes" supportedParams="q,artist,album"/>
    - <limits max="100" default="100"/>
    - <categories> with one <category> per entry in *categories*.

    Args:
        categories: list of (id, name) tuples.

    Returns:
        UTF-8 encoded XML bytes.
    """
    root = ET.Element("caps")

    limits = ET.SubElement(root, "limits")
    limits.set("max", "100")
    limits.set("default", "100")

    searching = ET.SubElement(root, "searching")

    search = ET.SubElement(searching, "search")
    search.set("available", "yes")
    search.set("supportedParams", "q")

    tv_search = ET.SubElement(searching, "tv-search")
    tv_search.set("available", "no")
    tv_search.set("supportedParams", "q")

    audio_search = ET.SubElement(searching, "audio-search")
    audio_search.set("available", "yes")
    audio_search.set("supportedParams", "q,artist,album")

    cats_el = ET.SubElement(root, "categories")
    for cat_id, name in categories:
        cat = ET.SubElement(cats_el, "category")
        cat.set("id", str(cat_id))
        cat.set("name", name)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    return _to_bytes(tree)


# ---------------------------------------------------------------------------
# build_results_rss
# ---------------------------------------------------------------------------

def build_results_rss(
    items: list[dict],
    channel_title: str = "slskd-bridge",
) -> bytes:
    """Build a Newznab results RSS 2.0 feed.

    Each item dict must have keys:
        title (str), guid (str), link (str), pubDate (datetime),
        size (int), category (int).

    Renders per item:
        <title>, <guid>, <pubDate> (RFC-822), <enclosure>, and
        <newznab:attr name="size"> + <newznab:attr name="category">.

    Args:
        items: list of item dicts.
        channel_title: value of <channel><title>.

    Returns:
        UTF-8 encoded XML bytes.
    """
    root = ET.Element("rss")
    root.set("version", "2.0")

    channel = ET.SubElement(root, "channel")

    title_el = ET.SubElement(channel, "title")
    title_el.text = channel_title

    for item in items:
        item_el = ET.SubElement(channel, "item")

        t = ET.SubElement(item_el, "title")
        t.text = item["title"]

        guid_el = ET.SubElement(item_el, "guid")
        guid_el.text = item["guid"]

        pub = ET.SubElement(item_el, "pubDate")
        pub.text = email.utils.format_datetime(item["pubDate"])

        enc = ET.SubElement(item_el, "enclosure")
        enc.set("url", item["link"])
        enc.set("length", str(item["size"]))
        enc.set("type", "application/x-nzb")

        size_attr = ET.SubElement(item_el, f"{{{NEWZNAB_NS}}}attr")
        size_attr.set("name", "size")
        size_attr.set("value", str(item["size"]))

        cat_attr = ET.SubElement(item_el, f"{{{NEWZNAB_NS}}}attr")
        cat_attr.set("name", "category")
        cat_attr.set("value", str(item["category"]))

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    return _to_bytes(tree)


# ---------------------------------------------------------------------------
# build_error
# ---------------------------------------------------------------------------

def build_error(code: int, description: str) -> bytes:
    """Build a Newznab <error> XML response.

    Args:
        code: numeric error code.
        description: human-readable description.

    Returns:
        UTF-8 encoded XML bytes.
    """
    root = ET.Element("error")
    root.set("code", str(code))
    root.set("description", description)

    tree = ET.ElementTree(root)
    return _to_bytes(tree)
