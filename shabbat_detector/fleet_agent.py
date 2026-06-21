"""
Fleet agent — דיווח גרסה/סטטוס ועדכון-מרחוק עבור דשבורד-העל (ramada-admin).

חלק 2 בתוכנית `docs/admin-dashboard-plan.md`. רץ כחלק מה-detector (thread-ים
ברקע) ומבוסס על אותו מודל `secret_key` שכבר משמש לכתיבות הקיימות.

מה הוא עושה:
  • דיווח: PATCH /fleet/{ELEVATOR_ID} בהפעלה וכל ~5 דקות עם
    {version: <git sha>, status: "online", last_seen, branch}.
  • עדכון-מרחוק: מאזין (SSE) ל-/fleet/{ELEVATOR_ID}/command. כשמגיעה פקודה
    {action: "update", secret_key, requested_at} → מאמת secret (constant-time)
    ובודק idempotency (requested_at) → `git pull --ff-only origin <branch>` →
    יציאה מבוקרת (systemd עם Restart=always מפעיל מחדש עם הקוד החדש) → מדווח תוצאה.

אבטחה (ראה docs/fleet-remote-update.md):
  • כל פקודה חייבת לכלול secret_key תקין (אלא אם require_command_secret=False).
  • git pull הוא --ff-only ועל branch קבוע — לא מושך merge/ref שרירותי.
  • idempotency לפי requested_at מונע ריצה כפולה ב-reconnect/restart.
"""
from __future__ import annotations

import hmac
import logging
import os
import signal
import subprocess
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from .firebase_client import FirebaseClient

log = logging.getLogger("fleet")

# שורש הריפו (ההורה של חבילת shabbat_detector) — שם רץ git pull.
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(args: list[str], cwd: str = _REPO_DIR, timeout: int = 120) -> tuple[bool, str]:
    """מריץ פקודת git ומחזיר (success, output)."""
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out
    except Exception as e:  # noqa: BLE001 — git/subprocess errors must never crash the detector
        return False, f"{type(e).__name__}: {e}"


def local_version(repo_dir: str = _REPO_DIR) -> str:
    """ה-git sha הקצר הנוכחי, או 'unknown'."""
    ok, out = _git(["rev-parse", "--short", "HEAD"], cwd=repo_dir, timeout=10)
    return out if ok and out else "unknown"


def current_branch(repo_dir: str = _REPO_DIR) -> str:
    """הענף הנוכחי, או 'main' כברירת מחדל."""
    ok, out = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir, timeout=10)
    return out if ok and out and out != "HEAD" else "main"


def _default_restart() -> None:
    """יציאה מבוקרת של ה-detector: SIGTERM → שמירת state → exit → systemd Restart=always."""
    os.kill(os.getpid(), signal.SIGTERM)


