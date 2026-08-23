"""בדיקות לצמצום קריאות ה-Firebase של monitor.fetch_status (גרסה 1.1.11).

הדשבורד המקומי (שנפתח אוטומטית על כל Pi) קורא ל-fetch_status כל 5 שניות.
הקוד הישן הוריד בכל קריאה את כל עץ /settings + את קונפיג המעלית; עכשיו
settings נקרא רק בשני מפתחות צרים, וה-config וה-settings יושבים ב-TTL cache
של 60 שניות. רק /elevators/{id} (הקומה החיה) נשאר טרי בכל קריאה.
"""
from __future__ import annotations

import pytest

import monitor


BASE = "https://example.test"
EID = "A"


class _FakeResp:
    def __init__(self, val):
        self._val = val

    def json(self):
        return self._val


class _FakeNet:
    """requests.get מזויף שרושם את כל ה-URLs ומחזיר ערכים לפי נתיב."""

    def __init__(self):
        self.urls: list[str] = []
        self.hebcal_val = False
        self.fail_settings = False

    def get(self, url, timeout=None):
        self.urls.append(url)
        if "/settings/HEBCAL_GATE_ENABLED.json" in url:
            if self.fail_settings:
                raise ConnectionError("no network")
            return _FakeResp(self.hebcal_val)
        if "/settings/SHABBAT_DETECTION.json" in url:
            if self.fail_settings:
                raise ConnectionError("no network")
            return _FakeResp({"REQUIRED_MATCHING_CYCLES": 2})
        if f"/elevator_configs/{EID}.json" in url:
            return _FakeResp({"TOP_FLOOR": 9, "SHABBAT_ACTIVE": False})
        if f"/elevators/{EID}.json" in url:
            return _FakeResp({"floor": 3, "timestamp": 123})
        raise AssertionError(f"unexpected URL fetched: {url}")

    def count(self, needle: str) -> int:
        return sum(1 for u in self.urls if needle in u)


@pytest.fixture()
def net(monkeypatch):
    fake = _FakeNet()
    monkeypatch.setattr(monitor.requests, "get", fake.get)
    monitor._ttl_cache.clear()
    return fake


class TestNarrowSettings:
    def test_never_fetches_whole_settings_tree(self, net):
        monitor.fetch_status(BASE, EID)
        assert not any(u.endswith("/settings.json") for u in net.urls), net.urls

    def test_fetches_only_needed_settings_keys(self, net):
        out = monitor.fetch_status(BASE, EID)
        assert out["settings"] == {
            "HEBCAL_GATE_ENABLED": False,
            "SHABBAT_DETECTION": {"REQUIRED_MATCHING_CYCLES": 2},
        }

    def test_null_key_stays_absent_for_default(self, net):
        # מפתח שלא קיים ב-DB חוזר כ-null - אסור להכניס אותו כ-None, אחרת
        # settings.get("HEBCAL_GATE_ENABLED", True) היה מאבד את ברירת המחדל.
        net.hebcal_val = None
        out = monitor.fetch_status(BASE, EID)
        assert "HEBCAL_GATE_ENABLED" not in out["settings"]
        assert out["settings"].get("HEBCAL_GATE_ENABLED", True) is True
        assert "SHABBAT_DETECTION" in out["settings"]


class TestTtlCache:
    def test_second_call_within_ttl_hits_cache(self, net):
        monitor.fetch_status(BASE, EID)
        monitor.fetch_status(BASE, EID)
        # קומה חיה - בכל קריאה; קונפיג + settings - פעם אחת בלבד.
        assert net.count(f"/elevators/{EID}.json") == 2
        assert net.count(f"/elevator_configs/{EID}.json") == 1
        assert net.count("/settings/HEBCAL_GATE_ENABLED.json") == 1
        assert net.count("/settings/SHABBAT_DETECTION.json") == 1

    def test_cache_expires_after_ttl(self, net, monkeypatch):
        t = {"now": 1000.0}
        monkeypatch.setattr(monitor.time, "time", lambda: t["now"])
        monitor.fetch_status(BASE, EID)
        t["now"] += monitor._TTL_S + 1
        monitor.fetch_status(BASE, EID)
        assert net.count(f"/elevator_configs/{EID}.json") == 2
        assert net.count("/settings/HEBCAL_GATE_ENABLED.json") == 2

    def test_settings_failure_not_cached(self, net):
        net.fail_settings = True
        out1 = monitor.fetch_status(BASE, EID)
        assert out1["settings"] == {}
        # הרשת חזרה - הקריאה הבאה חייבת לנסות שוב ולהצליח (כישלון לא נכנס ל-cache)
        net.fail_settings = False
        out2 = monitor.fetch_status(BASE, EID)
        assert out2["settings"].get("SHABBAT_DETECTION") == {"REQUIRED_MATCHING_CYCLES": 2}


class TestRenderSmoke:
    def test_render_with_narrow_status(self, net, capsys):
        out = monitor.fetch_status(BASE, EID)
        monitor.render(out, EID)  # לא נזרקת חריגה על הסטטוס הצר
        printed = capsys.readouterr().out
        assert "Hebcal" in printed

    def test_render_default_when_hebcal_absent(self, net, capsys):
        net.hebcal_val = None
        out = monitor.fetch_status(BASE, EID)
        monitor.render(out, EID)
        printed = capsys.readouterr().out
        # ברירת המחדל היא "פעיל" (True) כשהמפתח לא קיים
        assert "פעיל" in printed
