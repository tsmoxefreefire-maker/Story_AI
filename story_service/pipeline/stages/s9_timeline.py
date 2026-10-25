"""S9 Timeline Compiler (code) — Section 5, S9. Its output becomes `story-data` in the bundle (Section 7.3).

Timing rules:
  * a scene starts with a 600 ms lead-in before its first line;
  * after a narrator line comes a 350 ms gap, after a dialogue line 250 ms; after the scene's last line an 800 ms tail;
  * line start/end come from the REAL audio durations (S6), word times are shifted onto the story clock;
  * every cue anchor (line start / end / a word) is resolved to an absolute time and clamped inside its scene;
  * an interaction fires 200 ms after the end of the line it follows and pauses the clock, so interaction time is not
    part of `duration_ms`; `duration_ms` is the end of the last scene."""

from __future__ import annotations

from ..models import (
    AudioOut,
    BindingTarget,
    ScenePlan,
    Script,
    StoryMeta,
    TimedCue,
    TimedInteraction,
    TimedLine,
    TimedScene,
    Timeline,
    ValidationReport,
    Word,
)

LEAD_IN, GAP_NARRATOR, GAP_DIALOGUE, TAIL, INTERACTION_DELAY = 600, 350, 250, 800, 200
SCENE_MIN_MS, SCENE_MAX_MS = 8000, 40000


class UnresolvedCue(ValueError):
    pass


def _plans_by_id(plans) -> dict[str, ScenePlan]:
    if isinstance(plans, dict):
        return {k: (v if isinstance(v, ScenePlan) else ScenePlan.model_validate(v)) for k, v in plans.items()}
    out = {}
    for p in plans or []:
        p = p if isinstance(p, ScenePlan) else ScenePlan.model_validate(p)
        out[p.scene_id] = p
    return out


def scene_clock(scene, audio: AudioOut, start: int = 0) -> tuple[dict, int]:
    """Place one scene's lines on the clock from `start`: {line_id: (start_ms, end_ms, words)} and the scene end.
    S7 uses it with start=0 to check the 6-second rule on scene time; `compile` uses it on story time."""
    cursor, when = start + LEAD_IN, {}
    for i, line in enumerate(scene.lines):
        a = audio.line(line.id)
        if a is None:
            raise ValueError(f"no audio for line {line.id}")
        ls, le = cursor, cursor + a.duration_ms
        when[line.id] = (ls, le, [Word(w=w.w, s=ls + w.s, e=ls + w.e) for w in a.words])
        last = i == len(scene.lines) - 1
        cursor = le + (TAIL if last else GAP_NARRATOR if line.speaker == "narrator" else GAP_DIALOGUE)
    return when, cursor


