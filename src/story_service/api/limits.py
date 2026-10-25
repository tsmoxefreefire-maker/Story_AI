"""Two request limits of the plan, in plain Python (no FastAPI here, so they are tested without a server).

- `RateLimiter`: RATE_LIMIT_RPM requests per minute per API key (Section 11.1); over it -> 429 RATE_LIMITED.
- `BodyLimit`: request bodies up to 1 MB (Section 11.3); a bigger body -> 422 INPUT_INVALID, before it is held in
  memory. It is ASGI middleware: `app.add_middleware(BodyLimit, max_bytes=MAX_BODY_BYTES)`.

Both keep their state in memory, per process: with several API processes each one counts on its own."""

from __future__ import annotations

import json
import threading
import time
from collections import deque

from .errors import ApiError

MAX_BODY_BYTES = 1_000_000
WINDOW_SEC = 60.0


class RateLimiter:
    """A sliding window of 60 seconds per key: the times of the key's last requests are kept, older ones dropped."""

    def __init__(self, per_minute: int, clock=time.monotonic):
        self.per_minute, self.clock = per_minute, clock
        self._seen: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """Count one request of `key`, or raise 429 RATE_LIMITED when it already made `per_minute` in the window."""
        now = self.clock()
        with self._lock:
            times = self._seen.setdefault(key, deque())
            while times and now - times[0] >= WINDOW_SEC:
                times.popleft()
            if len(times) >= self.per_minute:
                wait = max(1, int(WINDOW_SEC - (now - times[0])) + 1)
                raise ApiError(
                    429,
                    "RATE_LIMITED",
                    f"more than {self.per_minute} requests in a minute for this key",
                    {"retry_after_sec": wait},
                    headers={"Retry-After": str(wait)},
                )
            times.append(now)


class BodyLimit:
    """ASGI middleware: refuse a request body over `max_bytes` with the plan's error body.

    A Content-Length over the limit is refused before anything is read. A body without one (chunked) is counted as
    it arrives: past the limit the refusal is sent, the app is told the client left, and whatever it sends after
    that is dropped. Either way at most `max_bytes` (plus one chunk) is ever held in memory."""

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app, self.max_bytes = app, max_bytes

    async def _refuse(self, send) -> None:
        err = ApiError(422, "INPUT_INVALID", f"the request body is over {self.max_bytes} bytes")
        body = json.dumps(err.body()).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", b"%d" % len(body))]
        await send({"type": "http.response.start", "status": err.status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length" and value.isdigit() and int(value) > self.max_bytes:
                await self._refuse(send)
                return
        state = {"total": 0, "cut": False, "refused": False, "started": False}

        async def counted_receive():
            if state["cut"]:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                state["total"] += len(message.get("body", b""))
                if state["total"] > self.max_bytes:
                    state["cut"] = True
                    if not state["started"]:  # the app has not answered yet: the refusal is the answer
                        state["refused"] = True
                        await self._refuse(send)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            if state["refused"]:
                return  # the refusal already answered this request
            if message["type"] == "http.response.start":
                state["started"] = True
            await send(message)

        await self.app(scope, counted_receive, guarded_send)
