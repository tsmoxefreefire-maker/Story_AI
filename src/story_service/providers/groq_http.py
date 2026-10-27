"""Calls to Groq that live with its rate limits (TTS and STT).

Groq's free plan is small (docs, "Rate Limits", checked 2026-10-27): the Orpheus voices allow 10 requests a minute,
100 a day, 1.2K tokens a minute and 3.6K a day; Whisper 20 requests a minute. A story is one request per line, so the
per-minute limit is met within seconds. Section 12.2 retries 5 times over about 30 s, which a one-minute window
outlasts, so these calls are a little more patient (اجتهاد, inside the spirit of 12.2):

- 429, 5xx, timeouts: wait the `Retry-After` Groq sends (else 2, 4, 8, 16, 32, 60 s) and try again, up to 8 tries.
- a `Retry-After` longer than 2 minutes is a daily limit: stop at once and say so, with Groq's own reason.
- `GROQ_MIN_INTERVAL_SEC` (optional, e.g. 7 on the free plan) spaces the requests so the limit is not met at all.

Every error message carries Groq's reason and never the key. Plain Python: `send()` does the HTTP call (and turns a
transport error into TransientError), so this file needs no HTTP library and is tested without one."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable

from ..core.retry import ProviderError, TransientError

TRIES = 8
BACKOFF_S = (2, 4, 8, 16, 32, 60, 60)
MAX_WAIT_S = 120  # a longer Retry-After means a daily limit: no point waiting inside a build

_pace_lock = threading.Lock()
_last_call = [0.0]


def reason(text: str) -> str:
    """Groq's message from an error body, short and on one line."""
    try:
        msg = (json.loads(text).get("error") or {}).get("message") or text
    except (ValueError, AttributeError):
        msg = text
    return re.sub(r"\s+", " ", str(msg or ""))[:300]


def _retry_after(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _pace(sleep: Callable[[float], None], clock: Callable[[], float]) -> None:
    try:
        gap = float(os.environ.get("GROQ_MIN_INTERVAL_SEC") or 0)
    except ValueError:
        gap = 0.0
    if gap <= 0:
        return
    with _pace_lock:  # one request at a time through the gate, at least `gap` seconds apart
        wait = _last_call[0] + gap - clock()
        if wait > 0:
            sleep(wait)
        _last_call[0] = clock()


def request(send: Callable[[], object], what: str, *, sleep=None, clock=None, tries: int = TRIES):
    """The response of `send()` once it is 200. `send` returns an object with `status_code`, `text` and `headers`,
    or raises TransientError for a transport problem."""
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    err = what
    for attempt in range(tries):
        _pace(sleep, clock)
        wait = None
        try:
            r = send()
        except TransientError as e:
            err = f"{what}: {e}"
        else:
            if r.status_code == 200:
                return r
            err = f"{what} {r.status_code}: {reason(r.text)}"
            if r.status_code != 429 and r.status_code < 500:
                raise ProviderError(err)
            wait = _retry_after(r.headers.get("retry-after"))
            if wait is not None and wait > MAX_WAIT_S:
                raise ProviderError(f"{err} (Groq asks to wait about {wait / 60:.0f} min: a daily limit; try later)")
        if attempt == tries - 1:
            raise TransientError(err)
        sleep(wait if wait is not None else BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
