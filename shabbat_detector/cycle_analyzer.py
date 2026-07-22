"""
Processes a stream of floor-change events from the RFID tracker and
emits completed Cycle objects when the elevator finishes a full
round trip between the terminal floors.

The RPi only reports floor *changes*, so dwell at floor F is inferred
as  timestamp[next_event] − timestamp[F_event].
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)

# Floor for the idle-abandon gap (seconds).  _idle_reset_seconds() lifts this
# above the longest configured dwell so a legitimate long hold is never treated
# as idle (#15).
IDLE_RESET_SECONDS = 300

# How many floors short of the far terminal still counts as "reached it", so a
# single missed terminal-tag read does not discard an otherwise-full cycle (#14).
TERMINAL_MISS_TOLERANCE = 1


def normalize_floor_waits(raw) -> dict:
    """FLOOR_WAITS אמור להיות dict {קומה: שניות}. אבל Firebase RTDB ממיר מפתחות
    שלמים רצופים שמתחילים מ-0 (0,1,2...) ל-**list** [שניות0, שניות1, ...] (האינדקס =
    הקומה), ומוסיף חורי-null כשהמפתחות דלילים/לא-מ-0. מנרמל תמיד ל-dict {str(קומה): ערך}
    ומדלג על null. בלי זה, קוד שקורא `.items()` קורס: 'list' object has no attribute 'items'
    (קומות 0..N שלמות ⇒ RTDB מחזיר list ⇒ קריסת ה-detector בלולאה)."""
    if isinstance(raw, list):
        return {str(i): v for i, v in enumerate(raw) if v is not None}
    if isinstance(raw, dict):
        return raw
    return {}


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class FloorEvent:
    floor: str          # e.g. "-3", "0", "12"
    timestamp: float    # Unix seconds (Firebase server time)
    direction: Optional[str] = None   # "up"/"down"/"stopped"/None (optional hint)


@dataclass
class StopRecord:
    """Describes the elevator's stay at one floor."""
    floor: str
    arrival_ts: float
    dwell_s: float      # seconds before moving to the next floor
    is_stop: bool       # dwell >= stop_threshold → considered a real stop


@dataclass
class Cycle:
    """One complete round trip between the two terminal floors."""
    start_terminal: str         # "BOTTOM" or "TOP"
    start_ts: float
    end_ts: float
    # Floors where elevator dwelled ≥ stop_threshold (excluding terminals)
    up_stops: list[str]
    down_stops: list[str]
    # Floors that were passed quickly (excluding terminals)
    up_passes: list[str]
    down_passes: list[str]
    # dwell_s for every floor in this cycle, including terminals
    up_dwells: dict[str, float]
    down_dwells: dict[str, float]

    @property
    def duration_s(self) -> float:
        return self.end_ts - self.start_ts


@dataclass
class AnalyzerResult:
    """What CycleAnalyzer.push_event() computed for the latest event."""
    completed_cycle: Optional[Cycle] = None
    cycle_just_started: bool = False
    prev_stop_record: Optional[StopRecord] = None  # for violation checking


# ── Internal state ─────────────────────────────────────────────────────────────

class _Phase(str, Enum):
    WAITING = "WAITING"     # waiting for elevator to first reach a terminal
    LEG_ONE = "LEG_ONE"     # first half: from start_terminal toward opposite
    LEG_TWO = "LEG_TWO"     # second half: returning to start_terminal


# ── CycleAnalyzer ──────────────────────────────────────────────────────────────

