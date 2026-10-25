"""Character kit builder (Section 6.3): one `<g>` per character from `parts/*.svg` and the cast parameters.

Local coordinates: viewBox 0 0 200 400, feet on y = 390, ground contact (100, 390). The result has every required part
group (head, hair-back, hair-front, face, eyes, brows, mouth, neck, torso, arm-l, arm-r, leg-l, leg-r, shoes-l,
shoes-r, head-covering, accessory-<id>). Ids are prefixed with the character key (`nadia-head`) because one page holds
many characters; the player styles parts by class (`p-head`), never by id.

Expressions, mouth shapes, poses and the build scale are NOT drawn here: every variant is in the markup and the
player's CSS shows the right one from `data-expr`, `data-mouth`, `data-pose` and `data-build` (D3: no JS from the LLM,
no geometry per pose)."""

from __future__ import annotations

import copy
import hashlib
from functools import cache
from pathlib import Path

from lxml import etree

from ..svgutil import E, load, to_str
from ..tokens import actor_style

PARTS = Path(__file__).resolve().parent / "parts"

# parameter domains = the catalog enums (Section 6.3)
DOMAINS = {
    "build": ["child", "teen", "adult", "elder"],
    "presentation": ["f", "m"],
    "skin_tone": list(range(6)),
    "hair_style": [
        "short_crop",
        "side_part",
        "curly_short",
        "afro_puff",
        "long_straight",
        "ponytail",
        "bun",
        "braids",
        "bald",
        "thin_elder",
    ],
    "hair_color": list(range(6)),
    "top_style": ["tshirt", "long_sleeve", "hoodie", "collared_shirt", "dress_long", "tunic", "jacket", "school_top"],
    "bottom_style": ["trousers", "shorts", "skirt_long", "skirt_knee", "none"],
    "shoes": ["sneakers", "sandals", "school_shoes"],
    "accessories": ["glasses_round", "glasses_square", "backpack", "cap", "scarf", "watch", "headband"],
    "head_covering": ["none", "hijab", "cap", "kufi"],
    "color_token": ["c-primary", "c-secondary", "c-accent", "c-support", "c-neutral-1", "c-neutral-2", "ink", "paper"],
}
EXPRESSIONS = ["neutral", "happy", "surprised", "worried", "thinking", "laughing"]
MOUTHS = ["closed", "open_s", "open_m", "open_l", "smile"]
POSES = ["idle", "talk", "wave", "point_right", "point_left", "walk", "think", "celebrate", "shrug", "surprised"]
PIVOTS = {
    "head": (100, 112),
    "arm-l": (62, 150),
    "arm-r": (138, 150),
    "leg-l": (84, 250),
    "leg-r": (116, 250),
    "torso": (100, 250),
}
MAX_ELEMENTS = 140


@cache
def _parts(name: str) -> etree._Element:
    return load(PARTS / f"{name}.svg")


def _pick(name: str, **attrs) -> list[etree._Element]:
    """The children of the part group whose data-* attributes all match (deep copies, safe to edit)."""
    for g in _parts(name):
        if all(g.get(f"data-{k}") == v for k, v in attrs.items()):
            return [copy.deepcopy(c) for c in g]
    return []


def _group(cls: str, children: list, el_id: str | None = None) -> etree._Element:
    return E("g", {"id": el_id, "class": cls}, *children)


def _shift(elements: list, dx: float) -> list:
    """Move a copy of a left-side part to the right side (`translate` on a wrapper keeps the paths untouched)."""
    if not elements:
        return elements
    return [E("g", {"transform": f"translate({dx} 0)"}, *elements)]


def blink_delay(key: str) -> str:
    """Characters blink every 3 to 5 s, never all at once: the delay comes from the key."""
    n = int(hashlib.sha256(key.encode()).hexdigest()[:6], 16)
    return f"-{(n % 4000) / 1000:.2f}s"


