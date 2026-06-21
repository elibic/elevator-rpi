#!/bin/bash
# Wrapper שמופעל מקיצור הדרך בשולחן העבודה.
# פותח טרמינל גרפי שמריץ את הכלי הגרפי עם sudo (כדי שיוכל לנהל שירותים),
# כך שבקשת הסיסמה מוצגת בבירור, ואז Flask עולה והדפדפן נפתח אוטומטית.
set -e

DIR="$(cd "$(dirname "$0")/.." && pwd)"

# מאתר אמולטור טרמינל זמין (Raspberry Pi OS: lxterminal; fallback: x-terminal-emulator).
TERM_CMD=""
for t in x-terminal-emulator lxterminal xterm gnome-terminal konsole; do
    if command -v "$t" >/dev/null 2>&1; then
        TERM_CMD="$t"
        break
    fi
done

CMD="cd '$DIR' && sudo ./setup.sh --web; echo; echo 'הכלי נסגר. אפשר לסגור חלון זה.'; read -n1"

if [ -n "$TERM_CMD" ]; then
    case "$TERM_CMD" in
        gnome-terminal) exec "$TERM_CMD" -- bash -c "$CMD" ;;
        *)              exec "$TERM_CMD" -e bash -c "$CMD" ;;
    esac
else
    # אין טרמינל גרפי — מריצים ישירות (יתכן שתידרש סיסמה ב-stdout).
    cd "$DIR" && exec sudo ./setup.sh --web
fi
