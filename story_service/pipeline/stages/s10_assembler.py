"""S10 Assembler (code) — Section 5, S10 and Section 7.

Builds ONE self-contained `index.html` from `runtime/template.html`:
  * inline CSS: design tokens, scene and character styles, player UI, and the Cairo font subset as a WOFF2 data URI;
  * every scene's SVG inline, each in `<svg class="scene" id="scene-sNN">`;
  * `<script type="application/json" id="story-data">` with the timeline;
  * the player script (with the UI strings of the story's language).
Copies the MP3 files to `audio/{line_id}.mp3` and writes `meta.json`.

One page holds every scene, and the same character (or background) appears in several scenes, so ids are made unique
here: every id inside scene sNN becomes `sNN-<id>` (`s03-nadia`, `s03-camera`), and the cue targets and interaction
bindings in story-data are rewritten the same way. Nothing from the story is ever inserted as raw HTML: text goes into
JSON (with `<` escaped) or is HTML-escaped; scenes are re-sanitized after the rewrite."""

from __future__ import annotations

import base64
import datetime as dt
import html as html_lib
import io
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ... import RUNTIME_VERSION, SCHEMA_VERSION
from ...assets.svgutil import parse, to_str
from ...assets.tokens import tokens_css
from ...validators.bundle import check_bundle
from ...validators.svg_sanitizer import sanitize
from ..models import BundleMeta, Sizes, Timeline, ValidationReport, Warning_

