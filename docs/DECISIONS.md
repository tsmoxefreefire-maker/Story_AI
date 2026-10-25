# Decisions (module 6)

Choices the spec left open, or where the code had to go slightly further than the text. Each entry: what, why,
and where it lives. Anything here can be changed by the team; the tests name the behavior they rely on.

## Scope

1. **Module 6 carries a minimal copy of the shared pieces.** `pipeline/models.py` (upstream artifacts),
   `providers/*` (LLM, VLM, TTS, STT, blob store interfaces and fakes), `core/config.py` and `pipeline/versions.py`
   (story versions, comments, idea memory, webhook deliveries) exist so S6–S12 and Review run and are tested before
   M0/M1 land. The stage code only uses the interfaces; M0 implements `VersionStore` over the database.
2. **`pipeline/module6.py` → `StoryBuild`** holds one version's artifacts stage by stage (audio, plans, scenes,
   timeline, bundle) with `produce_audio`, `direct_scene`, `compose_scene`, `assemble`. The S11 repair loop and the
   revision flow re-run one scene through it; untouched scenes are reused as they are. The real orchestrator (M0)
   adds retries, resume and storage around the same calls.
3. **Steps that belong to other modules are passed in.** A revision `lines`/`interaction` patch needs S4 in edit
   mode (`edit_lines`, `edit_interaction`), a `cast` patch or an S11 `S5_cast` repair needs S5 (`rerun_cast`), the
   coverage re-check needs the Story Critic (`coverage_check`). Without them the version ends `needs_review` (or a
   minor `COVERAGE_UNCHECKED`), never silently.

## Assets and coordinates

4. **Anchors.** Actor: `x` = center, `y` = feet. Prop: `x, y` = bottom-center of its catalog bbox. Widget: `x, y` =
   center of its box. Props that hold things carry `surface` (the local `y` of their top).
5. **Depth shades and UI tints.** Every palette token has a `-d` twin (×0.82). UI highlights use `-l` tints and
   `--scrim`, computed in Python, instead of CSS `color-mix()`, which iOS 15 and Android WebView 100 lack.
6. **Widgets sit on a paper card** (label cards already are one), so a pie over a kitchen window reads as a teaching
   aid. The card is part of the widget's box, so every size check includes it.
7. **Pointing is stage-relative.** `point_left` points to the left of the stage even when the actor faces left.
8. **Builds** (child, teen, adult, elder) differ by head/body scale in CSS, not by separate drawings.
9. **Sanitizer limit for widgets: 200 elements** (the spec lists none for widgets).
10. **Backgrounds without night.** School yard, market and classroom have day and evening only (nobody is there at
    night); the catalog's `supports` says so and S7 rejects a plan that asks for a missing time of day. The library
    contact sheet shows the times each background supports.
11. **The talk pose is livelier**: the near arm gestures with upper arm and forearm out of phase, the other arm and
    the head move too. Frozen frames (`?freeze=1`) keep the middle of the gesture.

## Scene SVG and nesting depth

12. **Ids are namespaced per scene in S10** (`sNN-<id>`), story-data is rewritten the same way.
13. **One wrapper per scene.** S10 turns S8's root group into the scene's own `<svg class="scene scene-root tod-…">`;
    with the actor's placement and the kit offset merged into one transform, the deepest element is exactly at the
    12-level limit (8.2). `p-flip` mirrors around the kit's center (`transform-origin: 100px 0`).

## Director (S7)

14. **"A visible change every 6 s" counts real changes**: a state cue that sets the value an actor already has does
    not count.
15. **`offstage_ok` only relaxes the 40 px margin, and only for an actor with an `enter` or `exit` cue.** The planned
    position is where the actor stands while on stage, so it must still be on the stage; otherwise a speaker could
    talk while invisible (blocker `OFFSTAGE_NO_CUE`).
16. **Moves are bounded too.** A `move`/`keyframes` cue is relative to the planned position; where it starts (`from`)
    and ends must be inside the safe area, and a moved prop or widget box must stay on the stage (S11 found a bus
    driving in from past the edge whose anchor was inside). The camera (plan and cues) must never show outside the
    stage at its zoom (`CAMERA_BOUNDS`).
17. **No two tweens animate the same property of one element at the same time** (`TWEEN_OVERLAP`): the later one
    would replace the earlier one and the element would jump. Transform and opacity are checked separately.
18. **Extra checks**: the whole widget box inside the safe area; the background matches the setting (major); speaker
    facing and z ranges are minor. Schema errors come back as blockers so the LLM can fix them.
19. **Re-directing a scene shows the director its current plan** (`<current_plan>`) with the points to change (S11
    fix hints, review instructions), so it changes only what is needed. Text in every prompt block is escaped so it
    cannot close its tag.

## Composer (S8)

