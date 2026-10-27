"""
فايف إم: حالة السيرفر (Endpoints عامة) + رسالة الحالة التلقائية في ديسكورد + التحكم عبر Bridge/RCON.
الأسرار (التوكن وكلمة RCON) تُقرأ من .env فقط ولا تُرسل للمتصفح أبداً.
"""
import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import time
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import tasks

log = logging.getLogger("shield.fivem")
_bot = None
_config = None
_peak: dict[str, tuple[str, int]] = {}
_cache: dict[str, tuple[float, dict]] = {}
_cooldown: dict[int, float] = {}
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
JOIN_RE = re.compile(r"^[a-z0-9]{4,10}$")
COLOR_CODE = re.compile(r"\^\d")


class FiveMError(Exception):
    pass


def env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def owner_port() -> int:
    try:
        return int(env("FIVEM_PORT", "30120") or 30120)
    except ValueError:
        return 30120


def capabilities() -> dict:
    return {
        "status": bool(env("FIVEM_HOST")), "host": env("FIVEM_HOST"), "port": owner_port(),
        "bridge": bool(env("FIVEM_BRIDGE_URL") and env("FIVEM_BRIDGE_TOKEN")),
        "rcon": bool(env("FIVEM_HOST") and env("FIVEM_RCON_PASSWORD")),
        "ai": bool(env("ANTHROPIC_API_KEY")),
        "deploy": bool(env("FIVEM_RESOURCES_PATH") and env("FIVEM_ALLOW_DEPLOY") == "1"),
    }


# ───────────────────────── أمان الاتصال (منع SSRF) ─────────────────────────
def _private_ip(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    return a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified


def literal_private(host: str) -> bool:
    """يرفض عناوين IP الداخلية المكتوبة مباشرة (إلا سيرفر المالك المحدد في .env)."""
    if host.lower() == env("FIVEM_HOST").lower():
        return False
    try:
        return _private_ip(host)
    except ValueError:
        return host.lower() in ("localhost",) or host.lower().endswith(".local")


async def host_allowed(host: str) -> bool:
    if not HOST_RE.match(host) or literal_private(host):
        return False
    if host.lower() == env("FIVEM_HOST").lower():
        return True
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except OSError:
        return False
    return bool(infos) and all(not _private_ip(i[4][0]) for i in infos)


# ───────────────────────── قراءة حالة السيرفر ─────────────────────────
def _bar(cur: int, mx: int, n: int = 14) -> str:
    filled = min(n, round(n * cur / mx)) if mx else 0
    return "▰" * filled + "▱" * (n - filled)


async def fetch_status(host: str, port: int, fresh: bool = False) -> dict:
    key = f"{host}:{port}"
    c = _cache.get(key)
    if c and not fresh and time.time() - c[0] < 8:
        return c[1]
    res = {"online": False, "ping_ms": None, "players": [], "max": 0, "name": "", "resources": 0,
           "gametype": "", "map": "", "peak": 0, "error": None}
    if not await host_allowed(host):
        res["error"] = "العنوان غير مسموح (لازم IP أو دومين عام)"
        return res
    base = f"http://{host}:{port}"
    t0 = time.perf_counter()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async def get(path):
                async with s.get(base + path, allow_redirects=False) as r:
                    if r.status != 200:
                        raise ValueError(r.status)
                    return json.loads(await r.content.read(2_000_000))

            players, info = await asyncio.gather(get("/players.json"), get("/info.json"))
            res["ping_ms"] = round((time.perf_counter() - t0) * 1000)
            try:
                dyn = await get("/dynamic.json")
            except Exception:
                dyn = {}
        res["players"] = sorted(
            [{"id": int(p.get("id", 0)), "name": COLOR_CODE.sub("", str(p.get("name", "?"))),
              "ping": int(p.get("ping", 0))} for p in players if isinstance(p, dict)], key=lambda p: p["id"])
        mx = dyn.get("sv_maxclients") or (info.get("vars") or {}).get("sv_maxClients") or 0
        res.update(online=True, max=int(mx) if str(mx).isdigit() else 0,
                   name=COLOR_CODE.sub("", str(dyn.get("hostname") or (info.get("vars") or {}).get("sv_projectName") or "")),
                   resources=len(info.get("resources") or []), gametype=str(dyn.get("gametype") or ""),
                   map=str(dyn.get("mapname") or ""))
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, json.JSONDecodeError, OSError) as e:
        res["error"] = f"تعذر الاتصال بالسيرفر ({type(e).__name__})"
    today = datetime.now(timezone.utc).date().isoformat()
    day, pk = _peak.get(key, (today, 0))
    pk = max(pk if day == today else 0, len(res["players"]))
    _peak[key] = (today, pk)
    res["peak"] = pk
    _cache[key] = (time.time(), res)
    return res


