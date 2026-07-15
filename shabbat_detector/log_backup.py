"""
Log backup - push the Pi's logs directory (and a sanitized config snapshot) to a
separate GitHub repo.

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

Config snapshot
---------------
Alongside the logs, a **sanitized** copy of ``rfid_config.json`` is written to
``{project}/{ELEVATOR_ID}/config/rfid_config.sanitized.json`` so the
hard-to-recreate RFID tag->floor mapping survives an SD-card loss. It is
sanitized *fail-closed*: the ``tags`` mapping and non-secret settings are kept,
but the ``SECRET_KEY`` and every token/URL-embedded credential are redacted, so
no secret ever reaches the (possibly shared) backup repo - matching the repo's
hard rule that the SECRET_KEY value is never written to Git. See
``_sanitize_config``. Gated by ``CONFIG_BACKUP_ENABLED`` (default on).

Settings (``rfid_config.json -> settings``, all optional):
  ``LOG_BACKUP_ENABLED``    gate for the *weekly* auto backup (on-demand ignores it)
  ``LOG_BACKUP_REPO_URL``   https URL, may embed a write token:
                            ``https://x-access-token:<PAT>@github.com/elibic/elevator-logs.git``
  ``LOG_BACKUP_BRANCH``     default ``main``
  ``LOG_BACKUP_DIR``        local working clone (default ``/var/lib/elevator-logs``)
  ``LOG_BACKUP_GIT_NAME``   commit author name  (default ``elevator-pi``)
  ``LOG_BACKUP_GIT_EMAIL``  commit author email (default ``elevator-pi@econtrol.co.il``)
  ``CONFIG_BACKUP_ENABLED``   include the sanitized config snapshot (default true)
  ``LOG_BACKUP_MAX_FILE_MB``  per-file size cap (default 90). GitHub hard-rejects
                            files over 100MB (the "gh.io/lfs" push error) - one log
                            that ballooned past the limit used to fail the *whole*
                            backup, forever. Files over the cap are gzip-compressed
                            to ``<name>.gz`` (deterministic - an unchanged log does
                            not produce a new commit); if even the .gz is over the
                            cap, the file is skipped with a ``<name>.TOO_LARGE.txt``
                            note instead of breaking the push.

Security: the repo may be public (no secrets in the logs, by decision), but we
still scrub the ``SECRET_KEY`` value defensively out of the logs and the config
snapshot before pushing, and never log ``LOG_BACKUP_REPO_URL`` (it embeds the PAT).
"""
from __future__ import annotations

import gzip
import json
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
_DEFAULT_MAX_FILE_MB = 90.0                              # GitHub rejects >100MB; keep a margin
_GIT_TIMEOUT = 120
_PUSH_ATTEMPTS = 4                                       # converge+push tries before giving up
_BACKOFF_BASE = 1.5                                      # seconds; grows per attempt to de-sync racing Pis

_URL_CRED = re.compile(r"https://[^@/\s]+@")              # user:token@ in any text
_SECRET_KV = re.compile(r'("?secret_key"?\s*[:=]\s*"?)[^"\s,}]+')
# A config key whose *name* looks credential-shaped: its value is redacted no
# matter what, so a new secret field added to the config later leaks nothing by
# default (fail-closed). Matches SECRET_KEY, *_TOKEN, *PASSWORD, API_KEY, …
_SECRET_NAME = re.compile(r"secret|token|password|passwd|credential|private[_-]?key|api[_-]?key", re.I)
_REDACTED = "***REDACTED***"


def _redact(text: str) -> str:
    """Strip embedded credentials from text before logging/returning it."""
    return _URL_CRED.sub("https://***@", text or "")


def _scrub_text(text: str, secret: str) -> str:
    """Redact the SECRET_KEY value and any secret_key=... pattern from a log file."""
    if secret:
        text = text.replace(secret, "***")
    return _SECRET_KV.sub(r"\1***", text)


def _scrub_config_value(text: str, secret: str) -> str:
    """Scrub a single (non-secret-named) config string value: strip any
    URL-embedded credential (``https://user:tok@…`` -> ``https://***@…``) and
    the live SECRET_KEY value if it happens to appear inside it."""
    if secret and secret in text:
        text = text.replace(secret, _REDACTED)
    return _URL_CRED.sub("https://***@", text)


