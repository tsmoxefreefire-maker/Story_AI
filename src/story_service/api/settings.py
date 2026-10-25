"""The API's settings (Section 11.1). Read from environment variables only; secrets never appear in a repr or a log.

API_KEYS               comma-separated keys accepted in the X-API-Key header (required)
PLAY_TOKEN_SECRET      signs play links, at least 32 characters (required)
PUBLIC_BASE_URL        base of the play links (required; scripts/run_api.py sets http://localhost:<port> locally)
ALLOWED_EMBED_ORIGINS  space-separated origins allowed to embed the player, frame-ancestors (required; run_api.py
                       sets 'self' locally)
PLAY_LINK_TTL_SEC      default lifetime of a play link, default 21600 (6 hours)
RATE_LIMIT_RPM         requests per minute per API key, default 120
BLOB_DIR               where bundles and audio are stored, default ./data/blobs
STORIES_DIR            story folders the dev endpoint can build, default <repo>/stories
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MIN_SECRET = 32
MAX_TTL_SEC = 86400


class SettingsError(ValueError):
    """A required setting is missing or invalid; the message names it."""


def _int(env, name: str, default: int) -> int:
    try:
        return int(env.get(name) or default)
    except ValueError:
        raise SettingsError(f"{name} must be a whole number") from None


@dataclass(frozen=True)
class ApiSettings:
    api_keys: tuple[str, ...] = field(repr=False)
    play_token_secret: str = field(repr=False)
    public_base_url: str = "http://localhost:8000"
    allowed_embed_origins: str = "'self'"
    play_link_ttl_sec: int = 21600
    rate_limit_rpm: int = 120
    blob_dir: str = "./data/blobs"
    stories_dir: str = str(REPO / "stories")

    def __post_init__(self):
        if not self.api_keys or not all(self.api_keys):
            raise SettingsError("API_KEYS must list at least one key (comma-separated)")
        if len(self.play_token_secret or "") < MIN_SECRET:
            raise SettingsError(f"PLAY_TOKEN_SECRET must be at least {MIN_SECRET} characters")
        if not 60 <= self.play_link_ttl_sec <= MAX_TTL_SEC:
            raise SettingsError(f"PLAY_LINK_TTL_SEC must be between 60 and {MAX_TTL_SEC}")
        if self.rate_limit_rpm < 1:
            raise SettingsError("RATE_LIMIT_RPM must be at least 1")

    @classmethod
    def from_env(cls) -> ApiSettings:
        env = os.environ
        for name in ("PUBLIC_BASE_URL", "ALLOWED_EMBED_ORIGINS"):  # required in Section 11.1
            if not env.get(name, "").strip():
                raise SettingsError(f"{name} is required")
        return cls(
            api_keys=tuple(k.strip() for k in env.get("API_KEYS", "").split(",") if k.strip()),
            play_token_secret=env.get("PLAY_TOKEN_SECRET", ""),
            public_base_url=env["PUBLIC_BASE_URL"].strip(),
            allowed_embed_origins=env["ALLOWED_EMBED_ORIGINS"].strip(),
            play_link_ttl_sec=_int(env, "PLAY_LINK_TTL_SEC", 21600),
            rate_limit_rpm=_int(env, "RATE_LIMIT_RPM", 120),
            blob_dir=env.get("BLOB_DIR") or "./data/blobs",
            stories_dir=env.get("STORIES_DIR") or str(REPO / "stories"),
        )
