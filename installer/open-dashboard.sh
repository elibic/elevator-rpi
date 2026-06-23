#!/usr/bin/env bash
# פותח את הדשבורד המקומי במסך מלא (kiosk). מאתר את בינארי ה-Chromium הזמין
# (chromium-browser / chromium) — עמיד גם ל-Bullseye וגם ל-Bookworm.
URL="http://127.0.0.1:8080/"
BIN="$(command -v chromium-browser || command -v chromium || true)"
if [ -n "$BIN" ]; then
  exec "$BIN" --kiosk --noerrdialogs --disable-infobars --no-first-run "$URL"
fi
# נפילה אחורה — דפדפן ברירת המחדל (לא מסך מלא).
exec xdg-open "$URL"