def build_character(c, key: str | None = None) -> tuple[str, str]:
    """Return `(inner_markup, style)` for a `CastCharacter`: the parts inside `<g class="p-all">` (local units) and the
    CSS variables the actor wrapper must carry. The S8 composer adds the wrapper, placement and data-* state."""
    key = key or c.key
    covering = c.head_covering
    acc = set(c.accessories)
    if covering == "cap":
        acc.discard("cap")  # the same cap, drawn once
    top_style, bottom_style = c.top.style, c.bottom.style
    top_variant = next(g for g in _parts("tops") if g.get("data-variant") == top_style)
    sleeve = top_variant.get("data-sleeve")
    bottom_variant = next(g for g in _parts("bottoms") if g.get("data-variant") == bottom_style)
    legs_of = bottom_variant.get("data-legs")
    shoe_variant = next(g for g in _parts("shoes") if g.get("data-variant") == c.shoes)

    def bottom_slot(slot):
        for g in bottom_variant:
            if g.get("data-slot") == slot:
                return [copy.deepcopy(x) for x in g]
        return []

    def extras(place, kind=None):
        out = []
        for g in _parts("extras"):
            if g.get("data-place") != place:
                continue
            if g.get("data-covering") and g.get("data-covering") == covering and kind in (None, "covering"):
                out += [copy.deepcopy(x) for x in g]
            elif g.get("data-accessory") in acc and kind in (None, "accessory"):
                out.append(
                    _group(
                        f"acc acc-{g.get('data-accessory')}",
                        [copy.deepcopy(x) for x in g],
                        f"{key}-accessory-{g.get('data-accessory')}" if place != "torso" else None,
                    )
                )
        return out

    # ---- hair (hidden entirely under a hijab)
    hair_back = [] if covering == "hijab" else _pick("hair", variant=c.hair_style, layer="back")
    hair_front = [] if covering == "hijab" else _pick("hair", variant=c.hair_style, layer="front")
    # ---- legs and shoes
    shoe = [copy.deepcopy(x) for x in shoe_variant]

    def leg(side, dx):
        body = _pick("body", slot=f"leg-{side}")
        over = bottom_slot(f"leg-{side}")
        shoes = shoe if dx == 0 else _shift([copy.deepcopy(x) for x in shoe], dx)
        return _group(
            f"p-leg-{side}",
            body + over + [_group(f"p-shoes-{side}", shoes, f"{key}-shoes-{side}")],
            f"{key}-leg-{side}",
        )

    # ---- torso: hips, skirt, the top and its details, backpack straps
    torso_children = (
        bottom_slot("hips")
        + bottom_slot("skirt")
        + [copy.deepcopy(x) for x in top_variant]
        + extras("torso", "accessory")
    )
    torso = _group("p-torso", torso_children, f"{key}-torso")

    # ---- arms: upper arm (shoulder pivot) + forearm (elbow pivot)
    def arm(side):
        if sleeve == "long":
            upper = _pick("tops", **{"sleeve-part": f"long-upper-{side}"})
            fore = _pick("tops", **{"sleeve-part": f"long-fore-{side}"}) + _pick("body", slot=f"hand-{side}")
        else:
            upper = _pick("body", slot=f"upper-{side}") + _pick("tops", **{"sleeve-part": f"short-upper-{side}"})
            fore = _pick("body", slot=f"fore-{side}")
        if side == "l":
            fore += extras("wrist", "accessory")
        return _group(f"p-arm-{side}", upper + [_group(f"p-fore-{side}", fore)], f"{key}-arm-{side}")

    body = _group(
        "p-body",
        [leg("l", 0), leg("r", 32), _group("p-neck", _pick("body", slot="neck"), f"{key}-neck"), torso]
        + extras("neck", "accessory")
        + [arm("l"), arm("r")],
    )

    # ---- head: shape, face (eyes, brows, mouth), hair front, covering, accessories on the head
    face = _group(
        "p-face",
        [
            _group("p-cheeks", _pick("face", slot="cheeks")),
            _group("p-eyes", _pick("face", slot="eyes"), f"{key}-eyes"),
            _group("p-brows", _pick("face", slot="brows"), f"{key}-brows"),
            _group("p-mouth", _pick("face", slot="mouth"), f"{key}-mouth"),
        ],
        f"{key}-face",
    )
    head_children = _pick("body", slot="head") + [
        face,
        _group("p-hairfront", hair_front, f"{key}-hair-front"),
        _group("p-cover", extras("head", "covering"), f"{key}-head-covering"),
    ]
    head_children += extras("head", "accessory")
    head = _group("p-headwrap", [_group("p-head", head_children, f"{key}-head")])
    hair = _group("p-hairwrap", [_group("p-hairback", hair_back, f"{key}-hair-back")])
    back = extras("back", "accessory")

    root = _group("p-all", [hair] + back + [body, head])
    for el in root.iter():  # the kit's data-* markers are build-time only
        for a in [a for a in el.attrib if a.startswith("data-")]:
            del el.attrib[a]
    inner = to_str(root)
    shoe_token = shoe_variant.get("data-color")
    style = actor_style(c.skin_tone, c.hair_color, c.top.color_token, c.bottom.color_token, shoe_token)
    style += ";--leg:var(--bottom)" if legs_of == "bottom" else ";--leg:var(--skin)"
    style += f";--blink:{blink_delay(key)}"
    return inner, style


def count_elements(markup: str) -> int:
    root = etree.fromstring(f'<svg xmlns="http://www.w3.org/2000/svg">{markup}</svg>'.encode())
    return sum(1 for _ in root.iter()) - 1
