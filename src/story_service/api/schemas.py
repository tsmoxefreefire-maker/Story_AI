"""Request bodies of the API (Section 3), as Pydantic models: FastAPI validates them and documents them in /docs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .settings import MAX_TTL_SEC


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class External(_Body):
    lesson_id: str = Field(min_length=1, max_length=128)
    topic_id: str = Field(min_length=1, max_length=128)
    book_id: str | None = Field(default=None, max_length=128)


class Topic(_Body):
    title: str = Field(min_length=1, max_length=200)
    text: str | None = Field(default=None, min_length=200, max_length=20000)


class VoiceOverrides(_Body):
    narrator: str = Field(min_length=1, max_length=128)


class Metadata(_Body):
    country: str = Field(pattern=r"^[A-Z]{2}$")
    language: Literal["ar", "en"] = "ar"
    dialect: Literal["msa", "egyptian", "gulf", "levantine", "maghrebi"] | None = None
    age_group: Literal["6-8", "9-11", "12-14", "15-18"]
    grade: str = Field(min_length=1, max_length=16)
    subject: str = Field(min_length=1, max_length=64)
    target_duration_sec: int | None = Field(default=None, ge=60, le=420)
    voice_overrides: VoiceOverrides | None = None

    @model_validator(mode="after")
    def _dialect_only_for_arabic(self):
        if self.dialect is not None and self.language != "ar":
            raise ValueError("dialect is only for language ar")
        return self


class CreateStoryRequest(_Body):
    """POST /v1/stories: a lesson topic in. Running it needs modules 1 to 5 (S0 to S5)."""

    external: External
    lesson_text: str = Field(min_length=200, max_length=60000)
    topic: Topic
    metadata: Metadata
    callback_url: str | None = Field(default=None, pattern=r"^https://")
    force: bool = False


class DevStoryRequest(_Body):
    """POST /v1/dev/stories: build a story folder (what modules 1 to 5 would hand to module 6)."""

    folder: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$", examples=["plants"])
    external: External | None = None
    callback_url: str | None = Field(default=None, pattern=r"^https://")
    force: bool = False


class StoryJson(_Body):
    """A story as JSON: what modules 1 to 5 hand to module 6. `plans` (scene id -> scene plan) is optional when an LLM
    is connected for the director (S7); the deeper checks are those of a story folder."""

    intake: dict
    script: dict
    cast: dict
    plans: dict[str, dict] | list[dict] | None = None


class DevJsonRequest(_Body):
    """POST /v1/dev/stories/json: build a story pasted as JSON."""

    story: StoryJson
    external: External | None = None
    callback_url: str | None = Field(default=None, pattern=r"^https://")
    force: bool = False


class AccessLinkRequest(_Body):
    ttl_seconds: int | None = Field(default=None, ge=60, le=MAX_TTL_SEC)


class CommentRequest(_Body):
    body: str = Field(min_length=1, max_length=1000)
    scene_id: str | None = Field(default=None, pattern=r"^s\d{2}$")
    t_ms: int | None = Field(default=None, ge=0)
    author: str | None = Field(default=None, max_length=128)


class RollbackRequest(_Body):
    to_version: int = Field(ge=1)


class RegenerateRequest(_Body):
    reason: str | None = Field(default=None, max_length=500)
