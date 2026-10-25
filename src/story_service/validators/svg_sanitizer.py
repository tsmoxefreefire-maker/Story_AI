"""SVG sanitizer (Section 8.2). Everything that enters a bundle passes here: library assets, composed scenes and
custom props written by the LLM. Any violation is a blocker.

`sanitize(svg, kind)` parses with a locked-down XML parser (no DTD, no entities, no network), checks every tag,
attribute and style declaration against the whitelist and the size limits, and returns the violations plus a clean
re-serialization (with the offending parts removed). Callers decide: a library asset or a custom prop with any
violation is rejected; the clean copy exists so a report can say exactly what was stripped."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

SVG_NS = "http://www.w3.org/2000/svg"
ALLOWED_TAGS = {
    "svg",
    "g",
    "defs",
    "path",
    "circle",
    "ellipse",
    "rect",
    "line",
    "polyline",
    "polygon",
    "text",
    "tspan",
    "linearGradient",
    "radialGradient",
    "stop",
    "clipPath",
    "use",
    "title",
    "desc",
}
ALLOWED_ATTRS = {
    "id",
    "class",
    "d",
    "cx",
    "cy",
    "r",
    "rx",
    "ry",
    "x",
    "y",
    "x1",
    "y1",
    "x2",
    "y2",
    "points",
    "width",
    "height",
    "fill",
    "stroke",
    "stroke-width",
    "stroke-linecap",
    "stroke-linejoin",
    "opacity",
    "fill-opacity",
    "stroke-opacity",
    "transform",
    "viewBox",
    "preserveAspectRatio",
    "offset",
    "stop-color",
    "stop-opacity",
    "gradientUnits",
    "gradientTransform",
    "clip-path",
    "clip-rule",
    "fill-rule",
    "text-anchor",
    "font-size",
    "font-weight",
    "font-family",
    "direction",
    "href",
    "style",
    "xmlns",
}
STYLE_PROPS = {
    "fill",
    "stroke",
    "stroke-width",
    "opacity",
    "transform",
    "transform-origin",
    "transform-box",
    "font-size",
    "font-weight",
    "text-anchor",
}
LIMITS = {"scene": 600, "custom_prop": 40, "character": 140, "prop": 80, "background": 400, "widget": 200}
MAX_DEPTH, MAX_D = 12, 4000

_DATA_ATTR = re.compile(r"^data-[a-z0-9][a-z0-9\-]*$")
_LOCAL_REF = re.compile(r"^#[A-Za-z_][\w\-.]*$")
_URL = re.compile(r"url\(\s*([^)]*)\)", re.I)
_BAD_VALUE = ("javascript:", "data:", "http:", "https:", "vbscript:")
_HEX = re.compile(r"#[0-9a-fA-F]{3,6}\b")
_VAR = re.compile(r"var\(\s*--([A-Za-z0-9\-]+)\s*\)")
_COLOR_ATTRS = ("fill", "stroke", "stop-color")


@dataclass
class Violation:
    rule: str
    detail: str


@dataclass
class SanitizeResult:
    violations: list[Violation] = field(default_factory=list)
    elements: int = 0
    depth: int = 0
    clean: str = ""

    @property
    def ok(self) -> bool:
        return not self.violations


def _squash(value: str) -> str:
    """Lowercase and drop whitespace and control characters, so "java\\nscript:" is still caught."""
    return re.sub(r"[\s\x00-\x1f]+", "", value).lower()


def _bad_value(value: str) -> str | None:
    v = _squash(value)
    for bad in _BAD_VALUE:
        if bad in v:
            return bad
    if v.startswith("//"):
        return "//"
    return None


def _check_urls(value: str) -> str | None:
    for m in _URL.finditer(value):
        if not _LOCAL_REF.match(m.group(1).strip().strip("'\"")):
            return m.group(0)
    return None


def _check_style(style: str) -> list[str]:
    problems = []
    low = _squash(style)
    for bad in ("expression", "@import", "javascript:", "\\", "behavior:", "-moz-binding"):
        if bad in low:
            problems.append(f"forbidden {bad!r} in style")
    for decl in filter(None, (d.strip() for d in style.split(";"))):
        if ":" not in decl:
            problems.append(f"bad declaration {decl!r}")
            continue
        prop, value = (x.strip() for x in decl.split(":", 1))
        if not (prop.lower() in STYLE_PROPS or prop.startswith("--")):
            problems.append(f"style property {prop!r} not allowed")
        if (u := _check_urls(value)) is not None:
            problems.append(f"style url {u!r} is not a local #id")
        if (b := _bad_value(value)) is not None:
            problems.append(f"style value contains {b!r}")
    return problems


def _parse(svg: str | bytes) -> etree._Element:
    text = svg.decode("utf-8") if isinstance(svg, (bytes, bytearray)) else svg
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
        raise ValueError("DOCTYPE and entities are not allowed")
    if not re.match(r"\s*<svg\b", text):
        text = f'<svg xmlns="{SVG_NS}" data-wrapper="1">{text}</svg>'
    elif "xmlns=" not in text.split(">", 1)[0]:
        text = re.sub(r"<svg\b", f'<svg xmlns="{SVG_NS}"', text, count=1)
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, remove_comments=True, remove_pis=False
    )
    return etree.fromstring(text.encode("utf-8"), parser)


def _local(tag) -> str | None:
    if not isinstance(tag, str):
        return None
    if tag.startswith("{"):
        ns, name = tag[1:].split("}", 1)
        return name if ns == SVG_NS else None
    return tag


def sanitize(svg: str | bytes, kind: str = "scene") -> SanitizeResult:
    res = SanitizeResult()
    raw = svg.decode("utf-8", "replace") if isinstance(svg, (bytes, bytearray)) else svg
    if "<![CDATA[" in raw:
        res.violations.append(Violation("cdata", "CDATA sections are not allowed"))
    try:
        root = _parse(svg)
    except (etree.XMLSyntaxError, ValueError) as e:
        res.violations.append(Violation("parse", str(e)[:200]))
        return res
    wrapped = root.get("data-wrapper") == "1"

    def walk(el, depth: int):
        name = _local(el.tag)
        if name is None or name not in ALLOWED_TAGS:
            res.violations.append(Violation("tag", f"<{el.tag}> is not allowed"))
            el.getparent().remove(el) if el.getparent() is not None else None
            return
        if not (wrapped and el is root):
            res.elements += 1
        res.depth = max(res.depth, depth)
        for attr in list(el.attrib):
            value = el.attrib[attr]
            if attr.startswith("{"):  # xlink:href, xml:space…
                res.violations.append(Violation("attribute", f"namespaced attribute {attr!r} on <{name}>"))
                del el.attrib[attr]
                continue
            if attr.lower().startswith("on"):
                res.violations.append(Violation("attribute", f"event handler {attr!r} on <{name}>"))
                del el.attrib[attr]
                continue
            if attr not in ALLOWED_ATTRS and not _DATA_ATTR.match(attr):
                res.violations.append(Violation("attribute", f"{attr!r} not allowed on <{name}>"))
                del el.attrib[attr]
                continue
            if attr == "href":
                if name != "use" or not _LOCAL_REF.match(value.strip()):
                    res.violations.append(
                        Violation("href", f"href {value[:60]!r} on <{name}> must be an internal #id on <use>")
                    )
                    del el.attrib[attr]
                continue
            if attr == "style":
                problems = _check_style(value)
                if problems:
                    res.violations.extend(Violation("style", p) for p in problems)
                    del el.attrib[attr]
                continue
            if (b := _bad_value(value)) is not None or ("\\" in value and (b := "\\")):
                res.violations.append(Violation("value", f"{attr}={value[:60]!r} contains {b!r}"))
                del el.attrib[attr]
                continue
            if (u := _check_urls(value)) is not None:
                res.violations.append(Violation("value", f"{attr} url {u!r} is not a local #id"))
                del el.attrib[attr]
                continue
            if attr == "d" and len(value) > MAX_D:
                res.violations.append(Violation("limit", f"d attribute of {len(value)} characters (max {MAX_D})"))
        for child in list(el):
            if isinstance(child, etree._ProcessingInstruction):
                res.violations.append(Violation("pi", f"processing instruction <?{child.target} …?> is not allowed"))
                el.remove(child)
                continue
            if not isinstance(child.tag, str):  # comments (already dropped by the parser) and entities
                el.remove(child)
                continue
            walk(child, depth + 1)

    walk(root, 0 if wrapped else 1)
    limit = LIMITS.get(kind, LIMITS["scene"])
    if res.elements > limit:
        res.violations.append(Violation("limit", f"{res.elements} elements (max {limit} for a {kind})"))
    if res.depth > MAX_DEPTH:
        res.violations.append(Violation("limit", f"nesting depth {res.depth} (max {MAX_DEPTH})"))
    if wrapped:
        res.clean = "".join(etree.tostring(c, encoding="unicode") for c in root)
    else:
        res.clean = etree.tostring(root, encoding="unicode")
    res.clean = res.clean.replace(f' xmlns="{SVG_NS}"', "")
    return res


_NAMED = re.compile(
    r"^(red|green|blue|black|white|gray|grey|yellow|orange|purple|pink|brown|cyan|magenta|lime|navy|teal|maroon|olive|"
    r"silver|gold|violet|indigo)$",
    re.IGNORECASE,
)


def _color_values(el) -> tuple[list[str], list[str]]:
    """(paint values, custom property values) of an element. Paint values (fill, stroke, stop-color, also in style)
    must be tokens; a custom property may hold anything (4s, 0.5) except a raw color."""
    paint = [el.get(a) for a in _COLOR_ATTRS if el.get(a)]
    custom = []
    for decl in (el.get("style") or "").split(";"):
        if ":" in decl:
            prop, value = (x.strip() for x in decl.split(":", 1))
            if prop in _COLOR_ATTRS:
                paint.append(value)
            elif prop.startswith("--"):
                custom.append(value)
    return paint, custom


def _is_raw_color(v: str) -> bool:
    return bool(_NAMED.match(v) or _HEX.search(v) or re.search(r"\b(rgba?|hsla?)\(", v))


def raw_colors(svg: str) -> list[str]:
    """Raw (non-token) colors in attribute and style values: hex, rgb()/hsl(), named colors. Text content is ignored."""
    found = []
    for el in _parse(svg).iter():
        if not isinstance(el.tag, str):
            continue
        for attr, value in el.attrib.items():
            if attr == "href":
                continue
            scrubbed = re.sub(r"url\(\s*#[^)]*\)", "", value)  # url(#bed) is a reference, not the color #bed
            if m := _HEX.search(scrubbed):
                found.append(f"raw hex color {m.group(0)} in {attr}")
            if re.search(r"\b(rgba?|hsla?)\(", scrubbed):
                found.append(f"rgb()/hsl() color in {attr}")
        paint, custom = _color_values(el)
        found += [f"named color {v.strip()!r}" for v in paint + custom if _NAMED.match(v.strip())]
    return sorted(set(found))


def lint_colors(svg: str, known_vars: set[str] | None = None) -> list[str]:
    """The token-only color rule for asset files: no hex, no rgb()/hsl(), no named colors; every var() is a token."""
    try:
        problems = [m for m in raw_colors(svg) if not m.startswith("named color")]  # named ones are reported below
        root = _parse(svg)
    except (etree.XMLSyntaxError, ValueError) as e:
        return [f"does not parse: {str(e)[:120]}"]
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        paint, custom = _color_values(el)
        for v in (x.strip() for x in paint):
            if v in ("none", "transparent", "currentColor") or v.startswith("url(#") or _VAR.fullmatch(v):
                continue
            problems.append(f"color {v!r} is not a token")
        problems += [f"color {v.strip()!r} is not a token" for v in custom if _is_raw_color(v.strip())]
        if known_vars is not None:
            for v in el.attrib.values():
                for name in _VAR.findall(v):
                    if name not in known_vars:
                        problems.append(f"unknown token --{name}")
    return sorted(set(problems))
