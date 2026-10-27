"""The /v1/stories routes of Section 3, plus /v1/dev/... to build a ready story folder or a story pasted as JSON
(stand-ins while modules 1 to 5 are not connected; see upstream.py for where they plug in).

Each route only reads the request, calls `StoryService` and shapes the answer; the work itself is in service.py."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from .auth import require_api_key
from .schemas import (
    AccessLinkRequest,
    CommentRequest,
    CreateStoryRequest,
    DevJsonRequest,
    DevStoryRequest,
    RegenerateRequest,
    RollbackRequest,
)
from .service import StoryService

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


def service(request: Request) -> StoryService:
    return request.app.state.service


Svc = Annotated[StoryService, Depends(service)]  # the one StoryService of the app, handed to each route


IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)]


def _accepted(svc: StoryService, tasks: BackgroundTasks, story_id: str, version: int, created: bool) -> JSONResponse:
    """202 and the build in the background for a new story; 200 and the existing story for the same request again."""
    if created:
        tasks.add_task(svc.run_version, story_id, version)
    status = svc.view(story_id)["status"]
    body = {"story_id": story_id, "version": version, "status": status}
    return JSONResponse(body, status_code=202 if created else 200)


@router.post("/stories", status_code=202, tags=["stories"])
def create_story(
    body: CreateStoryRequest, tasks: BackgroundTasks, svc: Svc, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    """Create a story from a lesson topic: modules 1 to 5 (S0 to S5), then module 6. Until modules 1 to 5 are
    connected (upstream.py) this answers 501 NOT_CONNECTED; use POST /v1/dev/stories or /v1/dev/stories/json."""
    return _accepted(svc, tasks, *svc.create_from_request(body.model_dump(), idempotency_key))


@router.post("/dev/stories", status_code=202, tags=["dev"])
def create_dev_story(
    body: DevStoryRequest, tasks: BackgroundTasks, svc: Svc, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    """Build one of the story folders (stories/plants, stories/percent, …) through module 6 and publish it.
    The same request again returns the same story with 200; an Idempotency-Key header replaces that check."""
    external = body.external.model_dump() if body.external else None
    out = svc.create_from_folder(body.folder, external, body.callback_url, body.force, idempotency_key)
    return _accepted(svc, tasks, *out)


@router.post("/dev/stories/json", status_code=202, tags=["dev"])
def create_dev_story_from_json(
    body: DevJsonRequest, tasks: BackgroundTasks, svc: Svc, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    """Build a story pasted as JSON: {intake, script, cast, plans?}, the shape modules 1 to 5 hand to module 6.
    Scenes without a plan are planned by the director (S7) when an LLM is connected; otherwise 422 at once."""
    story = body.story.model_dump(exclude_none=True)
    external = body.external.model_dump() if body.external else None
    out = svc.create_from_json(story, external, body.callback_url, body.force, idempotency_key)
    return _accepted(svc, tasks, *out)


@router.get("/dev/folders/{folder}/json", tags=["dev"])
def folder_as_json(folder: str, svc: Svc) -> dict:
    """A ready story folder as one JSON object: an example to paste into POST /v1/dev/stories/json."""
    return svc.example(folder)


@router.get("/dev/folders", tags=["dev"])
def list_folders(svc: Svc) -> dict:
    """The story folders the dev endpoint can build."""
    return {"folders": svc.folders()}


@router.get("/stories", tags=["stories"])
def list_stories(
    svc: Svc,
    lesson_id: str | None = None,
    topic_id: str | None = None,
    book_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, pattern=r"^\d+$"),
) -> dict:
    return svc.list_stories(
        lesson_id=lesson_id, topic_id=topic_id, book_id=book_id, status=status, limit=limit, cursor=cursor
    )


@router.get("/stories/{story_id}", tags=["stories"])
def get_story(story_id: str, svc: Svc) -> dict:
    """Status, progress and result of a story."""
    return svc.view(story_id)


@router.post("/stories/{story_id}/access-link", tags=["play"])
def access_link(story_id: str, svc: Svc, body: AccessLinkRequest | None = None) -> dict:
    """A signed play URL for the published version (default lifetime PLAY_LINK_TTL_SEC, at most 24 hours)."""
    return svc.access_link(story_id, body.ttl_seconds if body else None)


@router.get("/stories/{story_id}/package", tags=["play"])
def package(story_id: str, svc: Svc) -> Response:
    """ZIP of the published version for offline use: index.html, audio/, meta.json."""
    return Response(
        svc.package(story_id),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="story-{story_id}.zip"'},
    )


@router.post("/stories/{story_id}/comments", status_code=202, tags=["review"])
def add_comment(story_id: str, body: CommentRequest, svc: Svc) -> dict:
    """A reviewer comment (Section 10.1). It waits `open`; the revision itself needs the planner LLM (milestone M1)."""
    return svc.comment(story_id, body.body, body.scene_id, body.t_ms, body.author)


@router.post("/stories/{story_id}/versions/{n}/approve", tags=["review"])
def approve(story_id: str, n: int, svc: Svc) -> dict:
    """Publish a needs_review version. Refused while it has an unresolved blocker; majors can be approved."""
    return svc.approve(story_id, n)


@router.post("/stories/{story_id}/rollback", tags=["review"])
def rollback(story_id: str, body: RollbackRequest, svc: Svc) -> dict:
    """Make an earlier published or superseded version the published one again."""
    return svc.rollback(story_id, body.to_version)


@router.post("/stories/{story_id}/regenerate", status_code=202, tags=["review"])
def regenerate(story_id: str, tasks: BackgroundTasks, svc: Svc, body: RegenerateRequest | None = None) -> dict:
    """A new version. 409 STORY_BUSY while another version is queued or running."""
    story_id, version = svc.regenerate(story_id, body.reason if body else None)
    tasks.add_task(svc.run_version, story_id, version)
    return {"story_id": story_id, "version": version, "status": "queued"}
