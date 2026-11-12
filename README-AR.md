# Genisi 4 Atlas — Portable Secure Release 58.65.6 (Windows 12 Embedded System Prompt)

هذه النسخة **مستقلة عن Hugging Face Spaces**. يمكن تشغيلها على أي استضافة تدعم Docker أو Python/ASGI (مثل Render أو Railway أو VPS أو أي مزود حاويات). تظل خدمات Hugging Face متصلة بها من جهة الخادم: تُحفظ الحسابات والذاكرة والمحادثات في Dataset الخاصة `AnesKAM/Genisi-users`، ويمكن اختيار Hugging Face Inference Providers لتشغيل نموذج الوكيل.

> GitHub هو مكان لحفظ الكود وإدارة الإصدارات، وليس بحد ذاته خادم FastAPI. GitHub Pages لا يشغّل هذا التطبيق الكامل ولا يحمي أسرار Hugging Face. استخدم GitHub للمستودع، ثم اربطه بخدمة استضافة Backend تدعم Docker. الواجهة والـAPI في هذه الحزمة يعملان من نفس الخادم/النطاق لتبقى Cookies وCSRF آمنة.

## Windows 12 — System Prompt مدمج داخل Genisi 4 Atlas

تم دمج مواصفات تصميم Windows 12 الكاملة مباشرة في كود التطبيق ضمن `app/windows12_prompt.py`، كما أُدرج النص الكامل نفسه مباشرة داخل `static/index.html` ليكون جزءاً من حزمة Genisi 4 Atlas نفسها. لا يحتاج المستخدم إلى إرفاق `genisi.html` أو `windows12.html` أو أي لقطة شاشة أو ملف آخر، ولا يقرأ التطبيق ملفاً من Dataset لتفعيل وضع Windows 12.

آلية العمل:

1. يكتشف Atlas طلب Windows 12 بالعربية أو الإنجليزية، وكذلك طلبات المتابعة القريبة مثل «غيّر شريط المهام» أو «أضف ميزة».
2. يستخدم موجّه المتصفح النص المضمّن مباشرة دون أي طلب شبكة أو قراءة ملف. ويُتاح مسار `/api/windows12/prompt` اختيارياً لخدمة مسار الوكيل الخلفي والتشخيص فقط.
3. يضيف الموجّه نص System Prompt الكامل إلى كل استدعاء للنموذج يتعلق بالمهمة، بما في ذلك استدعاءات متابعة الأدوات؛ لأن واجهة `chat_predict` عديمة الحالة.
4. مسار الوكيل الخلفي `app/agent.py` يضيف البرومبت نفسه مباشرة إلى رسائل النظام عند اكتشاف طلب Windows 12.
5. الرسائل غير المتعلقة بـ Windows 12 لا تحمل البرومبت المتخصص.

المواصفات تصف تجربة Windows 12 Concept Desktop كاملة: الهوية الزرقاء، Mica وAcrylic، الخلفية وسطح المكتب، شاشة القفل، شريط المهام، Start Menu، Quick Settings، مدير النوافذ، و11 تطبيقاً، مع قواعد التنفيذ والاختبارات ومعايير القبول.

مسار التحقق: `GET /api/windows12/prompt/status`؛ ويعرض حالة البرومبت المضمّن دون قراءة بيانات المستخدم. يعرض `embedded=true` و`attachment_required=false` وإصدار البرومبت وبصمته، وليس محتوى ملف مرجعي.

**مهم:** Windows 12 هنا مفهوم تصميمي تفاعلي وليس نظاماً رسمياً صادراً عن Microsoft. البرومبت يوجه النموذج لإنشاء الواجهة من المواصفات المدمجة ولا يدّعي مطابقة ملف خارجي.

## البنية

```text
Browser
  │ HTTPS (same origin)
  ▼
Genisi FastAPI + HTML + Node.js workspace (Docker)
  ├── Hugging Face Hub Dataset API
  │     └── AnesKAM/Genisi-users (private, encrypted records)
  ├── Hugging Face Inference Providers (optional model provider)
  └── Qwen-compatible API (optional alternative)
```

## الملفات

```text
Dockerfile
requirements.txt
.env.example
README.md
README-AR.md
app/
  __init__.py
  main.py
  genisi_auth.py
  agent.py
  worker_client.py
  windows12_prompt.py # Full built-in Windows 12 design System Prompt
static/
  index.html
```

## أسرار Hugging Face — لا تعتمد على Spaces

أنشئ Fine-grained token جديدًا من Hugging Face، بصلاحية الكتابة إلى Dataset `AnesKAM/Genisi-users` فقط (وأضف صلاحية Inference Providers إذا أردت تشغيل النموذج عبر Hugging Face). خزّنه في صفحة Environment/Secrets لدى مزود الاستضافة باسم `HF_TOKEN`.

**لا تضع الرمز في HTML أو JavaScript أو GitHub repository أو أي ملف `.env` ترفعه إلى Git.** ملف `.env.example` يوضح أسماء المتغيرات فقط ولا يحتوي أسرارًا.

