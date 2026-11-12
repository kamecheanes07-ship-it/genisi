# دليل ملفات المشروع الكامل

هذه الحزمة هي مشروع Genisi 4 Atlas Release 58.65.6 الكامل، مع برومبت Windows 12 مدمج في التطبيق.

## الملفات الأساسية

- `Dockerfile`: صورة التشغيل؛ تحتوي Node.js 22 وPython/FastAPI، وتنسخ مجلدات `app/` و`static/` فقط.
- `requirements.txt`: مكتبات Python اللازمة للخادم.
- `.env.example`: أسماء متغيرات البيئة مع قيم بديلة غير سرية؛ انسخه محلياً إلى `.env` ولا ترفع الملف الحقيقي.
- `app/main.py`: تطبيق FastAPI ومسارات API.
- `app/genisi_auth.py`: الحسابات والجلسات والتخزين المشفر والاتصال بـ Hugging Face Dataset لبيانات المستخدمين فقط.
- `app/agent.py`: منطق الوكيل وبناء رسائل النموذج واكتشاف طلب Windows 12 وإضافة System Prompt المدمج وأدوات مساحة العمل.
- `app/windows12_prompt.py`: النص الكامل المدمج لمواصفات Windows 12، وهو مصدر البرومبت وقت التشغيل.
- `app/worker_client.py`: واجهة ربط الوكيل بمشغل مساحة العمل.
- `static/index.html`: واجهة Genisi كاملة.
- `tests/test_windows12_prompt.py`: اختبارات التفعيل التلقائي وحقن System Prompt والتحقق من عدم الاعتماد على ملف مرجعي.

## ملفات Windows 12 المساعدة

- `docs/WINDOWS12_ATLAS_SYSTEM_PROMPT.md`: برومبت متخصص إضافي لـ Genisi 4 Atlas.
- `docs/genisi.design.json`: خريطة تصميم مساعدة اختيارية؛ ليست مدخلاً وقت التشغيل ولا يعتمد عليها التفعيل التلقائي.
- `docs/README-AR-PROMPT-PACK.md`: شرح الحزمة الإضافية السابقة.

## ملاحظات مهمة

1. لا يوجد أي مفتاح HF حقيقي داخل المشروع. أدخل `HF_TOKEN` و`SESSION_SECRET` و`WORKSPACE_HASH_SECRET` في Secrets لدى الاستضافة.
2. `GENISI_DATASET_ID` الافتراضي هو `AnesKAM/Genisi-users`.
3. مواصفات Windows 12 مدمجة في كود التطبيق؛ لا تعتمد على ملف HTML أو قراءة Dataset.
4. لا تعتبر نجاح اختبارات الوحدة دليلاً على نجاح النشر في حساب Hugging Face أو مزود النموذج؛ اختبر الأسرار والاتصال الفعلي بعد النشر.
