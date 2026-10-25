"""Review comments (Section 10.1).

A comment is saved `open` and a revision job is scheduled REVISION_BATCH_WINDOW_SEC (30 s) later unless one is
already scheduled; every comment still open when the job starts is handled by that one revision (`batched`)."""

from __future__ import annotations

import datetime as dt

from ..models import Timeline
from ..versions import Comment, VersionStore, utcnow

BATCH_WINDOW_SEC = 30
MAX_BODY = 1000


class CommentError(ValueError):
    """Invalid comment input (HTTP 422)."""


def scene_at(timeline: Timeline, t_ms: int) -> str | None:
    for s in timeline.scenes:
        if s.start_ms <= t_ms < s.end_ms:
            return s.id
    return None


def add_comment(
    store: VersionStore,
    story_id: str,
    body: str,
    *,
    scene_id: str | None = None,
    t_ms: int | None = None,
    author: str | None = None,
    timeline: Timeline | None = None,
    now: dt.datetime | None = None,
    window_sec: int = BATCH_WINDOW_SEC,
) -> tuple[Comment, dt.datetime]:
    """Save a comment on the story's current version; returns it and when the revision job runs."""
    body = (body or "").strip()
    if not 1 <= len(body) <= MAX_BODY:
        raise CommentError(f"body must be 1 to {MAX_BODY} characters")
    if scene_id is None and t_ms is not None:
        if timeline is None:
            raise CommentError("t_ms needs the version's timeline to find its scene")
        scene_id = scene_at(timeline, t_ms)
        if scene_id is None:
            raise CommentError(f"t_ms {t_ms} is outside the story")
    if scene_id is not None and timeline is not None and scene_id not in {s.id for s in timeline.scenes}:
        raise CommentError(f"unknown scene {scene_id}")
    story = store.get_story(story_id)
    versions = store.versions(story_id)
    version = story.published_version or (versions[-1].version if versions else 1)
    c = Comment(story_id=story_id, version=version, body=body, scene_id=scene_id, t_ms=t_ms, author=author)
    store.add_comment(c)
    now = now or utcnow()
    due = store.revision_due(story_id)
    if due is None:
        due = now + dt.timedelta(seconds=window_sec)
        store.set_revision_due(story_id, due)
    return c, due


def take_open(store: VersionStore, story_id: str) -> list[Comment]:
    """The revision job starts: every open comment becomes `batched` and is handled together."""
    out = store.comments(story_id, "open")
    for c in out:
        c.status = "batched"
        store.save_comment(c)
    store.set_revision_due(story_id, None)
    return out
