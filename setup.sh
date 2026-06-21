#!/usr/bin/env bash
# ============================================================================
#  setup.sh — מתקין "הרצה אחת" למערכת מעלית RFID.
#
#  Pi נקי:   git clone <repo> ~/elevator-RFID && cd ~/elevator-RFID && sudo ./setup.sh
#  עדכון:    sudo ./setup.sh            (מושך תמיד את הקוד העדכני מ-git)
#  גרפי:     sudo ./setup.sh --web
#
#  הסקריפט עושה רק bootstrap מינימלי (git pull + apt + venv), וכל שאר הלוגיקה
#  ב-`python -m installer` כדי שהטרמינל והכלי הגרפי ירוצו אותו קוד בדיוק.
# ============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
BRANCH=""
PY_ARGS=()

# ── פירוק דגלים: --branch נצרך כאן ל-git; השאר עוברים ל-installer ──
while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch) BRANCH="$2"; shift 2 ;;
    *) PY_ARGS+=("$1"); shift ;;
  esac
done

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "יש להריץ עם sudo:  sudo ./setup.sh ${PY_ARGS[*]:-}" >&2
    exit 1
  fi
}
require_root

# ── 0. עדכון תמיד מ-git (self-update + re-exec יחיד) ─────────────────────────
# git pull לא נוגע ב-rfid_config.json (מוחרג). אם setup.sh עצמו עודכן — נריץ
# את עצמנו פעם אחת מחדש כדי שתמיד תרוץ הגרסה החדשה.
if [[ "${ELEVATOR_SETUP_REEXEC:-0}" != "1" ]]; then
  if command -v git >/dev/null 2>&1 && [[ -d "$DIR/.git" ]]; then
    echo "== מושך עדכונים מ-git =="
    git config --global --add safe.directory "$DIR" 2>/dev/null || true
    BEFORE="$(git -C "$DIR" rev-parse HEAD 2>/dev/null || echo none)"
    if [[ -n "$BRANCH" ]]; then
      git -C "$DIR" pull --ff-only origin "$BRANCH" || echo "אזהרה: git pull נכשל (ממשיך עם הקוד הקיים)"
    else
      git -C "$DIR" pull --ff-only || echo "אזהרה: git pull נכשל (ממשיך עם הקוד הקיים)"
    fi
    AFTER="$(git -C "$DIR" rev-parse HEAD 2>/dev/null || echo none)"
    if [[ "$BEFORE" != "$AFTER" ]]; then
      echo "הקוד עודכן — מריץ מחדש את ההתקנה…"
      exec env ELEVATOR_SETUP_REEXEC=1 bash "$DIR/setup.sh" ${BRANCH:+--branch "$BRANCH"} "${PY_ARGS[@]:-}"
    fi
  fi
fi

# ── 1. תלויות bootstrap (apt) ────────────────────────────────────────────────
echo "== מוודא חבילות בסיס (apt) =="
apt-get update -qq || true
apt-get install -y python3 python3-venv python3-pip git >/dev/null

# ── 2. venv + תלויות פייתון ──────────────────────────────────────────────────
VENV="$DIR/venv"
if [[ ! -d "$VENV" ]]; then
  echo "== יוצר venv =="
  python3 -m venv "$VENV"
fi
echo "== מתקין תלויות פייתון =="
"$VENV/bin/pip" install --quiet --upgrade pip || true
"$VENV/bin/pip" install --quiet requests sseclient-py pyserial flask

# ה-venv שייך למשתמש האמיתי (לא root) כדי שיוכל לנהל אותו בלי sudo.
REAL_USER="${SUDO_USER:-$(id -un)}"
chown -R "$REAL_USER":"$REAL_USER" "$VENV" 2>/dev/null || true

# ── 3. מעבירים את השרביט ל-installer (אותה לוגיקה ל-CLI ול-Web) ──────────────
cd "$DIR"
exec "$VENV/bin/python" -m installer "${PY_ARGS[@]:-}"