class CycleAnalyzer:
    """
    Feed FloorEvent objects one by one via push_event().
    When the elevator completes a round trip, returns a Cycle in AnalyzerResult.
    Thread-unsafe; run from a single loop.
    """

    def __init__(
        self,
        top_floor: str,
        bottom_floor: str,
        time_per_floor: float,
        floor_waits: Optional[dict[str, float]] = None,
    ):
        self.top_floor = top_floor
        self.bottom_floor = bottom_floor
        self.time_per_floor = time_per_floor
        self.floor_waits: dict[str, float] = floor_waits or {}

        self._stop_threshold = time_per_floor * 0.5
        self._floor_order: list[str] = self._build_floor_order()

        # Cycle-tracking state
        self._phase = _Phase.WAITING
        self._start_terminal: Optional[str] = None
        self._start_ts: Optional[float] = None
        self._leg1: list[StopRecord] = []
        self._leg2: list[StopRecord] = []

        # Rolling window of recent events (for direction inference + dwell calc)
        self._prev_event: Optional[FloorEvent] = None

    # ── Public ─────────────────────────────────────────────────────────────────

    def update_config(self, config: dict) -> None:
        """Call whenever the elevator config changes in Firebase.

        Only the geometry/timing fields below affect cycle detection.  We reset
        the in-progress cycle ONLY when one of them actually changed - otherwise
        an unrelated config write (e.g. the detector's own SHABBAT_DETECTOR echo
        coming back over the SSE stream) would abandon a perfectly good cycle
        mid-flight and silently drop it (no abort/idle log), causing phantom
        "no-cycle" gaps that flap the FSM out of SHABBAT.  Defense-in-depth:
        the caller (detector.on_config_update) also guards this call.
        """
        new_top = str(config.get("TOP_FLOOR", self.top_floor))
        new_bottom = str(config.get("BOTTOM_FLOOR", self.bottom_floor))
        new_tpf = float(config.get("TIME_PER_FLOOR", self.time_per_floor))
        # Absent FLOOR_WAITS means "unchanged" (mirrors the .get(x, self.x) used
        # for the other fields), so a partial echo can't look like a change.
        if "FLOOR_WAITS" in config:
            new_waits = {str(k): float(v) for k, v in normalize_floor_waits(config.get("FLOOR_WAITS")).items()}
        else:
            new_waits = self.floor_waits

        changed = (
            new_top != self.top_floor
            or new_bottom != self.bottom_floor
            or new_tpf != self.time_per_floor
            or new_waits != self.floor_waits
        )

        self.top_floor = new_top
        self.bottom_floor = new_bottom
        self.time_per_floor = new_tpf
        self.floor_waits = new_waits
        self._stop_threshold = self.time_per_floor * 0.5
        self._floor_order = self._build_floor_order()

        # Abandon any in-progress cycle only when the geometry/timing truly changed.
        if changed:
            self._reset()

    def push_event(self, event: FloorEvent) -> AnalyzerResult:
        """
        Process one floor-change event.  Returns an AnalyzerResult that may
        contain a completed Cycle, a cycle-started flag, and/or the stop record
        for the *previous* floor (useful for mid-cycle violation detection).
        """
        result = AnalyzerResult()

        if self._prev_event is None:
            self._prev_event = event
            if self._phase == _Phase.WAITING:
                term = self._terminal(event.floor)
                if term:
                    self._start_cycle(term, event.timestamp)
                    result.cycle_just_started = True
            return result

        prev = self._prev_event
        gap = event.timestamp - prev.timestamp

        # Idle gap mid-cycle → abandon
        if gap > self._idle_reset_seconds() and self._phase != _Phase.WAITING:
            log.info(
                "Elevator idle %.0fs - resetting cycle (was in %s)", gap, self._phase
            )
            self._reset()
            # If it was parked AT a terminal, re-arm a fresh cycle there (from the
            # moment it leaves) so the upcoming leg is captured instead of waiting
            # for the next terminal touch and dropping a half-cycle (#15).
            prev_term = self._terminal(prev.floor)
            if prev_term:
                self._start_cycle(prev_term, event.timestamp)

        # Compute dwell of the *previous* floor (we now know when it left)
        dwell = max(0.0, gap)
        is_stop = dwell >= self._stop_threshold
        prev_record = StopRecord(
            floor=prev.floor,
            arrival_ts=prev.timestamp,
            dwell_s=dwell,
            is_stop=is_stop,
        )
        result.prev_stop_record = prev_record

        self._prev_event = event

        # Advance the cycle state machine
        completed, started = self._advance(prev_record, event)
        result.completed_cycle = completed
        result.cycle_just_started = started

        return result

    def reset(self) -> None:
        """Explicitly abandon the current cycle (e.g. on SHABBAT exit)."""
        self._reset()

    # ── Internal ───────────────────────────────────────────────────────────────

    def _build_floor_order(self) -> list[str]:
        try:
            lo, hi = int(self.bottom_floor), int(self.top_floor)
            return [str(i) for i in range(lo, hi + 1)]
        except ValueError:
            return [self.bottom_floor, self.top_floor]

    def _floor_idx(self, floor: str) -> int:
        try:
            return self._floor_order.index(floor)
        except ValueError:
            try:
                return int(floor)
            except ValueError:
                return 0

    def _terminal(self, floor: str) -> Optional[str]:
        if floor == self.bottom_floor:
            return "BOTTOM"
        if floor == self.top_floor:
            return "TOP"
        return None

    def _opposite(self, terminal: str) -> str:
        return "TOP" if terminal == "BOTTOM" else "BOTTOM"

    def _terminal_floor(self, terminal: str) -> str:
        return self.bottom_floor if terminal == "BOTTOM" else self.top_floor

    def _idle_reset_seconds(self) -> float:
        """Idle gap that abandons a partial cycle, kept safely above the longest
        legitimate dwell (a long FLOOR_WAITS hold or the terminal park) so a
        normal hold is never treated as idle - derived from config, not a bare
        constant (#15)."""
        longest_dwell = self.time_per_floor
        if self.floor_waits:
            longest_dwell = max(longest_dwell, max(self.floor_waits.values()))
        return max(float(IDLE_RESET_SECONDS), 4.0 * longest_dwell)

    def _try_complete_missed_apex(
        self, prev_record: StopRecord, curr_event: FloorEvent
    ) -> Optional[Cycle]:
        """LEG_ONE bounced back to the start terminal.  If the leg reached within
        TERMINAL_MISS_TOLERANCE floors of the far terminal, treat it as a full
        round trip whose apex read was missed: split the merged leg at its apex
        and build the cycle.  Returns the Cycle, or None for a genuine partial
        trip that should abort (#14)."""
        records = self._leg1 + [prev_record]
        try:
            start_idx = int(self._terminal_floor(self._start_terminal))
            opp_idx = int(self._terminal_floor(self._opposite(self._start_terminal)))
        except (TypeError, ValueError):
            return None

        def dist(floor: str) -> Optional[int]:
            try:
                return abs(int(floor) - start_idx)
            except (TypeError, ValueError):
                return None

        dists = [(i, dist(r.floor)) for i, r in enumerate(records)]
        dists = [(i, d) for i, d in dists if d is not None]
        if not dists:
            return None
        apex_dist = max(d for _, d in dists)
        if abs(opp_idx - start_idx) - apex_dist > TERMINAL_MISS_TOLERANCE:
            return None   # turned around too early - a real partial trip

        apex_pos = next(i for i, d in dists if d == apex_dist)   # first apex record
        self._leg1 = records[: apex_pos + 1]
        self._leg2 = records[apex_pos + 1:]
        cycle = self._build_cycle(curr_event.timestamp)
        log.info(
            "Cycle recovered (missed %s-terminal read): %.0fs, up_stops=%s, down_stops=%s",
            self._opposite(self._start_terminal), cycle.duration_s,
            cycle.up_stops, cycle.down_stops,
        )
        self._start_cycle(self._start_terminal, curr_event.timestamp)
        return cycle

    def _reset(self) -> None:
        self._phase = _Phase.WAITING
        self._start_terminal = None
        self._start_ts = None
        self._leg1 = []
        self._leg2 = []

    def _start_cycle(self, terminal: str, ts: float) -> None:
        self._phase = _Phase.LEG_ONE
        self._start_terminal = terminal
        self._start_ts = ts
        self._leg1 = []
        self._leg2 = []

    def _advance(
        self, prev_record: StopRecord, curr_event: FloorEvent
    ) -> tuple[Optional[Cycle], bool]:
        """
        Returns (completed_cycle_or_None, cycle_just_started).
        """
        curr_floor = curr_event.floor
        curr_terminal = self._terminal(curr_floor)

        if self._phase == _Phase.WAITING:
            if curr_terminal:
                self._start_cycle(curr_terminal, curr_event.timestamp)
                log.debug("Cycle started at %s (%s)", curr_terminal, curr_floor)
                return None, True
            return None, False

        if self._phase == _Phase.LEG_ONE:
            opposite = self._opposite(self._start_terminal)

            if curr_terminal == opposite:
                # Reached the opposite terminal → end of first leg
                self._leg1.append(prev_record)
                self._phase = _Phase.LEG_TWO
                log.debug("Leg 1 complete at %s (%s)", curr_terminal, curr_floor)
                return None, False

            if curr_terminal == self._start_terminal:
                # Returned to start without registering the far terminal.  If the
                # leg got within TERMINAL_MISS_TOLERANCE floors of it, this was a
                # full round trip whose apex tag read was simply missed - recover
                # it instead of discarding an otherwise-complete cycle (#14).
                recovered = self._try_complete_missed_apex(prev_record, curr_event)
                if recovered is not None:
                    return recovered, True
                # Genuine partial trip (turned around far from the far terminal).
                log.info(
                    "Cycle aborted: returned to %s without reaching %s",
                    self._start_terminal, opposite,
                )
                self._start_cycle(self._start_terminal, curr_event.timestamp)
                return None, True

            # Mid-leg floor
            self._leg1.append(prev_record)
            return None, False

        if self._phase == _Phase.LEG_TWO:
            if curr_terminal == self._start_terminal:
                # Full round trip complete!
                self._leg2.append(prev_record)
                cycle = self._build_cycle(curr_event.timestamp)
                log.info(
                    "Cycle complete: %.0fs, up_stops=%s, down_stops=%s",
                    cycle.duration_s,
                    cycle.up_stops,
                    cycle.down_stops,
                )
                # Immediately start a new cycle from this terminal
                self._start_cycle(self._start_terminal, curr_event.timestamp)
                return cycle, True

            if curr_terminal == self._opposite(self._start_terminal):
                # Touched the opposite terminal again mid leg-2 (unusual)
                log.warning("Unexpected: touched opposite terminal again in leg 2")
                self._leg2.append(prev_record)
                return None, False

            self._leg2.append(prev_record)
            return None, False

        return None, False

    def _build_cycle(self, end_ts: float) -> Cycle:
        # Assign legs to directions based on which terminal started the cycle
        if self._start_terminal == "BOTTOM":
            up_records, down_records = self._leg1, self._leg2
        else:
            up_records, down_records = self._leg2, self._leg1

        def extract(records: list[StopRecord]):
            stops, passes, dwells = [], [], {}
            for r in records:
                if self._terminal(r.floor) is None:   # exclude terminals from stops/passes
                    if r.is_stop:
                        stops.append(r.floor)
                    else:
                        passes.append(r.floor)
                dwells[r.floor] = r.dwell_s           # include terminals in dwells
            return stops, passes, dwells

        us, up_p, ud = extract(up_records)
        ds, dp, dd = extract(down_records)

        return Cycle(
            start_terminal=self._start_terminal,
            start_ts=self._start_ts,
            end_ts=end_ts,
            up_stops=us,
            down_stops=ds,
            up_passes=up_p,
            down_passes=dp,
            up_dwells=ud,
            down_dwells=dd,
        )
