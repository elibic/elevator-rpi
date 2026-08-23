"""בדיקות לתיקון ה-backoff של חיבורי ה-SSE (גרסה 1.1.11).

הבאג הישן: המונה התאפס על כל נתון שהתקבל, ו-Firebase תמיד שולח put מלא
בהתחברות - כך שחיבור שרועד (מתנתק שניות ספורות אחרי ההתחברות) התחבר-מחדש
בקצב הבסיס לנצח, והוריד את העץ המלא בכל פעם. עכשיו האיפוס נעשה רק כשהחיבור
החזיק מעמד לפחות _STREAM_HEALTHY_S.
"""
from __future__ import annotations

import pytest

from shabbat_detector import firebase_client as fc


class _StopLoop(Exception):
    """עוצר את לולאות ה-while True של הקליינט אחרי מספיק מחזורים."""


class _Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t


class _SleepRecorder:
    def __init__(self, stop_after: int):
        self.delays: list[float] = []
        self._stop_after = stop_after

    def __call__(self, d):
        self.delays.append(round(d, 3))
        if len(self.delays) >= self._stop_after:
            raise _StopLoop


def _make_client() -> fc.FirebaseClient:
    return fc.FirebaseClient("https://example.test", "secret", "A")


def _patch_env(monkeypatch, clock: _Clock, sleeper: _SleepRecorder, lives):
    """מזריק שעון מזויף, sleep מוקלט, jitter אפס וזרם SSE מדומה.

    lives - אורך-חיים בשניות לכל חיבור בתורו (האחרון חוזר על עצמו). כל חיבור
    מניב פריט אחד (כמו ה-put הראשוני של Firebase) ואז נופל.
    """
    monkeypatch.setattr(fc.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(fc.time, "sleep", sleeper)
    monkeypatch.setattr(fc.random, "uniform", lambda a, b: 0.0)

    calls = {"i": 0}

    def fake_sse(self, url):
        i = calls["i"]
        calls["i"] += 1
        life = lives[min(i, len(lives) - 1)]
        clock.t += life
        yield {"floor": str(i), "timestamp": 1000 + i}
        raise ConnectionError("stream died")

    monkeypatch.setattr(fc.FirebaseClient, "_sse_stream", fake_sse)


class TestStreamLoopBackoff:
    def test_flapping_stream_backoff_grows_to_cap(self, monkeypatch):
        # חיבור שנופל שנייה אחרי שהתחבר - למרות שהתקבל בו נתון (ה-put
        # הראשוני), ה-backoff חייב לטפס: 1,2,4,...,30,30 (חצי-בסיס כש-jitter=0).
        clock, sleeper = _Clock(), _SleepRecorder(stop_after=7)
        _patch_env(monkeypatch, clock, sleeper, lives=[1.0])
        client = _make_client()
        with pytest.raises(_StopLoop):
            client._stream_loop("settings", lambda data: None)
        assert sleeper.delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]

    def test_healthy_stream_resets_backoff(self, monkeypatch):
        # רועד, רועד, בריא (מעל הסף), רועד - האיפוס קורה רק אחרי הבריא.
        clock, sleeper = _Clock(), _SleepRecorder(stop_after=4)
        _patch_env(monkeypatch, clock, sleeper,
                   lives=[1.0, 1.0, fc._STREAM_HEALTHY_S + 1.0, 1.0])
        client = _make_client()
        with pytest.raises(_StopLoop):
            client._stream_loop("settings", lambda data: None)
        assert sleeper.delays == [1.0, 2.0, 1.0, 2.0]

    def test_callback_still_receives_data(self, monkeypatch):
        clock, sleeper = _Clock(), _SleepRecorder(stop_after=3)
        _patch_env(monkeypatch, clock, sleeper, lives=[1.0])
        received = []
        client = _make_client()
        with pytest.raises(_StopLoop):
            client._stream_loop("settings", received.append)
        assert len(received) == 3


class TestElevatorStreamBackoff:
    def _drain(self, client):
        for _ in client.stream_elevator_events():
            pass

    def test_flapping_elevator_stream_backoff_grows(self, monkeypatch):
        # גם כשכל חיבור מניב אירוע-קומה חדש (yield אמיתי) - חיבור קצר-חיים
        # לא מאפס את המונה.
        clock, sleeper = _Clock(), _SleepRecorder(stop_after=5)
        _patch_env(monkeypatch, clock, sleeper, lives=[1.0])
        client = _make_client()
        with pytest.raises(_StopLoop):
            self._drain(client)
        assert sleeper.delays == [1.0, 2.0, 4.0, 8.0, 16.0]

    def test_healthy_elevator_stream_resets(self, monkeypatch):
        clock, sleeper = _Clock(), _SleepRecorder(stop_after=3)
        _patch_env(monkeypatch, clock, sleeper,
                   lives=[1.0, fc._STREAM_HEALTHY_S + 5.0, 1.0])
        client = _make_client()
        with pytest.raises(_StopLoop):
            self._drain(client)
        assert sleeper.delays == [1.0, 1.0, 2.0]

    def test_yields_only_on_floor_change(self, monkeypatch):
        # דדופ הקומות נשמר: אותה קומה פעמיים = אירוע אחד.
        clock = _Clock()
        sleeper = _SleepRecorder(stop_after=1)
        monkeypatch.setattr(fc.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(fc.time, "sleep", sleeper)
        monkeypatch.setattr(fc.random, "uniform", lambda a, b: 0.0)

        def fake_sse(self, url):
            clock.t += 1.0
            yield {"floor": "3"}
            yield {"floor": "3"}
            yield {"floor": "4"}
            raise ConnectionError("died")

        monkeypatch.setattr(fc.FirebaseClient, "_sse_stream", fake_sse)
        client = _make_client()
        seen = []
        with pytest.raises(_StopLoop):
            for raw in client.stream_elevator_events():
                seen.append(raw["floor"])
        assert seen == ["3", "4"]
