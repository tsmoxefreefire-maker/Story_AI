"""Section 8.2: the sanitizer accepts clean SVG and rejects each forbidden construct (one test per rule)."""

import pytest

from story_service.validators.svg_sanitizer import lint_colors, sanitize

OK = '<g id="a"><rect x="0" y="0" width="10" height="10" fill="var(--c-primary)"/><use href="#a"/></g>'


def rules(svg, kind="scene"):
    return [v.rule for v in sanitize(svg, kind).violations]


def test_clean_fragment_passes():
    r = sanitize(OK)
    assert r.ok and r.elements == 3 and 'fill="var(--c-primary)"' in r.clean


@pytest.mark.parametrize(
    "tag",
    [
        "script",
        "foreignObject",
        "image",
        "iframe",
        "object",
        "embed",
        "animate",
        "animateTransform",
        "animateMotion",
        "set",
        "filter",
        "mask",
        "pattern",
        "marker",
        "a",
        "style",
        "video",
    ],
)
def test_rejected_tags(tag):
    r = sanitize(f"<g><{tag}/></g>")
    assert [v.rule for v in r.violations] == ["tag"] and f"<{tag}" not in r.clean


def test_event_handler_attribute():
    assert rules('<rect onclick="alert(1)"/>') == ["attribute"]
    assert rules('<rect ONLOAD="x()"/>') == ["attribute"]


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "http://x.y/z",
        "https://x.y",
        "//evil.example/x",
        "java\nscript:alert(1)",
    ],
)
def test_rejected_values(value):
    assert rules(f'<rect class="{value}"/>') == ["value"]


def test_unknown_attribute():
    assert rules('<rect srcdoc="x"/>') == ["attribute"]


def test_namespaced_xlink_href():
    assert rules('<use xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="#a"/>') == ["attribute"]


def test_href_only_internal_on_use():
    assert rules('<use href="http://x/y.svg#a"/>') == ["href"]
    assert rules('<rect href="#a"/>') == ["href"]
    assert rules('<use href="#ok-id"/>') == []


def test_style_whitelist():
    assert rules('<rect style="fill:var(--ink);opacity:.5;--x:1"/>') == []
    assert rules('<rect style="position:fixed"/>') == ["style"]
    assert "style" in rules('<rect style="fill:url(http://x/y)"/>')
    assert "style" in rules('<rect style="width:expression(alert(1))"/>')
    assert "style" in rules('<rect style="fill:red;@import x"/>')
    assert "style" in rules('<rect style="fill:\\75rl(x)"/>')


def test_url_only_local():
    assert rules('<rect fill="url(#g1)"/>') == []
    assert rules('<rect fill="url(other.svg#g1)"/>') == ["value"]


def test_data_attributes_allowed():
    assert rules('<g data-pose="idle" data-build="child"/>') == []
    assert rules('<g data-="x"/>') == ["attribute"]


def test_limits():
    many = "<g>" + "<rect/>" * 45 + "</g>"
    assert "limit" in rules(many, "custom_prop")
    assert rules(many, "prop") == []
    deep = "<g>" * 14 + "</g>" * 14
    assert "limit" in rules(deep)
    assert "limit" in rules(f'<path d="M0 0{" L1 1" * 900}"/>')


def test_doctype_and_entities_rejected():
    assert rules('<!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg"/>') == ["parse"]


def test_text_is_escaped_in_the_clean_copy():
    r = sanitize("<text>&lt;/svg&gt;&lt;script&gt;x&lt;/script&gt;</text>")
    assert r.ok and "<script" not in r.clean


def test_token_only_colors():
    assert lint_colors('<g><rect fill="var(--ink)"/><rect fill="url(#k)"/><rect fill="none"/></g>') == []
    assert lint_colors('<rect fill="#ff0000"/>')
    assert lint_colors('<rect style="fill:rgb(1,2,3)"/>')
    assert lint_colors('<rect fill="red"/>') == ["color 'red' is not a token"]
    assert lint_colors('<rect fill="var(--nope)"/>', known_vars={"ink"}) == ["unknown token --nope"]
