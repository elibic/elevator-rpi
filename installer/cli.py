"""
installer/cli.py — אשף ההתקנה וההגדרה האינטראקטיבי בטרמינל.

מריץ את אותם צעדי core.Installer כמו הכלי הגרפי, בסדר הנכון, עם פרומפטים
וצבעי ANSI (באותו סגנון כמו monitor.py).
"""
from __future__ import annotations

from . import core

# ─── ANSI colors (כמו monitor.py) ─────────────────────────────────────────────
GREEN, RED, YELLOW, CYAN, DIM, BOLD, RESET = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[2m", "\033[1m", "\033[0m"
)

_LEVEL_COLORS = {
    "step": CYAN + BOLD, "ok": GREEN, "warn": YELLOW, "cmd": DIM,
    "dry": DIM, "error": RED,
}


def _progress(msg: str, level: str = "info") -> None:
    color = _LEVEL_COLORS.get(level, "")
    print(f"{color}{msg}{RESET}" if color else msg, flush=True)


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{BOLD}{prompt}{RESET}{suffix}: ").strip()
    return val or default


def _ask_yes(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    val = input(f"{BOLD}{prompt}{RESET} ({d}): ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes", "כן")


def _collect_tags(inst: core.Installer, existing: dict) -> dict:
    tags = dict(existing or {})
    print(f"\n{CYAN}{BOLD}── מיפוי תגים ──{RESET}")
    print(f"{DIM}סרוק תג ליד הקורא, ואז הזן את שם הקומה. ריק = סיום.{RESET}")
    if tags:
        print(f"{DIM}תגים קיימים: {len(tags)}{RESET}")
    while True:
        if not _ask_yes("לסרוק תג חדש?", default=True):
            break
        print("מחפש תג… (קרב תג לקורא)")
        tag = inst.scan_tag(timeout_s=15)
        if not tag:
            print(f"{YELLOW}לא נקרא תג. נסה שוב.{RESET}")
            continue
        if tag in tags:
            print(f"{DIM}תג {tag} כבר ממופה לקומה '{tags[tag]}'.{RESET}")
        floor = _ask(f"שם הקומה עבור {GREEN}{tag}{RESET}")
        if floor:
            tags[tag] = floor
            print(f"{GREEN}✓ {tag} → קומה '{floor}'{RESET}")
    return tags


def run_cli(dry_run: bool = False, mock_serial: bool = False) -> None:
    env = core.detect_environment()
    inst = core.Installer(env, dry_run=dry_run, progress=_progress)

    print(f"{BOLD}{CYAN}═══════ מתקין מעלית RFID ═══════{RESET}")
    print(f"  משתמש:   {env.user}")
    print(f"  תיקייה:  {env.project_dir}")
    print(f"  Pi:      {'כן' if env.is_pi else 'לא'}   root: {'כן' if env.is_root else 'לא'}")
    print(f"  /dev/ttyUSB0: {'קיים' if env.serial_present else 'לא קיים'}")
    if dry_run:
        print(f"  {YELLOW}מצב DRY-RUN — לא יבוצעו שינויים{RESET}")
    print()

    if not _ask_yes("להתחיל התקנה?", default=True):
        print("בוטל.")
        return

    # שלבים 1–5: תשתית
    inst.install_system_packages()
    inst.install_cp210x_driver()
    inst.setup_serial_permissions()
    inst.setup_python_env()
    inst.setup_directories()

    # שלב 6: הגדרות
    print(f"\n{CYAN}{BOLD}── הגדרות Firebase ──{RESET}")
    cfg = inst.load_config()
    s = cfg.get("settings", {})
    settings = {
        "FIREBASE_URL": _ask("FIREBASE_URL", s.get("FIREBASE_URL", "")),
        "ELEVATOR_ID": _ask("ELEVATOR_ID (שם המעלית)", s.get("ELEVATOR_ID", "")),
        "SECRET_KEY": _ask("SECRET_KEY", s.get("SECRET_KEY", "")),
        "SERIAL_PORT": s.get("SERIAL_PORT", "/dev/ttyUSB0"),
        "BAUDRATE": s.get("BAUDRATE", 115200),
    }
    if mock_serial:
        tags = cfg.get("tags", {})
        print(f"{DIM}(mock-serial: מדלג על מיפוי תגים){RESET}")
    else:
        tags = _collect_tags(inst, cfg.get("tags", {}))
    # התראות עברו לענן (ramada-web/functions) — לא נשאלות יותר כאן.
    # write_config בלי notifications משאיר בלוק קיים ב-config ללא שינוי.
    res = inst.write_config(settings, tags)
    if not res.ok:
        print(f"{RED}שגיאה בכתיבת ההגדרות: {res.detail}{RESET}")
        return

    # שלבים 7–9: שירותים + קיצור + הפעלה
    inst.install_services()
    inst.install_desktop_shortcut()
    inst.start_services()

    print(f"\n{GREEN}{BOLD}✓ ההתקנה הושלמה!{RESET}")
    for st in inst.all_status():
        print(f"   {st['service']}: active={st['active']} enabled={st['enabled']}")

    # ── Raspberry Pi Connect — התחברות חד-פעמית בסוף ──
    print(f"\n{CYAN}{BOLD}── Raspberry Pi Connect ──{RESET}")
    cs = inst.rpi_connect_status()
    if cs.get("signed_in"):
        print(f"{GREEN}כבר מחובר ל-RPi Connect ✓{RESET}")
    elif _ask_yes("להתחבר עכשיו ל-RPi Connect?", default=True):
        inst.rpi_connect_signin_foreground()

    print(f"\n{DIM}ניטור: python monitor.py --watch   |   לוגים: journalctl -u shabbat-detector -f{RESET}")
    print(f"{DIM}כלי גרפי: sudo ./setup.sh --web{RESET}")
