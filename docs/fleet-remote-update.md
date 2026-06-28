# עדכון-מרחוק וניהול-צי — `fleet_agent.py`

תיעוד צד-ה-Pi של ניהול-הצי: דיווח גרסה/heartbeat לדשבורד-העל, וביצוע פקודת-עדכון
מרחוק המאומתת ב-`secret_key`. זהו **חלק 2** של תוכנית הדשבורד
(`docs/admin-dashboard-plan.md`, ובמיוחד `ramada-web/docs/admin-dashboard-handoff.md`).

> צד-הדשבורד (חלק 1) כבר בנוי בריפו `elibic/admin-dashboard` — הוא כותב פקודות
> ל-`/fleet/{id}/command` ומציג עמודת "גרסה / מקוון" לפי `/fleet/{id}`. עד עכשיו
> ה-Pi לא קרא/כתב ל-`/fleet` כלל; הסוכן הזה סוגר את הלולאה.

---

## תרשים זרימה

```
┌────────────────────┐   PATCH /fleet/{id}/command            ┌──────────────────┐
│  admin-dashboard   │  { action:"update", secret_key,        │   Firebase RTDB   │
│  (SDK מאומת)        │    requested_at }                ─────▶ │  של הפרויקט        │
│                    │                                        │   /fleet/{id}     │
│  קורא /fleet/{id}   │ ◀───── version · last_seen ·           └────────┬─────────┘
│  גרסה/מקוון/סטטוס   │        update_status                            │
└────────────────────┘                                                 │ GET command
                                                                       │ PATCH heartbeat
                                                          ┌────────────▼───────────┐
                                                          │   fleet_agent.py (Pi)   │
                                                          │  • heartbeat כל ~5 דק'  │
                                                          │  • מאמת secret_key      │
                                                          │  • מריץ ./setup.sh      │
                                                          │  • מדווח update_status  │
                                                          └─────────────────────────┘
```

---

## מודל הנתונים ב-`/fleet/{ELEVATOR_ID}`

| שדה | כותב | משמעות |
|------|------|--------|
| `version` | Pi | גרסת הקוד הרצה — מחרוזת `YYYY.MM.DD` (ראו "חוזה הגרסה"). |
| `commit` | Pi | `git rev-parse --short HEAD` — לזיהוי מדויק (הדשבורד מתעלם). |
| `last_seen` | Pi | epoch (שניות) של ה-heartbeat האחרון. הדשבורד = **offline** אם `now - last_seen > 660`. |
| `status` | Pi | `"online"` (אינפורמטיבי). |
| `update_status` | Pi | תוצאת העדכון האחרון: `updating` → `ok` / `failed: <reason>` / `rejected: bad secret_key`. |
| `secret_key` | שניהם | נלווה לכל כתיבה (מודל bearer-token; ראו אבטחה). הדשבורד **מסנן** מפתח זה בתצוגה. |
| `command` | דשבורד | `{ action:"update", secret_key, requested_at }`. הסוכן **מוחק** אותו אחרי ביצוע. |

הסוכן מנרמל את `FIREBASE_URL` שבקונפיג לשורש ה-DB (`scheme://host`) בדיוק כמו
`detector.py`, ולכן עובד עם כל צורה שהוזנה (`/elevators`, `/elevators.json`, או שורש).

---

## חוזה הגרסה (`version` ↔ `LATEST_VERSION`)

הדשבורד מסמן מעלית כ-**מעודכנת** רק כאשר `version === LATEST_VERSION` (השוואת
מחרוזות מדויקת; `LATEST_VERSION` קבוע בראש `admin-dashboard.js`).

הסוכן מדווח `version` לפי תוכן קובץ **`VERSION`** בשורש הריפו (`detect_version`, semver
כמו `1.0.0`). אם הקובץ חסר — fallback לתאריך ה-commit (`YYYY.MM.DD`).

> **בכל שחרור:** הקפץ את `VERSION` (למשל `1.0.0` → `1.0.1`) **ועדכן את `LATEST_VERSION`
> בדשבורד לאותו ערך**. אם הערך שה-Pi מדווח שווה ל-`LATEST_VERSION` → "מעודכן"; אחרת
> "עדכון זמין". יתרון על תאריך-commit: גם שני שחרורים באותו יום ניתנים להבחנה ולעדכון מרחוק.

---

## מודל ה-`secret_key` (מאושר: bearer-token, אימות בצד ה-Pi)

