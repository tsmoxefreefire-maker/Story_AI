"""Groq text-to-speech (default provider). The API key comes only from `GROQ_API_KEY` and is never logged.

Facts to confirm against Groq's documentation (Section 14.2): model ids, the voice list, the character limit per
request, rate limits and the response format. The values below are the documented defaults at the time of writing; if
a request is rejected for one of them the provider raises `ProviderError` with the provider's message, never the key."""

from __future__ import annotations

from ...core.retry import ProviderError, TransientError
from .. import groq_http
from . import SynthResult, TTSProvider

API = "https://api.groq.com/openai/v1/audio/speech"


class GroqTTS(TTSProvider):
    name = "groq"
    supported_languages = ("ar",)
    supported_dialects = ("gulf", "msa")  # the default model is Saudi Arabic
    max_chars = 200

    def __init__(
        self,
        api_key: str | None,
        model: str = "canopylabs/orpheus-arabic-saudi",
        model_en: str | None = None,
        timeout_s: float = 60,
    ):
        if not api_key:
            raise ProviderError("GROQ_API_KEY is not set")
        self._key, self.model, self.model_en, self.timeout = api_key, model, model_en, timeout_s
        if model_en:
            self.supported_languages = ("ar", "en")

    def synthesize(self, text, voice_id, language, dialect):
        import httpx  # imported here so the fakes need no HTTP library

        model = self.model if language == "ar" else self.model_en
        if not model:
            raise ProviderError(f"no TTS model configured for language {language!r}")

        def send():
            try:
                return httpx.post(
                    API,
                    timeout=self.timeout,
                    headers={"Authorization": f"Bearer {self._key}"},
                    json={"model": model, "voice": voice_id, "input": text, "response_format": "wav"},
                )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                raise TransientError(type(e).__name__) from None

        r = groq_http.request(send, "groq tts")  # waits out Groq's rate limits; see groq_http
        return SynthResult(bytes=r.content, format="wav", sample_rate=24000)
