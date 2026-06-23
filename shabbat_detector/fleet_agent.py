"""
Fleet agent — version heartbeat + secret-gated remote update for a single Pi.

This closes the loop with the admin dashboard (``admin-dashboard/public/
admin-dashboard.js``): the dashboard *shows* a "version / online" column and
*writes* update commands to ``/fleet/{id}/command`` — but until now nothing on
the Pi wrote to or read from ``/fleet``. This agent does both.

Data model (must stay in sync with the dashboard)
-------------------------------------------------
* **Heartbeat** — every ``FLEET_REPORT_INTERVAL`` seconds (and once at startup)::

      PATCH /fleet/{ELEVATOR_ID}
          { "version": "<YYYY.MM.DD>", "commit": "<short-sha>",
            "last_seen": <epoch>, "status": "online", "secret_key": "<…>" }

  The dashboard marks a Pi *offline* when ``now - last_seen > 660`` and *behind*
  when ``version !== LATEST_VERSION`` (a ``YYYY.MM.DD`` string it holds).

* **Command** — the dashboard writes::

      /fleet/{ELEVATOR_ID}/command =
          { "action": "update", "secret_key": "<…>", "requested_at": <epoch> }

  The agent validates ``secret_key`` against the local ``SECRET_KEY`` (the same
  bearer-token model as ``firebase_client.patch_*``), runs the update command
  (default ``./setup.sh``), reports ``update_status``, and clears the command.

Replay safety
-------------
The agent acts on a command only when its ``requested_at`` is newer than the
last one it executed (persisted to a state file) **and** deletes the command
node after acting. A reconnect, a service restart, or ``setup.sh`` restarting
the agent therefore never re-runs the same command. If the agent is killed
*during* an update (``setup.sh`` restart / power loss), a ``pending_update``
marker written before launch lets it reconcile and report success on the next
boot.

Security note
-------------
A valid command makes the Pi run ``./setup.sh`` (``git pull`` + reinstall) and
restart services — so the agent runs as **root**. The only thing standing
between a caller and code execution on the Pi is the ``secret_key`` and the
Realtime-Database security rules on ``/fleet``. See
``docs/fleet-remote-update.md`` for the required per-project rules.

Run::

    python -m shabbat_detector.fleet_agent --config rfid_config.json
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Optional
from urllib.parse import urlsplit

import requests

try:                       # Linux only; absent on dev machines (Windows simulators)
    import pwd
except ImportError:        # pragma: no cover
    pwd = None  # type: ignore

from .state_persistence import StatePersistence

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fleet_agent")

# ── Defaults (all overridable via rfid_config.json → settings) ────────────────
_DEFAULT_REPORT_INTERVAL = 300.0    # heartbeat cadence (< dashboard's 660s stale)
_DEFAULT_POLL_INTERVAL = 15.0       # how often we look for a new command
_UPDATE_TIMEOUT_S = 1800            # hard cap on a single setup.sh run


# ── Version detection ─────────────────────────────────────────────────────────
def _git(repo_dir: str, args: list[str]) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", repo_dir, *args],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


def detect_version(repo_dir: str) -> str:
    """The version string reported to the dashboard.

    Default: the commit date of ``HEAD`` as ``YYYY.MM.DD`` — it matches the
    dashboard's ``LATEST_VERSION`` format and advances automatically after each
    ``git pull``. An optional ``VERSION`` file in the repo root overrides it.
    """
    vf = os.path.join(repo_dir, "VERSION")
    try:
        if os.path.isfile(vf):
            with open(vf, encoding="utf-8") as f:
                v = f.read().strip()
            if v:
                return v
    except Exception:
        pass
    return _git(repo_dir, ["log", "-1", "--format=%cd", "--date=format:%Y.%m.%d"]) or "unknown"


def detect_commit(repo_dir: str) -> str:
    return _git(repo_dir, ["rev-parse", "--short", "HEAD"]) or "unknown"


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truthy(value, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


# ── Minimal /fleet REST client ────────────────────────────────────────────────
class FleetClient:
    """Tiny REST wrapper for ``/fleet/{id}`` — mirrors ``firebase_client`` style.

    Writes carry ``secret_key`` in the body (the existing bearer-token model).
    An optional ``auth_token`` is appended as ``?auth=`` when the project's RTDB
    rules require authentication (recommended hardening — see the docs).
    """

    def __init__(self, base_url: str, secret_key: str, elevator_id: str,
                 auth_token: str = "") -> None:
        self._node = f"{base_url.rstrip('/')}/fleet/{elevator_id}"
        self._key = secret_key
        self._auth = auth_token or ""

    def _url(self, suffix: str = "") -> str:
        url = f"{self._node}{suffix}.json"
        return url + (f"?auth={self._auth}" if self._auth else "")

    def patch(self, fields: dict) -> bool:
        """PATCH /fleet/{id} (shallow merge) with secret_key appended."""
        payload = {**fields, "secret_key": self._key}
        try:
            r = requests.patch(self._url(), data=json.dumps(payload), timeout=10)
            r.raise_for_status()
            return True
        except Exception as e:
            log.error("fleet PATCH failed: %s", e)
            return False

    def get_command(self) -> Optional[dict]:
        try:
            r = requests.get(self._url("/command"), timeout=10)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, dict) else None
        except Exception as e:
            log.debug("fleet GET command failed: %s", e)
            return None

    def clear_command(self) -> bool:
        # PATCH command:null carries secret_key in the body, so it works even
        # when the rules forbid an unauthenticated DELETE. Shallow-merge leaves
        # version/last_seen untouched.
        return self.patch({"command": None})


# ── Agent ─────────────────────────────────────────────────────────────────────
class FleetAgent:
    def __init__(self, *, client: FleetClient, secret_key: str, elevator_id: str,
                 repo_dir: str, settings: dict, persistence: StatePersistence,
                 state: dict, test_mode: bool = False) -> None:
        self._client = client
        self._key = secret_key
        self._id = elevator_id
        self._repo = repo_dir
        self._persistence = persistence
        self._state = state
        self._test_mode = test_mode

        self._report_interval = float(settings.get("FLEET_REPORT_INTERVAL", _DEFAULT_REPORT_INTERVAL))
        self._poll_interval = float(settings.get("FLEET_POLL_INTERVAL", _DEFAULT_POLL_INTERVAL))
        self._self_restart = _truthy(settings.get("FLEET_SELF_RESTART"), True)
        # FLEET_UPDATE_COMMAND: str (shell) | list (argv) | None (default ./setup.sh)
        self._update_cmd = settings.get("FLEET_UPDATE_COMMAND")

    # ── state ────────────────────────────────────────────────────────────────
    def _persist(self) -> None:
        self._persistence.save(self._state, force=True)

    # ── reporting ────────────────────────────────────────────────────────────
    def _report(self, status: str = "online", **extra) -> None:
        fields = {
            "version": detect_version(self._repo),
            "commit": detect_commit(self._repo),
            "last_seen": int(time.time()),
            "status": status,
            **extra,
        }
        if self._test_mode:
            log.info("[test] PATCH /fleet/%s %s", self._id, fields)
            return
        self._client.patch(fields)

    # ── main loop ────────────────────────────────────────────────────────────
    def run(self) -> None:
        self._reconcile()           # finish a previously-interrupted update
        self._report("online")      # immediate heartbeat on boot
        last_report = time.monotonic()
        log.info("Fleet agent ready for elevator %s (report=%ss, poll=%ss)",
                 self._id, int(self._report_interval), int(self._poll_interval))
        while True:
            try:
                self._check_command()
            except Exception as e:
                log.warning("command check failed: %s", e)
            now = time.monotonic()
            if now - last_report >= self._report_interval:
                self._report("online")
                last_report = now
            time.sleep(self._poll_interval)

    # ── command handling ─────────────────────────────────────────────────────
    def _check_command(self) -> None:
        cmd = self._client.get_command()
        if not cmd:
            return
        if cmd.get("action") != "update":
            return  # unknown / no actionable command — ignore

        requested_at = _as_int(cmd.get("requested_at"))
        last_done = _as_int(self._state.get("last_command_requested_at"))

        # Replay guard: already handled (or older than) the last one we ran.
        if requested_at and requested_at <= last_done:
            self._client.clear_command()   # stale duplicate lingering in the DB
            return

        # Authenticate — bearer token, constant-time compare.
        if not hmac.compare_digest(str(cmd.get("secret_key", "")), str(self._key)):
            log.warning("Rejected update command for %s: bad secret_key", self._id)
            self._report("online", update_status="rejected: bad secret_key")
            self._client.clear_command()
            return

        self._execute_update(requested_at)

    def _execute_update(self, requested_at: int) -> None:
        log.info("Authenticated update command (requested_at=%s) — starting", requested_at)
        # Persist BEFORE running: if setup.sh restarts us (or power is lost)
        # mid-update, the reboot must not re-run this command. Reconciliation
        # then reports the result.
        self._state["last_command_requested_at"] = requested_at or int(time.time())
        self._state["pending_update"] = requested_at or int(time.time())
        self._persist()
        self._report("online", update_status="updating")
        self._client.clear_command()       # single-shot: consume the command

        if self._test_mode:
            log.info("[test] would run update command in %s", self._repo)
            self._state["pending_update"] = None
            self._persist()
            self._report("online", update_status="ok")
            return

        rc, detail = self._run_update()
        self._state["pending_update"] = None
        self._persist()
        if rc == 0:
            self._report("online", update_status="ok")
            log.info("Update OK for %s", self._id)
            self._maybe_self_restart()
        else:
            self._report("online", update_status=f"failed: {detail}")
            log.error("Update FAILED for %s: %s", self._id, detail)

    def _run_update(self) -> tuple[int, str]:
        cmd = self._update_cmd or ["./setup.sh"]
        shell = isinstance(cmd, str)
        display = cmd if shell else " ".join(cmd)

        env = dict(os.environ)
        env["FLEET_AGENT_UPDATE"] = "1"    # tells the installer not to restart us
        # setup.sh chowns the repo back to the real user, but only when SUDO_USER
        # is set (it isn't, since we already run as root). Supply the repo owner
        # so the .git chown-back still happens and future `git pull` works.
        owner = self._repo_owner()
        if owner:
            env.setdefault("SUDO_USER", owner)

        log.info("Running update: %s (cwd=%s)", display, self._repo)
        try:
            p = subprocess.run(
                cmd, cwd=self._repo, env=env, shell=shell,
                capture_output=True, text=True, timeout=_UPDATE_TIMEOUT_S,
            )
            if p.returncode != 0:
                lines = (p.stderr or p.stdout or "").strip().splitlines()
                tail = lines[-1] if lines else ""
                return p.returncode, f"rc={p.returncode} {tail}".strip()[:180]
            return 0, "ok"
        except subprocess.TimeoutExpired:
            return 1, f"timeout after {_UPDATE_TIMEOUT_S}s"
        except Exception as e:
            return 1, str(e)[:180]

    def _repo_owner(self) -> str:
        if pwd is None:
            return ""
        try:
            return pwd.getpwuid(os.stat(self._repo).st_uid).pw_name
        except Exception:
            return ""

    def _maybe_self_restart(self) -> None:
        # After a successful git pull the running process still holds the OLD
        # fleet_agent code. Restart our own unit so the new code takes effect.
        # Safe now: the command is cleared and pending_update is None, so the
        # restarted agent re-runs nothing.
        if not self._self_restart or shutil.which("systemctl") is None:
            return
        log.info("Self-restarting fleet-agent to load updated code")
        subprocess.run(["systemctl", "restart", "fleet-agent.service"], check=False)

    def _reconcile(self) -> None:
        pending = _as_int(self._state.get("pending_update"))
        if not pending:
            return
        # We were interrupted mid-update (setup.sh restart / power loss). The
        # pull has run by now; report success with the fresh version and clear.
        log.info("Reconciling interrupted update (requested_at=%s)", pending)
        self._state["pending_update"] = None
        self._persist()
        self._report("online", update_status="ok")
        self._client.clear_command()


# ── Config / entrypoint ───────────────────────────────────────────────────────
def _load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_base(raw_url: str) -> str:
    """Reduce any FIREBASE_URL form to scheme://host (DB root), like the detector."""
    raw_url = (raw_url or "").rstrip("/")
    pu = urlsplit(raw_url)
    return f"{pu.scheme}://{pu.netloc}" if (pu.scheme and pu.netloc) else raw_url


