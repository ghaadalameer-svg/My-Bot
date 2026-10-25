"""
قوالب الإعداد السريع: رتب + تصنيفات + رومات + صلاحيات + (للقوالب الجاهزة) تذاكر وتقديمات وتحقق.
إضافة فقط: لا يحذف ولا يعدّل أي شيء موجود، وإذا شغّلته مرتين ما يكرر اللي انعمل.
"""
import asyncio
import logging

import discord

import security
from eventlog import LOG_CATEGORIES
from systems import PanelError, publish_panel

log = logging.getLogger("shield.setup")
_running: set[int] = set()


class SetupError(Exception):
    pass


RULES = (
    "1️⃣ احترام جميع الأعضاء، وممنوع السب والتنمر.\n"
    "2️⃣ ممنوع السبام والإعلانات ونشر روابط الدعوات.\n"
    "3️⃣ ممنوع أي محتوى مخالف أو غير لائق.\n"
    "4️⃣ استخدم كل روم في موضوعه.\n"
    "5️⃣ قرارات الإدارة نهائية، ولأي استفسار تواصل مع المشرفين.\n\n"
    "بدخولك السيرفر فأنت موافق على هذه القوانين."
)
RP_RULES = (
    "1️⃣ احترام جميع اللاعبين والإدارة، وممنوع السب والتنمر.\n"
    "2️⃣ ممنوع الخروج عن الدور (Meta Gaming / Power Gaming) أثناء اللعب.\n"
    "3️⃣ ممنوع (RDM / VDM / Fail RP) وقتل اللاعبين بدون سبب رول بلاي.\n"
    "4️⃣ ممنوع استغلال الثغرات أو استخدام الهاكات.\n"
    "5️⃣ ممنوع السبام والإعلانات ونشر روابط الدعوات.\n"
    "6️⃣ للمشاكل افتح تذكرة، وقرارات الإدارة نهائية.\n\n"
    "بدخولك السيرفر فأنت موافق على هذه القوانين."
)
WELCOME = "أهلاً بك في السيرفر! 👋\nاقرأ القوانين، وتعرّف على الأعضاء، واستمتع بوقتك."
HOWTO = ("1️⃣ اقرأ القوانين.\n2️⃣ اضغط زر التحقق في روم ✅┃التحقق عشان تظهر لك باقي الرومات.\n"
         "3️⃣ قدّم على الوظيفة أو الوايت ليست من روم التقديم.\n4️⃣ لأي مشكلة افتح تذكرة.")


def role(key, name, color, staff=False, hoist=False, perms=None):
    return {"key": key, "name": name, "color": color, "staff": staff, "hoist": hoist, "perms": perms or {}}


def ch(name, topic=None, readonly=False, post=None, log_channel=False, panel=None, review=False, logcat=None):
    return {"type": "text", "name": name, "topic": topic, "readonly": readonly, "post": post,
            "log": log_channel, "panel": panel, "review": review, "logcat": logcat}


def vc(name, limit=0):
    return {"type": "voice", "name": name, "limit": limit, "readonly": False}


def cat(name, channels, staff=False, gate=False, allow=None, tickets=False):
    return {"name": name, "channels": channels, "staff": staff, "gate": gate,
            "allow": allow or [], "tickets": tickets}


ADMIN_PERMS = dict(manage_messages=True, kick_members=True, ban_members=True,
                   moderate_members=True, manage_nicknames=True, view_audit_log=True)
MOD_PERMS = dict(manage_messages=True, kick_members=True, moderate_members=True, manage_nicknames=True)
SUPPORT_PERMS = dict(manage_messages=True, moderate_members=True)

