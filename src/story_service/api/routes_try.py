"""The try page route (اجتهاد, not in the plan): GET / (and /try) gives the page of try_page.py, no API key needed to
open it; building a story from it needs the key, like every /v1 route."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .try_page import HEADERS, render

router = APIRouter()


@router.get("/", include_in_schema=False)
@router.get("/try", include_in_schema=False)
def try_page(request: Request) -> HTMLResponse:
    svc = request.app.state.service
    return HTMLResponse(render(svc.settings.stories_dir, svc.folders()), headers=HEADERS)
