"""
Discord Shield - بوت حماية وإدارة للسيرفر
حماية (فلتر كلمات، سبام، غارات) + ترحيب + رد تلقائي + أدوار تلقائية + سجل + لوحة تحكم
"""
import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import eventlog
import security
import systems
from setup_tools import TEMPLATES, SetupError, apply_template, format_report
from stats import Stats

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not TOKEN or TOKEN.startswith("ضع_"):
    raise SystemExit("ضع توكن البوت في ملف .env أولاً (DISCORD_TOKEN=...)")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("shield")
START_TIME = time.time()

# ───────────────────────── الإعدادات ─────────────────────────
CONFIG_FILE = Path(__file__).with_name("config.json")

DEFAULTS = {
    "log_channel_id": None,
    "banned_words": [],
    "block_invites": False,
    "antispam": {"enabled": True, "max_messages": 5, "per_seconds": 5, "timeout_minutes": 10},
    "antiraid": {"enabled": True, "join_threshold": 5, "per_seconds": 10,
                 "lockdown_seconds": 120, "min_account_age_days": 0},
    "welcome": {"enabled": False, "channel_id": None, "embed": True,
                "message": "أهلاً {user} في {server}! 🎉 أنت العضو رقم {count}"},
    "goodbye": {"enabled": False, "channel_id": None,
                "message": "{username} غادر السيرفر 👋"},
    "autorole": {"enabled": False, "role_id": None},
    "autoreply": {"enabled": False, "rules": []},
    "tickets": {"enabled": False, "category_id": None, "support_role_ids": [], "log_channel_id": None,
                "max_open": 1, "counter": 0,
                "welcome": "أهلاً {user}! اكتب مشكلتك بالتفصيل وبيرد عليك الفريق قريباً.",
                "panel": {"channel_id": None, "title": "🎫 الدعم الفني",
                          "description": "اضغط على الزر لفتح تذكرة وبيرد عليك الفريق."},
                "types": [{"id": "t1", "label": "دعم فني", "emoji": "🛠️"}]},
    "applications": {"enabled": False, "review_channel_id": None, "forms": [],
                     "panel": {"channel_id": None, "title": "🧾 التقديمات",
                               "description": "اختر القسم اللي تبي تقدم عليه واضغط الزر."}},
    "verify": {"enabled": False, "role_id": None, "remove_role_id": None,
               "panel": {"channel_id": None, "title": "✅ التحقق",
                         "description": "اضغط الزر بالأسفل عشان تتحقق وتظهر لك باقي الرومات."}},
    "antinuke": {"enabled": True, "limit": 4, "per_seconds": 10, "action": "strip",
                 "protect_perms": False, "block_new_bots": False, "whitelist_ids": []},
    "automod": {"mention_enabled": True, "mention_max": 5, "links_enabled": False, "links_allow": [],
                "emoji_enabled": False, "emoji_max": 10, "dup_enabled": True, "dup_count": 3},
    "lockdown": {"active": False, "prev": {}},
    "logs": {"enabled": True,
             "channels": {"messages": None, "members": None, "voice": None, "server": None, "mod": None},
             "events": {"message_delete": True, "message_edit": True, "member_join": True, "member_leave": True,
                        "member_update": True, "voice": True, "channel": True, "role": True, "invite": True,
                        "ban": True, "kick": True, "timeout": True, "guild": True}},
}


def _merge(base: dict, extra: dict) -> dict:
    out = json.loads(json.dumps(base))
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self):
        self.data: dict = {}
        if CONFIG_FILE.exists():
            try:
                self.data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.warning("config.json تالف، سيتم البدء من جديد")

    def get(self, guild_id: int) -> dict:
        return _merge(DEFAULTS, self.data.get(str(guild_id), {}))

    def update(self, guild_id: int, cfg: dict):
        self.data[str(guild_id)] = cfg
        CONFIG_FILE.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")


config = Config()
stats = Stats(Path(__file__).with_name("stats.json"))


@tasks.loop(minutes=1)
async def flush_stats():
    stats.flush()


