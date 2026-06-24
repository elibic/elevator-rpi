"""
installer/web.py — הכלי הגרפי בדפדפן (Flask, localhost בלבד).

שלושה חלקים:
  • אשף התקנה (/)            — מריץ את צעדי core.Installer עם progress חי (SSE).
  • הגדרה (/config)         — Firebase, מיפוי תגים (סריקה חיה).
  • דשבורד (/dashboard)     — סטטוס חי, ניהול שירותים, עדכון מגיט, לוגים.

רץ תחת sudo (נדרש ל-systemctl/התקנה) ומאזין רק על 127.0.0.1 — כלי אדמין מקומי.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser

from flask import Flask, Response, jsonify, render_template, request

from . import core

_HERE = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, template_folder=os.path.join(_HERE, "templates"),
            static_folder=os.path.join(_HERE, "static"))

# מצב משותף לאפליקציה (תהליך יחיד, localhost)
_state: dict = {"dry_run": False, "mock_serial": False, "env": None, "job": None}


def _installer(progress=None) -> core.Installer:
    return core.Installer(_state["env"], dry_run=_state["dry_run"], progress=progress)


@app.after_request
def _no_cache(resp):
    """כלי אדמין מקומי — תמיד טרי, בלי קאש דפדפן (מונע "תקוע על גרסה ישנה")."""
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ── דפים ──────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/wizard")
def wizard_page():
    return render_template("wizard.html")


@app.route("/config")
def config_page():
    return render_template("config.html")


@app.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html")


# ── API: סביבה והגדרות ────────────────────────────────────────────────────────
@app.route("/api/env")
def api_env():
    env = _state["env"]
    return jsonify({
        "user": env.user, "project_dir": env.project_dir, "is_pi": env.is_pi,
        "is_root": env.is_root, "serial_present": env.serial_present,
        "git_branch": env.git_branch, "dry_run": _state["dry_run"],
    })


@app.route("/api/config", methods=["GET"])
def api_get_config():
    cfg = _installer().load_config()
    cfg.pop("_comment", None)
    return jsonify({
        "settings": cfg.get("settings", {}),
        "tags": cfg.get("tags", {}),
    })


@app.route("/api/config", methods=["POST"])
def api_save_config():
    body = request.get_json(force=True) or {}
    res = _installer().write_config(
        body.get("settings", {}), body.get("tags", {}),
    )
    return jsonify({"ok": res.ok, "detail": res.detail})


# ── API: סריקת תג ─────────────────────────────────────────────────────────────
@app.route("/api/scan-tag", methods=["POST"])
def api_scan_tag():
    if _state["mock_serial"]:
        time.sleep(0.5)
        return jsonify({"tag": "00E2000000000000MOCK0001"})
    tag = _installer().scan_tag(timeout_s=15)
    return jsonify({"tag": tag})


# ── API: התקנה (SSE progress) ─────────────────────────────────────────────────
@app.route("/api/install/start", methods=["POST"])
def api_install_start():
    body = request.get_json(force=True) or {}
    q: queue.Queue = queue.Queue()

    def progress(msg, level="info"):
        q.put({"msg": msg, "level": level})

    def worker():
        inst = _installer(progress=progress)
        try:
            results = inst.install_all(
                body.get("settings", {}), body.get("tags", {}),
            )
            ok = all(r.ok for r in results)
            for r in results:
                progress(f"{r.name}: {'OK' if r.ok else 'נכשל — ' + r.detail}",
                         "ok" if r.ok else "error")
            progress("ההתקנה הסתיימה" + (" בהצלחה ✓" if ok else " עם שגיאות"),
                     "done" if ok else "error")
        except Exception as e:
            progress(f"שגיאה בהתקנה: {e}", "error")
            progress("ההתקנה נכשלה", "error")
        finally:
            q.put(None)  # sentinel

    t = threading.Thread(target=worker, daemon=True)
    _state["job"] = {"queue": q, "thread": t}
    t.start()
    return jsonify({"ok": True})


@app.route("/api/install/stream")
def api_install_stream():
    job = _state.get("job")
    if not job:
        return Response("data: {}\n\n", mimetype="text/event-stream")
    q: queue.Queue = job["queue"]

    def gen():
        while True:
            item = q.get()
            if item is None:
                yield "event: end\ndata: {}\n\n"
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return Response(gen(), mimetype="text/event-stream")


# ── API: דשבורד / ניהול ───────────────────────────────────────────────────────
@app.route("/api/status")
def api_status():
    inst = _installer()
    out = {"services": inst.all_status()}
    # סטטוס חי מ-Firebase (שימוש חוזר במונה monitor.py)
    try:
        if _state["env"].project_dir not in sys.path:
            sys.path.insert(0, _state["env"].project_dir)
        import monitor  # type: ignore
        cfg = inst.load_config().get("settings", {})
        base, eid = monitor._parse_config(inst.config_path())
        if base and eid:
            out["live"] = monitor.fetch_status(base, eid)
            out["elevator_id"] = eid
    except Exception as e:
        out["live_error"] = str(e)
    return jsonify(out)


@app.route("/api/service/<name>/<action>", methods=["POST"])
def api_service(name, action):
    try:
        ok = _installer().service_action(name, action)
        return jsonify({"ok": ok})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/update-git", methods=["POST"])
def api_update_git():
    res = _installer().update_from_git()
    return jsonify({"ok": res.ok, "detail": res.detail})


@app.route("/api/rpi-connect/status")
def api_rpi_connect_status():
    return jsonify(_installer().rpi_connect_status())


@app.route("/api/rpi-connect/signin", methods=["POST"])
def api_rpi_connect_signin():
    url = _installer().rpi_connect_signin_url()
    return jsonify({"url": url})


@app.route("/api/logs")
def api_logs():
    env = _state["env"]
    logs_dir = os.path.join(env.project_dir, "logs")
    files = {"tracker": "rfid_tracker.log", "detector": "shabbat_detector.log"}
    out = {}
    for key, fname in files.items():
        path = os.path.join(logs_dir, fname)
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                out[key] = f.readlines()[-200:]
        except Exception:
            out[key] = []
    return jsonify(out)


def _open_browser(url: str) -> None:
    """פותח את הדפדפן כמשתמש שולחן-העבודה האמיתי (לא root) — אחרת, תחת sudo,
    הדפדפן לא נפתח (אין גישה ל-display של המשתמש). תומך X11 ו-Wayland."""
    sudo_user = os.environ.get("SUDO_USER")
    try:
        if hasattr(os, "geteuid") and os.geteuid() == 0 and sudo_user and sudo_user != "root":
            import pwd
            uid = pwd.getpwnam(sudo_user).pw_uid
            env_pairs = [
                f"DISPLAY={os.environ.get('DISPLAY', ':0')}",
                f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY', 'wayland-0')}",
                f"XDG_RUNTIME_DIR=/run/user/{uid}",
            ]
            subprocess.Popen(["sudo", "-u", sudo_user, "env", *env_pairs, "xdg-open", url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    except Exception:
        pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def run_web(port: int = 8080, dry_run: bool = False,
            open_browser: bool = True, mock_serial: bool = False) -> None:
    _state["dry_run"] = dry_run
    _state["mock_serial"] = mock_serial
    _state["env"] = core.detect_environment()

    url = f"http://127.0.0.1:{port}/"
    print(f"הכלי הגרפי זמין בכתובת: {url}", flush=True)
    if open_browser:
        threading.Timer(1.2, lambda: _open_browser(url)).start()
    # localhost בלבד — כלי אדמין מקומי
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
