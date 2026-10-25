"""Retry rules for transient provider errors (Section 12.2).

Backoff 2, 4, 8, 16, 32 s with ±20% jitter, at most 5 tries.
These retries do not count as stage attempts."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")
BACKOFF_S = (2, 4, 8, 16, 32)


class TransientError(Exception):
    """HTTP 429 or 5xx, a timeout or a connection reset: worth trying again."""


class ProviderError(Exception):
    """A non-retryable provider failure (authentication, no matching voice, unsupported language): `PROVIDER_ERROR`."""


def with_retries(
    fn: Callable[[], T],
    *,
    tries: int = 5,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> T:
    rng = rng or random.Random()
    for attempt in range(tries):
        try:
            return fn()
        except TransientError:
            if attempt == tries - 1:
                raise
            base = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
            sleep(base * rng.uniform(0.8, 1.2))
    raise AssertionError("unreachable")
