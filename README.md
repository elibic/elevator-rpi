# Elevator RPi

קוד ה-Raspberry Pi למערכת מעלית שבת — קריאת תגיות RFID, זיהוי קומות, זיהוי שבת,
התראות, ושליחת מצב המעלית ל-Firebase.

## ⚠️ קונפיג סודי

הקובץ `rfid_config.json` מכיל את `SECRET_KEY`, מיפוי תגים, וסודות התראות (טוקני
Telegram / סיסמת SMTP). לכן הוא **אינו ב-Git** (ראה `.gitignore`). השתמש ב-
`rfid_config.example.json` כתבנית, או מלא דרך הכלי הגרפי.

## התקנה בפקודה אחת (Pi נקי)

```bash
git clone https://github.com/elibic/elevator-rpi.git ~/elevator-RFID
cd ~/elevator-RFID
sudo ./setup.sh          # אשף טרמינל אינטראקטיבי
# או:
sudo ./setup.sh --web    # כלי גרפי בדפדפן (אשף + דשבורד ניהול)
```

`setup.sh` מתקין הכל בסדר הנכון: **git pull** (תמיד הקוד העדכני) → חבילות מערכת →
**דרייבר CP210x** → הרשאות serial → venv+תלויות → תיקיות לוגים → הגדרות (Firebase,
שם מעלית, מיפוי תגים עם סריקה חיה, התראות) → **שני שירותי systemd** (`rfid-tracker`
+ `shabbat-detector`) → קיצור דרך בשולחן העבודה → הפעלה.

לאחר ההתקנה מופיע אייקון **"הגדרת מעלית RFID"** בשולחן העבודה שפותח את הכלי הגרפי.

## עדכון Pi קיים

```bash
cd ~/elevator-RFID
sudo ./setup.sh                       # מושך מגיט ומפעיל מחדש
# או מהדשבורד: כפתור "עדכן מגיט והפעל מחדש"
```
`git pull` לעולם לא נוגע ב-`rfid_config.json`.

## התראות

- **כניסה/יציאה ממצב שבת** ו-**אין תנועה ≥ N שעות (לא כולל לילה)**.
- ערוצים: **Email + Telegram** (WhatsApp — ממשק pluggable להמשך).
- מוגדר ב-`rfid_config.json → notifications`, ניתן לעריכה ובדיקה מהכלי הגרפי.
- בדיקה מהירה: `python -m shabbat_detector.notifier --test`.

## ניהול ותחזוקה

- שירותים: `rfid-tracker`, `shabbat-detector`, `fix_cp210x` (דרייבר).
- לוגים חיים: `journalctl -u shabbat-detector -f` · לוגי קובץ: `logs/`.
- ניטור טרמינל: `python monitor.py --watch`.

## קבצים עיקריים

- `setup.sh` — מתקין "הרצה אחת" (bootstrap → `installer/`).
- `installer/` — לוגיקת התקנה/הגדרה/ניהול משותפת + CLI + כלי גרפי (Flask).
- `systemd/*.service.in` — תבניות שירות (נתיב/משתמש נקבעים בזמן התקנה).
- `elevator_tracker_rfid.py` — מעקב קומות לפי RFID.
- `shabbat_detector/` — חבילת זיהוי שבת (FSM, learner, Firebase, התראות, שירות).
- `tag_mapper.py`, `monitor.py` — כלי מיפוי תגים וניטור.
- `deploy_elevator.sh`, `shabbat_detector/install.sh` — **deprecated** (מוחלפים ע"י `setup.sh`).
