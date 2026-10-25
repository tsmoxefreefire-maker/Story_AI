"""Review and revision (Section 10 table): comments, batching, planner, the revision run."""

import datetime as dt

import pytest

import golden
from story_service.pipeline.models import Script
from story_service.pipeline.review import comments as rc
from story_service.pipeline.review import revision
from story_service.pipeline.review.planner import check_plan
from story_service.pipeline.stages import s12_publish
from story_service.pipeline.stages.s11_validate import S11Result
from story_service.pipeline.versions import MemoryVersionStore, Story, StoryBusy, StoryVersion
from story_service.providers.llm import FakeLLM

T0 = dt.datetime(2026, 10, 21, 8, 0, tzinfo=dt.UTC)


def published_store():
    st = MemoryVersionStore()
    st.save_story(
        Story(
            id="st1",
            external_lesson_id="L-1",
            external_topic_id="T-1",
            country="EG",
            language="ar",
            age_group="9-11",
            subject="math",
            callback_url=None,
        )
    )
    st.save_version(StoryVersion(story_id="st1", version=1, status="running", idea={"one_liner": "x"}))
    s12_publish.run(st, "st1", 1)
    return st


def clean(build, touched):
    return S11Result(reports=[])


def comment(st, body, scene_id="s03"):
    c, _ = rc.add_comment(st, "st1", body, scene_id=scene_id, now=T0)
    return c


# ----------------------------------------------------------------------------------------------- comments
def test_comment_is_open_and_schedules_one_revision():
    st = published_store()
    c1, due1 = rc.add_comment(st, "st1", "ممكن تظهر الكسر 1/4 بحجم أكبر؟", scene_id="s03", now=T0)
    _, due2 = rc.add_comment(st, "st1", "تعليق ثان", now=T0 + dt.timedelta(seconds=10))
    _, due3 = rc.add_comment(st, "st1", "تعليق ثالث", now=T0 + dt.timedelta(seconds=20))
    assert c1.status == "open" and c1.version == 1
    assert due1 == due2 == due3 == T0 + dt.timedelta(seconds=30)  # one revision job
    batch = rc.take_open(st, "st1")
    assert len(batch) == 3 and {c.status for c in batch} == {"batched"} and st.revision_due("st1") is None


def test_t_ms_resolves_to_its_scene_and_body_is_checked():
    st = published_store()
    tl = golden.build().timeline
    c, _ = rc.add_comment(st, "st1", "هنا", t_ms=tl.scenes[2].start_ms + 1000, timeline=tl, now=T0)
    assert c.scene_id == "s03"
    with pytest.raises(rc.CommentError):
        rc.add_comment(st, "st1", "", now=T0)
    with pytest.raises(rc.CommentError):
        rc.add_comment(st, "st1", "x" * 1001, now=T0)


# ----------------------------------------------------------------------------------------------- planner
def test_planner_answer_is_checked():
    st = published_store()
    c = comment(st, "أكبر")
    script = golden.script()
    plan, problems = check_plan(
        {"patches": [{"scope": "visual", "scene_id": "s99", "instruction": "x"}], "rejected": []}, [c], script
    )
    assert plan is None and "unknown scene" in problems[0]
    plan, problems = check_plan(
        {"patches": [{"scope": "lines", "scene_id": "s03", "instruction": "x"}], "rejected": []}, [c], script
    )
    assert plan is None and "changed_line_ids" in problems[0]
    plan, _ = check_plan(
        {"patches": [{"scope": "visual", "scene_id": "s03", "instruction": "scale pie1 to 1.1"}], "rejected": []},
        [c],
        script,
    )
    assert plan.patches[0].comment_ids == [c.id]


def test_rejected_comment_makes_no_new_version():
    st = published_store()
    c = comment(st, "احذف مفهوم المقام من القصة")
    planner = FakeLLM({"patches": [], "rejected": [{"comment_id": c.id, "reason": "would remove a must concept"}]})
    out = revision.run(st, "st1", golden.build(), planner_llm=planner, validate=clean)
    assert out.status == "rejected" and out.version is None
    assert st.comments("st1")[0].status == "rejected" and "must" in st.comments("st1")[0].reason
    assert [v.version for v in st.versions("st1")] == [1]


