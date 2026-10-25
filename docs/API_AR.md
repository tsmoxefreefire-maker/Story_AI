# الـ API تبع الموديول 6 (FastAPI)

> هاد الملف جديد ومستقل. كل كود الـ API ملفات جديدة، وما تغيّر ولا ملف قديم.
> **خطة** = مكتوب بالـ spec (`00_shared.md`). **اجتهاد** = قرار مني مش مكتوب بالخطة.

---

## 1. الفكرة

- طبقة FastAPI فوق الموديول 6: بتستقبل الطلب، بتبني القصة بالخلفية (S6 لـ S10)، وبعدها S12 بينشرها، وبتعطيك رابط تشغيل.
- **خطة:** العقد بـ §3، المتغيرات بـ §11.1، حد الـ 1 MB بـ §11.3، أكواد الأخطاء بـ §12.1.
- **اجتهاد:** الـ API بالأصل جزء مشترك (milestones M0 و M7 و M8)، وبنيناه جوّا الموديول 6 لأنك طلبت إنه شغلنا دايماً FastAPI.

---

## 2. كيف تشغّله (PyCharm، PowerShell)

```powershell
pip install -r requirements.txt
$env:API_KEYS = "dev-key-123"
python scripts/run_api.py
```

1. افتح `http://localhost:8000/docs`.
2. اكبس **Authorize** واكتب `dev-key-123`.
3. جرّب `POST /v1/dev/stories` بالـ body هاد: `{"folder": "plants"}`.
4. انسخ الـ `story_id` من الرد، وافتح `GET /v1/stories/{story_id}` لحد ما الحالة تصير `published`.
5. اعمل `POST /v1/stories/{story_id}/access-link`، وافتح الـ `url` اللي برجع بالمتصفح.

- `dev-key-123` مفتاح تجربة على جهازك بس. على السيرفر بتحط مفاتيح حقيقية بمتغيرات البيئة، وما بتنحط بملف ولا على GitHub.
- إذا ما حطيت `PLAY_TOKEN_SECRET`، السكربت بيعمل واحد عشوائي لهاي التشغيلة بس، فروابط التشغيل بتبطل تشتغل لما تطفي السيرفر.
- إذا ما حطيت `PUBLIC_BASE_URL` و `ALLOWED_EMBED_ORIGINS`، السكربت بيحط قيم جهازك (`http://localhost:8000` و `'self'`).

**الفحوصات:**

```powershell
pytest tests\unit\test_api_core.py tests\unit\test_api_routes.py
```

- `test_api_core.py`: الأجزاء اللي ما بتحتاج FastAPI. نجحت كلها عندي (20).
- `test_api_routes.py`: بتمرّ على الـ routes نفسها بـ FastAPI TestClient. **ما قدرت أشغّلها عندي**، لأنه ما في إنترنت لتنزيل FastAPI. لازم تشتغل عندك، وإذا FastAPI مش منزّل بتنعمل skip.

---

## 3. الـ endpoints

