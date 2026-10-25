"""
أنظمة تفاعلية: التذاكر، التقديمات، التحقق.
الأزرار دائمة (DynamicItem) فتشتغل حتى بعد إعادة تشغيل البوت.
"""
import asyncio
import io
import logging
import time

import discord

log = logging.getLogger("shield.systems")
_config = None
_send_log = None
_cooldown: dict[tuple, float] = {}


class PanelError(Exception):
    pass


def init(config, send_log):
    global _config, _send_log
    _config, _send_log = config, send_log


def register(bot):
    bot.add_dynamic_items(OpenButton, CloseButton, ClaimButton, ApplyButton, DecisionButton, VerifyButton)


def _ow(me: discord.Member, **flags) -> discord.PermissionOverwrite:
    """Overwrite يحتوي فقط الصلاحيات اللي يملكها البوت (ديسكورد يمنع منح ما لا تملك)."""
    gp = me.guild_permissions
    if not gp.administrator:
        flags = {k: v for k, v in flags.items() if getattr(gp, k, False)}
    return discord.PermissionOverwrite(**flags)


async def _reply(interaction: discord.Interaction, text: str):
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


def _fmt(text: str, member: discord.Member) -> str:
    return (text.replace("{user}", member.mention).replace("{username}", member.display_name)
            .replace("{server}", member.guild.name))[:2000]


# ───────────────────────── التذاكر ─────────────────────────
def _parse_topic(channel):
    topic = getattr(channel, "topic", None) or ""
    if not topic.startswith("ticket:"):
        return None
    p = topic.split(":")
    try:
        return int(p[1]), (p[2] if len(p) > 2 else ""), (int(p[3]) if len(p) > 3 and p[3] else None)
    except ValueError:
        return None


def _is_staff(member: discord.Member, t: dict) -> bool:
    gp = member.guild_permissions
    return gp.administrator or gp.manage_channels or any(r.id in t["support_role_ids"] for r in member.roles)


def _controls() -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(CloseButton())
    v.add_item(ClaimButton())
    return v


async def open_ticket(interaction: discord.Interaction, type_id: str):
    guild = interaction.guild
    if guild is None:
        return
    t = _config.get(guild.id)["tickets"]
    if not t["enabled"]:
        return await _reply(interaction, "نظام التذاكر غير مفعّل ❌")
    ttype = next((x for x in t["types"] if x["id"] == type_id), None)
    category = guild.get_channel(t["category_id"]) if t["category_id"] else None
    if not ttype or not isinstance(category, discord.CategoryChannel):
        return await _reply(interaction, "نظام التذاكر غير مضبوط، كلّم الإدارة ⚠️")

    user = interaction.user
    mine = [c for c in guild.text_channels if (c.topic or "").startswith(f"ticket:{user.id}:")]
    if len(mine) >= t["max_open"]:
        return await _reply(interaction, f"عندك تذكرة مفتوحة: {mine[0].mention}")

    await interaction.response.defer(ephemeral=True)
    cfg = _config.get(guild.id)
    cfg["tickets"]["counter"] += 1
    n = cfg["tickets"]["counter"]
    _config.update(guild.id, cfg)

    me = guild.me
    support = [r for r in (guild.get_role(i) for i in t["support_role_ids"]) if r]
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: _ow(me, view_channel=True, send_messages=True),
        me: _ow(me, view_channel=True, send_messages=True, manage_channels=True),
    }
    for r in support:
        overwrites[r] = _ow(me, view_channel=True, send_messages=True)
    try:
        channel = await category.create_text_channel(
            f"تذكرة-{n:04d}", topic=f"ticket:{user.id}:{type_id}", overwrites=overwrites,
            reason=f"Discord Shield: تذكرة {user}")
    except discord.Forbidden:
        return await interaction.followup.send("البوت ما عنده صلاحية إنشاء الرومات ⚠️", ephemeral=True)

    embed = discord.Embed(title=f"{ttype.get('emoji') or '🎫'} {ttype['label']}",
                          description=_fmt(t["welcome"], user), color=discord.Color.blurple())
    embed.set_footer(text=f"تذكرة رقم {n:04d}")
    mentions = " ".join([user.mention] + [r.mention for r in support])
    await channel.send(mentions, embed=embed, view=_controls(),
                       allowed_mentions=discord.AllowedMentions(users=[user], roles=support, everyone=False))
    await interaction.followup.send(f"تم فتح تذكرتك: {channel.mention} ✅", ephemeral=True)
    if _send_log:
        await _send_log(guild, "🎫 تذكرة جديدة", f"{user.mention} فتح {channel.mention} ({ttype['label']})",
                        discord.Color.green())


