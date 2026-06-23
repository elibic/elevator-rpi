#!/usr/bin/env bash
# פותח את הדשבורד המקומי במסך מלא (kiosk). מאתר את בינארי ה-Chromium הזמין
# (chromium-browser / chromium) — עמיד גם ל-Bullseye וגם ל-Bookworm.
URL="http://127.0.0.1:8080/"
BIN="$(command -v chromium-browser || command -v chromium || true)"
if [ -n "$BIN" ]; then
  # --app = חלון אפליקציה נקי (בלי שורת כתובת/טאבים) שאפשר לסגור (כפתור ✕ בדף),
  # בניגוד ל--kiosk שחוסם סגירה ומכריח Alt+F4 (ששובר סשן מרוחק).
  exec "$BIN" --app="$URL" --start-fullscreen --noerrdialogs --disable-infobars --no-first-run
fi
# נפילה אחורה — דפדפן ברירת המחדל (לא מסך מלא).
exec xdg-open "$URL"
