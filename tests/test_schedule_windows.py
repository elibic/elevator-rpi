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


class _FakeResponse:
    def __init__(self, items):
        self._items = items

    def raise_for_status(self):
        pass

    def json(self):
        return {"items": self._items}


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().isoformat()


class TestFetch:
    def _patch(self, monkeypatch, israel_items, diaspora_items=None, fail=False):
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(dict(params or {}))
            if fail:
                raise ConnectionError("network down")
            if "geonameid" in (params or {}):
                return _FakeResponse(israel_items)
            return _FakeResponse(diaspora_items or [])

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
        assert calls[1].get("i") == "off" and "geonameid" not in calls[1]
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