| الطلب | شو بيعمل | المصدر |
| --- | --- | --- |
| `POST /v1/stories` | بيفحص الطلب، وبعدين بيرجع 501 `NOT_CONNECTED` لأنه الموديولات 1–5 مش موصولة | العقد خطة، والـ 501 اجتهاد |
| `POST /v1/dev/stories` | بيبني مجلد قصة من `stories/` وبينشره: 202 لقصة جديدة، 200 لنفس الطلب مرة ثانية | اجتهاد (بديل الموديولات 1–5) |
| `GET /v1/dev/folders` | أسماء مجلدات القصص اللي بتقدر تبنيها | اجتهاد |
| `GET /v1/stories/{id}` | الحالة والتقدّم والنتيجة والمشاكل | خطة |
| `GET /v1/stories` | قائمة، مع فلاتر `lesson_id` و `topic_id` و `book_id` و `status` و `limit` و `cursor` | خطة |
| `POST /v1/stories/{id}/access-link` | رابط تشغيل موقّع، مدته الافتراضية 6 ساعات وأقصاها 24 | خطة |
| `GET /v1/play/{token}/index.html` | القصة نفسها. بدون مفتاح، الـ token هو الإذن، ومع هيدرات الحماية بالحرف | خطة |
| `GET /v1/play/{token}/audio/{file}` | ملف صوت. بيدعم `Range` عشان المشغّل يقدّم ويرجّع | الملف خطة، والـ Range اجتهاد |
| `GET /v1/stories/{id}/package` | ZIP فيه `index.html` و `audio/` و `meta.json` للاستخدام بدون نت | خطة |
| `POST /v1/stories/{id}/comments` | تعليق مراجع، بيستنى بحالة `open` | خطة (التعديل نفسه بدو LLM، milestone M1) |
| `POST /v1/stories/{id}/versions/{n}/approve` | بينشر نسخة `needs_review`. بيرفض إذا فيها blocker | خطة |
| `POST /v1/stories/{id}/rollback` | بيرجّع نسخة قديمة منشورة | خطة |
| `POST /v1/stories/{id}/regenerate` | نسخة جديدة. 409 `STORY_BUSY` إذا في نسخة شغّالة | خطة |
| `GET /v1/health` | هل التخزين شغّال، وهل ffmpeg موجود. بدون مفتاح | خطة |

كل الطلبات تحت `/v1` بدها `X-API-Key`، إلا `/v1/play/...` و `/v1/health`.

---

## 4. الملفات: كل ملف وشو بيستخدم

> ولا ملف فيهم بيستخدم LLM. الـ API بيشغّل الموديول 6 بس.

| الملف | شو فيه | بيستخدم |
| --- | --- | --- |
| `api/app.py` | `create_app()` بتجمع كل إشي: الإعدادات، والخدمة، والـ routes، ومعالجات الأخطاء، و `/v1/health`. أي خطأ بيطلع بشكل `{"error": …}` | FastAPI |
| `api/routes_stories.py` | routes القصص: كل route بيقرأ الطلب، وبينادي `StoryService`، وبيرجّع الرد | FastAPI |
| `api/routes_play.py` | التشغيل والصوت، مع هيدرات الحماية والـ Range | FastAPI |
| `api/auth.py` | `require_api_key`: بيفحص المفتاح، وبعدين بيعدّ الطلب على حد الدقيقة | FastAPI (`APIKeyHeader`) |
| `api/service.py` | `StoryService`: الشغل كله. بيعمل القصة، وبيبنيها بالخلفية، وبعدين S12، والرابط، والـ ZIP، والتعليقات، و approve و rollback و regenerate | Python عادي + الموديول 6 |
| `api/schemas.py` | شكل كل body، ومين بيرفض الحقول الزايدة والقيم الغلط | Pydantic |
| `api/tokens.py` | `make_token` و `read_token`: توقيع رابط التشغيل وفحصه (مزوّر، منتهي، أو خربان) | Python عادي (HMAC-SHA256) |
| `api/headers.py` | `play_headers` (هيدرات §3 بالحرف) و `parse_range` | Python عادي |
| `api/limits.py` | `RateLimiter` (حد الطلبات بالدقيقة لكل مفتاح) و `BodyLimit` (أي body أكبر من 1 MB بينرفض قبل ما ينحفظ بالذاكرة) | Python عادي (ASGI) |
| `api/settings.py` | `ApiSettings.from_env()`: بتقرأ متغيرات البيئة بس، وما بتطبع الأسرار أبداً | Python عادي |
| `api/errors.py` | `ApiError`: فيه الـ status والكود والرسالة | Python عادي |
| `scripts/run_api.py` | بيشغّل السيرفر محلياً بـ uvicorn | uvicorn |

**كيف بتمشي القصة جوّا `service.py`:**

