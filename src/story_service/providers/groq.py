"""Groq speech-to-text (Whisper). Asks for verbose JSON with word-level timestamps; if the provider returns no word
times, S6 falls back to `timing_source = estimated` (Section 14.2)."""

from __future__ import annotations

from ...core.retry import ProviderError, TransientError
from .. import groq_http
from . import STTProvider

API = "https://api.groq.com/openai/v1/audio/transcriptions"


class GroqSTT(STTProvider):
    name = "groq"

    def __init__(self, api_key: str | None, model: str = "whisper-large-v3", timeout_s: float = 90):
        if not api_key:
            raise ProviderError("GROQ_API_KEY is not set")
        self._key, self.model, self.timeout = api_key, model, timeout_s

    def transcribe(self, audio_bytes, language, *, reference_text=None):  # reference_text is ignored on purpose
        import httpx

        def send():
            try:
                return httpx.post(
                    API,
                    timeout=self.timeout,
                    headers={"Authorization": f"Bearer {self._key}"},
                    files={"file": ("line.mp3", audio_bytes, "audio/mpeg")},
                    data={
                        "model": self.model,
                        "language": language,
                        "response_format": "verbose_json",
                        "timestamp_granularities[]": "word",
                    },
                )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                raise TransientError(type(e).__name__) from None

        r = groq_http.request(send, "groq stt")  # waits out Groq's rate limits; see groq_http
        try:
            body = r.json()
        except ValueError:
            raise ProviderError(f"groq stt returned a body that is not JSON: {r.text[:120]!r}") from None
        words = [
            {"w": w.get("word", "").strip(), "s": round(float(w["start"]) * 1000), "e": round(float(w["end"]) * 1000)}
            for w in body.get("words") or []
            if w.get("word")
        ]
        return {"text": body.get("text", ""), "words": words}
