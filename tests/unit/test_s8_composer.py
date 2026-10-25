"""S8 Scene Composer (spec sample and table) and the widgets it places."""

import re

import pytest

import golden
from story_service.assets.svgutil import parse
from story_service.assets.widgets import WIDGETS, render_widget, widget_texts
from story_service.pipeline.models import ScenePlan
from story_service.pipeline.stages import s8_composer
from story_service.pipeline.stages.s8_composer import make_custom_prop
from story_service.pipeline.stages.s10_assembler import namespace_scene
from story_service.providers.blobstore import MemoryBlobStore
from story_service.providers.llm import FakeLLM
from story_service.providers.vlm import FakeVLM
from story_service.validators.svg_sanitizer import MAX_DEPTH, sanitize

CUTTER = "s8_custom_pizza_cutter.json"


def PNG(svg: str) -> bytes:  # noqa: N802  (stands in for the Chromium render in unit tests)
    return b"\x89PNG fake"


DESCRIPTION = "A round pizza cutter wheel with a short handle, flat style, centered."


def svg_of(shapes: str) -> dict:
    return {"svg": f'<svg viewBox="0 0 200 200">{shapes}</svg>'}


def test_s8_composes_valid_svg():
    svg, meta = s8_composer.run(
        golden.plan("s03"), golden.cast(), golden.CATALOG, llm=FakeLLM(CUTTER), vlm=FakeVLM(score=5)
    )
    root = parse(svg)[0]
    assert root.get("id") == "scene-s03"
    ids = [e.get("id") for e in root.iter() if isinstance(e.tag, str) and e.get("id")]
    assert len(ids) == len(set(ids)) and {"nadia", "karim", "table1", "pizza1", "pie1"} <= set(ids)
    assert root.xpath("//*[@id='pie1']")[0].get("data-initial") == "hidden"
    assert sanitize(svg).violations == []
    assert len(list(root.iter())) <= 600
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b|rgb\(", svg)
    assert set(meta.elements) >= {"nadia", "karim", "table1", "pizza1", "pie1", "cutter1"}


@pytest.mark.parametrize("scene_id", ["s01", "s02", "s03", "s04", "s05"])
def test_every_golden_scene_composes_within_limits(scene_id):
    svg, _ = s8_composer.run(
        golden.plan(scene_id), golden.cast(), golden.CATALOG, llm=FakeLLM(CUTTER), vlm=FakeVLM(score=5)
    )
    res = sanitize(namespace_scene(scene_id, svg), "scene")  # as it sits in the bundle (S10)
    assert res.ok and res.depth <= MAX_DEPTH, (res.violations, res.depth)


def test_bboxes_are_catalog_bbox_times_scale():
    plan = golden.plan("s03")
    _, meta = s8_composer.compose(plan, golden.cast(), golden.CATALOG, custom_props={"pizza_cutter": "<g/>"})
    table = next(p for p in plan.props if p.id == "table1")
    x0, y0, x1, y1 = next(p for p in golden.CATALOG["props"] if p["id"] == "prop_table")["bbox"]
    b = meta.elements["table1"].bbox
    assert b[3] == table.y and abs((b[2] - b[0]) - (x1 - x0) * table.scale) < 0.01
    assert abs((b[3] - b[1]) - (y1 - y0) * table.scale) < 0.01


def test_custom_prop_ok_is_cached():
    blob = MemoryBlobStore()
    inner, warning = make_custom_prop(DESCRIPTION, llm=FakeLLM(CUTTER), vlm=FakeVLM(score=4), blob=blob, render_png=PNG)
    assert warning is None and "<circle" in inner
    keys = blob.list("custom_props/")
    assert len(keys) == 1 and re.fullmatch(r"custom_props/[0-9a-f]{64}\.svg", keys[0])


def test_cache_hit_makes_no_llm_call():
    blob, llm = MemoryBlobStore(), FakeLLM(CUTTER)
    make_custom_prop(DESCRIPTION, llm=llm, vlm=FakeVLM(score=5), blob=blob, render_png=PNG)
    make_custom_prop(DESCRIPTION, llm=llm, vlm=FakeVLM(score=5), blob=blob, render_png=PNG)
    assert len(llm.calls) == 1


def test_custom_prop_fails_falls_back():
    vlm = FakeVLM(prop_scores=[2, 2, 2])
    inner, warning = make_custom_prop(DESCRIPTION, llm=FakeLLM(CUTTER), vlm=vlm, blob=MemoryBlobStore(), render_png=PNG)
    assert warning.code == "CUSTOM_PROP_FALLBACK" and vlm.calls.count("prop") == 3
    assert "<circle" not in inner  # the generic box, not the rejected drawing


def test_custom_prop_with_script_is_retried():
    bad = svg_of('<script>alert(1)</script><circle cx="100" cy="100" r="50" fill="var(--c-primary)"/>')
    llm = FakeLLM(bad, CUTTER)
    inner, warning = make_custom_prop(DESCRIPTION, llm=llm, vlm=FakeVLM(score=5), blob=None, render_png=PNG)
    assert warning is None and len(llm.calls) == 2 and "script" not in inner
    assert "rejected" in llm.calls[1]["user"]


def test_custom_prop_with_55_elements_rejected():
    many = svg_of("".join(f'<circle cx="{i * 3}" cy="100" r="2" fill="var(--ink)"/>' for i in range(55)))
    _, warning = make_custom_prop(DESCRIPTION, llm=FakeLLM(many), vlm=FakeVLM(score=5), blob=None, render_png=PNG)
    assert warning is not None and warning.code == "CUSTOM_PROP_FALLBACK"


def test_custom_prop_with_hex_color_rejected():
    hexed = svg_of('<circle cx="100" cy="100" r="60" fill="#ff0000"/>')
    _, warning = make_custom_prop(DESCRIPTION, llm=FakeLLM(hexed), vlm=FakeVLM(score=5), blob=None, render_png=PNG)
    assert warning is not None


def test_pie_fraction_has_exact_slices():
    markup, _ = render_widget("widget_pie_fraction", {"parts": 4, "filled": 1})
    root = parse(markup)
    slices = root.xpath("//*[contains(concat(' ', @class, ' '), ' slice ')]")
    assert len(slices) == 4 and sum(" on" in s.get("class") for s in slices) == 1


@pytest.mark.parametrize("ref", sorted(WIDGETS))
def test_every_widget_renders_clean_with_its_example(ref):
    examples = {
        "widget_pie_fraction": {"parts": 3, "filled": 2},
        "widget_bar_fraction": {"parts": 5, "filled": 2},
        "widget_number_line": {"min": 0, "max": 10, "step": 1, "marks": [3], "highlight": 7},
        "widget_bar_chart": {"labels": ["أ", "ب"], "values": [3, 5]},
        "widget_balance_scale": {"left_count": 2, "right_count": 3},
        "widget_grid_array": {"rows": 3, "cols": 4},
        "widget_clock_face": {"hour": 3, "minute": 30},
        "widget_ruler": {"length_cm": 10, "mark_cm": 4},
        "widget_coin_stack": {"count": 5},
        "widget_thermometer": {"value": 20, "min": 0, "max": 40},
        "widget_shape_set": {"shapes": ["triangle", "square"]},
        "widget_timeline_axis": {"events": ["صباح", "ظهر"]},
        "widget_label_card": {"text": "1/4"},
    }
    markup, bbox = render_widget(ref, examples[ref])
    res = sanitize(markup, "widget")
    assert res.ok, res.violations
    assert bbox[2] > bbox[0] and bbox[3] > bbox[1]
    assert all(size >= 44 for _, size in widget_texts(markup))


def test_widget_bad_params_raise():
    with pytest.raises(ValueError):
        render_widget("widget_pie_fraction", {"parts": 4, "filled": 6})


def test_actor_kit_structure_and_flip():
    svg, _ = s8_composer.compose(
        golden.plan("s03"), golden.cast(), golden.CATALOG, custom_props={"pizza_cutter": "<g/>"}
    )
    karim = parse(svg).xpath("//*[@id='karim']")[0]
    assert karim.get("data-facing") == "left" and karim.get("class") == "actor"
    placement = karim[0]
    assert placement.get("transform").endswith("translate(-100 -390)")
    assert [c.get("class") for c in placement] == ["shadow", "p-flip"]  # the contact shadow does not flip
    assert karim.get("data-build") == "child"


def test_props_carry_their_name_and_a_shadow():
    plan = golden.plan("s03")
    svg, _ = s8_composer.compose(plan, golden.cast(), golden.CATALOG, custom_props={"pizza_cutter": "<g/>"})
    table = parse(svg).xpath("//*[@id='table1']")[0]
    assert table.get("data-name") == "طاولة" and table[0][0].get("class") == "shadow"
    svg_en, _ = s8_composer.compose(
        plan, golden.cast(), golden.CATALOG, custom_props={"pizza_cutter": "<g/>"}, language="en"
    )
    assert parse(svg_en).xpath("//*[@id='table1']")[0].get("data-name") == "Table"


def test_wall_props_have_no_shadow():
    plan = golden.plan_dict("s01")
    clock = next(p for p in plan["props"] if p["ref"] == "prop_wall_clock")
    svg, _ = s8_composer.compose(ScenePlan.model_validate(plan), golden.cast(), golden.CATALOG)
    el = parse(svg).xpath(f"//*[@id='{clock['id']}']")[0]
    assert not el.xpath(".//*[@class='shadow']") and el.get("data-name") == "ساعة حائط"


def test_unjudged_drawing_falls_back():
    """With a VLM but no picture of the drawing, nothing can be judged, so the generic box is used."""
    _, warning = make_custom_prop(DESCRIPTION, llm=FakeLLM(CUTTER), vlm=FakeVLM(score=5), blob=None, render_png=None)
    assert warning is not None and warning.code == "CUSTOM_PROP_FALLBACK"


def test_malformed_custom_prop_is_retried_not_crashed():
    broken = {"svg": '<svg viewBox="0 0 200 200"><rect></svg>'}
    llm = FakeLLM(broken, CUTTER)
    inner, warning = make_custom_prop(DESCRIPTION, llm=llm, vlm=FakeVLM(score=5), blob=None, render_png=PNG)
    assert warning is None and len(llm.calls) == 2


def test_label_text_with_hash_is_not_a_color():
    plan = golden.plan_dict("s03")
    next(w for w in plan["widgets"] if w["ref"] == "widget_label_card")["params"]["text"] = "غرفة #101"
    from story_service.pipeline.models import ScenePlan

    svg, _ = s8_composer.compose(
        ScenePlan.model_validate(plan), golden.cast(), golden.CATALOG, custom_props={"pizza_cutter": "<g/>"}
    )
    assert s8_composer.check_scene_svg(svg) == []
