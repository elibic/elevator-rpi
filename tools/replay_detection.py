"""Replay recorded tracker logs through the real CycleAnalyzer + FSM.

Shabbat detection is judged by what it does on one specific night a week, which
makes it nearly impossible to iterate on live: a tuning change can only be
evaluated a week later, on a building full of guests.  This tool closes that
loop - it feeds the `rfid_tracker.log` files that every Pi already backs up
(see `log_backup.py`) through the *real* `CycleAnalyzer` and `ElevatorFSM`, with
the *real* elevator config and `/settings` block from a Firebase export, and
prints the state timeline that would have been produced.

Because the inputs are the genuine recorded floor events, the output matches the
field to the second.  Validated against Ramada 2026-08-15, where it reproduces
the real detector log exactly:

    B  SHABBAT -> CANDIDATE_EXIT  21:59:11   (field log: 21:59:11)
    D  CANDIDATE_EXIT -> NORMAL   20:37:55   (field log: 20:37:55)

Usage
-----
    python3 tools/replay_detection.py --db export.json --elevator B \\
        --log ../elevator-logs/ramada/B/rfid_tracker.log \\
        --from "2026-08-14 17:00" --to "2026-08-16 03:00" \\
        --candles 1786723560 --havdalah 1786814100

`--legacy` replays with the post-window exit and the clock-driven
CANDIDATE_EXIT timeout disabled, i.e. the behaviour before those were added, so
a change can be diffed against the previous release on the same recording.

NOTE: `_watchdog()` below mirrors `detector._watchdog_tick()`.  The daemon owns
the real thing; keep this in sync when the watchdog gains a mechanism.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabbat_detector.cycle_analyzer import (      # noqa: E402
    CycleAnalyzer,
    FloorEvent,
    normalize_floor_waits,
)
from shabbat_detector.fsm import (                 # noqa: E402
    DetectorState,
    ElevatorFSM,
    Violation,
)

# The Pi writes local wall-clock timestamps; Israel is UTC+3 in summer.
DEFAULT_TZ_OFFSET_H = 3

LOG_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] Status Updated: Floor '(-?\d+)'"
)

# Mirrors detector._FLAP_WINDOW_S.
_FLAP_WINDOW_S = 3.0
_WATCHDOG_PERIOD_S = 30.0


@dataclass
class Transition:
    ts: float
    from_state: str
    to_state: str
    reason: str
    trigger: str
    in_window: bool


def load_events(path: str, start: float, end: float, tz) -> list[tuple[float, str]]:
    """Parse 'Status Updated' lines from a tracker log into (epoch, floor)."""
    out: list[tuple[float, str]] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LOG_RE.match(line)
            if not m:
                continue
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).timestamp()
            if start <= ts <= end:
                out.append((ts, m.group(2)))
    return out


def all_valid_stops(config: dict) -> set[str]:
    """Mirrors detector._all_valid_stops."""
    up = {str(f) for f in (config.get("STOPPING_FLOORS_UP") or [])}
    dn = {str(f) for f in (config.get("STOPPING_FLOORS_DOWN") or [])}
    terminals = {
        str(config.get("TOP_FLOOR", "")).strip(),
        str(config.get("BOTTOM_FLOOR", "")).strip(),
    }
    return up | dn | terminals


def replay(
    elevator_id: str,
    events: list[tuple[float, str]],
    config: dict,
    settings: dict,
    candles: Optional[float],
    havdalah: Optional[float],
    end_ts: float,
    legacy: bool = False,
) -> tuple[ElevatorFSM, list[Transition]]:
    """Run recorded events through the real analyzer + FSM.

    Returns the final FSM and every state transition it made.
    """
    cfg = dict(config)
    fsm = ElevatorFSM(elevator_id)
    fsm.update_settings(settings)
    analyzer = CycleAnalyzer(
        top_floor=str(cfg.get("TOP_FLOOR", "12")).strip(),
        bottom_floor=str(cfg.get("BOTTOM_FLOOR", "-3")).strip(),
        time_per_floor=float(cfg.get("TIME_PER_FLOOR", 26)),
        floor_waits={
            str(k): float(v)
            for k, v in normalize_floor_waits(cfg.get("FLOOR_WAITS")).items()
        },
    )
    stop_threshold = float(cfg.get("TIME_PER_FLOOR", 26)) * 0.5
    valid_stops = all_valid_stops(cfg)

    before_min = float(settings.get("HEBCAL_GATE_WINDOW_BEFORE_MINUTES", 240))
    after_min = float(settings.get("HEBCAL_GATE_WINDOW_AFTER_MINUTES", 120))
    gate_enabled = settings.get("HEBCAL_GATE_ENABLED", True)

    def in_window(now: float) -> bool:
        # Mirrors HebcalGate.is_in_window, including its fail-open behaviour.
        if not gate_enabled or candles is None or havdalah is None:
            return True
        return (candles - before_min * 60) <= now <= (havdalah + after_min * 60)

    transitions: list[Transition] = []
    prev_event: Optional[FloorEvent] = None
    prev_prev: Optional[FloorEvent] = None
    shared = {
        "last_event_received_ts": events[0][0] if events else 0.0,
        "last_missed_fire_ts": 0.0,
    }

    def record(prev_state: DetectorState, result, when: float, trigger: str) -> None:
        if result is None or result.new_state == prev_state:
            return
        transitions.append(Transition(
            ts=when,
            from_state=prev_state.value,
            to_state=result.new_state.value,
            reason=result.reason_he,
            trigger=trigger,
            in_window=in_window(when),
        ))

    def _watchdog(now: float) -> None:
        """Mirrors detector._watchdog_tick()."""
        if fsm.state not in (DetectorState.SHABBAT, DetectorState.CANDIDATE_EXIT):
            return
        tunables = fsm.tunables
        hebcal_ok = in_window(now)
        prev_state = fsm.state

        # 1: stuck on an invalid floor
        inactivity_min = float(tunables["INACTIVITY_AT_INVALID_FLOOR_MIN"])
        if (
            inactivity_min > 0
            and prev_event is not None
            and prev_event.floor not in valid_stops
            and (now - prev_event.timestamp) >= inactivity_min * 60
        ):
            v = Violation(ts=now, floor=prev_event.floor, reason="inactivity")
            record(prev_state, fsm.process_violation(v, cfg, now, hebcal_ok), now, "inactivity")
            prev_state = fsm.state

        # 2: tracker silent
        no_report_min = float(tunables["NO_REPORT_TIMEOUT_MIN"])
        if no_report_min > 0 and (now - shared["last_event_received_ts"]) >= no_report_min * 60:
            v = Violation(ts=now, floor="?", reason="no-report")
            record(prev_state, fsm.process_violation(v, cfg, now, hebcal_ok), now, "no-report")
            shared["last_event_received_ts"] = now
            prev_state = fsm.state

        # 3: cadence break (disabled fleet-wide via MISSED_CYCLE_FACTOR=0)
        missed_factor = float(tunables.get("MISSED_CYCLE_FACTOR", 0) or 0)
        period = fsm.expected_cycle_period or ElevatorFSM.expected_cycle_period_from_config(cfg)
        last_match = fsm.last_clean_cycle_ts
        gap_needed = missed_factor * period
        if (
            missed_factor > 0
            and period > 0
            and last_match > 0
            and fsm._stickiness_expired(now, True if legacy else hebcal_ok)
            and (now - last_match) >= gap_needed
            and (now - shared["last_missed_fire_ts"]) >= gap_needed
        ):
            v = Violation(ts=now, floor="cadence", reason="missed-cycle")
            fsm._consecutive_nonmatch += 1
            record(prev_state, fsm.process_violation(v, cfg, now, hebcal_ok), now, "missed-cycle")
            shared["last_missed_fire_ts"] = now
            prev_state = fsm.state

        if legacy:
            return

        # 4: halachic window closed and the Shabbat program stopped
        pw = fsm.check_post_window_exit(now, hebcal_ok, cfg)
        if pw is not None:
            record(prev_state, pw, now, "post-window")
            prev_state = fsm.state

        # 5: CANDIDATE_EXIT timeout, on the clock
        to = fsm.check_candidate_exit_timeout(now, hebcal_ok)
        if to is not None:
            record(prev_state, to, now, "candidate-timeout")

    next_tick = (events[0][0] if events else 0.0) + _WATCHDOG_PERIOD_S

    for now, floor in events:
        while next_tick <= now:
            _watchdog(next_tick)
            next_tick += _WATCHDOG_PERIOD_S
        shared["last_event_received_ts"] = now

        # Mirrors the dedup + reader-flap suppression in detector's event loop.
        if prev_event is not None and floor == prev_event.floor:
            continue
        if (
            prev_event is not None
            and prev_prev is not None
            and floor == prev_prev.floor
            and now - prev_event.timestamp < _FLAP_WINDOW_S
        ):
            continue

        event = FloorEvent(floor=floor, timestamp=now)
        hebcal_ok = in_window(now)

        if prev_event is not None and fsm.state in (
            DetectorState.SHABBAT, DetectorState.CANDIDATE_EXIT
        ):
            dwell = now - prev_event.timestamp
            if dwell >= stop_threshold and prev_event.floor not in valid_stops:
                v = Violation(
                    ts=now, floor=prev_event.floor,
                    reason=f"עצירה בקומה לא מוגדרת בשבת ({dwell:.0f}s)",
                )
                record(fsm.state, fsm.process_violation(v, cfg, now, hebcal_ok), now,
                       f"illegal-stop F{prev_event.floor}")

        ar = analyzer.push_event(event)
        prev_state = fsm.state
        if ar.cycle_just_started:
            record(prev_state, fsm.on_cycle_started(now), now, "cycle-start")
            prev_state = fsm.state
        if ar.completed_cycle:
            record(
                prev_state,
                fsm.on_cycle_completed(ar.completed_cycle, cfg, settings, now, hebcal_ok),
                now,
                f"cycle-{ar.completed_cycle.duration_s:.0f}s",
            )
        prev_prev, prev_event = prev_event, event

    while next_tick <= end_ts:
        _watchdog(next_tick)
        next_tick += _WATCHDOG_PERIOD_S

    return fsm, transitions


def _parse_when(s: str, tz) -> float:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=tz).timestamp()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="Firebase export JSON (settings + elevator_configs)")
    ap.add_argument("--elevator", required=True)
    ap.add_argument("--log", required=True, help="rfid_tracker.log to replay")
    ap.add_argument("--from", dest="start", required=True, help='"YYYY-MM-DD HH:MM" local time')
    ap.add_argument("--to", dest="end", required=True, help='"YYYY-MM-DD HH:MM" local time')
    ap.add_argument("--candles", type=float, help="candle-lighting epoch seconds")
    ap.add_argument("--havdalah", type=float, help="havdalah epoch seconds")
    ap.add_argument("--tz-offset", type=float, default=DEFAULT_TZ_OFFSET_H)
    ap.add_argument("--legacy", action="store_true",
                    help="replay without the post-window exit / clock-driven timeout")
    args = ap.parse_args()

    tz = timezone(timedelta(hours=args.tz_offset))
    db = json.load(open(args.db, encoding="utf-8"))
    settings = db.get("settings", {})
    config = db.get("elevator_configs", {})[args.elevator]

    start, end = _parse_when(args.start, tz), _parse_when(args.end, tz)
    events = load_events(args.log, start, end, tz)

    def fmt(t: float) -> str:
        return datetime.fromtimestamp(t, tz).strftime("%d/%m %H:%M:%S")

    print(f"elevator {args.elevator}  events={len(events)}  "
          f"mode={'legacy' if args.legacy else 'current'}")
    if args.candles and args.havdalah:
        b = float(settings.get("HEBCAL_GATE_WINDOW_BEFORE_MINUTES", 240))
        a = float(settings.get("HEBCAL_GATE_WINDOW_AFTER_MINUTES", 120))
        print(f"hebcal window {fmt(args.candles - b * 60)} .. {fmt(args.havdalah + a * 60)}"
              f"   (havdalah {fmt(args.havdalah)})")
    print()

    fsm, transitions = replay(
        args.elevator, events, config, settings,
        args.candles, args.havdalah, end, legacy=args.legacy,
    )

    for t in transitions:
        flag = "in-window " if t.in_window else "POST-WINDOW"
        print(f"{fmt(t.ts)}  [{flag}] {t.from_state} -> {t.to_state}  ({t.trigger})")
        print(f"                          {t.reason}")
    print(f"\nfinal state: {fsm.state.value}")


if __name__ == "__main__":
    main()
