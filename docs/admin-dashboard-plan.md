# תוכנית: דשבורד-על לניהול כל הפרויקטים (ramada-admin)

> ⚠️ **מיושן — לארכיון בלבד.** זהו מסמך התכנון המקורי. המצב העדכני (חלקים 1-3
> כבר בנויים/מאומתים) חי ב-**`ramada-web/docs/admin-dashboard-handoff.md`**, וצד-ה-Pi
> מתועד ב-**`docs/fleet-remote-update.md`**. אל תסתמך על שדה "סטטוס" כאן.
>
> ~~**סטטוס: תכנון בלבד. טרם התחילה בנייה.**~~ — לא נכון יותר; הבנייה הושלמה.

---

## 🎯 המטרה
דשבורד-על אחד שמרכז את **כל הפרויקטים** (Ramada, Nitza20, Hilton וכו'),
מציג סטטוס חי של כל מעלית, ומאפשר ניהול גרסאות ועדכון מרחוק של ה-Pi-ים.

## 🧩 רקע ארכיטקטוני (איך המערכת בנויה היום)
- כל **פרויקט** = מלון/אתר עם מעלית אחת או יותר.
- לכל מעלית: **Raspberry Pi** + גלאי RFID. תגי RFID על הקיר ממופים לקומות.
  ה-Pi משדר כל הזמן ל-Firebase על איזו קומה המעלית נמצאת.
- בצד השני: קבצי web (משוכפלים מהתבנית `elibic/ramada-web`) שמציגים את מיקום
  המעלית, ובמצב שבת מחשבים זמן הגעה.
- **כל פרויקט הוא פרויקט Firebase נפרד** (לא הכל באותו DB). ← נקודה קריטית.
- **כל פרויקט הוא subdomain** תחת `econtrol.co.il`. מה שמשתנה בין פרויקטים זה רק
  ההתחלה של ה-URL:
  - `https://ramada.econtrol.co.il/public?floor=0`
  - `https://nitza20.econtrol.co.il/public?floor=0`
- דף ההגדרות של כל פרויקט = ה-`setup` שלו, על אותו subdomain.

## ✅ החלטות שכבר התקבלו
1. **איפה זה חי**: ריפו חדש (`ramada-admin`), מתארח ב-Firebase Hosting על
   **פרויקט Firebase ייעודי לאדמין** (נפרד מפרויקטי הפרויקטים).
2. **יכולות**: צפייה (view) בסטטוס כל פרויקט + לינק לדף ההגדרות (`/setup`) שלו +
   ניהול גרסאות ועדכון קבצי ה-Pi.
3. **הוספת פרויקט**: כפתור "הוסף פרויקט" → מדביקים את ה-**Firebase web config המלא**
   של הפרויקט (apiKey, authDomain, databaseURL, projectId...) → נוסף אוטומטית.
4. **התחברות**: נדרשת (Firebase Auth, email/password — רק הבעלים).
5. **עדכון מרחוק**: לכלול **עכשיו** (לא רק תצוגת גרסה).
6. **ניהול התראות/תזכורות**: ב**אדמין דשבורד**, לא על המעלית עצמה. ✅ **בוצע** — ראה סקשן ההתראות למטה.

## 🏗️ התוכנית — 3 חלקים

### 1) `ramada-admin` — ריפו חדש (הדשבורד)
אפליקציית web סטטית, Firebase Hosting על פרויקט ה-Firebase של האדמין:
- **התחברות** — Firebase Auth (email/password).
- **`/projects`** ב-DB של האדמין: לכל פרויקט נשמרים ה-web config המלא + subdomain +
  שם תצוגה + ה-`secret_key` שלו (מוגן מאחורי login).
- **גריד פרויקטים**: לכל פרויקט פותחים Firebase app משני מה-config שלו
  (Firebase JS SDK תומך בכמה apps במקביל), נרשמים ל-`/elevators/{id}` ול-`/fleet/{id}`,
  ומציגים: קומה נוכחית · שבת/חול · online/offline · גרסת ה-Pi.
- **כפתורים לכל פרויקט**: ⚙️ "הגדרות" → `https://<subdomain>/setup` ·
  ⬆️ "עדכן עכשיו" → כותב פקודת עדכון ל-Firebase של הפרויקט.
- **"הוסף פרויקט"**: טופס שמדביקים בו את ה-web config + subdomain → נשמר ל-`/projects`.

### 2) `elevator-rpi` (הריפו הזה) — צד ה-Pi לעדכון מרחוק
מתבסס על המודל הקיים (`secret_key` ב-PATCH, SSE streams ב-`firebase_client.py`):
- **דיווח גרסה**: בהפעלה + כל ~5 דק', `PATCH /fleet/{ELEVATOR_ID}` עם
  `{version: <git sha>, last_seen, status:"online"}` + `secret_key`.
- **watcher לעדכון**: stream על `/fleet/{ELEVATOR_ID}/command`; כשמגיע
  `{action:"update", secret_key, requested_at}` → מאמת secret →
  `git pull` + `systemctl restart shabbat-detector` → מדווח תוצאה חזרה ל-`/fleet/{id}`.
- ⚠️ אבטחה: ה-Pi מריץ קוד לפי טריגר מרוחק — חייב לאמת `secret_key` לפני כל פעולה
  (זהה למודל ה-PATCH הקיים). **ממתין לאישור סופי של המודל הזה לפני בנייה.**

### 3) `ramada-web` — כמעט כלום
רק לאמת את הנתיב המדויק של דף ההגדרות (`/setup`) ואת מבנה ה-Firebase config של פרויקט.
**דורש צירוף ל-scope של הסשן** (כרגע ה-scope מוגבל ל-`elibic/elevator-rpi` בלבד).

## 🔔 התראות (Email + Telegram) — ✅ בוצע (מרכזי)
**ההחלטה שמומשה: שליחה מרכזית מלאה.** ההתראות **הוסרו לגמרי מה-Pi** (אין יותר
`notifier.py`/`MovementWatchdog`/סקשן `notifications` ב-`rfid_config.json`), ומנוהלות
ונשלחות מרכזית:
- **ניהול ההגדרות** (נמענים, אילו אירועים, ספים, הפעלה/כיבוי) — בדשבורד האדמין, **פר-פרויקט**
  (סקשן **"🔔 התראות"** בכל כרטיס; נשמר ב-Firebase של האדמין).
- **השליחה בפועל** — **Google Apps Script** אחד לכל הצי (`ramada-web/apps-script/elevator-monitor.gs`),
  עם Trigger מתוזמן (~5 דק'). Email דרך `MailApp` (בלי סוד-SMTP) ו-Telegram דרך REST.
- מבוסס על המצב שה-Pi כותב ל-Firebase (`SHABBAT_ACTIVE`, `last_seen` וכו') — כולל זיהוי
  **"Pi מת/offline"** בצד המרכזי (Pi שנפל לא מדווח על עצמו).
- יתרון: אין סודות-התראות על אף Pi, אין כפילות שליחה, וסקריפט אחד מרכזי לכל הפרויקטים.

## 🔑 מה צריך כדי להתחיל לבנות
1. **config של פרויקט ה-Firebase של האדמין** (apiKey, authDomain, databaseURL, projectId...) —
   או לבנות עם placeholders ולמלא.
2. **לצרף `elibic/ramada-web` ל-scope** של הסשן (דרך "Edit environment" / New session).
3. **אישור סופי על מודל ה-`secret_key`** לעדכון מרחוק.
4. **היכן ייווצר הריפו `ramada-admin`** (כנראה `elibic/ramada-admin`).

## 📁 קבצים רלוונטיים ב-elevator-rpi (להתמצאות מהירה)
- `shabbat_detector/firebase_client.py` — wrapper ל-Firebase RTDB REST (SSE streams + PATCH/POST).
  מבנה ה-DB: `/elevators/{id}` (קומה חיה), `/elevator_configs/{id}`, `/settings`,
  `/logs/shabbat_detector/{id}`.
- `shabbat_detector/detector.py` — ה-detector הראשי שמחבר הכול.
- `rfid_config.example.json` — תבנית הקונפיג (ההתראות הוסרו — מנוהלות מרכזית בדשבורד).
- שירות systemd: `shabbat-detector`.

## ▶️ הצעד הבא
כשהסקופ יורחב ויתקבלו האישורים: להתחיל מחלק 2 (צד ה-Pi) — בתוך הריפו הזה, מוגדר היטב,
ופותח את כל השאר. במקביל לפתוח את `ramada-admin` (חלק 1).
