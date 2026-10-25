"""The FastAPI application of module 6 (Section 3).

    python scripts/run_api.py            # then open http://localhost:8000/docs

`create_app()` wires the settings, the service and the routes; every error leaves as {"error": {code, message,
details}}. What the API does is in service.py; the routes are in routes_stories.py and routes_play.py."""

from __future__ import annotations

import logging
import shutil

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import routes_play, routes_stories
from .errors import ApiError
from .limits import MAX_BODY_BYTES, BodyLimit, RateLimiter
from .service import StoryService
from .settings import ApiSettings

log = logging.getLogger("story_service.api")


def create_app(settings: ApiSettings | None = None, service: StoryService | None = None) -> FastAPI:
    service = service or StoryService(settings or ApiSettings.from_env())
    app = FastAPI(
        title="story-service · module 6",
        version="0.1.0",
        description="Audio, visuals, assembly, validation, publish and review of a lesson story (S6 to S12).",
    )
    app.state.service = service
    app.state.limiter = RateLimiter(service.settings.rate_limit_rpm)
    app.add_middleware(BodyLimit, max_bytes=MAX_BODY_BYTES)

    @app.exception_handler(ApiError)
    def api_error(_: Request, e: ApiError) -> JSONResponse:
        return JSONResponse(e.body(), status_code=e.status, headers=e.headers or None)

    @app.exception_handler(RequestValidationError)
    def invalid_request(_: Request, e: RequestValidationError) -> JSONResponse:
        err = ApiError(422, "INPUT_INVALID", "the request is not valid", {"errors": jsonable_encoder(e.errors())})
        return JSONResponse(err.body(), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    def http_error(_: Request, e: StarletteHTTPException) -> JSONResponse:
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(e.status_code, f"HTTP_{e.status_code}")
        body = ApiError(e.status_code, code, str(e.detail)).body()
        return JSONResponse(body, status_code=e.status_code, headers=getattr(e, "headers", None))  # e.g. Allow on 405

    @app.exception_handler(Exception)
    def unexpected(_: Request, e: Exception) -> JSONResponse:
        """A defect of ours: the same error shape, no details (the traceback goes to the server log only)."""
        log.exception("unexpected error")
        return JSONResponse(ApiError(500, "INTERNAL", "unexpected server error").body(), status_code=500)

    @app.get("/v1/health", tags=["health"])
    def health() -> dict:
        """Liveness and readiness: the blob store answers, and the tools the build needs are installed."""
        try:
            service.blob.list("health-check/")
            blob_ok = True
        except Exception:
            blob_ok = False
        checks = {"blob_store": blob_ok, "version_store": "memory", "ffmpeg": shutil.which("ffmpeg") is not None}
        return {"status": "ok" if blob_ok and checks["ffmpeg"] else "degraded", "checks": checks}

    app.include_router(routes_stories.router)
    app.include_router(routes_play.router)
    return app
