"""The FastAPI routes (Section 3) through FastAPI's TestClient: the API key, the error format, the play routes with
their headers and byte ranges, the package, review actions, the rate limit and the body limit.

Needs `fastapi` and `httpx` (pip install -r requirements.txt); without them these tests are skipped.
The plain-Python parts under the routes are tested in test_api_core.py."""

import io
import shutil
import zipfile

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from story_service.api.app import create_app  # noqa: E402
from story_service.api.service import StoryService  # noqa: E402
from story_service.api.settings import ApiSettings  # noqa: E402

SECRET = "s" * 40
KEY = {"X-API-Key": "k1"}
BASE = "http://testserver"
ORIGIN = "https://school.example"
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def settings(tmp_path, **kw) -> ApiSettings:
    return ApiSettings(
        api_keys=("k1",),
        play_token_secret=SECRET,
        public_base_url=BASE,
        allowed_embed_origins=ORIGIN,
        blob_dir=str(tmp_path / "blobs"),
        **kw,
    )


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(settings(tmp_path)))


def error_code(res) -> str:
    body = res.json()
    assert set(body["error"]) == {"code", "message", "details"}  # the plan's error shape, always
    return body["error"]["code"]


# ----------------------------------------------------------------------------------------------- basics
def test_health_and_docs_need_no_key(client):
    res = client.get("/v1/health")
    assert res.status_code == 200 and set(res.json()["checks"]) == {"blob_store", "version_store", "ffmpeg"}
    spec = client.get("/openapi.json").json()
    assert {"/v1/stories", "/v1/stories/{story_id}/access-link", "/v1/play/{token}/index.html"} <= set(spec["paths"])
    assert "APIKeyHeader" in spec["components"]["securitySchemes"]  # the Authorize button in /docs
    assert client.get("/docs").status_code == 200


def test_every_story_route_needs_a_valid_key(client):
    assert client.get("/v1/stories").status_code == 401
    res = client.get("/v1/stories", headers={"X-API-Key": "wrong"})
    assert res.status_code == 401 and error_code(res) == "UNAUTHORIZED"
    res = client.get("/v1/stories", headers={"X-API-Key": "é".encode("latin-1")})  # any byte: 401, not a 500
    assert res.status_code == 401 and error_code(res) == "UNAUTHORIZED"
    assert client.get("/v1/stories", headers=KEY).json() == {"items": [], "next_cursor": None}


def test_errors_use_the_plan_format(client):
    res = client.get("/v1/stories/nope", headers=KEY)
    assert res.status_code == 404 and error_code(res) == "NOT_FOUND"
    res = client.get("/v1/no-such-route", headers=KEY)
    assert res.status_code == 404 and error_code(res) == "NOT_FOUND"
    res = client.post("/v1/dev/stories", headers=KEY, json={"folder": "../etc"})
    assert res.status_code == 422 and error_code(res) == "INPUT_INVALID" and res.json()["error"]["details"]["errors"]
    res = client.post("/v1/dev/stories", headers=KEY, json={"folder": "clock", "extra": 1})
    assert res.status_code == 422 and error_code(res) == "INPUT_INVALID"
    res = client.post("/v1/dev/stories", headers=KEY, json={"folder": "nope"})
    assert res.status_code == 422 and "clock" in res.json()["error"]["details"]["folders"]


def test_an_unexpected_error_keeps_the_error_shape(tmp_path):
    client = TestClient(create_app(settings(tmp_path)), raise_server_exceptions=False)

    def broken(story_id):
        raise ZeroDivisionError("a defect")

    client.app.state.service.view = broken
    res = client.get("/v1/stories/x", headers=KEY)
    assert res.status_code == 500 and error_code(res) == "INTERNAL" and "defect" not in res.text


def test_create_story_is_validated_then_waits_for_modules_1_to_5(client):
    assert error_code(client.post("/v1/stories", headers=KEY, json={})) == "INPUT_INVALID"
    body = {
        "external": {"lesson_id": "L1", "topic_id": "T1"},
        "lesson_text": "نص الدرس " * 40,
        "topic": {"title": "أجزاء النبات"},
        "metadata": {"country": "JO", "age_group": "9-11", "grade": "4", "subject": "science"},
    }
    res = client.post("/v1/stories", headers=KEY, json=body)
    assert res.status_code == 501 and error_code(res) == "NOT_CONNECTED"
    english_with_dialect = body | {"metadata": body["metadata"] | {"language": "en", "dialect": "gulf"}}
    assert error_code(client.post("/v1/stories", headers=KEY, json=english_with_dialect)) == "INPUT_INVALID"


def test_dev_folders(client):
    assert {"clock", "percent", "plants"} <= set(client.get("/v1/dev/folders", headers=KEY).json()["folders"])


# ----------------------------------------------------------------------------------------------- limits
def test_rate_limit_per_key(tmp_path):
    client = TestClient(create_app(settings(tmp_path, rate_limit_rpm=2)))
    assert client.get("/v1/stories", headers=KEY).status_code == 200
    assert client.get("/v1/stories", headers=KEY).status_code == 200
    res = client.get("/v1/stories", headers=KEY)
    assert res.status_code == 429 and error_code(res) == "RATE_LIMITED" and int(res.headers["Retry-After"]) >= 1
    assert client.get("/v1/health").status_code == 200  # health is not counted


