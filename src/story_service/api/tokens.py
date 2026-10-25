"""Signed play tokens (Section 3, "Play URL and security headers").

The token is base64url(story_id|version|expires_epoch|hmac), where hmac is the hex HMAC-SHA256 of the first three
fields with PLAY_TOKEN_SECRET. It sits in the URL path, so the bundle's relative audio paths resolve under it."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time


class TokenError(ValueError):
    """The token is malformed, forged or expired."""


def _mac(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(story_id: str, version: int, expires_epoch: int, secret: str) -> str:
    payload = f"{story_id}|{version}|{expires_epoch}"
    raw = f"{payload}|{_mac(payload, secret)}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def read_token(token: str, secret: str, now: float | None = None) -> tuple[str, int]:
    """(story_id, version) of a valid token; TokenError otherwise."""
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        story_id, version, expires, mac = raw.split("|")
        version_n, expires_n = int(version), int(expires)
    except (ValueError, UnicodeDecodeError):
        raise TokenError("malformed play token") from None
    expected = _mac(f"{story_id}|{version}|{expires}", secret)
    if not hmac.compare_digest(mac.encode("utf-8"), expected.encode()):  # bytes: the token may hold any letter
        raise TokenError("invalid play token")
    if expires_n < (time.time() if now is None else now):
        raise TokenError("expired play token")
    return story_id, version_n
