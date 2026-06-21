#!/usr/bin/env bash
# ============================================================================
# deploy_elevator.sh — פריסת build חדש של elevator-RFID מתוך ZIP, בבטחה מלאה.
#
# שימוש:
#   sudo bash ~/deploy_elevator.sh                 # ה-ZIP הכי חדש מ-~/Downloads
#   sudo bash ~/deploy_elevator.sh <File-ID|URL>   # הורדה ישירה מ-Google Drive
#   sudo bash ~/deploy_elevator.sh /path/to.zip    # נתיב מקומי
#
# הגנות מובנות (כדי שלא יחזור באג מחיקת ה-config):
#   • מסרב לרוץ אם ה-config הנוכחי שגוי (test-94822) או לא-JSON — נכשל בקול, לא מפיץ זבל
#   • גיבויים עם חותמת-זמן ב-3 מקומות — הרצה לא דורסת גיבוי קודם, לעולם
#   • מזריק את ה-config הטוב לתיקייה החדשה *לפני* ההחלפה — אין חלון של config שגוי חי
#   • בדיקת שפיות סופית שה-config החי תואם לאותה מעלית
# ============================================================================
set -uo pipefail

ECO_HOME="/home/eco"
DEST="$ECO_HOME/elevator-RFID"
CONFIG="$DEST/rfid_config.json"
DL_DIR="$ECO_HOME/Downloads"
BACKUP_DIR="$ECO_HOME/elevator-RFID-backups"
SYS_BACKUP_DIR="/var/backups/elevator-rfid"
TS="$(date +%Y%m%d-%H%M%S)"

die() { echo "❌ $*" >&2; exit 1; }
cfg_id()  { python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['settings'].get('ELEVATOR_ID',''))" "$1" 2>/dev/null; }
cfg_ok()  { python3 -m json.tool "$1" >/dev/null 2>&1; }

[ "$(id -u)" -eq 0 ] || die "הרץ עם sudo:  sudo bash ~/deploy_elevator.sh"
command -v unzip   >/dev/null || die "חסר unzip.  sudo apt install -y unzip"
command -v python3 >/dev/null || die "חסר python3."

# ---- 1. איתור / הורדת ה-ZIP ----
ARG="${1:-}"; ZIP=""
if [ -n "$ARG" ] && [ -f "$ARG" ]; then
    ZIP="$ARG"
elif [ -n "$ARG" ]; then
    FID="$ARG"
    case "$ARG" in *drive.google.com*) FID="$(echo "$ARG" | grep -oE '[-_A-Za-z0-9]{25,}' | head -n1)";; esac
    [ -n "$FID" ] || die "לא הצלחתי לחלץ File-ID מ: $ARG"
    mkdir -p "$DL_DIR"; ZIP="$DL_DIR/elevator-RFID.zip"
    echo "⬇️  מוריד מ-Google Drive (id=$FID)..."
    wget -q --no-check-certificate "https://drive.google.com/uc?export=download&id=${FID}" -O "$ZIP" \
        || die "ההורדה נכשלה. ודא שהקובץ משותף: 'כל מי שיש לו הקישור — Viewer'."
    unzip -tq "$ZIP" >/dev/null 2>&1 || die "הקובץ שהורד אינו ZIP תקין."
else
    ZIP="$(ls -t "$DL_DIR"/*.zip 2>/dev/null | head -n1)"
fi
[ -n "$ZIP" ] && [ -f "$ZIP" ] || die "לא נמצא ZIP. העבר נתיב/קישור/File-ID, או שים zip ב-~/Downloads."
echo "📦 ZIP: $ZIP"

# ---- 2. אימות ה-config הנוכחי לפני שנוגעים במשהו (ההגנה המרכזית!) ----
[ -f "$CONFIG" ] || die "אין config ב-$CONFIG. להתקנה ראשונה — צור rfid_config.json תקין ידנית, ואז הרץ."
cfg_ok "$CONFIG" || die "ה-config הנוכחי אינו JSON תקין. עצרתי כדי לא לגבות/להפיץ זבל. תקן ידנית קודם."
ELEV_ID="$(cfg_id "$CONFIG")"
[ -n "$ELEV_ID" ] || die "ל-config הנוכחי אין ELEVATOR_ID. עצרתי."
if grep -q 'test-94822' "$CONFIG"; then
    die "ה-config הנוכחי מצביע על test-94822 (תבנית/שגוי). עצרתי כדי לא להפיץ אותו!
   תקן את $CONFIG לערכים הנכונים של המעלית, או שחזר מגיבוי ב-$BACKUP_DIR, ואז הרץ שוב."
fi
echo "🛗 מעלית מזוהה: $ELEV_ID  (config תקין ✓)"

