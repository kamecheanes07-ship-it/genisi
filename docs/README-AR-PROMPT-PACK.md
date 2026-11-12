# مواصفات Windows 12 المدمجة في Genisi 4 Atlas

في إصدار 58.65.6 أصبح System Prompt الكامل جزءاً من كود التطبيق داخل `app/windows12_prompt.py`. النسخة الموجودة هنا للتوثيق والقراءة فقط. لا يحتاج Genisi إلى إرفاق `genisi.html` أو `windows12.html` أو أي ملف عند طلب تصميم Windows 12.

عند اكتشاف طلب Windows 12 أو متابعة مرتبطة به، تستدعي واجهة Atlas المسار المصادق عليه `/api/windows12/prompt`، وتضيف المواصفات كاملة إلى System Prompt المرسل للنموذج. يعيد مسار الوكيل الخلفي إضافة المواصفات مباشرة عند استخدام Backend Agent.

لا يقرأ التطبيق ملف HTML من Hugging Face Dataset لتفعيل وضع التصميم، ولا يدّعي أنه قرأ ملفاً مرجعياً غير موجود. مواصفات النظام تصف Windows 12 Concept Desktop كتجربة ويب تفاعلية، ولا تصفه كمنتج Microsoft رسمي.
