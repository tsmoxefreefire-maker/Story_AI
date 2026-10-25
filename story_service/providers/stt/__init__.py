"""Speech-to-text interface (Section 9.1) and the test double `FakeSTT`.

`transcribe(audio_bytes, language)` returns `{"text", "words": [{"w", "s", "e"}]}` with times in milliseconds.
The extra keyword `reference_text` exists only for the test double (it cannot hear a sine tone): real providers MUST
ignore it, otherwise the round-trip check of S6 would grade the provider's own hint (see docs/DECISIONS.md)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ...pipeline import audio_tools


class STTProvider(ABC):
    name = "base"

    @abstractmethod
    def transcribe(self, audio_bytes: bytes, language: str, *, reference_text: str | None = None) -> dict: ...


class FakeSTT(STTProvider):
    """Returns the words of the given text spaced evenly across the audio duration (Section 13.1).

    FakeSTT()                       echo the reference text
    FakeSTT(text="…")               always "hear" this text (round-trip tests)
    FakeSTT(no_word_times=True)     return the text but no word times (timing falls back to `estimated`)"""

    name = "fake"

    def __init__(self, text: str | None = None, no_word_times: bool = False):
        self.fixed, self.no_times = text, no_word_times
        self.calls = 0

    def transcribe(self, audio_bytes, language, *, reference_text=None):
        self.calls += 1
        text = self.fixed if self.fixed is not None else (reference_text or "")
        words = text.split()
        if self.no_times or not words:
            return {"text": text, "words": []}
        dur = audio_tools.probe(audio_bytes)["duration_ms"]
        step = dur / len(words)
        return {
            "text": text,
            "words": [{"w": w, "s": round(i * step), "e": round((i + 1) * step)} for i, w in enumerate(words)],
        }


def get_stt(name: str, **kwargs) -> STTProvider:
    if name == "fake":
        return FakeSTT()
    if name == "groq":
        from .groq import GroqSTT

        return GroqSTT(**kwargs)
    raise ValueError(f"unknown STT provider {name!r}")
