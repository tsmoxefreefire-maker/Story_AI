"""LLM client interface (Section 11.2) and its test double.

The real adapters (Anthropic, OpenAI-compatible) belong to the shared milestone M1. Module 6 only depends on this
interface, so it runs the same with the real client or with `FakeLLM`."""

from __future__ import annotations

import copy
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class LLMClient(ABC):
    @abstractmethod
    def complete_json(
        self,
        system: str,
        user: str,
        schema: dict,
        model_role: str,
        temperature: float,
        max_tokens: int,
        images: list[bytes] | None = None,
    ) -> dict:
        """Return a parsed dict that matches `schema`. `model_role` is `large`, `small` or `vision`."""


FIXTURES = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "llm"


class FakeLLM(LLMClient):
    """Returns canned, schema-valid JSON from `tests/fixtures/llm/` (or dicts) in the order given, then repeats
    the last one.

    FakeLLM("s7_plan_s03.json")                    one answer for every call
    FakeLLM("a.json", "b.json")                    first call gets a, later calls get b
    FakeLLM({"scene_id": "s03", ...})              a dict instead of a file
    """

    def __init__(self, *answers: str | dict, fixtures_dir: Path | None = None):
        self.dir = fixtures_dir or FIXTURES
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def _load(self, answer: str | dict) -> dict:
        if isinstance(answer, dict):
            return copy.deepcopy(answer)
        return json.loads((self.dir / answer).read_text(encoding="utf-8"))

    def complete_json(self, system, user, schema, model_role, temperature, max_tokens, images=None):
        self.calls.append(
            {
                "system": system,
                "user": user,
                "model_role": model_role,
                "temperature": temperature,
                "images": len(images or []),
            }
        )
        if not self.answers:
            raise AssertionError("FakeLLM has no answer configured")
        return self._load(self.answers[min(len(self.calls) - 1, len(self.answers) - 1)])
