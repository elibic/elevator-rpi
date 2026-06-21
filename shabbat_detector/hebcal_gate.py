"""
Optional Hebcal safety gate.

When HEBCAL_GATE_ENABLED is true in /settings, the detector only allows
Shabbat-mode entry if the current time is within a configurable window
around actual Shabbat / Yom-Tov times.

This does NOT drive Shabbat detection — it only prevents false-positive
entry during random weekday maintenance that happens to mimic the pattern.

Mirrors the Hebcal logic in kiosk-logic.js:415-518 but as a Python helper.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

_CACHE_TTL_S = 600        # re-fetch Hebcal at most every 10 minutes
_HEBCAL_API = "https://www.hebcal.com/shabbat"
_DEFAULT_GEO = "281184"   # Jerusalem (Ramada)


class HebcalGate:
    def __init__(self, firebase_client=None):
        self._fb = firebase_client
        self._candles_ts: Optional[float] = None
        self._havdalah_ts: Optional[float] = None
        self._last_fetch: float = 0.0
        self._geo: str = _DEFAULT_GEO

    def is_in_window(self, settings: dict, now: Optional[float] = None) -> bool:
        """
        Returns True if `now` falls within the Hebcal safety window.
        window = [candle_lighting - BEFORE_MIN, havdalah + AFTER_MIN]

        If Hebcal cannot be reached, returns True (fail-open: allows detection).
        """
        if now is None:
            now = time.time()

        before_min = float(settings.get("HEBCAL_GATE_WINDOW_BEFORE_MINUTES", 240))
        after_min = float(settings.get("HEBCAL_GATE_WINDOW_AFTER_MINUTES", 120))
        geo = str(settings.get("GEO_NAME_ID", _DEFAULT_GEO))

        self._maybe_refresh(geo, now)

        if self._candles_ts is None or self._havdalah_ts is None:
            log.warning("Hebcal data unavailable — gate open (allowing detection)")
            return True

        window_start = self._candles_ts - before_min * 60
        window_end = self._havdalah_ts + after_min * 60

        in_window = window_start <= now <= window_end
        if not in_window:
            log.debug(
                "Hebcal gate: %.0f not in [%.0f, %.0f]",
                now, window_start, window_end,
            )
        return in_window

    # ── Internal ───────────────────────────────────────────────────────────────

    def _maybe_refresh(self, geo: str, now: float) -> None:
        if now - self._last_fetch < _CACHE_TTL_S and geo == self._geo:
            return
        self._geo = geo
        self._last_fetch = now
        try:
            self._fetch(geo)
        except Exception as e:
            log.warning("Hebcal fetch failed: %s", e)

    def _fetch(self, geo: str) -> None:
        params = {
            "cfg": "json",
            "geonameid": geo,
            "m": 50,
            "lg": "h",
        }
        r = requests.get(_HEBCAL_API, params=params, timeout=10)
        r.raise_for_status()
        items = r.json().get("items", [])

        candles_ts = None
        havdalah_ts = None

        from datetime import datetime, timezone
        import re

        for item in items:
            cat = item.get("category", "")
            date_str = item.get("date", "")
            try:
                # Parse ISO-8601; Firebase/Hebcal returns "2026-04-25T19:30:00+03:00"
                dt = datetime.fromisoformat(date_str)
                ts = dt.timestamp()
            except Exception:
                continue

            if cat == "candles":
                candles_ts = ts
            elif cat == "havdalah":
                havdalah_ts = ts

        if candles_ts:
            self._candles_ts = candles_ts
        if havdalah_ts:
            self._havdalah_ts = havdalah_ts

        log.info(
            "Hebcal refreshed: candles=%s havdalah=%s",
            self._candles_ts, self._havdalah_ts,
        )