إذا كان الرمز الذي أرسلته سابقًا ما زال صالحًا، ألغِه وأنشئ رمزًا جديدًا؛ اعتبره مكشوفًا.

## متغيرات البيئة

### مطلوبة

| الاسم | القيمة |
|---|---|
| `HF_TOKEN` | Fine-grained token خادمي بصلاحية الكتابة على Dataset الخاصة |
| `SESSION_SECRET` | سر عشوائي ثابت لا يقل عن 32 حرفًا |
| `WORKSPACE_HASH_SECRET` | سر عشوائي مستقل وثابت لمساحات المشاريع |
| `GENISI_DATASET_ID` | `AnesKAM/Genisi-users` |
| `ENVIRONMENT` | `production` |
| `COOKIE_SECURE` | `1` عند استخدام HTTPS (موصى به دائمًا في الإنتاج) |
| `COOKIE_SAMESITE` | `none` عند تشغيل Space داخل إطار Hugging Face؛ يتطلب `COOKIE_SECURE=1` |

### اختيارية للوكيل

إما استخدام Hugging Face Inference Providers:

| الاسم | القيمة |
|---|---|
| `HF_INFERENCE_MODEL` | معرّف نموذج متاح لحسابك عبر Inference Providers، بصيغة المزود/النموذج التي تدعمها الخدمة |

في هذه الحالة يستخدم الخادم `HF_TOKEN` نفسه للاتصال بـ `https://router.huggingface.co/v1`، ولا يرسل الرمز إلى المتصفح.

أو استخدم مزود Qwen/OpenAI-compatible منفصلًا:

| الاسم | القيمة |
|---|---|
| `QWEN_BASE_URL` | عنوان API متوافق مع OpenAI |
| `QWEN_API_KEY` | مفتاح المزود |
| `QWEN_MODEL` | اسم النموذج لدى المزود |
| `QWEN_DISABLE_THINKING` | `false` افتراضيًا؛ اجعله `true` لإيقاف التفكير إذا كان مزودك لا يدعمه |

إذا لم تُضبط إعدادات أحد مزودي النماذج، تبقى المصادقة والتخزين عبر Hugging Face متاحين، لكن مسار Agent الخادمي لن يستطيع استدعاء نموذج حتى تضبط مزودًا.

### متغيرات اختيارية أخرى

| الاسم | الافتراضي/المعنى |
|---|---|
| `GENISI_POW_BITS` | `17`؛ صعوبة إثبات العمل (بين 16 و22) |
| `SANDBOX_MODE` | `local` لتفعيل مشغل مساحة العمل المدمج |
| `SANDBOX_OWNER_EMAIL` | بريد المالك الوحيد المسموح له باستخدام Terminal/Next.js المحليين |
| `GENISI_WORKSPACES_DIR` | `/tmp/genisi-workspaces` |
| `PORT` | `8000` افتراضيًا؛ مزود الاستضافة قد يمرر قيمة مختلفة |

ولّد الأسرار محليًا (مرتين، بقيمتين مختلفتين):

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

لا تغيّر `SESSION_SECRET` بعد بدء إنشاء الحسابات؛ فهو يوقّع الجلسات ويُشتق منه مفتاح تشفير سجلات المستخدمين. فقدانه يعني عدم القدرة على فك سجلات الحسابات المشفرة. احتفظ بنسخة آمنة منه خارج المستودع.

## النشر على أي Docker host

1. ارفع مجلد المشروع إلى GitHub repository خاص أو عام بعد التأكد من عدم وجود أي أسرار فيه.
2. أنشئ Web Service/Container من المستودع واختر Dockerfile الموجود في الجذر.
3. أدخل متغيرات البيئة والأسرار في لوحة مزود الاستضافة، لا في الكود.
4. اجعل الخدمة تستخدم المنفذ الذي يمرره المزود عبر `PORT`؛ التطبيق يستمع على `0.0.0.0`.
5. فعّل HTTPS واضبط `COOKIE_SECURE=1` و`COOKIE_SAMESITE=none` (مهم عند فتح Space داخل واجهة Hugging Face المضمنة).
6. اضبط `SANDBOX_OWNER_EMAIL` على البريد الذي أنشأت به حساب المالك؛ مساحة العمل والـTerminal مقيّدان بهذا الحساب.
7. بعد نجاح التشغيل، افتح `/api/auth/health`. يجب أن يرجع `200` مع `ok: true` و`private: true`.
8. اختبر إنشاء الحساب، ثم تسجيل الخروج والدخول مجددًا. إذا استخدمت Hugging Face Space فافتحه مباشرة في تبويب جديد عند منع المتصفح لملفات الطرف الثالث. بعد الدخول تحقق أن `/api/auth/me` و`/api/history` و`/api/preferences` ترجع `200` وأن الحفظ لا يرجع `403`.
9. افتح `/docs` و`/redoc` و`/openapi.json`؛ يجب أن يرجعا `404` لأن توثيق API معطّل في الإنتاج.

Dockerfile الحالي يبدأ Uvicorn بعامل واحد. لا تغيّر ذلك إلى عدة workers قبل إضافة Redis أو مخزن مشترك لتحديات CAPTCHA ومحددات المعدل.