20. **A custom prop is only accepted after the VLM saw a picture of it.** `pipeline/render.py` draws it with
    Chromium; with a VLM but no picture the generic box is used. Drawings are kept per story (`custom_props`) so
    composing a scene again reuses them.
21. **Color checks look at attribute and style values only** ("Room #101" on a label is not a color); custom
    properties may hold anything except a raw color.

## Audio (S6)

22. **`reference_text` on `STTProvider.transcribe`** exists only so `FakeSTT` can "hear" a tone. Real providers ignore it.
23. **All Groq voices are `adult`** (Groq publishes no ages); the fake voices include child voices for demos.
24. **The fake voice is a soft hum**: a sine tone of 0.4 s per word (13.1) with a smooth envelope and a small pitch
    glide, so a demo sounds like a murmur, not beeps.
25. **Failures are blockers, not crashes**: silent audio, an empty `text_spoken` or an ffmpeg failure on one line
    give a blocker for that line; the other lines still finish.
26. **Audio is stored per version** (`stories/{id}/v{n}/audio/…`) through `ScopedBlobStore`; custom props stay in
    the global `custom_props/` cache.
27. **Retries**: 5 tries with backoff 2, 4, 8, 16 s (12.2 lists five delays but also says at most 5 tries, so the
    32 s step is never reached).
28. **Calibration (9.7)**: `s6_audio.measured_wps(script, audio)` gives the story's spoken words per second;
    `build_golden.py` prints it, so SPEECH_WPS_AR / SPEECH_WPS_EN can be set from real voices.

## Bundle and player

29. **Templates are filled in one pass**, so story text containing `{{player}}` stays text.
30. **Bundle checks go further than 8.6**: forbidden elements (`iframe`, `embed`, `object`, `a`, `img`…), URL-like
    attribute values (`javascript:`, `src`, `srcdoc`…). The strict html5lib parse runs when html5lib is installed
    (it is a dependency); without it a minor `HTML_PARSE_UNCHECKED` says the check did not run.
31. **The sanitizer reports processing instructions and CDATA and rejects backslashes in any attribute value**
    (an HTML parser reads `<?x >` as a comment that ends early; `u\72l(` is a CSS escape for `url(`).
32. **Font.** Production embeds a subset of Cairo (`scripts/fetch_fonts.py`). Without it S10 embeds a local stand-in
    (DejaVu, Tahoma or Arial) with `FONT_FALLBACK`; without `brotli` WOFF instead of WOFF2 with `FONT_WOFF`.
33. **Audio timing.** The next two lines' audio preloads; at a line's start the clock waits (at most 2 s) until its
    audio plays, so the first word is never cut; only after a seek does the audio jump forward. An audio position
    more than 1 s off is a failed seek (a server without HTTP Range), not drift.
34. **Captions** are measured as a plain block when fitting, so long lines really shrink to 16 px before the 2-line
    clamp; they refit when the layout changes.
35. **tap_reveal**: hotspots are placed where the target is drawn at that moment (after its cues and the camera), in
    the scene's own coordinates, at least 64 stage px and 44 CSS px; the card goes to the half of the screen the
    targets are not in; the reveal bubble lives outside the scaled stage (20 px on a phone).
36. **Continue and Skip resume playback** and give the focus back to the play button; modal cards keep Tab inside;
    shortcuts ignore Ctrl/Cmd/Alt combinations; the rotate hint shows once; a `dur` of 0 means "at once".
37. **Test hooks**: `seek`, `play`, `openInteraction` leave the start screen; `answerInteraction` returns false for
    an invalid action; `getState()` also returns the open `interaction` id; Skip and Hint carry the classes `skip`
    and `hint`. i18n gained a `close` label for the rotate hint's button.
38. **Serving locally needs HTTP Range** for audio seeks: `devtools/bundle_server.py` / `scripts/serve_bundle.py`
    answer Range requests and send exactly the `/v1/play` headers (CSP on every page however it is spelled, no
    directory listings). Audio plays under `sandbox allow-scripts` + `media-src 'self'` in Chromium; Safari is still
    to be checked on a device.

## Render validation (S11)

39. **Samples**: the spec's sample times plus, on V1, one frame every 2 s, so a 6 s static stretch is always seen.
40. **Pixel work uses Pillow only** (no numpy dependency).
41. **Waiting is done by polling from Python**: Playwright's `wait_for_function` compiles code inside the page, which
    the production CSP forbids (it showed up as page errors of our own making).
42. **Offstage time does not count for bounds and overlap**: from the scene start until an `enter` ends, and from an
    `exit` until the scene ends.
43. **Audio**: a line whose audio never plays is a major (`AUDIO_NOT_PLAYING`); media errors are recorded through a
    hook (`AUDIO_BROKEN`, blocker). The real-time audio check can be limited to a few lines (`audio_lines`). The
    spec's "audio offset by 400 ms" fault cannot fail this check, because the player follows the audio by design.