1. `create_from_folder` بتعمل القصة والنسخة 1 بحالة `queued`. نفس الطلب مرة ثانية بيرجّع نفس القصة.
2. `run_version` بتشتغل بالخلفية. بتحوّل النسخة لـ `running`، وبتبني S6 لـ S10، وبتحط الـ bundle بالتخزين، وبعدين S12 بيقرر `published` أو `needs_review`.
3. إذا مرحلة استسلمت، النسخة بتصير `needs_review` ومعها مشاكلها.
4. إذا صار خطأ بالكود، النسخة بتصير `failed`، وما بتضل `running` أبداً.

---

## 5. أكواد الأخطاء

| الكود | HTTP | متى | المصدر |
| --- | --- | --- | --- |
| `UNAUTHORIZED` | 401 | مفتاح ناقص أو غلط، أو token تشغيل مزوّر أو منتهي | خطة |
| `INPUT_INVALID` | 422 | body غلط، أو مجلد مش موجود، أو body أكبر من 1 MB، أو تعليق وقته برّا القصة | خطة |
| `INPUT_INVALID` | 416 | `Range` برّا ملف الصوت | اجتهاد |
| `NOT_FOUND` | 404 | قصة أو نسخة أو ملف مش موجود، أو قصة لسا ما إلها نسخة منشورة | خطة |
| `STORY_BUSY` | 409 | في نسخة `queued` أو `running` | خطة |
| `RATE_LIMITED` | 429 | أكثر من `RATE_LIMIT_RPM` طلب بالدقيقة (الافتراضي 120)، ومعه هيدر `Retry-After` | خطة |
| `INVALID_VERSION_STATE` | 409 | approve لنسخة مش `needs_review` أو فيها blocker، أو rollback لنسخة ما بتنفع | اجتهاد |
| `NOT_CONNECTED` | 501 | `POST /v1/stories` قبل ما تنوصل الموديولات 1–5 | اجتهاد |
| `INTERNAL` | 500 | خطأ بالكود. الرسالة عامة، والتفاصيل بتروح للـ log بس | الكود خطة، والـ 500 اجتهاد |

---

## 6. قرارات (اجتهاد)

1. **التخزين بالذاكرة:** القصص بتنحفظ بالذاكرة لحد ما تجهز قاعدة البيانات (M0). لما تطفي السيرفر بتروح القصص، بس الـ bundles بتضل بـ `BLOB_DIR`.
2. **`/v1/dev/stories`:** بديل مؤقت للموديولات 1–5. بياخذ مجلد قصة جاهز (`plants` أو `percent` أو `clock`) ويبنيه.
3. **Range للصوت:** عشان المشغّل يقدّم ويرجّع جوّا السطر.
4. **حد الطلبات وحد الـ 1 MB بالذاكرة:** كل سيرفر بيعدّ لحاله. إذا صار في أكثر من سيرفر، بيصير بدها Redis أو proxy.
5. **مقارنة المفاتيح والتوقيع بالـ bytes وبـ `compare_digest`:** عشان أي حرف غريب بالهيدر يرجّع 401 مش 500، وعشان الوقت ما يفضح المفتاح.
6. **`/docs` فيه زر Authorize:** بتكتب المفتاح مرة وحدة بس.

---

## 7. شو لسا ناقص (مش شغل الموديول 6)

- **قاعدة البيانات** (`DATABASE_URL`، milestone M0): لسا بالذاكرة.
- **إرسال الـ webhooks** (`WEBHOOK_SECRET`، M7): S12 بيجهّزها، بس ما في worker يبعثها.
- **التخزين على S3** (`BLOB_BACKEND=s3`): هلّق بس local.
- **`POST /v1/stories` الحقيقي:** بدو الموديولات 1–5 (S0 لـ S5).
- **التعليق يعمل تعديل لحاله:** بدو الـ LLM تبع المخطِّط (M1).
- **`media-src`:** بنستخدم `'self'`، واشتغل الصوت فيه على Chromium. إذا علّق على Safari، الخطة بتقول نحط `PUBLIC_BASE_URL` بداله.
