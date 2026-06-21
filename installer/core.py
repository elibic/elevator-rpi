"""
installer/core.py — לב לוגיקת ההתקנה/הגדרה/ניהול של מערכת מעלית ה-RFID.

כל הפעולות idempotent ותומכות ב-dry_run. שני הממשקים (CLI ו-Web) קוראים בדיוק
לאותן פונקציות דרך המחלקה Installer, כך שלא קיימת כפילות לוגיקה.

סדר ההתקנה הנכון (ראו install_all):
  1. חבילות מערכת (apt)
  2. דרייבר CP210x + שירות תיקון
  3. הרשאות serial (dialout)
  4. venv + תלויות פייתון
  5. תיקיות לוגים/מצב
  6. כתיבת ההגדרות (rfid_config.json)
  7. שירותי systemd (tracker + detector)
  8. קיצור דרך לשולחן העבודה
  9. הפעלה
"""
from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

# ── קבועים ────────────────────────────────────────────────────────────────────
SERVICE_TRACKER = "rfid-tracker"
SERVICE_DETECTOR = "shabbat-detector"
SERVICE_CP210X = "fix_cp210x"

STATE_DIR = "/var/lib/shabbat_detector"
APT_PACKAGES = ["python3-venv", "python3-pip", "git"]
PIP_PACKAGES = ["requests", "sseclient-py", "pyserial", "flask"]

# מזהה ה-USB של קורה ה-RFID (CP210x) — נכרך ידנית לדרייבר ה-in-kernel.
CP210X_USB_ID = "1560 0003"

ProgressFn = Callable[[str, str], None]


def _default_progress(msg: str, level: str = "info") -> None:
    print(f"[{level}] {msg}", flush=True)


@dataclass
class Environment:
    user: str
    home: str
    project_dir: str
    venv_dir: str
    python_bin: str
    is_root: bool
    is_pi: bool
    serial_present: bool
    git_branch: Optional[str] = None
    git_remote: Optional[str] = None


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""


