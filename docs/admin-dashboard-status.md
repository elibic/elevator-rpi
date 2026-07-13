# מצב נוכחי ומשימות שנותרו — יוזמת דשבורד-העל (ramada-admin)

> מסמך **handoff מעשי**. משלים את [`admin-dashboard-plan.md`](admin-dashboard-plan.md)
> (החלטות + ארכיטקטורה) ואת [`fleet-remote-update.md`](fleet-remote-update.md)
> (פרוטוקול חלק 2). נכתב כדי שאפשר יהיה למסור אותו **כהנחיה** בסשן חדש או בתהליך
> האיחוד והסידור של הפרויקט.
>
> ⚠️ **כל העבודה של היוזמה יושבת על הענף `claude/vibrant-brown-ieo4zv`** —
> **לא מוזג ל-`main`, לא נפרס, וחלק 1 עדיין לא ב-GitHub בכלל.**

## סטטוס במבט-על

| חלק | מה | נבנה? | ב-GitHub? | מוזג ל-`main`? | נפרס? |
|-----|----|:-----:|:---------:|:-------------:|:-----:|
| **2** | צד ה-Pi: דיווח גרסה + עדכון מרחוק (`fleet_agent.py`) | ✅ (+14 טסטים) | ✅ elevator-rpi, ענף הפיצ'ר | ❌ | ❌ (ה-Pi-ים לא משכו) |
| **1** | הדשבורד `ramada-admin` (אתר סטטי) | ✅ סקאפולד MVP (9 קבצים) | ❌ **קיים רק ב-bundle** | ❌ | ❌ |
| **3** | `ramada-web`: אימות `/setup` | ✅ אומת (`cleanUrls`→`setup.html`) | — | — | — |

---

## חלק 2 — צד ה-Pi (מומש, על הענף)
קבצים ב-`elevator-rpi` (ענף `claude/vibrant-brown-ieo4zv`):
- `shabbat_detector/fleet_agent.py` — `FleetAgent`: דיווח `/fleet/{id}` כל ~5 דק',
  watcher לפקודות, אימות secret (constant-time), idempotency לפי `requested_at`,
  `git pull --ff-only` על branch קבוע + יציאה מבוקרת (systemd `Restart=always`
  מפעיל מחדש עם הקוד החדש, בלי sudo), ודיווח תוצאה.
- `shabbat_detector/firebase_client.py` — `patch_fleet_status` / `get_fleet_status` / `subscribe_fleet_command`.
- `shabbat_detector/detector.py` — הפעלת ה-agent ב-`run()` (כבוי תחת `--test-mode`).
  מתגים ב-`rfid_config.json`: `FLEET_AGENT_ENABLED`, `FLEET_REPORT_INTERVAL`,
  `FLEET_REMOTE_UPDATE_ENABLED`, `FLEET_REQUIRE_COMMAND_SECRET`, `FLEET_UPDATE_BRANCH`.
- `tests/test_fleet_agent.py` — 14 טסטים (אימות / idempotency / זרימת עדכון).
- `docs/fleet-remote-update.md` — פרוטוקול, מבנה `/fleet`, וחוקי-RTDB נדרשים בכל פרויקט.

**להפעלה בשטח** (אחרי מיזוג ל-`main`): בכל Pi → `git pull` + `systemctl restart
shabbat-detector` → ה-FleetAgent מתחיל לדווח, ומכאן ניתן לעדכן מרחוק מהדשבורד.

---

## חלק 1 — הדשבורד (נבנה, לא נחת ב-GitHub)
סקאפולד MVP מלא, נמסר כ-**`ramada-admin.bundle`** (git bundle, ענף
`claude/vibrant-brown-ieo4zv`). **לא ניתן היה לדחוף מהסשן**: יצירת ריפו נחסמה
(403 — ל-GitHub App אין הרשאת יצירה), וה-proxy חוסם push לריפו שאינו ב-scope.