BASE_ROLES = [
    role("admin", "الإدارة", 0xE74C3C, staff=True, hoist=True, perms=ADMIN_PERMS),
    role("mod", "المشرفون", 0x3498DB, staff=True, hoist=True, perms=MOD_PERMS),
    role("vip", "عضو مميز", 0xF1C40F),
]
INFO = cat("📌 معلومات", [
    ch("📜┃القوانين", "قوانين السيرفر", True, ("📜 قوانين السيرفر", RULES)),
    ch("📢┃الإعلانات", "إعلانات السيرفر", True),
    ch("👋┃الترحيب", "أهلاً بالأعضاء الجدد", True, ("👋 أهلاً بك!", WELCOME)),
])
STAFF = cat("🔒 الإدارة", [
    ch("💬┃دردشة-الإدارة"),
    ch("📜┃السجل", "سجل أحداث Discord Shield", log_channel=True),
    vc("🔊 اجتماع الإدارة"),
], staff=True)
LOGS_CAT = cat("📜 السجلات", [ch(n, t, logcat=k) for k, (n, t) in LOG_CATEGORIES.items()], staff=True)

# ─── لبنات قوالب السيرفرات الجاهزة (RP) ───
def rp_roles(extra=None):
    return ([role("admin", "الإدارة", 0xE74C3C, True, True, ADMIN_PERMS),
             role("mod", "المشرفون", 0x3498DB, True, True, MOD_PERMS),
             role("support", "الدعم الفني", 0x2ECC71, True, False, SUPPORT_PERMS)]
            + (extra or []) + [role("citizen", "مواطن", 0x95A5A6), role("vip", "عضو مميز", 0xF1C40F)])


RP_INFO = cat("📌 معلومات", [
    ch("📜┃القوانين", "قوانين السيرفر", True, ("📜 قوانين السيرفر", RP_RULES)),
    ch("📢┃الإعلانات", "إعلانات السيرفر", True),
    ch("📖┃طريقة-الدخول", "كيف تدخل السيرفر", True, ("📖 طريقة الدخول", HOWTO)),
])
VERIFY_CAT = cat("✅ التحقق", [ch("✅┃التحقق", "اضغط الزر عشان تدخل السيرفر", True, panel="verify")])
SUPPORT_CAT = cat("🎫 الدعم", [
    ch("🎫┃فتح-تذكرة", "افتح تذكرة للدعم", True, panel="tickets"),
    ch("❓┃الأسئلة-الشائعة", "إجابات الأسئلة المتكررة", True),
    ch("🐞┃الإبلاغ-عن-باق", "بلّغ عن أي خلل بالسيرفر"),
], gate=True)
TICKETS_CAT = cat("🎟️ التذاكر المفتوحة", [], staff=True, tickets=True)
RP_STAFF = cat("🔒 الإدارة", [
    ch("💬┃دردشة-الإدارة"),
    ch("📜┃السجل", "سجل أحداث Discord Shield", log_channel=True),
    ch("📥┃طلبات-التقديم", "هنا تظهر طلبات التقديم للقبول أو الرفض", review=True),
    vc("🔊 اجتماع الإدارة"),
], staff=True)


def apply_cat(extra_channels=None):
    return cat("🧾 التقديمات", [ch("🧾┃التقديم", "اختر القسم اللي تبي تقدم عليه", True, panel="apply"),
                              ch("📋┃نتائج-التقديم", None, True)] + (extra_channels or []), gate=True)


WL_FORM = {"name": "وايت ليست", "role_key": "wl",
           "questions": ["اسمك الحقيقي", "عمرك", "اسم شخصيتك", "قصة شخصيتك", "خبرتك في الرول بلاي"]}
CHAT_CAT = cat("💬 المجتمع", [ch("💬┃الدردشة-العامة"), ch("📷┃الصور"), ch("🎬┃المقاطع"), ch("💡┃الاقتراحات")], gate=True)
VOICE_CAT = cat("🔊 الصوتيات", [vc("⏳ انتظار الدخول"), vc("🔊 عام 1"), vc("🔊 عام 2"), vc("🎵 موسيقى")], gate=True)

