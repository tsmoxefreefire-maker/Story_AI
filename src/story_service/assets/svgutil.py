"""Small lxml helpers shared by the asset builders and the composer: create SVG elements in the default namespace and
serialize them without namespace declarations (the bundle inlines them into HTML, where the <svg> parent sets it)."""

from __future__ import annotations

import re

from lxml import etree

SVG_NS = "http://www.w3.org/2000/svg"
NS = f"{{{SVG_NS}}}"
NSMAP = {None: SVG_NS}
_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    remove_comments=True,
    remove_pis=True,
    remove_blank_text=True,
)


def E(tag: str, attrs: dict | None = None, *children, text: str | None = None) -> etree._Element:
    el = etree.Element(NS + tag, nsmap=NSMAP)
    for k, v in (attrs or {}).items():
        if v is not None:
            el.set(k, _fmt(v))
    for c in children:
        if c is not None:
            el.append(c)
    if text is not None:
        el.text = text
    return el


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return str(v)


def to_str(el: etree._Element) -> str:
    s = etree.tostring(el, encoding="unicode")
    return re.sub(r'\s+xmlns(:\w+)?="[^"]*"', "", s)


def parse(markup: str) -> etree._Element:
    """Parse a fragment or a document. A fragment is wrapped in an <svg> root that the caller can ignore."""
    text = markup.strip()
    if not text.startswith("<svg"):
        text = f'<svg xmlns="{SVG_NS}">{text}</svg>'
    elif "xmlns=" not in text.split(">", 1)[0]:
        text = text.replace("<svg", f'<svg xmlns="{SVG_NS}"', 1)
    return etree.fromstring(text.encode("utf-8"), _PARSER)


def load(path) -> etree._Element:
    return etree.parse(str(path), _PARSER).getroot()


def local(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""
