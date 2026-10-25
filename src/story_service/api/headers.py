"""HTTP details of the play endpoints: the exact security headers of Section 3, and byte ranges for audio seeks."""

from __future__ import annotations

import re

from ..devtools.bundle_server import CSP

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def play_headers(allowed_embed_origins: str, media_src: str = "'self'") -> dict[str, str]:
    """The headers every /v1/play/... response carries (the same CSP as the local bundle server)."""
    return {
        "Content-Security-Policy": CSP.format(media=media_src, ancestors=allowed_embed_origins),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "private, max-age=3600",
    }


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | str:
    """The (first, last) byte of a `Range: bytes=...` header; None for no header (send everything); "invalid" when
    the range cannot be served (answer 416). Only single ranges are supported, which is what audio players send."""
    if not header:
        return None
    m = _RANGE.match(header.strip())
    if not m or (not m[1] and not m[2]) or size <= 0:
        return "invalid"
    if not m[1]:  # bytes=-N: the last N bytes
        n = int(m[2])
        return (max(0, size - n), size - 1) if n > 0 else "invalid"
    first = int(m[1])
    last = min(int(m[2]), size - 1) if m[2] else size - 1
    return (first, last) if first <= last and first < size else "invalid"
