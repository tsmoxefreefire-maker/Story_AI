"""Settings come from environment variables only, and secrets never show in a repr (logs, error messages)."""

import pytest

from story_service.core.config import Settings


def test_defaults_follow_section_11_1(monkeypatch):
    for name in ("REVISION_BATCH_WINDOW_SEC", "REVISION_REQUIRES_APPROVAL", "WEBHOOK_SECRET", "MAX_SCENE_REPAIRS"):
        monkeypatch.delenv(name, raising=False)
    s = Settings.from_env()
    assert s.revision_batch_window_sec == 30 and s.revision_requires_approval is False
    assert s.max_scene_repairs == 2 and s.webhook_secret is None


def test_values_and_secrets(monkeypatch):
    monkeypatch.setenv("REVISION_REQUIRES_APPROVAL", "true")
    monkeypatch.setenv("WEBHOOK_SECRET", "whsec-test-value-123")
    monkeypatch.setenv("GROQ_API_KEY", "not-a-real-key-456")
    s = Settings.from_env()
    assert s.revision_requires_approval is True and s.webhook_secret == "whsec-test-value-123"
    assert "whsec-test-value-123" not in repr(s) and "not-a-real-key-456" not in repr(s)


def test_bad_boolean_is_an_error(monkeypatch):
    monkeypatch.setenv("REVISION_REQUIRES_APPROVAL", "maybe")
    with pytest.raises(ValueError, match="REVISION_REQUIRES_APPROVAL"):
        Settings.from_env()
