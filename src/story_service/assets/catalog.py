"""The asset catalog (Section 6.7): the ONLY source of ids and parameter domains given to LLM stages.

`build_catalog()` reads every asset file (backgrounds, props), the widget registry and the character kit, and returns
the dict that `scripts/build_catalog.py` writes to `assets/catalog.json`. `load_catalog()` reads that file (or builds it
when missing). Prompts show `label` and `tags` so the model picks sensible assets.

Placement conventions (the director must know them, so they are part of the catalog):
  actors   (x, y) = horizontal center and feet baseline
  props    (x, y) = bottom-center of the prop's bbox (where it touches the ground or a surface)
  widgets  (x, y) = center of the widget's box"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from .characters.build_character import DOMAINS, EXPRESSIONS, MOUTHS, POSES
from .svgutil import load
from .widgets import WIDGETS

ASSETS = Path(__file__).resolve().parent
CATALOG_FILE = ASSETS / "catalog.json"
# the character's bounding box in its local 200x400 box, per build (head top to feet, arms included)
CHARACTER_BBOX = {
    "child": [52, 64, 148, 391],
    "teen": [55, 42, 145, 391],
    "adult": [53, 28, 147, 391],
    "elder": [55, 45, 145, 391],
}
CONVENTIONS = {
    "stage": [1280, 720],
    "ground_y": 640,
    "safe_area": {"x": [40, 1240], "y": [40, 700]},
    "z": {"background": 0, "props": 10, "actors": 20, "widgets": 30},
    "anchors": {
        "actor": "x = center, y = feet baseline",
        "prop": "x, y = bottom-center of its bbox",
        "widget": "x, y = center of its box",
    },
    "scale": "a prop's default_scale is its natural size next to an adult at scale 1.0; a surface value is the local "
    "y of its top (put things on it at y = prop.y - (bbox_bottom - surface) * scale)",
}


def _floats(s: str) -> list[float]:
    return [float(v) for v in s.split()]


def build_catalog() -> dict:
    backgrounds = []
    for f in sorted((ASSETS / "backgrounds").glob("bg_*.svg")):
        r = load(f)
        slots = [[float(v) for v in xy.split(",")] for xy in r.get("data-slots", "").split()]
        backgrounds.append(
            {
                "id": f.stem,
                "label": r.get("data-label"),
                "kind": r.get("data-kind"),
                "ground_y": float(r.get("data-ground-y", 640)),
                "supports": r.get("data-supports").split(),
                "slots": dict(zip(["slot_left", "slot_center", "slot_right"], slots)),
            }
        )
    props = []
    for f in sorted((ASSETS / "props").glob("prop_*.svg")):
        r = load(f)
        item = {
            "id": f.stem,
            "label": r.get("data-label"),
            "label_ar": r.get("data-label-ar"),
            "tags": r.get("data-tags", "").split(),
            "bbox": _floats(r.get("data-bbox")),
            "default_scale": float(r.get("data-scale", 1)),
        }
        if r.get("data-surface"):
            item["surface"] = float(r.get("data-surface"))
        if r.get("data-shadow") == "none":  # hangs on a wall or floats: no contact shadow under it
            item["shadow"] = False
        props.append(item)
    widgets = [{"id": w.id, "label": w.label, "params": w.params, "bbox": list(w.bbox)} for w in WIDGETS.values()]
    character = {k: v for k, v in DOMAINS.items()}
    character["top_style"], character["bottom_style"] = DOMAINS["top_style"], DOMAINS["bottom_style"]
    return {
        "version": 1,
        "conventions": CONVENTIONS,
        "character": character,
        "character_bbox": CHARACTER_BBOX,
        "backgrounds": backgrounds,
        "props": props,
        "widgets": widgets,
        "poses": POSES,
        "expressions": EXPRESSIONS,
        "mouths": MOUTHS,
    }


@lru_cache(maxsize=1)
def load_catalog() -> dict:
    if CATALOG_FILE.exists():
        return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    return build_catalog()


def index(catalog: dict) -> dict:
    """Fast lookups: {"bg": {id: …}, "prop": {id: …}, "widget": {id: …}}."""
    return {
        "bg": {b["id"]: b for b in catalog["backgrounds"]},
        "prop": {p["id"]: p for p in catalog["props"]},
        "widget": {w["id"]: w for w in catalog["widgets"]},
    }


def prop_stage_bbox(prop: dict, x: float, y: float, scale: float) -> list[float]:
    x0, y0, x1, y1 = prop["bbox"]
    ax = (x0 + x1) / 2
    return [x + (x0 - ax) * scale, y - (y1 - y0) * scale, x + (x1 - ax) * scale, y]


def widget_stage_bbox(local_bbox, x: float, y: float, scale: float) -> list[float]:
    x0, y0, x1, y1 = local_bbox
    w, h = (x1 - x0) * scale, (y1 - y0) * scale
    return [x - w / 2, y - h / 2, x + w / 2, y + h / 2]


def actor_stage_bbox(build: str, x: float, y: float, scale: float) -> list[float]:
    x0, y0, x1, y1 = CHARACTER_BBOX[build]
    return [x + (x0 - 100) * scale, y + (y0 - 390) * scale, x + (x1 - 100) * scale, y + (y1 - 390) * scale]


def custom_ref(ref: str) -> str | None:
    """`custom:pizza_cutter` -> `pizza_cutter`; anything else -> None."""
    m = re.fullmatch(r"custom:([a-z0-9_]+)", ref or "")
    return m.group(1) if m else None
