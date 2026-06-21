"""
מסמלץ מעלית שבת A — רמדה ירושלים
Shabbat Elevator A Simulator — Ramada Jerusalem

מחזור: 12 ↓ -3 ↑ 12 (ללא הפסקה)
שולח PATCH ל-Firebase עם floor + direction + timestamp
"""

import time
import requests
import json
from datetime import datetime

# ─────────────────────────────────────────────
# הגדרות מעלית A (מ-Firebase)
# ─────────────────────────────────────────────
TOP_FLOOR    =  12
BOTTOM_FLOOR = -3

# עצירות בירידה — 26 שניות כל אחת (חוץ מ-FLOOR_WAITS)
STOPS_DOWN = {-3, -2, -1, 0, 1, 3, 5, 7, 9, 11, 12}

# עצירות בעלייה — 26 שניות כל אחת (חוץ מ-FLOOR_WAITS)
STOPS_UP   = {-2, -1}

# זמני המתנה מיוחדים לקומה (עוקף STOP_TIME)
FLOOR_WAITS = {
    -1: 95,   # קומת L (לובי) — 95 שניות בירידה ובעלייה
}

STOP_TIME = 26    # שניות לעצירה רגילה
PASS_TIME = 2     # שניות למעבר (1.7 בעיגול)

CONFIG_FILE = 'rfid_config.json'

# ─────────────────────────────────────────────
# גלובלים
# ─────────────────────────────────────────────
FIREBASE_BASE_URL = None
ELEVATOR_ID       = None
SECRET_KEY        = None


# ─────────────────────────────────────────────
# פונקציות עזר
# ─────────────────────────────────────────────

def log(message: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def load_config() -> dict | None:
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"ERROR: '{CONFIG_FILE}' not found. Create it from the template.")
        return None
    except Exception as e:
        print(f"ERROR loading config: {e}")
        return None


def send_floor(floor: int, direction: str) -> bool:
    """שולח PATCH ל-Firebase עם הקומה הנוכחית"""
    if not FIREBASE_BASE_URL or not ELEVATOR_ID:
        return False
    # בדיוק כמו הסקריפט המקורי: {FIREBASE_URL}/{ELEVATOR_ID}.json
    url = f"{FIREBASE_BASE_URL}/elevators/{ELEVATOR_ID}.json"
    payload = {
        "floor":     str(floor),
        "direction": direction,
        "timestamp": int(time.time()),
    }
    if SECRET_KEY:
        payload["secret_key"] = SECRET_KEY
    try:
        r = requests.patch(url, data=json.dumps(payload), timeout=5)
        if r.status_code != 200:
            log(f"  ⚠ server returned {r.status_code}")
        return True
    except Exception as e:
        log(f"  ✗ send error: {e}")
        return False


def wait_at_floor(floor: int, direction: str) -> float:
    """מחשב כמה זמן להמתין בקומה הנוכחית לפי כיוון"""
    if floor in FLOOR_WAITS:
        return FLOOR_WAITS[floor]
    stops = STOPS_DOWN if direction == 'down' else STOPS_UP
    return STOP_TIME if floor in stops else PASS_TIME


# ─────────────────────────────────────────────
# לוגיקת הסיבוב
# ─────────────────────────────────────────────

def visit(floor: int, direction: str) -> None:
    """שולח עדכון קומה, מחשב זמן המתנה ומחכה"""
    wait = wait_at_floor(floor, direction)
    action = "STOP" if wait >= STOP_TIME else ("WAIT(L)" if wait == FLOOR_WAITS.get(floor) else "pass")
    if floor in FLOOR_WAITS:
        action = f"WAIT-L ({wait}s)"
    elif wait >= STOP_TIME:
        action = f"STOP ({wait}s)"
    else:
        action = f"pass ({wait}s)"

    send_floor(floor, direction)
    dir_arrow = "↓" if direction == 'down' else ("↑" if direction == 'up' else "—")
    log(f"  {dir_arrow} floor {str(floor).rjust(3)}  {action}")
    time.sleep(wait)


def run_cycle() -> None:
    """מחזור מלא: 12 → -3 → 12"""

    # ── עמידה בקומה 12 (ראש המחזור) ─────────────
    send_floor(TOP_FLOOR, 'stopped')
    log(f"  — floor {TOP_FLOOR:3}  TOP (stopped, {STOP_TIME}s)")
    time.sleep(STOP_TIME)

    # ── ירידה: 11 → -3 ────────────────────────────
    log("↓↓  DESCENDING  ↓↓")
    for floor in range(TOP_FLOOR - 1, BOTTOM_FLOOR - 1, -1):
        visit(floor, 'down')

    # ── עמידה בקומה -3 (תחתית) ────────────────────
    send_floor(BOTTOM_FLOOR, 'stopped')
    log(f"  — floor {BOTTOM_FLOOR:3}  BOTTOM (stopped, {STOP_TIME}s)")
    time.sleep(STOP_TIME)

    # ── עלייה: -2 → 12 ────────────────────────────
    log("↑↑  ASCENDING   ↑↑")
    for floor in range(BOTTOM_FLOOR + 1, TOP_FLOOR + 1):
        visit(floor, 'up')


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────

def main() -> None:
    global FIREBASE_BASE_URL, ELEVATOR_ID, SECRET_KEY

    cfg = load_config()
    if not cfg:
        return

    s = cfg.get('settings', {})
    # תומך הן ב-FIREBASE_BASE_URL (חדש) והן ב-FIREBASE_URL (פורמט קיים)
    raw_url = s.get('FIREBASE_BASE_URL') or s.get('FIREBASE_URL', '')
    # הסרת .json בסוף אם קיים (כמו בסקריפט המקורי)
    if raw_url.endswith('.json'):
        raw_url = raw_url[:-5]
    FIREBASE_BASE_URL = raw_url.rstrip('/')

    ELEVATOR_ID = s.get('ELEVATOR_ID')
    SECRET_KEY  = s.get('SECRET_KEY')

    if not FIREBASE_BASE_URL or not ELEVATOR_ID:
        print("ERROR: Missing FIREBASE_BASE_URL / FIREBASE_URL or ELEVATOR_ID in config.")
        print(f"  Config keys found: {list(s.keys())}")
        return

    log("━" * 50)
    log(f"Shabbat Elevator Simulator — Ramada Jerusalem")
    log(f"Elevator: {ELEVATOR_ID} | Floor {BOTTOM_FLOOR} ↔ {TOP_FLOOR}")
    log(f"Firebase: {FIREBASE_BASE_URL}/{ELEVATOR_ID}.json")
    log(f"Cycle:  ~{(STOP_TIME + sum(wait_at_floor(f,'down') for f in range(TOP_FLOOR-1,BOTTOM_FLOOR-1,-1)) + STOP_TIME + sum(wait_at_floor(f,'up') for f in range(BOTTOM_FLOOR+1,TOP_FLOOR+1)))//60}m per full cycle")
    log("Press Ctrl+C to stop.")
    log("━" * 50)

    cycle = 0
    try:
        while True:
            cycle += 1
            log(f"\n══  CYCLE #{cycle}  ══")
            run_cycle()
    except KeyboardInterrupt:
        log("\nSimulator stopped.")
        send_floor(TOP_FLOOR, 'stopped')


if __name__ == '__main__':
    main()
