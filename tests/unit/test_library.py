"""Library acceptance, lint part (Section 6.8): every asset passes the sanitizer, its element limit and the
token-only color rule; the catalog matches the files."""

import pytest

from story_service.assets.catalog import ASSETS, build_catalog, load_catalog
from story_service.assets.characters.build_character import MAX_ELEMENTS, build_character, count_elements
from story_service.assets.tokens import known_var_names
from story_service.devtools.library import random_characters
from story_service.validators.svg_sanitizer import lint_colors, sanitize

BACKGROUNDS = sorted((ASSETS / "backgrounds").glob("bg_*.svg"))
PROPS = sorted((ASSETS / "props").glob("prop_*.svg"))
TOKENS = known_var_names()


@pytest.mark.parametrize("path", BACKGROUNDS, ids=lambda p: p.stem)
def test_background_is_clean(path):
    svg = path.read_text(encoding="utf-8")
    res = sanitize(svg, "background")
    assert res.ok, res.violations
    assert lint_colors(svg, TOKENS) == []


@pytest.mark.parametrize("path", PROPS, ids=lambda p: p.stem)
def test_prop_is_clean(path):
    svg = path.read_text(encoding="utf-8")
    res = sanitize(svg, "prop")
    assert res.ok, res.violations
    assert lint_colors(svg, TOKENS) == []


def test_library_sizes():
    assert len(BACKGROUNDS) >= 16 and len(PROPS) >= 48


@pytest.mark.parametrize("character", random_characters(), ids=lambda c: c.key)
def test_character_is_clean(character):
    inner, style = build_character(character, character.key)
    res = sanitize(f"<g>{inner}</g>", "character")
    assert res.ok, res.violations
    assert count_elements(inner) <= MAX_ELEMENTS
    assert lint_colors(f"<g>{inner}</g>", TOKENS) == []
    assert "#" not in style.replace("var(--", "")  # the style holds tokens, not raw colors


def test_catalog_matches_the_files():
    assert load_catalog() == build_catalog()
