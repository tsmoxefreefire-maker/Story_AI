"""Build any story folder into a playable bundle, without changing code.

    python scripts/build_story.py stories/plants out/plants                # the whole story
    python scripts/build_story.py stories/plants out/plants_s02 --scene s02  # only scene s02 (a preview)
    python scripts/build_story.py stories/plants out/plants --validate     # then S11 in Chromium
    python scripts/serve_bundle.py out/plants                              # open http://localhost:8000

What a story folder holds is described in src/story_service/devtools/story_folder.py. Test voices are used unless
TTS_PROVIDER=groq and GROQ_API_KEY are set in the environment (never in a file)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from story_service.devtools import story_folder  # noqa: E402
from story_service.pipeline import module6  # noqa: E402
from story_service.pipeline.stages.s6_audio import measured_wps  # noqa: E402
from story_service.providers.vlm import FakeVLM  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to a code page without Arabic
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("story", type=Path, help="story folder (intake.json, script.json, cast.json, plans/)")
    ap.add_argument("out", type=Path, help="folder for index.html, audio/ and meta.json")
    ap.add_argument("--scene", action="append", default=[], help="build only this scene (repeat for more)")
    ap.add_argument("--validate", action="store_true", help="also run S11 (Chromium)")
    args = ap.parse_args()
    t0 = time.time()
    try:
        story = story_folder.load(args.story)
        if args.scene:
            story = story_folder.only_scenes(story, args.scene)
        res = story_folder.build(story, preview=bool(args.scene))
    except story_folder.StoryFolderError as e:
        print(f"cannot build {args.story}: {e}")
        return 1
    except module6.StageFailed as e:  # a stage refused the story: which one and why, without a traceback
        print(f"{e.stage} refused the story:")
        for i in e.report.issues:
            if i.severity in ("blocker", "major"):
                print(f"  {i.severity} {i.code}: {i.message}")
        return 1
    index = story_folder.write(res, args.out)
    m = res.bundle.meta
    print(f"built «{story.script.title}» in {time.time() - t0:.1f} s -> {index}")
    print(
        f"duration {m.duration_ms / 1000:.1f} s, {m.scene_count} scene(s), {m.interaction_count} interaction(s), "
        f"page {m.sizes.bundle_bytes / 1024:.0f} KB, audio {m.sizes.audio_bytes / 1024:.0f} KB"
    )
    for w in res.warnings:
        print(f"warning {w.code}: {w.message}")
    for stage, rep in res.reports.items():
        for i in rep.issues:
            print(f"{stage} {i.severity} {i.code}: {i.message}")
    print(f"voices {json.dumps(res.audio.voices, ensure_ascii=False)}")
    print(f"speech rate {measured_wps(story.script, res.audio)} words per second")
    if args.validate:
        from story_service.devtools.bundle_server import serving
        from story_service.pipeline.stages import s11_validate

        with serving(str(args.out)) as url:
            rep = s11_validate.run(url, vlm=FakeVLM(score=5), script=story.script, cast=story.cast, audio_lines=3)
        (args.out / "validation.json").write_text(json.dumps(rep.to_json(), ensure_ascii=False, indent=1), "utf-8")
        print(
            f"S11: {'passed' if rep.passed else 'FAILED'} ({len(rep.blockers)} blockers, {len(rep.majors)} majors,"
            f" {len(rep.minors)} minors) -> {args.out / 'validation.json'}"
        )
        for i in rep.blockers + rep.majors:
            print(f"  {i.severity} {i.code} {i.scene_id or ''}: {i.message}")
        return 0 if rep.passed else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
