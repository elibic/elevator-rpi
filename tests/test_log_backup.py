"""Tests for config sanitization and disposable-clone recovery in log_backup.

The security contract: the sanitized config snapshot pushed to the (possibly
shared/public) backup repo must preserve the RFID tag mapping and non-secret
settings, and must NEVER contain the SECRET_KEY, a PAT/token, or any other
credential - the repo's hard rule. Git recovery tests use local repositories
only, without network access.
"""
import json
import subprocess

import pytest

from shabbat_detector import log_backup

SECRET = "super-secret-key-value-123"
PAT = "ghp_PATVALUE123456789"


def _sample_config() -> dict:
    """A realistic rfid_config.json: tag mapping + a mix of secret and
    non-secret settings, including the SECRET_KEY leaking into a free-text field."""
    return {
        "tags": {
            "00E2004707DC5060270EA801": "0",
            "00E28011704000021CEE45EB": "1",
            "00E2004707DCD060270EB001": "2",
            "00E2004707DD1060270EB401": "L",
        },
        "settings": {
            "SERIAL_PORT": "/dev/ttyUSB0",
            "BAUDRATE": 115200,
            "ELEVATOR_ID": "A",
            "FIREBASE_URL": "https://ramada-elev-default-rtdb.europe-west1.firebasedatabase.app/elevators.json",
            "SECRET_KEY": SECRET,
            "TIME_PER_FLOOR": 30,
            "FLEET_AUTH_TOKEN": "fleet-token-abc",
            "LOG_BACKUP_REPO_URL": f"https://x-access-token:{PAT}@github.com/elibic/elevator-logs.git",
            "NOTE": "remember the key is " + SECRET,
        },
    }


def test_sanitize_keeps_tags_and_nonsecret_settings():
    safe = log_backup._sanitize_config(_sample_config(), SECRET)
    # The tag mapping is the whole reason for the feature - preserved verbatim.
    assert safe["tags"] == _sample_config()["tags"]
    s = safe["settings"]
    assert s["SERIAL_PORT"] == "/dev/ttyUSB0"
    assert s["BAUDRATE"] == 115200
    assert s["ELEVATOR_ID"] == "A"
    assert s["TIME_PER_FLOOR"] == 30
    # The public Firebase URL carries no credential -> kept as-is.
    assert s["FIREBASE_URL"].endswith("/elevators.json")


def test_sanitize_redacts_every_secret():
    safe = log_backup._sanitize_config(_sample_config(), SECRET)
    blob = json.dumps(safe, ensure_ascii=False)
    # No secret material may appear anywhere in the sanitized output.
    assert SECRET not in blob
    assert PAT not in blob
    assert "fleet-token-abc" not in blob
    s = safe["settings"]
    assert s["SECRET_KEY"] == log_backup._REDACTED          # redacted by key name
    assert s["FLEET_AUTH_TOKEN"] == log_backup._REDACTED    # ...token... in the name
    # The repo URL keeps its shape but loses the embedded PAT.
    assert s["LOG_BACKUP_REPO_URL"].startswith("https://***@github.com/")
    # A secret pasted into a non-secret-named field is scrubbed by value too.
    assert SECRET not in s["NOTE"]


def test_sanitize_redacts_secret_named_keys_of_any_type():
    cfg = {"settings": {"API_KEY": 12345, "some_password": True,
                        "nested": {"auth_token": "x"}, "PLAIN": "ok"}}
    s = log_backup._sanitize_config(cfg, "")["settings"]
    assert s["API_KEY"] == log_backup._REDACTED
    assert s["some_password"] == log_backup._REDACTED
    assert s["nested"]["auth_token"] == log_backup._REDACTED
    assert s["PLAIN"] == "ok"


def test_write_snapshot_deterministic_and_scrubbed(tmp_path):
    cfg_path = tmp_path / "rfid_config.json"
    cfg_path.write_text(json.dumps(_sample_config()), encoding="utf-8")
    dest = tmp_path / "dest"

    assert log_backup._write_config_snapshot(str(cfg_path), str(dest), SECRET) is True
    snap = dest / "config" / "rfid_config.sanitized.json"
    assert snap.is_file()

    body = snap.read_text(encoding="utf-8")
    assert SECRET not in body and PAT not in body
    # Round-trips as JSON with the tag mapping intact.
    assert json.loads(body)["tags"] == _sample_config()["tags"]

    # Deterministic: re-running on the same config yields byte-identical output
    # (so an unchanged config produces no spurious commit in the backup repo).
    log_backup._write_config_snapshot(str(cfg_path), str(dest), SECRET)
    assert snap.read_text(encoding="utf-8") == body


