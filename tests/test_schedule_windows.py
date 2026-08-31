"""Unit tests for shabbat_detector.schedule_windows (schedule-mode engine)."""
from datetime import datetime

import pytest

from shabbat_detector import schedule_windows as sw
from shabbat_detector.schedule_windows import ScheduleWindows, decide_write


def ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


# Friday-night Shabbat: candles Fri 19:00, havdalah Sat 20:10 (Israel time).
CANDLES = ts("2026-07-17T19:00:00+03:00")
HAVDALAH = ts("2026-07-18T20:10:00+03:00")


def windows(starts, ends, fetched_at):
    return ScheduleWindows.from_dict(
        {"starts": starts, "ends": ends, "fetched_at": fetched_at}
    )


class TestSingleWindow:
    def setup_method(self):
        self.w = windows([CANDLES], [HAVDALAH], fetched_at=CANDLES - 3600)

    def test_before_window(self):
        assert self.w.is_active(CANDLES - 101 * 60, 100, 60) is False

    def test_entry_offset(self):
        assert self.w.is_active(CANDLES - 99 * 60, 100, 60) is True

    def test_entry_boundary_exact(self):
        # now == adjusted start counts as inside (<= in the port)
        assert self.w.is_active(CANDLES - 100 * 60, 100, 60) is True

    def test_during_shabbat(self):
        assert self.w.is_active(CANDLES + 6 * 3600, 100, 60) is True

    def test_exit_offset(self):
        assert self.w.is_active(HAVDALAH + 59 * 60, 100, 60) is True
        assert self.w.is_active(HAVDALAH + 61 * 60, 100, 60) is False

    def test_exit_boundary_exact(self):
        assert self.w.is_active(HAVDALAH + 60 * 60, 100, 60) is True

    def test_zero_offsets_are_legal(self):
        assert self.w.is_active(CANDLES - 30, 0, 0) is False
        assert self.w.is_active(CANDLES + 30, 0, 0) is True
        assert self.w.is_active(HAVDALAH + 30, 0, 0) is False


class TestMultiWindow:
    def test_multi_day_yomtov_chain(self):
        # Wed/Thu/Fri candles, single havdalah Sat night (e.g. Rosh Hashana
        # rolling into Shabbat).  Thursday midday must be INSIDE.
        starts = [
            ts("2026-09-09T18:20:00+03:00"),
            ts("2026-09-10T18:19:00+03:00"),
            ts("2026-09-11T18:18:00+03:00"),
        ]
        ends = [ts("2026-09-12T19:25:00+03:00")]
        w = windows(starts, ends, fetched_at=starts[0])
        thursday_noon = ts("2026-09-10T12:00:00+03:00")
        assert w.is_active(thursday_noon, 100, 60) is True
        after_havdalah = ends[0] + 61 * 60
        assert w.is_active(after_havdalah, 100, 60) is False

    def test_two_separate_windows_gap_is_outside(self):
        # Shabbat ends Sat night; next candles the following Friday.
        starts = [CANDLES, ts("2026-07-24T19:00:00+03:00")]
        ends = [HAVDALAH, ts("2026-07-25T20:05:00+03:00")]
        w = windows(starts, ends, fetched_at=CANDLES)
        tuesday = ts("2026-07-21T12:00:00+03:00")
        assert w.is_active(tuesday, 100, 60) is False

    def test_missing_havdalah_26h_safety(self):
        w = windows([CANDLES], [], fetched_at=CANDLES - 3600)
        assert w.is_active(CANDLES + 10 * 3600, 100, 60) is True
        # past the 26h safety (measured from the adjusted start)
        assert w.is_active(CANDLES - 100 * 60 + 27 * 3600, 100, 60) is False


class TestUnknownData:
    def test_empty_data_is_none(self):
        w = ScheduleWindows()
        assert w.is_active(CANDLES, 100, 60) is None

    def test_stale_data_is_none(self):
        w = windows([CANDLES], [HAVDALAH], fetched_at=CANDLES)
        nine_days_later = CANDLES + 9 * 24 * 3600
        assert w.is_active(nine_days_later, 100, 60) is None


class TestPersistenceRoundtrip:
    def test_roundtrip(self):
        w = windows([CANDLES], [HAVDALAH], fetched_at=CANDLES - 10)
        w._geo = "295530"
        w._diaspora = False
        restored = ScheduleWindows.from_dict(w.to_dict())
        assert restored.to_dict() == w.to_dict()
        assert restored.is_active(CANDLES + 60, 100, 60) is True

    def test_from_dict_garbage_starts_empty(self):
        assert ScheduleWindows.from_dict(None).is_active(CANDLES, 100, 60) is None
        assert ScheduleWindows.from_dict({"starts": "junk"}).to_dict()["starts"] == []