# ───────────────────────── البوت ─────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class ShieldBot(commands.Bot):
    started_at = START_TIME

    async def setup_hook(self):
        self.tree.add_command(shield)
        synced = await self.tree.sync()
        log.info("تمت مزامنة %d أمر سلاش", len(synced))
        flush_stats.start()
        systems.init(config, send_log)
        systems.register(self)

        if os.getenv("CLIENT_ID", "").strip() and os.getenv("CLIENT_SECRET", "").strip():
            try:
                from dashboard import Dashboard
                self.dashboard = Dashboard(self, config, send_log, stats)
                await self.dashboard.start()
            except OSError as e:
                log.error("تعذر تشغيل لوحة التحكم (المنفذ مشغول؟): %s", e)
        else:
            log.info("لوحة التحكم معطلة: أضف CLIENT_ID و CLIENT_SECRET في .env لتفعيلها")

    async def close(self):
        stats.flush()
        await super().close()


bot = ShieldBot(command_prefix="!", intents=intents)

spam_tracker: dict[tuple[int, int], deque] = defaultdict(deque)
join_tracker: dict[int, deque] = defaultdict(deque)
lockdown_until: dict[int, float] = {}
voice_since: dict[tuple[int, int], float] = {}
reply_cooldown: dict[tuple[int, str], float] = {}
recently_kicked: set[tuple[int, int]] = set()
bot_deleted: set[int] = set()

INVITE_RE = re.compile(r"(discord\.gg|discord(?:app)?\.com/invite)/\w+", re.I)
DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u0640]")


def normalize(text: str) -> str:
    text = DIACRITICS_RE.sub("", text.lower())
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ى", "ي"), ("ة", "ه")):
        text = text.replace(a, b)
    return text


async def send_log(guild: discord.Guild, title: str, description: str,
                   color: discord.Color = discord.Color.orange(), category: str = "mod"):
    cfg = config.get(guild.id)
    if not cfg["logs"]["enabled"]:
        return
    channel_id = cfg["logs"]["channels"].get(category) or cfg["log_channel_id"]
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not channel:
        return
    embed = discord.Embed(title=title, description=description[:4000], color=color,
                          timestamp=datetime.now(timezone.utc))
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        log.warning("تعذر الإرسال إلى قناة السجل")


def is_exempt(member: discord.Member) -> bool:
    p = member.guild_permissions
    return p.administrator or p.manage_messages or p.moderate_members


def fmt(template: str, member: discord.Member, mention: bool = True) -> str:
    return (template.replace("{user}", member.mention if mention else member.display_name)
            .replace("{username}", member.display_name)
            .replace("{server}", member.guild.name)
            .replace("{count}", str(member.guild.member_count)))[:2000]


# ───────────────────────── الأحداث ─────────────────────────
@bot.event
async def on_ready():
    log.info("تم تشغيل البوت: %s (في %d سيرفر)", bot.user, len(bot.guilds))
    now = monotonic()
    for g in bot.guilds:
        for vc in g.voice_channels:
            for m in vc.members:
                if not m.bot:
                    voice_since.setdefault((g.id, m.id), now)
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="السيرفر 🛡️"))


