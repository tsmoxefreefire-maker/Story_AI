"""S7 Visual Director: the code checks of a scene plan (spec table "Mutation of the golden plan"), plus the LLM loop."""

import pytest

import golden
from story_service.pipeline.stages.s7_director import DirectorError, check_cue_params, overlap_ratio, run, validate_s7
from story_service.providers.llm import FakeLLM


def check(plan, scene_id="s03"):
    return validate_s7(
        plan,
        scene=golden.scene(scene_id),
        audio=golden.audio(),
        catalog=golden.CATALOG,
        setting=golden.cast().setting(scene_id),
        cast=golden.cast(),
    )


def codes(rep, severity="blocker"):
    return {i.code for i in rep.issues if i.severity == severity}


@pytest.mark.parametrize("scene_id", ["s01", "s02", "s03", "s04", "s05"])
def test_golden_plans_valid(scene_id):
    rep = check(golden.plan_dict(scene_id), scene_id)
    assert rep.passed and not rep.blockers and not rep.majors, rep.issues


def test_spec_sample_without_setting_or_cast():
    rep = validate_s7(golden.plan("s03"), scene=golden.scene("s03"), audio=golden.audio(), catalog=golden.CATALOG)
    assert rep.passed and not [c for c in rep.checks if not c.passed and c.severity in ("blocker", "major")]


def test_unknown_prop_ref():
    p = golden.plan_dict("s03")
    p["props"][0]["ref"] = "prop_dragon"
    assert "UNKNOWN_REF" in codes(check(p))


def test_custom_ref_must_be_in_setting():
    p = golden.plan_dict("s03")
    p["props"][2]["ref"] = "custom:rocket"
    assert "UNKNOWN_REF" in codes(check(p))


def test_actor_out_of_bounds_has_fix_hint():
    p = golden.plan_dict("s03")
    p["actors"][0]["x"] = 1400
    rep = check(p)
    issue = next(i for i in rep.issues if i.code == "OUT_OF_BOUNDS")
    assert issue.severity == "blocker" and "40-1240" in issue.fix_hint


def _enter(actor, line="s03_l01"):
    return {
        "id": "s03_c90",
        "at": {"line": line, "anchor": "start"},
        "target": actor,
        "action": "enter",
        "params": {"from": "right"},
        "dur_ms": 600,
    }


def test_offstage_ok_relaxes_the_margin_for_a_walking_actor():
    p = golden.plan_dict("s03")
    p["actors"][2].update(x=1262, offstage_ok=True)  # mama, inside the stage but in the 40 px margin
    p["cues"].append(_enter("mama"))
    assert not codes(check(p)) & {"OUT_OF_BOUNDS", "OFFSTAGE_NO_CUE"}


def test_offstage_ok_needs_an_enter_or_exit_cue():
    p = golden.plan_dict("s03")
    p["actors"][1].update(x=1500, offstage_ok=True)  # karim speaks in s03: he would talk while invisible
    assert {"OFFSTAGE_NO_CUE", "OUT_OF_BOUNDS"} <= codes(check(p))


def test_move_that_ends_off_stage():
    p = golden.plan_dict("s03")
    p["cues"].append(
        {
            "id": "s03_c91",
            "at": {"line": "s03_l02", "anchor": "start"},
            "target": "karim",
            "action": "move",
            "params": {"to": [5000, 0]},
            "dur_ms": 600,
        }
    )
    assert "OUT_OF_BOUNDS" in codes(check(p))


def test_prop_that_drives_in_from_past_the_stage_edge():
    """Found by S11 on the clock story: the anchor was inside, but the bus box started past the right edge."""
    p = golden.plan_dict("s03")
    table = next(x for x in p["props"] if x["ref"] == "prop_table")
    move = {
        "id": "s03_c92",
        "at": {"line": "s03_l02", "anchor": "start"},
        "target": table["id"],
        "action": "move",
        "params": {"from": [560, 0], "to": [0, 0]},
        "dur_ms": 900,
    }
    p["cues"].append(move)
    assert "OUT_OF_BOUNDS" in codes(check(p))
    move["params"] = {"from": [60, 0], "to": [0, 0]}
    assert "OUT_OF_BOUNDS" not in codes(check(p))


def test_camera_must_not_show_outside_the_stage():
    p = golden.plan_dict("s03")
    p["camera"].update(x=-2000)
    assert "CAMERA_BOUNDS" in codes(check(p))