JERUSALEM_LOC = {"title": "Jerusalem, Israel", "tzid": "Asia/Jerusalem",
                 "latitude": 31.76904, "longitude": 35.21633, "cc": "IL"}


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


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().isoformat()


class TestFetch:
    def _patch(self, monkeypatch, israel_items, diaspora_items=None, fail=False):
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(dict(params or {}))
            if fail:
                raise ConnectionError("network down")
            # Both calls carry geonameid; only `i=off` marks the Diaspora
            # holiday scheme (same location, Israeli clock times).
            if (params or {}).get("i") == "off":
                return _FakeResponse(diaspora_items or [])
            return _FakeResponse(israel_items)

        monkeypatch.setattr(sw.requests, "get", fake_get)
        return calls

    def test_fetch_parses_candles_havdalah_and_yomtov(self, monkeypatch):
        items = [
            {"category": "candles", "date": _iso(CANDLES)},
            {"category": "havdalah", "date": _iso(HAVDALAH)},
            {"category": "holiday", "yomtov": True, "date": "2026-07-17"},
            {"category": "holiday", "yomtov": False, "date": "2026-07-18"},  # ignored
            {"category": "parashat", "date": "2026-07-18"},                  # ignored
        ]
        self._patch(monkeypatch, items)
        w = ScheduleWindows()
        now = CANDLES - 24 * 3600
        assert w.refresh_if_due({"GEO_NAME_ID": "281184", "YOM_TOV_SHENI": False}, now) is True
        d = w.to_dict()
        assert len(d["starts"]) == 2   # candles + yomtov holiday
        assert len(d["ends"]) == 1
        assert w.is_active(CANDLES + 60, 100, 60) is True

    def test_yom_tov_sheni_merges_diaspora(self, monkeypatch):
        israel = [
            {"category": "candles", "date": _iso(CANDLES)},
            {"category": "havdalah", "date": _iso(HAVDALAH)},
        ]
        extra_start = ts("2026-07-19T19:00:00+03:00")
        extra_end = ts("2026-07-20T20:00:00+03:00")
        diaspora = israel + [
            {"category": "candles", "date": _iso(extra_start)},
            {"category": "havdalah", "date": _iso(extra_end)},
        ]
        calls = self._patch(monkeypatch, israel, diaspora)
        w = ScheduleWindows()
        now = CANDLES - 24 * 3600
        # YOM_TOV_SHENI absent => enabled (web semantics: !== false)
        assert w.refresh_if_due({"GEO_NAME_ID": "281184"}, now) is True
        assert len(calls) == 2
        assert calls[1].get("i") == "off"
        assert calls[1].get("latitude") == "31.76904"   # same location
        # Sunday inside the diaspora-only second day
        assert w.is_active(extra_start + 3600, 100, 60) is True

    def test_fetch_failure_keeps_stored_windows(self, monkeypatch):
        w = windows([CANDLES], [HAVDALAH], fetched_at=CANDLES - 7 * 3600)
        w._geo = "281184"
        w._diaspora = True
        self._patch(monkeypatch, [], fail=True)
        now = CANDLES - 3600
        assert w.refresh_if_due({"GEO_NAME_ID": "281184"}, now) is False
        assert w.is_active(CANDLES + 60, 100, 60) is True   # data intact

    def test_empty_fetch_keeps_stored_windows(self, monkeypatch):
        w = windows([CANDLES], [HAVDALAH], fetched_at=CANDLES - 7 * 3600)
        w._geo = "281184"
        w._diaspora = True
        self._patch(monkeypatch, [])
        assert w.refresh_if_due({"GEO_NAME_ID": "281184"}, CANDLES - 3600) is False
        assert w.is_active(CANDLES + 60, 100, 60) is True

    def test_ttl_no_refetch_when_fresh(self, monkeypatch):
        calls = self._patch(monkeypatch, [{"category": "candles", "date": _iso(CANDLES)}])
        w = ScheduleWindows()
        now = CANDLES - 24 * 3600
        w.refresh_if_due({"GEO_NAME_ID": "281184", "YOM_TOV_SHENI": False}, now)
        w.refresh_if_due({"GEO_NAME_ID": "281184", "YOM_TOV_SHENI": False}, now + 60)
        assert len(calls) == 1   # second call skipped (6h TTL)

    def test_geo_change_invalidates(self, monkeypatch):
        calls = self._patch(monkeypatch, [{"category": "candles", "date": _iso(CANDLES)}])
        w = ScheduleWindows()
        now = CANDLES - 24 * 3600
        w.refresh_if_due({"GEO_NAME_ID": "281184", "YOM_TOV_SHENI": False}, now)
        w.refresh_if_due({"GEO_NAME_ID": "295530", "YOM_TOV_SHENI": False}, now + 60)
        assert len(calls) == 2   # geo change forced a refetch

    def test_failure_retry_throttled(self, monkeypatch):
        calls = self._patch(monkeypatch, [], fail=True)
        w = ScheduleWindows()
        now = CANDLES - 24 * 3600
        w.refresh_if_due({"GEO_NAME_ID": "281184", "YOM_TOV_SHENI": False}, now)
        w.refresh_if_due({"GEO_NAME_ID": "281184", "YOM_TOV_SHENI": False}, now + 60)
        assert len(calls) == 1   # within the 10-min failure backoff
        w.refresh_if_due({"GEO_NAME_ID": "281184", "YOM_TOV_SHENI": False}, now + 601)
        assert len(calls) == 2