# ───────────────────────── رسالة الحالة في ديسكورد ─────────────────────────
def build_embed(fc: dict, d: dict, guild: discord.Guild) -> discord.Embed:
    online = d["online"]
    try:
        color = discord.Color(int((fc.get("color") or "#22d3ee").lstrip("#"), 16))
    except ValueError:
        color = discord.Color.teal()
    e = discord.Embed(color=color if online else discord.Color.red(), timestamp=datetime.now(timezone.utc))
    name = fc.get("name") or d.get("name") or "سيرفر فايف إم"
    e.title = f"{'🟢' if online else '🔴'}  {name}"
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
    if fc.get("banner_url"):
        e.set_image(url=fc["banner_url"])
    if not online:
        e.description = ("```diff\n- السيرفر غير متصل حالياً\n```\n"
                         "نتابع الحالة كل دقيقة، وبيتحدث تلقائياً أول ما يرجع.")
    else:
        cur, mx = len(d["players"]), d["max"]
        e.description = f"```\n{_bar(cur, mx)}  {cur}/{mx or '?'}\n```"
        e.add_field(name="🟢 الحالة", value="يعمل")
        e.add_field(name="👥 اللاعبين", value=f"**{cur}** / {mx or '?'}")
        e.add_field(name="📈 أعلى عدد اليوم", value=f"**{d['peak']}**")
        e.add_field(name="📶 الاستجابة", value=f"{d['ping_ms']} ms")
        e.add_field(name="🧩 الريسورسات", value=str(d["resources"]))
        e.add_field(name="🎮 النمط", value=d["gametype"] or "—")
        if fc.get("show_players"):
            if d["players"]:
                lines, total = [], 0
                for p in d["players"]:
                    nm = discord.utils.escape_mentions(discord.utils.escape_markdown(p["name"]))[:22]
                    line = f"`{p['id']:>3}` {nm} · {p['ping']}ms"
                    if total + len(line) > 950:
                        lines.append(f"… و{len(d['players']) - len(lines)} آخرين")
                        break
                    lines.append(line)
                    total += len(line) + 1
                e.add_field(name="👤 اللاعبون المتصلون", value="\n".join(lines), inline=False)
            else:
                e.add_field(name="👤 اللاعبون المتصلون", value="لا يوجد لاعبين حالياً", inline=False)
    if fc.get("join_code"):
        e.add_field(name="🔗 دخول السيرفر", value=f"`cfx.re/join/{fc['join_code']}`", inline=False)
    e.set_footer(text="Discord Shield • يتحدث كل دقيقة")
    return e


def build_view(fc: dict) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    if fc.get("join_code"):
        v.add_item(discord.ui.Button(label="دخول السيرفر", emoji="🎮", style=discord.ButtonStyle.link,
                                     url=f"https://cfx.re/join/{fc['join_code']}"))
    v.add_item(RefreshButton())
    return v


async def update_guild(guild: discord.Guild):
    fc = _config.get(guild.id)["fivem"]
    if not fc["enabled"] or not fc["host"] or not fc["channel_id"]:
        return None
    ch = guild.get_channel(fc["channel_id"])
    if not isinstance(ch, discord.TextChannel):
        return None
    data = await fetch_status(fc["host"], fc["port"])
    embed, view = build_embed(fc, data, guild), build_view(fc)
    msg = None
    if fc["message_id"]:
        try:
            msg = await ch.fetch_message(fc["message_id"])
            await msg.edit(embed=embed, view=view)
        except discord.NotFound:
            msg = None
    if msg is None:
        msg = await ch.send(embed=embed, view=view)
        cfg = _config.get(guild.id)
        cfg["fivem"]["message_id"] = msg.id
        _config.update(guild.id, cfg)
    return msg


