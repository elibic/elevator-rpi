"""
monitor.py — ניטור בזמן אמת של ה-detector
─────────────────────────────────────────────
מציג כל ~3 שניות:
  - מצב FSM הנוכחי
  - הסיבה למעבר האחרון
  - סיכום המחזור האחרון (האם תאם, אילו עצירות חסרות/חוקיות, חריגות טיימינג)
  - חריגות אחרונות
  - הצעת auto-learn (אם יש)

שימוש:
  python monitor.py
  python monitor.py --watch       # לולאה אינסופית עם רענון
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import requests

# עוקב אחרי מתי הקומה המספרית האחרונה השתנתה באמת
# (Firebase מתעדכן כל שנייה בגלל תגיות מתחלפות — אנחנו מציגים זמן אמיתי)
_FLOOR_RE = re.compile(r"^-?\d+$")
_floor_tracker: dict = {"last_numeric_floor": None, "changed_at": None}


def _parse_config(path: str) -> tuple[str, str]:
    with open(path, encoding="utf-8") as f:
        s = json.load(f).get("settings", {})
    url = s.get("FIREBASE_BASE_URL") or s.get("BASE_FIREBASE_URL") or s.get("FIREBASE_URL", "")
    # מנרמלים לשורש ה-DB (scheme://host) — עקבי עם הגלאי, ובלי תלות אם הוזן
    # '/elevators', '/elevators.json' או שורש.
    from urllib.parse import urlsplit
    url = url.rstrip("/")
    _pu = urlsplit(url)
    if _pu.scheme and _pu.netloc:
        url = f"{_pu.scheme}://{_pu.netloc}"
    return url, str(s.get("ELEVATOR_ID", ""))


# ─── ANSI colors ─────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
DIM    = "\033[2m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def _color_state(state: str) -> str:
    if state == "SHABBAT":         return f"{GREEN}{BOLD}{state}{RESET}"
    if state == "CANDIDATE_SHABBAT": return f"{YELLOW}{state}{RESET}"
    if state == "CANDIDATE_EXIT": return f"{YELLOW}{state}{RESET}"
    if state == "NORMAL":         return f"{CYAN}{state}{RESET}"
    return state or "(unknown)"


def fetch_status(base_url: str, elevator_id: str) -> dict:
    out = {}
    try:
        r = requests.get(f"{base_url}/elevator_configs/{elevator_id}.json", timeout=5)
        out["config"] = r.json() or {}
    except Exception as e:
        out["error"] = str(e)
    try:
        r = requests.get(f"{base_url}/elevators/{elevator_id}.json", timeout=5)
        out["elevator"] = r.json() or {}
    except Exception:
        out["elevator"] = {}
    try:
        r = requests.get(f"{base_url}/settings.json", timeout=5)
        out["settings"] = r.json() or {}
    except Exception:
        out["settings"] = {}
    return out


def render(status: dict, elevator_id: str) -> None:
    cfg = status.get("config", {})
    elv = status.get("elevator", {})
    settings = status.get("settings", {})
    sd = cfg.get("SHABBAT_DETECTOR") or {}

    print(f"{BOLD}═══════ Shabbat Detector — מעלית {elevator_id} ═══════{RESET}")
    print(f"   זמן:                 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # --- State ---
    state = sd.get("state", "NORMAL")
    sa = cfg.get("SHABBAT_ACTIVE")
    print(f"   {BOLD}מצב FSM:{RESET}            {_color_state(state)}")
    sa_txt = f"{GREEN}true{RESET}" if sa else f"{DIM}false{RESET}"
    print(f"   SHABBAT_ACTIVE:      {sa_txt}")

    last_reason = sd.get("last_transition_reason", "—")
    print(f"   סיבת המעבר האחרון:   {last_reason}")
    last_ts = sd.get("last_transition_ts")
    if last_ts:
        try:
            ts_s = last_ts / 1000 if last_ts > 1e12 else last_ts
            elapsed = time.time() - ts_s
            print(f"   {DIM}({elapsed:.0f} שניות מהמעבר){RESET}")
        except Exception:
            pass
    print()

    # --- Current floor ---
    # Firebase מתעדכן כל שנייה בגלל תגיות מתחלפות (למשל '-1' ↔ 'מינוסתיים').
    # אנחנו עוקבים מתי הקומה המספרית השתנתה באמת, ומציגים רק קומות מספריות.
    raw_floor = str(elv.get("floor", "") or "")
    if _FLOOR_RE.match(raw_floor):
        if raw_floor != _floor_tracker["last_numeric_floor"]:
            _floor_tracker["last_numeric_floor"] = raw_floor
            _floor_tracker["changed_at"] = time.time()
    cur_floor = _floor_tracker["last_numeric_floor"] or raw_floor or "?"
    changed_at = _floor_tracker["changed_at"]
    if changed_at:
        ago = time.time() - changed_at
        ago_txt = f"{ago:.0f}s ago"
        if ago > 60:
            ago_txt = f"{RED}{ago/60:.1f} דקות ללא תנועה!{RESET}"
    else:
        ago_txt = "?"
    print(f"   {BOLD}קומה נוכחית:{RESET}        {cur_floor}    ({ago_txt})")
    print()

    # --- Configuration ---
    print(f"   {BOLD}הגדרות שבת:{RESET}")
    print(f"     STOPPING_FLOORS_UP:   {cfg.get('STOPPING_FLOORS_UP') or '(ריק)'}")
    print(f"     STOPPING_FLOORS_DOWN: {cfg.get('STOPPING_FLOORS_DOWN') or '(ריק)'}")
    print(f"     TIME_PER_FLOOR:       {cfg.get('TIME_PER_FLOOR')}s")
    print(f"     TIME_PASS_FLOOR:      {cfg.get('TIME_PASS_FLOOR')}s")
    print(f"     FLOOR_WAITS:          {cfg.get('FLOOR_WAITS') or '{}'}")
    print()

    # --- Last cycle ---
    lcs = sd.get("last_cycle_summary")
    if lcs:
        matched = lcs.get("matched")
        match_txt = f"{GREEN}✓ תאם{RESET}" if matched else f"{RED}✗ לא תאם{RESET}"
        print(f"   {BOLD}מחזור אחרון:{RESET}        {match_txt}")
        print(f"     משך:                  {lcs.get('duration_s', '?')}s")
        print(f"     up_stops נצפו:        {lcs.get('up_stops')}")
        print(f"     down_stops נצפו:      {lcs.get('down_stops')}")
        if lcs.get("illegal_up"):
            print(f"     {RED}עצירות לא חוקיות בעלייה: {lcs['illegal_up']}{RESET}")
        if lcs.get("illegal_dn"):
            print(f"     {RED}עצירות לא חוקיות בירידה: {lcs['illegal_dn']}{RESET}")
        if lcs.get("missing_up"):
            print(f"     {YELLOW}עצירות חסרות בעלייה:    {lcs['missing_up']}{RESET}")
        if lcs.get("missing_dn"):
            print(f"     {YELLOW}עצירות חסרות בירידה:    {lcs['missing_dn']}{RESET}")
        timing_x = lcs.get("timing_exceptions", 0)
        if timing_x:
            print(f"     {RED}חריגות טיימינג:        {timing_x}{RESET}")
        print()

    # --- Violations ---
    vw = sd.get("violations_window") or []
    if vw:
        print(f"   {BOLD}חריגות אחרונות ({len(vw)}):{RESET}")
        for v in vw[-5:]:
            print(f"     - קומה {v.get('floor')}: {v.get('reason')}")
        print()

    # --- Auto-learn ---
    ac = cfg.get("AUTO_LEARN_CONFIG", "off")
    print(f"   {BOLD}AUTO_LEARN_CONFIG:{RESET}   {ac}")
    sc = sd.get("suggested_config")
    if sc:
        print(f"   {BOLD}הצעת auto-learn:{RESET}     {sc.get('based_on_cycles')} מחזורים, "
              f"consistency={sc.get('consistency_score')}, cv={sc.get('timing_cv')}")
        print(f"     UP:   {sc.get('STOPPING_FLOORS_UP')}")
        print(f"     DOWN: {sc.get('STOPPING_FLOORS_DOWN')}")
        print(f"     TPF:  {sc.get('TIME_PER_FLOOR')}s")
    print()

    # --- Hebcal gate ---
    hg = settings.get("HEBCAL_GATE_ENABLED", True)
    hg_txt = f"{GREEN}פעיל{RESET}" if hg else f"{DIM}כבוי{RESET}"
    print(f"   Hebcal gate:         {hg_txt}")
    print()

    # --- Detection tunables (settings/SHABBAT_DETECTION) ---
    # תצוגה של ההגדרות הפעילות שמכתיבות מתי ה-FSM נכנס/יוצא ממצב שבת.
    # ערכים שחסרים → ייעשה fallback ל-DEFAULTS של ה-FSM.
    sd_cfg = settings.get("SHABBAT_DETECTION") or {}
    _D = {  # ברירות מחדל — חייב להישאר במסונכרן עם ElevatorFSM.DEFAULTS
        "REQUIRED_MATCHING_CYCLES": 1,
        "TIMING_TOLERANCE_PCT": 20,
        "MAX_TIMING_EXCEPTIONS": 1,
        "ALLOWED_ILLEGAL_STOPS_PER_LEG": 0,
        "ALLOWED_MISSING_STOPS_PER_LEG": 1,
        "STICKINESS_MINUTES": 90,
        "VIOLATIONS_FOR_EXIT": 3,
        "VIOLATION_WINDOW_MINUTES": 20,
        "CANDIDATE_EXIT_TIMEOUT_MIN": 30,
        "INACTIVITY_AT_INVALID_FLOOR_MIN": 10,
        "NO_REPORT_TIMEOUT_MIN": 15,
    }
    def _t(key):
        v = sd_cfg.get(key)
        return v if v is not None else _D[key]

    print(f"   {BOLD}תנאי זיהוי שבת:{RESET}")
    print(f"     {DIM}── כניסה ──{RESET}")
    print(f"     מחזורים תואמים נדרשים:   {_t('REQUIRED_MATCHING_CYCLES')}")
    print(f"     סובלנות טיימינג:         ±{_t('TIMING_TOLERANCE_PCT')}%")
    print(f"     חריגות טיימינג מותרות:   {_t('MAX_TIMING_EXCEPTIONS')}")
    print(f"     עצירות לא חוקיות מותרות: {_t('ALLOWED_ILLEGAL_STOPS_PER_LEG')} לכל כיוון")
    print(f"     עצירות חסרות מותרות:    {_t('ALLOWED_MISSING_STOPS_PER_LEG')} לכל כיוון")
    print(f"     {DIM}── יציאה ──{RESET}")
    print(f"     זמן הדבקה (stickiness):  {_t('STICKINESS_MINUTES')} דקות")
    print(f"     חריגות ליציאה:           {_t('VIOLATIONS_FOR_EXIT')}")
    print(f"     חלון ספירת חריגות:       {_t('VIOLATION_WINDOW_MINUTES')} דקות")
    print(f"     timeout ב-CANDIDATE_EXIT: {_t('CANDIDATE_EXIT_TIMEOUT_MIN')} דקות")
    iaf = _t('INACTIVITY_AT_INVALID_FLOOR_MIN')
    iaf_txt = f"{iaf} דקות" if iaf > 0 else f"{DIM}כבוי{RESET}"
    print(f"     חוסר תנועה בקומה זרה:    {iaf_txt}")
    nor = _t('NO_REPORT_TIMEOUT_MIN')
    nor_txt = f"{nor} דקות" if nor > 0 else f"{DIM}כבוי{RESET}"
    print(f"     timeout ללא דיווח:       {nor_txt}")
    print()

    # --- Diagnostic hints ---
    hints = []
    if changed_at and (time.time() - changed_at) > 600:
        hints.append("המעלית לא דיווחה על אירועים זה 10+ דקות — בדוק שה-RFID tracker רץ")
    if not cfg.get("STOPPING_FLOORS_UP"):
        hints.append("STOPPING_FLOORS_UP ריק — הdetector לא יוכל לזהות שבת")
    if not cfg.get("STOPPING_FLOORS_DOWN"):
        hints.append("STOPPING_FLOORS_DOWN ריק — הdetector לא יוכל לזהות שבת")
    if state == "CANDIDATE_SHABBAT" and lcs and not lcs.get("matched"):
        hints.append(f"מחזור אחרון לא תאם — בדוק את לרוחב 'last_cycle_summary' למעלה")
    if state == "NORMAL" and sa:
        hints.append("בעיה: FSM=NORMAL אבל SHABBAT_ACTIVE=true — אי-עקביות")
    if hints:
        print(f"   {YELLOW}{BOLD}⚠  אבחון:{RESET}")
        for h in hints:
            print(f"     • {h}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor Shabbat Detector status")
    parser.add_argument("--config", default="rfid_config.json")
    parser.add_argument("--watch",  action="store_true", help="לולאה עם רענון כל 3s")
    parser.add_argument("--interval", type=int, default=3)
    args = parser.parse_args()

    base_url, elevator_id = _parse_config(args.config)
    if not base_url or not elevator_id:
        print("חסר FIREBASE_URL / ELEVATOR_ID")
        sys.exit(1)

    try:
        while True:
            if args.watch:
                os.system("clear")
            status = fetch_status(base_url, elevator_id)
            render(status, elevator_id)
            if not args.watch:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nעצירה.")


if __name__ == "__main__":
    main()
