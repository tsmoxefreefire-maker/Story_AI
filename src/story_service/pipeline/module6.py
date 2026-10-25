"""Module 6 end to end: script + cast + intake -> playable bundle (S6 -> S7 -> S8 -> S9 -> S10), plus the pieces the
S11 repair loop and the revision flow re-run for one scene.

The shared orchestrator (milestone M0) will call the stages one by one with its own retries, resume and storage. This
module is the same chain without that machinery, for the golden test, local demos, the repair loop and revisions.
S11 (browser validation) is `stages/s11_validate.py` + `stages/s11_repair.py`; S12 is `stages/s12_publish.py`."""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field

from ..assets.catalog import load_catalog
from ..providers.blobstore import BlobStore, ScopedBlobStore
from .models import AudioOut, Cast, Intake, SceneMeta, ScenePlan, Script, Timeline, ValidationReport, Warning_
from .render import svg_to_png
from .stages import s6_audio, s7_director, s8_composer, s9_timeline, s10_assembler


class StageFailed(RuntimeError):
    """A stage ended with blockers or majors. `.stage` names it and `.report` holds its checks."""

    def __init__(self, stage: str, report: ValidationReport, detail: str = ""):
        issues = "; ".join(f"[{i.code}] {i.message}" for i in report.issues if i.severity in ("blocker", "major"))
        super().__init__(f"{stage} failed: {detail or issues}")
        self.stage, self.report = stage, report


@dataclass
class StoryBuild:
    """Everything one story version is made of, stage by stage. The repair loop and revisions change one scene and
    call `assemble` again; nothing else is recomputed."""

    script: Script
    cast: Cast
    intake: Intake
    tts: object
    stt: object
    llm: object = None
    vlm: object = None
    blob: BlobStore | None = None  # global store: custom props are cached under custom_props/ for every story
    prefix: str | None = None  # stories/{story_id}/v{version}: audio and the bundle are stored under it
    catalog: dict = field(default_factory=load_catalog)
    digits: str = "western"
    voices: list | None = None
    render_png: Callable[[str], bytes | None] | None = None
    strict: bool = True
    generated_at: str | None = None
    check_duration: bool = True  # False when only some scenes are built (a preview): the story's total is not checked
    # results
    audio: AudioOut | None = None
    audio_files: dict[str, bytes] = field(default_factory=dict)
    plans: dict[str, ScenePlan] = field(default_factory=dict)
    scenes: dict[str, str] = field(default_factory=dict)
    metas: dict[str, SceneMeta] = field(default_factory=dict)
    timeline: Timeline | None = None
    bundle: s10_assembler.Bundle | None = None
    reports: dict[str, ValidationReport] = field(default_factory=dict)
    warnings: list[Warning_] = field(default_factory=list)
    custom_props: dict[str, str] = field(default_factory=dict)  # description -> drawing, for this story

    # ------------------------------------------------------------------------------------------------- helpers
    def fork(self, prefix: str | None = None) -> StoryBuild:
        """An independent copy for a new version (revisions): models and dicts are copied, blobs are shared."""
        clone = copy.copy(self)
        clone.script, clone.cast = self.script.model_copy(deep=True), self.cast.model_copy(deep=True)
        clone.audio = self.audio.model_copy(deep=True) if self.audio else None
        if self.audio is not None:
            clone.audio._report, clone.audio._files = self.audio._report, dict(self.audio._files)
        clone.audio_files, clone.scenes, clone.metas = dict(self.audio_files), dict(self.scenes), dict(self.metas)
        clone.plans = {k: v.model_copy(deep=True) for k, v in self.plans.items()}
        clone.reports, clone.warnings = dict(self.reports), list(self.warnings)
        clone.custom_props = dict(self.custom_props)
        clone.prefix = prefix if prefix is not None else self.prefix
        return clone

    @property
    def story_blob(self) -> BlobStore | None:
        if self.blob is None or not self.prefix:
            return self.blob
        return ScopedBlobStore(self.blob, self.prefix)

    def gate(self, name: str, rep: ValidationReport) -> ValidationReport:
        self.reports[name] = rep
        if self.strict and not rep.passed:
            raise StageFailed(name, rep)
        return rep

    def scene(self, scene_id: str):
        return next(s for s in self.script.scenes if s.id == scene_id)

    # ------------------------------------------------------------------------------------------------- stages
    def produce_audio(self, only_lines: set[str] | None = None) -> AudioOut:
        """S6 for every line, or only `only_lines` (revisions), keeping the other lines' audio."""
        out = s6_audio.run(
            self.script,
            self.cast,
            self.intake,
            voices=self.voices or s6_audio.load_voices(provider=getattr(self.tts, "name", None)),
            tts=self.tts,
            stt=self.stt,
            blob=self.story_blob,
            only_lines=only_lines,
            previous=self.audio,
        )
        self.audio_files.update(out._files)
        self.audio = out
        self.warnings += [w for w in out.warnings if w not in self.warnings]
        self.gate("S6", out._report)
        return out

    def direct_scene(self, scene_id: str, feedback: list[str] | None = None) -> ScenePlan:
        """S7 for one scene: check the given plan, or ask the director (with `feedback`, e.g. an S11 fix_hint)."""
        sc = self.scene(scene_id)
        if scene_id in self.plans and not feedback:
            rep = s7_director.validate_s7(
                self.plans[scene_id],
                scene=sc,
                audio=self.audio,
                catalog=self.catalog,
                setting=self.cast.setting(scene_id),
                cast=self.cast,
            )
        else:
            if self.llm is None:
                raise ValueError(f"scene {scene_id} needs the director but no LLM was given")
            self.plans[scene_id], rep = s7_director.run(
                sc,
                self.audio,
                self.cast,
                self.catalog,
                self.llm,
                feedback=feedback,
                previous_plan=self.plans.get(scene_id),
            )
        self.gate(f"S7/{scene_id}", rep)
        return self.plans[scene_id]

    def compose_scene(self, scene_id: str, refresh_custom_props: bool = False) -> str:
        """S8 for one scene. `refresh_custom_props` draws the scene's custom props again (S11 `S8_prop` repair)."""
        render = self.render_png
        if render is None and self.vlm is not None:
            age = self.intake.age_group

            def render(svg: str) -> bytes | None:
                return svg_to_png(svg, age)

        try:
            svg, meta = s8_composer.run(
                self.plans[scene_id],
                self.cast,
                self.catalog,
                llm=self.llm,
                vlm=self.vlm,
                blob=None if refresh_custom_props else self.blob,
                digits=self.digits,
                age_group=self.intake.age_group,
                render_png=render,
                prop_cache=None if refresh_custom_props else self.custom_props,
                language=self.intake.language,
            )
        except s8_composer.ComposeError as e:
            rep = ValidationReport(stage="S8", scene_id=scene_id)
            rep.add("compose", False, "blocker", str(e), code="COMPOSE", suspect_stage="S7_director")
            self.gate(f"S8/{scene_id}", rep)
            raise
        self.scenes[scene_id], self.metas[scene_id] = svg, meta
        self.warnings += [w for w in meta.warnings if w not in self.warnings]
        return svg

    def assemble(self) -> s10_assembler.Bundle:
        """S9 + S10 over the current scenes and audio."""
        target = self.intake.target_duration_sec if self.check_duration else None
        self.timeline = s9_timeline.compile(self.script, self.audio, self.plans, target, language=self.intake.language)
        self.gate("S9", s9_timeline.validate_s9(self.timeline))
        self.bundle = s10_assembler.run(
            self.timeline,
            self.scenes,
            self.audio_files,
            age_group=self.intake.age_group,
            generated_at=self.generated_at,
        )
        self.warnings += [w for w in self.bundle.warnings if w not in self.warnings]
        self.gate("S10", self.bundle.report)
        if self.blob is not None and self.prefix:
            for rel, data in self.bundle.files().items():  # stories/{id}/v{n}/bundle/... (Section 4)
                self.story_blob.put(f"bundle/{rel}", data)
        return self.bundle


