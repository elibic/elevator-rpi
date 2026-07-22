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
SERVICE_WEB = "elevator-config-web"
SERVICE_FLEET = "fleet-agent"

STATE_DIR = "/var/lib/shabbat_detector"
APT_PACKAGES = ["python3-venv", "python3-pip", "git"]
PIP_PACKAGES = ["requests", "sseclient-py", "pyserial", "flask"]

# ── הקטנת שחיקת SD (settings.REDUCE_SD_WEAR, ברירת מחדל כבוי) ────────────────
# ה-marker מתעד בדיוק מה שינינו, כדי ש-false יחזיר רק את מה שאנחנו עשינו
# (ולא ידרוס שינויים ידניים של המפעיל). קבצי ה-state של הזיהוי חייבים להישאר
# על הדיסק - ראו configure_sd_wear.
SDWEAR_MARKER = "/etc/elevator-reduce-sd-wear.json"
JOURNALD_DROPIN = "/etc/systemd/journald.conf.d/60-elevator-volatile.conf"
AZLUX_LIST = "/etc/apt/sources.list.d/azlux.list"
AZLUX_KEYRING = "/usr/share/keyrings/azlux-archive-keyring.gpg"

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


def _real_user() -> Optional[str]:
    """המשתמש האמיתי של ה-Pi (לעולם לא root): UID 1000 (המשתמש הראשון ב-RPi OS),
    אחרת הבעלים הלא-root הראשון של תיקיית בית תחת /home. משמש כשההרצה היא כ-root
    בלי SUDO_USER אמיתי (עדכון-צי) - אחרת השירותים היו נכתבים User=root."""
    try:
        u = pwd.getpwuid(1000)
        if u.pw_name != "root":
            return u.pw_name
    except (KeyError, OSError):
        pass
    try:
        for name in sorted(os.listdir("/home")):
            try:
                st = os.stat(os.path.join("/home", name))
            except OSError:
                continue
            if st.st_uid != 0:
                return pwd.getpwuid(st.st_uid).pw_name
    except OSError:
        pass
    return None


