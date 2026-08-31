"""Tests for the Hebcal safety gate (shabbat_detector.hebcal_gate).

Two field bugs are pinned here:

1. **Multi-day Yom Tov in Israel.**  Rosh Hashana 5787 is Sat 12/9 + Sun 13/9 -
   a chain of candle-lightings under one havdalah.  The gate used to keep the
   LAST candles and the LAST havdalah only, which moved the window start to the
   last day of the chain and left the earlier days outside the window: entry
   blocked on the first day, and with POST_WINDOW_HARD_EXIT_MIN set, an exit
   fired in the middle of the chag.

2. **Yom Tov Sheni.**  The gate never read YOM_TOV_SHENI, so on a diaspora-only
   second day (Sukkot II, Sun 27/9/2026) the Israel window had already closed on
   motzaei Shabbat.  Entry was blocked for the whole day and the hard exit
   pushed the elevator out of Shabbat mode at havdalah + AFTER + grace.

Everything else asserts the fail-open contract: whenever the gate cannot say
anything it must report "in window", so a network problem can only suppress an
exit, never cause one.
"""
from datetime import datetime

from shabbat_detector import hebcal_gate as hg
from shabbat_detector.hebcal_gate import HebcalGate


def ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().isoformat()


SETTINGS = {"GEO_NAME_ID": "281184"}          # YOM_TOV_SHENI absent => enabled
SETTINGS_NO_YTS = dict(SETTINGS, YOM_TOV_SHENI=False)

# ── Sukkot 5787: Fri 25/9 candles, Shabbat + Sukkot I on the 26th (Israel
# havdalah Sat night), Sukkot II on Sunday the 27th in the diaspora only.
FRI_CANDLES = ts("2026-09-25T18:15:00+03:00")
SAT_HAVDALAH = ts("2026-09-26T19:12:00+03:00")     # Israel calendar
SAT_CANDLES = ts("2026-09-26T19:12:00+03:00")      # diaspora: lighting for day 2
SUN_HAVDALAH = ts("2026-09-27T19:10:00+03:00")     # diaspora calendar

SUKKOT_IL = [
    {"category": "candles", "date": _iso(FRI_CANDLES)},
    {"category": "holiday", "yomtov": True, "title": "Sukkot I", "date": "2026-09-26"},
    {"category": "havdalah", "date": _iso(SAT_HAVDALAH)},
]
SUKKOT_DIASPORA = SUKKOT_IL[:2] + [
    {"category": "candles", "date": _iso(SAT_CANDLES)},
    {"category": "holiday", "yomtov": True, "title": "Sukkot II", "date": "2026-09-27"},
    {"category": "havdalah", "date": _iso(SUN_HAVDALAH)},
]

# ── Rosh Hashana 5787: Fri 11/9 candles, chag Sat 12/9 + Sun 13/9, one
# havdalah on Sunday night.  Two days, in Israel as well as the diaspora.
RH_FRI_CANDLES = ts("2026-09-11T18:30:00+03:00")
RH_SAT_CANDLES = ts("2026-09-12T19:28:00+03:00")
RH_HAVDALAH = ts("2026-09-13T19:26:00+03:00")

ROSH_HASHANA_IL = [
    {"category": "candles", "date": _iso(RH_FRI_CANDLES)},
    {"category": "holiday", "yomtov": True, "title": "Rosh Hashana 5787", "date": "2026-09-12"},
    {"category": "candles", "date": _iso(RH_SAT_CANDLES)},
    {"category": "holiday", "yomtov": True, "title": "Rosh Hashana II", "date": "2026-09-13"},
    {"category": "havdalah", "date": _iso(RH_HAVDALAH)},
]


# Jerusalem (geonameid 281184) as Hebcal reports it - the primary response is
# where the Diaspora fetch gets its coordinates from.
JERUSALEM_LOC = {"title": "Jerusalem, Israel", "tzid": "Asia/Jerusalem",
                 "latitude": 31.76904, "longitude": 35.21633,
                 "cc": "IL", "geonameid": 281184}


class _FakeResponse:
    def __init__(self, items, location=JERUSALEM_LOC):
        self._items = items
        self._location = location

    def raise_for_status(self):
        pass

    def json(self):
        body = {"items": self._items}
        if self._location is not None:
            body["location"] = self._location
        return body


