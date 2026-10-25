"""حماية متقدمة: Anti-Nuke، فلترة تلقائية (Automod)، وقفل السيرفر."""
import asyncio
import logging
import re
from collections import defaultdict, deque
from time import monotonic
from urllib.parse import urlparse

import discord

log = logging.getLogger("shield.security")

DANGEROUS = ("administrator", "manage_guild", "manage_roles", "manage_channels",
             "manage_webhooks", "ban_members", "kick_members", "mention_everyone")

quiet: set[int] = set()  # سيرفرات يُوقف فيها تسجيل الأحداث مؤقتاً (إعداد/قفل جماعي)
nuke_tracker: dict[tuple, deque] = defaultdict(deque)
seen_entries: deque = deque(maxlen=1000)
dup_tracker: dict[tuple, deque] = defaultdict(deque)

URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.I)
EMOJI_RE = re.compile(r"<a?:\w+:\d+>|[\U0001F300-\U0001FAFF\u2600-\u27BF]")


class SecurityError(Exception):
    pass


def has_dangerous(perms: discord.Permissions) -> bool:
    return any(getattr(perms, p) for p in DANGEROUS)


def trusted(bot, guild: discord.Guild, uid: int, cfg: dict) -> bool:
    return uid == guild.owner_id or uid == bot.user.id or uid in cfg["whitelist_ids"]


async def find_entry(guild: discord.Guild, action, target_id=None, window: int = 20):
    if not guild.me.guild_permissions.view_audit_log:
        return None
    now = discord.utils.utcnow()
    try:
        async for e in guild.audit_logs(limit=8, action=action):
            if (now - e.created_at).total_seconds() > window:
                break
            if target_id is None or getattr(e.target, "id", None) == target_id:
                return e
    except discord.HTTPException:
        return None
    return None


async def punish(guild: discord.Guild, user, action: str, reason: str) -> str:
    member = guild.get_member(user.id)
    if user.bot and action == "strip":
        action = "kick"
    try:
        if action == "ban":
            await guild.ban(user, reason=reason, delete_message_seconds=0)
            return "تم حظره"
        if action == "kick":
            if member:
                await member.kick(reason=reason)
                return "تم طرده"
            return "العضو غير موجود في السيرفر"
        if member:
            roles = [r for r in member.roles
                     if not r.is_default() and not r.managed and has_dangerous(r.permissions)]
            if roles:
                await member.remove_roles(*roles, reason=reason)
                return f"تم سحب {len(roles)} رتبة خطيرة منه"
            return "ما عنده رتب خطيرة لسحبها"
        return "العضو غير موجود في السيرفر"
    except discord.Forbidden:
        return "تعذر: رتبة البوت أقل من رتبته (ارفع رتبة البوت)"
    except discord.HTTPException as e:
        return f"فشل: {e}"


async def nuke_event(bot, config, send_log, stats, guild, action, target_id, label):
    cfg = config.get(guild.id)["antinuke"]
    if not cfg["enabled"]:
        return
    await asyncio.sleep(1.2)  # ننتظر ظهور السجل في Audit Log
    entry = await find_entry(guild, action, target_id)
    if entry is None or entry.user is None or entry.id in seen_entries:
        return
    seen_entries.append(entry.id)
    user = entry.user
    if trusted(bot, guild, user.id, cfg):
        return
    key = (guild.id, user.id, label)
    now = monotonic()
    q = nuke_tracker[key]
    q.append(now)
    while q and now - q[0] > cfg["per_seconds"]:
        q.popleft()
    if len(q) < cfg["limit"]:
        return
    q.clear()
    result = await punish(guild, user, cfg["action"], f"Discord Shield Anti-Nuke: {label}")
    stats.add(guild.id, "mod")
    await send_log(guild, "🧱 حماية من التخريب",
                   f"**المنفذ:** {user.mention} ({user.id})\n**السبب:** {label} بشكل متكرر\n"
                   f"**الإجراء:** {result}", discord.Color.red())


async def role_update_guard(bot, config, send_log, guild, before, after):
    cfg = config.get(guild.id)["antinuke"]
    if not (cfg["enabled"] and cfg["protect_perms"]):
        return
    gained = [p for p in DANGEROUS if getattr(after.permissions, p) and not getattr(before.permissions, p)]
    if not gained:
        return
    await asyncio.sleep(1.2)
    entry = await find_entry(guild, discord.AuditLogAction.role_update, after.id)
    if entry is None or entry.user is None or trusted(bot, guild, entry.user.id, cfg):
        return
    try:
        await after.edit(permissions=before.permissions, reason="Discord Shield: منع رفع صلاحيات خطيرة")
        result = "تم التراجع عن التعديل"
    except discord.HTTPException:
        result = "تعذر التراجع (رتبة البوت أقل من الرتبة)"
    await send_log(guild, "🛑 تعديل صلاحيات خطيرة",
                   f"**المنفذ:** {entry.user.mention}\n**الرتبة:** {after.name}\n"
                   f"**الصلاحيات المضافة:** {', '.join(gained)}\n**الإجراء:** {result}", discord.Color.red())