# ---- 3. גיבויים עם חותמת-זמן ב-3 מקומות (לעולם לא דורסים גיבוי קודם) ----
mkdir -p "$BACKUP_DIR"
B1="$BACKUP_DIR/rfid_config.$ELEV_ID.$TS.json"
cp "$CONFIG" "$B1"; echo "💾 גיבוי 1 → $B1"
if mkdir -p "$SYS_BACKUP_DIR" 2>/dev/null; then
    B2="$SYS_BACKUP_DIR/rfid_config.$ELEV_ID.$TS.json"
    cp "$CONFIG" "$B2" 2>/dev/null && echo "💾 גיבוי 2 → $B2"
fi
# (גיבוי 3 = התיקייה הישנה המלאה, עם חותמת-זמן, בשלב 6)

# ---- 4. זיהוי שירות ה-tracker ----
TRACKER_SVC="$(grep -rl 'elevator_tracker_rfid' /etc/systemd/system/ 2>/dev/null | head -n1)"
[ -n "$TRACKER_SVC" ] && TRACKER_SVC="$(basename "$TRACKER_SVC")"
echo "🔧 שירות tracker: ${TRACKER_SVC:-<לא נמצא>}"

# ---- 5. חילוץ הקוד החדש ל-temp, והזרקת ה-config הטוב *לפני* ההחלפה ----
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
unzip -q "$ZIP" -x "*.log" -d "$TMP" || die "חילוץ ה-ZIP נכשל"
SRC="$(dirname "$(find "$TMP" -name elevator_tracker_rfid.py | head -n1)")"
[ -n "$SRC" ] && [ -f "$SRC/elevator_tracker_rfid.py" ] || die "לא נמצא elevator_tracker_rfid.py ב-ZIP"
# הזרקת ה-config הטוב הנוכחי לתוך התיקייה החדשה (דורס את ה-test שהגיע ב-ZIP)
cp "$CONFIG" "$SRC/rfid_config.json"
[ "$(cfg_id "$SRC/rfid_config.json")" = "$ELEV_ID" ] || die "הזרקת ה-config נכשלה. עצרתי לפני החלפה (כלום לא השתנה)."
rm -rf "$SRC/venv"
echo "✓ הקוד החדש מוכן עם ה-config הנכון של $ELEV_ID"

# ---- 6. עצירת שירותים + החלפה אטומית (גיבוי תיקייה עם חותמת-זמן) ----
[ -n "$TRACKER_SVC" ] && systemctl stop "$TRACKER_SVC" 2>/dev/null
systemctl stop shabbat-detector 2>/dev/null
if [ -d "$DEST" ]; then
    mv "$DEST" "$DEST.bak.$TS"           # חותמת-זמן → לעולם לא דורס גיבוי קודם
    rm -rf "$DEST.bak.$TS/venv"          # venv לא נשמר בגיבוי (נבנה מחדש, כבד)
    echo "🗄️  גיבוי 3 (תיקייה מלאה) → $DEST.bak.$TS"
fi
mv "$SRC" "$DEST"
chown -R eco:eco "$DEST"

# ---- 7. בדיקת שפיות סופית ----
cfg_ok "$CONFIG" || die "ה-config החי אינו JSON תקין! שחזר מ-$B1"
NEW_ID="$(cfg_id "$CONFIG")"
[ "$NEW_ID" = "$ELEV_ID" ] || die "אזהרה חמורה: ה-config החי הוא '$NEW_ID' ולא '$ELEV_ID'! שחזר מ-$B1"
grep -q 'test-94822' "$CONFIG" && die "אזהרה: ה-config החי הוא test-94822! שחזר מ-$B1"
echo "✅ config חי תקין למעלית $NEW_ID"

# ---- 8. venv + תלויות + שירות הדיטקטור ----
echo "🐍 בונה venv ומתקין תלויות..."
python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install --quiet --upgrade pip
"$DEST/venv/bin/pip" install --quiet requests sseclient-py pyserial
bash "$DEST/shabbat_detector/install.sh" || echo "⚠️  install.sh החזיר שגיאה — בדוק סטטוס למטה"

# ---- 9. הפעלת ה-tracker ----
[ -n "$TRACKER_SVC" ] && systemctl restart "$TRACKER_SVC" || echo "⚠️  לא נמצא שירות tracker — הפעל ידנית."

# ---- 10. סטטוס + מיקום הגיבויים ----
echo ""; echo "================= סטטוס ================="
[ -n "$TRACKER_SVC" ] && systemctl --no-pager status "$TRACKER_SVC" 2>/dev/null | head -n 6
echo "-----------------------------------------"
systemctl --no-pager status shabbat-detector 2>/dev/null | head -n 6
echo ""
echo "✅ סיום למעלית $NEW_ID."
echo "📂 גיבויי ה-config (עם חותמת-זמן, לא נדרסים):"
echo "   $B1"
[ -n "${B2:-}" ] && echo "   $B2"
echo "   $DEST.bak.$TS/rfid_config.json"
echo "↩️  לשחזור מהיר:  sudo cp $B1 $CONFIG && sudo systemctl restart ${TRACKER_SVC:-rfid-tracker}"
echo "🔎 לוג חי:  journalctl -u ${TRACKER_SVC:-<tracker>} -f"