def test_write_snapshot_fail_closed(tmp_path):
    dest = tmp_path / "dest"
    # Missing file -> skip, write nothing.
    assert log_backup._write_config_snapshot(str(tmp_path / "nope.json"), str(dest), SECRET) is False
    assert not (dest / "config").exists()
    # Invalid JSON -> skip, never fall back to writing the raw file.
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    assert log_backup._write_config_snapshot(str(bad), str(dest), SECRET) is False
    assert not (dest / "config").exists()
    # Empty path -> False.
    assert log_backup._write_config_snapshot("", str(dest), SECRET) is False


def test_backup_logs_requires_repo_url():
    # Earliest guard - returns before touching git or the filesystem.
    ok, detail = log_backup.backup_logs({}, "A", "/nonexistent/logs", "/nonexistent/config.json")
    assert ok is False
    assert "LOG_BACKUP_REPO_URL" in detail


def _git(work, *args):
    return subprocess.run(
        ["git", "-C", str(work), "-c", "user.name=backup-test",
         "-c", "user.email=backup-test@example.invalid", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.mark.parametrize("implicit_tracking", [True, False])
def test_backup_recovers_after_origin_history_rewrite(tmp_path, monkeypatch, implicit_tracking):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main")
    upstream = tmp_path / "upstream"
    _git(tmp_path, "clone", str(origin), str(upstream))
    (upstream / "old-history.txt").write_text("discarded history\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "Original history")
    _git(upstream, "push", "origin", "HEAD:main")

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "tracker.log").write_text("before rewrite\n")
    work = tmp_path / "backup"
    settings = {"LOG_BACKUP_REPO_URL": str(origin), "LOG_BACKUP_DIR": str(work),
                "LOG_BACKUP_PREFIX": "ramada"}
    assert log_backup.backup_logs(settings, "C", str(logs))[0]
    stale = _git(work, "rev-parse", "origin/main")

    _git(upstream, "checkout", "--orphan", "rewritten")
    _git(upstream, "rm", "-rf", ".")
    other_pi = upstream / "ramada" / "A"
    other_pi.mkdir(parents=True)
    (other_pi / "tracker.log").write_text("keep another Pi's logs\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "Rewritten remote history")
    rewritten = _git(upstream, "rev-parse", "HEAD")
    _git(upstream, "push", "--force", "origin", "HEAD:main")
    assert subprocess.run(["git", "-C", str(origin), "merge-base", "--is-ancestor",
                           stale, rewritten], capture_output=True).returncode == 1
    if not implicit_tracking:
        # A cached origin/main must refresh even without an implicit fetch map.
        _git(work, "config", "--unset-all", "remote.origin.fetch")
    assert _git(work, "rev-parse", "origin/main") == stale
    (logs / "tracker.log").write_text("after rewrite\n")
    real_run = log_backup._run
    pushes = []
    def tracked_run(args, cwd=None):
        if "push" in args:
            pushes.append(args)
        return real_run(args, cwd=cwd)
    monkeypatch.setattr(log_backup, "_run", tracked_run)
    monkeypatch.setattr(log_backup.time, "sleep", lambda _: None)

    ok, detail = log_backup.backup_logs(settings, "C", str(logs))

    assert ok, detail
    assert len(pushes) == 1
    assert _git(origin, "rev-parse", "main^") == rewritten
    assert _git(origin, "show", "main:ramada/C/tracker.log") == "after rewrite"
    assert _git(origin, "show", "main:ramada/A/tracker.log") == "keep another Pi's logs"
    assert not (work / "old-history.txt").exists()


def test_sync_fetch_failure_does_not_use_stale_ref(tmp_path, monkeypatch):
    calls = []
    def failed_fetch(args, cwd=None):
        calls.append(args)
        if "fetch" in args:
            return 1, "network unavailable"
        return 0, "cached-ref"
    monkeypatch.setattr(log_backup, "_run", failed_fetch)

    ok, detail = log_backup._sync_to_origin(str(tmp_path), "main")

    assert not ok
    assert "fetch failed" in detail and "network unavailable" in detail
    assert not any("reset" in args or "checkout" in args for args in calls)


def test_backup_to_empty_origin(tmp_path):
    origin = tmp_path / "empty.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "tracker.log").write_text("first backup\n")
    settings = {"LOG_BACKUP_REPO_URL": str(origin),
                "LOG_BACKUP_DIR": str(tmp_path / "backup"),
                "LOG_BACKUP_PREFIX": "ramada"}

    ok, detail = log_backup.backup_logs(settings, "C", str(logs))

    assert ok, detail
    assert _git(origin, "show", "main:ramada/C/tracker.log") == "first backup"
