Discord Shield + لوحة التحكم

أولاً: ملف .env  (انسخ .env.example إلى .env أو أضف الأسطر لملفك الحالي)
   DISCORD_TOKEN=...        (تبويب Bot)
   CLIENT_ID=...            (OAuth2 > Client ID)
   CLIENT_SECRET=...        (OAuth2 > Reset Secret، يظهر مرة واحدة)
   DASHBOARD_URL=http://localhost:8000

ثانياً: في Developer Portal > OAuth2 > Redirects > Add Redirect:
   http://localhost:8000/callback      ثم Save Changes
   (لو نشرتها على دومين: https://دومينك/callback وغيّر DASHBOARD_URL)

ثالثاً: شغّل run.bat ثم افتح  http://localhost:8000

للنشر على VPS: اجعل DASHBOARD_URL بعنوان https، وضع Nginx/Caddy أمام اللوحة،
واترك DASHBOARD_HOST=127.0.0.1 (اللوحة ما تُفتح للإنترنت مباشرة).

──────── تحديث: لوحة بتصميم الشريط الجانبي ────────
الأقسام: نظرة عامة (إحصائيات + صحة السيرفر) | إعداد سريع | الحماية | الأعضاء والإشراف |
الترحيب والمغادرة | الأدوار التلقائية | الرد التلقائي | الإعلانات | الرومات | الرتب
(المستويات والسحوبات والتذاكر: قريباً)
الإحصائيات تُحفظ في stats.json، والإعدادات في config.json (لا تحذفهما).

──────── التحديث الأخير ────────
جديد: التذاكر، التقديمات، التحقق، الحماية المتقدمة (Anti-Nuke / Automod / قفل السيرفر)،
سجلات الأحداث (5 أنواع بروم مخصص لكل نوع)، وقوالب الإعداد السريع: VRP / RP / CFW / مود شرطة / مقاطعة.
مهم: أضف للبوت صلاحية View Audit Log (يحتاجها Anti-Nuke وتفاصيل المنفّذ في السجلات).