def test_body_over_one_megabyte_is_refused(client):
    res = client.post(
        "/v1/stories/x/comments",
        headers=KEY | {"Content-Type": "application/json"},
        content=b'{"body": "' + b"x" * 1_000_001 + b'"}',
    )
    assert res.status_code == 422 and error_code(res) == "INPUT_INVALID"


def test_a_chunked_body_over_one_megabyte_is_refused(client):
    def chunks():
        for _ in range(11):
            yield b"x" * 100_000

    res = client.post("/v1/stories/x/comments", headers=KEY | {"Content-Type": "application/json"}, content=chunks())
    assert res.status_code == 422 and error_code(res) == "INPUT_INVALID"


# ----------------------------------------------------------------------------------------------- background build
def test_a_broken_build_fails_the_version_through_the_route(tmp_path):
    def broken_build(story, **kw):
        raise RuntimeError("boom")

    client = TestClient(create_app(service=StoryService(settings(tmp_path), builder=broken_build)))
    res = client.post("/v1/dev/stories", headers=KEY, json={"folder": "clock"})
    assert res.status_code == 202 and res.json()["status"] == "queued"
    view = client.get(f"/v1/stories/{res.json()['story_id']}", headers=KEY).json()  # the background task ran
    assert view["status"] == "failed" and view["issues"][0]["code"] == "INTERNAL"


def test_regenerate_while_a_version_is_queued_is_busy(client):
    svc = client.app.state.service
    story_id, _, _ = svc.create_from_folder("clock", None, None, False)  # queued, not run
    res = client.post(f"/v1/stories/{story_id}/regenerate", headers=KEY)
    assert res.status_code == 409 and error_code(res) == "STORY_BUSY"


# ----------------------------------------------------------------------------------------------- the whole flow
@needs_ffmpeg
def test_dev_story_to_play_package_and_review(client):
    res = client.post("/v1/dev/stories", headers=KEY, json={"folder": "clock"})
    assert res.status_code == 202
    story_id = res.json()["story_id"]
    view = client.get(f"/v1/stories/{story_id}", headers=KEY).json()
    assert view["status"] == "published" and view["published_version"] == 1
    again = client.post("/v1/dev/stories", headers=KEY, json={"folder": "clock"})
    assert again.status_code == 200 and again.json()["story_id"] == story_id  # same request: same story

    # access link -> play, no API key, the plan's headers exactly
    assert error_code(client.post(f"/v1/stories/{story_id}/access-link", headers=KEY, json={"ttl_seconds": 1})) == (
        "INPUT_INVALID"
    )
    link = client.post(f"/v1/stories/{story_id}/access-link", headers=KEY, json={"ttl_seconds": 600}).json()
    assert set(link) == {"url", "expires_at"}
    index_path = link["url"][len(BASE) :]
    page = client.get(index_path)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert page.headers["content-security-policy"].endswith(f"frame-ancestors {ORIGIN}; sandbox allow-scripts")
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert page.headers["cache-control"] == "private, max-age=3600"

    # audio with byte ranges
    audio_path = index_path.replace("index.html", "audio/s01_l01.mp3")
    full = client.get(audio_path)
    size = len(full.content)
    assert full.status_code == 200 and full.headers["accept-ranges"] == "bytes" and size > 10
    part = client.get(audio_path, headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and part.content == full.content[:10]
    assert part.headers["content-range"] == f"bytes 0-9/{size}"
    out_of_file = client.get(audio_path, headers={"Range": f"bytes={size}-"})
    assert out_of_file.status_code == 416 and out_of_file.headers["content-range"] == f"bytes */{size}"
    assert client.get(index_path.replace("index.html", "audio/..%2Fmeta.json")).status_code == 404

    # a forged token
    token = index_path.split("/")[3]
    forged = client.get(index_path.replace(token, token[:-2] + "xx"))
    assert forged.status_code == 401 and error_code(forged) == "UNAUTHORIZED"

    # package
    pkg = client.get(f"/v1/stories/{story_id}/package", headers=KEY)
    assert pkg.status_code == 200 and pkg.headers["content-type"] == "application/zip"
    assert {"index.html", "meta.json"} <= set(zipfile.ZipFile(io.BytesIO(pkg.content)).namelist())

    # review
    res = client.post(f"/v1/stories/{story_id}/comments", headers=KEY, json={"body": "كبّر الساعة", "t_ms": 20000})
    assert res.status_code == 202 and res.json()["status"] == "open"
    res = client.post(f"/v1/stories/{story_id}/versions/1/approve", headers=KEY)
    assert res.status_code == 409 and error_code(res) == "INVALID_VERSION_STATE"
    res = client.post(f"/v1/stories/{story_id}/regenerate", headers=KEY, json={"reason": "new idea"})
    assert res.status_code == 202 and res.json()["version"] == 2
    assert client.get(f"/v1/stories/{story_id}", headers=KEY).json()["published_version"] == 2
    res = client.post(f"/v1/stories/{story_id}/rollback", headers=KEY, json={"to_version": 1})
    assert res.status_code == 200 and res.json()["published_version"] == 1
    listed = client.get("/v1/stories", headers=KEY, params={"status": "published"}).json()
    assert [s["story_id"] for s in listed["items"]] == [story_id]