class FleetAgent:
    """דיווח סטטוס ועדכון-מרחוק. הלוגיקה הרגישה (אימות/idempotency) חשופה לבדיקות."""

    def __init__(
        self,
        fb: FirebaseClient,
        *,
        secret_key: str = "",
        report_interval: float = 300.0,
        remote_update_enabled: bool = True,
        require_command_secret: bool = True,
        update_branch: Optional[str] = None,
        repo_dir: str = _REPO_DIR,
        restart_callback: Optional[Callable[[], None]] = None,
    ):
        self._fb = fb
        self._secret = secret_key or ""
        self._report_interval = max(30.0, float(report_interval))
        self._remote_update_enabled = remote_update_enabled
        self._require_command_secret = require_command_secret
        self._repo_dir = repo_dir
        self._update_branch = update_branch or current_branch(repo_dir)
        self._restart = restart_callback or _default_restart
        self._running = True
        # requested_at של הפקודה האחרונה שכבר טופלה (idempotency).
        self._last_requested_at: float = 0.0
        self._update_lock = threading.Lock()

    # ── אימות + החלטה (לוגיקה טהורה — נבדקת ב-tests, בלי רשת) ─────────────────

    def _secret_ok(self, provided: Optional[str]) -> bool:
        if not self._require_command_secret:
            return True
        if not self._secret or provided is None:
            return False
        return hmac.compare_digest(str(provided), str(self._secret))

    def should_handle(self, command: Optional[dict]) -> bool:
        """האם לפעול על הפקודה: action==update, secret תקין, ו-requested_at חדש."""
        if not isinstance(command, dict):
            return False
        if command.get("action") != "update":
            return False
        if not self._secret_ok(command.get("secret_key")):
            log.warning("Fleet command rejected: bad/missing secret_key")
            return False
        try:
            req_at = float(command.get("requested_at") or 0)
        except (TypeError, ValueError):
            return False
        if req_at <= self._last_requested_at:
            return False  # פקודה ישנה / כבר טופלה
        return True

    # ── דיווח גרסה/סטטוס ──────────────────────────────────────────────────────

    def _report(self, **extra) -> None:
        now = int(time.time())
        payload = {
            "version": local_version(self._repo_dir),
            "branch": self._update_branch,
            "status": "online",
            "last_seen": now,
            "last_seen_str": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
            **extra,
        }
        self._fb.patch_fleet_status(payload)

    def _report_loop(self) -> None:
        # דיווח ראשוני מיידי, ואז כל report_interval שניות (בפרוסות, להגיב מהר ל-stop).
        while self._running:
            try:
                self._report()
            except Exception as e:  # noqa: BLE001
                log.warning("Fleet report failed: %s", e)
            slept = 0.0
            while self._running and slept < self._report_interval:
                time.sleep(min(5.0, self._report_interval - slept))
                slept += 5.0

    # ── טיפול בפקודות עדכון ────────────────────────────────────────────────────

    def _on_command(self, command: dict) -> None:
        if not self.should_handle(command):
            return
        if not self._remote_update_enabled:
            log.warning("Update command received but remote update is disabled")
            self._report(update_status="disabled")
            return
        req_at = float(command.get("requested_at") or 0)
        # נעילה: לא לטפל בשתי פקודות במקביל.
        if not self._update_lock.acquire(blocking=False):
            return
        try:
            # סימון idempotency *לפני* הביצוע — לא להיכנס ללולאת-retry על כשל.
            self._last_requested_at = req_at
            self._do_update(req_at)
        finally:
            self._update_lock.release()

    def _do_update(self, req_at: float) -> None:
        log.info("Fleet update requested (requested_at=%s) — git pull origin %s",
                 req_at, self._update_branch)
        self._report(update_status="updating", last_applied_requested_at=req_at)

        before = local_version(self._repo_dir)
        ok, out = _git(
            ["pull", "--ff-only", "origin", self._update_branch],
            cwd=self._repo_dir,
            timeout=180,
        )
        after = local_version(self._repo_dir)
        tail = out[-300:] if out else ""

        if not ok:
            log.error("git pull failed: %s", tail)
            self._report(update_status="failed", update_error=tail,
                         last_applied_requested_at=req_at, version=after)
            return

        if before == after:
            log.info("Already up to date (%s) — no restart needed", after)
            self._report(update_status="up_to_date",
                         last_applied_requested_at=req_at, version=after)
            return

        log.info("Updated %s -> %s — restarting service", before, after)
        self._report(update_status="restarting",
                     last_applied_requested_at=req_at, version=after)
        # יציאה מבוקרת; systemd (Restart=always) יפעיל מחדש עם הקוד החדש,
        # והתהליך החדש ידווח version=after עם status=online.
        self._restart()

    # ── מחזור חיים ────────────────────────────────────────────────────────────

    def start(self) -> None:
        # seed ל-idempotency מתוך מה שכבר נכתב ל-/fleet/{id} (לא להריץ שוב פקודה ישנה).
        try:
            existing = self._fb.get_fleet_status() or {}
            self._last_requested_at = float(existing.get("last_applied_requested_at") or 0)
        except Exception as e:  # noqa: BLE001
            log.debug("Could not seed fleet idempotency: %s", e)

        threading.Thread(target=self._report_loop, daemon=True, name="fleet-report").start()
        if self._remote_update_enabled:
            self._fb.subscribe_fleet_command(self._on_command)
            log.info("Fleet agent started (report=%.0fs, update branch=%s)",
                     self._report_interval, self._update_branch)
        else:
            log.info("Fleet agent started (report only; remote update disabled)")

    def stop(self) -> None:
        self._running = False