def run(config_path: str = "rfid_config.json", test_mode: bool = False,
        once: bool = False) -> None:
    cfg = _load_config(config_path)
    s = cfg.get("settings", cfg)   # support flat or nested config

    base = _normalize_base(
        s.get("FIREBASE_BASE_URL") or s.get("BASE_FIREBASE_URL") or s.get("FIREBASE_URL", "")
    )
    elevator_id = str(s.get("ELEVATOR_ID", cfg.get("ELEVATOR_ID", "")))
    secret_key = str(s.get("SECRET_KEY", cfg.get("SECRET_KEY", "")))

    if not base or not elevator_id or not secret_key:
        log.error("fleet_agent needs FIREBASE_URL, ELEVATOR_ID and SECRET_KEY in %s", config_path)
        sys.exit(1)

    if not _truthy(s.get("FLEET_ENABLED"), True):
        log.info("FLEET_ENABLED is false — fleet agent idle")
        if once:
            return
        while True:                # stay alive but inert (no crash-loop)
            time.sleep(3600)

    repo_dir = s.get("FLEET_REPO_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    state_dir = s.get("DETECTOR_STATE_DIR", cfg.get("DETECTOR_STATE_DIR"))
    # Separate state file from the detector's (state_fleet_{id}.json).
    persistence = StatePersistence(f"fleet_{elevator_id}", state_dir)
    state = persistence.load() or {"last_command_requested_at": 0, "pending_update": None}

    client = FleetClient(base, secret_key, elevator_id, str(s.get("FLEET_AUTH_TOKEN", "")))
    agent = FleetAgent(
        client=client, secret_key=secret_key, elevator_id=elevator_id,
        repo_dir=repo_dir, settings=s, persistence=persistence, state=state,
        test_mode=test_mode,
    )
    log.info("Fleet agent starting for elevator %s (repo=%s, version=%s, commit=%s)",
             elevator_id, repo_dir, detect_version(repo_dir), detect_commit(repo_dir))

    if once:
        agent._report("online")
        return
    agent.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Elevator Fleet Agent")
    parser.add_argument("--config", default="rfid_config.json",
                        help="Path to rfid_config.json (default: rfid_config.json)")
    parser.add_argument("--test-mode", action="store_true",
                        help="Log Firebase writes / updates without performing them")
    parser.add_argument("--once", action="store_true",
                        help="Send a single heartbeat and exit (for setup verification)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    logging.getLogger().setLevel(args.log_level)
    run(config_path=args.config, test_mode=args.test_mode, once=args.once)


if __name__ == "__main__":
    main()
