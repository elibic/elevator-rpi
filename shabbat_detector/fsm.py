"""
Autonomous Shabbat-mode state machine for one elevator.

States
------
NORMAL              elevator running in weekday mode
CANDIDATE_SHABBAT   first leg of a potential Shabbat cycle in progress
SHABBAT             Shabbat confirmed; SHABBAT_ACTIVE = true
CANDIDATE_EXIT      suspicious activity; accumulating violations

Transitions
-----------
NORMAL → CANDIDATE_SHABBAT   elevator reaches a terminal, cycle starts
CANDIDATE_SHABBAT → SHABBAT  REQUIRED_MATCHING_CYCLES consecutive matching cycles
                             (+ Hebcal gate if enabled)
CANDIDATE_SHABBAT → NORMAL   completed cycle does NOT match
SHABBAT → CANDIDATE_EXIT     ≥ VIOLATIONS_FOR_EXIT in window (after stickiness)
CANDIDATE_EXIT → NORMAL      one more violation OR timeout without a clean cycle
CANDIDATE_EXIT → SHABBAT     clean cycle observed — clear the candidate

All thresholds are tunable via `settings/SHABBAT_DETECTION` in Firebase.
See `ElevatorFSM.DEFAULTS` for the field schema and default values.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional

from .cycle_analyzer import Cycle


# Floor identifiers must be plain integers (or negative integers).
# Anything else (e.g. Hebrew aliases) is excluded from cycle validation.
_INT_FLOOR_RE = re.compile(r"^-?\d+$")


def _filter_int_floors(values: Iterable) -> set[str]:
    out: set[str] = set()
    for v in values or []:
        s = str(v).strip()
        if _INT_FLOOR_RE.match(s):
            out.add(s)
    return out


log = logging.getLogger(__name__)

# Time scaling for fast simulations.  Set FSM_TIME_SCALE=0.05 (or similar) to
# compress real-world minute-scale thresholds for testing.  Production = 1.0.
_TIME_SCALE = float(os.environ.get("FSM_TIME_SCALE", "1.0"))


# ── Enums / data classes ───────────────────────────────────────────────────────

class DetectorState(str, Enum):
    NORMAL = "NORMAL"
    CANDIDATE_SHABBAT = "CANDIDATE_SHABBAT"
    SHABBAT = "SHABBAT"
    CANDIDATE_EXIT = "CANDIDATE_EXIT"


@dataclass
class Violation:
    ts: float
    floor: str
    reason: str


@dataclass
class FSMResult:
    new_state: DetectorState
    shabbat_active: Optional[bool]   # None → no write needed
    reason_he: str                   # Hebrew string for admin UI
    last_cycle_summary: Optional[dict] = None
    violation: Optional[Violation] = None


# ── ElevatorFSM ────────────────────────────────────────────────────────────────

class ElevatorFSM:
    """
    All thresholds are now customer-tunable via Firebase
    `settings/SHABBAT_DETECTION/...`.  Missing fields fall back to DEFAULTS.
    """

    DEFAULTS: dict = {
        # ── Entry tunables ─────────────────────────────────────────
        # How many consecutive matching cycles are needed to enter SHABBAT.
        "REQUIRED_MATCHING_CYCLES":      1,
        # ±% allowed deviation of dwell from the configured TIME_PER_FLOOR / FLOOR_WAITS.
        "TIMING_TOLERANCE_PCT":          20,
        # Maximum timing exceptions tolerated within a single cycle.
        "MAX_TIMING_EXCEPTIONS":         1,
        # How many "illegal" stops (not in STOPPING_FLOORS) are forgiven per leg.
        "ALLOWED_ILLEGAL_STOPS_PER_LEG": 0,
        # How many configured stops can be missed per leg.
        "ALLOWED_MISSING_STOPS_PER_LEG": 1,

        # ── Exit tunables ──────────────────────────────────────────
        # Minimum time the FSM must remain in SHABBAT before any exit logic runs.
        "STICKINESS_MINUTES":            90,
        # Number of violations within VIOLATION_WINDOW_MINUTES that triggers
        # SHABBAT → CANDIDATE_EXIT (and later CANDIDATE_EXIT → NORMAL).
        "VIOLATIONS_FOR_EXIT":           3,
        # How far back (minutes) we look when counting violations.
        "VIOLATION_WINDOW_MINUTES":      20,
        # If we've been in CANDIDATE_EXIT this long with no resolution → exit.
        "CANDIDATE_EXIT_TIMEOUT_MIN":    30,
        # If the elevator stays this long on a NON-Shabbat-stop floor → violation.
        "INACTIVITY_AT_INVALID_FLOOR_MIN": 10,
        # If we don't receive any tracker reports for this long → violation.
        "NO_REPORT_TIMEOUT_MIN":         15,
    }

    # Cooldown between state transitions (prevents rapid flapping)
    COOLDOWN_S: float = 10 * 60 * _TIME_SCALE

    # Mid-cycle (already-in-SHABBAT) violation timing tolerance — not customer-tunable.
    EXIT_TIMING_TOLERANCE: float = 0.50

    def __init__(self, elevator_id: str):
        self.elevator_id = elevator_id
        self.state = DetectorState.NORMAL

        self._entered_state_at: float = 0.0
        self._shabbat_entered_at: Optional[float] = None  # for stickiness
        self._cooldown_until: float = 0.0
        self._candidate_exit_started: Optional[float] = None
        self._violations: list[Violation] = []
        self._last_clean_cycle_ts: float = 0.0

        # Counter for consecutive matching cycles in NORMAL/CANDIDATE_SHABBAT.
        # Promotes to SHABBAT once it reaches REQUIRED_MATCHING_CYCLES.
        self._consecutive_matches: int = 0

        # Tunables — initialised from DEFAULTS, overridden by update_settings().
        self._tunables: dict = dict(self.DEFAULTS)
        self._settings: dict = {}

    def update_settings(self, settings: dict) -> None:
        """Refresh the FSM's view of global settings.

        Reads `settings/SHABBAT_DETECTION` and merges with DEFAULTS.  Unknown
        keys in the customer block are ignored (so admin UI typos don't break
        anything); missing keys fall back to defaults.
        """
        self._settings = settings or {}
        cfg = self._settings.get("SHABBAT_DETECTION") or {}
        merged = dict(self.DEFAULTS)
        for k in self.DEFAULTS:
            if k in cfg and cfg[k] is not None:
                merged[k] = cfg[k]
        self._tunables = merged

    # Public read-only access for monitor.py / admin UI
    @property
    def tunables(self) -> dict:
        return dict(self._tunables)

    # ── Tunable accessors (apply _TIME_SCALE for time-based fields) ────────────

    def _stickiness_seconds(self) -> float:
        return float(self._tunables["STICKINESS_MINUTES"]) * 60 * _TIME_SCALE

    def _violation_window_seconds(self) -> float:
        return float(self._tunables["VIOLATION_WINDOW_MINUTES"]) * 60 * _TIME_SCALE

    def _candidate_exit_timeout_seconds(self) -> float:
        return float(self._tunables["CANDIDATE_EXIT_TIMEOUT_MIN"]) * 60 * _TIME_SCALE

    def _timing_tolerance(self) -> float:
        return float(self._tunables["TIMING_TOLERANCE_PCT"]) / 100.0

    # ── Public API ──────────────────────────────────────────────────────────────

    def on_cycle_started(self, now: float) -> Optional[FSMResult]:
        """Call when CycleAnalyzer signals a cycle has begun at a terminal."""
        if self.state != DetectorState.NORMAL:
            return None
        return self._transition_to(DetectorState.CANDIDATE_SHABBAT, now, FSMResult(
            new_state=DetectorState.CANDIDATE_SHABBAT,
            shabbat_active=None,
            reason_he="מחזור אפשרי החל — ממתין להשלמה",
        ))

    def on_cycle_completed(
        self,
        cycle: Cycle,
        config: dict,
        settings: dict,
        now: float,
        hebcal_in_window: bool = True,
    ) -> FSMResult:
        """Call when CycleAnalyzer emits a completed Cycle."""
        eval_result = self._evaluate_cycle(cycle, config)
        summary = self._make_summary(cycle, eval_result)
        return self._handle_completed_cycle(eval_result, summary, config, settings, now, hebcal_in_window)

    def process_violation(
        self,
        violation: Violation,
        config: dict,
        now: float,
        hebcal_in_window: bool = True,
    ) -> Optional[FSMResult]:
        """Call when a mid-cycle illegal stop or inactivity is detected."""
        if self.state not in (DetectorState.SHABBAT, DetectorState.CANDIDATE_EXIT):
            return None

        if not self._stickiness_expired(now):
            return FSMResult(
                new_state=self.state,
                shabbat_active=None,
                reason_he=(
                    f"חסום ע״י זמן הדבקה מינימלי — "
                    f"{self._remaining_stickiness_min(now):.0f} דקות נשארות"
                ),
                violation=violation,
            )

        self._add_violation(violation, now)
        return self._maybe_exit(now, hebcal_in_window)

    # ── Serialization ───────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "entered_state_at": self._entered_state_at,
            "shabbat_entered_at": self._shabbat_entered_at,
            "cooldown_until": self._cooldown_until,
            "candidate_exit_started": self._candidate_exit_started,
            "last_clean_cycle_ts": self._last_clean_cycle_ts,
            "consecutive_matches": self._consecutive_matches,
            "violations": [
                {"ts": v.ts, "floor": v.floor, "reason": v.reason}
                for v in self._violations
            ],
        }

    @classmethod
    def from_dict(cls, elevator_id: str, d: dict) -> "ElevatorFSM":
        fsm = cls(elevator_id)
        fsm.state = DetectorState(d.get("state", "NORMAL"))
        fsm._entered_state_at = float(d.get("entered_state_at", 0))
        fsm._shabbat_entered_at = d.get("shabbat_entered_at")
        if fsm._shabbat_entered_at is not None:
            fsm._shabbat_entered_at = float(fsm._shabbat_entered_at)
        fsm._cooldown_until = float(d.get("cooldown_until", 0))
        fsm._candidate_exit_started = d.get("candidate_exit_started")
        if fsm._candidate_exit_started is not None:
            fsm._candidate_exit_started = float(fsm._candidate_exit_started)
        fsm._last_clean_cycle_ts = float(d.get("last_clean_cycle_ts", 0))
        fsm._consecutive_matches = int(d.get("consecutive_matches", 0))
        fsm._violations = [
            Violation(ts=float(v["ts"]), floor=str(v["floor"]), reason=str(v["reason"]))
            for v in d.get("violations", [])
        ]
        return fsm

    # ── Cycle evaluation ────────────────────────────────────────────────────────

    def _evaluate_cycle(self, cycle: Cycle, config: dict) -> dict:
        # Only integer-valued floors participate in validation.
        # Non-numeric values from Firebase (e.g. "קומה אפס") are silently ignored,
        # both in the configured stops and in the observed stops.
        stops_up_cfg = _filter_int_floors(config.get("STOPPING_FLOORS_UP") or [])
        stops_dn_cfg = _filter_int_floors(config.get("STOPPING_FLOORS_DOWN") or [])
        top = str(config.get("TOP_FLOOR", "")).strip()
        bottom = str(config.get("BOTTOM_FLOOR", "")).strip()
        terminals = {t for t in (top, bottom) if _INT_FLOOR_RE.match(t)}
        stops_up_cfg -= terminals
        stops_dn_cfg -= terminals
        time_per_floor = float(config.get("TIME_PER_FLOOR", 26))
        floor_waits: dict = {str(k): float(v) for k, v in (config.get("FLOOR_WAITS") or {}).items()}

        observed_up = _filter_int_floors(cycle.up_stops)
        observed_dn = _filter_int_floors(cycle.down_stops)

        # Illegal stops: observed but NOT in config list
        illegal_up = sorted(observed_up - stops_up_cfg)
        illegal_dn = sorted(observed_dn - stops_dn_cfg)

        # Missing stops: configured but NOT observed
        missing_up = sorted(stops_up_cfg - observed_up)
        missing_dn = sorted(stops_dn_cfg - observed_dn)

        # All thresholds come from customer-tunable settings.
        timing_tol = self._timing_tolerance()
        max_timing_exc = int(self._tunables["MAX_TIMING_EXCEPTIONS"])
        allow_illegal = int(self._tunables["ALLOWED_ILLEGAL_STOPS_PER_LEG"])
        allow_missing = int(self._tunables["ALLOWED_MISSING_STOPS_PER_LEG"])

        # A missed RFID read inflates the adjacent floor's dwell, producing exactly
        # one illegal stop next to one missing stop.  Treat that pair as one miss.
        def _is_rfid_miss(illegal: list, missing: list) -> bool:
            if len(illegal) != 1 or not missing:
                return False
            try:
                ill = int(illegal[0])
                return any(abs(ill - int(m)) <= 2 for m in missing)
            except ValueError:
                return False

        def _illegal_ok(illegal: list, missing: list) -> bool:
            if len(illegal) == 0:
                return True
            if _is_rfid_miss(illegal, missing):
                return True
            return len(illegal) <= allow_illegal

        floors_ok = (
            _illegal_ok(illegal_up, missing_up)
            and _illegal_ok(illegal_dn, missing_dn)
            and len(missing_up) <= allow_missing
            and len(missing_dn) <= allow_missing
        )

        # Timing check (±timing_tol) on all stops in this cycle
        timing_exceptions = 0
        timing_details: dict[str, dict] = {}
        all_stops = observed_up | observed_dn
        all_dwells = {**cycle.up_dwells, **cycle.down_dwells}

        for floor, dwell in all_dwells.items():
            if floor not in all_stops:
                continue
            expected = floor_waits.get(floor, time_per_floor)
            lo = expected * (1 - timing_tol)
            hi = expected * (1 + timing_tol)
            ok = lo <= dwell <= hi
            timing_details[floor] = {"dwell": round(dwell, 1), "expected": expected, "ok": ok}
            if not ok:
                timing_exceptions += 1

        timing_ok = timing_exceptions <= max_timing_exc

        return {
            "matches": floors_ok and timing_ok,
            "illegal_up": illegal_up,
            "illegal_dn": illegal_dn,
            "missing_up": missing_up,
            "missing_dn": missing_dn,
            "timing_exceptions": timing_exceptions,
            "timing_details": timing_details,
            "floors_ok": floors_ok,
            "timing_ok": timing_ok,
        }

    # ── State transitions ───────────────────────────────────────────────────────

    def _handle_completed_cycle(
        self,
        eval_result: dict,
        summary: dict,
        config: dict,
        settings: dict,
        now: float,
        hebcal_in_window: bool,
    ) -> FSMResult:
        matches = eval_result["matches"]
        required = max(1, int(self._tunables["REQUIRED_MATCHING_CYCLES"]))

        if self.state in (DetectorState.NORMAL, DetectorState.CANDIDATE_SHABBAT):
            if matches:
                hebcal_enabled = settings.get("HEBCAL_GATE_ENABLED", True)
                if hebcal_enabled and not hebcal_in_window:
                    # Stay in CANDIDATE_SHABBAT — don't promote yet
                    if self.state == DetectorState.NORMAL:
                        self._transition_to(DetectorState.CANDIDATE_SHABBAT, now)
                    return FSMResult(
                        new_state=self.state,
                        shabbat_active=None,
                        reason_he="שבת זוהתה מחוץ לחלון הלכתי — מתעלם",
                        last_cycle_summary=summary,
                    )

                # Increment consecutive-match counter
                self._consecutive_matches += 1
                if self.state == DetectorState.NORMAL:
                    self._transition_to(DetectorState.CANDIDATE_SHABBAT, now)

                if self._consecutive_matches >= required:
                    return self._enter_shabbat(now, summary)

                return FSMResult(
                    new_state=self.state,
                    shabbat_active=None,
                    reason_he=(
                        f"מחזור {self._consecutive_matches}/{required} תואם — "
                        f"ממתין למחזור הבא"
                    ),
                    last_cycle_summary=summary,
                )
            else:
                # Mismatch resets the counter
                self._consecutive_matches = 0
                if self.state == DetectorState.CANDIDATE_SHABBAT:
                    self._transition_to(DetectorState.NORMAL, now)
                return FSMResult(
                    new_state=self.state,
                    shabbat_active=None,
                    reason_he=f"מחזור לא תאם — {self._mismatch_reason(eval_result)}",
                    last_cycle_summary=summary,
                )

        if self.state == DetectorState.SHABBAT:
            if matches:
                self._last_clean_cycle_ts = now
                self._violations.clear()
                return FSMResult(
                    new_state=self.state,
                    shabbat_active=None,
                    reason_he="מחזור תקין",
                    last_cycle_summary=summary,
                )
            # Mismatch in SHABBAT
            if not self._stickiness_expired(now):
                return FSMResult(
                    new_state=self.state,
                    shabbat_active=None,
                    reason_he=(
                        f"מחזור חורג — חסום ע״י זמן דבקה "
                        f"({self._remaining_stickiness_min(now):.0f} דקות נשארות)"
                    ),
                    last_cycle_summary=summary,
                )
            v = Violation(ts=now, floor="cycle", reason=self._mismatch_reason(eval_result))
            self._add_violation(v, now)
            return self._maybe_exit(now, hebcal_in_window, summary)

        if self.state == DetectorState.CANDIDATE_EXIT:
            if matches:
                # Clean cycle — return to SHABBAT
                self._last_clean_cycle_ts = now
                self._violations.clear()
                self._candidate_exit_started = None
                return self._transition_to(DetectorState.SHABBAT, now, FSMResult(
                    new_state=DetectorState.SHABBAT,
                    shabbat_active=None,
                    reason_he="מחזור תקין — חזרה למצב שבת",
                    last_cycle_summary=summary,
                ))
            v = Violation(ts=now, floor="cycle", reason=self._mismatch_reason(eval_result))
            self._add_violation(v, now)
            return self._maybe_exit(now, hebcal_in_window, summary)

        return FSMResult(
            new_state=self.state,
            shabbat_active=None,
            reason_he="ללא שינוי",
            last_cycle_summary=summary,
        )

    def _enter_shabbat(self, now: float, summary: dict) -> FSMResult:
        self._shabbat_entered_at = now
        self._violations.clear()
        self._consecutive_matches = 0
        self._last_clean_cycle_ts = now
        return self._transition_to(DetectorState.SHABBAT, now, FSMResult(
            new_state=DetectorState.SHABBAT,
            shabbat_active=True,
            reason_he="מחזור מלא תאם הגדרות — נכנס למצב שבת",
            last_cycle_summary=summary,
        ))

    def _maybe_exit(
        self,
        now: float,
        hebcal_in_window: bool,
        summary: Optional[dict] = None,
    ) -> FSMResult:
        threshold = int(self._tunables["VIOLATIONS_FOR_EXIT"])
        window_s = self._violation_window_seconds()
        window_min = int(self._tunables["VIOLATION_WINDOW_MINUTES"])
        recent = [v for v in self._violations if now - v.ts <= window_s]

        if len(recent) >= threshold:
            if self.state == DetectorState.SHABBAT:
                self._candidate_exit_started = now
                return self._transition_to(DetectorState.CANDIDATE_EXIT, now, FSMResult(
                    new_state=DetectorState.CANDIDATE_EXIT,
                    shabbat_active=None,
                    reason_he=f"{len(recent)} חריגות תוך {window_min} דקות — ממתין לאישור יציאה",
                    last_cycle_summary=summary,
                ))
            elif self.state == DetectorState.CANDIDATE_EXIT:
                # Another violation after CANDIDATE_EXIT → actually exit
                return self._transition_to(DetectorState.NORMAL, now, FSMResult(
                    new_state=DetectorState.NORMAL,
                    shabbat_active=False,
                    reason_he=f"יציאה ממצב שבת — {len(recent)} חריגות זוהו",
                    last_cycle_summary=summary,
                ))

        # Check CANDIDATE_EXIT timeout
        if (
            self.state == DetectorState.CANDIDATE_EXIT
            and self._candidate_exit_started
            and now - self._candidate_exit_started > self._candidate_exit_timeout_seconds()
        ):
            return self._transition_to(DetectorState.NORMAL, now, FSMResult(
                new_state=DetectorState.NORMAL,
                shabbat_active=False,
                reason_he="יציאה ממצב שבת — פסק זמן ב-CANDIDATE_EXIT",
                last_cycle_summary=summary,
            ))

        return FSMResult(
            new_state=self.state,
            shabbat_active=None,
            reason_he=f"{len(recent)}/{threshold} חריגות — ממשיך לצבור ראיות",
            last_cycle_summary=summary,
        )

    # ── Helpers ─────────────────────────────────────────────────────────────────

    def _transition_to(
        self, new_state: DetectorState, now: float, result: Optional[FSMResult] = None
    ) -> Optional[FSMResult]:
        old = self.state
        self.state = new_state
        self._entered_state_at = now
        self._cooldown_until = now + self.COOLDOWN_S
        if new_state not in (DetectorState.SHABBAT, DetectorState.CANDIDATE_EXIT):
            self._candidate_exit_started = None
        if new_state == DetectorState.NORMAL:
            self._shabbat_entered_at = None
            self._consecutive_matches = 0
        log.info("[%s] %s → %s", self.elevator_id, old, new_state)
        if result:
            result.new_state = new_state
        return result

    def _stickiness_expired(self, now: float) -> bool:
        if self._shabbat_entered_at is None:
            return True
        return now - self._shabbat_entered_at >= self._stickiness_seconds()

    def _remaining_stickiness_min(self, now: float) -> float:
        if self._shabbat_entered_at is None:
            return 0.0
        elapsed = now - self._shabbat_entered_at
        return max(0.0, self._stickiness_seconds() - elapsed) / 60

    def _add_violation(self, v: Violation, now: float) -> None:
        self._violations.append(v)
        # Prune old violations and cap the list
        window_s = self._violation_window_seconds()
        self._violations = [
            x for x in self._violations if now - x.ts <= window_s
        ][-20:]

    @staticmethod
    def _make_summary(cycle: Cycle, eval_result: dict) -> dict:
        return {
            "up_stops": cycle.up_stops,
            "down_stops": cycle.down_stops,
            "up_dwells": cycle.up_dwells,
            "down_dwells": cycle.down_dwells,
            "duration_s": round(cycle.duration_s, 1),
            "matched": eval_result["matches"],
            "illegal_up": eval_result["illegal_up"],
            "illegal_dn": eval_result["illegal_dn"],
            "missing_up": eval_result["missing_up"],
            "missing_dn": eval_result["missing_dn"],
            "timing_exceptions": eval_result["timing_exceptions"],
        }

    @staticmethod
    def _mismatch_reason(er: dict) -> str:
        parts = []
        if er.get("illegal_up"):
            parts.append(f"עצירות לא חוקיות בעלייה: {er['illegal_up']}")
        if er.get("illegal_dn"):
            parts.append(f"עצירות לא חוקיות בירידה: {er['illegal_dn']}")
        if er.get("missing_up") and len(er["missing_up"]) > 1:
            parts.append(f"עצירות חסרות בעלייה: {er['missing_up']}")
        if er.get("missing_dn") and len(er["missing_dn"]) > 1:
            parts.append(f"עצירות חסרות בירידה: {er['missing_dn']}")
        if er.get("timing_exceptions", 0) > 0:
            parts.append(f"{er['timing_exceptions']} חריגות טיימינג")
        return "; ".join(parts) if parts else "מחזור לא תאם"
