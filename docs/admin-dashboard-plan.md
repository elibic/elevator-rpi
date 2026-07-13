# תוכנית: דשבורד-על לניהול כל הפרויקטים (ramada-admin)

> מסמך handoff — מרכז את כל ההחלטות והארכיטקטורה שסוכמו, כדי שאפשר יהיה להמשיך
> בכל סשן חדש. בסשן חדש: *"קרא את `docs/admin-dashboard-plan.md` ותמשיך"*.
>
> **סטטוס:** חלק 2 (צד ה-Pi: דיווח גרסה + עדכון מרחוק) **מומש** — ראה
> [`fleet-remote-update.md`](fleet-remote-update.md). חלקים 1 (דשבורד `ramada-admin`)
> ו-3 עדיין פתוחים. ה-config של האדמין (`econtrolelevelev`) התקבל ו-`ramada-web` ב-scope.
>
> 📋 **מצב עדכני ורשימת המשימות שנותרו** (לא מוזג ל-`main` / לא נפרס; חלק 1 — סקאפולד
> נבנה ונמסר כ-bundle, טרם ב-GitHub): ראה [`admin-dashboard-status.md`](admin-dashboard-status.md).

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
6. **ניהול התראות/תזכורות**: עובר ל**אדמין דשבורד**, לא על המעלית עצמה. ← עדכון מאוחר.

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

### 2) `elevator-rpi` (הריפו הזה) — צד ה-Pi לעדכון מרחוק  ✅ מומש
מומש ב-[`shabbat_detector/fleet_agent.py`](../shabbat_detector/fleet_agent.py)
(משולב ב-`detector.py`, רץ ב-thread-ים ברקע). תיעוד מלא + חוקי-RTDB:
[`fleet-remote-update.md`](fleet-remote-update.md).
- **דיווח גרסה**: בהפעלה + כל ~5 דק', `PATCH /fleet/{ELEVATOR_ID}` עם
  `{version: <git sha>, branch, last_seen, status:"online"}` + `secret_key`.
- **watcher לעדכון**: stream על `/fleet/{ELEVATOR_ID}/command`; על
  `{action:"update", secret_key, requested_at}` → אימות secret (constant-time) +
  idempotency → `git pull --ff-only` → יציאה מבוקרת ⇐ systemd (`Restart=always`)
  מפעיל מחדש עם הקוד החדש (ללא sudo) → מדווח תוצאה ל-`/fleet/{id}`.
- ⚠️ אבטחה: אימות `secret_key` לפני כל פעולה; חוקי-ה-RTDB הנדרשים מתועדים ב-fleet-remote-update.md.

### 3) `ramada-web` — כמעט כלום
רק לאמת את הנתיב המדויק של דף ההגדרות (`/setup`) ואת מבנה ה-Firebase config של פרויקט.
**דורש צירוף ל-scope של הסשן** (כרגע ה-scope מוגבל ל-`elibic/elevator-rpi` בלבד).

## 🔔 התראות (Email + Telegram) — מצב נוכחי ויעד
**קיים היום** (`shabbat_detector/notifier.py`, config ב-`rfid_config.json` → `notifications`):
- שני אירועים: כניסה/יציאה משבת (edge-triggered), ואין-תנועה ≥ N שעות-יום
  (`MovementWatchdog`, "לא כולל לילה").
- שני ערוצים: **Telegram** (REST ל-`api.telegram.org`, `bot_token`+`chat_id`),
  **Email** (SMTP דרך `smtplib`, ב-Gmail צריך App Password). WhatsApp = שלד לא ממומש.
- best-effort: כשל בשליחה רק נרשם ל-log, לעולם לא מפיל את ה-detector.
- הסודות (טוקנים/SMTP) חיים רק ב-`rfid_config.json` (מוחרג מ-Git).

**היעד** (לפי ההחלטה): ניהול ההתראות עובר לאדמין דשבורד. הבחנה חשובה:
- **ניהול ההגדרות** (מי מקבל, אילו אירועים, ספים, הפעלה/כיבוי) → מרכזי בדשבורד, פר-פרויקט.
- **השליחה בפועל** דורשת רכיב שרץ תמיד. web סטטי לא יכול לשלוח ברקע →
  צריך **Cloud Function** (או backend תמידי) על ה-Firebase.
- שתי גישות להחליט בהן בהמשך:
  1. **שליחה מרכזית מלאה** — Cloud Function שולחת הכל; ה-Pi רק מדווח אירועים.
     (מזיז את הסודות לענן.)
  2. **היברידי** — ניהול מרכזי, ההגדרות נדחפות לכל Pi, וה-Pi עדיין שולח. (סודות נשארים על ה-Pi.)
- **זיהוי "Pi מת/offline" חייב להיות בצד האדמין** (Cloud Function) — Pi שנפל לא מדווח על עצמו.

## 🔑 מה צריך כדי להתחיל לבנות
1. ✅ **config של פרויקט ה-Firebase של האדמין** — התקבל: `econtrolelevelev`
   (חסר `databaseURL` — להוסיף אם האדמין משתמש ב-RTDB).
2. ✅ **`elibic/ramada-web` ב-scope** של הסשן (אומת `/setup`→`setup.html` דרך `cleanUrls`).
3. ✅ **מודל ה-`secret_key` לעדכון מרחוק** — מומש (חלק 2); חוקי-RTDB ב-fleet-remote-update.md.
4. ⏳ **היכן ייווצר הריפו `ramada-admin`** (כנראה `elibic/ramada-admin`).

## 📁 קבצים רלוונטיים ב-elevator-rpi (להתמצאות מהירה)
- `shabbat_detector/firebase_client.py` — wrapper ל-Firebase RTDB REST (SSE streams + PATCH/POST).
  מבנה ה-DB: `/elevators/{id}` (קומה חיה), `/elevator_configs/{id}`, `/settings`,
  `/logs/shabbat_detector/{id}`.
- `shabbat_detector/notifier.py` — מנגנון ההתראות (Telegram/Email/Watchdog).
- `shabbat_detector/detector.py` — ה-detector הראשי שמחבר הכול.
- `rfid_config.example.json` — תבנית הקונפיג (כולל סקשן `notifications`).
- שירות systemd: `shabbat-detector`.

## ▶️ הצעד הבא
חלק 2 (צד ה-Pi) **מומש ונבדק** (14 unit-tests ב-`tests/test_fleet_agent.py`).
חלק 1 (הדשבורד) — **סקאפולד MVP נבנה** ונמסר כ-`ramada-admin.bundle` (טרם ב-GitHub;
יצירת הריפו נחסמה בסשן). רשימת המשימות המלאה — הנחתה, הגדרת Firebase, פריסה, ומיזוג
ל-`main` — מרוכזת ב-[`admin-dashboard-status.md`](admin-dashboard-status.md).