async def _transcript(channel: discord.TextChannel) -> bytes:
    lines = []
    async for m in channel.history(limit=2000, oldest_first=True):
        text = m.content or ""
        if m.attachments:
            text += " " + " ".join(a.url for a in m.attachments)
        if not text and m.embeds:
            text = "[embed]"
        lines.append(f"[{m.created_at.strftime('%Y-%m-%d %H:%M')}] {m.author} ({m.author.id}): {text}")
    return "\n".join(lines).encode("utf-8")


async def close_ticket(interaction: discord.Interaction):
    ch, guild = interaction.channel, interaction.guild
    info = _parse_topic(ch)
    if not info:
        return await _reply(interaction, "هذي مو تذكرة ❌")
    owner_id = info[0]
    cfg = _config.get(guild.id)
    t = cfg["tickets"]
    if interaction.user.id != owner_id and not _is_staff(interaction.user, t):
        return await _reply(interaction, "بس صاحب التذكرة أو الدعم يقدر يغلقها ❌")

    await interaction.response.send_message("🔒 جاري حفظ نسخة المحادثة وإغلاق التذكرة خلال 5 ثواني…")
    data = await _transcript(ch)
    fname = f"{ch.name}.txt"
    embed = discord.Embed(title="🎫 تذكرة مغلقة", color=discord.Color.red(),
                          description=f"**الروم:** {ch.name}\n**صاحب التذكرة:** <@{owner_id}>\n"
                                      f"**أغلقها:** {interaction.user.mention}")
    target = guild.get_channel(t["log_channel_id"] or cfg["log_channel_id"] or 0)
    if isinstance(target, discord.TextChannel):
        try:
            await target.send(embed=embed, file=discord.File(io.BytesIO(data), filename=fname),
                              allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("تعذر إرسال نسخة التذكرة")
    owner = guild.get_member(owner_id)
    if owner:
        try:
            await owner.send(f"تم إغلاق تذكرتك في **{guild.name}**. هذي نسخة من المحادثة:",
                             file=discord.File(io.BytesIO(data), filename=fname))
        except discord.HTTPException:
            pass
    await asyncio.sleep(5)
    try:
        await ch.delete(reason=f"Discord Shield: إغلاق تذكرة بواسطة {interaction.user}")
    except discord.HTTPException:
        pass


async def claim_ticket(interaction: discord.Interaction):
    ch, user = interaction.channel, interaction.user
    info = _parse_topic(ch)
    if not info:
        return await _reply(interaction, "هذي مو تذكرة ❌")
    if not _is_staff(user, _config.get(interaction.guild.id)["tickets"]):
        return await _reply(interaction, "بس فريق الدعم يقدر يستلم التذاكر ❌")
    owner, ttype, claimed = info
    if claimed:
        return await _reply(interaction, f"استلمها <@{claimed}> مسبقاً")
    await interaction.response.defer()
    await ch.edit(topic=f"ticket:{owner}:{ttype}:{user.id}")
    await interaction.followup.send(f"✋ استلم {user.mention} هذي التذكرة.",
                                    allowed_mentions=discord.AllowedMentions.none())


class OpenButton(discord.ui.DynamicItem[discord.ui.Button], template=r"shield:ticket:open:(?P<t>[A-Za-z0-9]+)"):
    def __init__(self, type_id: str, label: str = "فتح تذكرة", emoji: str | None = None):
        super().__init__(discord.ui.Button(label=label[:80], emoji=emoji or None,
                                           style=discord.ButtonStyle.primary,
                                           custom_id=f"shield:ticket:open:{type_id}"))
        self.type_id = type_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["t"])

    async def callback(self, interaction):
        await open_ticket(interaction, self.type_id)


class CloseButton(discord.ui.DynamicItem[discord.ui.Button], template=r"shield:ticket:close"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="إغلاق التذكرة", emoji="🔒", style=discord.ButtonStyle.danger,
                                           custom_id="shield:ticket:close"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction):
        await close_ticket(interaction)