def patch_hebcal(monkeypatch, israel_items, diaspora_items=None, fail=False):
    """Stub requests.get; returns the list of param dicts actually requested."""
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(dict(params or {}))
        if fail:
            raise ConnectionError("network down")
        # Both calls now carry geonameid (same location, Israeli clock times);
        # only `i=off` marks the Diaspora holiday scheme.
        if (params or {}).get("i") == "off":
            return _FakeResponse(diaspora_items if diaspora_items is not None else [])
        return _FakeResponse(israel_items)

    monkeypatch.setattr(hg.requests, "get", fake_get)
    return calls


class TestYomTovSheni:
    """The diaspora-only second day must stay inside the window."""

    def test_second_day_is_in_window(self, monkeypatch):
        patch_hebcal(monkeypatch, SUKKOT_IL, SUKKOT_DIASPORA)
        gate = HebcalGate()
        sunday_noon = ts("2026-09-27T12:00:00+03:00")
        assert gate.is_in_window(SETTINGS, sunday_noon) is True

    def test_second_day_is_out_when_toggle_off(self, monkeypatch):
        patch_hebcal(monkeypatch, SUKKOT_IL, SUKKOT_DIASPORA)
        gate = HebcalGate()
        sunday_noon = ts("2026-09-27T12:00:00+03:00")
        assert gate.is_in_window(SETTINGS_NO_YTS, sunday_noon) is False

    def test_motzash_of_a_second_day_has_no_hole(self, monkeypatch):
        # The merge must not truncate the chain at the Israel havdalah: minute
        # by minute from the Israel havdalah to Sunday morning, always inside.
        patch_hebcal(monkeypatch, SUKKOT_IL, SUKKOT_DIASPORA)
        gate = HebcalGate()
        t = SAT_HAVDALAH
        while t <= ts("2026-09-27T09:00:00+03:00"):
            assert gate.is_in_window(SETTINGS, t) is True, _iso(t)
            t += 300

    def test_window_closes_after_the_diaspora_havdalah(self, monkeypatch):
        patch_hebcal(monkeypatch, SUKKOT_IL, SUKKOT_DIASPORA)
        gate = HebcalGate()
        assert gate.is_in_window(SETTINGS, SUN_HAVDALAH + 119 * 60) is True
        assert gate.is_in_window(SETTINGS, SUN_HAVDALAH + 121 * 60) is False

    def test_toggle_change_refetches(self, monkeypatch):
        calls = patch_hebcal(monkeypatch, SUKKOT_IL, SUKKOT_DIASPORA)
        gate = HebcalGate()
        now = ts("2026-09-26T12:00:00+03:00")
        gate.is_in_window(SETTINGS_NO_YTS, now)
        assert len(calls) == 1                       # Israel only
        gate.is_in_window(SETTINGS, now + 60)        # toggle flipped on
        assert len(calls) == 3                       # Israel + diaspora, inside the TTL
        assert calls[2].get("i") == "off"
        assert calls[2].get("latitude") == "31.76904"   # same location

    def test_diaspora_fetch_failure_falls_back_to_israel(self, monkeypatch):
        def fake_get(url, params=None, timeout=None):
            if (params or {}).get("i") == "off":
                raise ConnectionError("diaspora endpoint down")
            return _FakeResponse(SUKKOT_IL)

        monkeypatch.setattr(hg.requests, "get", fake_get)
        gate = HebcalGate()
        # Shabbat itself is still covered by the Israel calendar...
        assert gate.is_in_window(SETTINGS, ts("2026-09-26T12:00:00+03:00")) is True
        # ...only the second day is lost, which is the pre-fix behaviour.
        assert gate.is_in_window(SETTINGS, ts("2026-09-27T12:00:00+03:00")) is False


class TestMultiDayYomTovInIsrael:
    """A chain of candle-lightings under one havdalah, no diaspora involved."""

    def test_first_day_is_in_window(self, monkeypatch):
        # The regression: keeping only the LAST candles moved the window start
        # to Saturday evening, leaving Friday night and Shabbat outside it.
        patch_hebcal(monkeypatch, ROSH_HASHANA_IL, ROSH_HASHANA_IL)
        gate = HebcalGate()
        assert gate.is_in_window(SETTINGS, ts("2026-09-11T22:00:00+03:00")) is True
        assert gate.is_in_window(SETTINGS, ts("2026-09-12T12:00:00+03:00")) is True

    def test_second_day_is_in_window(self, monkeypatch):
        patch_hebcal(monkeypatch, ROSH_HASHANA_IL, ROSH_HASHANA_IL)
        gate = HebcalGate()
        assert gate.is_in_window(SETTINGS, ts("2026-09-13T12:00:00+03:00")) is True

    def test_whole_chain_is_continuous(self, monkeypatch):
        patch_hebcal(monkeypatch, ROSH_HASHANA_IL, ROSH_HASHANA_IL)
        gate = HebcalGate()
        t = RH_FRI_CANDLES
        while t <= RH_HAVDALAH:
            assert gate.is_in_window(SETTINGS, t) is True, _iso(t)
            t += 600

    def test_opens_before_and_closes_after(self, monkeypatch):
        patch_hebcal(monkeypatch, ROSH_HASHANA_IL, ROSH_HASHANA_IL)
        gate = HebcalGate()
        assert gate.is_in_window(SETTINGS, RH_FRI_CANDLES - 241 * 60) is False
        assert gate.is_in_window(SETTINGS, RH_FRI_CANDLES - 239 * 60) is True
        assert gate.is_in_window(SETTINGS, RH_HAVDALAH + 121 * 60) is False