# ----------------------------------------------------------------------------------------------- revision run
def test_single_visual_comment_redirects_only_that_scene():
    st = published_store()
    c = comment(st, "ممكن تظهر الكسر 1/4 بحجم أكبر على الشاشة؟")
    parent = golden.build()
    planner = FakeLLM(
        {
            "patches": [
                {
                    "scope": "visual",
                    "scene_id": "s03",
                    "instruction": "scale widget frac1 to 1.1 and keep it inside the stage",
                    "comment_ids": [c.id],
                }
            ],
            "rejected": [],
        }
    )
    bigger = golden.plan_dict("s03")
    next(w for w in bigger["widgets"] if w["id"] == "frac1")["scale"] = 1.1
    parent.llm = FakeLLM(bigger)  # the director's new plan
    out = revision.run(st, "st1", parent, planner_llm=planner, validate=clean)
    assert out.status == "published" and out.touched == {"s03"}
    v2 = st.get_version("st1", 2)
    assert v2.trigger == "revision" and v2.parent_version == 1 and v2.status == "published"
    assert st.get_version("st1", 1).status == "superseded"
    assert st.comments("st1")[0].status == "applied" and st.comments("st1")[0].resulting_version == 2
    for sid in ("s01", "s02", "s04", "s05"):  # reused as they were
        assert out.build.scenes[sid] == parent.scenes[sid]
    assert out.build.scenes["s03"] != parent.scenes["s03"]
    assert len(parent.llm.calls) == 1 and "frac1" in parent.llm.calls[0]["user"]


def test_lines_patch_without_the_writer_needs_review():
    st = published_store()
    c = comment(st, "غيّر جملة نادية في المشهد الثالث")
    planner = FakeLLM(
        {
            "patches": [
                {
                    "scope": "lines",
                    "scene_id": "s03",
                    "instruction": "simpler words",
                    "changed_line_ids": ["s03_l01"],
                    "comment_ids": [c.id],
                }
            ],
            "rejected": [],
        }
    )
    out = revision.run(st, "st1", golden.build(), planner_llm=planner, validate=clean)
    assert out.status == "needs_review" and st.get_story("st1").published_version == 1


def test_lines_patch_reads_only_the_changed_lines_again():
    st = published_store()
    c = comment(st, "غيّر جملة نادية في المشهد الثالث")
    planner = FakeLLM(
        {
            "patches": [
                {
                    "scope": "lines",
                    "scene_id": "s03",
                    "instruction": "simpler words",
                    "changed_line_ids": ["s03_l01"],
                    "comment_ids": [c.id],
                }
            ],
            "rejected": [],
        }
    )
    parent = golden.build()
    parent.llm = FakeLLM(golden.plan_dict("s03"))

    def edit_lines(script: Script, scene_id, instruction, line_ids) -> Script:
        new = script.model_copy(deep=True)
        line = next(ln for sc in new.scenes for ln in sc.lines if ln.id == "s03_l01")
        line.text_display = "سأقسم البيتزا إلى 4 أجزاء متساوية."
        line.text_spoken = "سأقسم البيتزا إلى أربعة أجزاء متساوية"
        return new

    calls_before = len(parent.tts.calls)
    out = revision.run(st, "st1", parent, planner_llm=planner, edit_lines=edit_lines, validate=clean)
    assert out.status == "published", out.log
    spoken = [t for t, _ in parent.tts.calls[calls_before:]]
    assert spoken == ["سأقسم البيتزا إلى أربعة أجزاء متساوية"]  # S6 for that line only
    assert out.build.audio.line("s03_l01").duration_ms < parent.audio.line("s03_l01").duration_ms


def test_revision_fails_and_v1_stays_published():
    st = published_store()
    c = comment(st, "حرّك كريم")
    planner = FakeLLM(
        {
            "patches": [{"scope": "visual", "scene_id": "s03", "instruction": "move karim", "comment_ids": [c.id]}],
            "rejected": [],
        }
    )
    bad = golden.plan_dict("s03")
    bad["cues"][0]["target"] = "ghost"
    parent = golden.build()
    parent.llm = FakeLLM(bad)  # the director keeps failing
    out = revision.run(st, "st1", parent, planner_llm=planner, validate=clean)
    assert out.status == "needs_review" and st.get_version("st1", 2).status == "needs_review"
    assert st.get_story("st1").published_version == 1 and st.get_version("st1", 1).status == "published"


def test_story_busy_while_a_version_runs():
    st = published_store()
    st.save_version(StoryVersion(story_id="st1", version=2, status="running"))
    c = comment(st, "أكبر")
    planner = FakeLLM(
        {"patches": [{"scope": "visual", "scene_id": "s03", "instruction": "x", "comment_ids": [c.id]}], "rejected": []}
    )
    with pytest.raises(StoryBusy):
        revision.run(st, "st1", golden.build(), planner_llm=planner, validate=clean)
    with pytest.raises(StoryBusy):
        s12_publish.regenerate(st, "st1")


# ----------------------------------------------------------------------------------------------- robustness
def test_busy_story_keeps_its_comments_open():
    st = published_store()
    st.save_version(StoryVersion(story_id="st1", version=2, status="running"))
    c, due = rc.add_comment(st, "st1", "أكبر", scene_id="s03", now=T0)
    with pytest.raises(StoryBusy):
        revision.run(st, "st1", golden.build(), planner_llm=FakeLLM({"patches": [], "rejected": []}), validate=clean)
    assert st.comments("st1")[0].status == "open" and st.revision_due("st1") == due  # the job waits


