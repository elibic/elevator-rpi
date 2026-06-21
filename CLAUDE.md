# CLAUDE.md — elevator-rpi

קוד ה-Raspberry Pi למערכת מעלית שבת: קריאת תגיות RFID, זיהוי קומות וזיהוי שבת,
ושליחת מצב המעלית ל-Firebase.

## ⚠️ סודות — קריטי
- `rfid_config.json` מכיל את `SECRET_KEY` ואת מיפוי תגיות-RFID של מעלית מסוימת.
  הוא **מוחרג ב-`.gitignore`** — לעולם אל תוסיף אותו ל-Git, ואל תכתוב את ערך ה-`SECRET_KEY`
  בשום קובץ מנוהל (גם לא בקובץ הזה).
- תבנית למבנה: `rfid_config.example.json` (הסוד מרוקן). הקונפיג האמיתי חי על כל Pi בנפרד.

## התקנה / עדכון
- **Pi חדש:** `git clone` → `cp rfid_config.example.json rfid_config.json` → מלא ערכים אמיתיים → `sudo bash shabbat_detector/install.sh`
- **Pi קיים:** `git pull` (לא נוגע ב-`rfid_config.json`) → `sudo systemctl restart shabbat-detector`
- שירות systemd: `shabbat-detector`. לוגים חיים: `journalctl -u shabbat-detector -f`

## Firebase
- פרודקשן: `https://ramada-elev-default-rtdb.europe-west1.firebasedatabase.app` (אזור EU בלבד!)
- עדכון מצב מעלית: PATCH ל-`/elevators/{ELEVATOR_ID}.json`, עם `secret_key` מתוך הקונפיג.

## סימולטור (הרצה מקומית לבדיקות)
- `shabbat_elevator_A_simulator.py`, `firebase_elevator_simulator.py`
- ב-Windows הגדר `$env:PYTHONIOENCODING="utf-8"` למניעת שגיאות Unicode.
- קומת BOTTOM/TOP נספרת כ-52 שניות (visit 26s + stopped 26s = שני events).

## קבצים עיקריים
- `elevator_tracker_rfid.py` — מעקב קומות לפי RFID.
- `shabbat_detector/` — חבילת זיהוי שבת (FSM, auto_learner, cycle_analyzer, firebase_client, hebcal_gate, שירות systemd).
- `deploy_elevator.sh` — סקריפט פריסה ישן מ-ZIP/Drive. **מוחלף ע"י `git pull`.**
