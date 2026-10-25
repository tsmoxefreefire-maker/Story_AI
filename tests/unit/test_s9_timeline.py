"""S9 Timeline Compiler (spec samples and table)."""

import pytest

import golden
from story_service.pipeline.models import AudioLine, AudioOut, ScenePlan, Word
from story_service.pipeline.stages import s9_timeline
from story_service.pipeline.stages.s9_timeline import UnresolvedCue, validate_s9


def audio_with(durations: dict[str, int]) -> AudioOut:
    """The golden audio with some lines set to the given durations (words spread evenly)."""
    out = golden.audio()
    lines = []
    for a in out.lines:
        if a.line_id in durations:
            d, n = durations[a.line_id], len(a.words)
            a = AudioLine(
                line_id=a.line_id,
                file=a.file,
                duration_ms=d,
                voice_id=a.voice_id,
                timing_source=a.timing_source,
                words=[Word(w=w.w, s=i * d // n, e=(i + 1) * d // n) for i, w in enumerate(a.words)],
            )
        lines.append(a)
    return AudioOut(voices=out.voices, lines=lines)


def plans_with_cue(scene_id: str, cue: dict) -> dict:
    p = golden.plans()
    data = p[scene_id].model_dump()
    data["cues"] = [cue]
    p[scene_id] = ScenePlan.model_validate(data)
    return p


S01 = {"s01_l01": 4400, "s01_l02": 2000, "s01_l03": 4800}


def test_s9_timing_arithmetic():
    cue = {
        "id": "s01_c01",
        "at": {"line": "s01_l02", "anchor": "start"},
        "target": "karim",
        "action": "expression",
        "params": {"name": "happy"},
        "dur_ms": 600,
    }
    tl = s9_timeline.compile(golden.script(), audio_with(S01), plans_with_cue("s01", cue), 150)
    s = tl.scenes[0]
    assert [(ln.start_ms, ln.end_ms) for ln in s.lines] == [(600, 5000), (5350, 7350), (7600, 12400)]
    assert s.end_ms == 13200 and tl.scenes[1].start_ms == 13200
    c = next(c for c in s.cues if c.id == "s01_c01")
    assert c.t == 5350
    assert s.lines[0].words[0].s == 600  # word times moved onto the story clock


def test_s9_interaction_offset_and_pause():
    tl = s9_timeline.compile(golden.script(), golden.audio(), golden.plans(), 150)
    s03 = next(s for s in tl.scenes if s.id == "s03")
    after = next(ln for ln in s03.lines if ln.id == s03.interaction.after_line)
    assert tl.interaction_at("s03_i1") == after.end_ms + 200
    assert tl.meta.duration_ms == tl.scenes[-1].end_ms


def test_tap_reveal_bindings_are_copied():
    tl = s9_timeline.compile(golden.script(), golden.audio(), golden.plans(), 150)
    s02 = next(s for s in tl.scenes if s.id == "s02")
    assert [b.element_id for b in s02.interaction.bindings] == ["pizza_small", "pizza_big"]


def test_word_anchor_with_offset():
    cue = {
        "id": "s01_c01",
        "at": {"line": "s01_l01", "anchor": "word", "word_index": 2, "offset_ms": 50},
        "target": "nadia",
        "action": "highlight",
        "params": {},
        "dur_ms": 400,
    }
    tl = s9_timeline.compile(golden.script(), audio_with(S01), plans_with_cue("s01", cue), 150)
    line = tl.scenes[0].lines[0]
    assert tl.scenes[0].cues[0].t == line.words[2].s + 50


def test_cue_past_scene_end_is_clamped():
    cue = {
        "id": "s01_c01",
        "at": {"line": "s01_l03", "anchor": "end", "offset_ms": 60000},
        "target": "nadia",
        "action": "highlight",
        "params": {},
        "dur_ms": 600,
    }
    tl = s9_timeline.compile(golden.script(), audio_with(S01), plans_with_cue("s01", cue), 150)
    s = tl.scenes[0]
    assert s.start_ms <= s.cues[0].t <= s.end_ms - 600


@pytest.mark.parametrize("length_ms,ok", [(6000, False), (45000, False), (13200, True)])
def test_scene_length_limits(length_ms, ok):
    rest = length_ms - 600 - 800 - 350 - 250
    tl = s9_timeline.compile(
        golden.script(),
        audio_with({"s01_l01": rest // 3, "s01_l02": rest // 3, "s01_l03": rest - 2 * (rest // 3)}),
        golden.plans(),
    )
    rep = validate_s9(tl)
    flagged = any(i.code == "SCENE_LENGTH" and "scene s01 " in i.message for i in rep.issues)
    assert flagged != ok


@pytest.mark.parametrize("actual_s,severity", [(160, None), (175, "minor"), (200, "blocker")])
def test_total_duration(actual_s, severity):
    tl = s9_timeline.compile(golden.script(), golden.audio(), golden.plans(), 150)
    tl.meta.duration_ms = actual_s * 1000
    rep = validate_s9(tl, 150)
    found = [i for i in rep.issues if i.code == "DURATION_OFF"]
    if severity is None:
        assert not found
    else:
        assert found[0].severity == severity
    if severity == "blocker":
        assert found[0].suspect_stage == "S4_writer" and "too long by 33%" in found[0].message


def test_unresolved_cue_is_a_blocker():
    cue = {
        "id": "s01_c01",
        "at": {"line": "s01_l09", "anchor": "start"},
        "target": "nadia",
        "action": "highlight",
        "params": {},
        "dur_ms": 400,
    }
    tl = s9_timeline.compile(golden.script(), golden.audio(), plans_with_cue("s01", cue), 150)
    rep = validate_s9(tl)
    assert any(i.code == "CUE_UNRESOLVED" and i.severity == "blocker" for i in rep.issues)
    with pytest.raises(UnresolvedCue):
        s9_timeline.compile(golden.script(), golden.audio(), plans_with_cue("s01", cue), 150, strict=True)


def test_golden_timeline_passes():
    tl = s9_timeline.compile(golden.script(), golden.audio(), golden.plans(), 150)
    rep = validate_s9(tl)
    assert rep.passed and not rep.issues, rep.issues
    assert 127_500 <= tl.meta.duration_ms <= 172_500  # within 15% of 150 s
