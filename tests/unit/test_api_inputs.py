"""Where a story's input comes from, through the service (no FastAPI): a story pasted as JSON (with or without scene
plans), the director's LLM planning the missing scenes, the connection point for modules 1 to 5 (POST /v1/stories),
and how a version ends when a stage, the LLM provider or modules 1 to 5 give up."""

import re
import shutil
import sys

import pytest

from story_service.api.errors import ApiError
from story_service.api.service import StoryService
from story_service.api.settings import ApiSettings
from story_service.api.upstream import UpstreamError, load_upstream
from story_service.core.retry import ProviderError
from story_service.pipeline.models import ValidationReport
from story_service.pipeline.stages.s7_director import DirectorError
from story_service.providers.llm import LLMClient

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def service(tmp_path, **kw) -> StoryService:
    s = ApiSettings(api_keys=("k1",), play_token_secret="s" * 40, blob_dir=str(tmp_path / "blobs"))
    return StoryService(s, **kw)


class PlanningLLM(LLMClient):
    """A stand-in director: answers each scene with that scene's plan, found by the scene id in the prompt."""

    def __init__(self, plans: dict):
        self.plans, self.scenes = plans, []

    def describe(self):
        return "fake director"

    def complete_json(self, system, user, schema, model_role, temperature, max_tokens, images=None):
        if "recognizable" in (schema.get("properties") or {}):
            return {"recognizable": 5, "reason": "fake"}
        scene_id = re.search(r'"scene_id": "(s\d{2})"', user).group(1)
        self.scenes.append(scene_id)
        return self.plans[scene_id]


class FakeModules:
    """Modules 1 to 5 stand-in for the connection point: always hands over the clock story."""

    def __init__(self, story: dict, refuse: str | None = None):
        self.story, self.refuse, self.requests = story, refuse, []

    def run(self, request: dict) -> dict:
        self.requests.append(request)
        if self.refuse:
            raise UpstreamError(self.refuse, "the topic is not in the lesson text")
        return self.story


REQUEST = {
    "external": {"lesson_id": "L-1", "topic_id": "T-1", "book_id": None},
    "lesson_text": "نص " * 100,
    "topic": {"title": "قراءة الساعة", "text": None},
    "metadata": {
        "country": "JO",
        "language": "ar",
        "dialect": None,
        "age_group": "6-8",
        "grade": "1",
        "subject": "math",
    },
    "callback_url": None,
    "force": False,
}


# ----------------------------------------------------------------------------------------------- pasted JSON
def test_example_is_a_ready_folder_as_json(tmp_path):
    ex = service(tmp_path).example("clock")
    assert set(ex) == {"intake", "script", "cast", "plans"} and sorted(ex["plans"]) == [
        "s01",
        "s02",
        "s03",
        "s04",
        "s05",
    ]
    with pytest.raises(ApiError) as e:
        service(tmp_path).example("nope")
    assert e.value.code == "INPUT_INVALID"


@needs_ffmpeg
def test_pasted_story_with_plans_is_built(tmp_path):
    svc = service(tmp_path)
    story = svc.example("clock")
    story_id, n, created = svc.create_from_json(story, None, None, False)
    assert created and svc.create_from_json(story, None, None, False) == (story_id, 1, False)  # same paste: same story
    assert svc.run_version(story_id, n) == "published"
    assert svc.view(story_id)["result"]["title"] == "يزن والحافلة"


def test_pasted_story_without_plans_needs_an_llm(tmp_path):
    svc = service(tmp_path)
    story = svc.example("clock") | {"plans": {}}
    with pytest.raises(ApiError) as e:
        svc.create_from_json(story, None, None, False)
    assert e.value.status == 422 and e.value.details["missing_plans"] == ["s01", "s02", "s03", "s04", "s05"]


@needs_ffmpeg
def test_the_director_llm_plans_the_scenes_without_a_plan(tmp_path):
    plans = service(tmp_path).example("clock")["plans"]
    llm = PlanningLLM(plans)
    svc = service(tmp_path, llm=llm)
    story = svc.example("clock") | {"plans": {"s01": plans["s01"]}}  # only s01 is given
    story_id, n, _ = svc.create_from_json(story, None, None, False)
    assert svc.run_version(story_id, n) == "published"
    assert llm.scenes == ["s02", "s03", "s04", "s05"] and svc.llm_name() == "fake director"


@pytest.mark.parametrize(
    "change,words",
    [
        ({"cast": None}, "no cast"),
        ({"plans": {"../x": {}}}, "scene ids look like s01"),
        ({"script": {"title": "x"}}, "script.json"),
        ("s09", "s09"),
    ],
)
def test_a_wrong_pasted_story_says_what_is_wrong(tmp_path, change, words):
    svc = service(tmp_path)
    story = svc.example("clock")
    if change == "s09":  # a complete plan for a scene the script does not have
        change = {"plans": {"s09": story["plans"]["s01"]}}
    story = story | change
    with pytest.raises(ApiError) as e:
        svc.create_from_json(story, None, None, False)
    assert e.value.status == 422 and words in e.value.message


# ----------------------------------------------------------------------------------------------- modules 1 to 5
def test_post_stories_waits_for_modules_1_to_5(tmp_path):
    with pytest.raises(ApiError) as e:
        service(tmp_path).create_from_request(REQUEST)
    assert e.value.status == 501 and e.value.code == "NOT_CONNECTED"


