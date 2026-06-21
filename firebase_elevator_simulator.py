"""
firebase_elevator_simulator.py
────────────────────────────────
שולח נתוני קומות ל-Firebase בדיוק כמו ה-RFID tracker האמיתי.
הרץ את detector.py בטרמינל נפרד — הוא יקבל את הנתונים דרך Firebase SSE
ויזהה את דפוס השבת אוטומטית.

שימוש:
    python firebase_elevator_simulator.py                     # בזמן אמת (~11 דקות/מחזור)
    python firebase_elevator_simulator.py --fast              # מהיר ×20 (~35 שניות/מחזור)
    python firebase_elevator_simulator.py --fast --cycles 2   # 2 מחזורים ועצור
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fb-sim")

_running = True


def _signal_handler(sig, frame):
    global _running
    log.info("עוצר...")
    _running = False


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def _parse_config(path: str) -> tuple[str, str, str]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    s = raw.get("settings", raw)
    url = s.get("FIREBASE_BASE_URL") or s.get("BASE_FIREBASE_URL") or s.get("FIREBASE_URL", "")
    url = url.rstrip("/")
    if url.endswith(".json"):
        url = url[: url.rfind("/")]
    return url, str(s.get("ELEVATOR_ID", "")), s.get("SECRET_KEY", "")


def _get_el_config(base_url: str, elevator_id: str) -> dict:
    r = requests.get(f"{base_url}/elevator_configs/{elevator_id}.json", timeout=10)
    r.raise_for_status()
    return r.json() or {}


def _patch_config(base_url: str, elevator_id: str, secret: str, updates: dict) -> None:
    requests.patch(
        f"{base_url}/elevator_configs/{elevator_id}.json",
        json={**updates, "secret_key": secret},
        timeout=10,
    )


def _send_floor(base_url: str, elevator_id: str, secret: str, floor: str) -> None:
    payload = {
        "floor": floor,
        "timestamp": time.time(),
        "secret_key": secret,
    }
    try:
        r = requests.patch(
            f"{base_url}/elevators/{elevator_id}.json",
            json=payload,
            timeout=5,
        )
        if r.status_code != 200:
            log.warning("Firebase error %s: %s", r.status_code, r.text[:60])
    except Exception as e:
        log.warning("שגיאת רשת: %s", e)


def _run_cycle(base_url: str, elevator_id: str, secret: str, el_config: dict, fast: bool, mode: str = "shabbat") -> None:
    top_n  = int(el_config["TOP_FLOOR"])
    bot_n  = int(el_config["BOTTOM_FLOOR"])
    tpf    = float(el_config["TIME_PER_FLOOR"])
    tpass  = float(el_config.get("TIME_PASS_FLOOR", 2))
    fw     = {str(k): float(v) for k, v in (el_config.get("FLOOR_WAITS") or {}).items()}
    dn     = {str(f) for f in (el_config.get("STOPPING_FLOORS_DOWN") or [])}
    up     = {str(f) for f in (el_config.get("STOPPING_FLOORS_UP")   or [])}

    # In "normal" mode: invert stops — stop at floors NOT in shabbat config
    # (these become violations from the detector's POV)
    if mode == "normal":
        all_floors = {str(i) for i in range(bot_n, top_n + 1)}
        terminals = {str(top_n), str(bot_n)}
        dn = (all_floors - dn - terminals)  # stop where shabbat does NOT
        up = (all_floors - up - terminals)

    speed = 0.05 if fast else 1.0

    def dwell(f: str, stops: set) -> float:
        base = fw.get(f, tpf if f in stops else tpass)
        return base * speed

    def send(floor: str, d: float, label: str) -> bool:
        if not _running:
            return False
        log.info("  קומה %4s  %s", floor, label)
        _send_floor(base_url, elevator_id, secret, floor)
        time.sleep(d)
        return True

    # TOP terminal
    if not send(str(top_n), tpf * speed, f"TERMINAL ({tpf*speed:.1f}s)"): return

    # ירידה: TOP-1 → BOTTOM
    for fn in range(top_n - 1, bot_n - 1, -1):
        f = str(fn)
        d = dwell(f, dn)
        tag = "STOP" if d >= tpf * speed * 0.4 else "pass"
        if not send(f, d, f"{tag} ({d:.1f}s)"): return

    # עלייה: BOTTOM+1 → TOP
    for fn in range(bot_n + 1, top_n + 1):
        f = str(fn)
        d = dwell(f, up)
        tag = "STOP" if d >= tpf * speed * 0.4 else "pass"
        if not send(f, d, f"{tag} ({d:.1f}s)"): return


def main() -> None:
    parser = argparse.ArgumentParser(description="Firebase Elevator Simulator")
    parser.add_argument("--config", default="rfid_config.json")
    parser.add_argument("--fast", action="store_true", help="×20 מהיר יותר (~35 שניות/מחזור)")
    parser.add_argument("--cycles", type=int, default=0, help="מספר מחזורים (0 = אין הגבלה)")
    parser.add_argument("--mode", choices=["shabbat", "normal"], default="shabbat",
                        help="shabbat=דפוס שבת | normal=דפוס יום חול (יוצר חריגות)")
    parser.add_argument("--restore", action="store_true",
                        help="שחזר TIME_PER_FLOOR המקורי בסיום (כברירת מחדל: לא — שומר על מצב fast)")
    args = parser.parse_args()

    base_url, elevator_id, secret = _parse_config(args.config)
    if not base_url or not elevator_id:
        log.error("חסר FIREBASE_URL / ELEVATOR_ID בקונפיג")
        sys.exit(1)

    log.info("━" * 55)
    log.info("  Firebase Elevator Simulator  |  מעלית %s", elevator_id)
    log.info("  שולח נתונים ל: %s/elevators/%s", base_url, elevator_id)

    # קריאת הגדרות מעלית מ-Firebase
    el_config = _get_el_config(base_url, elevator_id)
    if not el_config:
        log.error("לא ניתן לקרוא elevator_config עבור %s", elevator_id)
        sys.exit(1)

    tpf = float(el_config["TIME_PER_FLOOR"])
    top_n = int(el_config["TOP_FLOOR"])
    bot_n = int(el_config["BOTTOM_FLOOR"])

    log.info("  %s ↔ %s  |  TIME_PER_FLOOR=%.0fs  |  fast=%s",
             bot_n, top_n, tpf, args.fast)

    orig_tpf   = tpf
    orig_tpass = float(el_config.get("TIME_PASS_FLOOR", 2))
    orig_fw    = {str(k): float(v) for k, v in (el_config.get("FLOOR_WAITS") or {}).items()}

    # במצב fast — עדכן את הגדרות הזמן ב-Firebase כדי שה-detector יסווג נכון.
    # חשוב: el_config נשאר עם הערכים המקוריים — _run_cycle מכפיל ב-speed בעצמו.
    if args.fast:
        # הגנה מפני כפל-scaling אם Firebase כבר במצב fast
        if tpf < 5:
            log.info("  [FAST] Firebase כבר במצב fast (TPF=%.2fs) — לא משנה.", tpf)
            # שחזר el_config לערכים מקוריים סבירים לחישוב dwell בסימולטור
            el_config = {**el_config, "TIME_PER_FLOOR": 26, "TIME_PASS_FLOOR": 1.7,
                         "FLOOR_WAITS": {k: 95.0 if str(k) == "-1" else v for k, v in orig_fw.items()}}
            tpf = 26.0
        else:
            log.info("  [FAST] מעדכן TIME_PER_FLOOR ל-%.2fs ב-Firebase...", tpf * 0.05)
            scaled = {
                "TIME_PER_FLOOR":  tpf * 0.05,
                "TIME_PASS_FLOOR": orig_tpass * 0.05,
                "FLOOR_WAITS":     {k: v * 0.05 for k, v in orig_fw.items()},
            }
            _patch_config(base_url, elevator_id, secret, scaled)
            log.info("  ✓ הגדרות Firebase עודכנו (השאר ב-fast עד --restore)")

    log.info("━" * 55)
    log.info("  הרץ עכשיו בטרמינל נפרד:")
    log.info("  python -m shabbat_detector.detector --config rfid_config.json")
    log.info("━" * 55)

    cycle_num = 0
    try:
        while _running and (args.cycles == 0 or cycle_num < args.cycles):
            cycle_num += 1
            log.info("")
            log.info("══  מחזור #%d  (mode=%s)  ══", cycle_num, args.mode)
            _run_cycle(base_url, elevator_id, secret, el_config, args.fast, args.mode)
            if not _running:
                break
            log.info("  ✓ מחזור #%d הסתיים", cycle_num)

    finally:
        if args.fast and args.restore:
            log.info("מחזיר TIME_PER_FLOOR המקורי (%.0fs) ל-Firebase...", orig_tpf)
            _patch_config(base_url, elevator_id, secret, {
                "TIME_PER_FLOOR":  orig_tpf,
                "TIME_PASS_FLOOR": orig_tpass,
                "FLOOR_WAITS":     orig_fw,
            })
            log.info("✓ הגדרות Firebase הוחזרו")
        elif args.fast:
            log.info("ℹ Firebase נשאר במצב fast (TIME_PER_FLOOR=%.2fs).", orig_tpf * 0.05)
            log.info("  לשחזור הרץ: python firebase_elevator_simulator.py --fast --restore --cycles 0")

    log.info("סיום.")


if __name__ == "__main__":
    main()