class TestDecideWrite:
    def test_no_write_when_db_agrees(self):
        assert decide_write(True, True, None, 0.0, now=1000.0) is False
        assert decide_write(False, None, None, 0.0, now=1000.0) is False  # absent == falsy

    def test_write_on_flip(self):
        assert decide_write(True, False, None, 0.0, now=1000.0) is True
        assert decide_write(False, True, True, 0.0, now=1000.0) is True

    def test_echo_grace_suppresses_rewrite(self):
        # we wrote True 30s ago; SSE echo not back yet (cache still False)
        assert decide_write(True, False, True, 970.0, now=1000.0) is False

    def test_manual_write_healed_after_grace(self):
        # someone flipped the DB to False 10 minutes after our True write
        assert decide_write(True, False, True, 400.0, now=1000.0) is True


class TestDiasporaUsesTheSameLocation:
    """Yom Tov Sheni in an Israeli hotel runs on Israeli clock times: the
    secondary fetch changes the holiday scheme (i=off), never the location."""

    def test_both_calls_carry_the_same_geonameid(self, monkeypatch):
        calls = TestFetch()._patch(
            monkeypatch,
            [{"category": "candles", "date": _iso(CANDLES)}],
            [{"category": "candles", "date": _iso(CANDLES)}],
        )
        w = ScheduleWindows()
        w.refresh_if_due({"GEO_NAME_ID": "294801"}, CANDLES - 24 * 3600)
        assert calls[0]["geonameid"] == "294801" and "i" not in calls[0]
        # The Diaspora call repeats the primary response's coordinates.
        assert calls[1]["geo"] == "pos" and calls[1]["i"] == "off"
        assert (calls[1]["latitude"], calls[1]["longitude"]) == ("31.76904", "35.21633")
        assert calls[1]["tzid"] != "Asia/Jerusalem"


# ── Recorded live Hebcal payloads, Sukkot 5787 (fetched 2026-08-31) ──────────
# Trimmed to the fields the parser reads.  These are the real responses, so the
# tests below pin the actual API contract rather than an assumed one.
_JLM = {"title": "Jerusalem, Israel", "tzid": "Asia/Jerusalem",
        "latitude": 31.76904, "longitude": 35.21633, "cc": "IL", "geonameid": 281184}

# geonameid=281184 - Israel scheme.  Sukkot II comes back as chol ha-moed and
# there is no Sunday havdalah.
REAL_ISRAEL = {"location": _JLM, "items": [
    {"title": "Erev Sukkot", "date": "2026-09-25", "category": "holiday"},
    {"title": "Candle lighting: 18:31", "date": "2026-09-25T18:31:00+03:00", "category": "candles"},
    {"title": "Sukkot I", "date": "2026-09-26", "category": "holiday", "yomtov": True},
    {"title": "Havdalah: 19:07", "date": "2026-09-26T19:07:00+03:00", "category": "havdalah"},
    {"title": "Sukkot II (CH''M)", "date": "2026-09-27", "category": "holiday"},
    {"title": "Sukkot III (CH''M)", "date": "2026-09-28", "category": "holiday"},
]}

# geo=pos at the SAME coordinates with a non-Israel tzid - Diaspora scheme.
# Note the second-day candle lighting lands on the exact instant of the Israel
# fetch's havdalah: same sunset, different calendar.
REAL_DIASPORA = {"location": {"tzid": "Europe/Athens", "latitude": 31.76904,
                              "longitude": 35.21633, "geo": "pos"}, "items": [
    {"title": "Erev Sukkot", "date": "2026-09-25", "category": "holiday"},
    {"title": "Candle lighting: 6:31pm", "date": "2026-09-25T18:31:00+03:00", "category": "candles"},
    {"title": "Sukkot I", "date": "2026-09-26", "category": "holiday", "yomtov": True},
    {"title": "Candle lighting: 7:07pm", "date": "2026-09-26T19:07:00+03:00", "category": "candles"},
    {"title": "Sukkot II", "date": "2026-09-27", "category": "holiday", "yomtov": True},
    {"title": "Havdalah: 7:06pm", "date": "2026-09-27T19:06:00+03:00", "category": "havdalah"},
    {"title": "Sukkot III (CH''M)", "date": "2026-09-28", "category": "holiday"},
]}

