"""Pydantic models of the stage artifacts.

The first part mirrors what modules 1 to 5 hand over (intake.json, script.json, cast.json), exactly as the spec defines
them, so module 6 can be built and tested on the golden fixtures before those modules exist. The second part is what
module 6 produces (S6 to S12). JSON keys are English; story text is in the story's language (Section 5.0)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

Severity = Literal["blocker", "major", "minor"]
TimeOfDay = Literal["day", "evening", "night"]
AgeGroup = Literal["6-8", "9-11", "12-14", "15-18"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------------------------------------- shared reporting
class Warning_(_Model):
    code: str
    message: str


class Check(_Model):
    name: str
    passed: bool
    severity: Severity
    detail: str = ""


class Issue(_Model):
    severity: Severity
    code: str
    message: str
    fix_hint: str = ""
    suspect_stage: str | None = None
    scene_id: str | None = None
    line_id: str | None = None


class ValidationReport(_Model):
    """The report every validator returns (Section 8.1). A stage passes when no blocker or major check failed."""

    stage: str
    scene_id: str | None = None
    passed: bool = True
    checks: list[Check] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    issues: list[Issue] = Field(default_factory=list)

    def add(
        self,
        name: str,
        ok: bool,
        severity: Severity,
        detail: str = "",
        *,
        code: str | None = None,
        fix_hint: str = "",
        suspect_stage: str | None = None,
        line_id: str | None = None,
    ) -> bool:
        """Record one check; a failed check also becomes an issue. Returns `ok` so callers can chain."""
        self.checks.append(Check(name=name, passed=ok, severity=severity, detail="" if ok else detail))
        if not ok:
            self.issues.append(
                Issue(
                    severity=severity,
                    code=code or name.upper(),
                    message=detail,
                    fix_hint=fix_hint,
                    suspect_stage=suspect_stage,
                    scene_id=self.scene_id,
                    line_id=line_id,
                )
            )
            if severity in ("blocker", "major"):
                self.passed = False
        return ok

    @property
    def blockers(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "blocker"]

    @property
    def majors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "major"]

    @property
    def minors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "minor"]


# ------------------------------------------------------------------------------------------------- module 1: intake
class Intake(_Model):
    topic_title: str
    topic_text: str
    prior_text: str = ""
    later_preview: str = ""
    language: Literal["ar", "en"]
    dialect_applied: str | None
    country: str
    age_group: AgeGroup
    grade: str
    subject: str
    target_duration_sec: int
    warnings: list[Warning_] = Field(default_factory=list)


# ------------------------------------------------------------------------------------------------- module 4: script
class Character(_Model):
    key: str
    name: str
    role: str
    gender: Literal["f", "m"]
    age_years: int
    traits: list[str] = Field(default_factory=list)


class Line(_Model):
    id: str
    speaker: str
    emotion: str = "neutral"
    text_display: str
    text_spoken: str


class _InteractionBase(_Model):
    id: str
    after_line: str
    concept_id: str
    prompt: str
    hint: str | None = None


class RevealTarget(_Model):
    label: str
    reveal_text: str
    thing: str


class TapReveal(_InteractionBase):
    type: Literal["tap_reveal"]
    targets: list[RevealTarget] = Field(min_length=2, max_length=4)


class AnswerOption(_Model):
    id: str
    text: str
    correct: bool
    feedback: str


class ChooseAnswer(_InteractionBase):
    type: Literal["choose_answer"]
    options: list[AnswerOption] = Field(min_length=2, max_length=4)
    max_attempts: int = 2

    @model_validator(mode="after")
    def _one_correct(self):
        if sum(o.correct for o in self.options) != 1:
            raise ValueError("choose_answer needs exactly one correct option")
        return self


class MatchPair(_Model):
    id: str
    item: str
    target: str


class DragMatch(_InteractionBase):
    type: Literal["drag_match"]
    pairs: list[MatchPair] = Field(min_length=2, max_length=4)
    wrong_feedback: str


class PredictOption(_Model):
    id: str
    text: str


class Predict(_InteractionBase):
    type: Literal["predict"]
    options: list[PredictOption] = Field(min_length=2, max_length=3)
    expected_option_id: str
    reveal_text: str


Interaction = Annotated[TapReveal | ChooseAnswer | DragMatch | Predict, Field(discriminator="type")]


class Scene(_Model):
    id: str
    summary: str
    location: str
    time_of_day: TimeOfDay
    lines: list[Line]
    concepts_covered: list[str] = Field(default_factory=list)
    interaction: Interaction | None = None


class Script(_Model):
    title: str
    logline: str
    characters: list[Character]
    scenes: list[Scene]
    recap_line_ids: list[str] = Field(default_factory=list)

    def all_lines(self) -> list[Line]:
        return [line for scene in self.scenes for line in scene.lines]

    def character(self, key: str) -> Character | None:
        return next((c for c in self.characters if c.key == key), None)


# ------------------------------------------------------------------------------------------------- module 5: cast
class Clothing(_Model):
    style: str
    color_token: str


class VoiceProfile(_Model):
    gender: Literal["f", "m"]
    age_band: Literal["child", "teen", "adult", "elder"]
    energy: str = "normal"


class CastCharacter(_Model):
    key: str
    build: Literal["child", "teen", "adult", "elder"]
    presentation: Literal["f", "m"]
    skin_tone: int = Field(ge=0, le=5)
    hair_style: str
    hair_color: int = Field(ge=0, le=5)
    top: Clothing
    bottom: Clothing
    shoes: str
    accessories: list[str] = Field(default_factory=list)
    head_covering: Literal["none", "hijab", "cap", "kufi"] = "none"
    voice_profile: VoiceProfile
    idle_style: Literal["calm", "normal", "lively"] = "normal"


class CustomProp(_Model):
    key: str
    description: str


class Setting(_Model):
    scene_id: str
    background_id: str
    time_of_day: TimeOfDay
    prop_ids: list[str] = Field(default_factory=list)
    custom_props: list[CustomProp] = Field(default_factory=list)


class Cast(_Model):
    characters: list[CastCharacter]
    settings: list[Setting]

    def character(self, key: str) -> CastCharacter | None:
        return next((c for c in self.characters if c.key == key), None)

    def setting(self, scene_id: str) -> Setting | None:
        return next((s for s in self.settings if s.scene_id == scene_id), None)


# ------------------------------------------------------------------------------------------------- S6 audio
class Word(_Model):
    w: str
    s: int
    e: int


class AudioLine(_Model):
    line_id: str
    file: str
    duration_ms: int
    voice_id: str
    words: list[Word]
    timing_source: Literal["stt", "estimated"]


class AudioOut(_Model):
    voices: dict[str, str]
    lines: list[AudioLine]
    warnings: list[Warning_] = Field(default_factory=list)
    _report: ValidationReport | None = PrivateAttr(default=None)  # S6 checks, kept out of audio.json
    _files: dict = PrivateAttr(default_factory=dict)  # audio/{line_id}.mp3 -> bytes

    def line(self, line_id: str) -> AudioLine | None:
        return next((a for a in self.lines if a.line_id == line_id), None)


# ------------------------------------------------------------------------------------------------- S7 scene plan
Anchor = Literal["start", "end", "word"]
Action = Literal[
    "show",
    "hide",
    "fade",
    "move",
    "scale",
    "rotate",
    "keyframes",
    "highlight",
    "shake",
    "camera",
    "enter",
    "exit",
    "pose",
    "expression",
    "face",
]
TWEEN_ACTIONS = {
    "show",
    "hide",
    "fade",
    "move",
    "scale",
    "rotate",
    "keyframes",
    "highlight",
    "shake",
    "camera",
    "enter",
    "exit",
}
STATE_ACTIONS = {"pose", "expression", "face"}
VISUAL_CHANGE_ACTIONS = TWEEN_ACTIONS | STATE_ACTIONS  # everything except an idle loop counts as a visible change


class Background(_Model):
    id: str
    time_of_day: TimeOfDay


class Camera(_Model):
    x: float = 640
    y: float = 360
    zoom: float = 1.0


class Actor(_Model):
    char_key: str
    x: float
    y: float
    scale: float = 1.0
    facing: Literal["left", "right"] = "right"
    z: int = 20
    pose: str = "idle"
    expression: str = "neutral"
    offstage_ok: bool = False


class PropPlacement(_Model):
    ref: str
    id: str
    x: float
    y: float
    scale: float = 1.0
    rotation: float = 0
    z: int = 10


class WidgetPlacement(_Model):
    ref: str
    id: str
    params: dict = Field(default_factory=dict)
    x: float
    y: float
    scale: float = 1.0
    z: int = 30
    initial: Literal["visible", "hidden"] = "visible"


class CueAt(_Model):
    line: str
    anchor: Anchor = "start"
    word_index: int | None = None
    offset_ms: int = 0


class Cue(_Model):
    id: str
    at: CueAt
    target: str
    action: Action
    params: dict = Field(default_factory=dict)
    dur_ms: int = 400


class BindingTarget(_Model):
    index: int
    element_id: str


class InteractionBinding(_Model):
    interaction_id: str
    targets: list[BindingTarget]


class ScenePlan(_Model):
    scene_id: str
    background: Background
    camera: Camera = Field(default_factory=Camera)
    actors: list[Actor] = Field(default_factory=list)
    props: list[PropPlacement] = Field(default_factory=list)
    widgets: list[WidgetPlacement] = Field(default_factory=list)
    cues: list[Cue] = Field(default_factory=list)
    interaction_bindings: list[InteractionBinding] = Field(default_factory=list)

    def element_ids(self) -> list[str]:
        return [a.char_key for a in self.actors] + [p.id for p in self.props] + [w.id for w in self.widgets]


# ------------------------------------------------------------------------------------------------- S8 scene meta
class ElementBox(_Model):
    bbox: list[float]  # [x0, y0, x1, y1] in stage px at the planned position
    kind: Literal["actor", "prop", "widget"] = "prop"


class SceneMeta(_Model):
    scene_id: str
    elements: dict[str, ElementBox]
    warnings: list[Warning_] = Field(default_factory=list)


# ------------------------------------------------------------------------------------ S9 timeline (= story-data)
class TimedLine(_Model):
    id: str
    speaker: str
    text: str
    audio: str
    start_ms: int
    end_ms: int
    words: list[Word]


class TimedCue(_Model):
    id: str
    t: int
    dur: int
    target: str
    action: Action
    params: dict = Field(default_factory=dict)


class TimedInteraction(BaseModel):
    """The script's interaction plus its time on the clock (`at_ms`) and, for `tap_reveal`, the bound elements."""

    model_config = ConfigDict(extra="allow")
    id: str
    type: str
    at_ms: int
    bindings: list[BindingTarget] = Field(default_factory=list)


