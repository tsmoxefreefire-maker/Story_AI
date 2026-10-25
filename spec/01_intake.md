<!-- Module 1: Intake (S0) + Localization Brief (S1) — split from Design Interactive Lesson Stories.md (v2). Shared rules in 00_shared.md -->

> Module: Module 1: Intake (S0) + Localization Brief (S1)
> Shared conventions, API, data model, config, errors and tests: see `00_shared.md`.

### S0 Intake (code; an LLM only to locate the topic)

1. Normalize text: Unicode NFC, replace NBSP, collapse 3 or more newlines into 2. Keep diacritics.
2. **Topic span.** If `topic.text` is present, use it as `topic_text`. Otherwise call the Topic Locator (small model) with `lesson_text` and `topic.title`; it returns `{start_quote, end_quote}`, each 6 to 12 words copied verbatim. Find both with `norm_ar` search. The span runs from the start of the first to the end of the second. If either is not found, fail with `TOPIC_NOT_FOUND`.
3. `topic_text` must be at least 200 characters, else fail `INPUT_TOO_SHORT`.
4. **Context.** `prior_text` = up to 2,500 characters before the span (what the student already studied). `later_preview` = up to 400 characters after it (must not be taught).
5. **Language check.** For `ar`, at least 60% of letters in `topic_text` are Arabic script. For `en`, at least 60% are Latin. Else fail `LANGUAGE_MISMATCH`.
6. **Dialect.** If the request has none, derive it from the country: EG gives `egyptian`; SA, AE, KW, QA, BH, OM, YE, IQ give `gulf`; JO, LB, SY, PS give `levantine`; MA, DZ, TN, LY give `maghrebi`; anything else gives `msa`. If the active TTS provider’s `supported_dialects` does not include it, set `dialect_applied` to `msa` and add the warning `DIALECT_FALLBACK`. For `en` the dialect is null.
7. **Duration.** `target_duration_sec` = request value or the age default, clamped to the age range (Section 1).

Artifact `intake.json`: `topic_title`, `topic_text`, `prior_text`, `later_preview`, `language`, `dialect_applied`, `country`, `age_group`, `grade`, `subject`, `target_duration_sec`, `warnings`.

### S1 Localization Brief (small LLM, cached)

Cache key (country, language, dialect\_applied, age\_group), TTL `BRIEF_TTL_DAYS` (90). Fields:

- `names_female` and `names_male`: at least 20 each; common first names, no public figures.
- `currency`: `code`, `symbol`, `spoken_name`, `typical_prices` (6 everyday items with a realistic price in local currency).
- `places` (at least 8 everyday places), `foods` (at least 6), `transport` (at least 3), `holidays_and_seasons` (at least 4), `sensitivities` (at least 3 things to avoid).
- `school_context`, `clothing_norms` (daily clothes by gender and age, and whether head coverings are common), `family_terms`, `humor_notes` (1 to 2 sentences), `speech_style` (register guidance for the dialect and age, with 5 example phrases).

Validation: schema and counts are blockers. A second LLM call reviews for stereotypes, religious or political content, real brands and public figures. Pass only if it flags nothing.

---

## I/O Contract and Test Sample (Module 1: S0 + S1)

> Function names below (`run`, `validate_*`) are suggestions. If you choose different names, record them in `docs/DECISIONS.md`. All sample data uses the golden request from Section 15 (`tests/fixtures/sample_request.json`).

### S0 Intake

**Input** = the create request (Section 3) plus the active TTS provider's `supported_dialects`.

```json
{
  "request": "<tests/fixtures/sample_request.json: lesson_id L-1, topic_id T-1, title 'مفهوم الكسر', topic.text null, country EG, language ar, dialect null, age_group 9-11, target_duration_sec null>",
  "tts_supported_dialects": ["gulf", "msa"]
}
```

**Output** `intake.json`:

```json
{
  "topic_title": "مفهوم الكسر",
  "topic_text": "مفهوم الكسر\nالكسر جزء من كل. عندما نقسم شيئًا إلى أجزاء متساوية، يسمى كل جزء كسرًا من هذا الشيء. نكتب الكسر على صورة عددين بينهما خط: العدد الأسفل هو المقام، ويدل على عدد الأجزاء المتساوية التي قُسم إليها الكل. والعدد الأعلى هو البسط، ويدل على عدد الأجزاء التي نأخذها. مثال: إذا قسمنا قطعة بيتزا إلى 4 أجزاء متساوية وأكلنا جزءًا واحدًا، فقد أكلنا ربع البيتزا، ونكتب ذلك 1/4.",
  "prior_text": "الكسور",
  "later_preview": "مقارنة الكسور\nعندما يكون المقام واحدًا في كسرين، يكون الكسر الأكبر هو الذي بسطه أكبر، فالكسر 3/8 أكبر من 2/8. وعندما يكون البسط واحدًا، يكون الكسر الأكبر هو الذي مقامه أصغر، فالكسر 1/2 أكبر من 1/4، لأن الأجزاء الأقل عددًا تكون أكبر حجمًا.",
  "language": "ar",
  "dialect_applied": "msa",
  "country": "EG",
  "age_group": "9-11",
  "grade": "4",
  "subject": "math",
  "target_duration_sec": 150,
  "warnings": [
    {"code": "DIALECT_FALLBACK", "message": "Dialect 'egyptian' (derived from country EG) is not supported by TTS provider 'groq'; using 'msa'."}
  ]
}
```