@needs_ffmpeg
def test_the_connection_point_runs_modules_1_to_5_then_module_6(tmp_path):
    modules = FakeModules(service(tmp_path).example("clock"))
    svc = service(tmp_path, upstream=modules)
    story_id, n, created = svc.create_from_request(REQUEST)
    assert created and svc.view(story_id)["status"] == "queued" and svc.view(story_id)["external"]["lesson_id"] == "L-1"
    assert svc.create_from_request(REQUEST) == (story_id, 1, False)  # Section 3 natural key
    assert svc.create_from_request(REQUEST, idempotency_key="other")[2]  # an Idempotency-Key replaces it
    assert svc.run_version(story_id, n) == "published" and modules.requests[0]["topic"]["title"] == "قراءة الساعة"
    _, n2 = svc.regenerate(story_id, None)
    assert svc.run_version(story_id, n2) == "published" and len(modules.requests) == 2  # full regeneration: S0 again


def test_modules_1_to_5_refusing_fails_the_version_with_their_code(tmp_path):
    svc = service(tmp_path, upstream=FakeModules({}, refuse="TOPIC_NOT_FOUND"))
    story_id, n, _ = svc.create_from_request(REQUEST)
    assert svc.run_version(story_id, n) == "failed"
    issue = svc.view(story_id)["issues"][0]
    assert issue["code"] == "TOPIC_NOT_FOUND" and issue["stage"] == "S0-S5"


def test_load_upstream(tmp_path, monkeypatch):
    assert load_upstream(None) is None
    (tmp_path / "team_modules.py").write_text("class Modules:\n    def run(self, request):\n        return {}\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert callable(load_upstream("team_modules:Modules").run)
    with pytest.raises(ValueError, match="run"):
        load_upstream("json:JSONDecoder")
    with pytest.raises(ValueError, match="package.module:name"):
        load_upstream("team_modules")
    sys.modules.pop("team_modules", None)


# ----------------------------------------------------------------------------------------------- how a version ends
def test_a_director_out_of_attempts_is_needs_review_and_cannot_be_approved(tmp_path):
    def director_gives_up(story, **kw):
        rep = ValidationReport(stage="S7", scene_id="s02")
        rep.add(
            "speaker", False, "major", "yazan speaks while hidden", code="OFFSTAGE_NO_CUE", suspect_stage="S7_director"
        )
        raise DirectorError("S7 s02: no valid plan after 3 attempts", rep)

    svc = service(tmp_path, builder=director_gives_up)
    story_id, n, _ = svc.create_from_folder("clock", None, None, False)
    assert svc.run_version(story_id, n) == "needs_review"
    codes = [i["code"] for i in svc.view(story_id)["issues"]]
    assert codes == ["OFFSTAGE_NO_CUE", "VALIDATION_EXHAUSTED"]
    with pytest.raises(ApiError) as e:
        svc.approve(story_id, n)  # no finished bundle: never approvable
    assert e.value.code == "INVALID_VERSION_STATE"


def test_an_llm_provider_refusal_is_provider_error(tmp_path):
    def bad_key(story, **kw):
        raise ProviderError("openai_compatible 401: invalid api key")

    svc = service(tmp_path, builder=bad_key)
    story_id, n, _ = svc.create_from_folder("clock", None, None, False)
    assert svc.run_version(story_id, n) == "failed" and svc.view(story_id)["issues"][0]["code"] == "PROVIDER_ERROR"


def test_a_paste_never_changes_another_storys_input(tmp_path):
    svc = service(tmp_path)
    a = svc.example("clock")
    story_a, _, _ = svc.create_from_json(a, None, None, False, idempotency_key="same")
    folder_a = svc._inputs[story_a]
    before = (folder_a / "script.json").read_text(encoding="utf-8")
    b = a | {"script": a["script"] | {"title": "DIFFERENT STORY"}}
    assert svc.create_from_json(b, None, None, False, idempotency_key="same")[0] == story_a  # the key decides
    assert (folder_a / "script.json").read_text(encoding="utf-8") == before  # A's input untouched
    other, _, _ = svc.create_from_json(b, None, None, False)
    assert svc._inputs[other] != folder_a


def test_the_key_of_a_plan_names_its_scene(tmp_path):
    svc = service(tmp_path)
    story = svc.example("clock")
    plans = dict(story["plans"])
    plans["s01"] = plans["s01"] | {"scene_id": "s02"}  # pasted under s01 but saying s02
    sid, _, _ = svc.create_from_json(story | {"plans": plans}, None, None, False)
    assert (svc._inputs[sid] / "plans" / "scene_plan_s01.json").exists()
    with pytest.raises(ApiError):
        svc.create_from_json(story | {"plans": {"s01\n": story["plans"]["s01"]}}, None, None, False)


def test_a_provider_busy_after_every_retry_is_provider_error(tmp_path):
    from story_service.core.retry import TransientError

    def busy(story, **kw):
        raise TransientError("openai_compatible 429")

    svc = service(tmp_path, builder=busy)
    story_id, n, _ = svc.create_from_folder("clock", None, None, False)
    assert svc.run_version(story_id, n) == "failed" and svc.view(story_id)["issues"][0]["code"] == "PROVIDER_ERROR"