class TimedScene(_Model):
    id: str
    start_ms: int
    end_ms: int
    svg_id: str
    lines: list[TimedLine]
    cues: list[TimedCue]
    interaction: TimedInteraction | None = None


class StoryMeta(_Model):
    title: str
    language: Literal["ar", "en"]
    dir: Literal["rtl", "ltr"]
    duration_ms: int


class Timeline(_Model):
    schema_version: str = "1.0"
    meta: StoryMeta
    characters: dict[str, dict]
    scenes: list[TimedScene]
    # compile-time notes for validate_s9, never serialized into story-data
    _unresolved: list[str] = PrivateAttr(default_factory=list)
    _clamped: list[str] = PrivateAttr(default_factory=list)
    _target_sec: int | None = PrivateAttr(default=None)

    def interaction_at(self, interaction_id: str) -> int | None:
        for s in self.scenes:
            if s.interaction and s.interaction.id == interaction_id:
                return s.interaction.at_ms
        return None


# ------------------------------------------------------------------------------------------------- S10 bundle meta
class Sizes(_Model):
    bundle_bytes: int
    audio_bytes: int
    audio_files: int


class BundleMeta(_Model):
    title: str
    duration_ms: int
    language: Literal["ar", "en"]
    dir: Literal["rtl", "ltr"]
    age_group: AgeGroup
    scene_count: int
    interaction_count: int
    sizes: Sizes
    schema_version: str
    runtime_version: str
    generated_at: str