RUNTIME = Path(__file__).resolve().parents[2] / "runtime"
FONTS = Path(__file__).resolve().parents[2] / "assets" / "fonts"
# development stand-ins with Arabic glyphs, by family (Linux, Windows, macOS); the first family found is used
FALLBACK_FONTS = [
    [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ],
    [Path("C:/Windows/Fonts/tahoma.ttf"), Path("C:/Windows/Fonts/tahomabd.ttf")],
    [Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/arialbd.ttf")],
    [Path("/System/Library/Fonts/Supplemental/Arial.ttf"), Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")],
]
_URLREF = re.compile(r"url\(#([^)]+)\)")


@dataclass
class Bundle:
    index_html: bytes
    audio: dict[str, bytes]
    meta: BundleMeta
    report: ValidationReport
    warnings: list[Warning_] = field(default_factory=list)

    @property
    def audio_total(self) -> int:
        return sum(len(b) for b in self.audio.values())

    @property
    def audio_paths(self) -> set[str]:
        return set(self.audio)

    def files(self) -> dict[str, bytes]:
        """Everything that goes under `.../bundle/`: index.html, audio/*.mp3, meta.json."""
        out = {"index.html": self.index_html, "meta.json": self.meta.model_dump_json(indent=1).encode()}
        out.update(self.audio)
        return out


# ----------------------------------------------------------------------------------------------- scenes
def namespace_scene(scene_id: str, svg: str) -> str:
    """Prefix every id inside the scene with `{scene_id}-` and rewrite `url(#…)` / `href="#…"` to match. The S8 root
    group becomes the scene's own <svg class="scene scene-root tod-…" id="scene-{scene_id}"> (the svg_id of
    story-data): one wrapper instead of two keeps the deepest character part within the 12-level nesting limit."""
    root = parse(svg)
    g = root[0] if root.get("data-bg") is None and len(root) and root[0].get("id", "").startswith("scene-") else root
    pre = f"{scene_id}-"
    rename = {}
    for el in g.iter():
        if not isinstance(el.tag, str) or el is g:
            continue
        old = el.get("id")
        if old:
            rename[old] = pre + old
            el.set("id", pre + old)
    for el in g.iter():
        if not isinstance(el.tag, str):
            continue
        for k, v in list(el.attrib.items()):
            if "url(#" in v:
                el.set(k, _URLREF.sub(lambda m: f"url(#{rename.get(m.group(1), m.group(1))})", v))
            elif k == "href" and v.startswith("#"):
                el.set(k, "#" + rename.get(v[1:], v[1:]))
    inner = "".join(to_str(c) for c in g)
    cls = g.get("class", "scene-root")
    bg = g.get("data-bg", "")
    return (
        f'<svg class="scene {cls}" id="scene-{scene_id}" data-bg="{bg}" viewBox="0 0 1280 720" '
        f'preserveAspectRatio="xMidYMid meet">{inner}</svg>'
    )


def namespace_story_data(tl: Timeline) -> dict:
    data = json.loads(tl.model_dump_json())
    for s in data["scenes"]:
        pre = s["id"] + "-"
        for c in s["cues"]:
            c["target"] = pre + c["target"]
        if s.get("interaction"):
            for b in s["interaction"].get("bindings", []):
                b["element_id"] = pre + b["element_id"]
    return data


# ----------------------------------------------------------------------------------------------- font
def _font_files() -> tuple[list[Path], Warning_ | None]:
    cairo = sorted(FONTS.glob("Cairo*.ttf"))
    if cairo:
        return cairo, None
    present = next((found for family in FALLBACK_FONTS if (found := [f for f in family if f.exists()])), [])
    if present:
        return present, Warning_(
            code="FONT_FALLBACK",
            message="Cairo is not installed (run scripts/fetch_fonts.py); "
            "a local stand-in font was embedded for development",
        )
    return [], Warning_(code="FONT_MISSING", message="no font file found")


def font_chars(tl: Timeline, scenes_svg: str, ui: dict) -> str:
    """Every character the page can show: captions, names, title, interaction texts, UI strings, SVG labels."""
    parts = [tl.meta.title] + [c.get("name", "") for c in tl.characters.values()] + list(ui.values())
    for s in tl.scenes:
        parts += [ln.text for ln in s.lines] + [w.w for ln in s.lines for w in ln.words]
        if s.interaction is not None:
            parts.append(json.dumps(s.interaction.model_dump(), ensure_ascii=False))
    parts += re.findall(r">([^<>]+)</text>", scenes_svg)
    parts.append("0123456789٠١٢٣٤٥٦٧٨٩×:.,،؛؟!?()-–—/ «»\"'")
    return "".join(sorted(set("".join(parts))))


def subset_font(chars: str) -> tuple[list[tuple[str, int, str]], list[Warning_]]:
    """Return `[(family, weight, data_uri)]`: one subset per font file, WOFF2 when brotli is installed."""
    from fontTools import subset
    from fontTools.ttLib import TTFont

    logging.getLogger("fontTools.subset").setLevel(logging.ERROR)  # "FFTM NOT subset" and similar notes

    files, warn = _font_files()
    warnings = [warn] if warn else []
    try:
        import brotli  # noqa: F401

        flavor, mime = "woff2", "font/woff2"
    except ImportError:
        flavor, mime = "woff", "font/woff"
        warnings.append(
            Warning_(
                code="FONT_WOFF",
                message="brotli is not installed: the font is embedded as WOFF, "
                "not WOFF2 (install brotli in production)",
            )
        )
    out = []
    for f in files:
        font = TTFont(str(f))
        opts = subset.Options()
        opts.layout_features = ["*"]  # keep Arabic shaping (init/medi/fina, ligatures)
        opts.name_IDs, opts.notdef_outline, opts.hinting, opts.flavor = ["*"], True, False, flavor
        sub = subset.Subsetter(opts)
        sub.populate(text=chars)
        sub.subset(font)
        buf = io.BytesIO()
        font.flavor = flavor
        font.save(buf)
        weight = 700 if re.search(r"bold|bd$", f.stem, re.IGNORECASE) else 400
        if "[" in f.stem:  # a variable font covers every weight
            weight = 0
        out.append(("Cairo", weight, f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode()))
    return out, warnings


def font_css(faces: list[tuple[str, int, str]]) -> str:
    css = []
    for family, weight, uri in faces:
        w = "200 1000" if weight == 0 else str(weight)
        css.append(f"@font-face{{font-family:{family};src:url({uri});font-weight:{w};font-display:block}}")
    return "".join(css)


# ----------------------------------------------------------------------------------------------- runtime text
_BLOCK_COMMENT = re.compile(r"/\*[\s\S]*?\*/")


def _strip(code: str) -> str:
    """Remove /* */ comments and indentation (the runtime sources use only block comments)."""
    code = _BLOCK_COMMENT.sub("", code)
    return "\n".join(line.strip() for line in code.splitlines() if line.strip())


def _json_for_script(obj) -> str:
    """JSON that is safe inside <script>: `<` can never close the tag, and U+2028/2029 cannot break the line."""
    return (
        json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def runtime_css(age_group: str) -> str:
    return _strip(
        tokens_css(age_group)
        + "\n"
        + "\n".join((RUNTIME / f).read_text(encoding="utf-8") for f in ("scene.css", "actors.css", "player.css"))
    )


def runtime_js(language: str) -> str:
    i18n = json.loads((RUNTIME / "i18n.json").read_text(encoding="utf-8"))
    src = (RUNTIME / "player.js").read_text(encoding="utf-8")
    src = src.replace("/*@I18N*/{}", _json_for_script({language: i18n[language]}))
    return _strip(src)


# ----------------------------------------------------------------------------------------------- assemble
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def fill_template(template: str, values: dict[str, str]) -> str:
    """Replace every `{{name}}` in ONE pass over the template. Inserted text is never scanned again, so a story line
    that happens to contain "{{player}}" stays plain text instead of pulling the player source into the page."""
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)] if m.group(1) in values else m.group(0), template)


