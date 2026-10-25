"""سجلات الأحداث: رسائل، أعضاء، صوت، سيرفر، إشراف — كل نوع يروح لروم مخصص (أو للروم الرئيسي)."""
import asyncio
import logging

import discord

import security

log = logging.getLogger("shield.eventlog")

LOG_CATEGORIES = {
    "messages": ("💬┃سجل-الرسائل", "حذف وتعديل الرسائل"),
    "members": ("👥┃سجل-الأعضاء", "دخول وخروج الأعضاء وتغيير اللقب والرتب"),
    "voice": ("🔊┃سجل-الصوت", "دخول وخروج وانتقال الرومات الصوتية"),
    "server": ("🛠️┃سجل-السيرفر", "الرومات والرتب والدعوات وإعدادات السيرفر"),
    "mod": ("🛡️┃سجل-الإشراف", "الحظر والطرد والكتم والسبام والغارات والحماية"),
}


class LogSetupError(Exception):
    pass


def enabled(config, guild, key: str) -> bool:
    lg = config.get(guild.id)["logs"]
    return lg["enabled"] and lg["events"].get(key, True)


async def emit(config, send_log, guild, key, category, title, desc, color, action=None, target_id=None):
    """يرسل سجلاً (مع المنفذ والسبب من Audit Log إذا طُلب)."""
    if guild is None or guild.id in security.quiet or not enabled(config, guild, key):
        return
    extra = ""
    if action is not None:
        await asyncio.sleep(1.2)
        e = await security.find_entry(guild, action, target_id)
        if e and e.user:
            extra += f"\n**المنفذ:** {e.user.mention}"
            if e.reason:
                extra += f"\n**السبب:** {e.reason}"
    try:
        await send_log(guild, title, desc + extra, color, category)
    except Exception:
        log.exception("تعذر إرسال السجل")


async def log_kick(config, send_log, guild, member, bot_id: int):
    if guild.id in security.quiet or not enabled(config, guild, "kick"):
        return
    await asyncio.sleep(1.5)
    e = await security.find_entry(guild, discord.AuditLogAction.kick, member.id)
    if e is None or e.user is None or e.user.id == bot_id:
        return
    desc = f"**العضو:** {member} ({member.id})\n**المنفذ:** {e.user.mention}"
    if e.reason:
        desc += f"\n**السبب:** {e.reason}"
    await send_log(guild, "👢 طرد عضو", desc, discord.Color.orange(), "mod")


async def test_logs(guild, config) -> list[dict]:
    cfg = config.get(guild.id)
    lg = cfg["logs"]
    items = [(k, v[0]) for k, v in LOG_CATEGORIES.items()] + [("main", "الروم الرئيسي (احتياطي)")]
    results = []
    for key, label in items:
        cid = cfg["log_channel_id"] if key == "main" else lg["channels"].get(key)
        if not cid:
            results.append({"key": key, "label": label, "ok": False,
                            "status": "غير محدد" + ("" if key == "main" or not cfg["log_channel_id"]
                                                    else " (يستخدم الروم الرئيسي)")})
            continue
        ch = guild.get_channel(cid)
        if not isinstance(ch, discord.TextChannel):
            results.append({"key": key, "label": label, "ok": False, "status": "الروم محذوف أو غير كتابي"})
            continue
        try:
            await ch.send(embed=discord.Embed(title="✅ اختبار السجلات", color=discord.Color.green(),
                                              description=f"هذي رسالة اختبار لقسم **{label}**."))
            results.append({"key": key, "label": label, "ok": True, "status": f"تم الإرسال في #{ch.name}"})
        except discord.Forbidden:
            results.append({"key": key, "label": label, "ok": False,
                            "status": f"البوت ما يقدر يرسل في #{ch.name}"})
        except discord.HTTPException as e:
            results.append({"key": key, "label": label, "ok": False, "status": f"فشل: {e}"})
    return results


async def create_log_channels(guild, config, actor) -> list[str]:
    me = guild.me
    gp = me.guild_permissions
    if not (gp.administrator or gp.manage_channels):
        raise LogSetupError("البوت يحتاج صلاحية Manage Channels")
    reason = f"Discord Shield: إنشاء رومات السجلات بواسطة {actor}"
    security.quiet.add(guild.id)
    try:
        viewers = [r for r in guild.roles if not r.is_default() and not r.managed and (
            r.permissions.administrator or r.permissions.manage_guild or r.permissions.ban_members
            or r.permissions.kick_members or r.permissions.manage_messages)][:20]
        ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        for r in viewers:
            ow[r] = discord.PermissionOverwrite(view_channel=True)
        if not gp.administrator:
            ow[me] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        category = discord.utils.get(guild.categories, name="📜 السجلات") or \
            await guild.create_category("📜 السجلات", overwrites=ow, reason=reason)
        cfg = config.get(guild.id)
        names = []
        for key, (name, topic) in LOG_CATEGORIES.items():
            ch = discord.utils.get(category.text_channels, name=name.lower()) or \
                await guild.create_text_channel(name, category=category, topic=topic, overwrites=ow, reason=reason)
            cfg["logs"]["channels"][key] = ch.id
            names.append(name)
        cfg["logs"]["enabled"] = True
        config.update(guild.id, cfg)
        return names
    finally:
        asyncio.get_running_loop().call_later(6, security.quiet.discard, guild.id)