def compile(
    script: Script, audio: AudioOut, plans, target_sec: int | None = None, *, language: str = "ar", strict: bool = False
) -> Timeline:
    """Build the timeline. Unresolvable cues are dropped and listed in `timeline.unresolved` (validate_s9 turns them
    into blockers); with `strict=True` the first one raises."""
    plans = _plans_by_id(plans)
    t = 0
    scenes, unresolved, moved = [], [], []
    for sc in script.scenes:
        start = t
        when, end = scene_clock(sc, audio, start)
        lines = [
            TimedLine(
                id=line.id,
                speaker=line.speaker,
                text=line.text_display,
                audio=audio.line(line.id).file,
                start_ms=when[line.id][0],
                end_ms=when[line.id][1],
                words=when[line.id][2],
            )
            for line in sc.lines
        ]
        plan = plans.get(sc.id)
        cues = []
        for c in plan.cues if plan else []:
            try:
                ts = resolve_cue(c, when)
            except UnresolvedCue as e:
                if strict:
                    raise
                unresolved.append(f"{sc.id}/{c.id}: {e}")
                continue
            dur = max(0, c.dur_ms)
            clamped = max(start, min(ts, end - min(dur, end - start)))  # clamp inside the scene
            if clamped != ts:
                moved.append(f"{sc.id}/{c.id} {ts - start} -> {clamped - start} ms")
            ts = clamped
            cues.append(TimedCue(id=c.id, t=ts, dur=dur, target=c.target, action=c.action, params=c.params))
        cues.sort(key=lambda c: (c.t, c.id))
        inter = None
        if sc.interaction is not None:
            it = sc.interaction
            if it.after_line not in when:
                raise ValueError(f"interaction {it.id} follows unknown line {it.after_line}")
            binding = next((b for b in (plan.interaction_bindings if plan else []) if b.interaction_id == it.id), None)
            inter = TimedInteraction(
                **it.model_dump(),
                at_ms=when[it.after_line][1] + INTERACTION_DELAY,
                bindings=[
                    BindingTarget(index=b.index, element_id=b.element_id) for b in (binding.targets if binding else [])
                ],
            )
        scenes.append(
            TimedScene(
                id=sc.id, start_ms=start, end_ms=end, svg_id=f"scene-{sc.id}", lines=lines, cues=cues, interaction=inter
            )
        )
        t = end
    tl = Timeline(
        meta=StoryMeta(title=script.title, language=language, dir="rtl" if language == "ar" else "ltr", duration_ms=t),
        characters={c.key: {"name": c.name} for c in script.characters},
        scenes=scenes,
    )
    tl._unresolved, tl._clamped, tl._target_sec = unresolved, moved, target_sec
    return tl


def resolve_cue(cue, when) -> int:
    """Absolute time of a cue anchor; raises UnresolvedCue when the line or word does not exist."""
    at = cue.at
    if at.line not in when:
        raise UnresolvedCue(f"line {at.line} does not exist in this scene")
    ls, le, words = when[at.line]
    if at.anchor == "start":
        base = ls
    elif at.anchor == "end":
        base = le
    else:
        if at.word_index is None or not 0 <= at.word_index < len(words):
            raise UnresolvedCue(f"word_index {at.word_index} outside line {at.line} ({len(words)} words)")
        base = words[at.word_index].s
    return base + (at.offset_ms or 0)


def validate_s9(tl: Timeline, target_sec: int | None = None) -> ValidationReport:
    rep = ValidationReport(stage="S9")
    unresolved = tl._unresolved
    rep.add(
        "cues_resolved",
        not unresolved,
        "blocker",
        "; ".join(unresolved[:5]),
        code="CUE_UNRESOLVED",
        suspect_stage="S7_director",
        fix_hint="anchor every cue to a line of its scene and a word index inside it",
    )
    for sc in tl.scenes:
        length = sc.end_ms - sc.start_ms
        rep.add(
            f"scene_length_{sc.id}",
            SCENE_MIN_MS <= length <= SCENE_MAX_MS,
            "blocker",
            f"scene {sc.id} lasts {length / 1000:.1f} s (needs 8 to 40 s)",
            code="SCENE_LENGTH",
            suspect_stage="S4_writer",
            fix_hint="change the number or length of the scene's lines",
        )
    rep.add(
        "cues_inside",
        not tl._clamped,
        "minor",
        f"cues moved inside their scene: {tl._clamped[:5]}",
        code="CUE_CLAMPED",
        suspect_stage="S7_director",
    )
    target_sec = target_sec if target_sec is not None else tl._target_sec
    if target_sec:
        off = (tl.meta.duration_ms / 1000 - target_sec) / target_sec
        pct = round(abs(off) * 100)
        word = "too long" if off > 0 else "too short"
        if abs(off) <= 0.15:
            rep.add("duration", True, "minor")
        elif abs(off) <= 0.25:
            rep.add("duration", False, "minor", f"{word} by {pct}%", code="DURATION_OFF")
        else:
            rep.add(
                "duration",
                False,
                "blocker",
                f"{word} by {pct}%",
                code="DURATION_OFF",
                suspect_stage="S4_writer",
                fix_hint=f"{word} by {pct}%",
            )
    return rep