def _sanitize_config(cfg: dict, secret: str = "") -> dict:
    """Return a deep copy of ``cfg`` safe to store in a (possibly shared/public)
    Git repo: the RFID ``tags`` mapping and non-secret settings are preserved,
    but every credential is stripped. Redaction is fail-closed and layered:

    * a key whose *name* looks secret (secret/token/password/api-key/…) -> its
      value becomes ``***REDACTED***``, whatever its type;
    * any remaining string value carrying a URL-embedded credential
      (``https://user:tok@…``) is scrubbed;
    * the live ``SECRET_KEY`` value, wherever it appears, is scrubbed.

    This keeps the hard-to-recreate tag mapping recoverable without ever writing
    the SECRET_KEY (or a PAT/token) to Git - the repo's hard rule.
    """
    def clean(value, key_hint: str = ""):
        if key_hint and _SECRET_NAME.search(key_hint):
            return _REDACTED
        if isinstance(value, dict):
            return {k: clean(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, str):
            return _scrub_config_value(value, secret)
        return value
    return clean(cfg)


def _write_config_snapshot(config_path: str, dest: str, secret: str) -> bool:
    """Write a sanitized snapshot of the Pi's config to
    ``dest/config/rfid_config.sanitized.json`` (tag mapping kept, secrets
    stripped). Returns True iff a snapshot was written.

    Fail-closed: on any read/parse/sanitize error nothing is written (never the
    raw config), and a final guard aborts the write if the SECRET_KEY somehow
    survives - so the log backup proceeds but no secret can leak. Output is
    deterministic (sorted keys), so an unchanged config yields identical bytes
    and therefore no spurious commit."""
    if not config_path or not os.path.isfile(config_path):
        return False
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        log.warning("config backup skipped - cannot read config: %s", e)
        return False
    try:
        safe = _sanitize_config(cfg, secret)
        text = json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    except Exception as e:
        log.warning("config backup skipped - sanitize failed: %s", e)
        return False
    if secret and secret in text:                       # defense in depth - never push the raw secret
        log.error("config backup aborted - secret survived sanitize; not writing snapshot")
        return False
    try:
        cfg_dir = os.path.join(dest, "config")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "rfid_config.sanitized.json"), "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        log.warning("config backup skipped - cannot write snapshot: %s", e)
        return False
    return True


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