class RefreshButton(discord.ui.DynamicItem[discord.ui.Button], template=r"shield:fivem:refresh"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="تحديث", emoji="🔄", style=discord.ButtonStyle.secondary,
                                           custom_id="shield:fivem:refresh"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        if _cooldown.get(gid, 0) > time.time():
            return await interaction.response.send_message("انتظر ثواني قبل التحديث مرة ثانية ⏳", ephemeral=True)
        _cooldown[gid] = time.time() + 15
        await interaction.response.defer(ephemeral=True)
        _cache.clear()
        try:
            await update_guild(interaction.guild)
        except discord.HTTPException:
            return await interaction.followup.send("تعذر التحديث ⚠️", ephemeral=True)
        await interaction.followup.send("تم التحديث ✅", ephemeral=True)


@tasks.loop(seconds=60)
async def status_loop():
    for g in list(_bot.guilds):
        if _config.get(g.id)["fivem"]["enabled"]:
            try:
                await update_guild(g)
            except Exception:
                log.exception("فشل تحديث حالة السيرفر")
            await asyncio.sleep(0.5)


def start(bot, config):
    global _bot, _config
    _bot, _config = bot, config
    bot.add_dynamic_items(RefreshButton)
    status_loop.start()


# ───────────────────────── التحكم (مالك البوت فقط) ─────────────────────────
def safe(text: str) -> str:
    """تنظيف نص قبل إرساله لأمر كونسول (يمنع ; وأسطر جديدة وعلامات الاقتباس)."""
    return re.sub(r"[;\r\n\"'\\`]", " ", str(text)).strip()[:200]


async def bridge(method: str, path: str, payload: dict | None = None, timeout: int = 10) -> dict:
    url, token = env("FIVEM_BRIDGE_URL").rstrip("/"), env("FIVEM_BRIDGE_TOKEN")
    if not url or not token:
        raise FiveMError("الـ Bridge غير مضبوط (FIVEM_BRIDGE_URL و FIVEM_BRIDGE_TOKEN في .env)")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.request(method, url + path, json=payload, allow_redirects=False,
                                 headers={"Authorization": f"Bearer {token}"}) as r:
                raw = await r.content.read(3_000_000)
                if r.status == 401:
                    raise FiveMError("رفض الـ Bridge التوكن: لازم يطابق Config.Token في السكربت")
                if r.status == 403:
                    raise FiveMError("الـ Bridge رفض الأمر (غير مسموح في إعداداته)")
                if r.status >= 400:
                    raise FiveMError(f"خطأ من الـ Bridge (HTTP {r.status})")
                return json.loads(raw or b"{}")
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        raise FiveMError(f"تعذر الاتصال بالـ Bridge: تأكد إن السيرفر شغال وريسورس shield_bridge مفعّل ({type(e).__name__})")
    except json.JSONDecodeError:
        raise FiveMError("رد غير صالح من الـ Bridge")


def _rcon_sync(host: str, port: int, password: str, command: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    try:
        s.sendto(b"\xff\xff\xff\xffrcon " + password.encode() + b" " + command.encode(), (host, port))
        try:
            data, _ = s.recvfrom(65535)
        except socket.timeout:
            return ""
        return data.decode("utf-8", "replace").replace("\xff", "").removeprefix("print ").strip()
    finally:
        s.close()


async def rcon(command: str) -> str:
    host, pwd = env("FIVEM_HOST"), env("FIVEM_RCON_PASSWORD")
    if not host or not pwd:
        raise FiveMError("RCON غير مضبوط (FIVEM_HOST و FIVEM_RCON_PASSWORD في .env)")
    try:
        out = await asyncio.to_thread(_rcon_sync, host, owner_port(), pwd, command)
    except OSError as e:
        raise FiveMError(f"تعذر إرسال RCON ({type(e).__name__})")
    return out or "(تم إرسال الأمر)"