# ----------------------------------------------------------------------------------------------- the whole chain
@dataclass
class Module6Result:
    audio: AudioOut
    plans: dict[str, ScenePlan]
    scenes: dict[str, str]  # scene_id -> composed scene svg (S8)
    metas: dict[str, SceneMeta]
    timeline: Timeline
    bundle: s10_assembler.Bundle
    reports: dict[str, ValidationReport] = field(default_factory=dict)
    warnings: list[Warning_] = field(default_factory=list)
    build: StoryBuild | None = None


def run(
    script: Script,
    cast: Cast,
    intake: Intake,
    *,
    tts,
    stt,
    llm=None,
    vlm=None,
    blob: BlobStore | None = None,
    prefix: str | None = None,
    plans: dict[str, ScenePlan] | None = None,
    voices=None,
    catalog: dict | None = None,
    digits: str = "western",
    render_png=None,
    generated_at: str | None = None,
    strict: bool = True,
    check_duration: bool = True,
) -> Module6Result:
    """Build one story. `plans` (scene_id -> ScenePlan) skips the director LLM for those scenes, which is how the
    golden test runs without an LLM. With `strict=True` a failed stage raises StageFailed."""
    b = StoryBuild(
        script=script,
        cast=cast,
        intake=intake,
        tts=tts,
        stt=stt,
        llm=llm,
        vlm=vlm,
        blob=blob,
        prefix=prefix,
        catalog=catalog or load_catalog(),
        digits=digits,
        voices=voices,
        render_png=render_png,
        strict=strict,
        generated_at=generated_at,
        check_duration=check_duration,
        plans=dict(plans or {}),
        warnings=list(intake.warnings),
    )
    b.produce_audio()
    for sc in script.scenes:
        b.direct_scene(sc.id)
        b.compose_scene(sc.id)
    b.assemble()
    return Module6Result(
        audio=b.audio,
        plans=b.plans,
        scenes=b.scenes,
        metas=b.metas,
        timeline=b.timeline,
        bundle=b.bundle,
        reports=b.reports,
        warnings=b.warnings,
        build=b,
    )
