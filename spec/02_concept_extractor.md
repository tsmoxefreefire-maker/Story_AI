<!-- Module 2: Concept Extractor (S2) — split from Design Interactive Lesson Stories.md (v2). Shared rules in 00_shared.md -->

> Module: Module 2: Concept Extractor (S2)
> Shared conventions, API, data model, config, errors and tests: see `00_shared.md`.

### S2 Concept Extractor (large LLM)

Output `concepts.json`:

- `concepts[]`: `id` (c1, c2…), `title` (at most 8 words), `weight` (`must`, `important` or `optional`), `definition` (at most 40 words, own words), `evidence[]` (1 to 2 verbatim quotes of 5 to 40 words from `topic_text`), `real_world_uses[]` (1 to 3 items `{situation, why_it_matters}`, each at most 25 words), `misconception` (`{wrong_belief, why_it_happens}` or null).
- `assumed_prior_knowledge[]`: strings taken from `prior_text`.

Rules: use only `topic_text` (`prior_text` only to know what is assumed). 2 to 8 concepts, 1 to 4 of them `must`. Merge closely related ideas instead of splitting them. `must` means a student cannot apply the topic without it. Real-world uses are everyday situations for the age group and country (use the localization brief).

Validation. Code: every quote is found in `topic_text` after `norm_ar`; ids unique; counts and word limits; at least one `must` (all blockers). Then an independent **Concept Reviewer** (large LLM, separate prompt) receives `topic_text` and the concepts and returns `{missing_concepts: [{title, reason, suggested_weight}], invented_concepts: [id], weight_issues: [{id, issue}]}`. It is a blocker if a missing concept has suggested weight `must` or `important`, or if any concept is invented. Retries receive this report.

---

## I/O Contract and Test Sample (Module 2: S2)

### Input

`intake.json` (Module 1) and `brief.json` (used for real-world uses). Both are passed to the LLM; lesson text is wrapped in `<lesson_text>…</lesson_text>`.

```json
{
  "intake": "<intake.json from Module 1>",
  "brief": "<brief.json from Module 1>",
  "feedback": null
}
```

On a retry, `feedback` holds the failed checks and the Concept Reviewer report.

### Output `concepts.json`

```json
{
  "concepts": [
    {
      "id": "c1",
      "title": "الكسر جزء من كل متساوي",
      "weight": "must",
      "definition": "الكسر هو جزء واحد أو أكثر من شيء قُسم إلى أجزاء متساوية في الحجم.",
      "evidence": [
        "الكسر جزء من كل. عندما نقسم شيئًا إلى أجزاء متساوية، يسمى كل جزء كسرًا من هذا الشيء"
      ],
      "real_world_uses": [
        {"situation": "تقسيم بيتزا أو كعكة بين الأصدقاء", "why_it_matters": "حتى يأخذ كل واحد نصيبًا عادلًا."}
      ],
      "misconception": null
    },
    {
      "id": "c2",
      "title": "المقام والبسط",
      "weight": "must",
      "definition": "المقام يخبرنا إلى كم جزء متساوٍ قُسم الكل، والبسط يخبرنا كم جزءًا أخذنا.",
      "evidence": [
        "العدد الأسفل هو المقام، ويدل على عدد الأجزاء المتساوية التي قُسم إليها الكل",
        "والعدد الأعلى هو البسط، ويدل على عدد الأجزاء التي نأخذها"
      ],
      "real_world_uses": [
        {"situation": "قراءة وصفة تقول ثلاثة أرباع كوب", "why_it_matters": "لنعرف الكمية الصحيحة."}
      ],
      "misconception": {
        "wrong_belief": "المقام هو عدد الأجزاء التي أخذتها.",
        "why_it_happens": "الطفل يرى العددين ولا يربط كل واحد بمعناه."
      }
    }
  ],
  "assumed_prior_knowledge": []
}
```

`assumed_prior_knowledge` is empty here because `prior_text` is only the chapter title "الكسور". The comparison rules (`later_preview`) must **not** appear as a concept.

### Concept Reviewer output (separate call)

```json
{"missing_concepts": [], "invented_concepts": [], "weight_issues": []}
```

### Test sample (`tests/unit/test_s2_concepts.py`)

```python
def test_s2_golden():
    out = s2_concepts.run(intake=load("golden/intake.json"), brief=load("golden/brief.json"),
                          llm=FakeLLM("s2_concepts.json", reviewer="s2_review_clean.json"))
    ids = [c.id for c in out.concepts]
    assert ids == ["c1", "c2"] and len(set(ids)) == 2
    assert {c.weight for c in out.concepts} == {"must"}
    topic_norm = norm_ar(load("golden/intake.json")["topic_text"])
    for c in out.concepts:
        for q in c.evidence:
            assert norm_ar(q) in topic_norm
            assert 5 <= len(q.split()) <= 40
        assert len(c.title.split()) <= 8 and len(c.definition.split()) <= 40
    assert not any("مقارنة" in c.title for c in out.concepts)   # later_preview must not leak
```

| Case | Mutation of the fixture | Expected |
| --- | --- | --- |
| Quote not in text | change one evidence quote | blocker (code check) |
| Quote from `later_preview` | evidence = "فالكسر 3/8 أكبر من 2/8" | blocker |
| No `must` concept | all weights `important` | blocker |
| Duplicate ids | two concepts with id `c1` | blocker |
| Definition too long | 55-word definition | blocker |
| Quote too short | 3-word evidence | blocker |
| Reviewer: missing `must` | reviewer returns `missing_concepts:[{title:"…",suggested_weight:"must"}]` | blocker, retry receives the report |
| Reviewer: missing `optional` | `suggested_weight:"optional"` | passes |
| Reviewer: invented concept | `invented_concepts:["c2"]` | blocker |
| Diacritics | quote copied without diacritics (e.g. "قسم" for "قُسم") | accepted via `norm_ar` |
| Retry | first LLM output invalid, second valid | exactly 2 `stage_runs` (attempt 1 `failed`, attempt 2 `passed`) |
