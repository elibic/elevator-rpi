"""
בדיקות יחידה ל-FleetAgent: אימות secret_key, idempotency לפי requested_at,
וזרימת העדכון (success / failed / up_to_date / disabled).
הרצה:  python tests/test_fleet_agent.py     (או pytest)
"""
import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shabbat_detector.fleet_agent as fa  # noqa: E402
from shabbat_detector.fleet_agent import FleetAgent  # noqa: E402


class FakeFB:
    """תחליף ל-FirebaseClient — לוכד כתיבות /fleet בלי רשת."""

    def __init__(self, fleet=None):
        self.patches = []
        self.fleet = dict(fleet or {})
        self.subscribed = None

    def patch_fleet_status(self, updates):
        self.patches.append(dict(updates))
        return True

    def get_fleet_status(self):
        return dict(self.fleet)

    def subscribe_fleet_command(self, cb):
        self.subscribed = cb


@contextmanager
def patched(**names):
    """מחליף זמנית פונקציות ברמת-מודול (local_version/_git) ומשחזר בסוף."""
    saved = {k: getattr(fa, k) for k in names}
    try:
        for k, v in names.items():
            setattr(fa, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(fa, k, v)


def _agent(**kw):
    fb = FakeFB(kw.pop("fleet", None))
    kw.setdefault("update_branch", "main")
    kw.setdefault("secret_key", "S3CR3T")
    kw.setdefault("restart_callback", lambda: None)
    return FleetAgent(fb, **kw), fb


def _statuses(fb):
    return [p["update_status"] for p in fb.patches if p.get("update_status")]


# ── should_handle: אימות + idempotency (לוגיקה טהורה) ─────────────────────────

def test_should_handle_accepts_valid():
    agent, _ = _agent()
    assert agent.should_handle({"action": "update", "secret_key": "S3CR3T", "requested_at": 100}) is True


def test_should_handle_rejects_bad_secret():
    agent, _ = _agent()
    assert agent.should_handle({"action": "update", "secret_key": "WRONG", "requested_at": 100}) is False


def test_should_handle_rejects_missing_secret():
    agent, _ = _agent()
    assert agent.should_handle({"action": "update", "requested_at": 100}) is False


def test_should_handle_rejects_non_update_action():
    agent, _ = _agent()
    assert agent.should_handle({"action": "reboot", "secret_key": "S3CR3T", "requested_at": 100}) is False


def test_should_handle_rejects_non_dict():
    agent, _ = _agent()
    assert agent.should_handle(None) is False
    assert agent.should_handle("update") is False


def test_should_handle_idempotent_requested_at():
    agent, _ = _agent()
    agent._last_requested_at = 100
    assert agent.should_handle({"action": "update", "secret_key": "S3CR3T", "requested_at": 100}) is False
    assert agent.should_handle({"action": "update", "secret_key": "S3CR3T", "requested_at": 99}) is False
    assert agent.should_handle({"action": "update", "secret_key": "S3CR3T", "requested_at": 101}) is True


def test_secret_not_required_bypass():
    agent, _ = _agent(secret_key="", require_command_secret=False)
    assert agent.should_handle({"action": "update", "requested_at": 1}) is True


# ── דיווח ─────────────────────────────────────────────────────────────────────

def test_report_payload_shape():
    agent, fb = _agent()
    with patched(local_version=lambda *a, **k: "deadbee"):
        agent._report()
    last = fb.patches[-1]
    assert last["status"] == "online"
    assert last["version"] == "deadbee"
    assert last["branch"] == "main"
    assert "last_seen" in last and "last_seen_str" in last


# ── זרימת העדכון ──────────────────────────────────────────────────────────────

def test_update_success_triggers_restart():
    restarted = []
    agent, fb = _agent(restart_callback=lambda: restarted.append(True))
    state = {"v": "aaa1111"}

    def fake_git(args, cwd=None, timeout=None):
        if args and args[0] == "pull":
            state["v"] = "bbb2222"
            return True, "Updating aaa1111..bbb2222"
        return True, ""

    with patched(local_version=lambda *a, **k: state["v"], _git=fake_git):
        agent._on_command({"action": "update", "secret_key": "S3CR3T", "requested_at": 10})

    assert restarted == [True]
    assert "updating" in _statuses(fb)
    assert "restarting" in _statuses(fb)
    assert agent._last_requested_at == 10
    # הדיווח האחרון נושא את הגרסה החדשה
    assert fb.patches[-1]["version"] == "bbb2222"


def test_update_failure_no_restart_but_marks_idempotency():
    restarted = []
    agent, fb = _agent(restart_callback=lambda: restarted.append(True))

    def fake_git(args, cwd=None, timeout=None):
        if args and args[0] == "pull":
            return False, "fatal: Not possible to fast-forward, aborting."
        return True, ""

    with patched(local_version=lambda *a, **k: "aaa1111", _git=fake_git):
        agent._on_command({"action": "update", "secret_key": "S3CR3T", "requested_at": 20})

    assert restarted == []
    assert "failed" in _statuses(fb)
    # סומן כדי לא להיכנס ללולאת-retry על אותה פקודה כושלת
    assert agent._last_requested_at == 20


def test_update_up_to_date_no_restart():
    restarted = []
    agent, fb = _agent(restart_callback=lambda: restarted.append(True))

    def fake_git(args, cwd=None, timeout=None):
        return True, "Already up to date."

    with patched(local_version=lambda *a, **k: "aaa1111", _git=fake_git):
        agent._on_command({"action": "update", "secret_key": "S3CR3T", "requested_at": 30})

    assert restarted == []
    assert "up_to_date" in _statuses(fb)


def test_update_command_when_disabled():
    restarted = []
    agent, fb = _agent(remote_update_enabled=False,
                       restart_callback=lambda: restarted.append(True))
    agent._on_command({"action": "update", "secret_key": "S3CR3T", "requested_at": 5})
    assert restarted == []
    assert _statuses(fb) == ["disabled"]


def test_old_command_ignored_after_handling():
    """פקודה עם requested_at ישן (כמו ב-reconnect/restart) לא תרוץ שוב."""
    agent, fb = _agent()
    agent._last_requested_at = 10  # כאילו כבר טופלה פקודה 10

    def fake_git(args, cwd=None, timeout=None):
        raise AssertionError("git should not run for an old command")

    with patched(local_version=lambda *a, **k: "aaa1111", _git=fake_git):
        agent._on_command({"action": "update", "secret_key": "S3CR3T", "requested_at": 10})
    assert _statuses(fb) == []


def test_start_seeds_idempotency_and_subscribes():
    agent, fb = _agent(fleet={"last_applied_requested_at": 555})
    with patched(local_version=lambda *a, **k: "x"):
        agent.start()
        agent.stop()
    assert agent._last_requested_at == 555
    assert fb.subscribed == agent._on_command  # bound-method equality (not identity)


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
