#!/usr/bin/env bash
# פותח את הדשבורד המקומי במסך מלא (kiosk). מאתר את בינארי ה-Chromium הזמין
# (chromium-browser / chromium) - עמיד גם ל-Bullseye וגם ל-Bookworm.
# מופעל משני מקומות: קיצור שולחן-העבודה, וגם רשומת ה-autostart בהתחברות. בבוט
# ה-autostart עלול לרוץ לפני ש-elevator-config-web סיים לעלות, אז ממתינים
# (חסום בזמן, עד ~30ש') שהפורט יענה - כדי שהדפדפן לא ייפתח על "connection refused".
URL="http://127.0.0.1:8080/"

for _i in $(seq 1 30); do
  # בדיקת פורט דרך /dev/tcp של bash (בלי תלות ב-curl/nc). הצלחה => השירות מוכן.
  (exec 3<>/dev/tcp/127.0.0.1/8080) 2>/dev/null && break
  sleep 1
done

BIN="$(command -v chromium-browser || command -v chromium || true)"
if [ -n "$BIN" ]; then
  # --app = חלון אפליקציה נקי (בלי שורת כתובת/טאבים) שאפשר לסגור (כפתור x בדף),
  # בניגוד ל--kiosk שחוסם סגירה ומכריח Alt+F4 (ששובר סשן מרוחק).
  exec "$BIN" --app="$URL" --start-fullscreen --noerrdialogs --disable-infobars --no-first-run
fi
# נפילה אחורה - דפדפן ברירת המחדל (לא מסך מלא).
exec xdg-open "$URL"