9 קבצים:
- `firebase.json`, `.firebaserc` (project=`econtrolelevelev`), `.gitignore`
- `public/index.html` — login + גריד + מודאל "הוסף פרויקט" (RTL, ערכת נושא כהה)
- `public/style.css`
- `public/firebase-config.js` — אתחול `econtrolelevelev`. **`databaseURL` ריק — להשלים.**
- `public/admin-dashboard.js` — Auth · טעינת `/projects` · Firebase app **משני** לכל
  פרויקט (`/elevators`+`/elevator_configs`+`/fleet`) · כפתור ⚙️ הגדרות (`/setup`) ·
  כפתור ⬆️ עדכן (כותב `/fleet/{id}/command` לפי פרוטוקול חלק 2).
- `README.md`, `public/CLAUDE.md` — כולל סכימת `/projects` וחוקי-RTDB לאדמין.

סכימת `/projects` (RTDB של האדמין): `{ name, subdomain, secret_key, webConfig:{…}, createdAt }`.

---

## ✅ משימות שנותרו — רשימת ביצוע (מתאים למסירה כהנחיה)

**A. הנחתת הדשבורד ב-GitHub**
- [ ] ליצור ריפו ריק `elibic/ramada-admin` (private, בלי README/gitignore/license).
- [ ] להעלות את הסקאפולד: `git clone ramada-admin.bundle` → `git remote set-url origin …` → `git push`,
      **או** בסשן חדש ששולב בו הריפו — לצרף את ה-bundle ולתת ל-Claude לדחוף.

**B. הגדרת פרויקט ה-Firebase של האדמין (`econtrolelevelev`)**
- [ ] להפעיל Realtime Database ולמלא `databaseURL` ב-`public/firebase-config.js` (כרגע ריק).
- [ ] ליצור משתמש Auth (email/password) לבעלים.
- [ ] חוקי-RTDB ל-`/projects`: read/write רק ל-`auth != null` (מכיל `secret_key`-ים!). ראה `README.md`.

**C. רישום הפרויקטים**
- [ ] לכל פרויקט (Ramada, Nitza20…): בדשבורד → "הוסף פרויקט" → להדביק web config + subdomain + secret_key.

**D. פריסת הדשבורד**
- [ ] `firebase deploy --only hosting -P econtrolelevelev`, ולחבר subdomain (למשל `admin.econtrol.co.il`).

**E. הפעלת חלק 2 בשטח**
- [ ] למזג `claude/vibrant-brown-ieo4zv` → `main` ב-`elevator-rpi`.
- [ ] בכל Pi: `git pull` + `systemctl restart shabbat-detector`.
- [ ] בכל פרויקט: להוסיף חוקי-RTDB ל-`/fleet` (קריאה + כתיבת `command` עם secret) — ראה `fleet-remote-update.md`.

**F. עתידי (החלטות פתוחות)**
- [ ] התראות → **Cloud Function** (מרכזי-מלא מול היברידי — טרם הוכרע). זיהוי "Pi offline" בצד האדמין.
- [ ] חלק 3: (רשות) לתעד מבנה ה-config של פרויקט; הנתיב `/setup` כבר אומת.

---

## 🔒 אבטחה (לא לשבור באיחוד)
- **`secret_key` לעולם לא ב-Git** — חי רק ב-`rfid_config.json` (על ה-Pi) וב-`/projects`
  מאחורי login (אדמין).
- `apiKey` = מזהה web ציבורי → מותר ב-Git.
- **חוקי-ה-RTDB הם קו-ההגנה**: `/projects` ו-`/fleet/{id}/command` חייבים אימות/secret.

## 📌 מצביעים
| נושא | מיקום |
|------|-------|
| החלטות + ארכיטקטורה | `docs/admin-dashboard-plan.md` |
| פרוטוקול חלק 2 + חוקי-RTDB | `docs/fleet-remote-update.md` |
| קוד חלק 2 | `shabbat_detector/fleet_agent.py` (+ שילוב ב-`detector.py`) |
| קוד חלק 1 | ה-bundle `ramada-admin.bundle` (טרם ב-GitHub) |