def test_overlapping_transform_tweens_on_one_element():
    p = golden.plan_dict("s03")
    p["cues"] += [
        {
            "id": "s03_c92",
            "at": {"line": "s03_l02", "anchor": "start"},
            "target": "karim",
            "action": "move",
            "params": {"to": [-40, 0]},
            "dur_ms": 1000,
        },
        {
            "id": "s03_c93",
            "at": {"line": "s03_l02", "anchor": "start", "offset_ms": 200},
            "target": "karim",
            "action": "shake",
            "params": {"amplitude": 8},
            "dur_ms": 400,
        },
    ]
    assert "TWEEN_OVERLAP" in codes(check(p))


def test_cue_target_missing():
    p = golden.plan_dict("s03")
    p["cues"][3]["target"] = "ghost"
    assert "CUE_TARGET" in codes(check(p))


def test_cue_line_missing():
    p = golden.plan_dict("s03")
    p["cues"][0]["at"]["line"] = "s03_l09"
    assert "CUE_ANCHOR" in codes(check(p))


def test_word_anchor_without_index_or_out_of_range():
    p = golden.plan_dict("s03")
    p["cues"][4]["at"].pop("word_index")
    assert "CUE_ANCHOR" in codes(check(p))
    p = golden.plan_dict("s03")
    p["cues"][4]["at"]["word_index"] = 40
    assert "CUE_ANCHOR" in codes(check(p))


def test_unknown_action_is_a_schema_blocker():
    p = golden.plan_dict("s03")
    p["cues"][0]["action"] = "spin"
    rep = check(p)
    assert not rep.passed and "SCHEMA" in codes(rep)


def test_five_actors():
    p = golden.plan_dict("s03")
    extra = dict(p["actors"][0], char_key="nadia")
    p["actors"] += [dict(extra, x=200), dict(extra, x=1200)]
    assert "TOO_MANY_ACTORS" in codes(check(p))


def test_no_visual_change_for_8_seconds():
    p = golden.plan_dict("s03")
    p["cues"] = [c for c in p["cues"] if c["at"]["line"] not in ("s03_l03", "s03_l04", "s03_l05")]
    rep = check(p)
    assert "NO_VISUAL_CHANGE" in codes(rep)


def test_same_state_again_is_not_a_change():
    """A pose cue that sets the pose the actor already has changes nothing on screen."""
    p = golden.plan_dict("s01")
    keep = [c for c in p["cues"] if c["at"]["line"] == "s01_l01"]
    p["cues"] = keep + [
        {
            "id": "s01_c99",
            "at": {"line": "s01_l03", "anchor": "start"},
            "target": "nadia",
            "action": "pose",
            "params": {"name": "idle"},
            "dur_ms": 0,
        }
    ]
    assert "NO_VISUAL_CHANGE" in codes(check(p, "s01"))


def test_actors_overlap():
    p = golden.plan_dict("s03")
    p["actors"][0]["x"] = 600
    p["actors"][1]["x"] = 600
    assert "ACTOR_OVERLAP" in codes(check(p))