זהה למודל הקיים ב-`firebase_client.py` (`patch_elevator_config`, `append_detector_log`):
- **כתיבות** של ה-Pi נושאות את `secret_key` בגוף ה-PATCH.
- **פקודת-עדכון**: הדשבורד כותב `command` עם `secret_key` של הפרויקט (אותו ערך כמו
  `SECRET_KEY` ב-`rfid_config.json` של ה-Pi).
- **אימות**: הסוכן משווה את `secret_key` שבפקודה ל-`SECRET_KEY` המקומי
  (`hmac.compare_digest`, השוואת זמן-קבוע). רק בהתאמה — הוא מבצע. אחרת מדווח
  `rejected: bad secret_key` ומוחק את הפקודה.

### הגנת replay (מאושר: dedupe + מחיקת הפקודה)
- הסוכן מבצע פקודה רק אם `requested_at` שלה **חדש** מהאחרון שטופל (נשמר בקובץ-state
  נפרד, `state_fleet_{id}.json`).
- אחרי ביצוע — מוחק את `/fleet/{id}/command` (ב-`PATCH command:null`, כך שה-`secret_key`
  עדיין נלווה ועובר את חוקי-הכתיבה גם אם DELETE לא-מאומת חסום).
- **קריסה באמצע עדכון** (`setup.sh` מפעיל restart / נפילת-חשמל): לפני ההרצה נשמר
  `pending_update`. בהפעלה הבאה הסוכן **מפייס** (`_reconcile`) — מדווח `ok` עם הגרסה
  הטרייה ומנקה — בלי להריץ שוב.

---

## מה ה-Pi מבצע על פקודת `update` (מאושר: `sudo ./setup.sh`)

ברירת-מחדל: הסוכן מריץ `./setup.sh` (כבר רץ כ-root — ראו למטה) מתיקיית הריפו.
זהו המתקין המלא: `git pull` + venv/תלויות + שירותים — מכסה גם שינויי-infra, לא רק קוד.

רצף מלא:
1. שמירת `last_command_requested_at` + `pending_update` (force) — **לפני** ההרצה.
2. דיווח `update_status: "updating"` + מחיקת הפקודה (single-shot).
3. הרצת `setup.sh` (timeout 30 דק'). מעבירים `FLEET_AGENT_UPDATE=1` (שלא יהרוג אותנו —
   ראו למטה) ו-`SUDO_USER=<בעל-הריפו>` (כדי ש-`setup.sh` יחזיר בעלות `.git` למשתמש
   ולא ישבור `git pull` עתידי).
4. ניקוי `pending_update`; דיווח `ok` (rc=0) או `failed: <reason>`.
5. בהצלחה — **restart-עצמי** של `fleet-agent` כדי לטעון את קוד-הסוכן המעודכן
   (`FLEET_SELF_RESTART`, ברירת-מחדל פעיל).

> **למה הסוכן לא נהרג ע"י `setup.sh`:** `install_fleet_agent` מפעיל את השירות ב-`start`
> (לא `restart`) כשהמשתנה `FLEET_AGENT_UPDATE=1` נוכח — בדיוק כמו שירות ה-web. כך
> התהליך ששולח את `setup.sh` שורד, מדווח `ok`, ורק אז מבצע restart-עצמי מסודר.
> בהרצה אינטראקטיבית (`sudo ./setup.sh` ידני) המשתנה לא קיים → `restart`, לרענון מיידי.

---

## התקנה ושילוב

מותקן אוטומטית ע"י `setup.sh` → `python -m installer` (`install_fleet_agent`):
- כותב `/etc/systemd/system/fleet-agent.service` מהתבנית `systemd/fleet-agent.service.in`.
- `enable` + הפעלה (ראו הערת `start`/`restart` למעלה).
- מופיע ב-`all_status` ובכלי-ה-web המקומי (אפשר start/stop/restart דרך הממשק).

**השירות רץ כ-root** — הכרחי, כי פקודת-עדכון מריצה את `setup.sh` (שדורש root) ו-`systemctl restart`.
זוהי החלטת-אבטחה מודעת: השער היחיד לפני הרצת-קוד על ה-Pi הוא ה-`secret_key` **וחוקי-RTDB**.

לוגים: `journalctl -u fleet-agent -f`.

---

## ⚠️ חוקי-RTDB פר-פרויקט (קריטי לאבטחה)

