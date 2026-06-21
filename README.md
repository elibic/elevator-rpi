# Elevator RPi

קוד ה-Raspberry Pi למערכת מעלית שבת — קריאת תגיות RFID, זיהוי קומות
ושליחת מצב המעלית ל-Firebase.

## ⚠️ קונפיג סודי

הקובץ `rfid_config.json` מכיל את `SECRET_KEY` ואת מיפוי התגיות של מעלית מסוימת,
ולכן **אינו נמצא ב-Git** (ראה `.gitignore`). השתמש ב-`rfid_config.example.json`
כתבנית.

## התקנה על Pi חדש

```bash
git clone https://github.com/elibic/elevator-rpi.git ~/elevator-RFID
cd ~/elevator-RFID
cp rfid_config.example.json rfid_config.json
nano rfid_config.json          # מלא SECRET_KEY, FIREBASE_URL, ELEVATOR_ID והתגיות של המעלית
sudo bash shabbat_detector/install.sh
```

## עדכון Pi קיים

```bash
cd ~/elevator-RFID
git pull                       # מוריד רק את שינויי הקוד — לא נוגע ב-rfid_config.json
sudo systemctl restart shabbat-detector
```

## קבצים עיקריים

- `elevator_tracker_rfid.py` — מעקב קומות לפי RFID.
- `shabbat_detector/` — חבילת זיהוי שבת (FSM, learner, Firebase client, שירות systemd).
- `deploy_elevator.sh` — סקריפט פריסה ישן (מ-ZIP/Drive). מוחלף ע"י `git pull`.
- `rfid_config.example.json` — תבנית קונפיג.
