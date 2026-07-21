# Elevator RPi

קוד ה-Raspberry Pi למערכת מעלית שבת — קריאת תגיות RFID, זיהוי קומות, זיהוי שבת,
ושליחת מצב המעלית ל-Firebase.

## ⚠️ קונפיג סודי

הקובץ `rfid_config.json` מכיל את `SECRET_KEY` ומיפוי תגים. לכן הוא **אינו ב-Git**
(ראה `.gitignore`). השתמש ב-`rfid_config.example.json` כתבנית, או מלא דרך הכלי הגרפי.

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
שם מעלית, מיפוי תגים עם סריקה חיה) → **שני שירותי systemd** (`rfid-tracker`
+ `shabbat-detector`) → קיצור דרך בשולחן העבודה → **Raspberry Pi Connect** → הפעלה.

לאחר ההתקנה מופיע אייקון **"הגדרת מעלית RFID"** בשולחן העבודה שפותח את הכלי הגרפי.

## שחזור מהיר מקונפיג ישן (החלפת חומרה)

מעלית שכבר רצה, החומרה נהרסה/הוחלפה, ובידך קובץ `rfid_config.json` ישן (מיפוי-תגים +
`SECRET_KEY` + `ELEVATOR_ID` + `FIREBASE_URL`)? `restore-pi.sh` מניח את הקונפיג הישן
במקומו ומריץ התקנה מלאה **ללא שום שאלה** - ה-Pi החדש ממשיך בדיוק את זהות המעלית הקודמת:

```bash
git clone https://github.com/elibic/elevator-rpi.git ~/elevator-RFID
cd ~/elevator-RFID
# העתק את הקונפיג הישן לתיקיית הבית / כונן USB, ואז:
sudo ./restore-pi.sh                       # חיפוש אוטומטי של rfid_config.json
# או עם נתיב מפורש:
sudo ./restore-pi.sh /media/USB/rfid_config.json
```

הסקריפט מאמת את הקונפיג (שדות חובה, בלי להדפיס את הסוד), מגבה קונפיג קיים אם יש, ומעביר
את השרביט ל-`setup.sh --unattended`. אחר כך: **ריבוט → הדשבורד נפתח לבד**, וכל השאר
(חיבור RPi Connect, בדיקת זיהוי-קומות, לוגים, עריכת מיפוי-תגים) - מהדשבורד. זהו wrapper
דק מעל `setup.sh` - כל לוגיקת ההתקנה נשארת ב-`installer/`.

## Raspberry Pi Connect (גישה מרחוק)

ההתקנה מתקינה ומפעילה את `rpi-connect` (גישת מסך + shell מהדפדפן) ומפעילה linger.
בסוף ההתקנה (טרמינל) או מהדשבורד/אשף (Web) מתבצעת **התחברות חד-פעמית**: מוצג קישור
אימות שיש לפתוח ולאשר בחשבון ה-Raspberry Pi שלך (לא ניתן לאוטומציה מלאה — זה מקשר
את המכשיר לחשבון). אחר כך הגישה זמינה ב-https://connect.raspberrypi.com.

## עדכון Pi קיים

```bash
cd ~/elevator-RFID
sudo ./setup.sh                       # מושך מגיט ומפעיל מחדש
# או מהדשבורד: כפתור "עדכן מגיט והפעל מחדש"
```
`git pull` לעולם לא נוגע ב-`rfid_config.json`.

## התראות

- ההתראות **אינן רצות יותר על ה-Pi**. הן מנוהלות מרכזית מדשבורד האדמין
  (סקשן **"🔔 התראות"** לכל פרויקט) ונשלחות ע"י Google Apps Script
  (`admin-dashboard/apps-script`) על בסיס המצב שה-Pi כותב ל-Firebase.

## ניהול ותחזוקה

- שירותים: `rfid-tracker`, `shabbat-detector`, `fix_cp210x` (דרייבר).
- לוגים חיים: `journalctl -u shabbat-detector -f` · לוגי קובץ: `logs/`.
- ניטור טרמינל: `python monitor.py --watch`.

## קבצים עיקריים

- `setup.sh` — מתקין "הרצה אחת" (bootstrap → `installer/`).
- `installer/` — לוגיקת התקנה/הגדרה/ניהול משותפת + CLI + כלי גרפי (Flask).
- `systemd/*.service.in` — תבניות שירות (נתיב/משתמש נקבעים בזמן התקנה).
- `elevator_tracker_rfid.py` — מעקב קומות לפי RFID.
- `shabbat_detector/` — חבילת זיהוי שבת (FSM, learner, Firebase, שירות).
- `tag_mapper.py`, `monitor.py` — כלי מיפוי תגים וניטור.
- `deploy_elevator.sh`, `shabbat_detector/install.sh` — **deprecated** (מוחלפים ע"י `setup.sh`).