44. **Captions too long for 2 lines at 16 px on a phone** are a minor issue for S4 (`CAPTION_TOO_LONG`).
45. **VLM**: a low score without an issue points at the likeliest stage (`characters_consistent` → S5_cast, layout
    scores → S7_director, `no_visual_defects` → S8_prop). The whole-story continuity review runs once every scene
    passed, also on a partial re-validation (one frame of every scene is taken).
46. **Partial re-validation** (repair loop, revisions) checks only the touched scenes plus the bundle checks; the
    untouched scenes' reports are kept from the earlier run.
47. **The playthrough** runs on Playwright's fake clock with audio refused (fake time cannot drive real audio), so the
    clock leads and every interaction is answered through the hooks.

## Publish, review and versions (S12, Section 10)

48. **Warnings**: S12 keeps the stage warnings already on the version (DIALECT_FALLBACK, FONT_FALLBACK…) and adds the
    minor issues, without duplicates.
49. **Idea memory** is written when a `create` or `regenerate` version is published (also through `approve`); a
    revision keeps its parent's idea, which is already remembered.
50. **Webhooks**: the first attempt plus 6 retries, backoff 30, 60, 120, 240, 480, 960 s, then the delivery is dead;
    `X-Signature` is the HMAC-SHA256 of the exact bytes sent.
51. **A revision never leaves the story busy**: a stage that runs out of attempts gives a blocker
    (`VALIDATION_EXHAUSTED`, so it cannot be approved); any other error fails the version with `INTERNAL`, reopens the
    comments and schedules a new job. A busy story makes the job wait with the comments still open.
52. **A `lines` patch re-reads every line whose spoken text changed** (compared with the parent), not only the lines
    the planner listed.
53. **The comments a revision answers are recorded on the version**; they become `applied` when that version is
    published, by S12 or by `approve`.

## Tooling

54. **Tests run with pytest** (markers `integration`, `e2e`, `live`). The integration tests need ffmpeg and Chromium.
55. **Lint and format: ruff**, line length 120 (JavaScript strings inside Python and SVG markup in the widgets are
    exempt from the length rule).

## Additions after the first delivery (asked for by Hareth, not in the plan)

56. **Tap reactions.** A tapped character waves, smiles and shows its name for about 1.5 s; a tapped prop hops and
    shows its name. They never pause the story, are off while a question is open, and are not one of the story's 2 to
    4 interactions (D7 is unchanged). Pointer only, so the keyboard path through the story is the same as before.
    The hop uses the CSS `translate` property, which adds to the cue tweens on `transform` instead of replacing them;
    a browser without it still shows the name. Test hook: `__story.tap(id)`.
57. **Prop names.** Every library prop carries its Arabic name (`data-label-ar` → catalog `label_ar`); S8 writes
    `data-name` on each prop in the story's language (custom props get none).
58. **Contact shadows and cheeks.** Every actor, and every prop that stands on something, gets a soft ellipse under
    it (`ink` at 14%); props that hang on a wall or float (`data-shadow="none"`: sun, cloud, window, wall clock…)
    get none. Faces get two soft cheeks (`c-primary` at 26%). The kitchen's oven moved to the left wall, so a table in
    the middle of the scene no longer stands in front of it.
59. **New props for the example stories**: watering can, seedling with its roots, price tag, sneakers, alarm clock.
60. **Story folders.** `stories/<name>/` holds what modules 1 to 5 and the S7 director hand to module 6, as JSON
    (`intake.json`, `script.json`, `cast.json`, `plans/`). `scripts/build_story.py` builds any of them without code
    changes; `--scene` builds a preview of one scene (the story's total length is then not checked, `check_duration`
    on `StoryBuild`). Three example stories, written by hand as stand-ins: plant parts (9-11), percentages (12-14),
    reading a clock (6-8). Each passes S7 to S11 with no issue.
61. **Ambient loops.** Clock hands tick, the sun's rays turn slowly, flowers, plants and trees sway, clouds drift,
    steam rises from the kitchen pot and from cups, the oven glows, stars twinkle at night, birds cross the sky of
    outdoor places, and cats (with a swishing tail and blinking eyes) sit in public places: street, park, market,
    school yard, playground (`prop_cat` is in the library too). They follow the plan's rules for idle loops (7.5):
    CSS only, not seekable, off with `?freeze=1` and with reduced motion, never counted as a visible change, and paused
    in scenes that are not on screen. Transform and opacity only; every `amb-*` class an asset uses is defined in
    `runtime/scene.css` (a unit test checks it).
62. **S11 measures performance on real playback**: the frame-rate check lifts `freeze` while it measures, so idle and
    ambient loops run (about 60 fps under the 4x CPU throttle on the example stories), then puts it back.