def detect_environment(project_dir: Optional[str] = None) -> Environment:
    """מזהה משתמש-יעד, נתיבים, האם Pi אמיתי, האם הפורט קיים, ומצב git."""
    # המשתמש האמיתי כשרצים תחת sudo; אחרת המשתמש הנוכחי.
    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
    try:
        home = pwd.getpwnam(user).pw_dir
    except KeyError:
        home = os.path.expanduser("~")

    if project_dir is None:
        # שורש הריפו = ההורה של חבילת installer.
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    venv_dir = os.path.join(project_dir, "venv")
    python_bin = os.path.join(venv_dir, "bin", "python")

    is_pi = False
    try:
        with open("/proc/device-tree/model", "r", encoding="utf-8", errors="ignore") as f:
            is_pi = "raspberry pi" in f.read().lower()
    except Exception:
        is_pi = False

    branch = remote = None
    try:
        branch = subprocess.run(
            ["git", "-C", project_dir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or None
        remote = subprocess.run(
            ["git", "-C", project_dir, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or None
    except Exception:
        pass

    return Environment(
        user=user,
        home=home,
        project_dir=project_dir,
        venv_dir=venv_dir,
        python_bin=python_bin,
        is_root=(os.geteuid() == 0),
        is_pi=is_pi,
        serial_present=os.path.exists("/dev/ttyUSB0"),
        git_branch=branch,
        git_remote=remote,
    )


class Installer:
    """אוסף פעולות ההתקנה/ההגדרה/הניהול. מצב משותף: env, dry_run, progress."""

    def __init__(self, env: Optional[Environment] = None, dry_run: bool = False,
                 progress: Optional[ProgressFn] = None):
        self.env = env or detect_environment()
        self.dry_run = dry_run
        self._progress = progress or _default_progress

    # ── עזרים ──────────────────────────────────────────────────────────────────
    def emit(self, msg: str, level: str = "info") -> None:
        self._progress(msg, level)

    def _run(self, cmd: list[str], check: bool = True, **kw) -> subprocess.CompletedProcess:
        """מריץ פקודת מערכת (מכבד dry_run)."""
        printable = " ".join(cmd)
        if self.dry_run:
            self.emit(f"DRY-RUN $ {printable}", "dry")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self.emit(f"$ {printable}", "cmd")
        return subprocess.run(cmd, check=check, text=True, capture_output=True, **kw)

    def _write_file(self, path: str, content: str, mode: Optional[int] = None,
                    owner: Optional[str] = None) -> None:
        if self.dry_run:
            self.emit(f"DRY-RUN write {path} ({len(content)} bytes)", "dry")
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        if mode is not None:
            os.chmod(path, mode)
        if owner:
            self._chown(path, owner)
        self.emit(f"כתבתי {path}", "ok")

    def _chown(self, path: str, user: str) -> None:
        if self.dry_run:
            return
        try:
            pw = pwd.getpwnam(user)
            os.chown(path, pw.pw_uid, pw.pw_gid)
        except Exception as e:
            self.emit(f"אזהרה: chown נכשל ל-{path}: {e}", "warn")

    def _require_root(self) -> bool:
        if self.env.is_root or self.dry_run:
            return True
        self.emit("נדרשת הרשאת root (sudo) לפעולה זו — מדלג.", "warn")
        return False

    def _templates_dir(self) -> str:
        return os.path.dirname(os.path.abspath(__file__))

    def _render_template(self, name: str, mapping: dict) -> str:
        """קורא תבנית מתיקיית installer/ או systemd/ ומחליף placeholders {{KEY}}."""
        candidates = [
            os.path.join(self._templates_dir(), name),
            os.path.join(self.env.project_dir, "systemd", name),
        ]
        src = next((p for p in candidates if os.path.exists(p)), None)
        if src is None:
            raise FileNotFoundError(f"תבנית לא נמצאה: {name}")
        with open(src, "r", encoding="utf-8") as f:
            text = f.read()
        for k, v in mapping.items():
            text = text.replace("{{" + k + "}}", str(v))
        return text

    def _tmpl_mapping(self) -> dict:
        return {
            "USER": self.env.user,
            "PROJECT_DIR": self.env.project_dir,
            "VENV": self.env.venv_dir,
            "PYTHON": self.env.python_bin,
        }

    # ── 1. חבילות מערכת ───────────────────────────────────────────────────────
    def install_system_packages(self) -> StepResult:
        self.emit("מתקין חבילות מערכת (apt)…", "step")
        if not self._require_root():
            return StepResult("system_packages", False, "no root")
        try:
            self._run(["apt-get", "update", "-qq"], check=False)
            self._run(["apt-get", "install", "-y", *APT_PACKAGES])
            return StepResult("system_packages", True)
        except subprocess.CalledProcessError as e:
            return StepResult("system_packages", False, e.stderr or str(e))

    # ── 2. דרייבר CP210x ──────────────────────────────────────────────────────
    def install_cp210x_driver(self) -> StepResult:
        """כותב סקריפט תיקון + שירות oneshot שכורך את הקורא לדרייבר cp210x.

        מבוסס בדיוק על המדריך: modprobe + echo של ה-USB ID אל new_id.
        """
        self.emit("מתקין תיקון דרייבר CP210x…", "step")
        if not self._require_root():
            return StepResult("cp210x", False, "no root")

        fix_script = "/usr/local/bin/fix_cp210x.sh"
        src = os.path.join(self._templates_dir(), "fix_cp210x.sh")
        with open(src, "r", encoding="utf-8") as f:
            self._write_file(fix_script, f.read(), mode=0o755)

        service = self._render_template("fix_cp210x.service.in", self._tmpl_mapping())
        self._write_file(f"/etc/systemd/system/{SERVICE_CP210X}.service", service, mode=0o644)

        self._run(["systemctl", "daemon-reload"])
        self._run(["systemctl", "enable", f"{SERVICE_CP210X}.service"], check=False)
        self._run(["systemctl", "start", f"{SERVICE_CP210X}.service"], check=False)

        # נותנים רגע ל-udev/דרייבר ובודקים שהפורט הופיע.
        if not self.dry_run:
            time.sleep(2)
            if os.path.exists("/dev/ttyUSB0"):
                self.emit("/dev/ttyUSB0 קיים ✓", "ok")
            else:
                self.emit("שים לב: /dev/ttyUSB0 עדיין לא קיים — ודא שהקורא מחובר.", "warn")
        return StepResult("cp210x", True)

    # ── 3. הרשאות serial ──────────────────────────────────────────────────────
    def setup_serial_permissions(self) -> StepResult:
        self.emit(f"מוסיף את {self.env.user} לקבוצת dialout…", "step")
        if not self._require_root():
            return StepResult("serial_perms", False, "no root")
        self._run(["usermod", "-aG", "dialout", self.env.user], check=False)
        return StepResult("serial_perms", True)

    # ── 4. סביבת פייתון ───────────────────────────────────────────────────────
    def setup_python_env(self) -> StepResult:
        self.emit("מקים venv ומתקין תלויות פייתון…", "step")
        if not os.path.isdir(self.env.venv_dir) and not self.dry_run:
            self._run(["python3", "-m", "venv", self.env.venv_dir])
        pip = os.path.join(self.env.venv_dir, "bin", "pip")
        self._run([pip, "install", "--quiet", "--upgrade", "pip"], check=False)
        self._run([pip, "install", "--quiet", *PIP_PACKAGES])
        # ה-venv שייך למשתמש כדי שיוכל לנהל אותו בלי sudo.
        if not self.dry_run and self.env.is_root:
            self._run(["chown", "-R", f"{self.env.user}:{self.env.user}", self.env.venv_dir],
                      check=False)
        return StepResult("python_env", True)

    # ── 5. תיקיות ─────────────────────────────────────────────────────────────
    def setup_directories(self) -> StepResult:
        self.emit("מכין תיקיות לוגים ומצב…", "step")
        logs = os.path.join(self.env.project_dir, "logs")
        if not self.dry_run:
            os.makedirs(logs, exist_ok=True)
            self._chown(logs, self.env.user)
        if self._require_root():
            if not self.dry_run:
                os.makedirs(STATE_DIR, exist_ok=True)
            self._chown(STATE_DIR, self.env.user)
        return StepResult("directories", True)

    # ── 6. כתיבת ההגדרות ──────────────────────────────────────────────────────
    def config_path(self) -> str:
        return os.path.join(self.env.project_dir, "rfid_config.json")

    def load_config(self) -> dict:
        """טוען את rfid_config.json הקיים, או את התבנית לדוגמה אם אין."""
        path = self.config_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                self.emit(f"אזהרה: config קיים אך לא תקין ({e}); משתמש בתבנית.", "warn")
        example = os.path.join(self.env.project_dir, "rfid_config.example.json")
        try:
            with open(example, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.pop("_comment", None)
            return data
        except Exception:
            return {"tags": {}, "settings": {}, "notifications": {}}

    def backup_config(self) -> Optional[str]:
        """גיבוי timestamped של config קיים לפני דריסה (בסגנון deploy_elevator.sh)."""
        path = self.config_path()
        if not os.path.exists(path) or self.dry_run:
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{path}.{stamp}.bak"
        try:
            shutil.copy2(path, backup)
            self.emit(f"גיבוי config: {backup}", "ok")
            return backup
        except Exception as e:
            self.emit(f"אזהרה: גיבוי config נכשל: {e}", "warn")
            return None

    def write_config(self, settings: dict, tags: dict,
                     notifications: Optional[dict] = None) -> StepResult:
        """בונה ושומר את rfid_config.json. מאמת JSON ומגבה קודם."""
        self.emit("כותב את rfid_config.json…", "step")
        cfg = self.load_config()
        cfg.pop("_comment", None)
        cfg.setdefault("settings", {})
        cfg.setdefault("tags", {})
        cfg["settings"].update({k: v for k, v in settings.items() if v is not None and v != ""})
        if tags is not None:
            cfg["tags"] = tags
        if notifications is not None:
            cfg["notifications"] = notifications

        # ולידציה: ערכים חיוניים
        s = cfg["settings"]
        missing = [k for k in ("FIREBASE_URL", "ELEVATOR_ID", "SECRET_KEY") if not s.get(k)]
        if missing:
            return StepResult("config", False, f"חסרים שדות חובה: {missing}")

        self.backup_config()
        rendered = json.dumps(cfg, ensure_ascii=False, indent=2)
        # ולידציה אחרונה שה-JSON תקין
        json.loads(rendered)
        self._write_file(self.config_path(), rendered, mode=0o600, owner=self.env.user)
        return StepResult("config", True)

    # ── 7. שירותי systemd ─────────────────────────────────────────────────────
    def install_services(self) -> StepResult:
        self.emit("מתקין שירותי systemd (tracker + detector)…", "step")
        if not self._require_root():
            return StepResult("services", False, "no root")
        mapping = self._tmpl_mapping()
        for svc, tmpl in (
            (SERVICE_TRACKER, "rfid-tracker.service.in"),
            (SERVICE_DETECTOR, "shabbat-detector.service.in"),
        ):
            content = self._render_template(tmpl, mapping)
            self._write_file(f"/etc/systemd/system/{svc}.service", content, mode=0o644)
        self._run(["systemctl", "daemon-reload"])
        for svc in (SERVICE_TRACKER, SERVICE_DETECTOR):
            self._run(["systemctl", "enable", f"{svc}.service"], check=False)
        return StepResult("services", True)

    # ── 8. קיצור דרך לשולחן העבודה ────────────────────────────────────────────
    def install_desktop_shortcut(self) -> StepResult:
        self.emit("יוצר קיצור דרך בשולחן העבודה…", "step")
        content = self._render_template("elevator-config.desktop.in", self._tmpl_mapping())
        desktop = os.path.join(self.env.home, "Desktop")
        apps = os.path.join(self.env.home, ".local", "share", "applications")
        wrote = []
        for d in (desktop, apps):
            if self.dry_run:
                self.emit(f"DRY-RUN write {os.path.join(d, 'elevator-config.desktop')}", "dry")
                continue
            try:
                os.makedirs(d, exist_ok=True)
                target = os.path.join(d, "elevator-config.desktop")
                with open(target, "w", encoding="utf-8") as f:
                    f.write(content)
                os.chmod(target, 0o755)
                self._chown(target, self.env.user)
                self._chown(d, self.env.user)
                wrote.append(target)
            except Exception as e:
                self.emit(f"אזהרה: יצירת קיצור ב-{d} נכשלה: {e}", "warn")
        # סימון הקיצור כ"מהימן" כדי שלא ייחסם ב-Raspberry Pi OS desktop.
        desktop_file = os.path.join(desktop, "elevator-config.desktop")
        if not self.dry_run and os.path.exists(desktop_file) and shutil.which("gio"):
            self._run(["sudo", "-u", self.env.user, "gio", "set", desktop_file,
                       "metadata::trusted", "true"], check=False)
        return StepResult("desktop_shortcut", True, f"{len(wrote)} files")

    # ── 9. ניהול שירותים ──────────────────────────────────────────────────────
    def _systemctl(self, action: str, service: str) -> bool:
        try:
            self._run(["systemctl", action, f"{service}.service"])
            return True
        except subprocess.CalledProcessError as e:
            self.emit(f"systemctl {action} {service} נכשל: {e.stderr or e}", "warn")
            return False

    def start_services(self) -> StepResult:
        ok = all(self._systemctl("restart", s) for s in (SERVICE_TRACKER, SERVICE_DETECTOR))
        return StepResult("start", ok)

    def stop_services(self) -> StepResult:
        ok = all(self._systemctl("stop", s) for s in (SERVICE_TRACKER, SERVICE_DETECTOR))
        return StepResult("stop", ok)

    def restart_services(self) -> StepResult:
        return self.start_services()

    def service_action(self, service: str, action: str) -> bool:
        if service not in (SERVICE_TRACKER, SERVICE_DETECTOR, SERVICE_CP210X):
            raise ValueError(f"שירות לא מוכר: {service}")
        if action not in ("start", "stop", "restart"):
            raise ValueError(f"פעולה לא מוכרת: {action}")
        return self._systemctl(action, service)

    def service_status(self, service: str) -> dict:
        """מחזיר {active, enabled} עבור שירות."""
        def _q(args):
            try:
                return subprocess.run(["systemctl", *args, f"{service}.service"],
                                      capture_output=True, text=True).stdout.strip()
            except Exception:
                return "unknown"
        return {"service": service, "active": _q(["is-active"]), "enabled": _q(["is-enabled"])}

    def all_status(self) -> list[dict]:
        return [self.service_status(s) for s in (SERVICE_CP210X, SERVICE_TRACKER, SERVICE_DETECTOR)]

    # ── עדכון מ-git ───────────────────────────────────────────────────────────
    def update_from_git(self, branch: Optional[str] = None) -> StepResult:
        """git pull (ff-only) ואז restart לשירותים. לא נוגע ב-rfid_config.json."""
        self.emit("מושך עדכונים מ-git…", "step")
        try:
            self._run(["git", "-C", self.env.project_dir, "fetch", "origin"], check=False)
            args = ["git", "-C", self.env.project_dir, "pull", "--ff-only"]
            if branch:
                args += ["origin", branch]
            self._run(args)
        except subprocess.CalledProcessError as e:
            return StepResult("update_git", False, e.stderr or str(e))
        self.restart_services()
        return StepResult("update_git", True)

    # ── סריקת תג (עוצרת זמנית את ה-tracker) ────────────────────────────────────
    def scan_tag(self, timeout_s: float = 15.0) -> Optional[str]:
        """סורק תג בודד. עוצר את ה-tracker (תפיסת הפורט) וסוגר/מחזיר אחריו."""
        from . import rfid_scan
        s = self.load_config().get("settings", {})
        port = s.get("SERIAL_PORT", "/dev/ttyUSB0")
        baud = int(s.get("BAUDRATE", 115200))

        tracker_was_active = self.service_status(SERVICE_TRACKER)["active"] == "active"
        if tracker_was_active:
            self._systemctl("stop", SERVICE_TRACKER)
            time.sleep(0.5)
        try:
            if self.dry_run:
                self.emit("DRY-RUN: מחזיר תג מדומה", "dry")
                return "00E2000000000000DRYRUN01"
            return rfid_scan.scan_for_tag(port, baud, timeout_s=timeout_s)
        finally:
            if tracker_was_active:
                self._systemctl("start", SERVICE_TRACKER)

    # ── Raspberry Pi Connect ──────────────────────────────────────────────────
    def _user_cmd(self, args: list[str]) -> list[str]:
        """עוטף פקודה כך שתרוץ בהקשר ה-user (נדרש ל-rpi-connect שהוא user-service)."""
        try:
            uid = pwd.getpwnam(self.env.user).pw_uid
        except KeyError:
            uid = os.getuid()
        return ["sudo", "-u", self.env.user, "env", f"XDG_RUNTIME_DIR=/run/user/{uid}", *args]

    def install_rpi_connect(self, lite: bool = False) -> StepResult:
        """מתקין ומפעיל Raspberry Pi Connect (ההתחברות עצמה — signin — נפרדת)."""
        self.emit("מתקין ומפעיל Raspberry Pi Connect…", "step")
        if not self._require_root():
            return StepResult("rpi_connect", False, "no root")
        pkg = "rpi-connect-lite" if lite else "rpi-connect"
        try:
            self._run(["apt-get", "install", "-y", pkg])
        except subprocess.CalledProcessError as e:
            return StepResult("rpi_connect", False, e.stderr or str(e))
        # linger כדי שה-user service ירוץ גם ללא התחברות אינטראקטיבית של המשתמש.
        self._run(["loginctl", "enable-linger", self.env.user], check=False)
        # הפעלה עבור המשתמש (מפעיל את rpi-connect.service ב-user systemd).
        self._run(self._user_cmd(["rpi-connect", "on"]), check=False)
        self.emit("rpi-connect מותקן ופעיל — נותרה התחברות חד-פעמית (signin).", "ok")
        return StepResult("rpi_connect", True)

    def rpi_connect_status(self) -> dict:
        """{installed, signed_in, raw} — לתצוגה בדשבורד."""
        if self.dry_run:
            return {"installed": True, "signed_in": False, "raw": "DRY-RUN"}
        if not shutil.which("rpi-connect"):
            return {"installed": False, "signed_in": False, "raw": ""}
        try:
            out = subprocess.run(self._user_cmd(["rpi-connect", "status"]),
                                 capture_output=True, text=True, timeout=10).stdout
        except Exception as e:
            return {"installed": True, "signed_in": False, "raw": str(e)}
        low = out.lower()
        signed = "signed in: yes" in low or ("signed in" in low and "not signed in" not in low)
        return {"installed": True, "signed_in": signed, "raw": out.strip()}

    def rpi_connect_signin_url(self, wait_s: float = 25) -> Optional[str]:
        """מתחיל signin ומחזיר את קישור האימות (התהליך ממשיך לרוץ עד שתאשר בדפדפן)."""
        if self.dry_run:
            return "https://connect.raspberrypi.com/verify/DRYR-UN00"
        if not shutil.which("rpi-connect"):
            return None
        import re
        proc = subprocess.Popen(self._user_cmd(["rpi-connect", "signin"]),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        pat = re.compile(r"https://connect\.raspberrypi\.com/\S+")
        deadline = time.time() + wait_s
        while time.time() < deadline:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
                continue
            m = pat.search(line)
            if m:
                return m.group(0)  # משאירים את התהליך רץ כדי להשלים את האימות
        return None

    def rpi_connect_signin_foreground(self) -> None:
        """הרצת signin בטרמינל (CLI) — מציג את הקישור וממתין לאישור בדפדפן."""
        if self.dry_run:
            self.emit("DRY-RUN: rpi-connect signin", "dry")
            return
        if not shutil.which("rpi-connect"):
            self.emit("rpi-connect לא מותקן — מדלג על ההתחברות.", "warn")
            return
        self.emit("התחברות ל-RPi Connect — פתח את הקישור שיוצג ואשר בחשבון ה-Raspberry Pi:", "step")
        try:
            subprocess.run(self._user_cmd(["rpi-connect", "signin"]))
        except KeyboardInterrupt:
            self.emit("דילגת. אפשר להתחבר אחר כך: rpi-connect signin", "warn")

    # ── התקנה מלאה בסדר הנכון ─────────────────────────────────────────────────
    def install_all(self, settings: dict, tags: dict,
                    notifications: Optional[dict] = None,
                    rpi_connect: bool = True, rpi_connect_lite: bool = False) -> list[StepResult]:
        results = [
            self.install_system_packages(),
            self.install_cp210x_driver(),
            self.setup_serial_permissions(),
            self.setup_python_env(),
            self.setup_directories(),
            self.write_config(settings, tags, notifications),
            self.install_services(),
            self.install_desktop_shortcut(),
        ]
        if rpi_connect:
            results.append(self.install_rpi_connect(lite=rpi_connect_lite))
        results.append(self.start_services())
        return results
