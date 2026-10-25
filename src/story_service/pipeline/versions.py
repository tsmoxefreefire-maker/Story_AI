"""Story versions, idea memory, review comments and webhook deliveries (Section 4) as module 6 needs them.

The real tables (Postgres via SQLAlchemy, Alembic migrations) belong to the shared milestone M0. S12, the revision
flow and approve/rollback only talk to `VersionStore`; `MemoryVersionStore` implements it for tests and local runs,
and M0 implements the same methods over the database."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Literal, Protocol

VersionStatus = Literal["queued", "running", "published", "needs_review", "failed", "superseded"]
Trigger = Literal["create", "regenerate", "revision"]
CommentStatus = Literal["open", "batched", "applied", "rejected"]


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class Story:
    id: str
    external_lesson_id: str
    external_topic_id: str
    country: str
    language: str
    age_group: str
    subject: str
    external_book_id: str | None = None
    callback_url: str | None = None
    published_version: int | None = None

    @property
    def external(self) -> dict:
        return {
            "lesson_id": self.external_lesson_id,
            "topic_id": self.external_topic_id,
            "book_id": self.external_book_id,
        }


@dataclass
class StoryVersion:
    story_id: str
    version: int
    status: VersionStatus = "queued"
    trigger: Trigger = "create"
    parent_version: int | None = None
    issues: list[dict] = field(default_factory=list)  # Issue.model_dump() of every unresolved issue
    idea: dict | None = None  # the selected idea of idea.json (one_liner, …)
    result: dict = field(default_factory=dict)  # meta.json fields, warnings, coverage
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None

    def blocking(self, severities=("blocker", "major")) -> list[dict]:
        return [i for i in self.issues if i.get("severity") in severities]


@dataclass
class IdeaMemory:
    story_id: str
    country: str
    language: str
    age_group: str
    subject: str
    one_liner: str
    idea_json: dict
    id: str = field(default_factory=new_id)
    created_at: dt.datetime = field(default_factory=utcnow)


@dataclass
class Comment:
    story_id: str
    version: int
    body: str
    scene_id: str | None = None
    t_ms: int | None = None
    author: str | None = None
    status: CommentStatus = "open"
    resulting_version: int | None = None
    reason: str | None = None  # why a comment was rejected
    id: str = field(default_factory=new_id)
    created_at: dt.datetime = field(default_factory=utcnow)


@dataclass
class WebhookDelivery:
    story_id: str
    version: int
    event: str
    payload: dict
    attempts: int = 0
    last_http_status: int | None = None
    next_attempt_at: dt.datetime = field(default_factory=utcnow)
    delivered_at: dt.datetime | None = None
    dead: bool = False
    id: str = field(default_factory=new_id)


class VersionStore(Protocol):
    def get_story(self, story_id: str) -> Story: ...
    def save_story(self, story: Story) -> None: ...
    def get_version(self, story_id: str, version: int) -> StoryVersion: ...
    def save_version(self, v: StoryVersion) -> None: ...
    def versions(self, story_id: str) -> list[StoryVersion]: ...
    def add_idea(self, idea: IdeaMemory) -> None: ...
    def ideas(self, *, country: str, language: str, age_group: str, subject: str | None = None) -> list[IdeaMemory]: ...
    def add_comment(self, c: Comment) -> None: ...
    def save_comment(self, c: Comment) -> None: ...
    def comments(self, story_id: str, status: str | None = None) -> list[Comment]: ...
    def add_delivery(self, d: WebhookDelivery) -> None: ...
    def save_delivery(self, d: WebhookDelivery) -> None: ...
    def deliveries(self, story_id: str | None = None) -> list[WebhookDelivery]: ...
    def revision_due(self, story_id: str) -> dt.datetime | None: ...
    def set_revision_due(self, story_id: str, when: dt.datetime | None) -> None: ...


class MemoryVersionStore:
    """`VersionStore` in memory (tests and local runs)."""

    def __init__(self):
        self.stories: dict[str, Story] = {}
        self._versions: dict[tuple[str, int], StoryVersion] = {}
        self._ideas: list[IdeaMemory] = []
        self._comments: dict[str, Comment] = {}
        self._deliveries: dict[str, WebhookDelivery] = {}
        self._revision_due: dict[str, dt.datetime] = {}

    def get_story(self, story_id):
        return self.stories[story_id]

    def save_story(self, story):
        self.stories[story.id] = story

    def get_version(self, story_id, version):
        return self._versions[(story_id, version)]

    def save_version(self, v):
        self._versions[(v.story_id, v.version)] = v

    def versions(self, story_id):
        return sorted((v for (s, _), v in self._versions.items() if s == story_id), key=lambda v: v.version)

    def add_idea(self, idea):
        self._ideas.append(idea)

    def ideas(self, *, country, language, age_group, subject=None):
        return [
            i
            for i in self._ideas
            if (i.country, i.language, i.age_group) == (country, language, age_group)
            and (subject is None or i.subject == subject)
        ]

    def add_comment(self, c):
        self._comments[c.id] = c

    def save_comment(self, c):
        self._comments[c.id] = c

    def comments(self, story_id, status=None):
        return sorted(
            (c for c in self._comments.values() if c.story_id == story_id and (status is None or c.status == status)),
            key=lambda c: c.created_at,
        )

    def add_delivery(self, d):
        self._deliveries[d.id] = d

    def save_delivery(self, d):
        self._deliveries[d.id] = d

    def deliveries(self, story_id=None):
        return [d for d in self._deliveries.values() if story_id is None or d.story_id == story_id]

    def revision_due(self, story_id):
        return self._revision_due.get(story_id)

    def set_revision_due(self, story_id, when):
        if when is None:
            self._revision_due.pop(story_id, None)
        else:
            self._revision_due[story_id] = when


# ----------------------------------------------------------------------------------------------- shared rules
class StoryBusy(RuntimeError):
    """A version of this story is queued or running (HTTP 409 STORY_BUSY)."""

    code = "STORY_BUSY"


def ensure_not_busy(store: VersionStore, story_id: str) -> None:
    if any(v.status in ("queued", "running") for v in store.versions(story_id)):
        raise StoryBusy(f"story {story_id} has a version queued or running")


def next_version(store: VersionStore, story_id: str, trigger: Trigger, parent: int | None = None) -> StoryVersion:
    """Create the story's next version (queued). Refused while another version is queued or running."""
    ensure_not_busy(store, story_id)
    n = max((v.version for v in store.versions(story_id)), default=0) + 1
    v = StoryVersion(story_id=story_id, version=n, status="queued", trigger=trigger, parent_version=parent)
    store.save_version(v)
    return v