class ClaimButton(discord.ui.DynamicItem[discord.ui.Button], template=r"shield:ticket:claim"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="استلام", emoji="✋", style=discord.ButtonStyle.success,
                                           custom_id="shield:ticket:claim"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction):
        await claim_ticket(interaction)


# ───────────────────────── التقديمات ─────────────────────────
class ApplyModal(discord.ui.Modal):
    def __init__(self, form: dict):
        super().__init__(title=form["name"][:45], timeout=900)
        self.form = form
        self.inputs: list[discord.ui.TextInput] = []
        for q in form["questions"][:5]:
            ti = discord.ui.TextInput(label=q[:45], style=discord.TextStyle.paragraph,
                                      max_length=1000, required=True)
            self.add_item(ti)
            self.inputs.append(ti)

    async def on_submit(self, interaction: discord.Interaction):
        guild, user = interaction.guild, interaction.user
        a = _config.get(guild.id)["applications"]
        review = guild.get_channel(a["review_channel_id"]) if a["review_channel_id"] else None
        if not isinstance(review, discord.TextChannel):
            return await _reply(interaction, "روم مراجعة التقديمات غير مضبوط ⚠️")
        embed = discord.Embed(title=f"🧾 {self.form['name']}", color=discord.Color.blurple(),
                              description=f"**المتقدم:** {user.mention} ({user.id})")
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        for q, inp in zip(self.form["questions"], self.inputs):
            embed.add_field(name=q[:256], value=(inp.value or "—")[:1024], inline=False)
        view = discord.ui.View(timeout=None)
        view.add_item(DecisionButton("accept", user.id, self.form["id"]))
        view.add_item(DecisionButton("reject", user.id, self.form["id"]))
        await review.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        _cooldown[(guild.id, user.id, self.form["id"])] = time.time() + 600
        await interaction.response.send_message(
            "تم إرسال تقديمك ✅ بتوصلك النتيجة على الخاص (تأكد إن رسائل الخاص مفتوحة).", ephemeral=True)


async def start_application(interaction: discord.Interaction, form_id: str):
    guild = interaction.guild
    a = _config.get(guild.id)["applications"]
    if not a["enabled"]:
        return await _reply(interaction, "التقديمات غير مفعّلة ❌")
    form = next((f for f in a["forms"] if f["id"] == form_id), None)
    if not form or not form.get("open", True):
        return await _reply(interaction, "التقديم مغلق حالياً ❌")
    if _cooldown.get((guild.id, interaction.user.id, form_id), 0) > time.time():
        return await _reply(interaction, "قدّمت قبل شوي، انتظر شوي وحاول مرة ثانية ⏳")
    await interaction.response.send_modal(ApplyModal(form))


async def decide(interaction: discord.Interaction, action: str, uid: int, fid: str):
    guild, user = interaction.guild, interaction.user
    gp = user.guild_permissions
    if not (gp.administrator or gp.manage_roles):
        return await _reply(interaction, "تحتاج صلاحية Manage Roles لمراجعة التقديمات ❌")
    form = next((f for f in _config.get(guild.id)["applications"]["forms"] if f["id"] == fid), None)
    applicant = guild.get_member(uid)
    accepted = action == "accept"
    note = ""
    if applicant is None:
        note = "\n⚠️ المتقدم خرج من السيرفر"
    elif accepted and form and form.get("role_id"):
        role = guild.get_role(form["role_id"])
        if role and not role.managed and role < guild.me.top_role:
            try:
                await applicant.add_roles(role, reason=f"Discord Shield: قبول تقديم ({form['name']})")
                note = f"\nتم إعطاؤه رتبة {role.mention}"
            except discord.HTTPException:
                note = "\n⚠️ تعذر إعطاء الرتبة"
        else:
            note = "\n⚠️ تعذر إعطاء الرتبة (رتبة البوت أقل منها)"
    embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
    embed.color = discord.Color.green() if accepted else discord.Color.red()
    embed.add_field(name="القرار", inline=False,
                    value=f"{'✅ مقبول' if accepted else '❌ مرفوض'} بواسطة {user.mention}{note}")
    await interaction.response.edit_message(embed=embed, view=None)
    try:
        target = applicant or await interaction.client.fetch_user(uid)
        name = form["name"] if form else "التقديم"
        await target.send(f"{'✅ تم قبول' if accepted else '❌ تم رفض'} تقديمك ({name}) في **{guild.name}**.")
    except discord.HTTPException:
        pass


class ApplyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"shield:app:apply:(?P<f>[A-Za-z0-9]+)"):
    def __init__(self, form_id: str, label: str = "تقديم"):
        super().__init__(discord.ui.Button(label=label[:80], emoji="🧾", style=discord.ButtonStyle.primary,
                                           custom_id=f"shield:app:apply:{form_id}"))
        self.form_id = form_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["f"])

    async def callback(self, interaction):
        await start_application(interaction, self.form_id)


class DecisionButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"shield:app:(?P<a>accept|reject):(?P<u>\d+):(?P<f>[A-Za-z0-9]+)"):
    def __init__(self, action: str, uid: int, fid: str):
        ok = action == "accept"
        super().__init__(discord.ui.Button(
            label="قبول" if ok else "رفض", emoji="✅" if ok else "❌",
            style=discord.ButtonStyle.success if ok else discord.ButtonStyle.danger,
            custom_id=f"shield:app:{action}:{uid}:{fid}"))
        self.action, self.uid, self.fid = action, uid, fid

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["a"], int(match["u"]), match["f"])

    async def callback(self, interaction):
        await decide(interaction, self.action, self.uid, self.fid)


# ───────────────────────── التحقق ─────────────────────────
async def do_verify(interaction: discord.Interaction):
    guild, member = interaction.guild, interaction.user
    v = _config.get(guild.id)["verify"]
    role = guild.get_role(v["role_id"]) if v["role_id"] else None
    if not v["enabled"] or not role:
        return await _reply(interaction, "نظام التحقق غير مضبوط ⚠️")
    if role in member.roles:
        return await _reply(interaction, "أنت متحقق مسبقاً ✅")
    if role.managed or role >= guild.me.top_role:
        return await _reply(interaction, "رتبة التحقق أعلى من رتبة البوت، كلّم الإدارة ⚠️")
    try:
        await member.add_roles(role, reason="Discord Shield: تحقق")
        rm = guild.get_role(v["remove_role_id"]) if v["remove_role_id"] else None
        if rm and rm in member.roles and not rm.managed and rm < guild.me.top_role:
            await member.remove_roles(rm, reason="Discord Shield: تحقق")
    except discord.HTTPException:
        return await _reply(interaction, "تعذر إعطاؤك الرتبة، كلّم الإدارة ⚠️")
    await _reply(interaction, "تم التحقق ✅ أهلاً بك!")


class VerifyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"shield:verify"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="تحقق", emoji="✅", style=discord.ButtonStyle.success,
                                           custom_id="shield:verify"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction):
        await do_verify(interaction)


# ───────────────────────── نشر اللوحات ─────────────────────────
async def publish_panel(guild: discord.Guild, kind: str) -> discord.Message:
    section = {"tickets": "tickets", "applications": "applications", "verify": "verify"}[kind]
    s = _config.get(guild.id)[section]
    if not s["enabled"]:
        raise PanelError("فعّل النظام واحفظ الإعدادات أولاً")
    p = s["panel"]
    ch = guild.get_channel(p["channel_id"]) if p["channel_id"] else None
    if not isinstance(ch, discord.TextChannel):
        raise PanelError("اختر روماً لنشر اللوحة واحفظ الإعدادات أولاً")
    mine = ch.permissions_for(guild.me)
    if not (mine.view_channel and mine.send_messages):
        raise PanelError("البوت ما يقدر يرسل في روم اللوحة")

    view = discord.ui.View(timeout=None)
    if kind == "tickets":
        if not s["types"]:
            raise PanelError("أضف نوع تذكرة واحد على الأقل")
        for t in s["types"][:5]:
            view.add_item(OpenButton(t["id"], t["label"], t.get("emoji")))
    elif kind == "applications":
        forms = [f for f in s["forms"] if f.get("open", True)][:25]
        if not forms:
            raise PanelError("أضف نموذج تقديم مفتوح واحد على الأقل")
        for f in forms:
            view.add_item(ApplyButton(f["id"], f["name"]))
    else:
        view.add_item(VerifyButton())
    embed = discord.Embed(title=p["title"], description=p["description"], color=discord.Color.blurple())
    return await ch.send(embed=embed, view=view)