class TestOrdinaryWeek:
    """A plain Friday-Saturday must behave exactly as before the rewrite."""

    ITEMS = [
        {"category": "candles", "date": _iso(ts("2026-07-17T19:00:00+03:00"))},
        {"category": "parashat", "date": "2026-07-18"},
        {"category": "havdalah", "date": _iso(ts("2026-07-18T20:10:00+03:00"))},
    ]
    CANDLES = ts("2026-07-17T19:00:00+03:00")
    HAVDALAH = ts("2026-07-18T20:10:00+03:00")

    def test_boundaries(self, monkeypatch):
        patch_hebcal(monkeypatch, self.ITEMS, self.ITEMS)
        gate = HebcalGate()
        assert gate.is_in_window(SETTINGS, self.CANDLES - 241 * 60) is False
        assert gate.is_in_window(SETTINGS, self.CANDLES - 240 * 60) is True
        assert gate.is_in_window(SETTINGS, self.CANDLES + 3600) is True
        assert gate.is_in_window(SETTINGS, self.HAVDALAH + 120 * 60) is True
        assert gate.is_in_window(SETTINGS, self.HAVDALAH + 121 * 60) is False

    def test_custom_offsets(self, monkeypatch):
        patch_hebcal(monkeypatch, self.ITEMS, self.ITEMS)
        gate = HebcalGate()
        settings = dict(SETTINGS,
                        HEBCAL_GATE_WINDOW_BEFORE_MINUTES=30,
                        HEBCAL_GATE_WINDOW_AFTER_MINUTES=10)
        assert gate.is_in_window(settings, self.CANDLES - 31 * 60) is False
        assert gate.is_in_window(settings, self.CANDLES - 29 * 60) is True
        assert gate.is_in_window(settings, self.HAVDALAH + 11 * 60) is False

    def test_midweek_is_outside(self, monkeypatch):
        patch_hebcal(monkeypatch, self.ITEMS, self.ITEMS)
        gate = HebcalGate()
        assert gate.is_in_window(SETTINGS, ts("2026-07-21T12:00:00+03:00")) is False


class TestFailOpen:
    def test_unreachable_hebcal_opens_the_gate(self, monkeypatch):
        patch_hebcal(monkeypatch, [], fail=True)
        gate = HebcalGate()
        assert gate.is_in_window(SETTINGS, ts("2026-07-21T12:00:00+03:00")) is True

    def test_empty_response_opens_the_gate(self, monkeypatch):
        patch_hebcal(monkeypatch, [], [])
        gate = HebcalGate()
        assert gate.is_in_window(SETTINGS, ts("2026-07-21T12:00:00+03:00")) is True

    def test_failure_keeps_stored_windows(self, monkeypatch):
        calls = patch_hebcal(monkeypatch, TestOrdinaryWeek.ITEMS, TestOrdinaryWeek.ITEMS)
        gate = HebcalGate()
        friday = TestOrdinaryWeek.CANDLES + 3600
        assert gate.is_in_window(SETTINGS, friday) is True
        calls.clear()
        patch_hebcal(monkeypatch, [], fail=True)
        # TTL expired, the refetch fails - the stored window must survive.
        assert gate.is_in_window(SETTINGS, friday + _ttl() + 60) is True
        assert gate.is_in_window(SETTINGS, TestOrdinaryWeek.HAVDALAH + 3 * 3600) is False

    def test_stale_data_opens_the_gate(self, monkeypatch):
        patch_hebcal(monkeypatch, TestOrdinaryWeek.ITEMS, TestOrdinaryWeek.ITEMS)
        gate = HebcalGate()
        friday = TestOrdinaryWeek.CANDLES + 3600
        gate.is_in_window(SETTINGS, friday)
        patch_hebcal(monkeypatch, [], fail=True)
        # Nine days later, with every refetch failing, the gate stops trusting
        # the stored window rather than declaring an arbitrary weekday "outside".
        assert gate.is_in_window(SETTINGS, friday + 9 * 86400) is True


