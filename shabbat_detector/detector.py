"""
Main daemon for the Shabbat Elevator Detector.

Run one instance per elevator, on the same RPi as the RFID tracker.
Reads its elevator ID and Firebase URL from the same rfid_config.json
the tracker uses — no separate config needed.

Usage:
    python -m shabbat_detector.detector [--config rfid_config.json]
    python -m shabbat_detector.detector --test-mode   (skips Firebase writes)
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Optional

# Floor IDs we accept from Firebase. Anything else (e.g. Hebrew names like
# "קומה אפס") is dropped before reaching the cycle analyzer / FSM.
_FLOOR_RE = re.compile(r"^-?\d+$")


def _is_valid_floor(s) -> bool:
    return isinstance(s, str) and bool(_FLOOR_RE.match(s))

from .auto_learner import AutoLearner
from .cycle_analyzer import Cycle, CycleAnalyzer, FloorEvent
from .firebase_client import FirebaseClient
from .fsm import DetectorState, ElevatorFSM, FSMResult, Violation, _TIME_SCALE
from .hebcal_gate import HebcalGate
from .state_persistence import StatePersistence

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

# ── Persistent weekly-rotating file log ────────────────────────────────────
# The systemd journal here is RAM-only (wiped on every reboot). This handler
# keeps a plain-text log on disk that rotates every Tuesday at 00:00 and keeps
# 4 rotated files, so there is always ~4 weeks of clear, human-readable history
# in one place - covering every detector sub-module (fsm, firebase, learner...).
_LOG_DIR = os.environ.get(
    "SHABBAT_LOG_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"),
)
try:
    os.makedirs(_LOG_DIR, exist_ok=True)
    _file_handler = logging.handlers.TimedRotatingFileHandler(
        os.path.join(_LOG_DIR, "shabbat_detector.log"),
        when="W1",          # weekly, Tuesday at midnight
        interval=1,
        backupCount=4,      # keep 4 rotated files (~4 weeks)
        encoding="utf-8",
    )
    _file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",   # full date in the file
        )
    )
    logging.getLogger().addHandler(_file_handler)
except Exception as _exc:  # never crash the detector over logging setup
    logging.getLogger("detector").warning("File log setup failed: %s", _exc)

log = logging.getLogger("detector")


def _load_rfid_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _all_valid_stops(config: dict) -> set[str]:
    up = {str(f) for f in (config.get("STOPPING_FLOORS_UP") or [])}
    dn = {str(f) for f in (config.get("STOPPING_FLOORS_DOWN") or [])}
    terminals = {str(config.get("TOP_FLOOR", "")), str(config.get("BOTTOM_FLOOR", ""))}
    return up | dn | terminals


def _apply_override(shabbat_active: Optional[bool], override: str) -> Optional[bool]:
    """Apply manual SHABBAT_OVERRIDE on top of an FSM-decided shabbat_active.

    The FSM keeps running and recording cycles in the background (per UI promise).
    The override only changes what value gets written to Firebase.
    """
    if override == "force_on":
        return True
    if override == "force_off":
        return False
    return shabbat_active


def _apply_result(
    result: Optional[FSMResult],
    fsm: ElevatorFSM,
    fb: FirebaseClient,
    prev_state: DetectorState,
    test_mode: bool,
    override: str = "auto",
) -> None:
    if result is None:
        return

    now_ts = int(time.time())
    now_str = datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d %H:%M:%S")

    updates: dict = {
        "SHABBAT_DETECTOR": {
            "state": result.new_state.value,
            "last_transition_ts": now_ts * 1000,
            "last_transition_reason": result.reason_he,
        }
    }

    if result.last_cycle_summary:
        updates["SHABBAT_DETECTOR"]["last_cycle_summary"] = result.last_cycle_summary

    if result.violation:
        # Build violations_window from the FSM's internal list
        updates["SHABBAT_DETECTOR"]["violations_window"] = [
            {"ts": v.ts, "floor": v.floor, "reason": v.reason}
            for v in fsm._violations[-10:]
        ]

    # Apply manual override on top of FSM decision
    effective_shabbat_active = _apply_override(result.shabbat_active, override)
    changed = result.new_state != prev_state or effective_shabbat_active is not None

    if effective_shabbat_active is not None:
        updates["SHABBAT_ACTIVE"] = effective_shabbat_active

    if test_mode:
        log.info("[TEST] Would write: %s", json.dumps(updates, ensure_ascii=False))
        return

    if changed:
        # Precise, self-contained cause line on disk: state change + the exact
        # reason (which structural check entered/left Shabbat).  This replaces
        # the old Firebase /logs audit trail (removed - it only ever 401'd and
        # the user wants on-device logging only).
        if result.new_state != prev_state:
            log.info(
                "SHABBAT TRANSITION %s -> %s | סיבה: %s",
                prev_state.value, result.new_state.value, result.reason_he,
            )
        fb.patch_elevator_config(updates)
    elif result.last_cycle_summary:
        # Always update last_cycle_summary so admin sees it
        fb.patch_elevator_config({
            "SHABBAT_DETECTOR": {
                "state": result.new_state.value,
                "last_cycle_summary": result.last_cycle_summary,
                "last_transition_reason": result.reason_he,
            }
        })


def _apply_auto_learn(
    cycle: Cycle,
    matched: bool,
    fsm_state: DetectorState,
    el_config: dict,
    learner: AutoLearner,
    fb: FirebaseClient,
    test_mode: bool,
    now: float,
) -> None:
    """Feed a completed cycle to the auto-learner and write updates if warranted."""
    if not matched or fsm_state != DetectorState.SHABBAT:
        return

    auto_learn_mode = el_config.get("AUTO_LEARN_CONFIG", "off")
    if auto_learn_mode not in ("suggest", "auto"):
        return

    learner.add_cycle(cycle)

    suggestion = learner.get_suggestion(AutoLearner.MIN_CYCLES_SUGGEST)
    if not suggestion:
        return

    log.info(
        "AutoLearner: suggestion from %d cycles (consistency=%.2f, cv=%.3f)",
        suggestion["based_on_cycles"],
        suggestion["consistency_score"],
        suggestion.get("timing_cv", 0.0),
    )

    if auto_learn_mode == "auto" and learner.is_safe_to_auto_apply():
        # Apply suggestion directly to config fields
        update: dict = {
            "STOPPING_FLOORS_UP":   suggestion.get("STOPPING_FLOORS_UP"),
            "STOPPING_FLOORS_DOWN": suggestion.get("STOPPING_FLOORS_DOWN"),
            "TIME_PER_FLOOR":       suggestion.get("TIME_PER_FLOOR"),
            "SHABBAT_DETECTOR": {
                "suggested_config":    suggestion,
                "last_auto_learn_ts":  int(now * 1000),
            },
        }
        if "TIME_PASS_FLOOR" in suggestion:
            update["TIME_PASS_FLOOR"] = suggestion["TIME_PASS_FLOOR"]
        if "FLOOR_WAITS" in suggestion:
            update["FLOOR_WAITS"] = suggestion["FLOOR_WAITS"]
        log.info(
            "AutoLearner: auto-applying config (cv=%.3f, cycles=%d)",
            suggestion.get("timing_cv", 0.0), learner.cycle_count,
        )
        if test_mode:
            log.info("[TEST] Auto-learn auto-apply: %s", json.dumps(update, ensure_ascii=False))
        else:
            fb.patch_elevator_config(update)
    else:
        # Write suggestion for admin review only
        update = {"SHABBAT_DETECTOR": {"suggested_config": suggestion}}
        if test_mode:
            log.info("[TEST] Auto-learn suggestion: %s", json.dumps(update, ensure_ascii=False))
        else:
            fb.patch_elevator_config(update)


def run(config_path: str = "rfid_config.json", test_mode: bool = False) -> None:
    # ── Load RPi config ───────────────────────────────────────────────────────
    rfid_cfg = _load_rfid_config(config_path)
    # Support both flat {"ELEVATOR_ID":...} and nested {"settings":{"ELEVATOR_ID":...}}
    _s = rfid_cfg.get("settings", rfid_cfg)
    raw_url: str = _s.get("FIREBASE_BASE_URL") or _s.get("BASE_FIREBASE_URL") or _s.get("FIREBASE_URL", "")
    # הגלאי בונה בעצמו /elevators, /elevator_configs, /settings — לכן הוא צריך את
    # *שורש* ה-DB. ניקח scheme://host בלבד, כך שזה עובד עם '/elevators',
    # '/elevators.json', או שורש — בלי תלות בפורמט שהוזן (תיקון footgun).
    from urllib.parse import urlsplit
    raw_url = raw_url.rstrip("/")
    _pu = urlsplit(raw_url)
    firebase_url: str = f"{_pu.scheme}://{_pu.netloc}" if (_pu.scheme and _pu.netloc) else raw_url
    elevator_id: str = str(_s.get("ELEVATOR_ID", rfid_cfg.get("ELEVATOR_ID", "")))
    secret_key: str = _s.get("SECRET_KEY", rfid_cfg.get("SECRET_KEY", ""))

    if not firebase_url or not elevator_id:
        log.error("rfid_config.json must contain BASE_FIREBASE_URL (or FIREBASE_URL) and ELEVATOR_ID")
        sys.exit(1)

    log.info("Detector starting for elevator %s (test_mode=%s)", elevator_id, test_mode)

    # ── State persistence ─────────────────────────────────────────────────────
    state_dir = _s.get("DETECTOR_STATE_DIR", rfid_cfg.get("DETECTOR_STATE_DIR"))
    persistence = StatePersistence(elevator_id, state_dir)

    # ── Firebase client ───────────────────────────────────────────────────────
    fb = FirebaseClient(firebase_url, secret_key, elevator_id)

    # ── Load initial snapshots ────────────────────────────────────────────────
    el_config = fb.get_elevator_config()
    settings = fb.get_settings()

    log.info("Loaded config: TOP=%s BOTTOM=%s TIME_PER_FLOOR=%s",
             el_config.get("TOP_FLOOR"), el_config.get("BOTTOM_FLOOR"),
             el_config.get("TIME_PER_FLOOR"))

    # ── Restore or create FSM + AutoLearner ──────────────────────────────────
    fsm = ElevatorFSM(elevator_id)
    learner = AutoLearner()
    saved = persistence.load()
    if saved:
        if "fsm" in saved:
            try:
                fsm = ElevatorFSM.from_dict(elevator_id, saved["fsm"])
                log.info("FSM restored: state=%s", fsm.state)
            except Exception as e:
                log.warning("Could not restore FSM: %s — starting fresh", e)
        if "learner" in saved:
            try:
                learner = AutoLearner.from_dict(saved["learner"])
                log.info("AutoLearner restored: %d cycle(s)", learner.cycle_count)
            except Exception as e:
                log.warning("Could not restore AutoLearner: %s", e)

    # Seed FSM with current global settings (SHABBAT_DETECTION tunables etc.)
    fsm.update_settings(settings)

    # ── Hebcal gate ───────────────────────────────────────────────────────────
    hebcal = HebcalGate()

    # ── Cycle analyzer ────────────────────────────────────────────────────────
    def _make_cycle_analyzer(cfg: dict) -> CycleAnalyzer:
        return CycleAnalyzer(
            top_floor=str(cfg.get("TOP_FLOOR", "12")),
            bottom_floor=str(cfg.get("BOTTOM_FLOOR", "-3")),
            time_per_floor=float(cfg.get("TIME_PER_FLOOR", 26)),
            floor_waits={str(k): float(v) for k, v in (cfg.get("FLOOR_WAITS") or {}).items()},
        )

    analyzer = _make_cycle_analyzer(el_config)
    stop_threshold = float(el_config.get("TIME_PER_FLOOR", 26)) * 0.5

    # ── Config change listener (background thread) ────────────────────────────
    # Note: the lock is acquired below (defined right after the subscriptions),
    # so we use a forward-declared closure that resolves at call time.
    def on_config_update(new_cfg: dict) -> None:
        nonlocal el_config, stop_threshold
        with _fsm_lock:
            # CRITICAL: SSE PATCH events only carry the *changed* fields.
            # We MUST merge into el_config — replacing it would wipe out
            # STOPPING_FLOORS_UP/DOWN, FLOOR_WAITS, etc. and the next cycle
            # eval would mark every observed stop as illegal.
            log.info("Config updated remotely (keys=%s)", list((new_cfg or {}).keys()))
            prev_override = (el_config or {}).get("SHABBAT_OVERRIDE", "auto") or "auto"
            # The detector PATCHes /elevator_configs/{id} (SHABBAT_DETECTOR state,
            # last_cycle_summary, auto-learn suggestions) and ALSO subscribes to
            # that same node, so every such write echoes back here. Resetting the
            # CycleAnalyzer on those echoes silently discards the in-progress
            # cycle and flaps the FSM out of SHABBAT.  Only touch the analyzer
            # when a field it actually uses changed value.
            _CYCLE_KEYS = ("TOP_FLOOR", "BOTTOM_FLOOR", "TIME_PER_FLOOR", "FLOOR_WAITS")
            nc = new_cfg or {}
            cycle_relevant_changed = any(
                k in nc and nc[k] != (el_config or {}).get(k) for k in _CYCLE_KEYS
            )
            merged = {**el_config, **nc}
            el_config = merged
            stop_threshold = float(merged.get("TIME_PER_FLOOR", 26)) * 0.5
            if cycle_relevant_changed:
                analyzer.update_config(merged)

            # If SHABBAT_OVERRIDE changed, immediately reflect it in SHABBAT_ACTIVE
            # so kiosks see the switch without waiting for the next elevator event.
            new_override = (merged.get("SHABBAT_OVERRIDE") or "auto")
            if new_override != prev_override:
                # FSM decision is unaffected; we just rewrite what kiosks see.
                fsm_says = (fsm.state == DetectorState.SHABBAT)
                effective = _apply_override(fsm_says, new_override)
                prev_effective = _apply_override(fsm_says, prev_override)
                log.info(
                    "SHABBAT_OVERRIDE %s -> %s ; SHABBAT_ACTIVE := %s",
                    prev_override, new_override, effective,
                )
                if not test_mode:
                    updates = {"SHABBAT_ACTIVE": effective}
                    # רושמים זמן-מעבר טרי רק כשהמצב המוצג באמת מתחלף (לא כשהוא נשאר זהה,
                    # למשל force_off בזמן שה-FSM ממילא כבוי) — כדי שלא ניצור "מעבר" מדומה.
                    # ממזגים את שאר שדות SHABBAT_DETECTOR כדי לא לדרוס state/last_cycle_summary.
                    if effective != prev_effective:
                        sd = dict(el_config.get("SHABBAT_DETECTOR") or {})
                        sd["state"] = fsm.state.value
                        sd["last_transition_ts"] = int(time.time() * 1000)
                        sd["last_transition_reason"] = "override ידני: " + new_override
                        updates["SHABBAT_DETECTOR"] = sd
                    fb.patch_elevator_config(updates)

    def on_settings_update(new_settings: dict) -> None:
        nonlocal settings
        with _fsm_lock:
            # Same merge requirement as on_config_update — PATCH events are partial.
            settings = {**settings, **(new_settings or {})}
            # Push global settings (incl. SHABBAT_DETECTION tunables) into the FSM
            # so threshold changes take effect on the next cycle eval / watchdog tick.
            try:
                fsm.update_settings(settings)
            except Exception as e:
                log.warning("Could not propagate settings to FSM: %s", e)

    # Lock must exist BEFORE we subscribe (subscriber may fire immediately)
    _fsm_lock = threading.Lock()

    fb.subscribe_config(on_config_update)
    fb.subscribe_settings(on_settings_update)

    # ── Shared mutable state (accessed by main loop AND watchdog) ─────────────
    # All access to the FSM and to these values must be guarded by _fsm_lock
    # (already created above).
    _shared = {
        "prev_event": None,                      # type: Optional[FloorEvent]
        "last_event_received_ts": time.time(),   # any Firebase event (incl. dups)
        "last_missed_fire_ts": 0.0,              # throttle for cadence watchdog
    }

    def _full_state() -> dict:
        # Note: older saved state may still contain a "notify" key — it is simply
        # ignored on load, and no longer written here.
        return {
            "fsm": fsm.to_dict(),
            "learner": learner.to_dict(),
        }

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    _running = [True]

    def _shutdown(sig, frame):
        log.info("Shutting down (signal %s)", sig)
        _running[0] = False
        with _fsm_lock:
            persistence.save(_full_state(), force=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # ── Inactivity watchdog (background thread) ───────────────────────────────
    # Fires periodically to catch two scenarios that the event-driven main loop
    # cannot detect on its own:
    #  1. Elevator stuck on a non-Shabbat floor for INACTIVITY_AT_INVALID_FLOOR_MIN
    #  2. Tracker stopped reporting entirely for NO_REPORT_TIMEOUT_MIN
    # Both are reported as Violations and processed via fsm.process_violation,
    # so they accumulate toward VIOLATIONS_FOR_EXIT just like cycle-based ones.
    def _watchdog_tick() -> None:
        with _fsm_lock:
            if fsm.state not in (DetectorState.SHABBAT, DetectorState.CANDIDATE_EXIT):
                return
            now = time.time()
            tunables = fsm.tunables
            valid_stops = _all_valid_stops(el_config)

            hebcal_enabled = settings.get("HEBCAL_GATE_ENABLED", True)
            if hebcal_enabled:
                hebcal_ok = hebcal.is_in_window(settings, now)
            else:
                hebcal_ok = True

            override = (el_config.get("SHABBAT_OVERRIDE") or "auto")
            prev_state = fsm.state

            # ── Mechanism 1: stuck on an invalid floor too long ────────────
            prev = _shared["prev_event"]
            inactivity_min = float(tunables["INACTIVITY_AT_INVALID_FLOOR_MIN"])
            inactivity_s = inactivity_min * 60 * _TIME_SCALE
            if (
                inactivity_min > 0
                and prev is not None
                and prev.floor not in valid_stops
                and (now - prev.timestamp) >= inactivity_s
            ):
                v = Violation(
                    ts=now,
                    floor=prev.floor,
                    reason=f"חוסר תנועה {inactivity_min:.0f}+ דקות בקומה לא-מותרת ({prev.floor})",
                )
                log.info("Watchdog: inactivity violation at floor %s", prev.floor)
                vresult = fsm.process_violation(v, el_config, now, hebcal_ok)
                _apply_result(vresult, fsm, fb, prev_state, test_mode, override)
                # Reset the floor's "first seen" timestamp so we don't refire immediately
                _shared["prev_event"] = FloorEvent(floor=prev.floor, timestamp=now)
                prev_state = fsm.state

            # ── Mechanism 2: tracker has gone silent ────────────────────────
            no_report_min = float(tunables["NO_REPORT_TIMEOUT_MIN"])
            no_report_s = no_report_min * 60 * _TIME_SCALE
            last_evt_ts = _shared["last_event_received_ts"]
            if (
                no_report_min > 0
                and (now - last_evt_ts) >= no_report_s
            ):
                v = Violation(
                    ts=now,
                    floor="?",
                    reason=f"ה-tracker לא דיווח {no_report_min:.0f}+ דקות",
                )
                log.info("Watchdog: no-report violation")
                vresult = fsm.process_violation(v, el_config, now, hebcal_ok)
                _apply_result(vresult, fsm, fb, prev_state, test_mode, override)
                _shared["last_event_received_ts"] = now
                prev_state = fsm.state

            # ── Mechanism 3: Shabbat cadence broke (missed-cycle) ───────────
            # The elevator may still be moving (so Mechanism 2 stays quiet) but
            # has stopped producing Shabbat-pattern cycles - the motzaei-Shabbat
            # case.  If no matching cycle has completed for MISSED_CYCLE_FACTOR x
            # the config-implied cycle period, raise a violation anchored to the
            # elevator's own rhythm rather than a fixed clock window.
            missed_factor = float(tunables.get("MISSED_CYCLE_FACTOR", 0) or 0)
            period = fsm.expected_cycle_period or ElevatorFSM.expected_cycle_period_from_config(el_config)
            last_match = fsm.last_clean_cycle_ts
            gap_needed = missed_factor * period * _TIME_SCALE
            last_missed_fire = _shared["last_missed_fire_ts"]
            if (
                missed_factor > 0
                and period > 0
                and last_match > 0
                # Only once the minimum-stickiness window has passed (exit is
                # blocked before that anyway) — this keeps the nonmatch counter
                # from accumulating on blocked ticks and exiting prematurely.
                and fsm._stickiness_expired(now)
                # Genuinely off-pattern for the required gap since the LAST real
                # matching cycle (true anchor, never overwritten here)...
                and (now - last_match) >= gap_needed
                # ...and throttle re-fires to once per gap via a separate marker,
                # so last_clean_cycle_ts keeps meaning "last matching cycle".
                and (now - last_missed_fire) >= gap_needed
            ):
                elapsed_min = (now - last_match) / 60.0
                v = Violation(
                    ts=now,
                    floor="cadence",
                    reason=(
                        f"אין מחזור-שבת תקין {elapsed_min:.0f} דקות "
                        f"({(now - last_match) / period:.1f}x מזמן-המחזור הצפוי)"
                    ),
                )
                log.info("Watchdog: missed-cycle violation (%.0fm since last match)", elapsed_min)
                # A missed Shabbat period counts as a non-matching cycle, so two
                # of them satisfy CONSECUTIVE_NONMATCH_FOR_EXIT and drive the exit.
                fsm._consecutive_nonmatch += 1
                vresult = fsm.process_violation(v, el_config, now, hebcal_ok)
                _apply_result(vresult, fsm, fb, prev_state, test_mode, override)
                _shared["last_missed_fire_ts"] = now

    def _watchdog_loop() -> None:
        # Tick every 30s.  Cheap operation when not in SHABBAT/CANDIDATE_EXIT.
        while _running[0]:
            time.sleep(30)
            if not _running[0]:
                break
            try:
                _watchdog_tick()
            except Exception as e:
                log.warning("Watchdog tick failed: %s", e)

    watchdog_thread = threading.Thread(target=_watchdog_loop, daemon=True, name="inactivity-watchdog")
    watchdog_thread.start()

    # ── Main event loop ───────────────────────────────────────────────────────
    log.info("Listening for elevator events...")

    for raw in fb.stream_elevator_events():
        if not _running[0]:
            break

        # ANY event from Firebase resets the no-report timer (including
        # duplicates that we'll drop below).  Track this BEFORE filtering.
        _shared["last_event_received_ts"] = time.time()

        floor_raw = raw.get("floor")
        ts_raw = raw.get("timestamp")

        if not floor_raw or ts_raw is None:
            continue

        floor = str(floor_raw)
        # Drop non-integer floor identifiers (e.g. Hebrew aliases like "קומה אפס")
        # so they cannot poison cycle analysis or be flagged as illegal stops.
        if not _is_valid_floor(floor):
            log.debug("Ignoring non-integer floor value: %r", floor)
            continue

        # Deduplicate: if the tracker alternates between a numeric tag and a
        # non-numeric tag (e.g. '-1' ↔ 'מינוסתיים'), after filtering we get
        # repeated '-1' events every ~2s.  Without deduplication the
        # CycleAnalyzer measures dwell=2s instead of the real dwell (e.g. 72s),
        # so the floor is never recognised as a stop.
        # Fix: once a floor is seen, ignore further events for the *same* floor
        # until a different floor arrives.  The dwell will be measured from the
        # *first* arrival of the floor to the arrival of the next different floor.
        prev_event = _shared["prev_event"]
        if prev_event is not None and floor == prev_event.floor:
            log.debug("Skipping duplicate floor event: floor=%r (already at this floor)", floor)
            continue

        now = float(ts_raw)
        event = FloorEvent(floor=floor, timestamp=now)

        # Determine Hebcal window status
        hebcal_gate_enabled = settings.get("HEBCAL_GATE_ENABLED", True)
        if hebcal_gate_enabled:
            hebcal_ok = hebcal.is_in_window(settings, now)
        else:
            hebcal_ok = True   # gate disabled → always allow

        # All FSM mutations (and the reads that feed them) happen inside the lock
        # so the watchdog thread can't fire a violation in the middle of
        # cycle handling.
        with _fsm_lock:
            # ── Mid-cycle violation check (only while in SHABBAT / CANDIDATE_EXIT) ──
            if (
                prev_event is not None
                and fsm.state in (DetectorState.SHABBAT, DetectorState.CANDIDATE_EXIT)
            ):
                dwell = now - prev_event.timestamp
                valid_stops = _all_valid_stops(el_config)
                if dwell >= stop_threshold and prev_event.floor not in valid_stops:
                    v = Violation(
                        ts=now,
                        floor=prev_event.floor,
                        reason=f"עצירה בקומה לא מוגדרת בשבת ({dwell:.0f}s)",
                    )
                    prev_fsm_state = fsm.state
                    vresult = fsm.process_violation(v, el_config, now, hebcal_ok)
                    override = (el_config.get("SHABBAT_OVERRIDE") or "auto")
                    _apply_result(vresult, fsm, fb, prev_fsm_state, test_mode, override)

            # ── Feed to cycle analyzer ────────────────────────────────────────────
            ar = analyzer.push_event(event)

            prev_fsm_state = fsm.state
            override = (el_config.get("SHABBAT_OVERRIDE") or "auto")

            if ar.cycle_just_started:
                cresult = fsm.on_cycle_started(now)
                _apply_result(cresult, fsm, fb, prev_fsm_state, test_mode, override)
                prev_fsm_state = fsm.state

            if ar.completed_cycle:
                cresult = fsm.on_cycle_completed(
                    ar.completed_cycle, el_config, settings, now, hebcal_ok
                )
                _apply_result(cresult, fsm, fb, prev_fsm_state, test_mode, override)

                # Auto-learn: feed matched cycles to the learner
                if cresult and cresult.last_cycle_summary:
                    _apply_auto_learn(
                        ar.completed_cycle,
                        cresult.last_cycle_summary.get("matched", False),
                        fsm.state,
                        el_config,
                        learner,
                        fb,
                        test_mode,
                        now,
                    )

                # Reset learner when the elevator exits Shabbat mode
                if prev_fsm_state == DetectorState.SHABBAT and fsm.state == DetectorState.NORMAL:
                    learner.reset()
                    log.info("AutoLearner: reset (exited SHABBAT)")

            _shared["prev_event"] = event

            # Persist state (rate-limited internally)
            persistence.save(_full_state())

    log.info("Event loop ended")


def main() -> None:
    parser = argparse.ArgumentParser(description="Shabbat Elevator Detector")
    parser.add_argument(
        "--config", default="rfid_config.json",
        help="Path to rfid_config.json (default: rfid_config.json)",
    )
    parser.add_argument(
        "--test-mode", action="store_true",
        help="Log Firebase writes without actually sending them",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()
    logging.getLogger().setLevel(args.log_level)
    run(config_path=args.config, test_mode=args.test_mode)


if __name__ == "__main__":
    main()
