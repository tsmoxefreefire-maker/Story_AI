"""API key check (Section 3): every /v1 route except /v1/play/... and /v1/health needs a valid `X-API-Key`.
Then the key's rate limit (RATE_LIMIT_RPM, Section 11.1) is counted.

`APIKeyHeader` is FastAPI's own way to read the header: it also puts an "Authorize" button in /docs, so the key is
typed once there instead of on every request."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader

from .errors import ApiError

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="one of the keys in API_KEYS")


def require_api_key(request: Request, x_api_key: Annotated[str | None, Depends(api_key_header)]) -> None:
    keys = request.app.state.service.settings.api_keys
    # compare_digest on every key: the time taken does not tell how much of a key was right. Bytes, not str:
    # compare_digest refuses a str with non-ASCII letters, and a header can carry any byte.
    given = (x_api_key or "").encode("utf-8", "replace")
    if not given or not any(hmac.compare_digest(given, k.encode("utf-8")) for k in keys):
        raise ApiError(401, "UNAUTHORIZED", "a valid X-API-Key header is required")
    request.app.state.limiter.check(x_api_key)
