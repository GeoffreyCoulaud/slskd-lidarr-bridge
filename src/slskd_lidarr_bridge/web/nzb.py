"""Self-describing NZB carrier: build and parse NZB 1.1 XML documents
that embed a base64-encoded JSON payload in the <head><meta> element.
"""

import base64
import io
import json
import xml.etree.ElementTree as ET

NZB_NS = "http://www.newzbin.com/DTD/2003/nzb"
META_TYPE = "x-slskd-payload"

ET.register_namespace("", NZB_NS)


def build_nzb(payload: dict) -> bytes:
    """Build a valid NZB 1.1 XML document carrying *payload* as base64-encoded JSON.

    The document structure:
        <nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
          <head>
            <meta type="x-slskd-payload">BASE64_JSON</meta>
          </head>
          <file ...>...</file>   <!-- dummy so generic NZB parsers don't choke -->
        </nzb>
    """
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()

    root = ET.Element(f"{{{NZB_NS}}}nzb")

    head = ET.SubElement(root, f"{{{NZB_NS}}}head")
    meta = ET.SubElement(head, f"{{{NZB_NS}}}meta")
    meta.set("type", META_TYPE)
    meta.text = encoded

    # Dummy <file> element so generic NZB parsers accept the document
    dummy_file = ET.SubElement(root, f"{{{NZB_NS}}}file")
    dummy_file.set("poster", "")
    dummy_file.set("date", "0")
    dummy_file.set("subject", payload.get("title", ""))
    groups = ET.SubElement(dummy_file, f"{{{NZB_NS}}}groups")
    group = ET.SubElement(groups, f"{{{NZB_NS}}}group")
    group.text = "alt.binaries.slskd"
    ET.SubElement(dummy_file, f"{{{NZB_NS}}}segments")

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    buf = io.BytesIO()
    tree.write(buf, xml_declaration=True, encoding="UTF-8")
    return buf.getvalue()


def parse_nzb(data: bytes) -> dict:
    """Parse a self-describing NZB document and return the embedded payload dict.

    Raises ValueError if the x-slskd-payload meta element is absent.
    """
    root = ET.fromstring(data)
    # Search for meta element regardless of document depth
    meta_tag = f"{{{NZB_NS}}}meta"
    for elem in root.iter(meta_tag):
        if elem.get("type") == META_TYPE:
            encoded = elem.text
            if encoded is None:
                raise ValueError("x-slskd-payload meta element is empty")
            return json.loads(base64.b64decode(encoded).decode())
    raise ValueError("No x-slskd-payload meta element found in NZB document")