TEMPLATES = {
    "community": {
        "name": "مجتمع عام",
        "description": "سيرفر مجتمع عادي: معلومات، دردشة، صور، أوامر البوتات، رومات صوتية، وقسم للإدارة.",
        "roles": BASE_ROLES,
        "categories": [
            INFO,
            cat("💬 المجتمع", [ch("💬┃الدردشة-العامة"), ch("📷┃الصور"), ch("🤖┃الأوامر")]),
            cat("🔊 الصوتيات", [vc("🔊 عام 1"), vc("🔊 عام 2"), vc("🎵 الموسيقى")]),
            LOGS_CAT, STAFF,
        ],
    },
    "gaming": {
        "name": "مجتمع ألعاب",
        "description": "للاعبين: نقاش الألعاب، البحث عن فريق، المقاطع، وغرف صوتية للفرق (حد 5 أشخاص).",
        "roles": BASE_ROLES,
        "categories": [
            INFO,
            cat("💬 المجتمع", [ch("💬┃الدردشة-العامة"), ch("🎥┃المقاطع"), ch("🤖┃الأوامر")]),
            cat("🎮 الألعاب", [ch("🎮┃نقاش-الألعاب"), ch("🔍┃البحث-عن-فريق"), ch("🏆┃الإنجازات")]),
            cat("🔊 غرف اللعب", [vc("🎮 فريق 1", 5), vc("🎮 فريق 2", 5), vc("🎮 فريق 3", 5),
                                vc("🔊 دردشة صوتية")]),
            LOGS_CAT, STAFF,
        ],
    },
    "support": {
        "name": "متجر ودعم فني",
        "description": "لمتجر أو خدمة: قسم للمنتجات والتقييمات، وقسم للدعم والأسئلة الشائعة، وقسم للإدارة.",
        "roles": BASE_ROLES,
        "categories": [
            INFO,
            cat("🛒 المتجر", [ch("🛍️┃المنتجات", "عرض المنتجات والخدمات", True),
                             ch("⭐┃التقييمات"), ch("💬┃الدردشة")]),
            cat("🎫 الدعم", [ch("❓┃الأسئلة-الشائعة", "إجابات الأسئلة المتكررة", True),
                            ch("🆘┃طلب-دعم", "اكتب مشكلتك وبيرد عليك أحد الفريق")]),
            LOGS_CAT, STAFF,
        ],
    },
    "vrp": {
        "name": "سيرفر VRP (فايف إم)",
        "description": "سيرفر VRP كامل: تحقق، وايت ليست، وظائف (شرطة/إسعاف/ميكانيك)، اقتصاد، تذاكر، وصوتيات.",
        "verify_role": "citizen",
        "roles": rp_roles([role("wl", "مقبول وايت ليست", 0x1ABC9C, False, True),
                           role("police", "شرطة", 0x1F618D, False, True),
                           role("ems", "إسعاف", 0xE91E63, False, True),
                           role("mech", "ميكانيكي", 0xE67E22, False, True)]),
        "forms": [WL_FORM],
        "categories": [
            RP_INFO, VERIFY_CAT, CHAT_CAT,
            cat("🎮 السيرفر", [ch("📰┃أخبار-السيرفر", None, True), ch("🚗┃معرض-السيارات"),
                              ch("🏠┃العقارات"), ch("💰┃الاقتصاد-والتحويلات")], gate=True),
            cat("💼 الوظائف", [ch("👮┃وظيفة-الشرطة"), ch("🚑┃وظيفة-الإسعاف"),
                              ch("🔧┃وظيفة-الميكانيك"), ch("🏪┃التجار")], gate=True),
            apply_cat(), SUPPORT_CAT, VOICE_CAT, TICKETS_CAT, LOGS_CAT, RP_STAFF,
        ],
    },
    "rp": {
        "name": "سيرفر رول بلاي RP",
        "description": "رول بلاي عام: تحقق، وايت ليست، بطاقات وقصص الشخصيات، بلاغات، أخبار، تذاكر، وصوتيات.",
        "verify_role": "citizen",
        "roles": rp_roles([role("wl", "مقبول وايت ليست", 0x1ABC9C, False, True)]),
        "forms": [WL_FORM],
        "categories": [
            RP_INFO, VERIFY_CAT, CHAT_CAT,
            cat("🎭 الشخصيات", [ch("🪪┃بطاقات-الشخصيات"), ch("📖┃قصص-الشخصيات"),
                               ch("📰┃الأخبار-الرسمية", None, True)], gate=True),
            cat("🚨 البلاغات", [ch("🚓┃بلاغات-الشرطة"), ch("🚑┃بلاغات-الإسعاف"), ch("⚖️┃الشكاوى")], gate=True),
            apply_cat(), SUPPORT_CAT, VOICE_CAT, TICKETS_CAT, LOGS_CAT, RP_STAFF,
        ],
    },
    "cfw": {
        "name": "سيرفر CFW (عصابات وفصائل)",
        "description": "سيرفر عصابات وفصائل: تسجيل العصابات، إعلان الحروب، التحالفات، مناطق النفوذ، وغرف صوتية.",
        "verify_role": "citizen",
        "roles": rp_roles([role("gleader", "قائد عصابة", 0xC0392B, False, True),
                           role("gmember", "عضو عصابة", 0x7B241C, False, True),
                           role("police", "شرطة", 0x1F618D, False, True)]),
        "forms": [{"name": "تسجيل عصابة", "role_key": "gleader",
                   "questions": ["اسم العصابة", "اسم القائد", "عدد الأعضاء", "هدف العصابة", "خبرتكم في الأحداث"]}],
        "categories": [
            RP_INFO, VERIFY_CAT, CHAT_CAT,
            cat("🏴 العصابات", [ch("📜┃قوانين-العصابات", None, True), ch("📣┃إعلانات-العصابات"),
                               ch("⚔️┃إعلان-الحروب"), ch("🤝┃التحالفات"), ch("🗺️┃مناطق-النفوذ")], gate=True),
            cat("🔫 الأحداث", [ch("💥┃أحداث-السيرفر", None, True), ch("🏆┃نتائج-الحروب", None, True)], gate=True),
            cat("🔊 غرف العصابات", [vc("🏴 عصابة 1"), vc("🏴 عصابة 2"), vc("🚔 الشرطة"), vc("🔊 عام")], gate=True),
            apply_cat(), SUPPORT_CAT, TICKETS_CAT, LOGS_CAT, RP_STAFF,
        ],
    },
    "police": {
        "name": "مود شرطة",
        "description": "مجتمع شرطة رول بلاي: مركز الشرطة، لاسلكي، قيادة، تدريب، بلاغات، وتقديم على الشرطة.",
        "verify_role": "citizen",
        "roles": rp_roles([role("pcmd", "قيادة الشرطة", 0x1B2631, False, True),
                           role("pofficer", "ضابط", 0x2E86C1, False, True),
                           role("ptrainee", "متدرب", 0x85C1E9, False, True)]),
        "forms": [{"name": "التقديم على الشرطة", "role_key": "ptrainee",
                   "questions": ["اسمك الحقيقي", "عمرك", "اسم شخصيتك", "خبرتك في الشرطة", "ليش تبي تنضم؟"]}],
        "categories": [
            RP_INFO, VERIFY_CAT,
            cat("🚓 مركز الشرطة", [ch("📢┃إعلانات-الشرطة", None, True), ch("🚨┃بلاغات-الطوارئ"),
                                 ch("📝┃تقارير-الاعتقال"), ch("🚓┃الدوريات"),
                                 ch("📚┃الإجراءات-والدليل", None, True)],
                staff=True, allow=["pcmd", "pofficer", "ptrainee"]),
            cat("🎙️ اللاسلكي", [vc("📻 راديو 1"), vc("📻 راديو 2"), vc("🏢 غرفة العمليات"), vc("🔍 التحقيق")],
                staff=True, allow=["pcmd", "pofficer", "ptrainee"]),
            cat("🎓 التدريب", [ch("📅┃جدول-التدريب", None, True), ch("📝┃الاختبارات")],
                staff=True, allow=["pcmd", "pofficer", "ptrainee"]),
            cat("🛡️ قيادة الشرطة", [ch("💬┃دردشة-القيادة"), ch("📋┃قرارات-القيادة")], staff=True, allow=["pcmd"]),
            cat("💬 المجتمع", [ch("💬┃الدردشة-العامة"), ch("🚨┃إبلاغ-المدنيين")], gate=True),
            apply_cat(), SUPPORT_CAT, TICKETS_CAT, LOGS_CAT, RP_STAFF,
        ],
    },
    "county": {
        "name": "مقاطعة (حكومة ومدينة)",
        "description": "مقاطعة رول بلاي: حكومة وقرارات رسمية، أخبار المدينة، عقارات ووظائف، خدمات الشرطة والإسعاف والإطفاء.",
        "verify_role": "citizen",
        "roles": rp_roles([role("governor", "المحافظ", 0x8E44AD, False, True),
                           role("sheriff", "الشريف", 0x1F618D, False, True),
                           role("deputy", "نائب الشريف", 0x2E86C1, False, True),
                           role("govemp", "موظف حكومي", 0x16A085, False, True)]),
        "forms": [{"name": "وظيفة حكومية", "role_key": "govemp",
                   "questions": ["اسمك الحقيقي", "عمرك", "اسم شخصيتك", "الوظيفة المطلوبة", "خبرتك"]},
                  {"name": "التقديم على الشرطة", "role_key": "deputy",
                   "questions": ["اسمك الحقيقي", "عمرك", "اسم شخصيتك", "خبرتك في الشرطة", "ليش تبي تنضم؟"]}],
        "categories": [
            RP_INFO, VERIFY_CAT, CHAT_CAT,
            cat("🏛️ الحكومة", [ch("📜┃القرارات-الرسمية", None, True), ch("📢┃بيانات-المحافظة", None, True),
                              ch("🗳️┃الانتخابات")], gate=True),
            cat("🏙️ المدينة", [ch("📰┃أخبار-المقاطعة"), ch("🏠┃العقارات"), ch("💼┃الوظائف-المتاحة"),
                              ch("🛒┃الأسواق")], gate=True),
            cat("🛡️ الخدمات", [ch("🚓┃الشرطة"), ch("🚑┃الإسعاف"), ch("🚒┃الإطفاء")], gate=True),
            apply_cat(), SUPPORT_CAT, VOICE_CAT, TICKETS_CAT, LOGS_CAT, RP_STAFF,
        ],
    },
}

