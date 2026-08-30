"""
Optional Hebcal safety gate.

When HEBCAL_GATE_ENABLED is true in /settings, the detector only allows
Shabbat-mode entry while `now` is inside a configurable window around the real
Shabbat / Yom-Tov times, and the post-window exit paths (fsm Mechanisms 4-5)
arm only once that window has closed.

This does NOT drive Shabbat detection - it prevents false-positive entry during
weekday maintenance that happens to mimic the pattern, and it tells the exit
paths when the halachic day is over.

Contract: fail-OPEN.  Whenever the gate cannot say anything (Hebcal
unreachable, nothing fetched yet, stored data too old) it reports "in window",
so a network problem can only ever SUPPRESS an exit - never cause one.

The gate keeps LISTS of starts/ends and answers from the UNION of the
intervals they form.  Two field bugs made that necessary:

1. Multi-day Yom Tov (Rosh Hashana, Yom Tov adjacent to Shabbat) is a chain of
   candle-lightings under a single havdalah.  The old code kept one candles and
   one havdalah timestamp - the LAST of each - which moved the window start to
   the last day of the chain and left the earlier days OUTSIDE the window.
2. YOM_TOV_SHENI was not honoured here at all, so on a diaspora-only Yom Tov
   the window closed on the Israel calendar: entry was blocked and, with
   POST_WINDOW_HARD_EXIT_MIN set, the elevator was pushed out of Shabbat mode
   for the whole second day.

A union of intervals can only ever WIDEN the gate, so merging two calendars
cannot punch a hole in the middle.  That is the difference from
`schedule_windows`, which pairs starts and ends the way the web does: right for
the module that WRITES SHABBAT_ACTIVE, wrong for a fail-open gate.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

log = logging.getLogger(__name__)

_CACHE_TTL_S = 600           # re-fetch Hebcal at most every 10 minutes
_HEBCAL_API = "https://www.hebcal.com/shabbat"
_DEFAULT_GEO = "281184"      # Jerusalem (Ramada)
_NO_END_SAFETY_S = 26 * 3600  # start without a havdalah: assume <= 26h (web parity)
_MAX_DATA_AGE_S = 8 * 24 * 3600  # older than this cannot answer -> fail open


def _unwrap(v):
    """Config values may arrive as plain values or {value: ...} wrappers
    (setup.html / dashboard precedent).  Same rule as schedule_windows, kept
    local so the two modules stay independent."""
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    return v


def _num(settings: dict, key: str, default: float) -> float:
    """Numeric setting with the default kept for null/garbage.  A bad value
    written from the dashboard must not raise here: is_in_window runs inside
    the watchdog tick, where an exception would silently kill the loop."""
    try:
        v = _unwrap((settings or {}).get(key))
        return default if v is None else float(v)
    except (TypeError, ValueError):
        log.warning("Bad %s=%r - using %s", key, (settings or {}).get(key), default)
        return default


class HebcalGate:
    def __init__(self, firebase_client=None):
        self._fb = firebase_client
        self._starts: list[float] = []   # candle lighting + yom-tov day starts
        self._ends: list[float] = []     # havdalah
        self._fetched_at: float = 0.0    # epoch s of the last SUCCESSFUL fetch
        self._last_attempt: float = 0.0  # epoch s of the last fetch attempt
        self._geo: str = _DEFAULT_GEO
        self._diaspora: bool = True

    def is_in_window(self, settings: dict, now: Optional[float] = None) -> bool:
        """
        Returns True if `now` falls inside any Hebcal safety window:
            [start - BEFORE_MIN, end + AFTER_MIN]

        If Hebcal cannot be reached, returns True (fail-open: allows detection).
        """
        if now is None:
            now = time.time()

        before_min = _num(settings, "HEBCAL_GATE_WINDOW_BEFORE_MINUTES", 240)
        after_min = _num(settings, "HEBCAL_GATE_WINDOW_AFTER_MINUTES", 120)
        geo = str(_unwrap((settings or {}).get("GEO_NAME_ID")) or _DEFAULT_GEO)
        # Web semantics (kiosk-logic.js): YOM_TOV_SHENI !== false => enabled.
        diaspora = _unwrap((settings or {}).get("YOM_TOV_SHENI")) is not False

        self._maybe_refresh(geo, diaspora, now)

        if not self._starts:
            log.warning("Hebcal data unavailable - gate open (allowing detection)")
            return True
        if self._fetched_at and (now - self._fetched_at) > _MAX_DATA_AGE_S:
            log.warning(
                "Hebcal data is %.1f days old - gate open (allowing detection)",
                (now - self._fetched_at) / 86400,
            )
            return True

        for start, end in self._intervals(before_min * 60, after_min * 60):
            if start <= now <= end:
                return True

        log.debug("Hebcal gate: %.0f is outside every window", now)
        return False

    # ── Internal ───────────────────────────────────────────────────────────────

    def _intervals(self, before_s: float, after_s: float) -> list[tuple[float, float]]:
        """One interval per start, closed by the earliest end after it (or the
        26h safety cap when the fetch carried no matching havdalah)."""
        ends = sorted(self._ends)
        out: list[tuple[float, float]] = []
        for start in sorted(self._starts):
            end = next((e for e in ends if e > start), None)
            if end is None:
                end = start + _NO_END_SAFETY_S
            out.append((start - before_s, end + after_s))
        return out

    def _maybe_refresh(self, geo: str, diaspora: bool, now: float) -> None:
        if geo != self._geo or diaspora != self._diaspora:
            # Location or calendar changed - refetch now, but KEEP the stored
            # lists until fresh data replaces them.
            self._geo = geo
            self._diaspora = diaspora
        elif now - self._last_attempt < _CACHE_TTL_S:
            return

        self._last_attempt = now
        try:
            starts, ends = self._fetch(geo, diaspora, now)
        except Exception as e:
            log.warning("Hebcal fetch failed: %s (keeping stored windows)", e)
            return

        if not starts and not ends:
            log.warning("Hebcal fetch returned no items (keeping stored windows)")
            return

        self._starts = sorted(set(starts))
        self._ends = sorted(set(ends))
        self._fetched_at = now
        log.info(
            "Hebcal refreshed: %d start(s), %d end(s) (geo=%s, diaspora=%s)",
            len(self._starts), len(self._ends), geo, diaspora,
        )

    def _fetch(self, geo: str, diaspora: bool, now: float) -> tuple[list[float], list[float]]:
        # Anchor the query one day back, like the web and schedule_windows: an
        # un-anchored query returns the NEXT Shabbat, which on the Sunday of a
        # Yom Tov Sheni drops the very chain we are standing in.
        anchor = datetime.fromtimestamp(now) - timedelta(days=1)
        base_params = {
            "cfg": "json",
            "m": 50,
            "lg": "h",
            "tzid": "Asia/Jerusalem",
            "gy": str(anchor.year),
            "gm": str(anchor.month),
            "gd": str(anchor.day),
        }

        starts: list[float] = []
        ends: list[float] = []

        # Primary: the configured location (Israel calendar).
        params = dict(base_params)
        params["geonameid"] = geo
        self._parse_items(self._get_items(params), starts, ends)

        # Secondary: the Diaspora calendar (Yom Tov Sheni).  Tolerated failure -
        # web parity (respDiaspora.ok), and the union means a miss can only
        # narrow the gate back to the Israel calendar.
        if diaspora:
            try:
                params = dict(base_params)
                params["i"] = "off"
                self._parse_items(self._get_items(params), starts, ends)
            except Exception as e:
                log.warning("Diaspora Hebcal fetch failed (ignored): %s", e)

        return starts, ends

    @staticmethod
    def _get_items(params: dict) -> list:
        r = requests.get(_HEBCAL_API, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("items", []) or []

    @staticmethod
    def _parse_items(items: list, starts: list[float], ends: list[float]) -> None:
        # Web parity (checkWindows): starts = candles + yom-tov holidays,
        # ends = havdalah.  A holiday item carries a date-only string, which
        # resolves in the Pi's local tz (Israel) as the start of that day.
        for item in items:
            cat = item.get("category", "")
            if cat == "candles" or (cat == "holiday" and item.get("yomtov")):
                target = starts
            elif cat == "havdalah":
                target = ends
            else:
                continue
            try:
                # ISO-8601, e.g. "2026-04-25T19:30:00+03:00"
                ts = datetime.fromisoformat(item.get("date", "")).timestamp()
            except Exception:
                continue
            target.append(ts)