`secret_key` שנכתב לתוך `/fleet/{id}` נשאר ב-DB בטקסט גלוי. **כל מי שיש לו הרשאת
קריאה ל-`/fleet` יכול לקצור אותו** ואז לזייף פקודת-עדכון. לכן חובה לגדר את `/fleet`.

### מודל A — תואם לפריסה הנוכחית (ברירת-מחדל, חלש יותר)
ה-detector הקיים קורא `/elevator_configs` ו-`/settings` ב-GET לא-מאומת ⇒ הקריאה
פתוחה. אם משאירים כך, גם `secret_key` ב-`/fleet` חשוף בקריאה. מתאים רק אם כתובת
ה-DB אינה ידועה ציבורית. גדרו לפחות את ה**כתיבה** לפי הסוד:

```json
{
  "rules": {
    "fleet": {
      "$eid": {
        ".read": true,
        ".write": "newData.child('secret_key').val() === 'SECRET-OF-THIS-PROJECT'"
      }
    }
  }
}
```

### מודל B — מומלץ (אימות מלא, הסוד לא נחשף בקריאה)
לדרוש אימות לכל `/fleet`, ולתת לסוכן טוקן דרך `?auth=` (הגדרת `FLEET_AUTH_TOKEN`
בקונפיג — למשל ה-*database secret* של הפרויקט). הדשבורד כבר ניגש מאומת (`auth != null`),
כך שהוא ממשיך לעבוד בלי שינוי:

```json
{
  "rules": {
    "fleet": {
      "$eid": { ".read": "auth !== null", ".write": "auth !== null" }
    }
  }
}
```

> מעבר מלא למודל B דורש להוסיף `?auth=` גם ל-`firebase_client.py` של ה-detector
> (קריאות `/elevator_configs` ו-`/settings`), אחרת ה-detector ייחסם. עד אז — מודל A
> פעיל כברירת-מחדל, והסוכן תומך כבר עכשיו ב-`FLEET_AUTH_TOKEN` (one-liner בקונפיג).

---

## מפתחות-קונפיג (`rfid_config.json → settings`, כולם אופציונליים)

| מפתח | ברירת-מחדל | תיאור |
|------|------------|--------|
| `FLEET_ENABLED` | `true` | `false` → הסוכן רדום (השירות חי אך לא מדווח/מבצע). |
| `FLEET_REPORT_INTERVAL` | `300` | מרווח ה-heartbeat (שניות). חייב < `660` (סף offline בדשבורד). |
| `FLEET_POLL_INTERVAL` | `15` | תדירות בדיקת `command` (שניות). |
| `FLEET_UPDATE_COMMAND` | `["./setup.sh"]` | פקודת-העדכון. מחרוזת=shell, רשימה=argv. |
| `FLEET_SELF_RESTART` | `true` | restart-עצמי אחרי עדכון מוצלח (לטעון קוד-סוכן חדש). |
| `FLEET_REPO_DIR` | תיקיית-הריפו | מיקום הריפו (זיהוי אוטומטי מ-`shabbat_detector/`). |
| `FLEET_AUTH_TOKEN` | `""` | טוקן `?auth=` ל-REST (מודל B). ריק = ללא אימות (מודל A). |
| `LOG_BACKUP_ENABLED` | `false` | מפעיל גיבוי-לוגים **שבועי אוטומטי** (הכפתור הידני בדשבורד עובד גם בלי זה). |
| `LOG_BACKUP_REPO_URL` | `""` | https URL לריפו הלוגים, רשאי להטמיע token כתיבה: `https://x-access-token:<PAT>@github.com/elibic/elevator-logs.git`. |
| `LOG_BACKUP_INTERVAL_DAYS` | `7` | מרווח הגיבוי האוטומטי (ימים). |
| `LOG_BACKUP_GIT_NAME` / `LOG_BACKUP_GIT_EMAIL` | `elevator-pi` / `…@econtrol.co.il` | זהות ה-commit. |
| `LOG_BACKUP_DIR` | `/var/lib/elevator-logs` | clone מקומי קבוע (root). |

`ELEVATOR_ID`, `SECRET_KEY`, `FIREBASE_URL` — כבר קיימים בקונפיג; הסוכן משתמש בהם כמו ה-detector.

---

## גיבוי לוגים (`backup_logs`) - שירות-צי שני

