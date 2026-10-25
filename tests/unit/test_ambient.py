"""Ambient loops (DECISIONS 61): every amb-* class used by an asset is defined in scene.css, and the loops follow the
idle-loop rules (off with ?freeze=1 and with reduced motion)."""

import re

from story_service.assets.catalog import ASSETS, load_catalog

SCENE_CSS = (ASSETS.parent / "runtime" / "scene.css").read_text(encoding="utf-8")


def used_classes() -> set[str]:
    out = set()
    for f in list((ASSETS / "props").glob("*.svg")) + list((ASSETS / "backgrounds").glob("*.svg")):
        for cls in re.findall(r'class="([^"]+)"', f.read_text(encoding="utf-8")):
            out |= {c for c in cls.split() if c.startswith("amb-")}
    return out


def test_every_ambient_class_has_a_rule():
    used = used_classes()
    assert {"amb-hand", "amb-sway", "amb-steam", "amb-fly", "amb-tail", "amb-blink"} <= used
    missing = [c for c in used if not re.search(rf"\.{c}\b[^{{]*\{{", SCENE_CSS)]
    assert not missing, missing


def test_loops_stop_when_frozen_or_reduced():
    assert '.freeze [class*="amb-"]{animation:none!important}' in SCENE_CSS
    assert re.search(r'prefers-reduced-motion:reduce\)\{\[class\*="amb-"\]\{animation:none!important\}', SCENE_CSS)
    assert 'svg.scene:not(.active) [class*="amb-"]{animation-play-state:paused}' in SCENE_CSS


def test_cats_in_public_places_and_in_the_catalog():
    for bg in ("bg_street", "bg_park", "bg_market", "bg_school_yard", "bg_playground"):
        assert 'class="bg-cat"' in (ASSETS / "backgrounds" / f"{bg}.svg").read_text(encoding="utf-8"), bg
    cat = next(p for p in load_catalog()["props"] if p["id"] == "prop_cat")
    assert cat["label_ar"] == "قطة" and "animal" in cat["tags"]
