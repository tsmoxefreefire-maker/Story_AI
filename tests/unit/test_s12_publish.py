"""S12 Publish, webhooks and the version actions of Section 10.3 (spec tables)."""

import datetime as dt
import json

import pytest

from story_service.core import webhooks
from story_service.pipeline.stages import s12_publish
from story_service.pipeline.stages.s12_publish import VersionActionError
from story_service.pipeline.versions import MemoryVersionStore, Story, StoryBusy, StoryVersion

IDEA = {"id": "i1", "one_liner": "طفلة تقسم بيتزا بالتساوي بين أربعة أولاد لتنهي خلافهم."}
MAJOR = {"severity": "major", "code": "OUT_OF_BOUNDS", "message": "karim exceeds the stage"}
BLOCKER = {"severity": "blocker", "code": "BLANK_FRAME", "message": "blank"}
MINOR = {"severity": "minor", "code": "DURATION_OFF", "message": "too long by 17%"}
T0 = dt.datetime(2026, 10, 21, 8, 0, tzinfo=dt.UTC)


def store_with(*versions, callback="https://lms.example/hook"):
    st = MemoryVersionStore()
    st.save_story(
        Story(
            id="st1",
            external_lesson_id="L-1",
            external_topic_id="T-1",
            external_book_id="B-1",
            country="EG",
            language="ar",
            age_group="9-11",
            subject="math",
            callback_url=callback,
        )
    )
    for n, (status, issues) in enumerate(versions, start=1):
        st.save_version(StoryVersion(story_id="st1", version=n, status=status, issues=list(issues), idea=IDEA))
    return st


def test_clean_version_is_published():
    st = store_with(("running", []))
    out = s12_publish.run(st, "st1", 1)
    assert out.status == "published" and st.get_story("st1").published_version == 1
    ideas = st.ideas(country="EG", language="ar", age_group="9-11", subject="math")
    assert [i.one_liner for i in ideas] == [IDEA["one_liner"]]
    (d,) = st.deliveries("st1")
    assert d.event == "story.published"
    assert d.payload == {
        "event": "story.published",
        "story_id": "st1",
        "version": 1,
        "external": {"lesson_id": "L-1", "topic_id": "T-1", "book_id": "B-1"},
        "status": "published",
    }


def test_new_version_supersedes_the_published_one():
    st = store_with(("running", []), ("running", []))
    s12_publish.run(st, "st1", 1)
    out = s12_publish.run(st, "st1", 2)
    assert out.superseded == 1 and st.get_version("st1", 1).status == "superseded"
    assert st.get_story("st1").published_version == 2


def test_unresolved_major_needs_review():
    st = store_with(("running", []), ("running", [MAJOR]))
    s12_publish.run(st, "st1", 1)
    out = s12_publish.run(st, "st1", 2)
    assert out.status == "needs_review" and st.get_story("st1").published_version == 1
    assert st.get_version("st1", 1).status == "published"
    assert [d.event for d in st.deliveries("st1")] == ["story.published", "story.needs_review"]
    assert len(st.ideas(country="EG", language="ar", age_group="9-11")) == 1  # no row for v2


def test_minors_only_publish_with_warnings():
    st = store_with(("running", [MINOR]))
    out = s12_publish.run(st, "st1", 1)
    assert out.status == "published" and out.version.result["warnings"] == [
        {"code": "DURATION_OFF", "message": "too long by 17%"}
    ]


def test_callback_unreachable_is_retried_six_times_with_backoff():
    st = store_with(("running", []))
    s12_publish.run(st, "st1", 1)
    st.deliveries("st1")[0].next_attempt_at = T0
    calls, now = [], T0

    def post(url, body, headers):
        calls.append(now)
        return 500

    for _ in range(12):
        webhooks.deliver_due(st, post, "secret", now)
        now += dt.timedelta(hours=1)
    (d,) = st.deliveries("st1")
    assert d.attempts == 7 and d.dead and d.delivered_at is None and d.last_http_status == 500
    assert len(calls) == 7  # first try + 6 retries
    assert st.get_story("st1").published_version == 1  # status unchanged


def test_backoff_doubles_from_30_seconds():
    st = store_with(("running", []))
    s12_publish.run(st, "st1", 1)
    (d,) = st.deliveries("st1")
    d.next_attempt_at = T0
    gaps = []
    now = T0
    for _ in range(4):
        webhooks.deliver_due(st, lambda *a: 503, "secret", now)
        gaps.append((d.next_attempt_at - now).total_seconds())
        now = d.next_attempt_at
    assert gaps == [30, 60, 120, 240]


def test_signature_is_hmac_of_the_raw_body():
    st = store_with(("running", []))
    s12_publish.run(st, "st1", 1)
    sent = {}

    def post(url, body, headers):
        sent.update(url=url, body=body, headers=headers)
        return 200

    webhooks.deliver_due(st, post, "s3cret")
    assert sent["headers"]["X-Signature"] == webhooks.sign(sent["body"], "s3cret")
    assert sent["headers"]["X-Event"] == "story.published"
    assert json.loads(sent["body"])["status"] == "published"
    assert st.deliveries("st1")[0].delivered_at is not None


def test_no_callback_no_delivery():
    st = store_with(("running", []), callback=None)
    s12_publish.run(st, "st1", 1)
    assert st.deliveries("st1") == []


def test_approve_with_majors_publishes():
    st = store_with(("running", [MAJOR]))
    s12_publish.run(st, "st1", 1)
    out = s12_publish.approve(st, "st1", 1)
    assert out.status == "published" and st.get_story("st1").published_version == 1


def test_approve_with_blocker_is_refused():
    st = store_with(("running", [BLOCKER]))
    s12_publish.run(st, "st1", 1)
    with pytest.raises(VersionActionError):
        s12_publish.approve(st, "st1", 1)


def test_rollback_and_rollback_to_failed():
    st = store_with(("running", []), ("running", []), ("failed", []))
    s12_publish.run(st, "st1", 1)
    s12_publish.run(st, "st1", 2)
    s12_publish.rollback(st, "st1", 1)
    assert st.get_version("st1", 1).status == "published" and st.get_version("st1", 2).status == "superseded"
    assert st.deliveries("st1")[-1].event == "story.published"
    with pytest.raises(VersionActionError):
        s12_publish.rollback(st, "st1", 3)
    assert st.get_story("st1").published_version == 1


def test_regenerate_while_busy_is_refused():
    st = store_with(("published", []), ("running", []))
    with pytest.raises(StoryBusy):
        s12_publish.regenerate(st, "st1")
    st.get_version("st1", 2).status = "needs_review"
    v = s12_publish.regenerate(st, "st1", reason="new idea")
    assert v.version == 3 and v.trigger == "regenerate" and v.status == "queued"


def test_stage_warnings_are_kept_next_to_minors():
    st = store_with(("running", [MINOR]))
    st.get_version("st1", 1).result["warnings"] = [{"code": "DIALECT_FALLBACK", "message": "msa"}]
    out = s12_publish.run(st, "st1", 1)
    assert [w["code"] for w in out.version.result["warnings"]] == ["DIALECT_FALLBACK", "DURATION_OFF"]


def test_approve_writes_idea_memory():
    st = store_with(("running", [MAJOR]))
    s12_publish.run(st, "st1", 1)
    assert st.ideas(country="EG", language="ar", age_group="9-11") == []
    s12_publish.approve(st, "st1", 1)
    assert len(st.ideas(country="EG", language="ar", age_group="9-11")) == 1
