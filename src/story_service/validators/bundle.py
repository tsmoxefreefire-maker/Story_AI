"""Bundle checks (Section 8.6, run by S10). Every failed check is a blocker.

* index.html at most 1.5 MB, audio at most 8 MB in total;
* the HTML parses, all element ids are unique;
* no http:, https: or // URLs anywhere: only data: URIs and relative audio/ paths;
* every referenced audio file exists; every cue target and interaction element id exists in the DOM;
* story-data validates against its JSON schema; the font is embedded;
* exactly two <script> elements (the JSON data and the player) and no on* attributes."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

import jsonschema

from ..pipeline.models import ValidationReport

MAX_HTML, MAX_AUDIO = 1_500_000, 8_000_000
_DATA_URI = re.compile(r"data:[\w/+.-]+(;[\w=-]+)*(;base64)?,[A-Za-z0-9+/=%._-]*")
_EXTERNAL = re.compile(r"(?<![\w-])(https?:)?//", re.I)

_WORD = {
    "type": "object",
    "required": ["w", "s", "e"],
    "properties": {"w": {"type": "string"}, "s": {"type": "integer"}, "e": {"type": "integer"}},
}
STORY_DATA_SCHEMA = {
    "type": "object",
    "required": ["schema_version", "meta", "characters", "scenes"],
    "properties": {
        "schema_version": {"type": "string"},
        "meta": {
            "type": "object",
            "required": ["title", "language", "dir", "duration_ms"],
            "properties": {
                "title": {"type": "string"},
                "language": {"enum": ["ar", "en"]},
                "dir": {"enum": ["rtl", "ltr"]},
                "duration_ms": {"type": "integer", "minimum": 1},
            },
        },
        "characters": {"type": "object", "additionalProperties": {"type": "object", "required": ["name"]}},
        "scenes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "start_ms", "end_ms", "svg_id", "lines", "cues", "interaction"],
                "properties": {
                    "id": {"type": "string"},
                    "start_ms": {"type": "integer"},
                    "end_ms": {"type": "integer"},
                    "svg_id": {"type": "string"},
                    "lines": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["id", "speaker", "text", "audio", "start_ms", "end_ms", "words"],
                            "properties": {
                                "audio": {"type": "string", "pattern": "^audio/[A-Za-z0-9_\\-]+\\.mp3$"},
                                "words": {"type": "array", "items": _WORD},
                            },
                        },
                    },
                    "cues": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["id", "t", "dur", "target", "action", "params"],
                            "properties": {
                                "t": {"type": "integer"},
                                "dur": {"type": "integer", "minimum": 0},
                                "action": {
                                    "enum": [
                                        "show",
                                        "hide",
                                        "fade",
                                        "move",
                                        "scale",
                                        "rotate",
                                        "keyframes",
                                        "highlight",
                                        "shake",
                                        "camera",
                                        "enter",
                                        "exit",
                                        "pose",
                                        "expression",
                                        "face",
                                    ]
                                },
                            },
                        },
                    },
                    "interaction": {
                        "anyOf": [
                            {"type": "null"},
                            {
                                "type": "object",
                                "required": ["id", "type", "at_ms", "prompt"],
                                "properties": {
                                    "type": {"enum": ["tap_reveal", "choose_answer", "drag_match", "predict"]},
                                    "at_ms": {"type": "integer"},
                                },
                            },
                        ]
                    },
                },
            },
        },
    },
}


FORBIDDEN_TAGS = {
    "iframe",
    "frame",
    "frameset",
    "embed",
    "object",
    "applet",
    "base",
    "link",
    "form",
    "a",
    "img",
    "video",
    "audio",
    "source",
    "portal",
    "foreignobject",
    "image",
}
_URL_SCHEME = re.compile(r"^\s*(javascript|vbscript|data|https?|file|ftp)\s*:", re.I)


class _Scan(HTMLParser):
    """One pass over the page: ids, script count, event attributes, forbidden tags, URL-like attribute values."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids, self.scripts, self.on_attrs, self.bad_tags, self.bad_values = [], 0, [], [], []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.scripts += 1
        if tag in FORBIDDEN_TAGS:
            self.bad_tags.append(tag)
        for k, v in attrs:
            k = k.lower()
            if k == "id" and v:
                self.ids.append(v)
            if k.startswith("on"):
                self.on_attrs.append(f"<{tag} {k}>")
            if k in ("srcdoc", "src", "action", "formaction", "http-equiv") or (v and _URL_SCHEME.match(v)):
                self.bad_values.append(f"<{tag} {k}={str(v)[:40]!r}>")

    handle_startendtag = handle_starttag


def html_parse_errors(html: str) -> list[str] | None:
    """html5lib's parse errors (Section 8.6: "HTML parses with html5lib without errors"); None if not installed."""
    try:
        import html5lib
    except ImportError:
        return None
    parser = html5lib.HTMLParser(namespaceHTMLElements=False)
    parser.parse(html)
    return [f"line {pos[0]} col {pos[1]}: {code}" for pos, code, _ in parser.errors]


def collect_ids(html: str) -> list[str]:
    """All element ids (html5lib when installed, as the spec's test uses; the standard parser otherwise)."""
    try:
        import html5lib

        doc = html5lib.parse(html, namespaceHTMLElements=False)
        return [e.get("id") for e in doc.iter() if e.get("id")]
    except ImportError:
        s = _Scan()
        s.feed(html)
        return s.ids


