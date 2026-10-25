# Example stories

Each folder is one story as module 6 receives it: what modules 1 to 5 produce, plus the S7 director's plans. They
were written by hand as stand-ins (the same way the golden story is), so module 6 can be tried on more than one story
before modules 1 to 5 and the real LLM client exist.

| Folder | Topic | Age | Scenes | Interactions |
| --- | --- | --- | --- | --- |
| `plants/` | أجزاء النبات (science) | 9-11 | 5 | choose answer, drag match, tap reveal |
| `percent/` | النسبة المئوية (math) | 12-14 | 5 | choose answer, drag match, predict |
| `clock/` | قراءة الساعة (math) | 6-8 | 5 | choose answer, predict |

```
intake.json               module 1: language, dialect, age group, target duration
script.json               module 4: scenes, lines, interactions
cast.json                 module 5: characters' looks, each scene's background and props
plans/scene_plan_sNN.json S7: where everyone stands and every cue
```

Build one (no code change, no key):

```bash
python scripts/build_story.py stories/plants out/plants            # the whole story
python scripts/build_story.py stories/plants out/s04 --scene s04   # one scene only
python scripts/build_story.py stories/plants out/plants --validate # then S11
python scripts/serve_bundle.py out/plants                          # http://localhost:8000
```

A new story is a new folder with the same four parts. If a plan is missing, the build stops and names the scene:
writing plans is the director LLM's job, whose real client is milestone M1.