async def _moderate(message: discord.Message, member: discord.Member, cfg: dict) -> bool:
    """يرجع True إذا تم التعامل مع الرسالة (حُذفت)."""
    content = normalize(message.content)
    for word in cfg["banned_words"]:
        if normalize(word) in content:
            await _delete(message)
            stats.add(message.guild.id, "mod")
            await _notice(message.channel, f"{member.mention} الكلمة هذه ممنوعة هنا ⚠️")
            await send_log(message.guild, "🚫 كلمة محظورة",
                           f"**العضو:** {member.mention}\n**القناة:** {message.channel.mention}\n"
                           f"**الكلمة:** ||{word}||")
            return True

    if cfg["block_invites"] and INVITE_RE.search(message.content):
        await _delete(message)
        stats.add(message.guild.id, "mod")
        await _notice(message.channel, f"{member.mention} ممنوع نشر روابط الدعوات 🔗")
        await send_log(message.guild, "🔗 رابط دعوة ممنوع",
                       f"**العضو:** {member.mention}\n**القناة:** {message.channel.mention}")
        return True

    verdict = security.automod_check(message, cfg["automod"])
    if verdict:
        label, severe = verdict
        await _delete(message)
        stats.add(message.guild.id, "mod")
        await _notice(message.channel, f"{member.mention} {label} ⚠️")
        action = "حُذفت الرسالة"
        if severe:
            try:
                await member.timeout(timedelta(minutes=cfg["antispam"]["timeout_minutes"]),
                                     reason=f"Discord Shield: {label}")
                action += f" + كتم {cfg['antispam']['timeout_minutes']} دقيقة"
            except discord.HTTPException:
                action += " (تعذر الكتم)"
        await send_log(message.guild, "🤖 فلترة تلقائية",
                       f"**العضو:** {member.mention}\n**القناة:** {message.channel.mention}\n"
                       f"**السبب:** {label}\n**الإجراء:** {action}")
        return True

    sp = cfg["antispam"]
    if sp["enabled"]:
        key = (message.guild.id, member.id)
        now = monotonic()
        q = spam_tracker[key]
        q.append((now, message))
        while q and now - q[0][0] > sp["per_seconds"]:
            q.popleft()
        if len(q) >= sp["max_messages"]:
            msgs = [m for _, m in q]
            q.clear()
            for m in msgs:
                await _delete(m)
            stats.add(message.guild.id, "mod")
            try:
                await member.timeout(timedelta(minutes=sp["timeout_minutes"]), reason="Discord Shield: سبام")
                action = f"تم كتمه {sp['timeout_minutes']} دقيقة"
            except discord.Forbidden:
                action = "تعذر الكتم (تأكد أن رتبة البوت أعلى من رتبة العضو)"
            await _notice(message.channel, f"{member.mention} توقف عن السبام 🛑")
            await send_log(message.guild, "💬 سبام",
                           f"**العضو:** {member.mention}\n**القناة:** {message.channel.mention}\n"
                           f"**الإجراء:** {action}")
            return True
    return False


