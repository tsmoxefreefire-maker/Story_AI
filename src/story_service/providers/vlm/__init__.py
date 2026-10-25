"""Vision model (VLM) used by the S11 critic and the custom-prop check. It goes through the same `LLMClient` with
`model_role="vision"` and images; `FakeVLM` gives fixed scores for tests."""

from __future__ import annotations

from ..llm import LLMClient

CRITIC_KEYS = (
    "matches_narration",
    "characters_consistent",
    "composition_clear",
    "age_appropriate",
    "culturally_appropriate",
    "no_visual_defects",
)


class FakeVLM(LLMClient):
    """FakeVLM(score=5): every critic score and every prop check returns `score`. `scores` overrides single keys,
    `issues` is returned as the critic's issue list; `continuity` is the whole-story score."""

    def __init__(
        self,
        score: float = 5,
        scores: dict | None = None,
        issues: list | None = None,
        continuity: float | None = None,
        prop_scores: list[float] | None = None,
    ):
        self.score, self.scores, self.issues = score, scores or {}, issues or []
        self.continuity = score if continuity is None else continuity
        self.prop_scores = list(prop_scores or [])
        self.calls: list[str] = []

    def complete_json(self, system, user, schema, model_role, temperature, max_tokens, images=None):
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if "recognizable" in props:  # custom prop check
            self.calls.append("prop")
            s = self.prop_scores.pop(0) if self.prop_scores else self.score
            return {"recognizable": s, "reason": "fake"}
        if "score" in props:  # library contact sheet review (Section 6.8)
            self.calls.append("sheet")
            return {"score": self.score, "issues": []}
        if "continuity" in props:  # whole-story review
            self.calls.append("story")
            return {"continuity": self.continuity, "issues": []}
        self.calls.append("scene")
        return {"scores": {k: self.scores.get(k, self.score) for k in CRITIC_KEYS}, "issues": list(self.issues)}
