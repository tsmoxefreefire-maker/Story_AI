"""What the API does, in plain Python (no FastAPI here, so it is tested without a server).

The routes in `routes_stories.py` and `routes_play.py` only translate HTTP to these methods. Module 6 runs a version
like this: queued -> running (S6 to S10 build the bundle into the blob store) -> S12 (published or needs_review).
Versions live in a `VersionStore`; this module uses the in-memory one until the database of milestone M0 exists, so
story rows are lost when the server restarts (the bundles stay in BLOB_DIR)."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..devtools import story_folder
from ..pipeline.models import Timeline
from ..pipeline.module6 import StageFailed
from ..pipeline.review import comments as review_comments
from ..pipeline.stages import s12_publish
from ..pipeline.versions import MemoryVersionStore, Story, StoryBusy, VersionStore, new_id, next_version, utcnow
from ..providers.blobstore import BlobStore, LocalBlobStore, version_prefix
from .errors import ApiError, not_found
from .settings import ApiSettings
from .tokens import TokenError, make_token, read_token

log = logging.getLogger("story_service.api")
AUDIO_FILE = r"^[A-Za-z0-9_-]{1,64}\.mp3$"
PLAYABLE = ("published", "superseded")


def build_into_blob(data: story_folder.StoryFolder, *, blob: BlobStore, prefix: str):
    """Module 6 (S6 to S10) on a story folder, then the bundle into the blob store under `{prefix}/bundle/` (the same
    place module6.run puts it), where the play and package routes read it."""
    res = story_folder.build(data)
    for rel, content in res.bundle.files().items():
        blob.put(f"{prefix}/bundle/{rel}", content)
    return res


def _iso(t: datetime | None) -> str | None:
    return t.isoformat().replace("+00:00", "Z") if t else None


@dataclass
class StoryService:
    settings: ApiSettings
    store: VersionStore = field(default_factory=MemoryVersionStore)
    blob: BlobStore | None = None
    builder: object = None  # story_folder.build by default; tests may pass a faster one
    _keys: dict[str, str] = field(default_factory=dict)  # natural key -> story_id
    _folders: dict[str, str] = field(default_factory=dict)  # story_id -> story folder name
    _times: dict[str, list[datetime]] = field(default_factory=dict)  # story_id -> [created_at, updated_at]
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self):
        self.blob = self.blob or LocalBlobStore(self.settings.blob_dir)
        self.builder = self.builder or build_into_blob

    # ------------------------------------------------------------------------------------------------ helpers
    def _story(self, story_id: str) -> Story:
        try:
            return self.store.get_story(story_id)
        except KeyError:
            raise not_found(f"story {story_id}") from None

    def _version(self, story_id: str, n: int):
        try:
            return self.store.get_version(story_id, n)
        except KeyError:
            raise not_found(f"version {n} of story {story_id}") from None

    @staticmethod
    def _status(latest, story: Story) -> str:
        """The story's status is its latest version's; after a rollback the latest is `superseded` while an older
        version is published again, and the story then reads `published` (the five statuses of Section 3)."""
        return "published" if latest.status == "superseded" and story.published_version else latest.status

    def _touch(self, story_id: str) -> None:
        now = utcnow()
        self._times.setdefault(story_id, [now, now])[1] = now

    def folders(self) -> list[str]:
        root = Path(self.settings.stories_dir)
        return sorted(p.name for p in root.iterdir() if (p / "script.json").exists()) if root.is_dir() else []

    # ------------------------------------------------------------------------------------------------ create
    def create_from_folder(
        self,
        folder: str,
        external: dict | None,
        callback_url: str | None,
        force: bool,
        idempotency_key: str | None = None,
    ):
        """A new story from a story folder (stand-in for modules 1 to 5). Returns (story_id, version, created).
        The same request again returns the same story (HTTP 200); an `Idempotency-Key` header replaces that key."""
        if folder not in self.folders():
            raise ApiError(422, "INPUT_INVALID", f"no story folder {folder!r}", {"folders": self.folders()})
        story_data = story_folder.load(Path(self.settings.stories_dir) / folder)
        external = external or {"lesson_id": f"dev-{folder}", "topic_id": story_data.intake.topic_title}
        natural = f"{folder}|{external['lesson_id']}|{external['topic_id']}"
        key = hashlib.sha256((f"idem|{idempotency_key}" if idempotency_key else natural).encode()).hexdigest()
        with self._lock:
            existing = self._keys.get(key)
            if existing and not force:
                latest = self.store.versions(existing)[-1]
                if latest.status != "failed":
                    return existing, latest.version, False
            intake = story_data.intake
            story = Story(
                id=new_id(),
                external_lesson_id=external["lesson_id"],
                external_topic_id=external["topic_id"],
                external_book_id=external.get("book_id"),
                country=intake.country,
                language=intake.language,
                age_group=intake.age_group,
                subject=intake.subject,
                callback_url=callback_url,
            )
            self.store.save_story(story)
            self._keys[key], self._folders[story.id] = story.id, folder
            self._touch(story.id)
            v = next_version(self.store, story.id, "create")
        return story.id, v.version, True

    # ------------------------------------------------------------------------------------------------ run
    def run_version(self, story_id: str, n: int) -> str:
        """Module 6 for one queued version: build (S6 to S10) into the blob store, then S12. Returns the status."""
        v = self._version(story_id, n)
        v.status, v.started_at = "running", utcnow()
        self.store.save_version(v)
        self._touch(story_id)
        prefix = version_prefix(story_id, n)
        try:
            data = story_folder.load(Path(self.settings.stories_dir) / self._folders[story_id])
            res = self.builder(data, blob=self.blob, prefix=prefix)
            self.blob.put(f"{prefix}/timeline.json", res.timeline.model_dump_json().encode())
            meta = res.bundle.meta
            v.result = {
                "title": meta.title,
                "language": meta.language,
                "dialect_applied": data.intake.dialect_applied,
                "age_group": meta.age_group,
                "duration_ms": meta.duration_ms,
                "scene_count": meta.scene_count,
                "interaction_count": meta.interaction_count,
                "coverage": None,  # the Story Critic's coverage (module 4) is not connected yet
                "warnings": [w.model_dump() for w in res.warnings],
                "sizes": meta.sizes.model_dump(),
            }
            v.issues = [i.model_dump() for rep in res.reports.values() for i in rep.issues]
            v.idea = {"one_liner": data.script.logline}  # stand-in for the idea module 3 selects
        except StageFailed as e:  # a stage gave up: needs_review with its issues (Section 10.2, D8)
            v.issues = [i.model_dump() for i in e.report.issues]
        except story_folder.StoryFolderError as e:
            return self._fail(v, "INPUT_INVALID", str(e))
        except Exception as e:  # a defect of ours: the version fails, the story is never left running
            log.exception("version %s/%s failed", story_id, n)
            return self._fail(v, "INTERNAL", f"{type(e).__name__}: {e}")
        self.store.save_version(v)
        try:
            out = s12_publish.run(self.store, story_id, n).status
        except Exception as e:  # never leave the version running: later requests would all get STORY_BUSY
            log.exception("S12 of %s/%s failed", story_id, n)
            return self._fail(v, "INTERNAL", f"{type(e).__name__}: {e}")
        self._touch(story_id)
        return out

    def _fail(self, v, code: str, message: str) -> str:
        v.status, v.finished_at = "failed", utcnow()
        v.issues = [{"severity": "blocker", "code": code, "message": message, "stage": "S6-S10", "scene_id": None}]
        self.store.save_version(v)
        self._touch(v.story_id)
        return "failed"

    # ------------------------------------------------------------------------------------------------ read
    def view(self, story_id: str) -> dict:
        """GET /v1/stories/{story_id} (Section 3, "Get response")."""
        story = self._story(story_id)
        versions = self.store.versions(story_id)
        latest = versions[-1]
        shown = self.store.get_version(story_id, story.published_version) if story.published_version else latest
        progress = {
            "queued": ("S6_audio", 0),
            "running": ("S6_S10", 50),
        }.get(latest.status, ("S12_publish", 100))
        issues = (
            [{k: i.get(k) for k in ("code", "severity", "stage", "scene_id", "message")} for i in latest.blocking()]
            if latest.status in ("needs_review", "failed")
            else []
        )
        created, updated = self._times.get(story_id, [None, None])
        return {
            "story_id": story.id,
            "external": story.external,
            "status": self._status(latest, story),
            "current_version": latest.version,
            "published_version": story.published_version,
            "progress": {"stage": progress[0], "completed_stages": 0 if progress[1] == 0 else 13 * progress[1] // 100,
                         "total_stages": 13, "percent": progress[1]},
            "result": shown.result or None,
            "issues": issues,
            "created_at": _iso(created),
            "updated_at": _iso(updated),
        }  # fmt: skip

    def list_stories(self, *, lesson_id=None, topic_id=None, book_id=None, status=None, limit=50, cursor=None) -> dict:
        with self._lock:  # a create at the same moment must not change the dict while it is read
            ids = sorted(self._times, key=lambda s: self._times[s][0])
        items = []
        for sid in ids:
            story, latest = self.store.get_story(sid), self.store.versions(sid)[-1]
            if lesson_id and story.external_lesson_id != lesson_id or topic_id and story.external_topic_id != topic_id:
                continue
            if book_id and story.external_book_id != book_id or status and self._status(latest, story) != status:
                continue
            items.append(sid)
        start = int(cursor or 0)
        page = items[start : start + limit]
        nxt = str(start + limit) if start + limit < len(items) else None
        return {"items": [self.view(s) for s in page], "next_cursor": nxt}

    # ------------------------------------------------------------------------------------------------ play
    def access_link(self, story_id: str, ttl_seconds: int | None) -> dict:
        story = self._story(story_id)
        if story.published_version is None:
            raise ApiError(404, "NOT_FOUND", "the story has no published version yet")
        expires = int(time.time()) + (ttl_seconds or self.settings.play_link_ttl_sec)
        token = make_token(story.id, story.published_version, expires, self.settings.play_token_secret)
        url = f"{self.settings.public_base_url.rstrip('/')}/v1/play/{token}/index.html"
        return {"url": url, "expires_at": _iso(datetime.fromtimestamp(expires, UTC))}

    def play_file(self, token: str, path: str) -> bytes:
        """The bytes of `index.html` or `audio/<file>.mp3` of the version the token names."""
        try:
            story_id, n = read_token(token, self.settings.play_token_secret)
        except TokenError as e:
            raise ApiError(401, "UNAUTHORIZED", f"play token {e}") from None
        if path != "index.html" and not (path.startswith("audio/") and re.match(AUDIO_FILE, path[6:])):
            raise not_found(path)
        v = self._version(story_id, n)
        if v.status not in PLAYABLE:
            raise ApiError(401, "UNAUTHORIZED", "this version is not playable")
        key = f"{version_prefix(story_id, n)}/bundle/{path}"
        if not self.blob.exists(key):
            raise not_found(path)
        return self.blob.get(key)

    def package(self, story_id: str) -> bytes:
        """ZIP of the published version: index.html, audio/, meta.json."""
        story = self._story(story_id)
        if story.published_version is None:
            raise ApiError(404, "NOT_FOUND", "the story has no published version yet")
        base = f"{version_prefix(story_id, story.published_version)}/bundle/"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for key in self.blob.list(base):
                z.writestr(key[len(base) :], self.blob.get(key))
        return buf.getvalue()

    # ------------------------------------------------------------------------------------------------ review
    def comment(self, story_id: str, body: str, scene_id: str | None, t_ms: int | None, author: str | None) -> dict:
        story = self._story(story_id)
        n = story.published_version or self.store.versions(story_id)[-1].version
        key = f"{version_prefix(story_id, n)}/timeline.json"
        timeline = Timeline.model_validate_json(self.blob.get(key)) if self.blob.exists(key) else None
        try:
            c, _due = review_comments.add_comment(
                self.store, story_id, body, scene_id=scene_id, t_ms=t_ms, author=author, timeline=timeline
            )
        except review_comments.CommentError as e:
            raise ApiError(422, "INPUT_INVALID", str(e)) from None
        self._touch(story_id)
        return {"comment_id": c.id, "status": c.status}

    def approve(self, story_id: str, n: int) -> dict:
        self._story(story_id)
        self._version(story_id, n)
        try:
            s12_publish.approve(self.store, story_id, n)
        except s12_publish.VersionActionError as e:
            raise ApiError(409, "INVALID_VERSION_STATE", str(e)) from None
        self._touch(story_id)
        return self.view(story_id)

    def rollback(self, story_id: str, to_version: int) -> dict:
        self._story(story_id)
        self._version(story_id, to_version)
        try:
            s12_publish.rollback(self.store, story_id, to_version)
        except s12_publish.VersionActionError as e:
            raise ApiError(409, "INVALID_VERSION_STATE", str(e)) from None
        self._touch(story_id)
        return self.view(story_id)

    def regenerate(self, story_id: str, reason: str | None) -> tuple[str, int]:
        """A new version (trigger regenerate). Here it re-runs module 6 on the same story folder."""
        self._story(story_id)
        try:
            with self._lock:
                v = s12_publish.regenerate(self.store, story_id, reason)
        except StoryBusy as e:
            raise ApiError(409, "STORY_BUSY", str(e)) from None
        self._touch(story_id)
        return story_id, v.version