class TestFetchPolicy:
    def test_ttl_throttles_refetch(self, monkeypatch):
        calls = patch_hebcal(monkeypatch, TestOrdinaryWeek.ITEMS, TestOrdinaryWeek.ITEMS)
        gate = HebcalGate()
        now = TestOrdinaryWeek.CANDLES
        gate.is_in_window(SETTINGS, now)
        gate.is_in_window(SETTINGS, now + 60)
        assert len(calls) == 2                      # one round trip (IL + diaspora)
        gate.is_in_window(SETTINGS, now + _ttl() + 1)
        assert len(calls) == 4

    def test_failure_is_throttled_too(self, monkeypatch):
        calls = patch_hebcal(monkeypatch, [], fail=True)
        gate = HebcalGate()
        now = TestOrdinaryWeek.CANDLES
        gate.is_in_window(SETTINGS, now)
        gate.is_in_window(SETTINGS, now + 60)
        assert len(calls) == 1

    def test_geo_change_refetches(self, monkeypatch):
        calls = patch_hebcal(monkeypatch, TestOrdinaryWeek.ITEMS, TestOrdinaryWeek.ITEMS)
        gate = HebcalGate()
        now = TestOrdinaryWeek.CANDLES
        gate.is_in_window(SETTINGS, now)
        gate.is_in_window({"GEO_NAME_ID": "295530"}, now + 60)
        assert len(calls) == 4
        assert calls[2]["geonameid"] == "295530"

    def test_query_is_anchored_one_day_back(self, monkeypatch):
        # Without the anchor, an un-dated query returns the NEXT Shabbat, which
        # on the Sunday of a Yom Tov Sheni drops the chain we are standing in.
        calls = patch_hebcal(monkeypatch, SUKKOT_IL, SUKKOT_DIASPORA)
        gate = HebcalGate()
        gate.is_in_window(SETTINGS, ts("2026-09-27T12:00:00+03:00"))
        assert (calls[0]["gy"], calls[0]["gm"], calls[0]["gd"]) == ("2026", "9", "26")

    def test_yomtov_holiday_items_become_starts(self, monkeypatch):
        # A date-only yom-tov item opens a window even with no candles item.
        items = [{"category": "holiday", "yomtov": True, "date": "2026-09-27"},
                 {"category": "havdalah", "date": _iso(SUN_HAVDALAH)}]
        patch_hebcal(monkeypatch, items, items)
        gate = HebcalGate()
        assert gate.is_in_window(SETTINGS, ts("2026-09-27T12:00:00+03:00")) is True

    def test_non_yomtov_holiday_is_ignored(self, monkeypatch):
        # Chol ha-moed / minor fasts must not open the gate.
        items = [{"category": "holiday", "yomtov": False, "date": "2026-09-29"}]
        patch_hebcal(monkeypatch, items, items)
        gate = HebcalGate()
        assert gate.is_in_window(SETTINGS, ts("2026-09-29T12:00:00+03:00")) is True  # no data -> fail open
        assert gate._starts == []

    def test_start_without_havdalah_uses_26h_safety(self, monkeypatch):
        items = [{"category": "candles", "date": _iso(FRI_CANDLES)}]
        patch_hebcal(monkeypatch, items, items)
        gate = HebcalGate()
        assert gate.is_in_window(SETTINGS, FRI_CANDLES + 25 * 3600) is True
        assert gate.is_in_window(SETTINGS, FRI_CANDLES + 26 * 3600 + 121 * 60) is False


def _ttl() -> float:
    return hg._CACHE_TTL_S


