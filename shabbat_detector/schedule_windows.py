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
# Hebcal FORCES i=on whenever it can tell the location is in Israel, and it
# decides that from the TZID: geonameid=281184 and even raw Jerusalem
# coordinates with tzid=Asia/Jerusalem both come back with the Israel scheme
# and "i=on" echoed in every item link (verified against the live API,
# 2026-08-31).  The Diaspora scheme is only served for a location whose tzid is
# not Israel's - hence geo=pos with the SAME coordinates and a stand-in tzid.
#
# This does not move any time.  Hebcal derives candle lighting and havdalah
# from the latitude/longitude, then merely RENDERS them in the given tzid, and
# every timestamp carries its UTC offset ("2026-09-27T19:06:00+03:00") which we
# always parse.  Measured on Sukkot 5787: the Israel fetch put havdalah at
# 2026-09-26T19:07:00+03:00 and the geo=pos fetch put the second day's candle
# lighting at the very same instant.  Athens is the stand-in because it shares
# Israel's UTC offset in all but a couple of days each spring, so logs stay
# readable; correctness does not depend on that.
_DIASPORA_TZID = "Europe/Athens"


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


def diaspora_params(base_params: dict, israel_response: dict) -> dict:
    """Params for the Diaspora-scheme fetch at the primary response's own
    coordinates.  Raises when the response carries no usable location, so the
    caller's tolerated-failure path skips the second calendar rather than
    silently querying somewhere else."""
    loc = (israel_response or {}).get("location") or {}
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if lat is None or lon is None:
        raise ValueError("Hebcal response carried no latitude/longitude")
    params = dict(base_params)
    params.update({
        "geo": "pos",
        "latitude": str(lat),
        "longitude": str(lon),
        "tzid": _DIASPORA_TZID,
        "i": "off",
    })
    return params


class ScheduleWindows:
    """Fetches, caches and persists Shabbat/Yom-Tov window lists from Hebcal."""

    def __init__(self):
        self._starts: list[float] = []   # candle-lighting / yomtov starts (epoch s)
        self._ends: list[float] = []     # havdalah ends (epoch s)
        # The Diaspora calendar is kept SEPARATE, never merged into the lists
        # above.  Merging looked harmless but truncated the chain: on the
        # motzaei Shabbat of a Yom Tov Sheni the Israel havdalah lands in the
        # shared end list, becomes the "earliest end after the active start",
        # and closes a window that the Diaspora calendar keeps open - a ~2 hour
        # hole in the middle of the second night (reproduced against the live
        # Hebcal payloads for Sukkot 5787).  Evaluating each calendar on its
        # own and OR-ing the answers is what the web does, and it cannot hole.
        self._d_starts: list[float] = []
        self._d_ends: list[float] = []
        self._fetched_at: float = 0.0    # epoch s of last SUCCESSFUL fetch
        self._last_attempt: float = 0.0  # epoch s of last fetch attempt
        self._geo: str = ""
        self._diaspora: bool = True

    # ── Persistence (rides the detector's state file) ─────────────────────────

    def to_dict(self) -> dict:
        return {
            "starts": list(self._starts),
            "ends": list(self._ends),
            "d_starts": list(self._d_starts),
            "d_ends": list(self._d_ends),
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
            # Absent in state files written before the split - an older file
            # simply restores as "Israel only" and the next fetch fills it in.
            inst._d_starts = sorted(float(x) for x in (d.get("d_starts") or []))
            inst._d_ends = sorted(float(x) for x in (d.get("d_ends") or []))
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
            starts, ends, d_starts, d_ends = self._fetch(geo, diaspora, now)
        except Exception as e:
            log.warning("Schedule windows fetch failed: %s (keeping stored windows)", e)
            return False

        if not starts and not ends:
            log.warning("Schedule windows fetch returned no items (keeping stored windows)")
            return False

        self._starts = sorted(set(starts))
        self._ends = sorted(set(ends))
        self._d_starts = sorted(set(d_starts))
        self._d_ends = sorted(set(d_ends))
        self._fetched_at = now
        log.info(
            "Schedule windows refreshed: %d/%d Israel, %d/%d Diaspora "
            "start(s)/end(s) (geo=%s, diaspora=%s)",
            len(self._starts), len(self._ends),
            len(self._d_starts), len(self._d_ends), geo, diaspora,
        )
        return True

    def _fetch(
        self, geo: str, diaspora: bool, now: float
    ) -> tuple[list[float], list[float], list[float], list[float]]:
        # Anchor the query one day back (web parity: catches a window that
        # already started yesterday).
        anchor = datetime.fromtimestamp(now) - timedelta(days=1)
        base_params = {
            "cfg": "json",
            "M": "on",
            "b": "1",
            "gy": str(anchor.year),
            "gm": str(anchor.month),
            "gd": str(anchor.day),
        }

        starts: list[float] = []
        ends: list[float] = []
        d_starts: list[float] = []
        d_ends: list[float] = []

        # Primary fetch: Israel calendar for the configured location.
        params = dict(base_params)
        params["geonameid"] = geo
        params["tzid"] = "Asia/Jerusalem"
        israel = self._get_json(params)
        self._parse_items(israel.get("items") or [], starts, ends)

        # Secondary fetch: the Diaspora HOLIDAY SCHEME at the SAME coordinates,
        # taken from the primary response.  Tolerated failure - web parity
        # (respDiaspora.ok check).
        if diaspora:
            try:
                self._parse_items(
                    self._get_json(diaspora_params(base_params, israel)).get("items") or [],
                    d_starts, d_ends,
                )
            except Exception as e:
                log.warning("Diaspora schedule fetch failed (ignored): %s", e)

        return starts, ends, d_starts, d_ends

    @staticmethod
    def _get_json(params: dict) -> dict:
        r = requests.get(_HEBCAL_API, params=params, timeout=10)
        r.raise_for_status()
        return r.json() or {}

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

        Each calendar is evaluated on its own and the answers are OR-ed, which
        is exactly what the web does (checkWindows(israel) || checkWindows
        (diaspora)).  Never merge the two lists: see the note on _d_starts.

        Returns None ("unknown") when there is no usable data at all: nothing
        was ever fetched/persisted, or the data is too old to say anything
        about `now`.  The caller must HOLD the last written state on None -
        never flip on missing data.
        """
        if self._fetched_at and (now - self._fetched_at) > _MAX_DATA_AGE_S:
            return None

        israel = self._check(self._starts, self._ends, now, before_min, after_min)
        if israel:
            return True
        diaspora = self._check(self._d_starts, self._d_ends, now, before_min, after_min)
        if diaspora:
            return True
        if israel is None and diaspora is None:
            return None
        return False

    @staticmethod
    def _check(
        starts: list[float],
        ends: list[float],
        now: float,
        before_min: float,
        after_min: float,
    ) -> Optional[bool]:
        """Exact port of the web checkWindows() pairing, with configurable
        offsets:
        - adjusted start = start - before_min, adjusted end = end + after_min
        - activeStart = the LATEST adjusted start <= now (none => False)
        - relevantEnd = the EARLIEST adjusted end > activeStart
          (found => now <= relevantEnd; missing => 26h safety)

        None means this calendar holds no data to answer with.
        """
        if not starts and not ends:
            return None

        adj_starts = [s - before_min * 60 for s in starts]
        adj_ends = [e + after_min * 60 for e in ends]

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