def test_overlap_ratio_uses_smaller_box():
    assert overlap_ratio([0, 0, 10, 10], [5, 0, 15, 10]) == 0.5
    assert overlap_ratio([0, 0, 10, 10], [0, 0, 100, 100]) == 1.0
    assert overlap_ratio([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0


def test_camera_zoom_too_far():
    p = golden.plan_dict("s03")
    p["camera"]["zoom"] = 1.8
    assert "CAMERA_ZOOM" in codes(check(p))
    p = golden.plan_dict("s03")
    p["cues"].append(
        {
            "id": "s03_c99",
            "at": {"line": "s03_l02", "anchor": "start"},
            "target": "camera",
            "action": "camera",
            "params": {"to": {"x": 640, "y": 360, "zoom": 1.8}},
            "dur_ms": 800,
        }
    )
    assert "CUE_PARAMS" in codes(check(p))


def test_three_camera_moves():
    p = golden.plan_dict("s03")
    for i in range(3):
        p["cues"].append(
            {
                "id": f"s03_cam{i}",
                "at": {"line": "s03_l02", "anchor": "start", "offset_ms": i * 900},
                "target": "camera",
                "action": "camera",
                "params": {"to": {"x": 640, "y": 360, "zoom": 1.1}},
                "dur_ms": 800,
            }
        )
    assert "CAMERA_MOVES" in codes(check(p))


def test_widget_label_too_long_or_too_small():
    p = golden.plan_dict("s03")
    card = next(w for w in p["widgets"] if w["ref"] == "widget_label_card")
    card["params"]["text"] = "هذا نص طويل جدا من ست كلمات"
    assert codes(check(p)) & {"WIDGET_PARAMS", "WIDGET_TEXT"}
    p = golden.plan_dict("s03")
    card = next(w for w in p["widgets"] if w["ref"] == "widget_label_card")
    card["scale"] = 30 / 48  # 48 px label rendered at 30 px
    assert "WIDGET_TEXT" in codes(check(p))


def test_speaker_not_on_stage():
    p = golden.plan_dict("s03")
    p["actors"] = [a for a in p["actors"] if a["char_key"] != "karim"]
    p["cues"] = [c for c in p["cues"] if c["target"] != "karim"]
    assert "SPEAKER_OFFSTAGE" in codes(check(p))


def test_enter_cue_before_first_line_is_fine_after_is_not():
    p = golden.plan_dict("s04")
    assert any(c["action"] == "enter" for c in p["cues"]), "golden s04 has mama entering"
    assert check(p, "s04").passed
    enter = next(c for c in p["cues"] if c["action"] == "enter")
    who = enter["target"]
    later = [ln.id for ln in golden.scene("s04").lines if ln.speaker == who][-1]
    enter["at"] = {"line": later, "anchor": "end", "offset_ms": 0}
    assert "SPEAKER_OFFSTAGE" in codes(check(p, "s04"))


def test_tap_target_too_small():
    p = golden.plan_dict("s02")
    binding = p["interaction_bindings"][0]["targets"][0]["element_id"]
    prop = next(x for x in p["props"] if x["id"] == binding)
    prop["scale"] = 0.1
    assert "TAP_TARGET_SMALL" in codes(check(p, "s02"))


def test_tap_reveal_needs_bindings():
    p = golden.plan_dict("s02")
    p["interaction_bindings"] = []
    assert "BINDING" in codes(check(p, "s02"))


@pytest.mark.parametrize(
    "action,params,ok",
    [
        ("show", {}, True),
        ("show", {"to": 1}, False),
        ("fade", {"to": 0.5}, True),
        ("fade", {"to": 2}, False),
        ("move", {"to": [10, -20]}, True),
        ("move", {"to": 10}, False),
        ("highlight", {"pulses": 2}, True),
        ("highlight", {"pulses": 5}, False),
        ("shake", {"amplitude": 12}, True),
        ("shake", {"amplitude": 20}, False),
        ("keyframes", {"frames": [{"o": 0, "x": 0}, {"o": 1, "x": 40}]}, True),
        ("keyframes", {"frames": [{"o": 0.5, "x": 0}, {"o": 1, "x": 40}]}, False),
        ("pose", {"name": "wave"}, True),
        ("pose", {"name": "moonwalk"}, False),
        ("expression", {"name": "happy"}, True),
        ("face", {"dir": "up"}, False),
        ("enter", {"from": "left"}, True),
        ("exit", {"to": "top"}, False),
        ("fade", {"to": 1, "easing": "bounce"}, False),
    ],
)
def test_cue_params(action, params, ok):
    assert (check_cue_params(action, params) is None) == ok


def test_run_retries_with_feedback_then_passes():
    bad = golden.plan_dict("s03")
    bad["actors"][0]["x"] = 1400
    llm = FakeLLM(bad, golden.plan_dict("s03"))
    plan, rep = run(golden.scene("s03"), golden.audio(), golden.cast(), golden.CATALOG, llm)
    assert rep.passed and plan.scene_id == "s03" and len(llm.calls) == 2
    assert "OUT_OF_BOUNDS" in llm.calls[1]["user"] and llm.calls[0]["model_role"] == "large"
    assert llm.calls[0]["temperature"] == 0.3


def test_run_gives_up_after_three_attempts():
    bad = golden.plan_dict("s03")
    bad["cues"][0]["target"] = "ghost"
    with pytest.raises(DirectorError):
        run(golden.scene("s03"), golden.audio(), golden.cast(), golden.CATALOG, FakeLLM(bad))


def test_scene_text_is_wrapped_as_data():
    llm = FakeLLM(golden.plan_dict("s03"))
    run(golden.scene("s03"), golden.audio(), golden.cast(), golden.CATALOG, llm)
    user = llm.calls[0]["user"]
    assert user.startswith("<scene>") and "</scene>" in user and "duration_ms" in user
