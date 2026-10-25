"""S12 Publish (Section 5, S12) and the version actions of Section 10.3 (approve, rollback, regenerate).

publish: no unresolved blocker or major -> the version is `published`, the previous published one `superseded`,
`stories.published_version` points at it, the idea goes to idea memory and `story.published` is queued. Otherwise
the version is `needs_review` and `story.needs_review` is queued; the previous published version stays published.
Minor issues never block: they are listed in `result.warnings`."""

from __future__ import annotations

from dataclasses import dataclass

from ...core import webhooks
from ..versions import IdeaMemory, StoryVersion, VersionStore, next_version, utcnow


class VersionActionError(ValueError):
    """An approve/rollback that the rules refuse (HTTP 4xx)."""


@dataclass
class PublishOutcome:
    status: str  # "published" or "needs_review"
    version: StoryVersion
    superseded: int | None = None


def _warnings(v: StoryVersion) -> list[dict]:
    """The stage warnings already on the version (DIALECT_FALLBACK, FONT_FALLBACK…) plus every minor issue."""
    out, seen = [], set()
    minors = [{"code": i.get("code"), "message": i.get("message")} for i in v.issues if i.get("severity") == "minor"]
    for w in list(v.result.get("warnings", [])) + minors:
        key = (w.get("code"), w.get("message"))
        if key not in seen:
            seen.add(key)
            out.append({"code": w.get("code"), "message": w.get("message")})
    return out


def _make_published(store: VersionStore, v: StoryVersion) -> int | None:
    story = store.get_story(v.story_id)
    previous = story.published_version
    if previous is not None and previous != v.version:
        old = store.get_version(v.story_id, previous)
        old.status = "superseded"
        store.save_version(old)
    v.status, v.finished_at = "published", utcnow()
    v.result["warnings"] = _warnings(v)
    story.published_version = v.version
    store.save_version(v)
    store.save_story(story)
    webhooks.enqueue(store, story, v.version, "story.published", "published")
    return previous if previous != v.version else None


def run(store: VersionStore, story_id: str, version: int) -> PublishOutcome:
    """S12 for a finished version."""
    v = store.get_version(story_id, version)
    story = store.get_story(story_id)
    if v.blocking():
        v.status, v.finished_at = "needs_review", utcnow()
        store.save_version(v)
        webhooks.enqueue(store, story, v.version, "story.needs_review", "needs_review")
        return PublishOutcome("needs_review", v)
    superseded = _make_published(store, v)
    _after_publish(store, v)
    return PublishOutcome("published", v, superseded)


def _after_publish(store: VersionStore, v: StoryVersion) -> None:
    """What S12 does besides the status: the idea goes to idea memory (a revision keeps its parent's idea, which is
    already there) and the comments the version answered become `applied`."""
    story = store.get_story(v.story_id)
    one_liner = (v.idea or {}).get("one_liner")
    if one_liner and v.trigger != "revision":
        store.add_idea(
            IdeaMemory(
                story_id=story.id,
                country=story.country,
                language=story.language,
                age_group=story.age_group,
                subject=story.subject,
                one_liner=one_liner,
                idea_json=v.idea,
            )
        )
    wanted = set(v.result.get("comment_ids", []))
    for c in store.comments(v.story_id):
        if c.id in wanted and c.status == "batched":
            c.status, c.resulting_version = "applied", v.version
            store.save_comment(c)


def approve(store: VersionStore, story_id: str, version: int) -> PublishOutcome:
    """Publish a `needs_review` version by hand. Refused while it has an unresolved blocker; majors can be approved."""
    v = store.get_version(story_id, version)
    if v.status != "needs_review":
        raise VersionActionError(f"version {version} is {v.status}, only needs_review can be approved")
    if v.blocking(("blocker",)):
        raise VersionActionError(f"version {version} has unresolved blockers")
    superseded = _make_published(store, v)  # approve runs only S12 (10.3)
    _after_publish(store, v)
    return PublishOutcome("published", v, superseded)


def rollback(store: VersionStore, story_id: str, to_version: int) -> PublishOutcome:
    """Make an earlier `published` or `superseded` version the published one again."""
    target = store.get_version(story_id, to_version)
    if target.status not in ("published", "superseded"):
        raise VersionActionError(f"version {to_version} is {target.status}; only published or superseded can return")
    superseded = _make_published(store, target)
    return PublishOutcome("published", target, superseded)


def regenerate(store: VersionStore, story_id: str, reason: str | None = None) -> StoryVersion:
    """A new version from S0 (`trigger = regenerate`); the orchestrator runs it. Idea memory already holds the
    previous idea, so S3 picks a different one. Refused (STORY_BUSY) while a version is queued or running."""
    v = next_version(store, story_id, "regenerate")
    if reason:
        v.result["regenerate_reason"] = reason
        store.save_version(v)
    return v
