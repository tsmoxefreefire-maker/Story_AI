"""The Revision Planner (Section 10.2, step 2): comments -> patches (or a rejection with a reason).

The LLM sees the comments, the affected scenes' lines and plans, the cast and the concepts. Code checks its answer:
known scopes, existing scene and line ids, every comment either served by a patch or rejected. One retry with the
problems, then the revision stops (`needs_review`)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Cast, ScenePlan, Script
from ..versions import Comment

PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "revision_planner.md"
SCOPES = ("lines", "visual", "audio", "interaction", "cast")
SCHEMA = {
    "type": "object",
    "properties": {
        "patches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scope": {"enum": list(SCOPES)},
                    "scene_id": {"type": ["string", "null"]},
                    "instruction": {"type": "string"},
                    "changed_line_ids": {"type": "array", "items": {"type": "string"}},
                    "comment_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["scope", "instruction"],
            },
        },
        "rejected": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"comment_id": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["comment_id", "reason"],
            },
        },
    },
    "required": ["patches", "rejected"],
}


@dataclass
class Patch:
    scope: str
    scene_id: str | None
    instruction: str
    changed_line_ids: list[str] = field(default_factory=list)
    comment_ids: list[str] = field(default_factory=list)


@dataclass
class RevisionPlan:
    patches: list[Patch]
    rejected: dict[str, str]  # comment id -> reason


class PlannerError(RuntimeError):
    pass


def _story_block(script: Script, plans: dict[str, ScenePlan], cast: Cast, scene_ids: set[str], concepts) -> str:
    scenes = []
    for sc in script.scenes:
        item = {"scene_id": sc.id, "summary": sc.summary}
        if sc.id in scene_ids or not scene_ids:
            item["lines"] = [{"id": ln.id, "speaker": ln.speaker, "text": ln.text_display} for ln in sc.lines]
            item["interaction"] = sc.interaction.model_dump() if sc.interaction else None
            if sc.id in plans:
                p = plans[sc.id]
                item["plan"] = {
                    "actors": [a.model_dump() for a in p.actors],
                    "props": [x.model_dump() for x in p.props],
                    "widgets": [w.model_dump() for w in p.widgets],
                }
        scenes.append(item)
    data = {
        "title": script.title,
        "characters": [c.model_dump() for c in script.characters],
        "cast": [c.model_dump(include={"key", "build", "hair_style", "top", "bottom"}) for c in cast.characters],
        "concepts": concepts or [],
        "scenes": scenes,
    }
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def check_plan(raw: dict, comments: list[Comment], script: Script) -> tuple[RevisionPlan | None, list[str]]:
    problems: list[str] = []
    lines = {sc.id: {ln.id for ln in sc.lines} for sc in script.scenes}
    cids = {c.id for c in comments}
    patches = []
    for p in raw.get("patches") or []:
        scope, sid = p.get("scope"), p.get("scene_id")
        if scope not in SCOPES:
            problems.append(f"unknown scope {scope!r}")
            continue
        if scope != "cast" and sid not in lines:
            problems.append(f"patch for unknown scene {sid!r}")
            continue
        changed = [x for x in p.get("changed_line_ids") or []]
        bad = [x for x in changed if sid in lines and x not in lines[sid]]
        if bad:
            problems.append(f"line ids {bad} are not in {sid}")
        if scope in ("lines", "audio") and not changed:
            problems.append(f"a {scope} patch for {sid} needs changed_line_ids")
        if not str(p.get("instruction", "")).strip():
            problems.append("a patch without an instruction")
        patches.append(
            Patch(
                scope,
                sid if scope != "cast" else None,
                str(p.get("instruction", "")),
                changed,
                [x for x in p.get("comment_ids") or [] if x in cids],
            )
        )
    rejected = {
        r["comment_id"]: str(r.get("reason", ""))
        for r in raw.get("rejected") or []
        if isinstance(r, dict) and r.get("comment_id") in cids
    }
    served = {x for p in patches for x in p.comment_ids}
    if not patches and not rejected and comments:
        problems.append("no patch and no rejection")
    missing = [c.id for c in comments if c.id not in served and c.id not in rejected]
    if missing and patches and not any(p.comment_ids for p in patches):
        for p in patches:  # an answer without comment_ids serves every comment it did not reject
            p.comment_ids = [c.id for c in comments if c.id not in rejected]
        missing = []
    if missing:
        problems.append(f"comments neither served nor rejected: {missing}")
    if problems:
        return None, problems
    return RevisionPlan(patches, rejected), []


def plan_revision(
    llm, comments: list[Comment], script: Script, plans: dict[str, ScenePlan], cast: Cast, concepts=None
) -> RevisionPlan:
    scene_ids = {c.scene_id for c in comments if c.scene_id}
    user = (
        "<comments>\n"
        + json.dumps(
            [{"comment_id": c.id, "scene_id": c.scene_id, "body": c.body} for c in comments], ensure_ascii=False
        ).replace("</", "<\\/")
        + "\n</comments>\n\n<story>\n"
        + _story_block(script, plans, cast, scene_ids, concepts)
        + "\n</story>"
    )
    system = PROMPT.read_text(encoding="utf-8")
    problems: list[str] = []
    for _ in range(2):
        raw = llm.complete_json(
            system, user + ("\n\nFix: " + "; ".join(problems) if problems else ""), SCHEMA, "large", 0.3, 3000
        )
        plan, problems = check_plan(raw, comments, script)
        if plan is not None:
            return plan
    raise PlannerError("; ".join(problems))
