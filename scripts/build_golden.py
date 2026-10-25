"""Build the golden story «بيتزا نادية» end to end with test providers and write the playable bundle to a folder.

    python scripts/build_golden.py out/golden            # build (about 10 s)
    python scripts/build_golden.py out/golden --validate # build, then S11 in Chromium (about a minute)
    python scripts/serve_bundle.py out/golden            # serve it, then open http://localhost:8000

No API key is needed: FakeTTS hums softly, 0.4 s per word (a real voice comes with TTS_PROVIDER=groq and GROQ_API_KEY),
the director plans come from tests/fixtures/golden/plans, the pizza cutter custom prop from tests/fixtures/llm."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from story_service.core.config import Settings  # noqa: E402
from story_service.pipeline import module6  # noqa: E402
from story_service.pipeline.models import Cast, Intake, ScenePlan, Script  # noqa: E402
from story_service.pipeline.stages.s6_audio import measured_wps  # noqa: E402
from story_service.providers.llm import FakeLLM  # noqa: E402
from story_service.providers.stt import FakeSTT  # noqa: E402
from story_service.providers.tts import FakeTTS  # noqa: E402
from story_service.providers.vlm import FakeVLM  # noqa: E402

GOLDEN = ROOT / "tests" / "fixtures" / "golden"


def load_golden():
    script = Script.model_validate_json((GOLDEN / "script.json").read_text(encoding="utf-8"))
    cast = Cast.model_validate_json((GOLDEN / "cast.json").read_text(encoding="utf-8"))
    intake = Intake.model_validate_json((GOLDEN / "intake.json").read_text(encoding="utf-8"))
    plans = {
        p.stem.removeprefix("scene_plan_"): ScenePlan.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted((GOLDEN / "plans").glob("scene_plan_*.json"))
    }
    return script, cast, intake, plans


def providers():
    """Test providers by default; the real Groq voice when TTS_PROVIDER=groq and the key is in the environment."""
    if os.environ.get("TTS_PROVIDER") == "groq":
        from story_service.providers.stt.groq import GroqSTT
        from story_service.providers.tts.groq import GroqTTS

        s = Settings.from_env()
        if not s.groq_api_key:
            raise SystemExit("TTS_PROVIDER=groq needs GROQ_API_KEY in the environment")
        return GroqTTS(api_key=s.groq_api_key, model=s.tts_model), GroqSTT(api_key=s.groq_api_key, model=s.stt_model)
    return FakeTTS(), FakeSTT()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to a code page without Arabic
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", type=Path, help="folder for index.html, audio/ and meta.json")
    ap.add_argument("--validate", action="store_true", help="also run S11 (Chromium, about a minute)")
    args = ap.parse_args()
    script, cast, intake, plans = load_golden()
    tts, stt = providers()
    t0 = time.time()
    try:
        res = module6.run(
            script,
            cast,
            intake,
            tts=tts,
            stt=stt,
            llm=FakeLLM("s8_custom_pizza_cutter.json"),
            vlm=FakeVLM(score=5),
            plans=plans,
            digits=Settings.from_env().display_digits,
        )
    except module6.StageFailed as e:  # a stage refused its input: say which and why, without a traceback
        print(f"{e.stage} refused the story:")
        for i in e.report.issues:
            if i.severity in ("blocker", "major"):
                print(f"  {i.severity} {i.code}: {i.message}")
        return 1
    for rel, data in res.bundle.files().items():
        path = args.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (args.out / "timeline.json").write_text(res.timeline.model_dump_json(indent=1), encoding="utf-8")
    (args.out / "audio.json").write_text(res.audio.model_dump_json(indent=1), encoding="utf-8")
    m = res.bundle.meta
    print(f"built in {time.time() - t0:.1f} s -> {args.out}/index.html")
    print(
        f"duration {m.duration_ms / 1000:.1f} s, {m.scene_count} scenes, {m.interaction_count} interactions, "
        f"page {m.sizes.bundle_bytes / 1024:.0f} KB, "
        f"audio {m.sizes.audio_bytes / 1024:.0f} KB in {m.sizes.audio_files} files"
    )
    for w in res.warnings:
        print(f"warning {w.code}: {w.message}")
    for stage, rep in res.reports.items():
        for i in rep.issues:
            print(f"{stage} {i.severity} {i.code}: {i.message}")
    print(json.dumps({"voices": res.audio.voices}, ensure_ascii=False))
    wps_key = "SPEECH_WPS_AR" if intake.language == "ar" else "SPEECH_WPS_EN"
    print(f"speech rate {measured_wps(script, res.audio)} words/s (9.7: with real voices, put it in {wps_key})")
    if args.validate:
        from story_service.devtools.bundle_server import serving
        from story_service.pipeline.stages import s11_validate

        with serving(str(args.out)) as url:
            rep = s11_validate.run(url, vlm=FakeVLM(score=5), script=script, cast=cast, audio_lines=3)
        (args.out / "validation.json").write_text(
            json.dumps(rep.to_json(), ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(
            f"S11: {'passed' if rep.passed else 'FAILED'} ({len(rep.blockers)} blockers, {len(rep.majors)} majors,"
            f" {len(rep.minors)} minors) -> {args.out}/validation.json"
        )
        for i in rep.blockers + rep.majors:
            print(f"  {i.severity} {i.code} {i.scene_id or ''}: {i.message}")
        return 0 if rep.passed else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