class TestBadSettings:
    """is_in_window runs inside the watchdog tick - a bad settings value must
    fall back to the default, never raise (an exception there kills the loop)."""

    def test_null_and_garbage_offsets_fall_back(self, monkeypatch):
        patch_hebcal(monkeypatch, TestOrdinaryWeek.ITEMS, TestOrdinaryWeek.ITEMS)
        gate = HebcalGate()
        settings = dict(SETTINGS,
                        HEBCAL_GATE_WINDOW_BEFORE_MINUTES=None,
                        HEBCAL_GATE_WINDOW_AFTER_MINUTES="not a number")
        assert gate.is_in_window(settings, TestOrdinaryWeek.CANDLES - 239 * 60) is True
        assert gate.is_in_window(settings, TestOrdinaryWeek.CANDLES - 241 * 60) is False

    def test_wrapped_values_are_unwrapped(self, monkeypatch):
        patch_hebcal(monkeypatch, TestOrdinaryWeek.ITEMS, TestOrdinaryWeek.ITEMS)
        gate = HebcalGate()
        settings = {"GEO_NAME_ID": {"value": "281184"},
                    "YOM_TOV_SHENI": {"value": False},
                    "HEBCAL_GATE_WINDOW_BEFORE_MINUTES": {"value": 30}}
        assert gate.is_in_window(settings, TestOrdinaryWeek.CANDLES - 29 * 60) is True
        assert gate.is_in_window(settings, TestOrdinaryWeek.CANDLES - 31 * 60) is False
        assert gate._diaspora is False


class TestDiasporaUsesTheSameLocation:
    """A hotel in Israel with chutz-la-aretz guests keeps Yom Tov Sheni on
    ISRAELI clock times, so only the holiday scheme may come from the Diaspora
    calendar.

    Hebcal forces i=on for any location it can tell is in Israel, and it reads
    that from the tzid - geonameid=281184 AND raw Jerusalem coordinates with
    tzid=Asia/Jerusalem both come back with Sukkot II as chol ha-moed and
    "i=on" echoed in the item links (checked against the live API on
    2026-08-31).  The same coordinates under a non-Israel tzid return the
    Diaspora scheme, with every timestamp still carrying its real UTC offset -
    the Israel fetch's havdalah and the geo=pos fetch's second-day candle
    lighting land on the very same instant.
    """

    def _params(self, monkeypatch, settings=SETTINGS):
        calls = patch_hebcal(monkeypatch, SUKKOT_IL, SUKKOT_DIASPORA)
        gate = HebcalGate()
        gate.is_in_window(settings, ts("2026-09-26T12:00:00+03:00"))
        return calls

    def test_primary_asks_for_the_configured_location(self, monkeypatch):
        israel, _ = self._params(monkeypatch)
        assert israel["geonameid"] == "281184"
        assert israel["tzid"] == "Asia/Jerusalem"
        assert "i" not in israel

    def test_diaspora_repeats_the_primary_coordinates(self, monkeypatch):
        _, diaspora = self._params(monkeypatch)
        assert diaspora["geo"] == "pos"
        assert (diaspora["latitude"], diaspora["longitude"]) == ("31.76904", "35.21633")
        assert diaspora["i"] == "off"
        assert "geonameid" not in diaspora

    def test_diaspora_tzid_is_not_israel(self, monkeypatch):
        # The whole point: an Israeli tzid makes Hebcal override i=off.
        _, diaspora = self._params(monkeypatch)
        assert diaspora["tzid"] != "Asia/Jerusalem"
        assert not diaspora["tzid"].startswith("Asia/")

    def test_only_location_and_calendar_differ(self, monkeypatch):
        israel, diaspora = self._params(monkeypatch)
        location_keys = {"geonameid", "geo", "latitude", "longitude", "tzid", "i"}
        assert {k: v for k, v in israel.items() if k not in location_keys} == \
               {k: v for k, v in diaspora.items() if k not in location_keys}

    def test_custom_location_propagates(self, monkeypatch):
        # The coordinates come from the primary RESPONSE, so a different
        # GEO_NAME_ID follows through with no extra setting.
        calls = patch_hebcal(monkeypatch, SUKKOT_IL, SUKKOT_DIASPORA)
        gate = HebcalGate()
        gate.is_in_window({"GEO_NAME_ID": "294801"},          # Tiberias
                          ts("2026-09-26T12:00:00+03:00"))
        assert calls[0]["geonameid"] == "294801"
        assert calls[1]["latitude"] == "31.76904"   # whatever the response said

    def test_response_without_coordinates_skips_the_diaspora_call(self, monkeypatch):
        # No coordinates -> query nothing rather than query somewhere else.
        def fake_get(url, params=None, timeout=None):
            calls.append(dict(params or {}))
            return _FakeResponse(SUKKOT_IL, location=None)

        calls = []
        monkeypatch.setattr(hg.requests, "get", fake_get)
        gate = HebcalGate()
        # Shabbat itself still works off the Israel calendar...
        assert gate.is_in_window(SETTINGS, ts("2026-09-26T12:00:00+03:00")) is True
        assert len(calls) == 1
        # ...and only the second day is lost, exactly as if the fetch failed.
        assert gate.is_in_window(SETTINGS, ts("2026-09-27T12:00:00+03:00")) is False