PANEL_LABELS = {"verify": "✅ تحقق", "tickets": "🎫 تذاكر", "apply": "🧾 تقديمات"}


def summarize() -> list[dict]:
    out = []
    for k, t in TEMPLATES.items():
        panels = sorted({x["panel"] for c in t["categories"] for x in c["channels"] if x.get("panel")})
        has_logs = any(x.get("logcat") for c in t["categories"] for x in c["channels"])
        out.append({
            "key": k, "name": t["name"], "description": t["description"],
            "features": [PANEL_LABELS[p] for p in panels] + (["📜 سجلات"] if has_logs else []),
            "roles": [{"name": r["name"], "color": f"#{r['color']:06x}", "staff": r["staff"]} for r in t["roles"]],
            "categories": [{
                "name": c["name"], "staff": c["staff"], "gate": c["gate"],
                "private": c["staff"] or bool(c["allow"]),
                "channels": [{"name": x["name"], "type": x["type"], "readonly": x.get("readonly", False)}
                             for x in c["channels"]],
            } for c in t["categories"]],
        })
    return out


def format_report(rep: dict) -> str:
    lines = [
        f"✅ رتب جديدة: {len(rep['roles_created'])} (موجودة مسبقاً: {len(rep['roles_skipped'])})",
        f"✅ تصنيفات جديدة: {len(rep['categories_created'])} (موجودة: {len(rep['categories_skipped'])})",
        f"✅ رومات جديدة: {len(rep['channels_created'])} (موجودة: {len(rep['channels_skipped'])})",
    ]
    if rep["posted"]:
        lines.append(f"📨 رسائل منشورة: {rep['posted']}")
    if rep["panels"]:
        lines.append("🧩 لوحات منشورة: " + "، ".join(rep["panels"]))
    if rep["panel_errors"]:
        lines.append("⚠️ " + " | ".join(rep["panel_errors"]))
    if rep["log_channel"]:
        lines.append(f"📜 تم تعيين قناة السجل: {rep['log_channel']}")
    if rep.get("logs_configured"):
        lines.append(f"📜 تم ربط {rep['logs_configured']} روم سجلات تلقائياً")
    return "\n".join(lines)


