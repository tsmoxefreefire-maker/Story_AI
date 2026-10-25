"""Build a story from a folder, without touching any code.

A story folder holds what modules 1 to 5 hand to module 6, as JSON files:

    intake.json                  module 1: language, dialect, age group, target duration
    script.json                  module 4: scenes, lines, interactions
    cast.json                    module 5: characters' looks, each scene's background and props
    plans/scene_plan_sNN.json    S7: the director's plan for each scene. A missing plan needs the director LLM, whose
                                 real client is milestone M1; without it the build stops and names the scene.
    llm/*.json                   optional recorded LLM answers (custom props), so a demo runs offline

`scripts/build_story.py` is the command line. The golden test story is the same shape (`tests/fixtures/golden`)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from ..core.config import Settings
from ..pipeline import module6
from ..pipeline.models import Cast, Intake, ScenePlan, Script
from ..providers.llm import FakeLLM
from ..providers.stt import FakeSTT
from ..providers.tts import FakeTTS
from ..providers.vlm import FakeVLM

FILES = {"intake.json": Intake, "script.json": Script, "cast.json": Cast}


class StoryFolderError(ValueError):
    """The folder cannot be built; the message says which file and why, in plain words."""


@dataclass
class StoryFolder:
    path: Path
    intake: Intake
    script: Script
    cast: Cast
    plans: dict[str, ScenePlan] = field(default_factory=dict)
    llm_answers: list[Path] = field(default_factory=list)

    @property
    def scene_ids(self) -> list[str]:
        return [s.id for s in self.script.scenes]


def _read(path: Path, model):
    if not path.exists():
        raise StoryFolderError(f"{path.name} is missing in {path.parent}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise StoryFolderError(f"{path.name} is not valid JSON (line {e.lineno}, column {e.colno}): {e.msg}") from None
    try:
        return model.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(x) for x in first["loc"])
        raise StoryFolderError(f"{path.name}: {where}: {first['msg']} ({e.error_count()} problem(s))") from None


def load(folder: str | Path) -> StoryFolder:
    folder = Path(folder)
    if not folder.is_dir():
        raise StoryFolderError(f"no story folder at {folder}")
    parts = {name: _read(folder / name, model) for name, model in FILES.items()}
    plans = {}
    for f in sorted((folder / "plans").glob("scene_plan_*.json")):
        plan = _read(f, ScenePlan)
        plans[plan.scene_id] = plan
    story = StoryFolder(
        path=folder,
        intake=parts["intake.json"],
        script=parts["script.json"],
        cast=parts["cast.json"],
        plans=plans,
        llm_answers=sorted((folder / "llm").glob("*.json")),
    )
    unknown = sorted(set(plans) - set(story.scene_ids))
    if unknown:
        raise StoryFolderError(f"plans for scenes that are not in script.json: {', '.join(unknown)}")
    return story


def only_scenes(story: StoryFolder, scene_ids: list[str]) -> StoryFolder:
    """The same story with only the given scenes (a preview of one scene, or a few)."""
    unknown = [s for s in scene_ids if s not in story.scene_ids]
    if unknown:
        raise StoryFolderError(f"no scene {', '.join(unknown)}; this story has {', '.join(story.scene_ids)}")
    keep = set(scene_ids)
    script = story.script.model_copy(update={"scenes": [s for s in story.script.scenes if s.id in keep]})
    return StoryFolder(
        path=story.path,
        intake=story.intake,
        script=script,
        cast=story.cast,
        plans={k: v for k, v in story.plans.items() if k in keep},
        llm_answers=story.llm_answers,
    )


def providers():
    """Test voices by default; the real Groq voice when TTS_PROVIDER=groq and GROQ_API_KEY are in the environment."""
    if os.environ.get("TTS_PROVIDER") == "groq":
        from ..providers.stt.groq import GroqSTT
        from ..providers.tts.groq import GroqTTS

        s = Settings.from_env()
        if not s.groq_api_key:
            raise StoryFolderError("TTS_PROVIDER=groq needs GROQ_API_KEY in the environment")
        return GroqTTS(api_key=s.groq_api_key, model=s.tts_model), GroqSTT(api_key=s.groq_api_key, model=s.stt_model)
    return FakeTTS(), FakeSTT()


def build(story: StoryFolder, *, preview: bool = False, tts=None, stt=None, llm=None, vlm=None):
    """Run module 6 (S6 to S10) on the folder. `preview` (some scenes only) skips the story's total-length check."""
    missing = [s for s in story.scene_ids if s not in story.plans]
    if missing and llm is None and not story.llm_answers:
        raise StoryFolderError(
            f"no director plan for {', '.join(missing)} in plans/, and no director LLM is connected (milestone M1)"
        )
    if llm is None and story.llm_answers:
        llm = FakeLLM(*[p.name for p in story.llm_answers], fixtures_dir=story.path / "llm")
    if tts is None or stt is None:
        tts, stt = providers()
    return module6.run(
        story.script,
        story.cast,
        story.intake,
        tts=tts,
        stt=stt,
        llm=llm,
        vlm=vlm or FakeVLM(score=5),
        plans=story.plans,
        digits=Settings.from_env().display_digits,
        check_duration=not preview,
    )


def write(res, out: str | Path) -> Path:
    """index.html, audio/, meta.json, plus timeline.json and audio.json for reading."""
    out = Path(out)
    for rel, data in res.bundle.files().items():
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (out / "timeline.json").write_text(res.timeline.model_dump_json(indent=1), encoding="utf-8")
    (out / "audio.json").write_text(res.audio.model_dump_json(indent=1), encoding="utf-8")
    return out / "index.html"