async def _autoreply(message: discord.Message, cfg: dict):
    ar = cfg["autoreply"]
    if not ar["enabled"] or not ar["rules"] or not message.content:
        return
    text = normalize(message.content).strip()
    for rule in ar["rules"]:
        trig = normalize(rule["trigger"]).strip()
        if not trig:
            continue
        hit = text == trig if rule.get("match") == "exact" else trig in text
        if not hit:
            continue
        key = (message.channel.id, trig)
        now = monotonic()
        if reply_cooldown.get(key, 0) > now:
            return
        if len(reply_cooldown) > 2000:
            reply_cooldown.clear()
        reply_cooldown[key] = now + 5
        try:
            await message.reply(rule["response"][:2000], mention_author=False,
                                allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
        return


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    stats.add(message.guild.id, "messages")
    member = message.author
    cfg = config.get(message.guild.id)
    if isinstance(member, discord.Member) and not is_exempt(member):
        if await _moderate(message, member, cfg):
            return
    await _autoreply(message, cfg)
    await bot.process_commands(message)


async def _antiraid(member: discord.Member, ar: dict) -> bool:
    """يرجع True إذا تم طرد العضو."""
    guild = member.guild
    if not ar["enabled"] or member.bot:
        return False
    now = monotonic()

    if lockdown_until.get(guild.id, 0) > now:
        await _kick_new(member, "وضع الحماية من الغارات مفعّل")
        return True

    min_age = ar["min_account_age_days"]
    if min_age > 0 and datetime.now(timezone.utc) - member.created_at < timedelta(days=min_age):
        await _kick_new(member, f"الحساب أصغر من {min_age} يوم")
        return True

    q = join_tracker[guild.id]
    q.append(now)
    while q and now - q[0] > ar["per_seconds"]:
        q.popleft()
    if len(q) >= ar["join_threshold"]:
        lockdown_until[guild.id] = now + ar["lockdown_seconds"]
        q.clear()
        await send_log(guild, "🚨 تم رصد غارة!",
                       f"دخول {ar['join_threshold']}+ أعضاء خلال {ar['per_seconds']} ثانية.\n"
                       f"تم تفعيل وضع الحماية لمدة {ar['lockdown_seconds']} ثانية.",
                       discord.Color.red())
        await _kick_new(member, "غارة محتملة")
        return True
    return False


@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    cfg = config.get(guild.id)
    stats.add(guild.id, "joins")
    if eventlog.enabled(config, guild, "member_join"):
        await send_log(guild, "📥 عضو جديد",
                       f"{member.mention} — عمر الحساب: <t:{int(member.created_at.timestamp())}:R>",
                       discord.Color.green(), "members")
    if await _antiraid(member, cfg["antiraid"]):
        return
    if member.bot:
        await security.new_bot_guard(bot, config, send_log, stats, guild, member)
        return

    # رتبة تلقائية
    ar = cfg["autorole"]
    if ar["enabled"] and ar["role_id"]:
        role = guild.get_role(ar["role_id"])
        if role and not role.managed and role < guild.me.top_role:
            try:
                await member.add_roles(role, reason="Discord Shield: رتبة تلقائية")
            except discord.HTTPException:
                log.warning("تعذر إعطاء الرتبة التلقائية")

    # رسالة ترحيب
    w = cfg["welcome"]
    if w["enabled"] and w["channel_id"]:
        ch = guild.get_channel(w["channel_id"])
        if isinstance(ch, discord.TextChannel):
            try:
                mentions = discord.AllowedMentions(users=[member], everyone=False, roles=False)
                if w["embed"]:
                    embed = discord.Embed(description=fmt(w["message"], member), color=discord.Color.blurple())
                    embed.set_thumbnail(url=member.display_avatar.url)
                    await ch.send(content=member.mention, embed=embed, allowed_mentions=mentions)
                else:
                    await ch.send(fmt(w["message"], member), allowed_mentions=mentions)
            except discord.HTTPException:
                log.warning("تعذر إرسال رسالة الترحيب")


@bot.event
async def on_member_remove(member: discord.Member):
    guild = member.guild
    stats.add(guild.id, "leaves")
    _nuke(guild, discord.AuditLogAction.kick, member.id, "طرد أعضاء")
    if eventlog.enabled(config, guild, "member_leave"):
        await send_log(guild, "📤 عضو غادر", f"{member} ({member.id})", discord.Color.dark_grey(), "members")
    asyncio.create_task(eventlog.log_kick(config, send_log, guild, member, bot.user.id))
    if (guild.id, member.id) in recently_kicked:
        recently_kicked.discard((guild.id, member.id))
        return
    g = config.get(guild.id)["goodbye"]
    if g["enabled"] and g["channel_id"] and not member.bot:
        ch = guild.get_channel(g["channel_id"])
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(fmt(g["message"], member, mention=False),
                              allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass


@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    if member.bot:
        return
    key = (member.guild.id, member.id)
    if before.channel is None and after.channel is not None:
        voice_since[key] = monotonic()
    elif before.channel is not None and after.channel is None:
        start = voice_since.pop(key, None)
        if start:
            stats.add(member.guild.id, "voice_min", (monotonic() - start) / 60)
    if before.channel != after.channel:
        if before.channel is None:
            title, desc = "🔊 دخول صوتي", f"{member.mention} دخل {after.channel.mention}"
        elif after.channel is None:
            title, desc = "🔇 خروج صوتي", f"{member.mention} خرج من {before.channel.mention}"
        else:
            title, desc = "🔀 انتقال صوتي", f"{member.mention}: {before.channel.mention} ← {after.channel.mention}"
        _ev(member.guild, "voice", "voice", title, desc, discord.Color.dark_teal())


@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    if message.id in bot_deleted:
        bot_deleted.discard(message.id)
        return
    if _is_ticket(message.channel):
        return
    text = (message.content or "[بدون نص]")[:900]
    att = f"\n**مرفقات:** {len(message.attachments)}" if message.attachments else ""
    _ev(message.guild, "message_delete", "messages", "🗑️ رسالة محذوفة",
        f"**العضو:** {message.author.mention}\n**القناة:** {message.channel.mention}{att}\n**المحتوى:** {text}",
        discord.Color.dark_grey())


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if after.author.bot or not after.guild or before.content == after.content or _is_ticket(after.channel):
        return
    _ev(after.guild, "message_edit", "messages", "✏️ رسالة معدّلة",
        f"**العضو:** {after.author.mention}\n**القناة:** {after.channel.mention} — [الرسالة]({after.jump_url})\n"
        f"**قبل:** {before.content[:800] or '—'}\n**بعد:** {after.content[:800] or '—'}", discord.Color.blue())


@bot.event
async def on_bulk_message_delete(messages):
    if not messages or not messages[0].guild:
        return
    m = messages[0]
    _ev(m.guild, "message_delete", "messages", "🧹 حذف رسائل جماعي",
        f"تم حذف **{len(messages)}** رسالة في {m.channel.mention}", discord.Color.dark_grey(),
        discord.AuditLogAction.message_bulk_delete, m.channel.id)


# ───────────────────────── Anti-Nuke + سجلات السيرفر ─────────────────────────
def _nuke(guild, action, target_id, label):
    asyncio.create_task(security.nuke_event(bot, config, send_log, stats, guild, action, target_id, label))


def _ev(guild, key, category, title, desc, color, action=None, target_id=None):
    asyncio.create_task(eventlog.emit(config, send_log, guild, key, category, title, desc, color, action, target_id))


def _is_ticket(channel) -> bool:
    return (getattr(channel, "topic", None) or "").startswith("ticket:")


CH_KIND = {discord.ChannelType.text: "كتابي", discord.ChannelType.voice: "صوتي",
           discord.ChannelType.category: "كاتيجوري", discord.ChannelType.stage_voice: "منصة",
           discord.ChannelType.forum: "منتدى", discord.ChannelType.news: "إعلانات"}


@bot.event
async def on_guild_channel_delete(channel):
    _nuke(channel.guild, discord.AuditLogAction.channel_delete, channel.id, "حذف رومات")
    if not _is_ticket(channel):
        _ev(channel.guild, "channel", "server", "🗑️ حذف روم",
            f"**الاسم:** {channel.name}\n**النوع:** {CH_KIND.get(channel.type, str(channel.type))}",
            discord.Color.red(), discord.AuditLogAction.channel_delete, channel.id)


@bot.event
async def on_guild_channel_create(channel):
    _nuke(channel.guild, discord.AuditLogAction.channel_create, channel.id, "إنشاء رومات")
    if not _is_ticket(channel):
        _ev(channel.guild, "channel", "server", "📁 إنشاء روم",
            f"**الروم:** {channel.mention}\n**النوع:** {CH_KIND.get(channel.type, str(channel.type))}",
            discord.Color.green(), discord.AuditLogAction.channel_create, channel.id)


@bot.event
async def on_guild_channel_update(before, after):
    if _is_ticket(after):
        return
    changes = []
    if before.name != after.name:
        changes.append(f"الاسم: {before.name} ← {after.name}")
    if getattr(before, "topic", None) != getattr(after, "topic", None):
        changes.append("تم تغيير وصف الروم")
    if before.category_id != after.category_id:
        changes.append("تم نقله لتصنيف آخر")
    only_perms = not changes
    if before.overwrites != after.overwrites:
        changes.append("تم تعديل الصلاحيات")
    if changes:
        action = discord.AuditLogAction.overwrite_update if only_perms else discord.AuditLogAction.channel_update
        _ev(after.guild, "channel", "server", "🛠️ تعديل روم", f"**الروم:** {after.mention}\n" + "\n".join(changes),
            discord.Color.blue(), action, after.id)


@bot.event
async def on_guild_role_delete(role):
    _nuke(role.guild, discord.AuditLogAction.role_delete, role.id, "حذف رتب")
    _ev(role.guild, "role", "server", "🗑️ حذف رتبة", f"**الرتبة:** {role.name}", discord.Color.red(),
        discord.AuditLogAction.role_delete, role.id)


@bot.event
async def on_guild_role_create(role):
    _nuke(role.guild, discord.AuditLogAction.role_create, role.id, "إنشاء رتب")
    _ev(role.guild, "role", "server", "🏷️ إنشاء رتبة", f"**الرتبة:** {role.mention}", discord.Color.green(),
        discord.AuditLogAction.role_create, role.id)


@bot.event
async def on_guild_role_update(before, after):
    if before.permissions != after.permissions:
        asyncio.create_task(security.role_update_guard(bot, config, send_log, after.guild, before, after))
    changes = []
    if before.name != after.name:
        changes.append(f"الاسم: {before.name} ← {after.name}")
    if before.colour != after.colour:
        changes.append("تم تغيير اللون")
    if before.permissions != after.permissions:
        changes.append("تم تعديل الصلاحيات")
    if before.hoist != after.hoist:
        changes.append("العرض المنفصل: " + ("نعم" if after.hoist else "لا"))
    if changes:
        _ev(after.guild, "role", "server", "🏷️ تعديل رتبة", f"**الرتبة:** {after.mention}\n" + "\n".join(changes),
            discord.Color.blue(), discord.AuditLogAction.role_update, after.id)


@bot.event
async def on_member_ban(guild, user):
    _nuke(guild, discord.AuditLogAction.ban, user.id, "حظر أعضاء")
    _ev(guild, "ban", "mod", "⛔ حظر عضو", f"**العضو:** {user} ({user.id})", discord.Color.red(),
        discord.AuditLogAction.ban, user.id)


@bot.event
async def on_member_unban(guild, user):
    _ev(guild, "ban", "mod", "✅ فك حظر", f"**العضو:** {user} ({user.id})", discord.Color.green(),
        discord.AuditLogAction.unban, user.id)


@bot.event
async def on_webhooks_update(channel):
    _nuke(channel.guild, discord.AuditLogAction.webhook_create, None, "إنشاء ويب هوكس")


@bot.event
async def on_member_update(before, after):
    g = after.guild
    if before.roles != after.roles:
        asyncio.create_task(security.member_roles_guard(bot, config, send_log, g, before, after))
        added = [r.mention for r in after.roles if r not in before.roles]
        removed = [r.mention for r in before.roles if r not in after.roles]
        lines = ([f"➕ {', '.join(added)}"] if added else []) + ([f"➖ {', '.join(removed)}"] if removed else [])
        if lines:
            _ev(g, "member_update", "members", "🎭 تغيير رتب عضو", f"**العضو:** {after.mention}\n" + "\n".join(lines),
                discord.Color.blue(), discord.AuditLogAction.member_role_update, after.id)
    if before.nick != after.nick:
        _ev(g, "member_update", "members", "📝 تغيير لقب",
            f"**العضو:** {after.mention}\n**قبل:** {before.nick or before.name}\n**بعد:** {after.nick or after.name}",
            discord.Color.blue(), discord.AuditLogAction.member_update, after.id)
    if before.timed_out_until != after.timed_out_until:
        if after.timed_out_until:
            title, extra = "🔇 كتم عضو", f"\n**ينتهي:** <t:{int(after.timed_out_until.timestamp())}:R>"
        else:
            title, extra = "🔊 إلغاء كتم", ""
        _ev(g, "timeout", "mod", title, f"**العضو:** {after.mention}{extra}", discord.Color.orange(),
            discord.AuditLogAction.member_update, after.id)


@bot.event
async def on_invite_create(invite):
    g = bot.get_guild(invite.guild.id) if invite.guild else None
    if g:
        who = invite.inviter.mention if invite.inviter else "—"
        where = invite.channel.mention if getattr(invite.channel, "mention", None) else "—"
        _ev(g, "invite", "server", "🔗 دعوة جديدة",
            f"**الكود:** {invite.code}\n**المنشئ:** {who}\n**الروم:** {where}\n**الحد:** {invite.max_uses or '∞'} استخدام",
            discord.Color.green())


@bot.event
async def on_invite_delete(invite):
    g = bot.get_guild(invite.guild.id) if invite.guild else None
    if g:
        _ev(g, "invite", "server", "🔗 حذف دعوة", f"**الكود:** {invite.code}", discord.Color.dark_grey())


@bot.event
async def on_guild_update(before, after):
    changes = []
    if before.name != after.name:
        changes.append(f"الاسم: {before.name} ← {after.name}")
    if before.icon != after.icon:
        changes.append("تم تغيير أيقونة السيرفر")
    if before.verification_level != after.verification_level:
        changes.append(f"مستوى التحقق: {before.verification_level} ← {after.verification_level}")
    if changes:
        _ev(after, "guild", "server", "⚙️ تعديل إعدادات السيرفر", "\n".join(changes), discord.Color.blue(),
            discord.AuditLogAction.guild_update, None)


# ───────────────────────── دوال مساعدة ─────────────────────────
async def _delete(message: discord.Message):
    if len(bot_deleted) > 5000:
        bot_deleted.clear()
    bot_deleted.add(message.id)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden):
        pass


async def _notice(channel: discord.abc.Messageable, text: str):
    try:
        await channel.send(text, delete_after=6)
    except discord.HTTPException:
        pass


async def _kick_new(member: discord.Member, reason: str):
    try:
        await member.send(f"تم رفض دخولك إلى **{member.guild.name}**: {reason}")
    except discord.HTTPException:
        pass
    recently_kicked.add((member.guild.id, member.id))
    try:
        await member.kick(reason=f"Discord Shield: {reason}")
        stats.add(member.guild.id, "mod")
        await send_log(member.guild, "👢 طرد تلقائي", f"{member} — {reason}", discord.Color.red())
    except discord.Forbidden:
        recently_kicked.discard((member.guild.id, member.id))
        await send_log(member.guild, "⚠️ تعذر الطرد",
                       f"{member} — تأكد من صلاحية Kick Members ورتبة البوت", discord.Color.red())


# ───────────────────────── أوامر السلاش ─────────────────────────
shield = app_commands.Group(
    name="shield", description="إعدادات Discord Shield", guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


def _edit(guild_id: int, fn):
    cfg = config.get(guild_id)
    fn(cfg)
    config.update(guild_id, cfg)


@shield.command(name="status", description="عرض حالة الحماية والإعدادات")
async def status(interaction: discord.Interaction):
    cfg = config.get(interaction.guild_id)
    sp, ar = cfg["antispam"], cfg["antiraid"]
    ch = f"<#{cfg['log_channel_id']}>" if cfg["log_channel_id"] else "غير محددة"
    on = lambda v: "✅" if v else "❌"
    embed = discord.Embed(title="🛡️ Discord Shield", color=discord.Color.blurple())
    embed.add_field(name="قناة السجل", value=ch, inline=False)
    embed.add_field(name=f"{on(sp['enabled'])} مكافحة السبام",
                    value=f"{sp['max_messages']} رسائل / {sp['per_seconds']} ث → كتم {sp['timeout_minutes']} د")
    embed.add_field(name=f"{on(ar['enabled'])} حماية الغارات",
                    value=f"{ar['join_threshold']} دخول / {ar['per_seconds']} ث\n"
                          f"أقل عمر حساب: {ar['min_account_age_days']} يوم")
    embed.add_field(name="الكلمات المحظورة", value=str(len(cfg["banned_words"])))
    embed.add_field(name=f"{on(cfg['block_invites'])} منع روابط الدعوات", value="\u200b")
    embed.add_field(name=f"{on(cfg['welcome']['enabled'])} الترحيب", value="\u200b")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@shield.command(name="setlog", description="تحديد قناة السجل")
async def setlog(interaction: discord.Interaction, channel: discord.TextChannel):
    _edit(interaction.guild_id, lambda c: c.update(log_channel_id=channel.id))
    await interaction.response.send_message(f"تم تعيين السجل: {channel.mention} ✅", ephemeral=True)


@shield.command(name="addword", description="إضافة كلمة محظورة")
async def addword(interaction: discord.Interaction, word: str):
    word = word.strip()
    def f(c):
        if word not in c["banned_words"]:
            c["banned_words"].append(word)
    _edit(interaction.guild_id, f)
    await interaction.response.send_message("تمت الإضافة ✅", ephemeral=True)


@shield.command(name="removeword", description="حذف كلمة محظورة")
async def removeword(interaction: discord.Interaction, word: str):
    word = word.strip()
    def f(c):
        if word in c["banned_words"]:
            c["banned_words"].remove(word)
    _edit(interaction.guild_id, f)
    await interaction.response.send_message("تم الحذف ✅", ephemeral=True)


@shield.command(name="words", description="عرض الكلمات المحظورة")
async def words(interaction: discord.Interaction):
    ws = config.get(interaction.guild_id)["banned_words"]
    text = "، ".join(f"||{w}||" for w in ws) if ws else "لا توجد كلمات محظورة"
    await interaction.response.send_message(text[:1900], ephemeral=True)


@shield.command(name="antispam", description="تشغيل/إيقاف مكافحة السبام")
async def antispam(interaction: discord.Interaction, enabled: bool,
                   max_messages: app_commands.Range[int, 2, 20] = 5,
                   per_seconds: app_commands.Range[int, 2, 60] = 5,
                   timeout_minutes: app_commands.Range[int, 1, 1440] = 10):
    def f(c):
        c["antispam"].update(enabled=enabled, max_messages=max_messages,
                             per_seconds=per_seconds, timeout_minutes=timeout_minutes)
    _edit(interaction.guild_id, f)
    await interaction.response.send_message("تم تحديث مكافحة السبام ✅", ephemeral=True)


@shield.command(name="antiraid", description="تشغيل/إيقاف حماية الغارات")
async def antiraid(interaction: discord.Interaction, enabled: bool,
                   join_threshold: app_commands.Range[int, 2, 50] = 5,
                   per_seconds: app_commands.Range[int, 3, 120] = 10,
                   lockdown_seconds: app_commands.Range[int, 10, 3600] = 120):
    def f(c):
        c["antiraid"].update(enabled=enabled, join_threshold=join_threshold,
                             per_seconds=per_seconds, lockdown_seconds=lockdown_seconds)
    _edit(interaction.guild_id, f)
    await interaction.response.send_message("تم تحديث حماية الغارات ✅", ephemeral=True)


@shield.command(name="minage", description="أقل عمر حساب مسموح (بالأيام، 0 = تعطيل)")
async def minage(interaction: discord.Interaction, days: app_commands.Range[int, 0, 365]):
    _edit(interaction.guild_id, lambda c: c["antiraid"].update(min_account_age_days=days))
    await interaction.response.send_message("تم التحديث ✅", ephemeral=True)


@shield.command(name="invites", description="منع روابط دعوات ديسكورد")
async def invites(interaction: discord.Interaction, block: bool):
    _edit(interaction.guild_id, lambda c: c.update(block_invites=block))
    await interaction.response.send_message("تم التحديث ✅", ephemeral=True)


@shield.command(name="endlockdown", description="إنهاء وضع الحماية من الغارات يدوياً")
async def endlockdown(interaction: discord.Interaction):
    lockdown_until.pop(interaction.guild_id, None)
    await interaction.response.send_message("تم إنهاء وضع الحماية ✅", ephemeral=True)


async def _lock(interaction: discord.Interaction, lock: bool):
    p = interaction.user.guild_permissions
    if not (p.administrator or p.manage_channels):
        await interaction.response.send_message("تحتاج صلاحية Manage Channels ❌", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        n = await security.lockdown(interaction.guild, lock, config, interaction.user)
    except security.SecurityError as e:
        await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
        return
    except discord.Forbidden:
        await interaction.followup.send("⚠️ البوت ما عنده صلاحية Manage Channels كافية", ephemeral=True)
        return
    await send_log(interaction.guild, "🔒 قفل السيرفر" if lock else "🔓 فتح السيرفر",
                   f"**المنفذ:** {interaction.user.mention}\n**الرومات:** {n}", discord.Color.red())
    await interaction.followup.send(f"{'🔒 تم قفل' if lock else '🔓 تم فتح'} {n} روم ✅", ephemeral=True)


@shield.command(name="lockdown", description="قفل كل الرومات الكتابية (وضع الطوارئ)")
async def lockdown_cmd(interaction: discord.Interaction):
    await _lock(interaction, True)


@shield.command(name="unlock", description="فتح الرومات بعد القفل")
async def unlock_cmd(interaction: discord.Interaction):
    await _lock(interaction, False)


@shield.command(name="setup", description="إنشاء رومات ورتب جاهزة للسيرفر (قالب)")
@app_commands.describe(template="نوع السيرفر", post_messages="نشر رسالة القوانين والترحيب")
@app_commands.choices(template=[app_commands.Choice(name=t["name"], value=k) for k, t in TEMPLATES.items()])
async def setup_cmd(interaction: discord.Interaction, template: app_commands.Choice[str],
                    post_messages: bool = True):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("هذا الأمر يحتاج صلاحية Administrator ❌", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        rep = await apply_template(interaction.guild, template.value, actor=interaction.user,
                                   config=config, post_messages=post_messages)
    except SetupError as e:
        await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
        return
    await send_log(interaction.guild, "⚙️ إعداد سريع",
                   f"**المنفذ:** {interaction.user.mention}\n**القالب:** {template.name}",
                   discord.Color.blurple())
    await interaction.followup.send("تم إعداد السيرفر 🎉\n" + format_report(rep), ephemeral=True)


@bot.tree.command(name="clear", description="حذف عدد من الرسائل")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"تم حذف {len(deleted)} رسالة ✅", ephemeral=True)


@bot.tree.command(name="ping", description="فحص عمل البوت")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! 🛡️ {round(bot.latency * 1000)}ms", ephemeral=True)


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "ما عندك صلاحية لهذا الأمر ❌"
    else:
        log.exception("خطأ في الأمر", exc_info=error)
        msg = "صار خطأ غير متوقع ⚠️"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


bot.run(TOKEN, log_handler=None)
