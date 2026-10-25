# story-service — Module 6 (audio, visuals, assembly, validation, publish, review)

One lesson topic in, one short narrated interactive story out. This repository holds **module 6** of the spec
(`spec/06_audio_visuals_assembly.md`): stages **S6 to S12** and the **Review** flow (Section 10), plus the minimal
shared pieces they need (models, provider interfaces, blob store, version store).

```
script.json + cast.json + intake.json
   │
   ├─ S6  Audio Producer      TTS → ffmpeg → STT round trip → audio.json + audio/*.mp3
   ├─ S7  Visual Director     LLM fills a scene plan; code checks every rule (validate_s7)
   ├─ S8  Scene Composer      plan + asset library → scene SVG (sanitized, tokens only)
   ├─ S9  Timeline Compiler   real audio durations → absolute times for lines, words, cues, interactions
   ├─ S10 Assembler           one self-contained index.html (+ audio files) with the vanilla JS player
   ├─ S11 Render validation   Chromium on 3 viewports, interaction tests, VLM critic → repair loop (S7/S8/S5)
   └─ S12 Publish             published / needs_review, idea memory, signed webhooks
Review: comments → revision planner → a new version that re-runs only the touched scenes
```

## Quick start

```bash
pip install -e ".[dev]"                     # Python 3.11+, plus ffmpeg on the PATH
python -m playwright install chromium       # browser tests, S11, contact sheets
python scripts/fetch_fonts.py               # Cairo font (without it a stand-in font is embedded, warning FONT_FALLBACK)

python scripts/build_golden.py out/golden              # the golden story «بيتزا نادية العادلة», about 10 s
python scripts/build_golden.py out/golden --validate   # the same, then S11 (about 2 min) → out/golden/validation.json
python scripts/serve_bundle.py out/golden              # open http://localhost:8000 (HTTP Range + production headers)
python scripts/contact_sheet.py out/golden             # one PNG with a frame per line
python scripts/library_sheets.py out/library           # library acceptance (6.8): contact sheets + joints check
python scripts/build_story.py stories/plants out/plants  # any story folder, no code change (--scene s04: one scene)

pytest                                      # 323 tests: unit + integration (integration needs ffmpeg and Chromium)
pytest tests/unit                           # the fast part, about 30 s
```

No API key is needed for any of the above: `FakeTTS` hums softly (0.4 s per word), `FakeSTT` echoes the text,
`FakeVLM` scores 5, and the director plans of the golden story are fixtures. To hear real voices put `GROQ_API_KEY`
in the environment (never in a file in the repo) and run
`TTS_PROVIDER=groq STT_PROVIDER=groq python scripts/build_golden.py out/golden-groq`; its last line prints the
measured speech rate to put in `SPEECH_WPS_AR` (9.7).

## Layout

| Path | What it is |
| --- | --- |
| `src/story_service/pipeline/stages/` | One file per stage: `s6_audio.py` … `s10_assembler.py`, `s11_validate.py`, `s11_repair.py`, `s12_publish.py` |
| `src/story_service/pipeline/module6.py` | `StoryBuild`: one version's artifacts stage by stage (used by the repair loop and revisions) |
| `src/story_service/pipeline/review/` | Section 10: `comments.py`, `planner.py` (Revision Planner), `revision.py` |
| `src/story_service/pipeline/versions.py` | Story versions, comments, idea memory, webhook deliveries; `VersionStore` + in-memory store |
| `src/story_service/pipeline/models.py` | Pydantic models of every artifact (script, cast, audio.json, scene plan, timeline …) |
| `src/story_service/pipeline/prompts/` | Director, VLM critic, continuity and revision planner prompts |
| `src/story_service/assets/` | The asset library: character kit, 16 backgrounds, 60 props, 13 widgets, `catalog.json` |
| `src/story_service/runtime/` | The player: `template.html`, `player.js`, CSS, `i18n.json` |
| `src/story_service/validators/` | SVG sanitizer, bundle checks, render checks, interaction tests, VLM critic, text helpers |
| `src/story_service/providers/` | Interfaces + test doubles: LLM, VLM, TTS, STT, blob store (Groq adapters for TTS/STT) |
| `src/story_service/core/` | Settings (environment only), retries, signed webhooks |
| `src/story_service/devtools/` | Local bundle server (production headers, HTTP Range), library sheets, story folders |
| `scripts/` | `build_golden`, `build_story`, `serve_bundle`, `contact_sheet`, `library_sheets`, `build_catalog`, `fetch_fonts` |
| `stories/` | Example stories as module 6 receives them: plant parts (9-11), percentages (12-14), reading a clock (6-8) |
| `tests/` | `unit/` per stage (the spec's tables), `integration/` the golden story, S11 fault injection and library in Chromium |
| `docs/` | `DECISIONS.md` (choices the spec left open), `MODULE6_AR.md` (Arabic walkthrough) |

## What other modules plug in

- The real LLM/VLM clients (milestone M1): the stages take any object with the `LLMClient` / VLM interface.
- The database, API and job queue (M0/M7): implement `pipeline.versions.VersionStore`; the stages only use that.
- S4 edit mode, S5 and the Story Critic's coverage check (modules 4 and 5): passed to the revision flow and the repair
  loop as callables (`edit_lines`, `edit_interaction`, `rerun_cast`, `coverage_check`). Without them the version
  ends `needs_review`, never silently.

## Rules this code keeps

- The LLM never writes code, CSS or SVG markup for the page: it fills JSON that code validates, and the player
  animates that JSON. The only LLM-drawn SVG (a rare custom prop) goes through the sanitizer and a vision check of
  its rendered picture.
- Every color is a design token (`var(--…)`); every SVG passes the sanitizer; the bundle makes no network request.
- Keys only come from environment variables and never appear in logs, reports, bundles or error messages.
- Additions outside the plan are kept apart in `docs/DECISIONS.md` (56 onward) so they can be accepted or dropped:
  tap reactions on characters and props (they never pause the story and are not one of its 2 to 4 interactions),
  contact shadows, prop names, ambient loops (clocks, plants, clouds, steam, birds, cats), the example story folders.
