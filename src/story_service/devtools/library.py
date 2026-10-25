"""Library acceptance (Section 6.8): contact sheets of the asset library, their VLM review, and the joints check.

    sheets = render_sheets(out_dir)            # backgrounds x 3 times of day, props, 12 characters x 6 x 10 poses
    review_sheets(sheets, vlm)                 # every sheet scored >= 4 by the vision model
    joints_problems()                          # limbs never detach from the torso, in any pose

`scripts/library_sheets.py` is the command line; `tests/integration/test_library_browser.py` runs it in CI."""

from __future__ import annotations

import random
from pathlib import Path

from ..assets.catalog import ASSETS, load_catalog
from ..assets.characters.build_character import DOMAINS, EXPRESSIONS, POSES, build_character
from ..assets.svgutil import load, to_str
from ..assets.tokens import tokens_css
from ..pipeline.models import CastCharacter

RUNTIME = ASSETS.parent / "runtime"
SHEET_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "number"}, "issues": {"type": "array", "items": {"type": "string"}}},
    "required": ["score"],
}
SHEET_REVIEW_PROMPT = (
    "You review a contact sheet of a children's flat vector illustration library. Score from 1 to 5 how clean it "
    "is: no clipped or floating parts, a consistent style, readable faces. List concrete problems in `issues`."
)


def random_characters(n: int = 12, seed: int = 6) -> list[CastCharacter]:
    """`n` varied characters (every build, both presentations, head coverings and accessories)."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        build = DOMAINS["build"][i % 4]
        pres = "fm"[i % 2]
        hair = rng.choice([h for h in DOMAINS["hair_style"] if build == "elder" or h != "thin_elder"])
        out.append(CastCharacter.model_validate({
            "key": f"c{i:02d}", "build": build, "presentation": pres, "skin_tone": i % 6, "hair_style": hair,
            "hair_color": rng.randrange(6),
            "top": {"style": rng.choice(DOMAINS["top_style"]), "color_token": rng.choice(DOMAINS["color_token"][:4])},
            "bottom": {"style": rng.choice(["trousers", "shorts", "skirt_long", "skirt_knee"]),
                       "color_token": rng.choice(DOMAINS["color_token"][4:7])},
            "shoes": rng.choice(DOMAINS["shoes"]),
            "accessories": rng.sample(DOMAINS["accessories"], k=i % 3),
            "head_covering": ["none", "hijab", "none", "cap", "none", "kufi"][i % 6] if pres == "f" or i % 6 in (3, 5)
            else "none",
            "voice_profile": {"gender": pres, "age_band": build}, "idle_style": "normal",
        }))  # fmt: skip
    return out


def _page(body: str, age_group: str = "9-11") -> str:
    """A page with the palette and the runtime CSS; `freeze` stops the idle loops so poses hold still."""
    css = tokens_css(age_group) + "".join(
        (RUNTIME / f).read_text(encoding="utf-8") for f in ("scene.css", "actors.css")
    )
    return (
        f"<!doctype html><meta charset='utf-8'><style>{css}"
        "body{margin:0;background:#fff;font:12px sans-serif}.cell{display:inline-block;margin:4px;text-align:center}"
        f"</style><body class='freeze'>{body}</body>"
    )


def _actor_svg(c: CastCharacter, pose: str = "idle", expr: str = "neutral", size: int = 110) -> str:
    inner, style = build_character(c, c.key)
    return (
        f"<svg width='{size}' height='{size * 2}' viewBox='0 0 200 400'><g class='actor' data-build='{c.build}' "
        f"data-pose='{pose}' data-expr='{expr}' data-facing='right' style='{style}'><g class='p-flip'>{inner}"
        "</g></g></svg>"
    )


def sheet_pages() -> dict[str, str]:
    """name -> HTML page of one contact sheet."""
    cat = load_catalog()
    pages = {}
    bgs = []
    for b in cat["backgrounds"]:
        svg = to_str(load(ASSETS / "backgrounds" / f"{b['id']}.svg"))
        for tod in ("day", "evening", "night"):
            if tod in b["supports"]:
                bgs.append(
                    f"<div class='cell'><svg class='tod-{tod}' width='320' height='180' viewBox='0 0 1280 720'>"
                    f"{svg}</svg><br>{b['id']} {tod}</div>"
                )
    pages["backgrounds"] = "".join(bgs)
    pages["props"] = "".join(
        f"<div class='cell'><svg class='tod-day' width='120' height='120' viewBox='0 0 200 200'>"
        f"{to_str(load(ASSETS / 'props' / (p['id'] + '.svg')))}</svg><br>{p['id'][5:]}</div>"
        for p in cat["props"]
    )
    for c in random_characters():  # every expression in every pose (6 x 10), one row per expression
        rows = [
            "<div>" + "".join(f"<div class='cell'>{_actor_svg(c, p, e, 56)}<br>{e} {p}</div>" for p in POSES) + "</div>"
            for e in EXPRESSIONS
        ]
        pages[f"character_{c.key}"] = "".join(rows)
    return {k: _page(v) for k, v in pages.items()}


def render_sheets(out_dir: str | Path, browser=None) -> dict[str, Path]:
    """Screenshot every sheet into `out_dir` (PNG, full page)."""
    from playwright.sync_api import sync_playwright

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    own = browser is None
    pw = sync_playwright().start() if own else None
    browser = browser or pw.chromium.launch()
    try:
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        paths = {}
        for name, html in sheet_pages().items():
            page.set_content(html)
            path = out_dir / f"{name}.png"
            page.screenshot(path=str(path), full_page=True)
            paths[name] = path
        return paths
    finally:
        if own:
            browser.close()
            pw.stop()


def review_sheets(paths: dict[str, Path], vlm) -> dict[str, tuple[float, list[str]]]:
    """name -> (VLM score 1-5, issues) per sheet; Section 6.8 wants every score at least 4."""
    out = {}
    for name, path in paths.items():
        r = vlm.complete_json(
            SHEET_REVIEW_PROMPT, f"Sheet: {name}", SHEET_REVIEW_SCHEMA, "vision", 0.2, 600, images=[path.read_bytes()]
        )
        out[name] = (float(r.get("score", 0)), [str(x) for x in r.get("issues", [])])
    return out


def joints_problems(browser=None) -> list[str]:
    """In every pose and build, the arm and leg groups' boxes intersect the torso's box (limbs never detach)."""
    from playwright.sync_api import sync_playwright

    chars = random_characters(4)  # one per build
    body = "".join(_actor_svg(c, p) for c in chars for p in POSES)
    own = browser is None
    pw = sync_playwright().start() if own else None
    browser = browser or pw.chromium.launch()
    try:
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.set_content(_page(body))
        return page.evaluate(
            """() => { const out = [];
            const hit = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
            document.querySelectorAll('.actor').forEach(a => {
              const name = `${a.dataset.build} ${a.dataset.pose}`, t = a.querySelector('.p-torso');
              if (!t) { out.push(`${name}: no torso`); return; }
              const torso = t.getBoundingClientRect();
              ['p-arm-l', 'p-arm-r', 'p-leg-l', 'p-leg-r'].forEach(cls => {
                const el = a.querySelector('.' + cls);
                if (!el) out.push(`${name}: no ${cls}`);
                else if (!hit(el.getBoundingClientRect(), torso)) out.push(`${name}: ${cls} detached`);
              }); });
            return out; }"""
        )
    finally:
        if own:
            browser.close()
            pw.stop()
