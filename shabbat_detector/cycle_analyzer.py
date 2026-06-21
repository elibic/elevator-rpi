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

# How long with no movement before we abandon a partial cycle (seconds).
IDLE_RESET_SECONDS = 300


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
        """Call whenever the elevator config changes in Firebase."""
        self.top_floor = str(config.get("TOP_FLOOR", self.top_floor))
        self.bottom_floor = str(config.get("BOTTOM_FLOOR", self.bottom_floor))
        self.time_per_floor = float(config.get("TIME_PER_FLOOR", self.time_per_floor))
        raw_waits = config.get("FLOOR_WAITS") or {}
        self.floor_waits = {str(k): float(v) for k, v in raw_waits.items()}
        self._stop_threshold = self.time_per_floor * 0.5
        self._floor_order = self._build_floor_order()
        # Abandon any in-progress cycle; config change may invalidate it
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
        if gap > IDLE_RESET_SECONDS and self._phase != _Phase.WAITING:
            log.info(
                "Elevator idle %.0fs — resetting cycle (was in %s)", gap, self._phase
            )
            self._reset()

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
                # Bounced back — abort and restart the cycle at this terminal
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