def _base_overwrites(guild, me, c, roles, staff_roles, verify_role):
    allow = [roles[k] for k in c["allow"] if k in roles]
    private = c["staff"] or bool(allow)
    gated = bool(c["gate"] and verify_role is not None and not private)
    ov = {}
    if private or gated:
        ov[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        viewers = (staff_roles if (c["staff"] or gated) else []) + allow + ([verify_role] if gated else [])
        for r in viewers:
            ov[r] = discord.PermissionOverwrite(view_channel=True)
        if not me.guild_permissions.administrator:
            ov[me] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    return ov


def _readonly_overwrites(base, guild, me, staff_roles):
    ov = {t: discord.PermissionOverwrite.from_pair(*o.pair()) for t, o in base.items()}
    everyone = ov.get(guild.default_role) or discord.PermissionOverwrite()
    everyone.send_messages = False
    ov[guild.default_role] = everyone
    for r in staff_roles:
        o = ov.get(r) or discord.PermissionOverwrite()
        o.send_messages = True
        ov[r] = o
    if not me.guild_permissions.administrator:
        o = ov.get(me) or discord.PermissionOverwrite()
        o.send_messages = True
        ov[me] = o
    return ov


async def apply_template(guild: discord.Guild, key: str, *, actor, config, post_messages: bool = True) -> dict:
    tpl = TEMPLATES.get(key)
    if not tpl:
        raise SetupError("قالب غير معروف")
    me = guild.me
    gp = me.guild_permissions
    if not (gp.administrator or (gp.manage_channels and gp.manage_roles)):
        raise SetupError("البوت يحتاج صلاحيتي Manage Channels و Manage Roles")
    need = sum(1 + len(c["channels"]) for c in tpl["categories"])
    if len(guild.channels) + need > 500:
        raise SetupError("السيرفر قريب من الحد الأقصى للرومات (500)")
    if len(guild.roles) + len(tpl["roles"]) > 250:
        raise SetupError("السيرفر قريب من الحد الأقصى للرتب (250)")
    if guild.id in _running:
        raise SetupError("فيه عملية إعداد شغالة الحين لهذا السيرفر، انتظر لين تخلص")

    _running.add(guild.id)
    security.quiet.add(guild.id)
    rep = {k: [] for k in ("roles_created", "roles_skipped", "categories_created", "categories_skipped",
                           "channels_created", "channels_skipped", "panels", "panel_errors", "configured")}
    rep["posted"] = 0
    rep["log_channel"] = None
    try:
        await _apply(guild, tpl, rep, actor, config, post_messages)
    except discord.Forbidden:
        done = len(rep["channels_created"]) + len(rep["roles_created"])
        raise SetupError(
            f"توقف الإعداد لأن البوت ما عنده صلاحية كافية (تم إنشاء {done} عنصر قبل التوقف). "
            "أعطِ البوت Manage Channels و Manage Roles وارفع رتبته، ثم أعد التشغيل: "
            "ما راح يكرر اللي انعمل.")
    finally:
        _running.discard(guild.id)
        asyncio.get_running_loop().call_later(6, security.quiet.discard, guild.id)
    return rep


async def _apply(guild, tpl, rep, actor, config, post_messages):
    me = guild.me
    gp = me.guild_permissions
    reason = f"Discord Shield — إعداد سريع بواسطة {actor}"

    roles = {}
    for r in tpl["roles"]:
        existing = discord.utils.get(guild.roles, name=r["name"])
        if existing:
            roles[r["key"]] = existing
            rep["roles_skipped"].append(r["name"])
            continue
        perms = {k: v for k, v in r["perms"].items() if gp.administrator or getattr(gp, k, False)}
        roles[r["key"]] = await guild.create_role(
            name=r["name"], colour=discord.Colour(r["color"]), hoist=r["hoist"],
            permissions=discord.Permissions(**perms), reason=reason)
        rep["roles_created"].append(r["name"])
    staff_roles = [roles[r["key"]] for r in tpl["roles"] if r["staff"]]
    verify_role = roles.get(tpl.get("verify_role"))

    panels, review, tickets_cat, logmap = {}, None, None, {}
    for c in tpl["categories"]:
        base = _base_overwrites(guild, me, c, roles, staff_roles, verify_role)
        category = discord.utils.get(guild.categories, name=c["name"])
        if category:
            rep["categories_skipped"].append(c["name"])
        else:
            category = await guild.create_category(c["name"], overwrites=base, reason=reason)
            rep["categories_created"].append(c["name"])
        if c["tickets"]:
            tickets_cat = category

        for spec in c["channels"]:
            if spec["type"] == "text":
                existing = discord.utils.get(category.text_channels, name=spec["name"].lower().replace(" ", "-"))
            else:
                existing = discord.utils.get(category.voice_channels, name=spec["name"])
            if existing:
                rep["channels_skipped"].append(spec["name"])
                if spec.get("logcat"):
                    logmap[spec["logcat"]] = existing
                continue

            ov = _readonly_overwrites(base, guild, me, staff_roles) if spec["readonly"] else base
            if spec["type"] == "text":
                kwargs = {"topic": spec["topic"]} if spec.get("topic") else {}
                channel = await guild.create_text_channel(
                    spec["name"], category=category, overwrites=ov, reason=reason, **kwargs)
            else:
                channel = await guild.create_voice_channel(
                    spec["name"], category=category, overwrites=ov, reason=reason,
                    user_limit=spec.get("limit", 0))
            rep["channels_created"].append(spec["name"])

            if spec.get("panel"):
                panels[spec["panel"]] = channel
            if spec.get("review"):
                review = channel
            if spec.get("logcat"):
                logmap[spec["logcat"]] = channel

            if post_messages and spec.get("post"):
                title, text = spec["post"]
                try:
                    await channel.send(
                        embed=discord.Embed(title=title, description=text, color=discord.Color.blurple()),
                        allowed_mentions=discord.AllowedMentions.none())
                    rep["posted"] += 1
                except discord.HTTPException:
                    log.warning("تعذر نشر الرسالة في %s", spec["name"])

            if spec.get("log"):
                cfg = config.get(guild.id)
                if not cfg["log_channel_id"]:
                    cfg["log_channel_id"] = channel.id
                    config.update(guild.id, cfg)
                    rep["log_channel"] = spec["name"]

    # ضبط أنظمة التذاكر والتقديم والتحقق للقوالب الجاهزة
    cfg = config.get(guild.id)
    kinds = []
    if "tickets" in panels and tickets_cat:
        t = cfg["tickets"]
        t.update(enabled=True, category_id=tickets_cat.id, support_role_ids=[r.id for r in staff_roles])
        t["panel"]["channel_id"] = panels["tickets"].id
        kinds.append("tickets")
    if "verify" in panels and verify_role:
        v = cfg["verify"]
        v.update(enabled=True, role_id=verify_role.id)
        v["panel"]["channel_id"] = panels["verify"].id
        kinds.append("verify")
    if "apply" in panels and review and tpl.get("forms"):
        a = cfg["applications"]
        a.update(enabled=True, review_channel_id=review.id)
        a["panel"]["channel_id"] = panels["apply"].id
        a["forms"] = [{"id": f"f{i + 1}", "name": f["name"], "questions": f["questions"], "open": True,
                       "role_id": roles[f["role_key"]].id if f.get("role_key") in roles else None}
                      for i, f in enumerate(tpl["forms"])]
        kinds.append("applications")
    if logmap:
        cfg["logs"]["channels"].update({k: c.id for k, c in logmap.items()})
        cfg["logs"]["enabled"] = True
        rep["logs_configured"] = len(logmap)
    if kinds or logmap:
        config.update(guild.id, cfg)
    rep["configured"] = kinds
    if post_messages:
        for k in kinds:
            try:
                await publish_panel(guild, k)
                rep["panels"].append(k)
            except (PanelError, discord.HTTPException) as e:
                rep["panel_errors"].append(f"{k}: {e}")
