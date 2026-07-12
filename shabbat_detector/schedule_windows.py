"""
Schedule-driven Shabbat windows (SHABBAT_SOURCE='schedule').

Some buildings have a FIXED set of Shabbat elevators that always switch on a
known schedule - they do not need behavioral detection at all.  For those,
the detector can drive SHABBAT_ACTIVE purely from the Hebcal calendar:

    window = [candle_lighting - SHABBAT_SCHEDULE_BEFORE_MINUTES,
              havdalah      + SHABBAT_SCHEDULE_AFTER_MINUTES]

This module is deliberately SEPARATE from HebcalGate:
- HebcalGate is a fuzzy safety gate for the behavioral FSM - fail-OPEN and a
  single candles/havdalah pair is fine there.
- The schedule engine is the actual writer of SHABBAT_ACTIVE - it must be
  fail-CLOSED (never flip on missing data) and must understand MULTI-window
  chains (multi-day Yom Tov, Yom Tov Sheni), so it keeps full start/end lists
  and persists them to disk between runs.

The window-pairing logic is an exact port of checkWindows() from the shared
web code (ramada-web monorepo/shared/public/kiosk-logic.js), with the
hardcoded 100/60-minute offsets replaced by the configurable settings.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

log = logging.getLogger(__name__)

_HEBCAL_API = "https://www.hebcal.com/shabbat"
_DEFAULT_GEO = "281184"          # Jerusalem

_REFRESH_OK_TTL_S = 6 * 3600     # successful fetch is fresh for 6 hours
_REFRESH_FAIL_RETRY_S = 600      # retry every 10 minutes after a failure
_NO_END_SAFETY_S = 26 * 3600     # window without havdalah: assume <= 26h (web parity)
_MAX_DATA_AGE_S = 8 * 24 * 3600  # data older than this cannot answer "is now in window"

# Default precise offsets - identical to the hardcoded browser fallback
# (kiosk-logic.js checkWindows: start -100min / end +60min), so an
# unconfigured schedule project can never disagree with the screens.
DEFAULT_BEFORE_MIN = 100.0
DEFAULT_AFTER_MIN = 60.0

VALID_SOURCES = ("auto", "schedule", "none")


def _unwrap(v):
    """Config values may arrive as plain values or {value: ...} wrappers
    (setup.html / dashboard precedent).  Return the plain value."""
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    return v


def resolve_source(el_config: Optional[dict], settings: Optional[dict]) -> str:
    """Resolution order: per-elevator SHABBAT_SOURCE -> project
    SHABBAT_SOURCE_DEFAULT -> 'auto'.  Unknown/junk values fall through."""
    v = _unwrap((el_config or {}).get("SHABBAT_SOURCE"))
    if v in VALID_SOURCES:
        return v
    v = _unwrap((settings or {}).get("SHABBAT_SOURCE_DEFAULT"))
    if v in VALID_SOURCES:
        return v
    return "auto"


def _minutes(settings: Optional[dict], key: str, default: float) -> float:
    v = _unwrap((settings or {}).get(key))
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float(default)
    if f < 0:
        return float(default)
    return f


def schedule_offsets(settings: Optional[dict]) -> tuple[float, float]:
    """(before_min, after_min) - the precise schedule offsets.  Separate from
    the deliberately-wide HEBCAL_GATE_WINDOW_* gate fields.  0 is a legal
    value (enter exactly at candle-lighting)."""
    return (
        _minutes(settings, "SHABBAT_SCHEDULE_BEFORE_MINUTES", DEFAULT_BEFORE_MIN),
        _minutes(settings, "SHABBAT_SCHEDULE_AFTER_MINUTES", DEFAULT_AFTER_MIN),
    )


def decide_write(
    desired: bool,
    current_active,
    last_written: Optional[bool],
    last_written_ts: float,
    now: float,
    grace_s: float = 120.0,
) -> bool:
    """Should the schedule tick PATCH SHABBAT_ACTIVE right now?

    - No write when the DB (SSE-mirrored cache) already agrees.
    - No re-write within `grace_s` of our own last write for the same value -
      the SSE echo may simply not have arrived yet.
    - After the grace, a mismatch IS rewritten - this is what self-heals a
      manual onoff.html write within one or two ticks.
    """
    if desired == bool(current_active):
        return False
    if desired == last_written and (now - last_written_ts) < grace_s:
        return False
    return True


class ScheduleWindows:
    """Fetches, caches and persists Shabbat/Yom-Tov window lists from Hebcal."""

    def __init__(self):
        self._starts: list[float] = []   # candle-lighting / yomtov starts (epoch s)
        self._ends: list[float] = []     # havdalah ends (epoch s)
        self._fetched_at: float = 0.0    # epoch s of last SUCCESSFUL fetch
        self._last_attempt: float = 0.0  # epoch s of last fetch attempt
        self._geo: str = ""
        self._diaspora: bool = True

    # ── Persistence (rides the detector's state file) ─────────────────────────

    def to_dict(self) -> dict:
        return {
            "starts": list(self._starts),
            "ends": list(self._ends),
            "fetched_at": self._fetched_at,
            "geo": self._geo,
            "diaspora": self._diaspora,
        }

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "ScheduleWindows":
        inst = cls()
        if not isinstance(d, dict):
            return inst
        try:
            inst._starts = sorted(float(x) for x in (d.get("starts") or []))
            inst._ends = sorted(float(x) for x in (d.get("ends") or []))
            inst._fetched_at = float(d.get("fetched_at") or 0.0)
            inst._geo = str(d.get("geo") or "")
            inst._diaspora = bool(d.get("diaspora", True))
        except Exception as e:
            log.warning("Could not restore schedule windows: %s - starting empty", e)
            return cls()
        return inst

    def invalidate(self) -> None:
        """Force a refetch on the next refresh (location / calendar changed).
        Existing lists are KEPT until fresh data replaces them - a wrong-by-
        minutes window beats no window at all."""
        self._fetched_at = 0.0
        self._last_attempt = 0.0

    # ── Refresh ────────────────────────────────────────────────────────────────

    def refresh_if_due(self, settings: Optional[dict], now: Optional[float] = None) -> bool:
        """Fetch from Hebcal when due.  Returns True if new data was stored.
        A failed fetch NEVER clears stored lists (fail-closed)."""
        if now is None:
            now = time.time()

        geo = str(_unwrap((settings or {}).get("GEO_NAME_ID")) or _DEFAULT_GEO)
        # Web semantics (kiosk-logic.js): YOM_TOV_SHENI !== false => enabled.
        diaspora = _unwrap((settings or {}).get("YOM_TOV_SHENI")) is not False
        if geo != self._geo or diaspora != self._diaspora:
            self._geo = geo
            self._diaspora = diaspora
            self.invalidate()

        if (now - self._fetched_at) < _REFRESH_OK_TTL_S:
            return False
        if (now - self._last_attempt) < _REFRESH_FAIL_RETRY_S:
            return False
        self._last_attempt = now

        try:
            starts, ends = self._fetch(geo, diaspora, now)
        except Exception as e:
            log.warning("Schedule windows fetch failed: %s (keeping stored windows)", e)
            return False

        if not starts and not ends:
            log.warning("Schedule windows fetch returned no items (keeping stored windows)")
            return False

        self._starts = sorted(set(starts))
        self._ends = sorted(set(ends))
        self._fetched_at = now
        log.info(
            "Schedule windows refreshed: %d start(s), %d end(s) (geo=%s, diaspora=%s)",
            len(self._starts), len(self._ends), geo, diaspora,
        )
        return True

    def _fetch(self, geo: str, diaspora: bool, now: float) -> tuple[list[float], list[float]]:
        # Anchor the query one day back (web parity: catches a window that
        # already started yesterday).
        anchor = datetime.fromtimestamp(now) - timedelta(days=1)
        base_params = {
            "cfg": "json",
            "M": "on",
            "b": "1",
            "tzid": "Asia/Jerusalem",
            "gy": str(anchor.year),
            "gm": str(anchor.month),
            "gd": str(anchor.day),
        }

        starts: list[float] = []
        ends: list[float] = []

        # Primary fetch: Israel calendar for the configured location.
        params = dict(base_params)
        params["geonameid"] = geo
        self._parse_items(self._get_items(params), starts, ends)

        # Secondary fetch: Diaspora calendar (Yom Tov Sheni).  Tolerated
        # failure - web parity (respDiaspora.ok check).
        if diaspora:
            try:
                params = dict(base_params)
                params["i"] = "off"
                self._parse_items(self._get_items(params), starts, ends)
            except Exception as e:
                log.warning("Diaspora schedule fetch failed (ignored): %s", e)

        return starts, ends

    @staticmethod
    def _get_items(params: dict) -> list:
        r = requests.get(_HEBCAL_API, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("items", []) or []

    @staticmethod
    def _parse_items(items: list, starts: list[float], ends: list[float]) -> None:
        # Web parity (checkWindows): starts = candles + yomtov holidays,
        # ends = havdalah.  Holiday items may carry a date-only string - the
        # naive datetime resolves in the Pi's local tz (Israel), i.e. the
        # start of that day, which is what the web intends.
        for item in items:
            cat = item.get("category", "")
            if cat == "candles" or (cat == "holiday" and item.get("yomtov")):
                target = starts
            elif cat == "havdalah":
                target = ends
            else:
                continue
            try:
                ts = datetime.fromisoformat(item.get("date", "")).timestamp()
            except Exception:
                continue
            target.append(ts)

    # ── Decision ──────────────────────────────────────────────────────────────

    def is_active(
        self,
        now: float,
        before_min: float,
        after_min: float,
    ) -> Optional[bool]:
        """Is `now` inside a schedule window?

        Exact port of the web checkWindows() pairing with configurable offsets:
        - adjusted start = start - before_min, adjusted end = end + after_min
        - activeStart = the LATEST adjusted start <= now (none => False)
        - relevantEnd = the EARLIEST adjusted end > activeStart
          (found => now <= relevantEnd; missing => 26h safety)

        Returns None ("unknown") when there is no usable data: nothing was
        ever fetched/persisted, or the data is too old to say anything about
        `now`.  The caller must HOLD the last written state on None - never
        flip on missing data.
        """
        if not self._starts and not self._ends:
            return None
        if self._fetched_at and (now - self._fetched_at) > _MAX_DATA_AGE_S:
            return None

        adj_starts = [s - before_min * 60 for s in self._starts]
        adj_ends = [e + after_min * 60 for e in self._ends]

        active_start = None
        for s in sorted(adj_starts):
            if s <= now:
                active_start = s
            else:
                break
        if active_start is None:
            return False

        relevant_end = None
        for e in sorted(adj_ends):
            if e > active_start:
                relevant_end = e
                break
        if relevant_end is not None:
            return now <= relevant_end
        return (now - active_start) < _NO_END_SAFETY_S