async def member_roles_guard(bot, config, send_log, guild, before, after):
    cfg = config.get(guild.id)["antinuke"]
    if not (cfg["enabled"] and cfg["protect_perms"]):
        return
    added = [r for r in after.roles if r not in before.roles and has_dangerous(r.permissions)]
    if not added:
        return
    await asyncio.sleep(1.2)
    entry = await find_entry(guild, discord.AuditLogAction.member_role_update, after.id)
    if entry is None or entry.user is None or trusted(bot, guild, entry.user.id, cfg):
        return
    try:
        await after.remove_roles(*added, reason="Discord Shield: منع إعطاء رتب خطيرة")
        result = "تم سحب الرتب"
    except discord.HTTPException:
        result = "تعذر السحب (رتبة البوت أقل)"
    await send_log(guild, "🛑 إعطاء رتبة خطيرة",
                   f"**المنفذ:** {entry.user.mention}\n**العضو:** {after.mention}\n"
                   f"**الرتب:** {', '.join(r.name for r in added)}\n**الإجراء:** {result}", discord.Color.red())


async def new_bot_guard(bot, config, send_log, stats, guild, member) -> bool:
    cfg = config.get(guild.id)["antinuke"]
    if not (cfg["enabled"] and cfg["block_new_bots"] and member.bot):
        return False
    if not guild.me.guild_permissions.view_audit_log:
        return False
    await asyncio.sleep(1.2)
    entry = await find_entry(guild, discord.AuditLogAction.bot_add, member.id)
    if entry and entry.user and trusted(bot, guild, entry.user.id, cfg):
        return False
    try:
        await member.kick(reason="Discord Shield: بوت غير مصرّح")
        stats.add(guild.id, "mod")
        adder = entry.user.mention if entry and entry.user else "غير معروف"
        await send_log(guild, "🤖 بوت غير مصرّح", f"تم طرد {member} — أضافه: {adder}", discord.Color.red())
        return True
    except discord.HTTPException:
        return False


def automod_check(message: discord.Message, am: dict):
    """يرجع (السبب، شديد؟) أو None."""
    content = message.content
    if am["mention_enabled"]:
        if message.mention_everyone or "@everyone" in content or "@here" in content:
            return ("منشن everyone/here ممنوع", True)
        n = len({m.id for m in message.mentions}) + len(message.role_mentions)
        if n > am["mention_max"]:
            return (f"المنشن الجماعي ممنوع ({n} منشن)", True)
    if am["links_enabled"]:
        for url in URL_RE.findall(content):
            host = (urlparse(url if "://" in url else "http://" + url).hostname or "").lower()
            if not any(host == d or host.endswith("." + d) for d in am["links_allow"]):
                return ("الروابط ممنوعة هنا", False)
    if am["emoji_enabled"] and len(EMOJI_RE.findall(content)) > am["emoji_max"]:
        return ("كثرة الإيموجي ممنوعة", False)
    if am["dup_enabled"] and len(content.strip()) >= 3:
        key = (message.guild.id, message.author.id)
        now = monotonic()
        txt = content.strip().lower()
        q = dup_tracker[key]
        q.append((now, txt))
        while q and now - q[0][0] > 15:
            q.popleft()
        if sum(1 for _, t in q if t == txt) >= am["dup_count"]:
            q.clear()
            return ("تكرار نفس الرسالة ممنوع", False)
    return None


async def lockdown(guild: discord.Guild, lock: bool, config, actor) -> int:
    cfg = config.get(guild.id)
    ld = cfg["lockdown"]
    reason = f"Discord Shield: {'قفل' if lock else 'فتح'} السيرفر بواسطة {actor}"
    count = 0
    quiet.add(guild.id)
    try:
        return await _lockdown_inner(guild, lock, config, cfg, ld, reason)
    finally:
        asyncio.get_running_loop().call_later(6, quiet.discard, guild.id)


async def _lockdown_inner(guild, lock, config, cfg, ld, reason) -> int:
    count = 0
    if lock:
        if ld["active"]:
            raise SecurityError("السيرفر مقفل مسبقاً")
        prev: dict = {}
        try:
            for ch in guild.text_channels:
                ow = ch.overwrites_for(guild.default_role)
                if ow.send_messages is False:
                    continue
                prev[str(ch.id)] = ow.send_messages
                ow.send_messages = False
                await ch.set_permissions(guild.default_role, overwrite=ow, reason=reason)
                count += 1
        finally:
            cfg["lockdown"] = {"active": bool(prev), "prev": prev}
            config.update(guild.id, cfg)
    else:
        if not ld["active"]:
            raise SecurityError("السيرفر مو مقفل")
        for cid, val in ld["prev"].items():
            ch = guild.get_channel(int(cid))
            if not ch:
                continue
            ow = ch.overwrites_for(guild.default_role)
            ow.send_messages = val
            await ch.set_permissions(guild.default_role, overwrite=None if ow.is_empty() else ow, reason=reason)
            count += 1
        cfg["lockdown"] = {"active": False, "prev": {}}
        config.update(guild.id, cfg)
    return count
