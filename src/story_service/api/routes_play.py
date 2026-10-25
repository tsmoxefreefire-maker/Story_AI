"""The play routes (Section 3): the bundle and its audio, authorized by the signed token in the path (no API key),
with exactly the security headers of the plan, and byte ranges so the player can seek inside a line's audio."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request, Response

from .errors import ApiError
from .headers import parse_range, play_headers

router = APIRouter(prefix="/v1/play", tags=["play"])


@router.get("/{token}/index.html")
def play_index(token: str, request: Request) -> Response:
    svc = request.app.state.service
    return Response(
        svc.play_file(token, "index.html"),
        media_type="text/html; charset=utf-8",
        headers=play_headers(svc.settings.allowed_embed_origins),
    )


@router.get("/{token}/audio/{file}")
def play_audio(
    token: str, file: str, request: Request, range_header: Annotated[str | None, Header(alias="Range")] = None
) -> Response:
    svc = request.app.state.service
    data = svc.play_file(token, f"audio/{file}")
    headers = play_headers(svc.settings.allowed_embed_origins) | {"Accept-Ranges": "bytes"}
    span = parse_range(range_header, len(data))
    if span is None:
        return Response(data, media_type="audio/mpeg", headers=headers)
    if span == "invalid":
        msg = f"the Range header is outside the file ({len(data)} bytes)"
        raise ApiError(416, "INPUT_INVALID", msg, headers=headers | {"Content-Range": f"bytes */{len(data)}"})
    first, last = span
    return Response(
        data[first : last + 1],
        status_code=206,
        media_type="audio/mpeg",
        headers=headers | {"Content-Range": f"bytes {first}-{last}/{len(data)}"},
    )
