"""S11 VLM critic (Section 8.4): a vision model scores each scene from its V1 frames, then the whole story once.

Pass for a scene: every score at least 4 and the mean at least 4.2. The whole-story review scores `continuity`;
below 4 is a major issue pointing at S5 (the cast design). The VLM sees still frames only, so it does not judge
motion or beauty; the automated checks in render.py cover what is broken, blank, overlapping or illegible."""

from __future__ import annotations

import json
from pathlib import Path

from ..pipeline.models import Cast, Script
from ..pipeline.render import escape_for_prompt
from ..providers.vlm import CRITIC_KEYS
from .render import Reports, Sample, frames_for_critic, to_png

PROMPTS = Path(__file__).resolve().parents[1] / "pipeline" / "prompts"
MIN_SCORE, MIN_MEAN, MIN_CONTINUITY = 4.0, 4.2, 4.0
SUSPECTS = {"S5_cast", "S7_director", "S8_prop", "S10_assembler"}
# when the VLM gives a low score but no issue, the likeliest cause of each score
DEFAULT_SUSPECT = {
    "matches_narration": "S7_director",
    "characters_consistent": "S5_cast",
    "composition_clear": "S7_director",
    "age_appropriate": "S7_director",
    "culturally_appropriate": "S5_cast",
    "no_visual_defects": "S8_prop",
}
CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "object",
            "properties": {k: {"type": "number"} for k in CRITIC_KEYS},
            "required": list(CRITIC_KEYS),
        },
        "issues": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["scores", "issues"],
}
CONTINUITY_SCHEMA = {
    "type": "object",
    "properties": {"continuity": {"type": "number"}, "issues": {"type": "array", "items": {"type": "object"}}},
    "required": ["continuity"],
}


def _scene_text(script: Script | None, cast: Cast | None, scene_id: str, age_group: str) -> str:
    if script is None:
        return f"<scene>{json.dumps({'scene_id': scene_id, 'age_group': age_group})}</scene>"
    sc = next(s for s in script.scenes if s.id == scene_id)
    names = {c.key: c.name for c in script.characters}
    looks = []
    for c in cast.characters if cast else []:
        if any(ln.speaker == c.key for ln in sc.lines):
            looks.append(
                f"{names.get(c.key, c.key)}: {c.build}, {c.presentation}, hair {c.hair_style}, "
                f"top {c.top.style} ({c.top.color_token}), bottom {c.bottom.style}"
            )
    data = {
        "scene_id": scene_id,
        "age_group": age_group,
        "summary": sc.summary,
        "lines": [f"{names.get(ln.speaker, ln.speaker)}: {ln.text_display}" for ln in sc.lines],
        "cast": looks,
    }
    return "<scene>" + escape_for_prompt(json.dumps(data, ensure_ascii=False)) + "</scene>"


def critique_scenes(
    rep: Reports,
    samples: list[Sample],
    vlm,
    *,
    script: Script | None = None,
    cast: Cast | None = None,
    age_group: str = "9-11",
) -> None:
    system = (PROMPTS / "s11_vlm_critic.md").read_text(encoding="utf-8")
    for scene_id, r in rep.scenes.items():
        frames = frames_for_critic(samples, scene_id)
        if not frames:
            continue
        out = vlm.complete_json(
            system,
            _scene_text(script, cast, scene_id, age_group),
            CRITIC_SCHEMA,
            "vision",
            0.2,
            1500,
            images=[to_png(f) for f in frames],
        )
        scores = {k: float(out.get("scores", {}).get(k, 0)) for k in CRITIC_KEYS}
        r.scores.update(scores)
        mean = sum(scores.values()) / len(scores)
        low = [k for k, v in scores.items() if v < MIN_SCORE]
        ok = not low and mean >= MIN_MEAN
        issues = [i for i in out.get("issues", []) if isinstance(i, dict)]
        if ok:
            r.add("vlm_critic", True, "major")
        elif issues:
            for n, i in enumerate(issues):
                suspect = (
                    i.get("suspect_stage")
                    if i.get("suspect_stage") in SUSPECTS
                    else DEFAULT_SUSPECT.get(low[0] if low else "", "S7_director")
                )
                sev = i.get("severity") if i.get("severity") in ("blocker", "major", "minor") else "major"
                r.add(
                    f"vlm_critic_{n}",
                    False,
                    sev,
                    str(i.get("message", "the critic found a problem"))[:300],
                    code="VLM_CRITIC",
                    suspect_stage=suspect,
                    fix_hint=str(i.get("fix_hint", ""))[:300],
                )
            if not any(i.get("severity") in ("blocker", "major") for i in issues):
                r.add(
                    "vlm_critic",
                    False,
                    "major",
                    f"scores {scores} (each at least 4, mean at least 4.2)",
                    code="VLM_CRITIC",
                    suspect_stage=DEFAULT_SUSPECT.get(low[0] if low else "", "S7_director"),
                )
        else:
            for k in low or ["matches_narration"]:
                r.add(
                    f"vlm_critic_{k}",
                    False,
                    "major",
                    f"{k} scored {scores[k]:g} (mean {mean:.2f})",
                    code="VLM_CRITIC",
                    suspect_stage=DEFAULT_SUSPECT[k],
                    fix_hint=f"improve {k.replace('_', ' ')}",
                )


def critique_story(rep: Reports, samples: list[Sample], vlm) -> None:
    order = list(dict.fromkeys(s.scene_id for s in sorted(samples, key=lambda s: s.t)))
    frames = [frames_for_critic(samples, sid, 1) for sid in order[:12]]
    images = [to_png(f[0]) for f in frames if f]
    if not images:
        return
    out = vlm.complete_json(
        (PROMPTS / "s11_continuity.md").read_text(encoding="utf-8"),
        "One frame per scene, in order.",
        CONTINUITY_SCHEMA,
        "vision",
        0.2,
        800,
        images=images,
    )
    score = float(out.get("continuity", 0))
    rep.bundle.scores["continuity"] = score
    rep.bundle.add(
        "continuity",
        score >= MIN_CONTINUITY,
        "major",
        f"continuity scored {score:g} (at least 4)",
        code="CONTINUITY",
        suspect_stage="S5_cast",
        fix_hint="keep every character's design identical in every scene",
    )
