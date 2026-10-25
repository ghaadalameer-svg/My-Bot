"""
لوحة تحكم Discord Shield
تشتغل داخل نفس عملية البوت. تسجيل الدخول عبر OAuth2 من ديسكورد، وكل طلب
يتحقق من صلاحيات المستخدم الفعلية داخل السيرفر (وليس من المتصفح).
"""
import hmac
import logging
import os
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
import discord
from aiohttp import web

import eventlog
import security
import systems
from setup_tools import TEMPLATES, SetupError, apply_template, summarize

log = logging.getLogger("shield.dashboard")

STATIC_DIR = Path(__file__).with_name("static")
DISCORD_API = "https://discord.com/api/v10"
SESSION_COOKIE = "shield_session"
STATE_COOKIE = "shield_oauth_state"
SESSION_TTL = 7 * 24 * 3600
MANAGE_GUILD = 0x20
ADMINISTRATOR = 0x8

# صلاحيات الدعوة المقترحة (بدون Administrator)
INVITE_PERMS = discord.Permissions(
    view_channel=True, send_messages=True, embed_links=True, read_message_history=True,
    manage_messages=True, kick_members=True, ban_members=True, moderate_members=True,
    manage_channels=True, manage_roles=True, view_audit_log=True,
)
WATCHED_PERMS = {
    "view_channel": "View Channels", "send_messages": "Send Messages",
    "embed_links": "Embed Links", "manage_messages": "Manage Messages",
    "kick_members": "Kick Members", "ban_members": "Ban Members",
    "moderate_members": "Moderate Members", "manage_channels": "Manage Channels",
    "manage_roles": "Manage Roles", "view_audit_log": "View Audit Log",
}
ACTOR_PERMS = ["manage_guild", "manage_messages", "manage_channels", "manage_roles",
               "kick_members", "ban_members", "moderate_members"]

# القيم المسموحة لإعدادات الحماية: (min, max) للأرقام، أو bool
CONFIG_SPEC = {
    "antispam": {"enabled": bool, "max_messages": (2, 20), "per_seconds": (2, 60),
                 "timeout_minutes": (1, 1440)},
    "antiraid": {"enabled": bool, "join_threshold": (2, 50), "per_seconds": (3, 120),
                 "lockdown_seconds": (10, 3600), "min_account_age_days": (0, 365)},
}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def fail(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"error": msg}, status=status)


def ok(**extra) -> web.Response:
    return web.json_response({"ok": True, **extra})


def parse_id(v, what="المعرّف") -> int:
    try:
        n = int(str(v))
    except (TypeError, ValueError):
        raise ApiError(f"{what} غير صالح")
    if n <= 0:
        raise ApiError(f"{what} غير صالح")
    return n


def clean_text(v, name: str, lo: int, hi: int) -> str:
    if not isinstance(v, str):
        raise ApiError(f"{name} مطلوب")
    v = v.strip()
    if not lo <= len(v) <= hi:
        raise ApiError(f"{name} لازم يكون بين {lo} و {hi} حرف")
    return v


