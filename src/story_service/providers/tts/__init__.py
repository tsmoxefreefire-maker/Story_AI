"""Text-to-speech interface (Section 9.1), the test double `FakeTTS`, and the provider registry.

No code outside `providers/` may depend on a provider's specifics: providers change over time."""

from __future__ import annotations

import hashlib
import io
import math
import struct
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SynthResult:
    bytes: bytes
    format: str  # "wav" or "mp3"
    sample_rate: int


class TTSProvider(ABC):
    name: str = "base"
    supported_languages: tuple[str, ...] = ()
    supported_dialects: tuple[str, ...] = ()
    max_chars: int = 1000

    @abstractmethod
    def synthesize(self, text: str, voice_id: str, language: str, dialect: str | None) -> SynthResult: ...


def wav_bytes(samples: list[float], rate: int) -> bytes:
    """16-bit mono WAV from floats in -1..1."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, s)) * 32000)) for s in samples))
    return buf.getvalue()


class FakeTTS(TTSProvider):
    """A WAV sine tone of 0.4 s per word (Section 13.1). Each word is a soft hum that rises and falls like a spoken
    syllable (a smooth envelope and a small pitch glide, no harmonics), so a demo sounds like a quiet murmur rather
    than beeps; the pitch depends on the voice, so turns of speech are still told apart. `silent=True` returns
    zeros (for the not-silent check)."""

    name = "fake"
    supported_languages = ("ar", "en")
    supported_dialects = ("gulf", "msa")
    max_chars = 400

    def __init__(self, rate: int = 24000, silent: bool = False, seconds_per_word: float = 0.4):
        self.rate, self.silent, self.spw = rate, silent, seconds_per_word
        self.calls: list[tuple[str, str]] = []

    def synthesize(self, text, voice_id, language, dialect):
        self.calls.append((text, voice_id))
        words = [w for w in text.split() if w]
        base = 120 + int(hashlib.sha256(voice_id.encode()).hexdigest()[:4], 16) % 110  # 120-230 Hz
        n_word = int(self.spw * self.rate)
        voiced = int(n_word * 0.78)
        out: list[float] = []
        phase = 0.0
        for i, word in enumerate(words):
            start_f = base * (1 + 0.05 * math.sin(i * 1.3 + len(word)))
            for k in range(n_word):
                if self.silent or k >= voiced:
                    out.append(0.0)
                    continue
                x = k / voiced  # 0..1 across the voiced part of the word
                f = start_f * (1.04 - 0.08 * x)  # a small falling glide, like the end of a syllable
                phase += 2 * math.pi * f / self.rate
                env = math.sin(math.pi * x) ** 2  # smooth in and out: no clicks
                out.append(0.22 * env * math.sin(phase))
        return SynthResult(bytes=wav_bytes(out, self.rate), format="wav", sample_rate=self.rate)


def get_tts(name: str, **kwargs) -> TTSProvider:
    """Provider registry: `TTS_PROVIDER` picks the class."""
    if name == "fake":
        return FakeTTS()
    if name == "groq":
        from .groq import GroqTTS

        return GroqTTS(**kwargs)
    raise ValueError(f"unknown TTS provider {name!r}")