def detect_environment(project_dir: Optional[str] = None) -> Environment:
    """מזהה משתמש-יעד, נתיבים, האם Pi אמיתי, האם הפורט קיים, ומצב git."""
    # המשתמש האמיתי כשרצים תחת sudo; אחרת המשתמש הנוכחי.
    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
    # השירותים חייבים לרוץ תחת משתמש שולחן-העבודה - לעולם לא root. עדכון-צי רץ כ-root,
    # ו-fleet_agent מעביר SUDO_USER=בעלים-של-הריפו; אם הריפו כבר root-owned זה מחזיר
    # 'root' וכל היחידות היו נכתבות User=root + chown ל-root (רגרסיה + לולאה שנועלת
    # את הריפו על root לתמיד). כשיצא root - מזהים את המשתמש האמיתי של ה-Pi.
    if user == "root":
        user = _real_user() or user
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
            return {"tags": {}, "settings": {}}

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

    def write_config(self, settings: dict, tags: dict) -> StepResult:
        """בונה ושומר את rfid_config.json. מאמת JSON ומגבה קודם."""
        self.emit("כותב את rfid_config.json…", "step")
        cfg = self.load_config()
        cfg.pop("_comment", None)
        cfg.setdefault("settings", {})
        cfg.setdefault("tags", {})
        cfg["settings"].update({k: v for k, v in settings.items() if v is not None and v != ""})
        if tags is not None:
            cfg["tags"] = tags

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
        # פורט הסיריאל נדרש בתבנית ה-tracker (לולאת-המתנה לפני פתיחתו) - קוראים מהקונפיג.
        serial_port = self.load_config().get("settings", {}).get("SERIAL_PORT", "/dev/ttyUSB0")
        mapping = {**self._tmpl_mapping(), "SERIAL_PORT": serial_port}
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

    def install_web_service(self) -> StepResult:
        """שירות systemd שמריץ את הכלי הגרפי תמיד על 127.0.0.1:8080 (root, localhost).
        כך הקיצור בשולחן העבודה רק *פותח דפדפן* — בלי sudo ובלי טרמינל."""
        self.emit("מתקין שירות לכלי הגרפי (web, localhost:8080)…", "step")
        if not self._require_root():
            return StepResult("web_service", False, "no root")
        content = self._render_template("elevator-config-web.service.in", self._tmpl_mapping())
        self._write_file(f"/etc/systemd/system/{SERVICE_WEB}.service", content, mode=0o644)
        self._run(["systemctl", "daemon-reload"])
        self._run(["systemctl", "enable", f"{SERVICE_WEB}.service"], check=False)
        # start (ולא restart) — לא להרוג מופע web שאולי מריץ את ההתקנה הזו עצמה.
        self._run(["systemctl", "start", f"{SERVICE_WEB}.service"], check=False)
        return StepResult("web_service", True)

    def install_fleet_agent(self) -> StepResult:
        """שירות סוכן-הצי: דיווח גרסה ל-/fleet/{id} + ביצוע פקודת-עדכון מרחוק
        (מאומתת ב-secret_key). רץ כ-root כי הפקודה מריצה את setup.sh.

        כשההתקנה הזו הופעלה *ע"י* הסוכן עצמו (FLEET_AGENT_UPDATE=1) — מפעילים
        ב-`start` בלבד כדי לא להרוג את התהליך שמריץ אותנו; הסוכן מבצע restart-
        עצמי בסיום העדכון. בהרצה אינטראקטיבית — `restart`, לרענון קוד הסוכן.
        ראו docs/fleet-remote-update.md."""
        self.emit("מתקין שירות סוכן-צי (דיווח גרסה + עדכון מרחוק)…", "step")
        if not self._require_root():
            return StepResult("fleet_agent", False, "no root")
        content = self._render_template("fleet-agent.service.in", self._tmpl_mapping())
        self._write_file(f"/etc/systemd/system/{SERVICE_FLEET}.service", content, mode=0o644)
        self._run(["systemctl", "daemon-reload"])
        self._run(["systemctl", "enable", f"{SERVICE_FLEET}.service"], check=False)
        action = "start" if os.environ.get("FLEET_AGENT_UPDATE") == "1" else "restart"
        self._run(["systemctl", action, f"{SERVICE_FLEET}.service"], check=False)
        return StepResult("fleet_agent", True)

    def _set_pcmanfm_quick_exec(self) -> None:
        """מבטל את חלונית "Execute File" של מנהל-הקבצים (PCManFM) - כך שלחיצה על
        הקיצור פותחת ישירות. כותב quick_exec=1 ל**כל** פרופילי pcmanfm של המשתמש -
        הקיימים וגם ברירות-המחדל (LXDE-pi ב-X11/LXDE, default בגרסאות אחרות) - כדי
        שההגדרה תחול בכל גרסת OS אחרי login/ריבוט, ולא רק על הפרופיל שכבר קיים.
        (ההגדרה נכנסת לתוקף כשמנהל-הקבצים טוען מחדש את הקונפיג, כלומר בהתחברות הבאה.)"""
        import glob
        import configparser
        base = os.path.join(self.env.home, ".config", "pcmanfm")
        confs = set(glob.glob(os.path.join(base, "*", "pcmanfm.conf")))
        confs.add(os.path.join(base, "LXDE-pi", "pcmanfm.conf"))   # ברירת המחדל ב-RPi OS (X11/LXDE)
        confs.add(os.path.join(base, "default", "pcmanfm.conf"))   # פרופיל default (גרסאות אחרות)
        for conf in sorted(confs):
            try:
                if self.dry_run:
                    self.emit(f"DRY-RUN set quick_exec=1 in {conf}", "dry")
                    continue
                os.makedirs(os.path.dirname(conf), exist_ok=True)
                cp = configparser.ConfigParser()
                cp.optionxform = str   # שמירת רישיות מפתחות
                if os.path.exists(conf):
                    cp.read(conf, encoding="utf-8")
                if not cp.has_section("config"):
                    cp.add_section("config")
                cp.set("config", "quick_exec", "1")
                with open(conf, "w", encoding="utf-8") as f:
                    cp.write(f, space_around_delimiters=False)
                self._chown(conf, self.env.user)
                self._chown(os.path.dirname(conf), self.env.user)
            except Exception as e:
                self.emit(f"אזהרה: הגדרת quick_exec ב-{conf} נכשלה: {e}", "warn")

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
        self._set_pcmanfm_quick_exec()   # בלי חלונית "Execute File" בלחיצה
        self.install_autostart()         # פתיחת הדשבורד אוטומטית בהתחברות/בוט
        return StepResult("desktop_shortcut", True, f"{len(wrote)} files")

    def _autostart_enabled(self) -> bool:
        """ברירת-מחדל: כן. אפשר לכבות עם settings.DASHBOARD_AUTOSTART=false."""
        try:
            val = self.load_config().get("settings", {}).get("DASHBOARD_AUTOSTART", True)
        except Exception:
            return True
        return str(val).strip().lower() not in ("0", "false", "no", "off", "")

    def install_autostart(self) -> None:
        """מתקין רשומת autostart (XDG) שפותחת את הדשבורד אוטומטית בהתחברות.

        Raspberry Pi OS מריץ רשומות ``~/.config/autostart/*.desktop`` גם ב-LXDE
        (Bullseye) וגם ב-labwc/wayfire (Bookworm), אז זו הדרך הניידת בין הגרסאות.
        רשומת autostart רצה ישירות דרך מנגנון-הסשן ולא עוברת דרך חלונית
        "Execute File" של מנהל-הקבצים. כיבוי: settings.DASHBOARD_AUTOSTART=false."""
        target = os.path.join(self.env.home, ".config", "autostart",
                              "elevator-dashboard.desktop")
        if not self._autostart_enabled():
            # כיבוי מפורש: הסר רשומה קיימת כדי שהשינוי ייכנס לתוקף בעדכון.
            if not self.dry_run and os.path.exists(target):
                try:
                    os.remove(target)
                    self.emit("autostart של הדשבורד כובה (DASHBOARD_AUTOSTART=false)", "info")
                except OSError as e:
                    self.emit(f"אזהרה: הסרת autostart נכשלה: {e}", "warn")
            return
        content = self._render_template("elevator-dashboard-autostart.desktop.in",
                                        self._tmpl_mapping())
        self._write_file(target, content, mode=0o644, owner=self.env.user)
        self._chown(os.path.dirname(target), self.env.user)
        self.emit("הדשבורד יעלה אוטומטית בהתחברות (autostart)", "ok")

    # ── 8b. הקטנת שחיקת כרטיס ה-SD (opt-in) ──────────────────────────────────
    def _sd_wear_enabled(self) -> bool:
        """settings.REDUCE_SD_WEAR - ברירת מחדל: כבוי (opt-in, אפס שינוי בפריסות קיימות)."""
        try:
            val = self.load_config().get("settings", {}).get("REDUCE_SD_WEAR", False)
        except Exception:
            return False
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    def _load_sdwear_marker(self) -> dict:
        try:
            with open(SDWEAR_MARKER, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _root_fstab_has_noatime(self, fstab: str = "/etc/fstab") -> Optional[bool]:
        """True/False לשורת ה-root ב-fstab; None אם לא נמצאה שורה."""
        try:
            with open(fstab, "r", encoding="utf-8") as f:
                for line in f:
                    fields = line.split("#", 1)[0].split()
                    if len(fields) >= 4 and fields[1] == "/":
                        return "noatime" in fields[3].split(",")
        except OSError:
            pass
        return None

    def _add_root_noatime(self, fstab: str = "/etc/fstab") -> bool:
        """מוסיף noatime לאופציות שורת ה-root ב-fstab (עריכה נקודתית, שומר הכל)."""
        try:
            with open(fstab, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for i, line in enumerate(lines):
                fields = line.split("#", 1)[0].split()
                if len(fields) >= 4 and fields[1] == "/" and "noatime" not in fields[3].split(","):
                    lines[i] = line.replace(fields[3], fields[3] + ",noatime", 1)
                    with open(fstab, "w", encoding="utf-8") as f:
                        f.writelines(lines)
                    return True
        except OSError as e:
            self.emit(f"אזהרה: עריכת fstab נכשלה: {e}", "warn")
        return False

    def _unit_exists(self, unit: str) -> bool:
        try:
            out = subprocess.run(["systemctl", "list-unit-files", unit],
                                 capture_output=True, text=True, timeout=10).stdout
            return unit in out
        except Exception:
            return False

    def configure_sd_wear(self) -> StepResult:
        """מפעיל/מכבה את חבילת הקטנת-הכתיבות לפי settings.REDUCE_SD_WEAR.

        מופעל (true): לוגי מערכת ב-RAM במקום על הכרטיס -
          1. journald -> Storage=volatile (drop-in; הלוג חי ב-/run, נמחק בריבוט).
          2. log2ram (ריפו azlux) - tmpfs על /var/log עם סנכרון תקופתי לדיסק.
          3. noatime לשורת ה-root ב-fstab (ברירת המחדל של RPi OS - מוודאים שקיים).
          4. כיבוי swap על הכרטיס (dphys-swapfile); zram-swap (RAM) לא נוגעים בו.

        מה ש-בכוונה נשאר על הדיסק (חובה שישרוד ריבוט/נתק-חשמל):
          - /var/lib/shabbat_detector - state של ה-FSM, חלונות ה-schedule
            (fail-closed) ו-state של סוכן-הצי (הגנת-replay). לא תחת /var/log!
          - logs/ בתיקיית הפרויקט - לוגי tracker/detector (רוטציה שבועית, מגובים
            ל-GitHub); הם רשומת-הדיבוג היחידה כש-journald הפך volatile.
        כיבוי (false/חסר): מחזיר רק את מה שאנחנו שינינו, לפי ה-marker.
        """
        enabled = self._sd_wear_enabled()
        marker = self._load_sdwear_marker()
        if not enabled and not marker:
            return StepResult("sd_wear", True, "כבוי (ברירת מחדל) - לא שונה דבר")
        if not self._require_root():
            return StepResult("sd_wear", False, "no root")

        if enabled:
            return self._sd_wear_enable(marker)
        return self._sd_wear_disable(marker)

    def _sd_wear_enable(self, marker: dict) -> StepResult:
        self.emit("מפעיל הקטנת שחיקת SD (REDUCE_SD_WEAR)…", "step")
        problems: list[str] = []

        # 1. journald -> RAM. שירותי systemd כותבים stdout ל-journal כל היום;
        # במצב volatile זה נשאר ב-/run (RAM) ואובד בריבוט - לוגי הקבצים של
        # האפליקציה (logs/) נשארים על הדיסק והם רשומת-הדיבוג לאורך זמן.
        self._write_file(JOURNALD_DROPIN, (
            "# נוצר ע\"י installer של מערכת המעלית (settings.REDUCE_SD_WEAR).\n"
            "# יומן המערכת ב-RAM במקום על כרטיס ה-SD; נמחק בריבוט.\n"
            "# journalctl -u shabbat-detector -f ממשיך לעבוד כרגיל (על הבוט הנוכחי).\n"
            "[Journal]\n"
            "Storage=volatile\n"
            "RuntimeMaxUse=32M\n"), mode=0o644)
        if not self.dry_run:
            if os.path.isdir("/var/log/journal"):
                # היומן הפרסיסטנטי הישן - לא ייכתב יותר; מסירים כדי לפנות מקום.
                self._run(["rm", "-rf", "/var/log/journal"], check=False)
                marker["journal_dir_removed"] = True
            self._run(["systemctl", "restart", "systemd-journald"], check=False)
        marker["journald_dropin"] = True

        # 2. log2ram - tmpfs על /var/log, סנכרון יומי + בעצירה מסודרת. מכסה את מה
        # ש-volatile לא תופס (dpkg.log וכו'). ברירת המחדל של PATH_DISK היא /var/log
        # בלבד - ולכן /var/lib (ה-state) לא מושפע.
        if not shutil.which("log2ram") and not os.path.exists("/etc/log2ram.conf"):
            self.emit("מתקין log2ram (ריפו azlux)…", "step")
            try:
                if not os.path.exists(AZLUX_KEYRING):
                    self._run(["wget", "-qO", AZLUX_KEYRING, "https://azlux.fr/repo.gpg"])
                if not os.path.exists(AZLUX_LIST):
                    self._write_file(AZLUX_LIST,
                                     f"deb [signed-by={AZLUX_KEYRING}] "
                                     "http://packages.azlux.fr/debian/ stable main\n", mode=0o644)
                    marker["azlux_repo_added"] = True
                self._run(["apt-get", "update", "-qq"], check=False)
                self._run(["apt-get", "install", "-y", "log2ram"])
                marker["log2ram_installed"] = True
            except (subprocess.CalledProcessError, OSError) as e:
                problems.append("log2ram לא הותקן (אין רשת לריפו azlux?)")
                self.emit(f"אזהרה: התקנת log2ram נכשלה ({e}); שאר הצעדים חלים - "
                          "אפשר להריץ setup שוב כשיש רשת.", "warn")
        if os.path.exists("/etc/log2ram.conf") and not self.dry_run:
            # 64M מספיקים בשפע אחרי ש-journald עבר ל-volatile, ובטוחים גם ל-Pi עם 512MB.
            self._run(["sed", "-i", "s|^SIZE=.*|SIZE=64M|", "/etc/log2ram.conf"], check=False)
            self._run(["systemctl", "enable", "log2ram"], check=False)
            # בדיקת בטיחות: ש-PATH_DISK לא יכסה בטעות את תיקיות ה-state.
            try:
                with open("/etc/log2ram.conf", "r", encoding="utf-8") as f:
                    conf = f.read()
                for line in conf.splitlines():
                    if line.startswith("PATH_DISK=") and "/var/lib" in line:
                        problems.append(f"PATH_DISK של log2ram מכסה /var/lib - ה-state יעבור ל-RAM! ({line})")
                        self.emit("אזהרה: PATH_DISK של log2ram חייב להישאר /var/log בלבד!", "warn")
            except OSError:
                pass

        # 3. noatime על שורת ה-root (ברירת המחדל של RPi OS; מוודאים ומוסיפים אם חסר).
        has = self._root_fstab_has_noatime()
        if has is False and not self.dry_run:
            if self._add_root_noatime():
                marker["fstab_noatime_added"] = True
                self.emit("נוסף noatime לשורת ה-root ב-fstab (ייכנס לתוקף בריבוט)", "ok")
        elif has:
            self.emit("noatime כבר מוגדר ב-fstab ✓", "ok")

        # 4. swap על הכרטיס (dphys-swapfile) - כיבוי. zram (swap ב-RAM דחוס, אם
        # קיים) לא שוחק את הכרטיס ולכן לא נוגעים בו.
        if self._unit_exists("dphys-swapfile.service"):
            st = self.service_status("dphys-swapfile")
            if st["enabled"] in ("enabled", "static") or st["active"] == "active":
                self._run(["dphys-swapfile", "swapoff"], check=False)
                self._run(["systemctl", "disable", "--now", "dphys-swapfile"], check=False)
                if not self.dry_run and os.path.exists("/var/swap"):
                    self._run(["rm", "-f", "/var/swap"], check=False)
                marker["dphys_swap_disabled"] = True
                self.emit("swap על ה-SD (dphys-swapfile) כובה; שים לב: בלי swap, עומס "
                          "זיכרון חריג (למשל דפדפן כבד) עלול להיסגר ע\"י ה-OOM killer", "ok")
        else:
            self.emit("אין dphys-swapfile (כנראה zram-swap ב-RAM) - אין swap על הכרטיס, מדלג", "info")

        # תיעוד מפורש: ה-state שחייב לשרוד נשאר על הדיסק - לא הועבר ל-RAM.
        for p in (STATE_DIR, os.path.join(self.env.project_dir, "logs")):
            self.emit(f"נשאר על הדיסק (שורד ריבוט/נתק-חשמל): {p}", "info")

        if not self.dry_run:
            self._write_file(SDWEAR_MARKER, json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
                             mode=0o644)
        detail = "journald=volatile, log2ram, noatime, swap-off; ייכנס לתוקף מלא בריבוט"
        if problems:
            detail += " | בעיות: " + "; ".join(problems)
        self.emit("הקטנת שחיקת SD הופעלה - " + detail, "ok")
        return StepResult("sd_wear", not problems, detail)

    def _sd_wear_disable(self, marker: dict) -> StepResult:
        """כיבוי מפורש (false/הסרת המפתח) אחרי שהופעל בעבר - מחזיר את מה ששינינו."""
        self.emit("מכבה הקטנת שחיקת SD (REDUCE_SD_WEAR כבוי אך היה פעיל)…", "step")
        if self.dry_run:
            return StepResult("sd_wear", True, "DRY-RUN: היה מוחזר לפי ה-marker")
        if marker.get("journald_dropin") and os.path.exists(JOURNALD_DROPIN):
            os.remove(JOURNALD_DROPIN)
        if marker.get("journal_dir_removed"):
            os.makedirs("/var/log/journal", exist_ok=True)   # Storage=auto יחזור לפרסיסטנטי
        self._run(["systemctl", "restart", "systemd-journald"], check=False)
        if marker.get("log2ram_installed"):   # רק אם אנחנו התקנו - לא נוגעים בהתקנה ידנית של המפעיל
            self._run(["systemctl", "disable", "log2ram"], check=False)
            self.emit("log2ram נוטרל (החבילה נשארת מותקנת; /var/log חוזר לדיסק בריבוט)", "info")
        if marker.get("azlux_repo_added") and os.path.exists(AZLUX_LIST):
            os.remove(AZLUX_LIST)
        if marker.get("dphys_swap_disabled") and self._unit_exists("dphys-swapfile.service"):
            self._run(["systemctl", "enable", "--now", "dphys-swapfile"], check=False)
        # noatime נשאר בכוונה - זו ברירת המחדל של RPi OS ואין סיבה להחזיר atime.
        try:
            os.remove(SDWEAR_MARKER)
        except OSError:
            pass
        self.emit("הוחזר; ייכנס לתוקף מלא בריבוט", "ok")
        return StepResult("sd_wear", True, "בוטל לפי ה-marker")

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
        if service not in (SERVICE_TRACKER, SERVICE_DETECTOR, SERVICE_CP210X, SERVICE_FLEET):
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
        return [self.service_status(s)
                for s in (SERVICE_CP210X, SERVICE_TRACKER, SERVICE_DETECTOR, SERVICE_FLEET)]

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
                    rpi_connect: bool = True, rpi_connect_lite: bool = False) -> list[StepResult]:
        results = [
            self.install_system_packages(),
            self.install_cp210x_driver(),
            self.setup_serial_permissions(),
            self.setup_python_env(),
            self.setup_directories(),
            self.write_config(settings, tags),
            self.install_services(),
            self.install_web_service(),
            self.install_fleet_agent(),
            self.install_desktop_shortcut(),
            self.configure_sd_wear(),
        ]
        if rpi_connect:
            results.append(self.install_rpi_connect(lite=rpi_connect_lite))
        results.append(self.start_services())
        return results