def parse_color(v, default: int = 0x5865F2) -> int:
    if v in (None, ""):
        return default
    if isinstance(v, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", v):
        return int(v.lstrip("#"), 16)
    raise ApiError("اللون غير صالح")


def parse_int(v, lo: int, hi: int, name: str) -> int:
    if isinstance(v, bool):
        raise ApiError(f"قيمة غير صالحة: {name}")
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ApiError(f"قيمة غير صالحة: {name}")
    if not lo <= n <= hi:
        raise ApiError(f"{name} لازم يكون بين {lo} و {hi}")
    return n


def role_editable(guild: discord.Guild, actor: discord.Member, role: discord.Role) -> bool:
    if role.is_default() or role.managed:
        return False
    if role >= guild.me.top_role:
        return False
    return actor.id == guild.owner_id or role < actor.top_role


def target_problem(guild: discord.Guild, actor: discord.Member, target: discord.Member):
    """يرجع نص المشكلة أو None إذا الإجراء مسموح على هذا العضو."""
    if target.id == guild.owner_id:
        return "ما تقدر تطبق هذا على مالك السيرفر"
    if target.id == guild.me.id:
        return "ما تقدر تطبق هذا على البوت نفسه"
    if target.id == actor.id:
        return "ما تقدر تطبق هذا على نفسك"
    if actor.id != guild.owner_id and target.top_role >= actor.top_role:
        return "رتبة العضو أعلى منك أو مساوية لك"
    if target.top_role >= guild.me.top_role:
        return "رتبة العضو أعلى من رتبة البوت (ارفع رتبة البوت)"
    return None


def channel_kind(c) -> str:
    if isinstance(c, discord.CategoryChannel):
        return "category"
    if isinstance(c, discord.TextChannel):
        return "text"
    if isinstance(c, (discord.VoiceChannel, discord.StageChannel)):
        return "voice"
    return "other"


ID_RE = re.compile(r"^[a-z0-9]{1,8}$")
DOMAIN_RE = re.compile(r"^[a-z0-9.-]{1,60}$")


def _bool(d, k):
    if not isinstance(d[k], bool):
        raise ApiError(f"قيمة غير صالحة: {k}")
    return d[k]


def _text_channel(guild, v, what="الروم"):
    if v in (None, ""):
        return None
    ch = guild.get_channel(parse_id(v, what))
    if not isinstance(ch, discord.TextChannel):
        raise ApiError(f"{what} لازم يكون روماً كتابياً")
    return ch.id


def _panel(guild, d, cur):
    if not isinstance(d, dict):
        raise ApiError("بيانات اللوحة غير صالحة")
    out = dict(cur)
    if "title" in d:
        out["title"] = clean_text(d["title"], "عنوان اللوحة", 1, 200)
    if "description" in d:
        out["description"] = clean_text(d["description"], "وصف اللوحة", 1, 1000)
    if "channel_id" in d:
        out["channel_id"] = _text_channel(guild, d["channel_id"], "روم اللوحة")
    return out


def _editable_role(guild, actor, v, what="الرتبة"):
    if v in (None, ""):
        return None
    role = guild.get_role(parse_id(v, what))
    if not role or not role_editable(guild, actor, role):
        raise ApiError(f"{what}: ما تقدر تستخدمها (أعلى منك أو من البوت، أو رتبة بوت)")
    return role.id


def _v_tickets(guild, d, cfg):
    t = cfg["tickets"]
    if not isinstance(d, dict):
        raise ApiError("قيمة غير صالحة: tickets")
    if "enabled" in d:
        t["enabled"] = _bool(d, "enabled")
    if "category_id" in d:
        if d["category_id"] in (None, ""):
            t["category_id"] = None
        else:
            c = guild.get_channel(parse_id(d["category_id"], "الكاتيجوري"))
            if not isinstance(c, discord.CategoryChannel):
                raise ApiError("اختر كاتيجوري صحيحة للتذاكر")
            t["category_id"] = c.id
    if "support_role_ids" in d:
        ids = d["support_role_ids"]
        if not isinstance(ids, list) or len(ids) > 10:
            raise ApiError("رتب الدعم غير صالحة (الحد 10)")
        out = []
        for i in ids:
            r = guild.get_role(parse_id(i, "الرتبة"))
            if not r or r.is_default():
                raise ApiError("رتبة دعم غير موجودة")
            out.append(r.id)
        t["support_role_ids"] = out
    if "log_channel_id" in d:
        t["log_channel_id"] = _text_channel(guild, d["log_channel_id"], "روم سجل التذاكر")
    if "max_open" in d:
        t["max_open"] = parse_int(d["max_open"], 1, 5, "أقصى عدد تذاكر")
    if "welcome" in d:
        t["welcome"] = clean_text(d["welcome"], "رسالة التذكرة", 1, 1000)
    if "panel" in d:
        t["panel"] = _panel(guild, d["panel"], t["panel"])
    if "types" in d:
        types = d["types"]
        if not isinstance(types, list) or not 1 <= len(types) <= 5:
            raise ApiError("أنواع التذاكر: من 1 إلى 5")
        out, seen = [], set()
        for x in types:
            if (not isinstance(x, dict) or not isinstance(x.get("id"), str)
                    or not ID_RE.match(x["id"]) or x["id"] in seen):
                raise ApiError("نوع تذكرة غير صالح")
            seen.add(x["id"])
            emoji = x.get("emoji") or ""
            if not isinstance(emoji, str) or len(emoji) > 8:
                raise ApiError("إيموجي غير صالح")
            out.append({"id": x["id"], "label": clean_text(x.get("label"), "اسم النوع", 1, 80),
                        "emoji": emoji.strip()})
        t["types"] = out
    if t["enabled"] and not t["category_id"]:
        raise ApiError("اختر كاتيجوري التذاكر قبل التفعيل")


def _v_apps(guild, actor, d, cfg):
    a = cfg["applications"]
    if not isinstance(d, dict):
        raise ApiError("قيمة غير صالحة: applications")
    if "enabled" in d:
        a["enabled"] = _bool(d, "enabled")
    if "review_channel_id" in d:
        a["review_channel_id"] = _text_channel(guild, d["review_channel_id"], "روم المراجعة")
    if "panel" in d:
        a["panel"] = _panel(guild, d["panel"], a["panel"])
    if "forms" in d:
        forms = d["forms"]
        if not isinstance(forms, list) or len(forms) > 10:
            raise ApiError("الحد الأقصى 10 نماذج تقديم")
        out, seen = [], set()
        for f in forms:
            if (not isinstance(f, dict) or not isinstance(f.get("id"), str)
                    or not ID_RE.match(f["id"]) or f["id"] in seen):
                raise ApiError("نموذج تقديم غير صالح")
            seen.add(f["id"])
            qs = f.get("questions")
            if not isinstance(qs, list) or not 1 <= len(qs) <= 5:
                raise ApiError("كل نموذج فيه من 1 إلى 5 أسئلة")
            out.append({
                "id": f["id"], "name": clean_text(f.get("name"), "اسم النموذج", 1, 45),
                "questions": [clean_text(q, "السؤال", 1, 45) for q in qs],
                "role_id": _editable_role(guild, actor, f.get("role_id"), "رتبة القبول"),
                "open": bool(f.get("open", True)),
            })
        a["forms"] = out
    if a["enabled"] and not a["review_channel_id"]:
        raise ApiError("اختر روم مراجعة التقديمات قبل التفعيل")


def _v_verify(guild, actor, d, cfg):
    v = cfg["verify"]
    if not isinstance(d, dict):
        raise ApiError("قيمة غير صالحة: verify")
    if "enabled" in d:
        v["enabled"] = _bool(d, "enabled")
    if "role_id" in d:
        v["role_id"] = _editable_role(guild, actor, d["role_id"], "رتبة التحقق")
    if "remove_role_id" in d:
        v["remove_role_id"] = _editable_role(guild, actor, d["remove_role_id"], "الرتبة المسحوبة")
    if "panel" in d:
        v["panel"] = _panel(guild, d["panel"], v["panel"])
    if v["enabled"] and not v["role_id"]:
        raise ApiError("اختر رتبة التحقق قبل التفعيل")


def _v_antinuke(guild, d, cfg):
    a = cfg["antinuke"]
    if not isinstance(d, dict):
        raise ApiError("قيمة غير صالحة: antinuke")
    for k in ("enabled", "protect_perms", "block_new_bots"):
        if k in d:
            a[k] = _bool(d, k)
    if "limit" in d:
        a["limit"] = parse_int(d["limit"], 2, 20, "الحد")
    if "per_seconds" in d:
        a["per_seconds"] = parse_int(d["per_seconds"], 3, 120, "المدة")
    if "action" in d:
        if d["action"] not in ("strip", "kick", "ban"):
            raise ApiError("إجراء غير صالح")
        a["action"] = d["action"]
    if "whitelist_ids" in d:
        ids = d["whitelist_ids"]
        if not isinstance(ids, list) or len(ids) > 20:
            raise ApiError("القائمة البيضاء: حد أقصى 20 معرّف")
        a["whitelist_ids"] = sorted({parse_id(i, "معرّف مستخدم") for i in ids})


def _v_automod(d, cfg):
    am = cfg["automod"]
    if not isinstance(d, dict):
        raise ApiError("قيمة غير صالحة: automod")
    for k in ("mention_enabled", "links_enabled", "emoji_enabled", "dup_enabled"):
        if k in d:
            am[k] = _bool(d, k)
    if "mention_max" in d:
        am["mention_max"] = parse_int(d["mention_max"], 1, 50, "أقصى منشن")
    if "emoji_max" in d:
        am["emoji_max"] = parse_int(d["emoji_max"], 1, 100, "أقصى إيموجي")
    if "dup_count" in d:
        am["dup_count"] = parse_int(d["dup_count"], 2, 10, "عدد التكرار")
    if "links_allow" in d:
        lst = d["links_allow"]
        if not isinstance(lst, list) or len(lst) > 50:
            raise ApiError("الدومينات المسموحة: حد أقصى 50")
        out = []
        for x in lst:
            x = str(x).strip().lower()
            if x and not DOMAIN_RE.match(x):
                raise ApiError(f"دومين غير صالح: {x[:30]}")
            if x and x not in out:
                out.append(x)
        am["links_allow"] = out


LOG_KEYS = tuple(eventlog.LOG_CATEGORIES)
EVENT_KEYS = ("message_delete", "message_edit", "member_join", "member_leave", "member_update", "voice",
              "channel", "role", "invite", "ban", "kick", "timeout", "guild")


def _v_logs(guild, d, cfg):
    lg = cfg["logs"]
    if not isinstance(d, dict):
        raise ApiError("قيمة غير صالحة: logs")
    if "enabled" in d:
        lg["enabled"] = _bool(d, "enabled")
    if "channels" in d:
        if not isinstance(d["channels"], dict):
            raise ApiError("قيمة غير صالحة: channels")
        for k, v in d["channels"].items():
            if k not in LOG_KEYS:
                raise ApiError("نوع سجل غير معروف")
            lg["channels"][k] = _text_channel(guild, v, "روم السجل")
    if "events" in d:
        if not isinstance(d["events"], dict):
            raise ApiError("قيمة غير صالحة: events")
        for k, v in d["events"].items():
            if k not in EVENT_KEYS or not isinstance(v, bool):
                raise ApiError("حدث سجل غير صالح")
            lg["events"][k] = v


def validate_extra(guild, actor, body, cfg):
    if body.get("logs") is not None:
        _v_logs(guild, body["logs"], cfg)
    if body.get("tickets") is not None:
        _v_tickets(guild, body["tickets"], cfg)
    if body.get("applications") is not None:
        _v_apps(guild, actor, body["applications"], cfg)
    if body.get("verify") is not None:
        _v_verify(guild, actor, body["verify"], cfg)
    if body.get("antinuke") is not None:
        _v_antinuke(guild, body["antinuke"], cfg)
    if body.get("automod") is not None:
        _v_automod(body["automod"], cfg)


class Dashboard:
    def __init__(self, bot, config, send_log=None, stats=None):
        self.bot = bot
        self.config = config
        self.send_log = send_log
        self.stats = stats
        self.client_id = os.getenv("CLIENT_ID", "").strip()
        self.client_secret = os.getenv("CLIENT_SECRET", "").strip()
        self.base_url = os.getenv("DASHBOARD_URL", "http://localhost:8000").strip().rstrip("/")
        self.host = os.getenv("DASHBOARD_HOST", "127.0.0.1").strip()
        self.port = int(os.getenv("DASHBOARD_PORT", "8000"))
        self.secure_cookie = self.base_url.startswith("https://")
        self.sessions: dict[str, dict] = {}
        self.states: dict[str, float] = {}
        self.audit: dict[int, deque] = defaultdict(lambda: deque(maxlen=50))

    # ───────── تشغيل ─────────
    async def start(self):
        app = web.Application(middlewares=[self.middleware])
        r = app.router
        r.add_get("/", self.index)
        r.add_get("/login", self.login)
        r.add_get("/callback", self.callback)
        r.add_post("/api/logout", self.logout)
        r.add_get("/api/me", self.me)
        r.add_get("/api/guilds", self.guilds)
        r.add_get("/api/templates", self.templates)
        g = "/api/guilds/{gid}"
        r.add_get(g + "/info", self.g(self.h_info))
        r.add_get(g + "/overview", self.g(self.h_overview))
        r.add_get(g + "/tickets", self.g(self.h_tickets))
        r.add_post(g + "/panel/{kind}", self.g(self.h_panel))
        r.add_post(g + "/logs/test", self.g(self.h_logs_test))
        r.add_post(g + "/logs/create", self.g(self.h_logs_create, "manage_channels"))
        r.add_post(g + "/lockdown", self.g(self.h_lockdown, "manage_channels"))
        r.add_get(g + "/config", self.g(self.h_get_config))
        r.add_put(g + "/config", self.g(self.h_put_config))
        r.add_get(g + "/audit", self.g(self.h_audit))
        r.add_post(g + "/setup", self.g(self.h_setup, "administrator"))
        r.add_post(g + "/announce", self.g(self.h_announce, "manage_messages"))
        r.add_post(g + "/purge", self.g(self.h_purge, "manage_messages"))
        r.add_post(g + "/channels", self.g(self.h_channel_create, "manage_channels"))
        r.add_patch(g + "/channels/{cid}", self.g(self.h_channel_rename, "manage_channels"))
        r.add_delete(g + "/channels/{cid}", self.g(self.h_channel_delete, "manage_channels"))
        r.add_post(g + "/roles", self.g(self.h_role_create, "manage_roles"))
        r.add_delete(g + "/roles/{rid}", self.g(self.h_role_delete, "manage_roles"))
        r.add_get(g + "/members", self.g(self.h_members))
        r.add_post(g + "/members/{uid}/kick", self.g(self.h_kick, "kick_members"))
        r.add_post(g + "/members/{uid}/ban", self.g(self.h_ban, "ban_members"))
        r.add_post(g + "/members/{uid}/timeout", self.g(self.h_timeout, "moderate_members"))
        r.add_post(g + "/members/{uid}/roles", self.g(self.h_member_roles, "manage_roles"))
        r.add_static("/static/", STATIC_DIR)

        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, self.host, self.port).start()
        log.info("لوحة التحكم تعمل على %s (المنفذ %s)", self.base_url, self.port)

    # ───────── حماية عامة ─────────
    @web.middleware
    async def middleware(self, request: web.Request, handler):
        # حماية CSRF: أي طلب يغيّر شيئاً لازم يحمل هذا الهيدر (المواقع الأخرى ما تقدر ترسله)
        if request.method not in ("GET", "HEAD") and request.headers.get("X-Requested-With") != "shield":
            return fail("طلب غير صالح", 403)
        try:
            resp = await handler(request)
        except web.HTTPException as ex:
            resp = ex
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' https://cdn.discordapp.com data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        if isinstance(resp, web.HTTPException):
            raise resp
        return resp

    def session(self, request: web.Request):
        token = request.cookies.get(SESSION_COOKIE)
        s = self.sessions.get(token) if token else None
        if not s:
            return None
        if s["expires"] < time.time():
            self.sessions.pop(token, None)
            return None
        return s

    def g(self, fn, perm: str | None = None):
        """يغلّف أي مسار خاص بسيرفر: تسجيل دخول + عضوية + صلاحيات + معالجة أخطاء."""
        async def wrapper(request: web.Request):
            sess = self.session(request)
            if not sess:
                return fail("سجّل الدخول أولاً", 401)
            try:
                guild = self.bot.get_guild(int(request.match_info["gid"]))
            except ValueError:
                guild = None
            if not guild:
                return fail("البوت غير موجود في هذا السيرفر", 404)
            uid = int(sess["user"]["id"])
            actor = guild.get_member(uid)
            if actor is None:
                try:
                    actor = await guild.fetch_member(uid)
                except discord.HTTPException:
                    return fail("أنت لست عضواً في هذا السيرفر", 403)
            p = actor.guild_permissions
            if not (p.administrator or p.manage_guild):
                return fail("تحتاج صلاحية Manage Server", 403)
            if perm and not (p.administrator or getattr(p, perm)):
                return fail(f"تحتاج صلاحية {perm} لهذا الإجراء", 403)
            body = {}
            if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.can_read_body:
                try:
                    body = await request.json()
                except Exception:
                    return fail("بيانات غير صالحة", 400)
                if not isinstance(body, dict):
                    return fail("بيانات غير صالحة", 400)
            try:
                return await fn(request, guild, actor, body)
            except ApiError as e:
                return fail(str(e), e.status)
            except discord.Forbidden:
                return fail("البوت ما عنده صلاحية كافية، أو رتبته أقل من الهدف (ارفع رتبة البوت)", 403)
            except discord.HTTPException as e:
                return fail(f"رفض ديسكورد الطلب: {e.text or e.status}", 400)
            except Exception:
                log.exception("خطأ داخلي في لوحة التحكم")
                return fail("خطأ داخلي", 500)
        return wrapper

    # ───────── تسجيل الدخول ─────────
    async def index(self, request):
        return web.FileResponse(STATIC_DIR / "index.html")

    async def login(self, request):
        state = secrets.token_urlsafe(24)
        self.states[state] = time.time() + 600
        for s in [s for s, t in self.states.items() if t < time.time()]:
            self.states.pop(s, None)
        params = {
            "client_id": self.client_id, "response_type": "code",
            "redirect_uri": f"{self.base_url}/callback", "scope": "identify guilds",
            "state": state, "prompt": "none",
        }
        url = "https://discord.com/oauth2/authorize?" + urlencode(params)
        resp = web.HTTPFound(url)
        resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True,
                        samesite="Lax", secure=self.secure_cookie, path="/")
        raise resp

    async def callback(self, request):
        q = request.query
        if q.get("error"):
            raise web.HTTPFound("/")
        state = q.get("state", "")
        cookie = request.cookies.get(STATE_COOKIE, "")
        if not state or not cookie or not hmac.compare_digest(state, cookie) or state not in self.states:
            return web.Response(text="جلسة تسجيل الدخول غير صالحة. ارجع وحاول مرة ثانية.", status=400)
        self.states.pop(state, None)
        code = q.get("code")
        if not code:
            return web.Response(text="لم يصل رمز التفويض.", status=400)

        async with aiohttp.ClientSession() as http:
            async with http.post(f"{DISCORD_API}/oauth2/token", data={
                "client_id": self.client_id, "client_secret": self.client_secret,
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": f"{self.base_url}/callback",
            }) as r:
                if r.status != 200:
                    log.warning("فشل تبادل الرمز: %s %s", r.status, await r.text())
                    return web.Response(
                        text="فشل تسجيل الدخول. تأكد من CLIENT_SECRET ومن إضافة رابط Redirect في Developer Portal.",
                        status=400)
                access_token = (await r.json())["access_token"]
            headers = {"Authorization": f"Bearer {access_token}"}
            async with http.get(f"{DISCORD_API}/users/@me", headers=headers) as r:
                user = await r.json()
            async with http.get(f"{DISCORD_API}/users/@me/guilds", headers=headers) as r:
                guilds = await r.json() if r.status == 200 else []

        now = time.time()
        for t in [t for t, s in self.sessions.items() if s["expires"] < now]:
            self.sessions.pop(t, None)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = {
            "expires": now + SESSION_TTL,
            "user": {k: user.get(k) for k in ("id", "username", "global_name", "avatar")},
            "guilds": [{k: g.get(k) for k in ("id", "name", "icon", "owner", "permissions")} for g in guilds],
        }
        resp = web.HTTPFound("/")
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True,
                        samesite="Lax", secure=self.secure_cookie, path="/")
        resp.del_cookie(STATE_COOKIE, path="/")
        raise resp

    async def logout(self, request):
        token = request.cookies.get(SESSION_COOKIE)
        self.sessions.pop(token, None)
        resp = ok()
        resp.del_cookie(SESSION_COOKIE, path="/")
        return resp

    async def me(self, request):
        s = self.session(request)
        if not s:
            return fail("غير مسجل", 401)
        return web.json_response({"user": s["user"]})

    async def guilds(self, request):
        s = self.session(request)
        if not s:
            return fail("غير مسجل", 401)
        out = []
        for g in s["guilds"]:
            perms = int(g.get("permissions") or 0)
            if g.get("owner") or perms & (ADMINISTRATOR | MANAGE_GUILD):
                out.append({"id": g["id"], "name": g["name"], "icon": g.get("icon"),
                            "bot_in": self.bot.get_guild(int(g["id"])) is not None})
        out.sort(key=lambda x: (not x["bot_in"], x["name"].lower()))
        return web.json_response({"guilds": out, "client_id": self.client_id,
                                  "invite_perms": str(INVITE_PERMS.value)})

    async def templates(self, request):
        if not self.session(request):
            return fail("غير مسجل", 401)
        return web.json_response({"templates": summarize()})

    # ───────── أدوات ─────────
    def cfg_out(self, guild):
        cfg = self.config.get(guild.id)
        if cfg["log_channel_id"]:
            cfg["log_channel_id"] = str(cfg["log_channel_id"])
        for sec, key in (("welcome", "channel_id"), ("goodbye", "channel_id"), ("autorole", "role_id"),
                         ("tickets", "category_id"), ("tickets", "log_channel_id"),
                         ("applications", "review_channel_id"), ("verify", "role_id"),
                         ("verify", "remove_role_id")):
            if cfg[sec][key]:
                cfg[sec][key] = str(cfg[sec][key])
        cfg["logs"]["channels"] = {k: (str(v) if v else None) for k, v in cfg["logs"]["channels"].items()}
        for sec in ("tickets", "applications", "verify"):
            if cfg[sec]["panel"]["channel_id"]:
                cfg[sec]["panel"]["channel_id"] = str(cfg[sec]["panel"]["channel_id"])
        cfg["tickets"]["support_role_ids"] = [str(i) for i in cfg["tickets"]["support_role_ids"]]
        cfg["antinuke"]["whitelist_ids"] = [str(i) for i in cfg["antinuke"]["whitelist_ids"]]
        for f in cfg["applications"]["forms"]:
            if f.get("role_id"):
                f["role_id"] = str(f["role_id"])
        return cfg

    def bot_missing(self, guild):
        gp = guild.me.guild_permissions
        return [label for p, label in WATCHED_PERMS.items() if not getattr(gp, p)]

    async def record(self, guild, actor, text):
        self.audit[guild.id].appendleft({
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "user": f"{actor} ({actor.id})", "action": text})
        if self.send_log:
            try:
                await self.send_log(guild, "🖥️ إجراء من لوحة التحكم",
                                    f"**المنفذ:** {actor.mention}\n**الإجراء:** {text}",
                                    discord.Color.blurple())
            except Exception:
                log.exception("تعذر كتابة السجل")

    @staticmethod
    def reason(body, actor) -> str:
        r = str(body.get("reason") or "")[:200]
        return f"لوحة التحكم ({actor}): {r}".strip()

    async def get_member(self, guild, uid: int) -> discord.Member:
        m = guild.get_member(uid)
        if m:
            return m
        try:
            return await guild.fetch_member(uid)
        except discord.NotFound:
            raise ApiError("العضو غير موجود في السيرفر", 404)

    def ensure_target(self, guild, actor, target):
        problem = target_problem(guild, actor, target)
        if problem:
            raise ApiError(problem, 403)

    def member_json(self, guild, actor, m: discord.Member):
        return {
            "id": str(m.id), "name": m.name, "display": m.display_name, "bot": m.bot,
            "avatar": m.display_avatar.url, "top_role": m.top_role.name,
            "roles": [str(r.id) for r in m.roles if not r.is_default()],
            "timed_out": m.is_timed_out(),
            "manageable": target_problem(guild, actor, m) is None,
        }

    # ───────── معلومات وإعدادات ─────────
    async def h_info(self, request, guild, actor, body):
        me = guild.me
        channels = [{
            "id": str(c.id), "name": c.name, "type": channel_kind(c), "position": c.position,
            "parent_id": str(c.category_id) if getattr(c, "category_id", None) else None,
        } for c in guild.channels]
        roles = [{
            "id": str(r.id), "name": r.name, "color": f"#{r.colour.value:06x}",
            "position": r.position, "managed": r.managed,
            "editable": role_editable(guild, actor, r),
        } for r in sorted(guild.roles, key=lambda r: -r.position) if not r.is_default()]
        return web.json_response({
            "guild": {"id": str(guild.id), "name": guild.name,
                      "icon": guild.icon.key if guild.icon else None, "members": guild.member_count},
            "channels": channels, "roles": roles,
            "perms": [p for p in ACTOR_PERMS if getattr(actor.guild_permissions, p)],
            "bot_missing": self.bot_missing(guild),
            "config": self.cfg_out(guild),
        })

    def health(self, guild, cfg):
        perms = [r.permissions for r in guild.roles if not r.is_default() and not r.managed]
        has_staff = any(p.administrator or p.kick_members or p.ban_members or p.manage_messages for p in perms)
        has_rules = any(k in c.name.lower() for c in guild.text_channels for k in ("قوانين", "rules", "القواعد"))
        checks = [
            ("قناة السجل محددة", bool(cfg["log_channel_id"]), 8, "protect"),
            ("مكافحة السبام مفعّلة", cfg["antispam"]["enabled"], 12, "protect"),
            ("حماية الغارات مفعّلة", cfg["antiraid"]["enabled"], 12, "protect"),
            ("فحص عمر الحساب مفعّل", cfg["antiraid"]["min_account_age_days"] > 0, 8, "protect"),
            ("فلتر الكلمات المحظورة فيه كلمات", bool(cfg["banned_words"]), 8, "protect"),
            ("منع روابط الدعوات مفعّل", cfg["block_invites"], 4, "protect"),
            ("صلاحيات البوت كاملة", not self.bot_missing(guild), 12, None),
            ("الحماية من التخريب (Anti-Nuke) مفعّلة", cfg["antinuke"]["enabled"], 12, "advanced"),
            ("حماية المنشن الجماعي مفعّلة", cfg["automod"]["mention_enabled"], 6, "advanced"),
            ("توجد رتبة إشراف", has_staff, 8, "setup"),
            ("يوجد روم للقوانين", has_rules, 4, "setup"),
            ("رسالة الترحيب مفعّلة", cfg["welcome"]["enabled"], 3, "welcome"),
            ("نظام التحقق مفعّل", cfg["verify"]["enabled"], 3, "verify"),
        ]
        score = sum(w for _, ok_, w, _ in checks if ok_)
        label = "ممتاز" if score >= 80 else "جيد" if score >= 60 else "متوسط" if score >= 40 else "ضعيف"
        return {"score": score, "label": label,
                "checks": [{"label": l, "ok": bool(o), "tab": t} for l, o, _, t in checks]}

    async def h_overview(self, request, guild, actor, body):
        cfg = self.config.get(guild.id)
        st = self.stats.summary(guild.id) if self.stats else {"messages": 0, "voice_min": 0, "mod": 0, "net_members": 0}
        systems = [cfg["antispam"]["enabled"], cfg["antiraid"]["enabled"], bool(cfg["log_channel_id"]),
                   bool(cfg["banned_words"]), cfg["block_invites"], cfg["welcome"]["enabled"],
                   cfg["goodbye"]["enabled"], cfg["autorole"]["enabled"],
                   cfg["autoreply"]["enabled"] and bool(cfg["autoreply"]["rules"]),
                   cfg["antinuke"]["enabled"], cfg["automod"]["mention_enabled"],
                   cfg["tickets"]["enabled"], cfg["applications"]["enabled"], cfg["verify"]["enabled"],
                   cfg["logs"]["enabled"] and any(cfg["logs"]["channels"].values())]
        lat = self.bot.latency
        return web.json_response({
            "stats": {"members": guild.member_count, **st},
            "health": self.health(guild, cfg),
            "systems": {"active": sum(1 for x in systems if x), "total": len(systems)},
            "bot": {"ping_ms": round(lat * 1000) if lat == lat and lat != float("inf") else None,
                    "uptime_s": int(time.time() - getattr(self.bot, "started_at", time.time())),
                    "missing": self.bot_missing(guild)},
        })

    async def h_get_config(self, request, guild, actor, body):
        return web.json_response({"config": self.cfg_out(guild)})

    async def h_put_config(self, request, guild, actor, body):
        cfg = self.config.get(guild.id)

        if "log_channel_id" in body:
            v = body["log_channel_id"]
            if v in (None, ""):
                cfg["log_channel_id"] = None
            else:
                ch = guild.get_channel(parse_id(v, "قناة السجل"))
                if not isinstance(ch, discord.TextChannel):
                    raise ApiError("قناة السجل لازم تكون روم كتابي")
                cfg["log_channel_id"] = ch.id

        if "block_invites" in body:
            if not isinstance(body["block_invites"], bool):
                raise ApiError("قيمة غير صالحة: block_invites")
            cfg["block_invites"] = body["block_invites"]

        if "banned_words" in body:
            words = body["banned_words"]
            if not isinstance(words, list) or len(words) > 300:
                raise ApiError("قائمة الكلمات غير صالحة (الحد 300 كلمة)")
            clean = []
            for w in words:
                if not isinstance(w, str):
                    raise ApiError("كلمة غير صالحة")
                w = w.strip()
                if not w:
                    continue
                if len(w) > 50:
                    raise ApiError("الكلمة الواحدة لا تزيد عن 50 حرف")
                if w not in clean:
                    clean.append(w)
            cfg["banned_words"] = clean

        for section, spec in CONFIG_SPEC.items():
            data = body.get(section)
            if data is None:
                continue
            if not isinstance(data, dict):
                raise ApiError(f"قيمة غير صالحة: {section}")
            for key, rule in spec.items():
                if key not in data:
                    continue
                if rule is bool:
                    if not isinstance(data[key], bool):
                        raise ApiError(f"قيمة غير صالحة: {key}")
                    cfg[section][key] = data[key]
                else:
                    cfg[section][key] = parse_int(data[key], rule[0], rule[1], key)

        def need_bool(d, k):
            if not isinstance(d[k], bool):
                raise ApiError(f"قيمة غير صالحة: {k}")
            return d[k]

        def text_channel(v):
            if v in (None, ""):
                return None
            ch = guild.get_channel(parse_id(v, "الروم"))
            if not isinstance(ch, discord.TextChannel):
                raise ApiError("اختر روماً كتابياً")
            return ch.id

        for sec, label in (("welcome", "الترحيب"), ("goodbye", "المغادرة")):
            d = body.get(sec)
            if d is None:
                continue
            if not isinstance(d, dict):
                raise ApiError(f"قيمة غير صالحة: {sec}")
            if "enabled" in d:
                cfg[sec]["enabled"] = need_bool(d, "enabled")
            if "channel_id" in d:
                cfg[sec]["channel_id"] = text_channel(d["channel_id"])
            if "message" in d:
                cfg[sec]["message"] = clean_text(d["message"], "الرسالة", 1, 1000)
            if sec == "welcome" and "embed" in d:
                cfg[sec]["embed"] = need_bool(d, "embed")
            if cfg[sec]["enabled"] and not cfg[sec]["channel_id"]:
                raise ApiError(f"اختر روماً لرسالة {label} قبل التفعيل")

        d = body.get("autorole")
        if d is not None:
            if not isinstance(d, dict):
                raise ApiError("قيمة غير صالحة: autorole")
            if "enabled" in d:
                cfg["autorole"]["enabled"] = need_bool(d, "enabled")
            if "role_id" in d:
                if d["role_id"] in (None, ""):
                    cfg["autorole"]["role_id"] = None
                else:
                    role = guild.get_role(parse_id(d["role_id"], "الرتبة"))
                    if not role or not role_editable(guild, actor, role):
                        raise ApiError("ما تقدر تستخدم هذي الرتبة (أعلى منك أو من البوت، أو رتبة بوت)")
                    cfg["autorole"]["role_id"] = role.id
            if cfg["autorole"]["enabled"] and not cfg["autorole"]["role_id"]:
                raise ApiError("اختر رتبة قبل تفعيل الرتبة التلقائية")

        d = body.get("autoreply")
        if d is not None:
            if not isinstance(d, dict):
                raise ApiError("قيمة غير صالحة: autoreply")
            if "enabled" in d:
                cfg["autoreply"]["enabled"] = need_bool(d, "enabled")
            if "rules" in d:
                if not isinstance(d["rules"], list) or len(d["rules"]) > 50:
                    raise ApiError("الحد الأقصى 50 رد تلقائي")
                rules = []
                for r in d["rules"]:
                    if not isinstance(r, dict) or r.get("match") not in ("contains", "exact"):
                        raise ApiError("رد تلقائي غير صالح")
                    rules.append({"trigger": clean_text(r.get("trigger"), "الكلمة", 1, 100),
                                  "response": clean_text(r.get("response"), "الرد", 1, 1000),
                                  "match": r["match"]})
                cfg["autoreply"]["rules"] = rules

        validate_extra(guild, actor, body, cfg)
        self.config.update(guild.id, cfg)
        await self.record(guild, actor, "تحديث الإعدادات")
        return ok(config=self.cfg_out(guild))

    async def h_audit(self, request, guild, actor, body):
        return web.json_response({"items": list(self.audit[guild.id])})

    # ───────── الإعداد السريع (يتطلب Administrator) ─────────
    async def h_setup(self, request, guild, actor, body):
        key = body.get("template")
        if key not in TEMPLATES:
            raise ApiError("قالب غير معروف")
        try:
            rep = await apply_template(guild, key, actor=actor, config=self.config,
                                       post_messages=bool(body.get("post_messages", True)))
        except SetupError as e:
            raise ApiError(str(e), 400)
        await self.record(guild, actor,
                          f"تطبيق قالب «{TEMPLATES[key]['name']}»: {len(rep['channels_created'])} روم، "
                          f"{len(rep['roles_created'])} رتبة")
        return ok(report=rep)

    # ───────── التذاكر واللوحات والقفل ─────────
    async def h_tickets(self, request, guild, actor, body):
        items = []
        for c in guild.text_channels:
            if (c.topic or "").startswith("ticket:"):
                p = c.topic.split(":")
                owner = guild.get_member(int(p[1])) if len(p) > 1 and p[1].isdigit() else None
                items.append({"id": str(c.id), "name": c.name, "type": p[2] if len(p) > 2 else "",
                              "owner": owner.display_name if owner else (p[1] if len(p) > 1 else ""),
                              "claimed": bool(len(p) > 3 and p[3])})
        return web.json_response({"items": items})

    async def h_panel(self, request, guild, actor, body):
        kind = request.match_info["kind"]
        if kind not in ("tickets", "applications", "verify"):
            raise ApiError("نوع اللوحة غير معروف")
        try:
            msg = await systems.publish_panel(guild, kind)
        except systems.PanelError as e:
            raise ApiError(str(e))
        await self.record(guild, actor, f"نشر لوحة {kind} في #{msg.channel.name}")
        return ok(url=msg.jump_url)

    async def h_logs_test(self, request, guild, actor, body):
        return web.json_response({"results": await eventlog.test_logs(guild, self.config)})

    async def h_logs_create(self, request, guild, actor, body):
        try:
            names = await eventlog.create_log_channels(guild, self.config, actor)
        except eventlog.LogSetupError as e:
            raise ApiError(str(e))
        await self.record(guild, actor, "إنشاء رومات السجلات تلقائياً")
        return ok(channels=names, config=self.cfg_out(guild))

    async def h_lockdown(self, request, guild, actor, body):
        lock = bool(body.get("lock"))
        try:
            n = await security.lockdown(guild, lock, self.config, actor)
        except security.SecurityError as e:
            raise ApiError(str(e))
        await self.record(guild, actor, f"{'قفل' if lock else 'فتح'} السيرفر ({n} روم)")
        return ok(count=n)

    # ───────── إعلانات وحذف رسائل ─────────
    async def h_announce(self, request, guild, actor, body):
        ch = guild.get_channel(parse_id(body.get("channel_id"), "الروم"))
        if not isinstance(ch, discord.TextChannel):
            raise ApiError("اختر روماً كتابياً")
        if not ch.permissions_for(actor).send_messages or not ch.permissions_for(actor).view_channel:
            raise ApiError("أنت نفسك ما تقدر ترسل في هذا الروم", 403)
        mine = ch.permissions_for(guild.me)
        if not (mine.view_channel and mine.send_messages):
            raise ApiError("البوت ما يقدر يرسل في هذا الروم", 403)
        mentions = discord.AllowedMentions.none()
        if body.get("mode") == "text":
            text = clean_text(body.get("text"), "النص", 1, 2000)
            await ch.send(text, allowed_mentions=mentions)
        else:
            title = str(body.get("title") or "").strip()[:256]
            text = clean_text(body.get("text"), "النص", 1, 4000)
            embed = discord.Embed(title=title or None, description=text,
                                  color=discord.Color(parse_color(body.get("color"))))
            await ch.send(embed=embed, allowed_mentions=mentions)
        await self.record(guild, actor, f"إرسال إعلان في #{ch.name}")
        return ok()

    async def h_purge(self, request, guild, actor, body):
        ch = guild.get_channel(parse_id(body.get("channel_id"), "الروم"))
        if not isinstance(ch, discord.TextChannel):
            raise ApiError("اختر روماً كتابياً")
        if not ch.permissions_for(actor).manage_messages:
            raise ApiError("أنت ما عندك Manage Messages في هذا الروم", 403)
        amount = parse_int(body.get("amount"), 1, 100, "العدد")
        deleted = await ch.purge(limit=amount)
        await self.record(guild, actor, f"حذف {len(deleted)} رسالة من #{ch.name}")
        return ok(deleted=len(deleted))

    # ───────── الرومات ─────────
    async def h_channel_create(self, request, guild, actor, body):
        name = clean_text(body.get("name"), "الاسم", 1, 100)
        kind = body.get("type")
        parent = None
        if body.get("parent_id"):
            parent = guild.get_channel(parse_id(body["parent_id"], "الكاتيجوري"))
            if not isinstance(parent, discord.CategoryChannel):
                raise ApiError("الكاتيجوري غير صالحة")
        reason = self.reason(body, actor)
        if kind == "text":
            ch = await guild.create_text_channel(name, category=parent, reason=reason)
        elif kind == "voice":
            ch = await guild.create_voice_channel(name, category=parent, reason=reason)
        elif kind == "category":
            ch = await guild.create_category(name, reason=reason)
        else:
            raise ApiError("نوع الروم غير صالح")
        await self.record(guild, actor, f"إنشاء روم {ch.name} ({kind})")
        return ok(id=str(ch.id))

    def _channel(self, guild, request):
        ch = guild.get_channel(parse_id(request.match_info["cid"], "الروم"))
        if not ch:
            raise ApiError("الروم غير موجود", 404)
        return ch

    async def h_channel_rename(self, request, guild, actor, body):
        ch = self._channel(guild, request)
        old = ch.name
        await ch.edit(name=clean_text(body.get("name"), "الاسم", 1, 100), reason=self.reason(body, actor))
        await self.record(guild, actor, f"إعادة تسمية الروم {old} إلى {ch.name}")
        return ok()

    async def h_channel_delete(self, request, guild, actor, body):
        ch = self._channel(guild, request)
        name = ch.name
        await ch.delete(reason=self.reason(body, actor))
        await self.record(guild, actor, f"حذف الروم {name}")
        return ok()

    # ───────── الرتب ─────────
    async def h_role_create(self, request, guild, actor, body):
        name = clean_text(body.get("name"), "الاسم", 1, 100)
        # الرتب الجديدة بدون أي صلاحيات (منعاً لرفع الصلاحيات من اللوحة)
        role = await guild.create_role(
            name=name, colour=discord.Colour(parse_color(body.get("color"), 0)),
            hoist=bool(body.get("hoist")), mentionable=bool(body.get("mentionable")),
            reason=self.reason(body, actor))
        await self.record(guild, actor, f"إنشاء رتبة {role.name}")
        return ok(id=str(role.id))

    async def h_role_delete(self, request, guild, actor, body):
        role = guild.get_role(parse_id(request.match_info["rid"], "الرتبة"))
        if not role:
            raise ApiError("الرتبة غير موجودة", 404)
        if not role_editable(guild, actor, role):
            raise ApiError("ما تقدر تحذف هذي الرتبة (أعلى منك/من البوت أو رتبة بوت)", 403)
        name = role.name
        await role.delete(reason=self.reason(body, actor))
        await self.record(guild, actor, f"حذف رتبة {name}")
        return ok()

    # ───────── الأعضاء ─────────
    async def h_members(self, request, guild, actor, body):
        q = request.query.get("q", "").strip().lower()[:50]
        if q:
            found = [m for m in guild.members
                     if q in m.name.lower() or q in m.display_name.lower() or q == str(m.id)]
        else:
            floor = datetime.min.replace(tzinfo=timezone.utc)
            found = sorted(guild.members, key=lambda m: m.joined_at or floor, reverse=True)
        return web.json_response({"members": [self.member_json(guild, actor, m) for m in found[:25]]})

    async def h_kick(self, request, guild, actor, body):
        t = await self.get_member(guild, parse_id(request.match_info["uid"], "العضو"))
        self.ensure_target(guild, actor, t)
        await t.kick(reason=self.reason(body, actor))
        if self.stats:
            self.stats.add(guild.id, "mod")
        await self.record(guild, actor, f"طرد {t} ({t.id})")
        return ok()

    async def h_ban(self, request, guild, actor, body):
        t = await self.get_member(guild, parse_id(request.match_info["uid"], "العضو"))
        self.ensure_target(guild, actor, t)
        await guild.ban(t, delete_message_seconds=0, reason=self.reason(body, actor))
        if self.stats:
            self.stats.add(guild.id, "mod")
        await self.record(guild, actor, f"حظر {t} ({t.id})")
        return ok()

    async def h_timeout(self, request, guild, actor, body):
        t = await self.get_member(guild, parse_id(request.match_info["uid"], "العضو"))
        self.ensure_target(guild, actor, t)
        minutes = parse_int(body.get("minutes"), 0, 40320, "المدة")
        await t.timeout(timedelta(minutes=minutes) if minutes else None, reason=self.reason(body, actor))
        await self.record(guild, actor,
                          f"كتم {t} ({t.id}) لمدة {minutes} دقيقة" if minutes else f"إلغاء كتم {t} ({t.id})")
        return ok()

    async def h_member_roles(self, request, guild, actor, body):
        t = await self.get_member(guild, parse_id(request.match_info["uid"], "العضو"))
        role = guild.get_role(parse_id(body.get("role_id"), "الرتبة"))
        if not role:
            raise ApiError("الرتبة غير موجودة", 404)
        if not role_editable(guild, actor, role):
            raise ApiError("ما تقدر تتحكم بهذي الرتبة (أعلى منك/من البوت أو رتبة بوت)", 403)
        if t.id != actor.id:
            self.ensure_target(guild, actor, t)
        action = body.get("action")
        if action == "add":
            await t.add_roles(role, reason=self.reason(body, actor))
        elif action == "remove":
            await t.remove_roles(role, reason=self.reason(body, actor))
        else:
            raise ApiError("إجراء غير صالح")
        await self.record(guild, actor, f"{'إعطاء' if action == 'add' else 'سحب'} رتبة {role.name} لـ {t}")
        return ok()