def test_planner_failure_reopens_and_reschedules():
    st = published_store()
    comment(st, "أكبر")
    bad = {"patches": [{"scope": "visual", "scene_id": "s99", "instruction": "x"}], "rejected": []}
    out = revision.run(st, "st1", golden.build(), planner_llm=FakeLLM(bad), validate=clean)
    assert out.status == "needs_review" and st.comments("st1")[0].status == "open"
    assert st.revision_due("st1") is not None


def test_unexpected_error_fails_the_version_and_frees_the_story():
    st = published_store()
    c = comment(st, "أكبر")
    planner = FakeLLM(
        {"patches": [{"scope": "visual", "scene_id": "s03", "instruction": "x", "comment_ids": [c.id]}], "rejected": []}
    )
    parent = golden.build()
    parent.llm = None  # direct_scene raises ValueError
    out = revision.run(st, "st1", parent, planner_llm=planner, validate=clean)
    assert out.status == "failed" and st.get_version("st1", 2).status == "failed"
    assert any(i["code"] == "INTERNAL" for i in st.get_version("st1", 2).issues)
    assert st.comments("st1")[0].status == "open"
    s12_publish.regenerate(st, "st1")  # not stuck busy


def test_failed_revision_cannot_be_approved():
    st = published_store()
    c = comment(st, "حرّك كريم")
    planner = FakeLLM(
        {
            "patches": [{"scope": "visual", "scene_id": "s03", "instruction": "move karim", "comment_ids": [c.id]}],
            "rejected": [],
        }
    )
    bad = golden.plan_dict("s03")
    bad["cues"][0]["target"] = "ghost"
    parent = golden.build()
    parent.llm = FakeLLM(bad)
    revision.run(st, "st1", parent, planner_llm=planner, validate=clean)
    assert any(
        i["code"] == "VALIDATION_EXHAUSTED" and i["severity"] == "blocker" for i in st.get_version("st1", 2).issues
    )
    with pytest.raises(s12_publish.VersionActionError):
        s12_publish.approve(st, "st1", 2)


def test_lines_patch_rereads_every_line_the_writer_changed():
    st = published_store()
    c = comment(st, "بسّط جمل المشهد الثالث")
    planner = FakeLLM(
        {
            "patches": [
                {
                    "scope": "lines",
                    "scene_id": "s03",
                    "instruction": "simpler words",
                    "changed_line_ids": ["s03_l01"],
                    "comment_ids": [c.id],
                }
            ],
            "rejected": [],
        }
    )
    parent = golden.build()
    parent.llm = FakeLLM(golden.plan_dict("s03"))

    def edit_lines(script, scene_id, instruction, line_ids):
        new = script.model_copy(deep=True)
        for lid, spoken in (
            ("s03_l01", "سأقسم البيتزا إلى أربعة أجزاء متساوية تماما"),
            ("s03_l02", "وإذا أخذت أنا قطعة واحدة كم أكون قد أخذت"),
        ):
            ln = next(x for sc in new.scenes for x in sc.lines if x.id == lid)
            ln.text_spoken, ln.text_display = spoken, spoken
        return new

    before = len(parent.tts.calls)
    out = revision.run(st, "st1", parent, planner_llm=planner, edit_lines=edit_lines, validate=clean)
    assert out.status == "published"
    assert sorted(t for t, _ in parent.tts.calls[before:]) == sorted(
        ["سأقسم البيتزا إلى أربعة أجزاء متساوية تماما", "وإذا أخذت أنا قطعة واحدة كم أكون قد أخذت"]
    )
    assert any(i["code"] == "COVERAGE_UNCHECKED" for i in st.get_version("st1", 2).issues)


def test_approve_does_what_s12_does():
    st = published_store()
    c = comment(st, "أكبر")
    planner = FakeLLM(
        {
            "patches": [{"scope": "visual", "scene_id": "s03", "instruction": "bigger", "comment_ids": [c.id]}],
            "rejected": [],
        }
    )
    parent = golden.build()
    parent.llm = FakeLLM(golden.plan_dict("s03"))
    out = revision.run(st, "st1", parent, planner_llm=planner, validate=clean, requires_approval=True)
    assert out.status == "needs_review" and st.comments("st1")[0].status == "batched"
    s12_publish.approve(st, "st1", 2)
    assert st.comments("st1")[0].status == "applied" and st.comments("st1")[0].resulting_version == 2
    assert st.get_version("st1", 2).result["warnings"]  # stage warnings kept