לצד `update`, הדשבורד יכול לכתוב פקודת `{ "action": "backup_logs", "secret_key", "requested_at" }`
לאותו `/fleet/{id}/command` (כפתור **"גבה לוגים"** בכרטיס המעלית). הסוכן מאמת את ה-`secret_key`
(אותה הגנת-replay כמו `update`), ומריץ את `shabbat_detector/log_backup.py`:

- כל Pi דוחף את תיקיית `logs/` שלו לתת-תיקייה **`{ELEVATOR_ID}/`** בריפו לוגים **נפרד**
  (למשל `elibic/elevator-logs`) - כך אין קונפליקטים בין מעליות (`pull --rebase` + retry על מרוץ-ref).
- רץ גם **שבועית אוטומטית** (`LOG_BACKUP_ENABLED` + `LOG_BACKUP_INTERVAL_DAYS`), עם `last_backup`
  ב-`state_fleet_{id}.json`. מדווח `backup_status` (`backing_up`/`ok`/`failed: …`) ל-`/fleet/{id}`.
- **אבטחה:** הריפו יכול להיות ציבורי (אין סודות בלוגים), אבל `push` דורש **token כתיבה**
  שמוטמע ב-`LOG_BACKUP_REPO_URL` - **נפרד** מטוקן משיכת-הקוד (שהוא read-only) ו-scope **רק** לריפו
  הלוגים. הסוכן מנקה הופעות `secret_key` מהלוגים לפני push, ולעולם לא מתעד את ה-URL עם הטוקן.

## תצוגת מצב-שירותים בדשבורד

ה-heartbeat כולל כעת `services` (מצב 4 שירותי ה-systemd לפי `systemctl is-active`):
`rfid-tracker`, `shabbat-detector`, `fleet-agent`, `elevator-config-web`. הדשבורד מציג תווית-צבע
לכל אחד בכרטיס המעלית. השדה נכתב באותו PATCH שנושא `secret_key`, אז חוקי-ה-RTDB על `/fleet` חלים כרגיל.

---

## בדיקות ואימות

```bash
# heartbeat יחיד בלי לבצע כלום (אימות חיבור/גרסה):
python -m shabbat_detector.fleet_agent --config rfid_config.json --once --test-mode

# מצב בדיקה רציף (מדפיס PATCH-ים, לא כותב ל-Firebase, לא מריץ setup.sh):
python -m shabbat_detector.fleet_agent --config rfid_config.json --test-mode --log-level DEBUG

# בדיקת קצה-לקצה אמיתית:
#   1) הסוכן רץ ⇒ /fleet/{id} מקבל version+last_seen ⇒ הדשבורד מראה "מקוון".
#   2) בדשבורד: "עדכן" ⇒ /fleet/{id}/command נכתב.
#   3) הסוכן מאמת, מריץ setup.sh, מדווח update_status:"ok", מנקה את command.
#   4) הגרסה החדשה מופיעה בדשבורד (אם LATEST_VERSION עודכן ⇒ "מעודכן").
```

### פתרון תקלות
- **מופיע offline** למרות שהסוכן רץ → בדקו ש-`last_seen` מתעדכן (`journalctl -u fleet-agent`),
  וש-`FLEET_REPORT_INTERVAL < 660`.
- **`rejected: bad secret_key`** → ה-`secret_key` ב-`/projects` בדשבורד אינו תואם ל-`SECRET_KEY`
  שב-`rfid_config.json` של ה-Pi.
- **הפקודה לא מתבצעת** → ודאו שחוקי-RTDB מאפשרים לסוכן לקרוא `/fleet/{id}/command`
  (במודל B צריך `FLEET_AUTH_TOKEN`).
- **`git pull` נשבר אחרי עדכון** (Permission denied) → `.git` בבעלות root; `setup.sh` החדש
  מתקן עם `SUDO_USER` שהסוכן מעביר. ידנית: `sudo chown -R $USER:$USER .`.
- **תמיד "עדכון זמין"** → עדכנו את `LATEST_VERSION` בדשבורד לתאריך ה-commit של השחרור.

---

## קבצים
- `shabbat_detector/fleet_agent.py` — הסוכן.
- `systemd/fleet-agent.service.in` — תבנית systemd (נפרסת ע"י המתקין).
- `shabbat_detector/fleet-agent.service` — עותק סטטי (להתקנה ידנית; המתקין מעדיף את התבנית).
- `installer/core.py` — `install_fleet_agent()` (משולב ב-`install_all`, `all_status`, `service_action`).
