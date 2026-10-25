"""Settings read from environment variables (spec Section 11.1).

Only plain reading and type conversion live here. Secrets (API keys) are read from the environment and never written
anywhere else: not into logs, artifacts, bundles or error messages."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw in (None, "") else int(raw)


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw in (None, "") else float(raw)


def _str(name: str, default: str | None = None) -> str | None:
    raw = os.environ.get(name)
    return default if raw in (None, "") else raw


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    if raw.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if raw.strip().lower() in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class Settings:
    """The settings module 6 (S6 to S12) reads. The shared API/worker settings use the same variables."""

    blob_backend: str = "local"
    blob_dir: str = "./data/blobs"
    tts_provider: str = "groq"
    stt_provider: str = "groq"
    tts_model: str = "canopylabs/orpheus-arabic-saudi"
    tts_model_en: str | None = None
    stt_model: str = "whisper-large-v3"
    groq_api_key: str | None = field(default=None, repr=False)
    llm_model_large: str = "claude-sonnet-5-5"
    llm_model_vision: str = "claude-sonnet-5-5"
    scene_concurrency: int = 4
    tts_concurrency: int = 4
    max_stage_attempts: int = 3
    max_scene_repairs: int = 2
    narrator_gender: str = "f"
    display_digits: str = "western"
    speech_wps_ar: float = 2.4
    speech_wps_en: float = 2.7
    public_base_url: str | None = None
    webhook_secret: str | None = field(default=None, repr=False)  # signs webhook bodies (S12)
    revision_batch_window_sec: int = 30  # Review: comments wait this long before the revision job
    revision_requires_approval: bool = False  # Review: revisions end needs_review instead of publishing

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            blob_backend=_str("BLOB_BACKEND", "local"),
            blob_dir=_str("BLOB_DIR", "./data/blobs"),
            tts_provider=_str("TTS_PROVIDER", "groq"),
            stt_provider=_str("STT_PROVIDER", "groq"),
            tts_model=_str("TTS_MODEL", "canopylabs/orpheus-arabic-saudi"),
            tts_model_en=_str("TTS_MODEL_EN"),
            stt_model=_str("STT_MODEL", "whisper-large-v3"),
            groq_api_key=_str("GROQ_API_KEY"),
            llm_model_large=_str("LLM_MODEL_LARGE", "claude-sonnet-5-5"),
            llm_model_vision=_str("LLM_MODEL_VISION", "claude-sonnet-5-5"),
            scene_concurrency=_int("SCENE_CONCURRENCY", 4),
            tts_concurrency=_int("TTS_CONCURRENCY", 4),
            max_stage_attempts=_int("MAX_STAGE_ATTEMPTS", 3),
            max_scene_repairs=_int("MAX_SCENE_REPAIRS", 2),
            narrator_gender=_str("NARRATOR_GENDER", "f"),
            display_digits=_str("DISPLAY_DIGITS", "western"),
            speech_wps_ar=_float("SPEECH_WPS_AR", 2.4),
            speech_wps_en=_float("SPEECH_WPS_EN", 2.7),
            public_base_url=_str("PUBLIC_BASE_URL"),
            webhook_secret=_str("WEBHOOK_SECRET"),
            revision_batch_window_sec=_int("REVISION_BATCH_WINDOW_SEC", 30),
            revision_requires_approval=_bool("REVISION_REQUIRES_APPROVAL", False),
        )