## تشغيل محلي للتطوير

انسخ `.env.example` إلى `.env`، وأدخل القيم الحقيقية محليًا فقط. لا ترفع `.env` إلى Git. ثم:

```bash
docker build -t genisi:58.65.5 .
docker run --env-file .env -p 8000:8000 genisi:58.65.5
```

افتح `http://localhost:8000`. للتطوير المحلي عبر HTTP فقط اضبط `ENVIRONMENT=development` و`COOKIE_SECURE=0`؛ لا تستخدم هذا الإعداد على موقع عام.

## ما يتصل بـ Hugging Face تحديدًا؟

- **Hub Dataset API:** مصدر التخزين الدائم للحسابات والتفضيلات وسجل المحادثات؛ ليس تخزينًا محليًا تابعًا لمضيف التطبيق.
- **Inference Providers:** خيار لتشغيل نموذج الوكيل من خلال Hugging Face Router عند ضبط `HF_INFERENCE_MODEL` ومنح الرمز صلاحية الاستدلال.
- نقل التطبيق بين Render وRailway وVPS وSpaces لا يغير Dataset ID أو السجلات؛ ما دمت تستخدم `HF_TOKEN` نفسه و`SESSION_SECRET` نفسه.

## الأمان والحدود الحالية

- كلمات المرور Argon2id، وسجلات المستخدمين مشفرة AES-GCM، وفهارس البريد/اسم المستخدم تستخدم HMAC.
- Cookie الجلسة `HttpOnly` و`SameSite=None; Secure; Partitioned` لتوافق إطار Hugging Face، مع CSRF double-submit وفحص Origin متوافق مع HTTPS خلف Reverse Proxy.
- تعطيل صفحات `/docs` و`/redoc` و`/openapi.json` في الإنتاج لتقليل كشف سطح API.
- إضافة مسارات توافق واجهة 58.65.5: `/api/chat/stream` و`/api/workspace` و`/api/workspace/file` و`/api/workspace/download` و`/api/agent/state`.
- بث المحادثة يستخدم Server-Sent Events؛ يمرر أجزاء التفكير إذا أعادها مزود النموذج فعليًا، ولا يصطنع أفكارًا غير موجودة.
- اختبار Genisi المخصص يزيد كلفة التسجيل الآلي، لكنه ليس حصانة مطلقة ولا بديلًا عن مراقبة إساءة الاستخدام أو التحقق من البريد.
- لا يوجد حاليًا تدفق تحقق بريد أو استعادة كلمة مرور.
- محددات المعدل والتحديات في ذاكرة عملية واحدة؛ تعدد النسخ أو workers يتطلب Redis/قاعدة بيانات مشتركة.
- Terminal وNext.js المحليان يعملان في حاوية التطبيق نفسها، وليسا عزلًا متعدد المستأجرين. لذلك يظلان مقيدين بـ `SANDBOX_OWNER_EMAIL`. لا تفتح الصلاحية لكل المستخدمين.
- الصور المرجعية محلية لكل حساب على المتصفح ولا تتزامن بين الأجهزة.
- إذا كان Dataset يحتوي على `auth/users.json` قديم، توجد محاولة ترحيل إلى السجلات المشفرة. حذف الملف من أحدث commit لا يمحو النسخ السابقة من تاريخ المستودع؛ أبقِ Dataset خاصة.
- الاختبارات المحلية لا تعادل تدقيق اختراق، ولا تثبت نجاح الاتصال الحي بحساب Hugging Face. افحص `/api/auth/health` بعد إعداد الأسرار.


### إصلاح الجلسات المضمّنة في Hugging Face

- الإنتاج يستخدم `SameSite=None` مع `Secure` و`Partitioned` (عند دعم Starlette) لتعمل Cookies داخل إطار Hugging Face حتى مع حظر Cookies الطرف الثالث التقليدية.
- يرسل المتصفح طلب تحقق إلى `/api/auth/me` مباشرة بعد تسجيل الدخول؛ إذا لم تُحفظ الجلسة تظهر رسالة واضحة بدل بدء التطبيق بحالة دخول وهمية.
- عند منع المتصفح Cookies الطرف الثالث بالكامل، افتح رابط `*.hf.space` مباشرة في تبويب جديد، ثم سجّل الدخول هناك.
- تمت إضافة حارس لإصلاح مؤشر المحادثة القديم، لمنع خطأ `Cannot set properties of undefined (setting 'msgs')` عند إرسال الرسالة.


## اختبارات Windows 12 System Prompt المدمج

شغّل اختبارات كشف الطلبات، وحقن البرومبت الكامل، وتحقق عدم الاعتماد على أي ملف خارجي:

```bash
python -m unittest discover -s tests -v
```

البرومبت المصدر موجود في `app/windows12_prompt.py`، والنسخة المقروءة للبشر في `docs/WINDOWS12_ATLAS_SYSTEM_PROMPT.md`. تُستخدم النسخة المقروءة للتوثيق فقط؛ التطبيق لا يقرأ ملفات docs في وقت التشغيل.
