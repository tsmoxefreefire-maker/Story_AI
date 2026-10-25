"""The API's plain-Python parts: settings, play tokens, play headers, byte ranges, request limits, and the service flow
(build a story folder -> S12 -> play link -> play files -> package -> comments -> approve/rollback/regenerate).
The FastAPI routes on top are tested in test_api_routes.py."""

import asyncio
import base64
import io
import json
import shutil
import time
import zipfile

import pytest

from story_service.api.errors import ApiError
from story_service.api.headers import parse_range, play_headers
from story_service.api.limits import BodyLimit, RateLimiter
from story_service.api.service import StoryService
from story_service.api.settings import ApiSettings, SettingsError
from story_service.api.tokens import TokenError, make_token, read_token

SECRET = "s" * 40
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def settings(tmp_path, **kw) -> ApiSettings:
    return ApiSettings(api_keys=("k1",), play_token_secret=SECRET, blob_dir=str(tmp_path / "blobs"), **kw)


# ----------------------------------------------------------------------------------------------- settings
def test_settings_need_keys_and_a_long_secret(tmp_path):
    with pytest.raises(SettingsError, match="API_KEYS"):
        ApiSettings(api_keys=(), play_token_secret=SECRET)
    with pytest.raises(SettingsError, match="PLAY_TOKEN_SECRET"):
        ApiSettings(api_keys=("k",), play_token_secret="short")
    s = settings(tmp_path)
    assert "k1" not in repr(s) and SECRET not in repr(s)


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("API_KEYS", "a, b ,")
    monkeypatch.setenv("PLAY_TOKEN_SECRET", SECRET)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("ALLOWED_EMBED_ORIGINS", "https://school.example")
    with pytest.raises(SettingsError, match="PUBLIC_BASE_URL"):  # required in Section 11.1
        ApiSettings.from_env()
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://stories.example")
    monkeypatch.setenv("RATE_LIMIT_RPM", "30")
    s = ApiSettings.from_env()
    assert s.api_keys == ("a", "b") and s.allowed_embed_origins == "https://school.example"
    assert s.rate_limit_rpm == 30
    monkeypatch.setenv("RATE_LIMIT_RPM", "many")
    with pytest.raises(SettingsError, match="RATE_LIMIT_RPM"):
        ApiSettings.from_env()


# ----------------------------------------------------------------------------------------------- tokens
def test_token_round_trip_and_refusals():
    t = make_token("story-1", 2, int(time.time()) + 60, SECRET)
    assert read_token(t, SECRET) == ("story-1", 2)
    with pytest.raises(TokenError, match="expired"):
        read_token(make_token("story-1", 2, int(time.time()) - 1, SECRET), SECRET)
    with pytest.raises(TokenError, match="invalid"):
        read_token(t, "x" * 40)  # another secret
    with pytest.raises(TokenError, match="invalid"):
        read_token(make_token("story-1", 3, int(time.time()) + 60, "x" * 40), SECRET)  # forged
    with pytest.raises(TokenError, match="malformed"):
        read_token("not-a-token", SECRET)
    odd = base64.urlsafe_b64encode("story-1|2|9999999999|é".encode()).decode()  # a non-ASCII signature
    with pytest.raises(TokenError, match="invalid"):
        read_token(odd, SECRET)


# ----------------------------------------------------------------------------------------------- headers
def test_play_headers_are_the_plan_exactly():
    h = play_headers("https://school.example")
    assert h["Content-Security-Policy"] == (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; font-src data:; img-src data:; "
        "media-src 'self'; connect-src 'none'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors https://school.example; sandbox allow-scripts"
    )
    assert h["X-Content-Type-Options"] == "nosniff" and h["Referrer-Policy"] == "no-referrer"
    assert h["Cache-Control"] == "private, max-age=3600"


@pytest.mark.parametrize(
    "header,expected",
    [
        (None, None),
        ("bytes=0-99", (0, 99)),
        ("bytes=100-", (100, 999)),
        ("bytes=-200", (800, 999)),
        ("bytes=900-5000", (900, 999)),
        ("bytes=1000-", "invalid"),
        ("bytes=5-2", "invalid"),
        ("items=0-1", "invalid"),
        ("bytes=-", "invalid"),
    ],
)
def test_parse_range(header, expected):
    assert parse_range(header, 1000) == expected


# ----------------------------------------------------------------------------------------------- limits
def test_rate_limit_per_key_in_a_sliding_minute():
    now = [1000.0]
    rl = RateLimiter(3, clock=lambda: now[0])
    for _ in range(3):
        rl.check("k1")
    with pytest.raises(ApiError) as e:
        rl.check("k1")
    assert e.value.status == 429 and e.value.code == "RATE_LIMITED" and "Retry-After" in e.value.headers
    rl.check("k2")  # another key has its own count
    now[0] += 60
    rl.check("k1")  # a minute later the old requests left the window


def _call(middleware, headers, chunks=(b"",)):
    """Run `middleware` around an app that reads the whole body (as FastAPI does) and answers 200, or 400 when the
    client left. Returns what reached the client."""
    sent, incoming = [], list(chunks)

    async def app(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                await send({"type": "http.response.start", "status": 400, "headers": []})
                await send({"type": "http.response.body", "body": b"left"})
                return
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        body = incoming.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(incoming)}

    async def send(msg):
        sent.append(msg)

    asyncio.run(middleware(app)({"type": "http", "headers": headers}, receive, send))
    return sent


def test_body_limit_refuses_a_big_body_before_reading_it():
    small = _call(lambda app: BodyLimit(app, max_bytes=100), [(b"content-length", b"100")])
    assert small[0]["status"] == 200
    big = _call(lambda app: BodyLimit(app, max_bytes=100), [(b"content-length", b"101")])
    assert big[0]["status"] == 422
    assert json.loads(big[1]["body"])["error"]["code"] == "INPUT_INVALID"


