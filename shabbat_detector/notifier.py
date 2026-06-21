"""
shabbat_detector/notifier.py — מנגנון התראות (Email + Telegram; WhatsApp pluggable).

שני סוגי אירועים:
  1. כניסה/יציאה ממצב שבת (edge-triggered מתוך ה-detector).
  2. אין תנועה ≥ N שעות "לא כולל לילה" (MovementWatchdog).

עיקרון: best-effort — כל כשל בשליחה נרשם ללוג ולעולם לא מפיל את ה-detector.

הרצת בדיקה ידנית:
    python -m shabbat_detector.notifier --test [--config rfid_config.json]
"""
from __future__ import annotations

import json
import logging
import smtplib
import time
from dataclasses import dataclass
from datetime import datetime
from email.mime.text import MIMEText
from typing import Optional

import requests

log = logging.getLogger(__name__)


# ── חישוב "לא כולל לילה" ──────────────────────────────────────────────────────
def _parse_hhmm(s: str) -> int:
    """ממיר 'HH:MM' לשניות מתחילת היום."""
    try:
        h, m = str(s).split(":")
        return int(h) * 3600 + int(m) * 60
    except Exception:
        return 0


def night_seconds_between(start: float, end: float,
                          night_start: str = "23:00", night_end: str = "06:00") -> float:
    """כמה שניות בקטע [start, end] נופלות בתוך חלון הלילה (תומך במעבר חצות)."""
    if end <= start:
        return 0.0
    ns = _parse_hhmm(night_start)
    ne = _parse_hhmm(night_end)
    if ns == ne:
        return 0.0  # אין חלון לילה מוגדר

    total = 0.0
    midnight = datetime.fromtimestamp(start).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    cur = midnight - 86400  # יום אחד אחורה כדי לתפוס לילה שהתחיל אמש
    while cur < end:
        a = cur + ns
        b = cur + ne
        if b <= a:
            # חלון חוצה חצות: [a, חצות הבא] + [חצות זה, b]
            intervals = [(a, cur + 86400), (cur, b)]
        else:
            intervals = [(a, b)]
        for lo, hi in intervals:
            lo = max(lo, start)
            hi = min(hi, end)
            if hi > lo:
                total += hi - lo
        cur += 86400
    return total


def daytime_seconds_between(start: float, end: float,
                            night_start: str = "23:00", night_end: str = "06:00") -> float:
    """שניות-יום בלבד שחלפו בין start ל-end (מחסיר את חלון הלילה)."""
    return max(0.0, (end - start) - night_seconds_between(start, end, night_start, night_end))


class MovementWatchdog:
    """עוקב אחרי זמן התנועה האחרון ויורה פעם אחת כשחוסר-תנועת-יום חוצה את הסף."""

    def __init__(self, threshold_hours: float = 10.0,
                 night_start: str = "23:00", night_end: str = "06:00",
                 now: Optional[float] = None):
        self.threshold_s = float(threshold_hours) * 3600
        self.night_start = night_start
        self.night_end = night_end
        self.last_movement_ts = now if now is not None else time.time()
        self.alerted = False

    def update_settings(self, threshold_hours: float, night_start: str, night_end: str) -> None:
        self.threshold_s = float(threshold_hours) * 3600
        self.night_start = night_start
        self.night_end = night_end

    def record_movement(self, ts: Optional[float] = None) -> None:
        """נקרא בכל תנועת-מעלית אמיתית (אירוע קומה חדש)."""
        self.last_movement_ts = ts if ts is not None else time.time()
        self.alerted = False

    def check(self, now: Optional[float] = None) -> bool:
        """מחזיר True בדיוק פעם אחת כשחוצים את הסף; מתאפס ב-record_movement."""
        if self.alerted:
            return False
        now = now if now is not None else time.time()
        daytime = daytime_seconds_between(
            self.last_movement_ts, now, self.night_start, self.night_end
        )
        if daytime >= self.threshold_s:
            self.alerted = True
            return True
        return False

    def to_dict(self) -> dict:
        return {"last_movement_ts": self.last_movement_ts, "alerted": self.alerted}

    def load_dict(self, d: dict) -> None:
        if not d:
            return
        self.last_movement_ts = d.get("last_movement_ts", self.last_movement_ts)
        self.alerted = bool(d.get("alerted", False))


# ── ערוצים (pluggable) ────────────────────────────────────────────────────────
@dataclass
class Sender:
    name: str = "base"

    def send(self, subject: str, body: str) -> None:
        raise NotImplementedError


class TelegramSender(Sender):
    def __init__(self, cfg: dict):
        super().__init__("telegram")
        self.token = cfg.get("bot_token", "")
        self.chat_id = cfg.get("chat_id", "")

    def send(self, subject: str, body: str) -> None:
        if not self.token or not self.chat_id:
            raise ValueError("חסר bot_token או chat_id ל-Telegram")
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        text = f"*{subject}*\n{body}"
        r = requests.post(url, json={
            "chat_id": self.chat_id, "text": text, "parse_mode": "Markdown",
        }, timeout=10)
        r.raise_for_status()


