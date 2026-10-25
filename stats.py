"""إحصائيات يومية خفيفة لكل سيرفر (تُحفظ في stats.json وتُحذف بعد 35 يوم)."""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("shield.stats")
KEEP_DAYS = 35


class Stats:
    def __init__(self, path):
        self.path = Path(path)
        self.data: dict = {}
        self.dirty = False
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                log.warning("stats.json تالف، سيبدأ من جديد")

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def add(self, gid: int, key: str, n: float = 1):
        days = self.data.setdefault(str(gid), {}).setdefault("days", {})
        day = days.setdefault(self._today(), {})
        day[key] = day.get(key, 0) + n
        self.dirty = True

    def flush(self):
        if not self.dirty:
            return
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=KEEP_DAYS)).isoformat()
        for g in self.data.values():
            g["days"] = {d: v for d, v in g.get("days", {}).items() if d >= cutoff}
        try:
            self.path.write_text(json.dumps(self.data), encoding="utf-8")
            self.dirty = False
        except OSError:
            log.exception("تعذر حفظ stats.json")

    def summary(self, gid: int, days: int = 7) -> dict:
        store = self.data.get(str(gid), {}).get("days", {})
        today = datetime.now(timezone.utc).date()
        keys = [(today - timedelta(days=d)).isoformat() for d in range(days)]

        def total(k):
            return sum(store.get(d, {}).get(k, 0) for d in keys)

        return {
            "messages": int(total("messages")),
            "voice_min": int(round(total("voice_min"))),
            "mod": int(total("mod")),
            "net_members": int(total("joins") - total("leaves")),
        }
