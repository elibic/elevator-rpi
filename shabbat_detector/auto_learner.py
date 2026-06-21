"""
Auto-learning module for the Shabbat Elevator Detector.

Accumulates matched Shabbat cycles and derives suggested configuration
parameters from observed behaviour.  The main FSM never calls this
directly — it is driven from detector.py after each matched cycle.

AUTO_LEARN_CONFIG values:
    "off"     — disabled (default); nothing is collected or written.
    "suggest" — accumulates cycles and writes SHABBAT_DETECTOR.suggested_config
                to Firebase for admin review.  No automatic config changes.
    "auto"    — same as "suggest" but also writes the suggestion directly to
                the top-level config fields once safety thresholds are met.
"""
from __future__ import annotations

import logging
from typing import Optional

from .cycle_analyzer import Cycle

log = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _median(sorted_vals: list) -> float:
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    mid = n // 2
    return sorted_vals[mid] if n % 2 else (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0


def _cv(vals: list) -> float:
    """Coefficient of variation (std / mean).  Returns 0 if fewer than 2 values."""
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    if mean == 0:
        return 0.0
    variance = sum((v - mean) ** 2 for v in vals) / len(vals)
    return (variance ** 0.5) / mean


def _sort_floors(floors: list) -> list:
    """Sort floor labels numerically when possible."""
    try:
        return sorted(floors, key=lambda f: int(f))
    except (ValueError, TypeError):
        return sorted(floors)


# ── AutoLearner ────────────────────────────────────────────────────────────────

class AutoLearner:
    """
    Accumulates matched Shabbat cycles and produces suggested_config.

    Thread-unsafe; call from the same event-loop thread as the detector.
    """

    # Minimum cycle counts
    MIN_CYCLES_SUGGEST: int = 1
    MIN_CYCLES_AUTO: int = 3

    # A floor is included in the suggested stop-set only if it appeared
    # as a stop in at least this fraction of accumulated cycles.
    STOP_FREQUENCY_THRESHOLD: float = 0.80

    # If timing coefficient of variation exceeds this, auto-apply is blocked.
    TIMING_CV_MAX: float = 0.15

    # Per-floor median dwell ratios that trigger a FLOOR_WAITS entry.
    FLOOR_WAIT_RATIO_MIN: float = 0.50   # floor_median ≤ 50 % of global median
    FLOOR_WAIT_RATIO_MAX: float = 1.50   # floor_median ≥ 150 % of global median

    def __init__(self) -> None:
        self._records: list[dict] = []
        self._first_ts: Optional[float] = None
        self._last_ts: Optional[float] = None

    # ── Public API ──────────────────────────────────────────────────────────────

    def add_cycle(self, cycle: Cycle) -> None:
        """Record one matched Shabbat cycle."""
        rec = {
            "up_stops":   list(cycle.up_stops),
            "down_stops": list(cycle.down_stops),
            "up_passes":  list(cycle.up_passes),
            "down_passes": list(cycle.down_passes),
            "up_dwells":   dict(cycle.up_dwells),
            "down_dwells": dict(cycle.down_dwells),
            "ts": cycle.end_ts,
        }
        self._records.append(rec)
        if self._first_ts is None:
            self._first_ts = cycle.start_ts
        self._last_ts = cycle.end_ts
        log.debug("AutoLearner: %d cycle(s) accumulated", len(self._records))

    def reset(self) -> None:
        """Discard all accumulated data (call when elevator exits SHABBAT)."""
        self._records = []
        self._first_ts = None
        self._last_ts = None

    @property
    def cycle_count(self) -> int:
        return len(self._records)

    def get_suggestion(self, min_cycles: int = 1) -> Optional[dict]:
        """
        Return a suggested_config dict if enough cycles have been accumulated.
        Returns None if fewer than *min_cycles* records exist.
        """
        n = len(self._records)
        if n < min_cycles:
            return None

        # ── Stop frequency ─────────────────────────────────────────────────────
        up_freq: dict[str, int] = {}
        dn_freq: dict[str, int] = {}
        for rec in self._records:
            for f in rec["up_stops"]:
                up_freq[f] = up_freq.get(f, 0) + 1
            for f in rec["down_stops"]:
                dn_freq[f] = dn_freq.get(f, 0) + 1

        stops_up = _sort_floors(
            [f for f, cnt in up_freq.items() if cnt / n >= self.STOP_FREQUENCY_THRESHOLD]
        )
        stops_dn = _sort_floors(
            [f for f, cnt in dn_freq.items() if cnt / n >= self.STOP_FREQUENCY_THRESHOLD]
        )

        # ── Consistency score: fraction of cycles with exactly the derived sets ─
        consistent_n = sum(
            1 for rec in self._records
            if set(rec["up_stops"]) == set(stops_up)
            and set(rec["down_stops"]) == set(stops_dn)
        )
        consistency_score = consistent_n / n

        # ── Dwell times for stop floors ────────────────────────────────────────
        all_stop_dwells: list[float] = []
        per_floor_dwells: dict[str, list[float]] = {}
        for rec in self._records:
            for direction in ("up", "down"):
                stops = set(rec[f"{direction}_stops"])
                dwells = rec[f"{direction}_dwells"]
                for f in stops:
                    d = dwells.get(f)
                    if d is not None:
                        all_stop_dwells.append(d)
                        per_floor_dwells.setdefault(f, []).append(d)

        if not all_stop_dwells:
            return None

        median_stop = _median(sorted(all_stop_dwells))

        # ── Pass-through dwell times ───────────────────────────────────────────
        pass_dwells: list[float] = []
        for rec in self._records:
            for direction in ("up", "down"):
                passes = set(rec[f"{direction}_passes"])
                dwells = rec[f"{direction}_dwells"]
                for f in passes:
                    d = dwells.get(f)
                    if d is not None:
                        pass_dwells.append(d)

        median_pass: Optional[float] = _median(sorted(pass_dwells)) if pass_dwells else None

        # ── FLOOR_WAITS: per-floor medians that deviate from global median ─────
        floor_waits: dict[str, float] = {}
        for floor, dwells_list in per_floor_dwells.items():
            floor_med = _median(sorted(dwells_list))
            ratio = floor_med / median_stop if median_stop > 0 else 1.0
            if ratio <= self.FLOOR_WAIT_RATIO_MIN or ratio >= self.FLOOR_WAIT_RATIO_MAX:
                floor_waits[floor] = round(floor_med, 1)

        # ── Timing CV on "regular" stops (not in FLOOR_WAITS) ─────────────────
        regular_dwells: list[float] = []
        for rec in self._records:
            for direction in ("up", "down"):
                stops = set(rec[f"{direction}_stops"])
                dwells = rec[f"{direction}_dwells"]
                for f in stops:
                    if f not in floor_waits:
                        d = dwells.get(f)
                        if d is not None:
                            regular_dwells.append(d)
        timing_cv = _cv(regular_dwells)

        # ── Assemble ───────────────────────────────────────────────────────────
        suggestion: dict = {
            "STOPPING_FLOORS_UP":   stops_up,
            "STOPPING_FLOORS_DOWN": stops_dn,
            "TIME_PER_FLOOR":       round(median_stop, 1),
            "based_on_cycles":      n,
            "first_cycle_ts":       self._first_ts,
            "last_cycle_ts":        self._last_ts,
            "consistency_score":    round(consistency_score, 3),
            "timing_cv":            round(timing_cv, 3),
        }
        if median_pass is not None:
            suggestion["TIME_PASS_FLOOR"] = round(median_pass, 1)
        if floor_waits:
            suggestion["FLOOR_WAITS"] = floor_waits

        return suggestion

    def is_safe_to_auto_apply(self) -> bool:
        """
        True when all safety thresholds for automatic config update are met:
        - At least MIN_CYCLES_AUTO cycles accumulated
        - 100% stop-set consistency across every cycle
        - Timing coefficient of variation ≤ TIMING_CV_MAX
        """
        n = len(self._records)
        if n < self.MIN_CYCLES_AUTO:
            return False

        # Identical stop sets across all cycles
        if len({frozenset(r["up_stops"])   for r in self._records}) > 1:
            return False
        if len({frozenset(r["down_stops"]) for r in self._records}) > 1:
            return False

        # Timing spread check (all stop-floor dwells)
        all_dwells: list[float] = []
        for rec in self._records:
            for direction in ("up", "down"):
                stops = set(rec[f"{direction}_stops"])
                dwells = rec[f"{direction}_dwells"]
                for f in stops:
                    d = dwells.get(f)
                    if d is not None:
                        all_dwells.append(d)

        cv = _cv(all_dwells)
        if cv > self.TIMING_CV_MAX:
            log.debug(
                "AutoLearner: CV=%.3f > %.3f — not safe to auto-apply", cv, self.TIMING_CV_MAX
            )
            return False

        return True

    # ── Serialization ────────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "records":   self._records,
            "first_ts":  self._first_ts,
            "last_ts":   self._last_ts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AutoLearner":
        learner = cls()
        learner._records  = d.get("records", [])
        learner._first_ts = d.get("first_ts")
        learner._last_ts  = d.get("last_ts")
        return learner
