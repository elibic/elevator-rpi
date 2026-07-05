"""
Log backup - push the Pi's logs directory to a separate GitHub repo.

Each Pi pushes only into its own subfolder ``{ELEVATOR_ID}/`` of a shared logs
repo (for example ``elibic/elevator-logs``), so concurrent pushes from different
Pis touch disjoint paths and only race on the ref. The local working clone is
treated as disposable: before every push attempt it is hard-synced to
``origin/<branch>`` (discarding any drift, a half-finished rebase or a dirty
tree) and the snapshot is re-copied, then the push is retried a few times. This
self-heals a clone that fell behind or got stuck after a past push race, instead
of failing every backup with a permanent non-fast-forward. Driven by the fleet
agent: weekly automatically and on-demand from the dashboard ("backup_logs"
command). See ``docs/fleet-remote-update.md``.

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
import time
from datetime import datetime
from urllib.parse import urlsplit

log = logging.getLogger("fleet_agent.log_backup")

_DEFAULT_DIR = "/var/lib/elevator-logs"
_DEFAULT_BRANCH = "main"
_GIT_TIMEOUT = 120
_PUSH_ATTEMPTS = 4                                       # converge+push tries before giving up
_BACKOFF_BASE = 1.5                                      # seconds; grows per attempt to de-sync racing Pis

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


def _sanitize_slug(s: str) -> str:
    """Lowercase + keep only [a-z0-9-] so it is safe as a repo folder name."""
    return re.sub(r"[^a-z0-9-]+", "-", str(s).strip().lower()).strip("-")


def project_slug(settings: dict) -> str:
    """Project identifier for the backup folder, so the same ELEVATOR_ID in two
    projects (e.g. B in ramada and B in nitza) does not collide.

    `LOG_BACKUP_PREFIX` overrides; otherwise derived from the FIREBASE_URL host -
    the first label minus a trailing ``-default-rtdb`` and ``-elev`` (so
    ``ramada-elev-default-rtdb.…`` -> ``ramada``). The dashboard derives the same
    slug from the project's databaseURL host, so the two always agree."""
    explicit = str(settings.get("LOG_BACKUP_PREFIX", "")).strip()
    if explicit:
        return _sanitize_slug(explicit)
    raw = str(settings.get("FIREBASE_URL", "") or settings.get("FIREBASE_BASE_URL", "")).strip()
    host = urlsplit(raw).netloc or raw
    label = host.split(".")[0]
    for suffix in ("-default-rtdb", "-elev"):
        if label.endswith(suffix):
            label = label[: -len(suffix)]
    return _sanitize_slug(label)


def _run(args: list[str], cwd: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=_GIT_TIMEOUT)
        return p.returncode, (p.stderr or p.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return 1, "timeout"
    except Exception as e:                                # pragma: no cover
        return 1, str(e)


def _copy_scrubbed(logs_dir: str, dest: str, secret: str) -> int:
    """Copy every regular file from ``logs_dir`` into ``dest`` (created if
    needed), scrubbing secrets on the way. Returns the number of files copied.
    Re-run each attempt because a hard-sync may have wiped a prior snapshot."""
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
    return copied


def _sync_to_origin(work: str, branch: str) -> None:
    """Leave the local clone's working branch ready for a fresh commit + push.

    If ``origin/<branch>`` is known (the normal case), force the branch to match
    it exactly, discarding any local drift: extra local commits, a half-finished
    rebase/merge, or a dirty working tree. This is what lets a Pi whose clone fell
    behind (or got stuck mid-rebase after a past push race) recover on its own
    instead of failing every backup with a non-fast-forward.

    If the branch is not known yet - a brand-new/empty remote on the first backup,
    or a transient fetch failure - just make sure a local ``<branch>`` is checked
    out so the later ``push HEAD:<branch>`` can create/advance it. The push is the
    real arbiter (the caller retries on failure), so a failed fetch here is never
    fatal on its own."""
    _run(["git", "-C", work, "rebase", "--abort"])         # clear a stuck rebase (no-op otherwise)
    _run(["git", "-C", work, "merge", "--abort"])          # ... or a stuck merge
    _run(["git", "-C", work, "fetch", "origin", branch])   # rc ignored - the ref check below decides
    ref = f"refs/remotes/origin/{branch}"
    rc_ref, _ = _run(["git", "-C", work, "rev-parse", "--verify", "--quiet", ref])
    if rc_ref == 0:                                          # remote branch known -> mirror it exactly
        _run(["git", "-C", work, "checkout", "-B", branch, f"origin/{branch}"])
        _run(["git", "-C", work, "reset", "--hard", f"origin/{branch}"])
        _run(["git", "-C", work, "clean", "-fd"])
        return
    _run(["git", "-C", work, "checkout", "-B", branch])    # empty remote / first backup: prepare commit


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

    # 1. ensure a local working clone exists (persistent under `work`)
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
    _run(["git", "-C", work, "remote", "set-url", "origin", repo_url])  # pick up a rotated token

    slug = project_slug(settings)
    rel = f"{slug}/{elevator_id}" if slug else elevator_id
    dest = os.path.join(work, *rel.split("/"))

    # 2. converge -> snapshot -> commit -> push, retrying on a non-fast-forward.
    # Each attempt hard-syncs the (disposable) local clone to origin/<branch>, so
    # a clone that drifted or got stuck mid-rebase heals itself; our own snapshot
    # under {project}/{elevator_id}/ is regenerated every attempt, so it is
    # idempotent and never conflicts with another Pi's disjoint folder.
    last = ""
    for attempt in range(_PUSH_ATTEMPTS):
        _sync_to_origin(work, branch)                       # converge onto origin/<branch> (or prep first commit)

        copied = _copy_scrubbed(logs_dir, dest, secret)
        if copied == 0:
            return True, "no log files to back up"

        _run(["git", "-C", work, "add", rel])
        rc, _ = _run(["git", "-C", work, "diff", "--cached", "--quiet"])
        if rc == 0:
            return True, "no changes"

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        rc, out = _run(["git", "-C", work,
                        "-c", f"user.name={name}", "-c", f"user.email={email}",
                        "commit", "-m", f"logs: {rel} {stamp}"])
        if rc != 0:
            return False, f"commit failed: {_redact(out)[:160]}"

        rc, out = _run(["git", "-C", work, "push", "origin", f"HEAD:{branch}"])
        if rc == 0:
            return True, f"pushed {copied} file(s) to {rel}"
        last = out                                          # lost the ref race - resync & retry
        if attempt < _PUSH_ATTEMPTS - 1:
            time.sleep(_BACKOFF_BASE * (attempt + 1))

    return False, f"push failed after {_PUSH_ATTEMPTS} tries: {_redact(last)[:160]}"
