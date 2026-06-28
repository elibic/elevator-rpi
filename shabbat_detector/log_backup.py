"""
Log backup - push the Pi's logs directory to a separate GitHub repo.

Each Pi pushes only into its own subfolder ``{ELEVATOR_ID}/`` of a shared logs
repo (for example ``elibic/elevator-logs``), so concurrent pushes from different
Pis touch disjoint paths and only race on the ref - resolved by ``pull --rebase``
plus a single retry. Driven by the fleet agent: weekly automatically and
on-demand from the dashboard ("backup_logs" command). See
``docs/fleet-remote-update.md``.

Config (``rfid_config.json -> settings``, all optional):
  ``LOG_BACKUP_ENABLED``    gate for the *weekly* auto backup (on-demand ignores it)
  ``LOG_BACKUP_REPO_URL``   https URL, may embed a write token:
                            ``https://x-access-token:<PAT>@github.com/elibic/elevator-logs.git``
  ``LOG_BACKUP_BRANCH``     default ``main``
  ``LOG_BACKUP_DIR``        local working clone (default ``/var/lib/elevator-logs``)
  ``LOG_BACKUP_GIT_NAME``   commit author name  (default ``elevator-pi``)
  ``LOG_BACKUP_GIT_EMAIL``  commit author email (default ``elevator-pi@econtrol.co.il``)

Security: the repo may be public (no secrets in the logs, by decision), but we
still scrub the ``SECRET_KEY`` value defensively before pushing, and never log
``LOG_BACKUP_REPO_URL`` (it embeds the PAT).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from datetime import datetime

log = logging.getLogger("fleet_agent.log_backup")

_DEFAULT_DIR = "/var/lib/elevator-logs"
_DEFAULT_BRANCH = "main"
_GIT_TIMEOUT = 120

_URL_CRED = re.compile(r"https://[^@/\s]+@")              # user:token@ in any text
_SECRET_KV = re.compile(r'("?secret_key"?\s*[:=]\s*"?)[^"\s,}]+')


def _redact(text: str) -> str:
    """Strip embedded credentials from text before logging/returning it."""
    return _URL_CRED.sub("https://***@", text or "")


def _scrub_text(text: str, secret: str) -> str:
    """Redact the SECRET_KEY value and any secret_key=... pattern from a log file."""
    if secret:
        text = text.replace(secret, "***")
    return _SECRET_KV.sub(r"\1***", text)


def _run(args: list[str], cwd: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=_GIT_TIMEOUT)
        return p.returncode, (p.stderr or p.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return 1, "timeout"
    except Exception as e:                                # pragma: no cover
        return 1, str(e)


def backup_logs(settings: dict, elevator_id: str, logs_dir: str) -> tuple[bool, str]:
    """Push ``logs_dir`` into ``{elevator_id}/`` of the backup repo.

    Returns ``(ok, detail)`` and never raises - the caller (fleet agent) must
    never have its heartbeat broken by a backup failure.
    """
    repo_url = str(settings.get("LOG_BACKUP_REPO_URL", "")).strip()
    if not repo_url:
        return False, "LOG_BACKUP_REPO_URL not set"
    branch = str(settings.get("LOG_BACKUP_BRANCH", _DEFAULT_BRANCH)).strip() or _DEFAULT_BRANCH
    work = str(settings.get("LOG_BACKUP_DIR", _DEFAULT_DIR)).strip() or _DEFAULT_DIR
    name = str(settings.get("LOG_BACKUP_GIT_NAME", "") or "elevator-pi")
    email = str(settings.get("LOG_BACKUP_GIT_EMAIL", "") or "elevator-pi@econtrol.co.il")
    secret = str(settings.get("SECRET_KEY", ""))

    if shutil.which("git") is None:
        return False, "git not installed"
    if not os.path.isdir(logs_dir):
        return False, f"logs dir missing: {logs_dir}"

    # 1. ensure a local clone exists and is current
    if not os.path.isdir(os.path.join(work, ".git")):
        if os.path.exists(work):
            shutil.rmtree(work, ignore_errors=True)        # clear a partial/non-git dir
        parent = os.path.dirname(work) or "/"
        os.makedirs(parent, exist_ok=True)
        rc, out = _run(["git", "clone", "--branch", branch, repo_url, work])
        if rc != 0:                                         # empty repo / branch absent
            rc, out = _run(["git", "clone", repo_url, work])
            if rc != 0:
                return False, f"clone failed: {_redact(out)[:160]}"
        try:
            os.chmod(work, 0o700)                            # token in .git/config -> root-only
        except OSError:
            pass
    else:
        _run(["git", "-C", work, "remote", "set-url", "origin", repo_url])  # rotated token
        _run(["git", "-C", work, "pull", "--rebase", "--autostash", "origin", branch])

    # 2. copy a clean (scrubbed) snapshot of logs/ into {elevator_id}/
    dest = os.path.join(work, elevator_id)
    os.makedirs(dest, exist_ok=True)
    copied = 0
    for fname in sorted(os.listdir(logs_dir)):
        src = os.path.join(logs_dir, fname)
        if not os.path.isfile(src):
            continue
        try:
            with open(src, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            with open(os.path.join(dest, fname), "w", encoding="utf-8") as f:
                f.write(_scrub_text(text, secret))
            copied += 1
        except Exception as e:
            log.warning("skip log file %s: %s", fname, e)
    if copied == 0:
        return True, "no log files to back up"

    # 3. commit (skip an empty commit)
    _run(["git", "-C", work, "add", elevator_id])
    rc, _ = _run(["git", "-C", work, "diff", "--cached", "--quiet"])
    if rc == 0:
        return True, "no changes"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    rc, out = _run(["git", "-C", work,
                    "-c", f"user.name={name}", "-c", f"user.email={email}",
                    "commit", "-m", f"logs: {elevator_id} {stamp}"])
    if rc != 0:
        return False, f"commit failed: {_redact(out)[:160]}"

    # 4. push, with one rebase+retry on a non-fast-forward race
    rc, out = _run(["git", "-C", work, "push", "origin", f"HEAD:{branch}"])
    if rc != 0:
        _run(["git", "-C", work, "pull", "--rebase", "--autostash", "origin", branch])
        rc, out = _run(["git", "-C", work, "push", "origin", f"HEAD:{branch}"])
        if rc != 0:
            return False, f"push failed: {_redact(out)[:160]}"
    return True, f"pushed {copied} file(s)"
