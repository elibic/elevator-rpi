#!/usr/bin/env bash
# ============================================================================
#  restore-pi.sh - שחזור מהיר של Pi מעלית מקונפיג ישן (החלפת חומרה).
#
#  התרחיש: מעלית שכבר הייתה בשימוש, החומרה נהרסה/הוחלפה, ויש בידך את קובץ
#  ה-rfid_config.json הישן (מיפוי-תגים + SECRET_KEY + ELEVATOR_ID + FIREBASE_URL).
#  הסקריפט מניח את הקונפיג הישן במקומו ומריץ התקנה מלאה *ללא שום שאלה*, כך שה-Pi
#  החדש ממשיך בדיוק את זהות המעלית הקודמת - בלי להקליד מחדש שום נתון.
#
#  שימוש (על ה-Pi החדש, אחרי git clone):
#     sudo ./restore-pi.sh [נתיב-לקונפיג-הישן]
#
#  בלי נתיב - חיפוש אוטומטי של rfid_config.json (בית / שולחן-עבודה / הורדות /
#  כונן USB / מחיצת boot). כל שאר הניהול (RPi Connect, בדיקת קומות, לוגים,
#  עריכת מיפוי-תגים) נעשה מהדשבורד המקומי שנפתח אוטומטית.
#
#  זהו wrapper דק מעל setup.sh --unattended: הוא רק משחזר את הקונפיג, וכל
#  לוגיקת ההתקנה נשארת ב-installer (אותו קוד לטרמינל ולדשבורד).
# ============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="$DIR/rfid_config.json"
REAL_USER="${SUDO_USER:-$(id -un)}"

log() { printf '%s\n' "$*"; }
die() { printf 'שגיאה: %s\n' "$*" >&2; exit 1; }

# ── עזרה ──────────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  log "שימוש: sudo ./restore-pi.sh [נתיב-לקונפיג-הישן]"
  log "  בלי נתיב - חיפוש אוטומטי של rfid_config.json (בית/שולחן-עבודה/הורדות/USB/boot)."
  log "  אחרי השחזור מורצת התקנה מלאה ללא שאלות (setup.sh --unattended)."
  exit 0
fi

# ── שורש נדרש (setup.sh ממילא דורש - נכשל כאן מוקדם וברור) ─────────────────────
[[ $EUID -eq 0 ]] || die "יש להריץ עם sudo:  sudo ./restore-pi.sh ${1:-}"

# ── 1. איתור הקונפיג הישן ─────────────────────────────────────────────────────
SRC="${1:-}"
if [[ -n "$SRC" ]]; then
  [[ -f "$SRC" ]] || die "הקובץ שנמסר לא קיים: $SRC"
elif [[ -f "$DEST" ]]; then
  log "== נמצא קונפיג במקומו ($DEST) - משתמש בו =="
  SRC="$DEST"
else
  log "== מחפש קונפיג ישן (rfid_config.json)... =="
  HOME_DIR="$(getent passwd "$REAL_USER" | cut -d: -f6)"
  [[ -n "$HOME_DIR" ]] || HOME_DIR="/home/$REAL_USER"
  CANDS=(
    "$HOME_DIR/rfid_config.json"
    "$HOME_DIR/Desktop/rfid_config.json"
    "$HOME_DIR/Downloads/rfid_config.json"
    "/boot/firmware/rfid_config.json"
    "/boot/rfid_config.json"
  )
  # כונני USB / mounts (חיפוש רדוד כדי לא לסרוק את כל הדיסק)
  while IFS= read -r f; do CANDS+=("$f"); done \
    < <(find /media /mnt -maxdepth 3 -name rfid_config.json 2>/dev/null || true)
  for c in "${CANDS[@]}"; do
    if [[ -f "$c" ]]; then SRC="$c"; log "  נמצא: $c"; break; fi
  done
fi

[[ -n "${SRC:-}" && -f "$SRC" ]] || die \
  "לא נמצא קונפיג ישן. העתק את rfid_config.json לתיקיית הבית של $REAL_USER (או לכונן USB) והרץ שוב, או מסור נתיב מפורש:  sudo ./restore-pi.sh /path/to/rfid_config.json"

# ── 2. ולידציה - JSON תקין + שדות חובה (בלי להדפיס סודות) ──────────────────────
if command -v python3 >/dev/null 2>&1; then
  python3 - "$SRC" <<'PY' || die "הקונפיג פסול: JSON לא תקין או חסר FIREBASE_URL / ELEVATOR_ID / SECRET_KEY."
import json, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
s = cfg.get("settings", {})
missing = [k for k in ("FIREBASE_URL", "ELEVATOR_ID", "SECRET_KEY") if not s.get(k)]
if missing:
    print("  חסרים שדות חובה:", ", ".join(missing), file=sys.stderr)
    sys.exit(1)
# מדפיס רק מזהים לא-סודיים לאישור חזותי - לעולם לא את ה-SECRET_KEY.
print(f"  מעלית: {s.get('ELEVATOR_ID')}   |   תגים ממופים: {len(cfg.get('tags', {}))}")
PY
else
  log "  (python3 לא נמצא - מדלג על ולידציה מקדימה; setup.sh יאמת בהמשך)"
fi

# ── 3. גיבוי קונפיג קיים + הנחת הישן במקום ─────────────────────────────────────
if [[ "$SRC" != "$DEST" ]]; then
  if [[ -f "$DEST" ]]; then
    BAK="$DEST.$(date +%Y%m%d-%H%M%S).bak"
    cp -p "$DEST" "$BAK"
    log "== גובה קונפיג קיים אל: $BAK =="
  fi
  cp "$SRC" "$DEST"
  log "== שוחזר הקונפיג הישן אל: $DEST =="
fi
# הקונפיג מכיל סוד - בעלות למשתמש והרשאות 600 בלבד.
chown "$REAL_USER":"$REAL_USER" "$DEST" 2>/dev/null || true
chmod 600 "$DEST"

# ── 4. התקנה מלאה ללא שאלות (כל הלוגיקה ב-installer) ───────────────────────────
log ""
log "== מריץ התקנה מלאה ללא שאלות (setup.sh --unattended)... =="
log "   דרייברים, שירותים, דשבורד מקומי והפעלה. שאר הניהול - מהדשבורד."
log ""
exec "$DIR/setup.sh" --unattended
