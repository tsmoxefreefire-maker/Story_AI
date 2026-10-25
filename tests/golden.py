"""The golden story «بيتزا نادية العادلة» (tests/fixtures/golden): loaders shared by the tests, each read once.

`audio()` is the S6 output made by FakeTTS + FakeSTT (tests/fixtures/golden/audio.json, rebuilt by
`python scripts/build_golden.py`); tests that need the MP3 bytes use `fake_mp3s()` unless they test S6 itself."""

from __future__ import annotations

import copy
import json
from functools import cache
from pathlib import Path

from story_service.assets.catalog import load_catalog
from story_service.pipeline.models import AudioOut, Cast, Intake, ScenePlan, Script

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden"
CATALOG = load_catalog()


def _json(name: str):
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


@cache
def _script() -> Script:
    return Script.model_validate(_json("script.json"))


@cache
def _cast() -> Cast:
    return Cast.model_validate(_json("cast.json"))


@cache
def _audio() -> AudioOut:
    return AudioOut.model_validate(_json("audio.json"))


def script() -> Script:
    return _script().model_copy(deep=True)


def cast() -> Cast:
    return _cast().model_copy(deep=True)


def intake() -> Intake:
    return Intake.model_validate(_json("intake.json"))


def audio() -> AudioOut:
    return _audio().model_copy(deep=True)


def plan_dict(scene_id: str) -> dict:
    return copy.deepcopy(_json(f"plans/scene_plan_{scene_id}.json"))


def plan(scene_id: str) -> ScenePlan:
    return ScenePlan.model_validate(plan_dict(scene_id))


def plans() -> dict[str, ScenePlan]:
    return {s.id: plan(s.id) for s in _script().scenes}


def scene(scene_id: str):
    return next(s for s in script().scenes if s.id == scene_id)


def fake_mp3s(audio_out: AudioOut | None = None) -> dict[str, bytes]:
    """Small stand-in bytes for every line file (S10 only checks presence and sizes)."""
    return {a.file: b"ID3" + b"\x00" * 200 for a in (audio_out or _audio()).lines}


CUTTER = "s8_custom_pizza_cutter.json"


def fake_png(svg: str) -> bytes:
    """Stands in for the Chromium render of a custom prop (the FakeVLM does not look at it)."""
    return b"\x89PNG fake"


@cache
def _build():
    from story_service.pipeline import module6
    from story_service.providers.llm import FakeLLM
    from story_service.providers.stt import FakeSTT
    from story_service.providers.tts import FakeTTS
    from story_service.providers.vlm import FakeVLM

    res = module6.run(
        script(),
        cast(),
        intake(),
        tts=FakeTTS(),
        stt=FakeSTT(),
        llm=FakeLLM(CUTTER),
        vlm=FakeVLM(score=5),
        plans=plans(),
        render_png=fake_png,
        generated_at="2026-10-21T08:00:00Z",
    )
    return res.build


def build():
    """A fresh copy of the golden story built end to end with fakes (built once per test run, about 10 s)."""
    return _build().fork()


def write_bundle(bundle, folder) -> str:
    from pathlib import Path as _P

    for rel, data in bundle.files().items():
        path = _P(folder) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return str(folder)


def inject(folder, *, css: str = "", js: str = "") -> None:
    """Fault injection for S11 tests: extra CSS in the head and/or an extra script at the end of the page."""
    from pathlib import Path as _P

    page = _P(folder) / "index.html"
    html = page.read_text(encoding="utf-8")
    if css:
        html = html.replace("</head>", f"<style>{css}</style></head>", 1)
    if js:
        html = html.replace("</body>", f"<script>{js}</script></body>", 1)
    page.write_text(html, encoding="utf-8")