CHAIN_START = ts("2026-09-25T18:31:00+03:00")   # Friday candle lighting
IL_HAVDALAH = ts("2026-09-26T19:07:00+03:00")   # Israel: motzaei Shabbat
CHAIN_END = ts("2026-09-27T19:06:00+03:00")     # Diaspora: end of the 2nd day


class _JsonResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def _patch_real(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        return _JsonResponse(REAL_DIASPORA if (params or {}).get("i") == "off"
                             else REAL_ISRAEL)
    monkeypatch.setattr(sw.requests, "get", fake_get)


class TestRealSukkotPayloads:
    """The two calendars must never be merged into one start/end list.

    Merged, the Israel havdalah (Sat 19:07) becomes the earliest end after the
    active start and closes the window while the Diaspora calendar still has
    the second day open - a ~2 hour hole on the second night, measured at
    26/9 20:37-22:20 before this fix.
    """

    def setup_windows(self, monkeypatch):
        _patch_real(monkeypatch)
        w = ScheduleWindows()
        assert w.refresh_if_due({"GEO_NAME_ID": "281184"},
                                ts("2026-09-25T06:00:00+03:00")) is True
        return w

    def test_no_hole_anywhere_in_the_chain(self, monkeypatch):
        w = self.setup_windows(monkeypatch)
        t = CHAIN_START
        while t <= CHAIN_END:
            assert w.is_active(t, 100, 60) is True, datetime.fromtimestamp(t).isoformat()
            t += 60

    def test_the_motzash_hole_specifically(self, monkeypatch):
        w = self.setup_windows(monkeypatch)
        for offset_min in (30, 60, 90, 120, 150, 180, 195):
            t = IL_HAVDALAH + offset_min * 60
            assert w.is_active(t, 100, 60) is True, f"+{offset_min}min"

    def test_second_day_is_active(self, monkeypatch):
        w = self.setup_windows(monkeypatch)
        assert w.is_active(ts("2026-09-27T12:00:00+03:00"), 100, 60) is True

    def test_closes_after_the_diaspora_havdalah(self, monkeypatch):
        w = self.setup_windows(monkeypatch)
        assert w.is_active(CHAIN_END + 59 * 60, 100, 60) is True
        assert w.is_active(CHAIN_END + 61 * 60, 100, 60) is False
        assert w.is_active(ts("2026-09-28T09:00:00+03:00"), 100, 60) is False

    def test_toggle_off_ends_at_the_israel_havdalah(self, monkeypatch):
        _patch_real(monkeypatch)
        w = ScheduleWindows()
        w.refresh_if_due({"GEO_NAME_ID": "281184", "YOM_TOV_SHENI": False},
                         ts("2026-09-25T06:00:00+03:00"))
        assert w.is_active(IL_HAVDALAH + 30 * 60, 100, 60) is True     # +60 offset
        assert w.is_active(IL_HAVDALAH + 61 * 60, 100, 60) is False
        assert w.is_active(ts("2026-09-27T12:00:00+03:00"), 100, 60) is False

    def test_the_two_calendars_stay_apart_in_state(self, monkeypatch):
        w = self.setup_windows(monkeypatch)
        d = w.to_dict()
        assert IL_HAVDALAH in d["ends"]          # Israel havdalah, Israel list
        assert IL_HAVDALAH not in d["d_ends"]    # never leaks into the Diaspora one
        assert CHAIN_END in d["d_ends"]
        assert CHAIN_END not in d["ends"]

    def test_state_survives_a_round_trip(self, monkeypatch):
        w = self.setup_windows(monkeypatch)
        restored = ScheduleWindows.from_dict(w.to_dict())
        t = IL_HAVDALAH + 2 * 3600           # inside the former hole
        assert restored.is_active(t, 100, 60) is True

    def test_old_state_file_restores_as_israel_only(self):
        # Written before the split: no d_starts/d_ends keys at all.
        legacy = {"starts": [CHAIN_START], "ends": [IL_HAVDALAH],
                  "fetched_at": CHAIN_START, "geo": "281184", "diaspora": True}
        w = ScheduleWindows.from_dict(legacy)
        assert w.is_active(CHAIN_START + 3600, 100, 60) is True
        assert w.is_active(ts("2026-09-27T12:00:00+03:00"), 100, 60) is False