def _scrub_stream(src: str, out, secret: str) -> None:
    """Stream ``src`` line-by-line into the binary file-object ``out``, scrubbing
    secrets on the way. Streaming (instead of one big read) keeps memory flat -
    a log that ballooned to >100MB must not OOM a Pi Zero (512MB RAM)."""
    with open(src, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            out.write(_scrub_text(line, secret).encode("utf-8"))


def _copy_scrubbed(logs_dir: str, dest: str, secret: str,
                   max_bytes: int) -> tuple[int, list[str]]:
    """Copy every regular file from ``logs_dir`` into ``dest`` (created if
    needed), scrubbing secrets on the way. Files larger than ``max_bytes`` are
    gzip-compressed to ``<name>.gz`` (GitHub hard-rejects >100MB files, and one
    such file used to fail the whole backup); if even the .gz is too large the
    file is skipped and a ``<name>.TOO_LARGE.txt`` note is left instead. The
    gzip stream is deterministic (mtime=0), so re-running on unchanged logs
    yields identical bytes and therefore "no changes" instead of a new commit.
    Returns ``(files_backed_up, oversize_notes)``. Re-run each attempt because
    a hard-sync may have wiped a prior snapshot."""
    os.makedirs(dest, exist_ok=True)
    copied, oversized = 0, []
    for fname in sorted(os.listdir(logs_dir)):
        src = os.path.join(logs_dir, fname)
        if not os.path.isfile(src):
            continue
        plain = os.path.join(dest, fname)
        gz = plain + ".gz"
        note = plain + ".TOO_LARGE.txt"
        try:
            big = os.path.getsize(src) > max_bytes
            # remove the counterpart variants so a file that crossed the size
            # threshold (either way) does not leave a stale duplicate in the repo
            for stale in ((plain,) if big else (gz, note)):
                if os.path.exists(stale):
                    os.remove(stale)
            if not big:
                with open(plain, "wb") as out:
                    _scrub_stream(src, out, secret)
                copied += 1
                continue
            with open(gz, "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as out:
                    _scrub_stream(src, out, secret)
            if os.path.getsize(gz) > max_bytes:          # incompressible - skip, don't break the push
                os.remove(gz)
                with open(note, "w", encoding="utf-8") as f:
                    f.write(f"{fname}: {os.path.getsize(src)} bytes exceeds the backup "
                            f"per-file cap even gzip-compressed - not backed up.\n")
                oversized.append(f"{fname} skipped (too large)")
                log.warning("log file %s too large even compressed - skipped", fname)
                continue
            if os.path.exists(note):
                os.remove(note)
            copied += 1
            oversized.append(f"{fname} gzipped")
            log.info("log file %s over per-file cap - backed up compressed as %s.gz",
                     fname, fname)
        except Exception as e:
            log.warning("skip log file %s: %s", fname, e)
    return copied, oversized


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


def backup_logs(settings: dict, elevator_id: str, logs_dir: str,
                config_path: str = "") -> tuple[bool, str]:
    """Push ``logs_dir`` (and a sanitized ``config_path`` snapshot) into
    ``{project}/{elevator_id}/`` of the backup repo.

    Returns ``(ok, detail)`` and never raises - the caller (fleet agent) must
    never have its heartbeat broken by a backup failure. When ``config_path`` is
    given and ``CONFIG_BACKUP_ENABLED`` is on (default), a redacted copy of the
    config (tag mapping kept, SECRET_KEY/tokens stripped) is committed alongside
    the logs - see ``_write_config_snapshot``.
    """
    repo_url = str(settings.get("LOG_BACKUP_REPO_URL", "")).strip()
    if not repo_url:
        return False, "LOG_BACKUP_REPO_URL not set"
    branch = str(settings.get("LOG_BACKUP_BRANCH", _DEFAULT_BRANCH)).strip() or _DEFAULT_BRANCH
    work = str(settings.get("LOG_BACKUP_DIR", _DEFAULT_DIR)).strip() or _DEFAULT_DIR
    name = str(settings.get("LOG_BACKUP_GIT_NAME", "") or "elevator-pi")
    email = str(settings.get("LOG_BACKUP_GIT_EMAIL", "") or "elevator-pi@econtrol.co.il")
    secret = str(settings.get("SECRET_KEY", ""))
    # Config snapshot is on by default; a project can opt out with a falsy value.
    config_enabled = str(settings.get("CONFIG_BACKUP_ENABLED", "true")).strip().lower() \
        not in ("0", "false", "no", "off", "")
    try:
        max_mb = float(settings.get("LOG_BACKUP_MAX_FILE_MB", _DEFAULT_MAX_FILE_MB))
    except (TypeError, ValueError):
        max_mb = _DEFAULT_MAX_FILE_MB
    max_bytes = int(max_mb * 1024 * 1024)

    if shutil.which("git") is None:
        return False, "git not installed"
    have_logs = os.path.isdir(logs_dir)
    want_config = config_enabled and bool(config_path) and os.path.isfile(config_path)
    # Only bail if there is nothing at all to back up - a Pi with a config but no
    # logs dir yet (e.g. schedule-only building) can still back its config up.
    if not have_logs and not want_config:
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

        copied, oversized = _copy_scrubbed(logs_dir, dest, secret, max_bytes) if have_logs else (0, [])
        # Sanitized config snapshot alongside the logs (idempotent + deterministic,
        # so an unchanged config adds no commit). Written every attempt because a
        # hard-sync wipes the snapshot, exactly like the log copy above.
        cfg_written = _write_config_snapshot(config_path, dest, secret) if config_enabled else False
        if copied == 0 and not oversized and not cfg_written:
            return True, "no log files or config to back up"

        _run(["git", "-C", work, "add", rel])
        rc, _ = _run(["git", "-C", work, "diff", "--cached", "--quiet"])
        if rc == 0:
            return True, "no changes"

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        rc, out = _run(["git", "-C", work,
                        "-c", f"user.name={name}", "-c", f"user.email={email}",
                        "commit", "-m", f"backup: {rel} {stamp}"])
        if rc != 0:
            return False, f"commit failed: {_redact(out)[:160]}"

        rc, out = _run(["git", "-C", work, "push", "origin", f"HEAD:{branch}"])
        if rc == 0:
            parts = ([f"{copied} log file(s)"] if copied else []) + (["config"] if cfg_written else [])
            detail = f"pushed {' + '.join(parts) or 'changes'} to {rel}"
            if oversized:
                detail += f" [{'; '.join(oversized)}]"
            return True, detail
        last = out                                          # lost the ref race - resync & retry
        if attempt < _PUSH_ATTEMPTS - 1:
            time.sleep(_BACKOFF_BASE * (attempt + 1))

    return False, f"push failed after {_PUSH_ATTEMPTS} tries: {_redact(last)[:160]}"