def strip_data_uris(html: str) -> str:
    return _DATA_URI.sub("data:", html)


def extract_story_data(html: str) -> str:
    m = re.search(r'<script type="application/json" id="story-data">(.*?)</script>', html, re.S)
    return m.group(1) if m else ""


def check_bundle(html: str, audio_files: dict[str, bytes]) -> ValidationReport:
    rep = ValidationReport(stage="S10")
    size = len(html.encode("utf-8"))
    rep.add(
        "html_size",
        size <= MAX_HTML,
        "blocker",
        f"index.html is {size:,} bytes (max {MAX_HTML:,})",
        code="BUNDLE_TOO_BIG",
        suspect_stage="S10_assembler",
    )
    audio_total = sum(len(b) for b in audio_files.values())
    rep.add(
        "audio_size",
        audio_total <= MAX_AUDIO,
        "blocker",
        f"audio is {audio_total:,} bytes (max {MAX_AUDIO:,})",
        code="AUDIO_TOO_BIG",
        suspect_stage="S6_audio",
    )
    scan = _Scan()
    scan.feed(html)
    errors = html_parse_errors(html)
    if errors is None:
        rep.add(
            "parses",
            False,
            "minor",
            "html5lib is not installed: the strict parse check did not run",
            code="HTML_PARSE_UNCHECKED",
        )
    else:
        rep.add(
            "parses",
            not errors,
            "blocker",
            f"HTML parse errors: {errors[:5]}",
            code="HTML_PARSE",
            suspect_stage="S10_assembler",
        )
    rep.add(
        "no_forbidden_tags",
        not scan.bad_tags,
        "blocker",
        f"forbidden elements: {sorted(set(scan.bad_tags))}",
        code="FORBIDDEN_TAG",
        suspect_stage="S10_assembler",
    )
    rep.add(
        "no_url_attributes",
        not scan.bad_values,
        "blocker",
        f"URL-like attributes: {scan.bad_values[:4]}",
        code="EXTERNAL_URL",
        suspect_stage="S10_assembler",
    )
    ids = collect_ids(html)
    dup = sorted({i for i in ids if ids.count(i) > 1})
    rep.add(
        "ids_unique",
        not dup,
        "blocker",
        f"duplicate ids: {dup[:6]}",
        code="DUPLICATE_ID",
        suspect_stage="S10_assembler",
    )
    ext = _EXTERNAL.search(strip_data_uris(html))
    rep.add(
        "no_external_urls",
        ext is None,
        "blocker",
        f"external URL near: {strip_data_uris(html)[max(0, ext.start() - 40) : ext.end() + 40]!r}" if ext else "",
        code="EXTERNAL_URL",
        suspect_stage="S10_assembler",
    )
    rep.add(
        "two_scripts",
        scan.scripts == 2,
        "blocker",
        f"{scan.scripts} <script> elements (exactly 2 allowed)",
        code="SCRIPT_COUNT",
        suspect_stage="S10_assembler",
    )
    rep.add(
        "no_event_attributes",
        not scan.on_attrs,
        "blocker",
        f"event attributes: {scan.on_attrs[:4]}",
        code="EVENT_ATTRIBUTE",
        suspect_stage="S10_assembler",
    )
    rep.add(
        "font_embedded",
        "data:font/woff2" in html or "data:font/woff;" in html,
        "blocker",
        "no embedded font",
        code="FONT_MISSING",
        suspect_stage="S10_assembler",
    )
    raw = extract_story_data(html)
    try:
        data = json.loads(raw)
        jsonschema.validate(data, STORY_DATA_SCHEMA)
    except (ValueError, jsonschema.ValidationError) as e:
        rep.add("story_data_schema", False, "blocker", f"story-data invalid: {str(e)[:200]}", code="STORY_DATA")
        return rep
    rep.add("story_data_schema", True, "blocker")
    idset = set(ids)
    missing_audio = [ln["audio"] for s in data["scenes"] for ln in s["lines"] if ln["audio"] not in audio_files]
    rep.add(
        "audio_exists",
        not missing_audio,
        "blocker",
        f"missing audio: {missing_audio[:4]}",
        code="AUDIO_MISSING",
        suspect_stage="S6_audio",
    )
    missing_targets = [c["target"] for s in data["scenes"] for c in s["cues"] if c["target"] not in idset]
    rep.add(
        "cue_targets_exist",
        not missing_targets,
        "blocker",
        f"cue targets not in the page: {missing_targets[:4]}",
        code="CUE_TARGET_MISSING",
        suspect_stage="S7_director",
    )
    missing_bound = [
        b["element_id"]
        for s in data["scenes"]
        if s["interaction"]
        for b in s["interaction"].get("bindings", [])
        if b["element_id"] not in idset
    ]
    rep.add(
        "interaction_elements_exist",
        not missing_bound,
        "blocker",
        f"interaction elements missing: {missing_bound[:4]}",
        code="BINDING_MISSING",
        suspect_stage="S7_director",
    )
    missing_svg = [s["svg_id"] for s in data["scenes"] if s["svg_id"] not in idset]
    rep.add(
        "scene_svgs_exist",
        not missing_svg,
        "blocker",
        f"scene svg missing: {missing_svg}",
        code="SCENE_MISSING",
        suspect_stage="S10_assembler",
    )
    return rep
