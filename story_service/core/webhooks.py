"""Webhooks (00_shared, "Webhook"): signed POSTs to the caller's `callback_url`, retried with backoff.

    POST {callback_url}  body {"event", "story_id", "version", "external", "status"}
    headers X-Signature (hex HMAC-SHA256 of the raw body with WEBHOOK_SECRET), X-Event

Up to 6 retries after the first attempt, with exponential backoff starting at 30 s (30, 60, 120, 240, 480, 960 s).
Every attempt is recorded on the delivery. Polling the GET endpoint stays the source of truth: a failed webhook never
changes a story's status."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
from collections.abc import Callable

from ..pipeline.versions import Story, VersionStore, WebhookDelivery, utcnow

FIRST_BACKOFF_S, MAX_RETRIES = 30, 6
EVENTS = ("story.published", "story.needs_review", "story.failed")


def body_bytes(payload: dict) -> bytes:
    """The exact bytes that are sent and signed (compact JSON, UTF-8)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def enqueue(store: VersionStore, story: Story, version: int, event: str, status: str) -> WebhookDelivery | None:
    """Record a delivery for the story's callback (none when the story has no callback_url)."""
    if event not in EVENTS:
        raise ValueError(f"unknown webhook event {event!r}")
    if not story.callback_url:
        return None
    payload = {"event": event, "story_id": story.id, "version": version, "external": story.external, "status": status}
    d = WebhookDelivery(story_id=story.id, version=version, event=event, payload=payload)
    store.add_delivery(d)
    return d


def deliver_due(
    store: VersionStore,
    post: Callable[[str, bytes, dict], int],
    secret: str,
    now: dt.datetime | None = None,
) -> list[WebhookDelivery]:
    """Try every delivery that is due. `post(url, body, headers) -> HTTP status` (raises or returns >= 400 on
    failure). Returns the deliveries tried."""
    now = now or utcnow()
    tried = []
    for d in store.deliveries():
        if d.delivered_at or d.dead or d.next_attempt_at > now:
            continue
        story = store.get_story(d.story_id)
        body = body_bytes(d.payload)
        headers = {"Content-Type": "application/json", "X-Signature": sign(body, secret), "X-Event": d.event}
        d.attempts += 1
        try:
            d.last_http_status = post(story.callback_url, body, headers)
        except Exception:  # noqa: BLE001  (network errors count as a failed attempt)
            d.last_http_status = None
        if d.last_http_status is not None and 200 <= d.last_http_status < 300:
            d.delivered_at = now
        elif d.attempts > MAX_RETRIES:
            d.dead = True
        else:
            d.next_attempt_at = now + dt.timedelta(seconds=FIRST_BACKOFF_S * 2 ** (d.attempts - 1))
        store.save_delivery(d)
        tried.append(d)
    return tried
