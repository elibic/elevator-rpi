"""
run_with_simulator.py
─────────────────────
מריץ סימולטור מחזורי שבת ישירות מול ה-CycleAnalyzer + FSM, בלי Firebase polling.

במקום:  Simulator → Firebase → poll → Detector (2s latency + תחרות עם tracker)
עכשיו:  Simulator → Detector (ישיר) → Firebase (כתיבה בלבד אם לא --test-mode)

כל ערכי העצירות, הזמנים, ו-FLOOR_WAITS נלקחים ישירות מ-elevator_configs
ב-Firebase — כך ש-FSM תמיד מקבל מחזור שתואם להגדרות.

שימוש:
    python run_with_simulator.py                          # בזמן אמת, כותב ל-Firebase
    python run_with_simulator.py --test-mode              # לא כותב ל-Firebase
    python run_with_simulator.py --cycles 2               # עוצר אחרי 2 מחזורים
    python run_with_simulator.py --fast                   # דוחס זמן (מהיר, לבדיקות)
    python run_with_simulator.py --config other_files/elevator-RFID/rfid_config.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Iterator

import os
sys.path.insert(0, os.path.dirname(__file__))

from shabbat_detector.cycle_analyzer import CycleAnalyzer, FloorEvent
from shabbat_detector.firebase_client import FirebaseClient
from shabbat_detector.fsm import DetectorState, ElevatorFSM, FSMResult
from shabbat_detector.hebcal_gate import HebcalGate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sim-direct")


# ── Config ────────────────────────────────────────────────────────────────────

def _parse_rfid(path: str) -> tuple[str, str, str]:
    """Returns (firebase_url, elevator_id, secret_key) from rfid_config.json."""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    s = raw.get("settings", raw)
    url = s.get("FIREBASE_BASE_URL") or s.get("BASE_FIREBASE_URL") or s.get("FIREBASE_URL", "")
    url = url.rstrip("/")
    if url.endswith(".json"):
        url = url[: url.rfind("/")]
    return url, str(s.get("ELEVATOR_ID", "")), s.get("SECRET_KEY", "")


# ── Cycle generator ───────────────────────────────────────────────────────────

def _cycle_events(el_config: dict, fast: bool) -> Iterator[tuple[str, float]]:
    """
    Yields (floor_label, dwell_seconds) for one full cycle that MATCHES the
    elevator_config exactly — so the FSM always recognises it.

    Sequence: TOP → descend every floor → BOTTOM → ascend every floor → TOP
    (same structure as the simulator, but values from Firebase config).
    """
    top_n  = int(el_config.get("TOP_FLOOR",    12))
    bot_n  = int(el_config.get("BOTTOM_FLOOR", -3))
    tpf    = float(el_config.get("TIME_PER_FLOOR",  26))
    tpass  = float(el_config.get("TIME_PASS_FLOOR",  2))
    fw     = {str(k): float(v) for k, v in (el_config.get("FLOOR_WAITS") or {}).items()}
    dn     = {str(f) for f in (el_config.get("STOPPING_FLOORS_DOWN") or [])}
    up     = {str(f) for f in (el_config.get("STOPPING_FLOORS_UP")   or [])}

    speed = 0.05 if fast else 1.0   # fast=True → dwells compressed to 5%

    def dwell(f: str, stops: set) -> float:
        d = fw.get(f, tpf if f in stops else tpass)
        return d * speed

    top = str(top_n)
    bot = str(bot_n)

    # ── TOP terminal ──────────────────────────────────────────────────────────
    yield top, tpf * speed

    # ── Descend: TOP-1 → BOTTOM (inclusive) ──────────────────────────────────
    for fn in range(top_n - 1, bot_n - 1, -1):
        yield str(fn), dwell(str(fn), dn)
    # bot is the last floor in the loop above; don't re-yield it.

    # ── Ascend: BOTTOM+1 → TOP ────────────────────────────────────────────────
    for fn in range(bot_n + 1, top_n + 1):
        yield str(fn), dwell(str(fn), up)


# ── Pretty labels ─────────────────────────────────────────────────────────────

def _label(floor: str, dwell: float, el_config: dict) -> str:
    top  = str(el_config.get("TOP_FLOOR",    12))
    bot  = str(el_config.get("BOTTOM_FLOOR", -3))
    tpf  = float(el_config.get("TIME_PER_FLOOR", 26))
    fw   = {str(k): float(v) for k, v in (el_config.get("FLOOR_WAITS") or {}).items()}
    if floor in (top, bot):
        return f"TERMINAL  ({dwell:.0f}s)"
    if floor in fw:
        return f"WAIT      ({dwell:.0f}s)"
    if dwell >= tpf * 0.4:
        return f"STOP      ({dwell:.0f}s)"
    return     f"pass      ({dwell:.1f}s)"


# ── Firebase write helper ─────────────────────────────────────────────────────

def _write(result: FSMResult, fsm: ElevatorFSM, fb: FirebaseClient,
           test_mode: bool) -> None:
    updates: dict = {
        "SHABBAT_DETECTOR": {
            "state":                    result.new_state.value,
            "last_transition_ts":       int(time.time() * 1000),
            "last_transition_reason":   result.reason_he,
        }
    }
    if result.last_cycle_summary:
        updates["SHABBAT_DETECTOR"]["last_cycle_summary"] = result.last_cycle_summary
    if result.violation:
        updates["SHABBAT_DETECTOR"]["violations_window"] = [
            {"ts": v.ts, "floor": v.floor, "reason": v.reason}
            for v in fsm._violations[-10:]
        ]
    if result.shabbat_active is not None:
        updates["SHABBAT_ACTIVE"] = result.shabbat_active

    if test_mode:
        log.info("  [TEST] would write → SHABBAT_ACTIVE=%s  state=%s",
                 updates.get("SHABBAT_ACTIVE", "—"), result.new_state.value)
        log.info("         reason: %s", result.reason_he)
    else:
        fb.patch_elevator_config(updates)
        log.info("  [Firebase] written → SHABBAT_ACTIVE=%s  state=%s",
                 updates.get("SHABBAT_ACTIVE", "—"), result.new_state.value)


# ── Main ──────────────────────────────────────────────────────────────────────

def run(config_path: str, test_mode: bool, max_cycles: int, fast: bool, no_hebcal: bool = False) -> None:
    firebase_url, elevator_id, secret_key = _parse_rfid(config_path)
    if not firebase_url or not elevator_id:
        log.error("Bad config — missing FIREBASE_BASE_URL / ELEVATOR_ID")
        sys.exit(1)

    fb = FirebaseClient(firebase_url, secret_key, elevator_id)
    el_config = fb.get_elevator_config()
    settings  = fb.get_settings()

    if not el_config:
        log.error("Could not fetch elevator_config for %s", elevator_id)
        sys.exit(1)

    top = str(el_config.get("TOP_FLOOR",    12))
    bot = str(el_config.get("BOTTOM_FLOOR", -3))
    tpf = float(el_config.get("TIME_PER_FLOOR", 26))
    fw  = {str(k): float(v) for k, v in (el_config.get("FLOOR_WAITS") or {}).items()}

    # In fast mode the actual dwell times are compressed (×0.05), so the
    # CycleAnalyzer and FSM must use compressed thresholds too.
    speed = 0.05 if fast else 1.0
    if fast:
        sim_config = {
            **el_config,
            "TIME_PER_FLOOR":  tpf * speed,
            "TIME_PASS_FLOOR": float(el_config.get("TIME_PASS_FLOOR", 2)) * speed,
            "FLOOR_WAITS":     {k: v * speed for k, v in fw.items()},
        }
    else:
        sim_config = el_config

    sim_tpf = float(sim_config["TIME_PER_FLOOR"])
    sim_fw  = {str(k): float(v) for k, v in (sim_config.get("FLOOR_WAITS") or {}).items()}

    analyzer = CycleAnalyzer(top_floor=top, bottom_floor=bot, time_per_floor=sim_tpf, floor_waits=sim_fw)
    fsm      = ElevatorFSM(elevator_id)
    hebcal   = HebcalGate()

    hebcal_enabled = settings.get("HEBCAL_GATE_ENABLED", True) and not no_hebcal

    top_n = int(el_config.get("TOP_FLOOR",    12))
    bot_n = int(el_config.get("BOTTOM_FLOOR", -3))
    n_floors = top_n - bot_n + 1
    est_min = int(
        (tpf + sum(
            fw.get(str(f), tpf if str(f) in {str(x) for x in el_config.get("STOPPING_FLOORS_DOWN") or []} else float(el_config.get("TIME_PASS_FLOOR", 2)))
            for f in range(bot_n, top_n)
        ) * 2) / 60
    )

    log.info("━" * 60)
    log.info("  Simulator → Detector (ישיר)  |  מעלית %s", elevator_id)
    log.info("  %s ↔ %s  |  TIME_PER_FLOOR=%.0fs  |  FLOOR_WAITS=%s",
             bot, top, tpf, fw or "none")
    log.info("  UP stops:   %s", el_config.get("STOPPING_FLOORS_UP"))
    log.info("  DOWN stops: %s", el_config.get("STOPPING_FLOORS_DOWN"))
    log.info("  Hebcal gate: %s  |  fast=%s  |  test-mode=%s",
             "ON" if hebcal_enabled else "OFF (behavioral only)", fast, test_mode)
    if not hebcal_enabled:
        log.info("  ⚠  Hebcal gate כבוי — יכנס לשבת כל יום עם מחזור תואם")
    log.info("  מחזור אחד ≈ %d דקות%s", est_min, " (מדחס ×20)" if fast else "")
    log.info("━" * 60)

    prev_state = fsm.state
    cycle_num  = 0

    try:
        while max_cycles == 0 or cycle_num < max_cycles:
            cycle_num += 1
            log.info("")
            log.info("══  CYCLE #%d  (FSM=%s)  ══", cycle_num, fsm.state.value)

            for floor, dwell in _cycle_events(el_config, fast):
                now = time.time()
                event = FloorEvent(floor=floor, timestamp=now)

                log.info("  floor %4s  %s", floor, _label(floor, dwell, el_config))

                # ── Feed to CycleAnalyzer ──────────────────────────────────
                prev_fsm = fsm.state
                ar = analyzer.push_event(event)

                if ar.cycle_just_started:
                    result = fsm.on_cycle_started(now)
                    if result:
                        log.info("  ▶ FSM: %s → %s | %s",
                                 prev_fsm.value, result.new_state.value, result.reason_he)
                        _write(result, fsm, fb, test_mode)
                        prev_state = result.new_state

                if ar.completed_cycle:
                    hebcal_ok = (not hebcal_enabled) or hebcal.is_in_window(settings, now)
                    result = fsm.on_cycle_completed(
                        ar.completed_cycle, sim_config, settings, now, hebcal_ok
                    )
                    if result:
                        log.info("  ★ FSM: %s → %s | %s",
                                 prev_state.value, result.new_state.value, result.reason_he)
                        _write(result, fsm, fb, test_mode)
                        prev_state = result.new_state

                # ── Real-time pause ────────────────────────────────────────
                time.sleep(dwell)

    except KeyboardInterrupt:
        log.info("\nעצרת. FSM נשאר ב-%s.", fsm.state.value)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulator → Detector (direct pipeline, no Firebase polling)"
    )
    parser.add_argument("--config",    default="rfid_config.json",
                        help="נתיב ל-rfid_config.json (ברירת מחדל: rfid_config.json)")
    parser.add_argument("--test-mode", action="store_true",
                        help="לא כותב ל-Firebase, מדפיס בלבד")
    parser.add_argument("--cycles",    type=int, default=0,
                        help="מספר מחזורים לפני עצירה (0 = אין הגבלה)")
    parser.add_argument("--fast",      action="store_true",
                        help="דוחס את הזמן ×20 (לבדיקות מהירות)")
    parser.add_argument("--no-hebcal", action="store_true",
                        help="כבה Hebcal gate (לבדיקה בימות השבוע)")
    args = parser.parse_args()
    run(args.config, args.test_mode, args.cycles, args.fast, args.no_hebcal)


if __name__ == "__main__":
    main()