**FakeLLM fixture** for the Topic Locator (`tests/fixtures/llm/s0_topic_locator.json`):

```json
{"start_quote": "مفهوم الكسر الكسر جزء من كل عندما", "end_quote": "فقد أكلنا ربع البيتزا ونكتب ذلك 1/4"}
```

**Test sample** (`tests/unit/test_s0_intake.py`):

```python
def test_s0_golden():
    out = s0_intake.run(request=load("sample_request.json"),
                        tts_supported_dialects=["gulf", "msa"],
                        llm=FakeLLM("s0_topic_locator.json"))
    assert out.topic_text.startswith("مفهوم الكسر")
    assert out.topic_text.rstrip(".").endswith("1/4")
    assert 300 <= len(out.topic_text) <= 450            # about 380 per Section 15
    assert out.later_preview.startswith("مقارنة الكسور")  # must NOT be inside topic_text
    assert "مقارنة الكسور" not in out.topic_text
    assert out.dialect_applied == "msa"
    assert [w.code for w in out.warnings] == ["DIALECT_FALLBACK"]
    assert out.target_duration_sec == 150
```

**Parametrized cases** (each must be a test):

| Case | Input change | Expected |
| --- | --- | --- |
| Topic text given | `topic.text` = 250-char string | Locator is **not** called; `topic_text` = that string |
| Topic not found | FakeLLM returns quotes absent from `lesson_text` | version `failed`, `TOPIC_NOT_FOUND` |
| Too short | `topic.text` of 120 chars | `failed`, `INPUT_TOO_SHORT` |
| Language mismatch | `language: "en"` with the Arabic text | `failed`, `LANGUAGE_MISMATCH` |
| Egyptian supported | `tts_supported_dialects=["egyptian","msa"]` | `dialect_applied == "egyptian"`, no warning |
| Explicit dialect | `dialect: "gulf"` | `dialect_applied == "gulf"`, no warning |
| Country map | SA→gulf, JO→levantine, MA→maghrebi, JP→msa | matches Section 5, S0 step 6 |
| Duration clamp | age 9-11 with `target_duration_sec` 400 / 60 | 210 / 120 |
| English | `language: "en"` | `dialect_applied is None` |
| Diacritics | quotes without diacritics vs text with them | found (via `norm_ar`) |

---

### S1 Localization Brief

**Input:** `{country, language, dialect_applied, age_group}` from `intake.json`. For the sample: `{"country": "EG", "language": "ar", "dialect_applied": "msa", "age_group": "9-11"}`.

**Output** `brief.json` (abbreviated here; the fixture must satisfy every minimum count in Section 5, S1):

```jsonc
{
  "names_female": ["نادية", "سلمى", "ليلى", "هدى", "ياسمين" /* ... at least 20 */],
  "names_male":   ["كريم", "يوسف", "عمر", "مازن", "آدم" /* ... at least 20 */],
  "currency": {
    "code": "EGP", "symbol": "ج.م", "spoken_name": "جنيه",
    "typical_prices": [
      {"item": "رغيف خبز", "price": "1 جنيه"}
      /* ... 6 everyday items */
    ]
  },
  "places": ["السوق", "المدرسة", "الحديقة", "المخبز" /* ... at least 8 */],
  "foods": ["فول", "طعمية", "بيتزا", "أرز" /* ... at least 6 */],
  "transport": ["أتوبيس", "ميكروباص", "دراجة"],
  "holidays_and_seasons": ["رمضان", "العيد", "الصيف", "بداية العام الدراسي"],
  "sensitivities": ["لا علامات تجارية حقيقية", "لا شخصيات عامة", "لا سياسة أو دين كجزء من الحبكة"],
  "school_context": "…",
  "clothing_norms": "…",
  "family_terms": ["ماما", "بابا", "تيتة", "جدو"],
  "humor_notes": "…",
  "speech_style": {"guidance": "…", "example_phrases": ["…", "…", "…", "…", "…"]}
}
```

**Test sample:**

```python
def test_s1_brief_valid_and_cached(db):
    brief = s1_brief.run(country="EG", language="ar", dialect_applied="msa", age_group="9-11",
                         llm=FakeLLM("s1_brief.json", reviewer="s1_review_clean.json"), db=db)
    assert len(brief.names_female) >= 20 and len(brief.names_male) >= 20
    assert len(brief.currency.typical_prices) == 6
    assert len(brief.places) >= 8 and len(brief.foods) >= 6 and len(brief.speech_style.example_phrases) == 5
    # second call with same key must hit the cache: zero LLM calls
    llm2 = CountingFakeLLM()
    s1_brief.run(country="EG", language="ar", dialect_applied="msa", age_group="9-11", llm=llm2, db=db)
    assert llm2.calls == 0
```

| Case | Setup | Expected |
| --- | --- | --- |
| Count too low | `names_female` has 15 items | blocker, retry with feedback |
| Reviewer flags | `s1_review_flagged.json` (flags a real brand) | stage fails, retried |
| Cache expiry | row `expires_at` in the past | brief regenerated |
| Different dialect | same country, `dialect_applied="egyptian"` | separate cache row (key includes dialect) |