class EmailSender(Sender):
    def __init__(self, cfg: dict):
        super().__init__("email")
        self.host = cfg.get("smtp_host", "")
        self.port = int(cfg.get("smtp_port", 587))
        self.username = cfg.get("username", "")
        self.password = cfg.get("password", "")
        self.sender = cfg.get("from") or cfg.get("username", "")
        to = cfg.get("to", [])
        self.recipients = [to] if isinstance(to, str) else list(to or [])

    def send(self, subject: str, body: str) -> None:
        if not self.host or not self.recipients:
            raise ValueError("חסר smtp_host או נמענים ל-Email")
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        with smtplib.SMTP(self.host, self.port, timeout=15) as s:
            s.ehlo()
            try:
                s.starttls()
                s.ehlo()
            except smtplib.SMTPException:
                pass  # שרת ללא TLS — ממשיכים
            if self.username:
                s.login(self.username, self.password)
            s.sendmail(self.sender, self.recipients, msg.as_string())


class WhatsAppSender(Sender):
    """שלד pluggable להמשך — לא ממומש כעת (ראו התוכנית: Cloud API / Twilio / CallMeBot)."""

    def __init__(self, cfg: dict):
        super().__init__("whatsapp")

    def send(self, subject: str, body: str) -> None:
        raise NotImplementedError("ערוץ WhatsApp עדיין לא ממומש")


_SENDER_TYPES = {
    "telegram": TelegramSender,
    "email": EmailSender,
    "whatsapp": WhatsAppSender,
}


# ── ה-Notifier הראשי ──────────────────────────────────────────────────────────
class Notifier:
    def __init__(self, config: Optional[dict], elevator_id: str = "?"):
        self.cfg = config or {}
        self.elevator_id = elevator_id
        self.enabled = bool(self.cfg.get("enabled", False))
        self.events = self.cfg.get("events", {})
        self._senders = self._build_senders()

    def _build_senders(self) -> list[Sender]:
        senders: list[Sender] = []
        channels = self.cfg.get("channels", {}) or {}
        for name, klass in _SENDER_TYPES.items():
            ch = channels.get(name) or {}
            if ch.get("enabled"):
                try:
                    senders.append(klass(ch))
                except Exception as e:
                    log.warning("לא ניתן לאתחל ערוץ %s: %s", name, e)
        return senders

    def _send(self, subject: str, body: str) -> None:
        if not self.enabled:
            return
        for s in self._senders:
            try:
                s.send(subject, body)
                log.info("התראה נשלחה דרך %s", s.name)
            except Exception as e:
                log.warning("שליחת התראה דרך %s נכשלה: %s", s.name, e)

    def _stamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── אירועים ────────────────────────────────────────────────────────────────
    def notify_shabbat_change(self, active: bool, reason: str = "") -> None:
        event_key = "shabbat_enter" if active else "shabbat_exit"
        if not self.events.get(event_key, True):
            return
        if active:
            subject = f"🕯️ מעלית {self.elevator_id}: נכנסה למצב שבת"
        else:
            subject = f"✅ מעלית {self.elevator_id}: יצאה ממצב שבת"
        body = f"זמן: {self._stamp()}"
        if reason:
            body += f"\nסיבה: {reason}"
        self._send(subject, body)

    def notify_no_movement(self, hours: float, last_movement_ts: float) -> None:
        if not self.events.get("no_movement", True):
            return
        subject = f"⚠️ מעלית {self.elevator_id}: אין תנועה {hours:.0f}+ שעות (לא כולל לילה)"
        last = datetime.fromtimestamp(last_movement_ts).strftime("%Y-%m-%d %H:%M:%S")
        body = (f"לא זוהתה תנועה מאז {last}.\n"
                f"זמן בדיקה: {self._stamp()}\n"
                f"ייתכן שהמעלית תקועה או שה-tracker אינו פעיל.")
        self._send(subject, body)

    # ── בדיקה ──────────────────────────────────────────────────────────────────
    def send_test(self) -> list[dict]:
        """שולח הודעת בדיקה בכל ערוץ פעיל ומחזיר תוצאה לכל ערוץ."""
        subject = f"🔔 בדיקת התראות — מעלית {self.elevator_id}"
        body = f"זוהי הודעת בדיקה ממערכת מעלית ה-RFID.\nזמן: {self._stamp()}"
        results = []
        if not self._senders:
            return [{"channel": "—", "ok": False, "error": "אין ערוצים פעילים"}]
        for s in self._senders:
            try:
                s.send(subject, body)
                results.append({"channel": s.name, "ok": True, "error": ""})
            except Exception as e:
                results.append({"channel": s.name, "ok": False, "error": str(e)})
        return results


def _load_notifications(config_path: str) -> tuple[dict, str]:
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    s = cfg.get("settings", cfg)
    elevator_id = str(s.get("ELEVATOR_ID", "?"))
    return cfg.get("notifications", {}), elevator_id


def main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="בדיקת ערוצי התראה")
    parser.add_argument("--config", default="rfid_config.json")
    parser.add_argument("--test", action="store_true", help="שלח הודעת בדיקה")
    args = parser.parse_args()

    notifications, elevator_id = _load_notifications(args.config)
    notifier = Notifier(notifications, elevator_id)
    # לבדיקה: מתעלמים מ-enabled כדי לאמת את הערוצים
    notifier.enabled = True
    if args.test:
        for r in notifier.send_test():
            status = "✓" if r["ok"] else f"✗ ({r['error']})"
            print(f"  {r['channel']}: {status}")


if __name__ == "__main__":
    main()
