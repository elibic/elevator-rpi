"""
בדיקות יחידה למנגנון ההתראות: חישוב "לא כולל לילה", MovementWatchdog, ו-Notifier.
הרצה:  python tests/test_notifier.py     (או pytest)
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabbat_detector.notifier import (  # noqa: E402
    MovementWatchdog, Notifier, daytime_seconds_between, night_seconds_between,
)

H = 3600


def _ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi).timestamp()


def test_daytime_fully_in_day():
    start, end = _ts(2026, 6, 21, 10), _ts(2026, 6, 21, 14)
    assert abs(daytime_seconds_between(start, end) - 4 * H) < 1


def test_daytime_fully_in_night():
    start, end = _ts(2026, 6, 21, 0), _ts(2026, 6, 21, 5)
    assert daytime_seconds_between(start, end) == 0


def test_daytime_crossing_night():
    # 22:00 → 08:00 למחרת = 10 שעות; לילה 23:00–06:00 = 7 שעות → יום = 3 שעות
    start, end = _ts(2026, 6, 21, 22), _ts(2026, 6, 22, 8)
    assert abs(daytime_seconds_between(start, end) - 3 * H) < 1
    assert abs(night_seconds_between(start, end) - 7 * H) < 1


def test_no_night_when_equal():
    start, end = _ts(2026, 6, 21, 0), _ts(2026, 6, 22, 0)
    assert night_seconds_between(start, end, "00:00", "00:00") == 0


def test_watchdog_fires_once_after_threshold():
    wd = MovementWatchdog(threshold_hours=10, now=_ts(2026, 6, 21, 8))
    assert wd.check(now=_ts(2026, 6, 21, 17)) is False   # 9h יום
    assert wd.check(now=_ts(2026, 6, 21, 18, 30)) is True  # 10.5h יום → יורה
    assert wd.check(now=_ts(2026, 6, 21, 20)) is False    # כבר התריע


def test_watchdog_quiet_night_no_false_alarm():
    # 22:00 → 07:00 = 9h, מתוכן 7h לילה → רק 2h יום, מתחת לסף 10
    wd = MovementWatchdog(threshold_hours=10, now=_ts(2026, 6, 21, 22))
    assert wd.check(now=_ts(2026, 6, 22, 7)) is False


def test_watchdog_reset_on_movement():
    wd = MovementWatchdog(threshold_hours=10, now=_ts(2026, 6, 21, 8))
    assert wd.check(now=_ts(2026, 6, 21, 19)) is True
    wd.record_movement(_ts(2026, 6, 21, 19))
    assert wd.alerted is False
    assert wd.check(now=_ts(2026, 6, 21, 20)) is False


class _FakeSender:
    def __init__(self):
        self.name = "fake"
        self.messages = []

    def send(self, subject, body):
        self.messages.append((subject, body))


def _notifier_with_fake():
    n = Notifier({"enabled": True, "events": {"shabbat_enter": True, "shabbat_exit": True,
                                              "no_movement": True}}, "A")
    fake = _FakeSender()
    n._senders = [fake]
    return n, fake


def test_notify_shabbat_change():
    n, fake = _notifier_with_fake()
    n.notify_shabbat_change(True, "מחזור תאם")
    n.notify_shabbat_change(False, "חריגות")
    assert len(fake.messages) == 2
    assert "שבת" in fake.messages[0][0]


def test_events_filtering():
    n, fake = _notifier_with_fake()
    n.events = {"shabbat_enter": False, "shabbat_exit": True, "no_movement": True}
    n.notify_shabbat_change(True, "")   # מסונן
    n.notify_shabbat_change(False, "")  # נשלח
    assert len(fake.messages) == 1


def test_disabled_sends_nothing():
    n, fake = _notifier_with_fake()
    n.enabled = False
    n.notify_shabbat_change(True, "")
    assert fake.messages == []


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} עברו")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