def test_body_limit_counts_a_chunked_body_as_it_arrives():
    ok = _call(lambda app: BodyLimit(app, max_bytes=100), [], [b"x" * 50, b"x" * 50])
    assert [m.get("status") for m in ok if "status" in m] == [200]
    big = _call(lambda app: BodyLimit(app, max_bytes=100), [], [b"x" * 60, b"x" * 60, b"x" * 60])
    assert [m.get("status") for m in big if "status" in m] == [422]  # the app's own answer is dropped
    assert json.loads(big[1]["body"])["error"]["code"] == "INPUT_INVALID" and len(big) == 2


# ----------------------------------------------------------------------------------------------- service
def test_unknown_folder_is_refused(tmp_path):
    svc = StoryService(settings(tmp_path))
    with pytest.raises(ApiError) as e:
        svc.create_from_folder("nope", None, None, False)
    assert e.value.status == 422 and e.value.code == "INPUT_INVALID" and "clock" in e.value.details["folders"]


@needs_ffmpeg
def test_story_flow_from_folder_to_play_and_package(tmp_path):
    svc = StoryService(settings(tmp_path))
    story_id, n, created = svc.create_from_folder("clock", None, None, False)
    assert created and n == 1 and svc.view(story_id)["status"] == "queued"
    with pytest.raises(ApiError) as e:
        svc.access_link(story_id, None)
    assert e.value.status == 404 and e.value.code == "NOT_FOUND"

    assert svc.run_version(story_id, n) == "published"
    view = svc.view(story_id)
    assert view["published_version"] == 1 and view["progress"]["percent"] == 100
    assert view["result"]["title"] == "يزن والحافلة" and view["result"]["scene_count"] == 5
    assert [w["code"] for w in view["result"]["warnings"]][0] == "DIALECT_FALLBACK"
    assert svc.create_from_folder("clock", None, None, False) == (story_id, 1, False)  # idempotent
    other, _, made = svc.create_from_folder("clock", None, None, False, idempotency_key="abc")
    assert made and other != story_id  # an Idempotency-Key replaces the natural key
    assert svc.create_from_folder("clock", None, None, False, idempotency_key="abc")[:2] == (other, 1)

    link = svc.access_link(story_id, 600)
    token = link["url"].split("/v1/play/")[1].split("/")[0]
    assert b"story-data" in svc.play_file(token, "index.html")
    assert svc.play_file(token, "audio/s01_l01.mp3")[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3")
    for bad in ("../meta.json", "audio/../../x.mp3", "meta.json", "audio/a b.mp3"):
        with pytest.raises(ApiError):
            svc.play_file(token, bad)
    with pytest.raises(ApiError) as e:
        svc.play_file(token[:-2] + "xx", "index.html")
    assert e.value.status == 401 and e.value.code == "UNAUTHORIZED"

    names = zipfile.ZipFile(io.BytesIO(svc.package(story_id))).namelist()
    assert {"index.html", "meta.json", "audio/s01_l01.mp3"} <= set(names)

    out = svc.comment(story_id, "كبّر الساعة شوي", None, 20000, "reviewer1")
    assert out["status"] == "open" and svc.store.comments(story_id)[0].scene_id == "s02"
    with pytest.raises(ApiError) as e:
        svc.comment(story_id, "x", None, 10_000_000, None)
    assert e.value.status == 422 and e.value.code == "INPUT_INVALID"

    with pytest.raises(ApiError) as e:
        svc.approve(story_id, 1)  # published, not needs_review
    assert e.value.code == "INVALID_VERSION_STATE"

    _, n2 = svc.regenerate(story_id, "a new idea")
    with pytest.raises(ApiError) as e:
        svc.regenerate(story_id, None)
    assert e.value.code == "STORY_BUSY"
    assert svc.run_version(story_id, n2) == "published"
    assert svc.store.get_version(story_id, 1).status == "superseded"

    svc.rollback(story_id, 1)
    assert svc.view(story_id)["published_version"] == 1
    assert svc.list_stories(status="published")["items"][0]["story_id"] == story_id


@needs_ffmpeg
def test_a_stage_that_gives_up_leaves_the_version_needs_review(tmp_path):
    from story_service.pipeline.models import ValidationReport
    from story_service.pipeline.module6 import StageFailed

    def failing_build(story, **kw):
        rep = ValidationReport(stage="S7", scene_id="s01")
        rep.add("bounds", False, "blocker", "karim is off the stage", code="OUT_OF_BOUNDS", suspect_stage="S7_director")
        raise StageFailed("S7/s01", rep)

    svc = StoryService(settings(tmp_path), builder=failing_build)
    story_id, n, _ = svc.create_from_folder("clock", None, None, False)
    assert svc.run_version(story_id, n) == "needs_review"
    view = svc.view(story_id)
    assert view["status"] == "needs_review" and view["issues"][0]["code"] == "OUT_OF_BOUNDS"
    with pytest.raises(ApiError) as e:
        svc.approve(story_id, n)  # an unresolved blocker
    assert e.value.code == "INVALID_VERSION_STATE"


def test_an_internal_error_fails_the_version_without_leaving_it_running(tmp_path):
    def broken_build(story, **kw):
        raise RuntimeError("boom")

    svc = StoryService(settings(tmp_path), builder=broken_build)
    story_id, n, _ = svc.create_from_folder("clock", None, None, False)
    assert svc.run_version(story_id, n) == "failed"
    assert svc.view(story_id)["issues"][0]["code"] == "INTERNAL"
    _, n2 = svc.regenerate(story_id, None)  # not busy: a new version can start
    assert n2 == 2
