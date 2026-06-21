# Fleet — דיווח גרסה ועדכון מרחוק (צד ה-Pi)

מימוש **חלק 2** מתוך [`admin-dashboard-plan.md`](admin-dashboard-plan.md): כל Pi
מדווח את גרסתו וסטטוסו ל-Firebase של הפרויקט, ומאזין לפקודת-עדכון מרחוק מהדשבורד.

הקוד: [`shabbat_detector/fleet_agent.py`](../shabbat_detector/fleet_agent.py),
משולב ב-`detector.py` ורץ ב-thread-ים ברקע יחד עם ה-detector.

---

## מבנה ה-DB (RTDB של הפרויקט, לכל מעלית)

```
/fleet/{ELEVATOR_ID}
    version                    "a1b2c3d"     ← git sha קצר נוכחי
    branch                     "main"
    status                     "online"
    last_seen                  1750000000    ← epoch שניות (דיווח אחרון)
    last_seen_str              "2026-06-21 19:50:00"
    update_status              "online" | "updating" | "restarting" |
                               "up_to_date" | "failed" | "disabled"
    update_error               "<tail של פלט git על כשל>"
    last_applied_requested_at  1750000000    ← idempotency (הפקודה שכבר טופלה)

/fleet/{ELEVATOR_ID}/command   ← הדשבורד כותב לכאן; ה-Pi קורא
    action                     "update"
    secret_key                 "<הסוד של הפרויקט>"
    requested_at               1750000123    ← epoch; חייב לגדול בכל פקודה חדשה
```

ה-Pi מזהה **online/offline** בצד הדשבורד לפי התיישנות `last_seen` (למשל מעל
פעמיים מרווח-הדיווח ⇐ "לא מקוון"). דיווח רץ בהפעלה ואז כל ~5 דקות.

## פרוטוקול העדכון

1. הדשבורד כותב `/fleet/{id}/command = {action:"update", secret_key, requested_at:<now>}`
   (אובייקט שלם, לא שדה בודד).
2. ה-Pi מקבל את הפקודה ב-SSE, ו**מאמת**:
   - `action == "update"`,
   - `secret_key` תקין (השוואת `hmac.compare_digest` — constant-time),
   - `requested_at > last_applied_requested_at` (idempotency — לא לרוץ שוב על אותה פקודה).
3. אם תקין: `git pull --ff-only origin <branch>` בשורש הריפו →
   - **הצליח עם שינוי קוד:** מדווח `restarting` ⇐ יוצא בצורה מבוקרת (SIGTERM →
     שמירת state) ⇐ **systemd (`Restart=always`) מפעיל מחדש עם הקוד החדש** ⇐ התהליך
     החדש מדווח `version` חדש עם `status:"online"`. (אין צורך ב-`sudo systemctl` —
     השירות רץ כמשתמש לא-root.)
   - **כבר מעודכן:** `up_to_date`, ללא הפעלה-מחדש.
   - **כשל** (למשל לא ניתן fast-forward): `failed` + `update_error`. **לא** נכנס
     ללולאת-retry — `last_applied_requested_at` מסומן, ועדכון חוזר דורש פקודה חדשה
     (עם `requested_at` חדש).

ה-pull הוא `--ff-only` על branch **קבוע** (הנוכחי, או `FLEET_UPDATE_BRANCH`) — לא
מבצע merge ולא מושך ref שרירותי.

## ⚠️ אבטחה — חוקי RTDB נדרשים (בצד הפרויקט)

ה-Pi מריץ `git pull` + הפעלה-מחדש לפי טריגר מרוחק, ולכן יש לאבטח את צומת הפקודה
**ברמת חוקי-ה-RTDB של הפרויקט** (לא בריפו הזה):

- **כתיבה** ל-`/fleet/{id}/command` תותר רק עם `secret_key` תקין
  (`newData.child('secret_key').val() === <secret>`) — בדיוק מודל ה-PATCH הקיים.
- **קריאה** של `/fleet/{id}/command` צריכה להיות מוגבלת ככל האפשר; הסוד מופיע שם
  כ-defense-in-depth, אך אין לחשוף אותו לקריאה ציבורית. אם מדיניות הקריאה של
  הפרויקט פתוחה (כמו `/elevators` לקיוסקים), עדיף לסמוך על חוק-הכתיבה בלבד ולכבות
  את דרישת-הסוד בצד ה-Pi (`FLEET_REQUIRE_COMMAND_SECRET=false`).
- ה-Pi מאמת את הסוד בכל מקרה (constant-time) כשכבת הגנה נוספת.

> ה-`secret_key` של הפרויקט הוא **סוד אמיתי** וחי רק ב-`rfid_config.json` (מוחרג
> מ-Git). אין לכתוב אותו בשום קובץ מנוהל.

## הגדרות (אופציונליות) ב-`rfid_config.json` → `settings`

| מפתח | ברירת מחדל | משמעות |
|------|-----------|--------|
| `FLEET_AGENT_ENABLED` | `true` | מתג-על לכל ה-fleet agent |
| `FLEET_REPORT_INTERVAL` | `300` | מרווח דיווח בשניות |
| `FLEET_REMOTE_UPDATE_ENABLED` | `true` | האם להאזין לפקודות-עדכון (כיבוי = דיווח בלבד) |
| `FLEET_REQUIRE_COMMAND_SECRET` | `true` | לדרוש `secret_key` תקין בכל פקודה |
| `FLEET_UPDATE_BRANCH` | הענף הנוכחי | הענף ל-`git pull --ff-only origin <branch>` |

ברירות-המחדל מאפשרות ל-Pi קיים לקבל את הפיצ'ר ע"י `git pull` + restart בלבד, ללא
עריכת קונפיג. תחת `--test-mode` ה-agent כבוי.

## איך הדשבורד (חלק 1) מפעיל עדכון

```js
// פותחים Firebase app משני עם ה-config של הפרויקט, ואז:
db.ref(`/fleet/${elevatorId}/command`).set({
  action: "update",
  secret_key: project.secret_key,      // מתוך /projects (מאחורי login)
  requested_at: Date.now() / 1000,     // epoch שניות, גדל בכל פקודה
});
// ואז מאזינים ל-/fleet/${elevatorId}.update_status ו-.version לקבלת התוצאה.
```