def run(
    timeline: Timeline,
    scenes: dict[str, str],
    audio: dict[str, bytes],
    *,
    age_group: str,
    generated_at: str | None = None,
) -> Bundle:
    """`scenes` maps scene_id -> scene.svg (S8 output); `audio` maps `audio/{line_id}.mp3` -> bytes (S6 output)."""
    language = timeline.meta.language
    ui = json.loads((RUNTIME / "i18n.json").read_text(encoding="utf-8"))[language]
    warnings: list[Warning_] = []
    scene_markup, problems = [], []
    for s in timeline.scenes:
        if s.id not in scenes:
            problems.append(f"no scene svg for {s.id}")
            continue
        ns = namespace_scene(s.id, scenes[s.id])
        res = sanitize(ns, "scene")
        problems += [f"{s.id}: {v.rule}: {v.detail}" for v in res.violations]
        scene_markup.append(ns)
    scenes_html = "".join(scene_markup)
    faces, font_warnings = subset_font(font_chars(timeline, scenes_html, ui))
    warnings += font_warnings
    data = namespace_story_data(timeline)
    title = html_lib.escape(timeline.meta.title, quote=True)
    page = fill_template(
        (RUNTIME / "template.html").read_text(encoding="utf-8"),
        {
            "lang": language,
            "dir": timeline.meta.dir,
            "title": title,
            "style": font_css(faces) + runtime_css(age_group),
            "scenes": scenes_html,
            "story_data": _json_for_script(data),
            "player": runtime_js(language),
        },
    )
    audio_files = {k: v for k, v in audio.items()}
    report = check_bundle(page, audio_files)
    for p in problems:
        report.add("scene_sanitized", False, "blocker", p, code="SCENE_UNSAFE", suspect_stage="S8_prop")
    body = page.encode("utf-8")
    meta = BundleMeta(
        title=timeline.meta.title,
        duration_ms=timeline.meta.duration_ms,
        language=language,
        dir=timeline.meta.dir,
        age_group=age_group,
        scene_count=len(timeline.scenes),
        interaction_count=sum(1 for s in timeline.scenes if s.interaction is not None),
        sizes=Sizes(
            bundle_bytes=len(body), audio_bytes=sum(len(b) for b in audio_files.values()), audio_files=len(audio_files)
        ),
        schema_version=SCHEMA_VERSION,
        runtime_version=RUNTIME_VERSION,
        generated_at=generated_at or dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )
    return Bundle(index_html=body, audio=audio_files, meta=meta, report=report, warnings=warnings)
