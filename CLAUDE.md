# CLAUDE.md - elevator-rpi

קוד ה-Raspberry Pi למערכת מעלית שבת: קריאת תגיות RFID, זיהוי קומות וזיהוי שבת,
ושליחת מצב המעלית ל-Firebase.

## ✍️ סגנון כתיבה (חובה)
- בכל טקסט מול-משתמש (UI, הודעות, דפים) ובקוד/דוקס: השתמש **תמיד** במקף רגיל `-` ולעולם
  **לא** במקף ארוך `—` (em-dash). זו העדפה קשיחה של המשתמש.

## ⚠️ סודות - קריטי
- `rfid_config.json` מכיל את `SECRET_KEY` ואת מיפוי תגיות-RFID של מעלית מסוימת.
  הוא **מוחרג ב-`.gitignore`** - לעולם אל תוסיף אותו ל-Git, ואל תכתוב את ערך ה-`SECRET_KEY`
  בשום קובץ מנוהל (גם לא בקובץ הזה).
- תבנית למבנה: `rfid_config.example.json` (הסוד מרוקן). הקונפיג האמיתי חי על כל Pi בנפרד.
- **גיבוי-קונפיג:** מותר לגבות עותק **מסונן** של הקונפיג (מיפוי-תגים) לריפו הגיבוי - `log_backup.py`
  מסיר את ה-`SECRET_KEY` וכל טוקן לפני push (`***REDACTED***`, fail-closed). הקובץ המקורי לעולם
  לא נכנס ל-Git. ראה סקשן "ניהול-צי / עדכון מרחוק".

## התקנה / עדכון
- **Pi חדש:** `git clone` → `sudo ./setup.sh` (אשף טרמינל) או `sudo ./setup.sh --web` (גרפי).
  מתקין הכל: דרייברים, venv, **שלושה שירותים** (`rfid-tracker` + `shabbat-detector` +
  `fleet-agent`), כלי-web מקומי (`elevator-config-web` על `127.0.0.1:8080`), קיצור
  שולחן-עבודה + **הפעלה-אוטומטית של הדשבורד בבוט** (XDG-autostart), Pi Connect.
- **Pi קיים - עדכון בטוח (לא נוגע בקונפיג):**
  ```bash
  cd ~/elevator-RFID            # או ~/elevator-rpi
  sudo chown -R $USER:$USER .   # מתקן .git שאולי root-owned מ-sudo setup ישן
  git pull                      # rfid_config.json מוחרג ⇒ לא נגעים בו
  sudo systemctl restart rfid-tracker shabbat-detector
  ```
- **גוצ'ה:** `git pull` תחת sudo יוצר קבצי `.git` בבעלות root ושובר pull עתידי של המשתמש
  (Permission denied) - לכן ה-`chown`. `setup.sh` החדש מתקן זאת אוטומטית.
- לוגים: `journalctl -u shabbat-detector -f`. `shabbat_detector/install.sh` ו-`deploy_elevator.sh`
  - **deprecated** (מוחלפים ע"י `setup.sh`).
- פיתוח בענף ייעודי; אחרי push → fast-forward ל-`main` (`git push origin <branch>:main`).
  **ה-Pi מושך מ-`main`.** העברת-סשן מפורטת: `docs/HANDOFF.md`.

## Firebase
- פרודקשן: `https://ramada-elev-default-rtdb.europe-west1.firebasedatabase.app` (אזור EU בלבד!)
- עדכון מצב מעלית: PATCH ל-`/elevators/{ELEVATOR_ID}.json`, עם `secret_key` מתוך הקונפיג.

## מודל נתונים ב-Firebase (מ-`ramada-web/public/setup.html` - מקור-אמת)
- **`elevator_configs/{id}`**: קונפיג + **`SHABBAT_OVERRIDE`** (`auto`/`force_on`/`force_off` -
  מחרוזות, **לא** true/false) + **`SHABBAT_SOURCE`** (`auto`/`schedule`/`none`; חסר = ירושה
  מברירת המחדל הפרויקטלית) + `SHABBAT_DETECTOR{state,last_transition_reason}` +
  `SHABBAT_ACTIVE` (פלט ה-Pi).
- **`settings`** (גלובלי): `HEBCAL_GATE_ENABLED` (+windows), `SHABBAT_DETECTION{ספי FSM}`,
  `YOM_TOV_SHENI`, `FLOOR_ALIASES`, **`SHABBAT_SOURCE_DEFAULT`** (`auto`/`schedule`/`none`),
  **`SHABBAT_SCHEDULE_BEFORE_MINUTES`/`SHABBAT_SCHEDULE_AFTER_MINUTES`** (אופסטים מדויקים
  למצב לוח-זמנים; נפרדים מחלונות השער הרחבים), `GEO_NAME_ID`.
- **`elevators/{id}`**: קומה חיה (tracker). **`fleet/{id}`**: version/last_seen/command (עדכון מרחוק) +
  **`services`** (מצב 4 שירותי systemd, מוצג בדשבורד) + **`backup_status`** (גיבוי-לוגים).
- `FIREBASE_URL` בקונפיג: detector+monitor מנרמלים ל-**root** של ה-DB (urlsplit), עם/בלי `.json`.
- FSM: `NORMAL → CANDIDATE_SHABBAT → SHABBAT (→ CANDIDATE_EXIT)`. `SHABBAT_ACTIVE` נדלק רק עם
  מחזורים תואמים רצופים **ובחלון hebcal** - או `SHABBAT_OVERRIDE=force_on`.

## זיהוי-שבת - יציבות וכיול (עודכן יולי 2026, גרסה 1.0.10)
- **שער-משך-מחזור כבוי כברירת-מחדל** (`CYCLE_DURATION_TOLERANCE_PCT=0`): המחזור-הצפוי מחושב
  עכשיו מדפוס-העצירות האמיתי לכל כיוון (`STOPPING_FLOORS_UP/DOWN` + `FLOOR_WAITS` +
  `TIME_PASS_FLOOR`) ב-`expected_cycle_period_from_config`, במקום `2×span×TPF` הנאיבי שניפח
  את הצפי וגרם ל-A לא-להיכנס ול-D לרצד. השער כבוי גלובלית כדי שפרויקט בלי רשימות-עצירה
  לכל-כיוון לא ייפסל בטעות; הנוסחה המתוקנת עדיין מעגנת את יציאת-הקצב (`MISSED_CYCLE_FACTOR`).
  להפעלה פר-פרויקט: ערך >0 ב-`settings/SHABBAT_DETECTION`. (ה-placeholder ב-`setup.html`
  שבמונו-רפו מיושר ל-0.)
- **אנטי-ריצוד ב-FSM:** הצינון (`COOLDOWN_S`) **נאכף** בפועל (היה קוד-מת); כניסה ל-CANDIDATE_EXIT
  מנקה ראיות (יציאה דורשת ראיות טריות - חוסם "יציאה תוך 21 שניות"); מחזור-לא-תואם נספר פעם
  אחת (בלי ספירה-כפולה מול חריגת-אמצע-מחזור); מחזור-מציל מחדֵש את זמן-הדבקה.
- **מדידת-מחזורים (`cycle_analyzer`):** קריאת-קצה מוחמצת אחת כבר לא זורקת מחזור שלם
  (`_try_complete_missed_apex`); סף-idle נגזר מהקונפיג (לא 300 קבוע) ולא מאבד חצי-מחזור;
  קומה `0` נשמרת (לא נזרקת כ-falsy); דיכוי flap בין שני תגים סמוכים (בלי לפגוע באקספרס).
- **Firebase/SSE (`firebase_client`+`detector`):** ההאזנה מכבדת את שדה `path` - כתיבת שדה-בודד
  (כמו `SHABBAT_OVERRIDE` לבד מהדשבורד) מגיעה ל-Pi ולא נזרקת בשקט; כתיבת `SHABBAT_ACTIVE`
  קריטית עם retry; **בזמן כפייה (`force_on/force_off`) לא נכתב ריצוד-FSM פנימי ל-Firebase**
  (מנע דליפת-ריצוד להתראות ולולאת-הד); reconnect עם backoff+jitter; `_fsm_lock`=RLock
  (בלי deadlock בכיבוי → המצב נשמר).
- **גיבוי-לוגים:** ריפוי-עצמי של הקלון המקומי במקום כשל `non-fast-forward` קבוע (הסיבה
  שלוגי C לא עלו). ראה `log_backup.py`.

## אמינות-אתחול וגיבוי-לוגים (עודכן יולי 2026, גרסה 1.0.11)
- **תוקן מעגל-סדר systemd שמנע מ-`rfid-tracker` לעלות בריבוט:** `fix_cp210x.service` הכריז
  `After=multi-user.target` וגם `WantedBy=multi-user.target` (שמשמעו סדר הפוך) - נוצר מעגל
  `multi-user -> rfid-tracker -> fix_cp210x -> multi-user`, ו-systemd שבר אותו במחיקת job
  שרירותי בזמן boot. הסימפטום: שירות enabled אך dead, בלי אף שורת journal (וכשהקורבן היה
  fix_cp210x - ה-tracker עלה בלי דרייבר ונכנס ללולאת-קריסות serial). התיקון: הוסרה שורת
  ה-`After`. **בפריסה קיימת נדרש `sudo ./setup.sh`** (או עריכת היחידה המותקנת) כדי שהיחידה
  המעודכנת תיכתב ל-`/etc/systemd/system`.
- **boot-rescue ב-`fleet_agent`:** רשת-ביטחון שרצה פעם אחת ~2 דק' אחרי עליית ה-agent: שירות
  enabled ש**מעולם לא התחיל** מאז ה-boot (`InactiveExitTimestampMonotonic==0` - טביעת-האצבע
  של job שנמחק) מופעל אוטומטית ומדווח ל-`fleet/{id}/boot_rescue`. עצירה ידנית/סריקת-תג
  (שהשירות היה פעיל בהן קודם) לעולם לא נדרסת. כיבוי: `FLEET_BOOT_RESCUE=false`.
- **גיבוי-לוגים לא נשבר יותר על קובץ ענק:** GitHub דוחה קובץ מעל 100MB (שגיאת `gh.io/lfs` -
  מה שהכשיל את גיבוי B ב-TEST). קובץ מעל `LOG_BACKUP_MAX_FILE_MB` (ברירת-מחדל 90) נדחס
  ל-`.gz` דטרמיניסטי (בלי commit-ים מיותרים כשאין שינוי); אם גם דחוס גדול מדי - מדולג עם
  קובץ-הערה `.TOO_LARGE.txt`. ההעתקה זורמת שורה-שורה (בלי לקרוא 100MB+ ל-RAM של Pi Zero).
- **בלימת הצפת "Tag Change" ב-tracker:** ריצוד בין שני תגים כתב כמה שורות בשנייה (עשרות MB
  ביום - כך נוצר הקובץ שחצה 100MB). כל תג נרשם לכל היותר פעם ב-`TAG_LOG_COOLDOWN_S` (ברירת-מחדל
  60ש'); הדיכוי מסוכם בשורה תקופתית. תג חדש/מעבר-קומה אמיתי נרשם מיידית; שליחת הקומות לענן
  לא מושפעת כלל.

## דשבורד מקומי - פתיחה אוטומטית ובלי חלונית-הרצה (עודכן יולי 2026, גרסה 1.0.12)
- **הדשבורד עולה אוטומטית בבוט:** המתקין כותב רשומת `~/.config/autostart/elevator-dashboard.desktop`
  (XDG) שמריצה את `installer/open-dashboard.sh`. RPi OS מכבד XDG-autostart גם ב-LXDE (Bullseye)
  וגם ב-labwc/wayfire (Bookworm), אז זה נייד. `open-dashboard.sh` ממתין (חסום, עד ~30ש') שהפורט
  `127.0.0.1:8080` יענה לפני פתיחת Chromium, כדי שב-boot הדפדפן לא ייפתח על "connection refused".
  כיבוי: `settings.DASHBOARD_AUTOSTART=false` (המתקין גם מסיר רשומה קיימת).
- **חלונית "Execute File" של PCManFM לא קופצת יותר:** `_set_pcmanfm_quick_exec` כותב `quick_exec=1`
  ל**כל** פרופילי pcmanfm (הקיימים + `LXDE-pi` + `default`), כך שההגדרה חלה בכל גרסת OS. שים לב:
  זה נכנס לתוקף כשמנהל-הקבצים טוען מחדש קונפיג - כלומר **בהתחברות/ריבוט הבא**, לא בזמן ה-`setup.sh`
  עצמו (ולכן בעדכון תוך-סשן החלונית עוד תופיע פעם אחת עד ריבוט). הקיצור גם מסומן `metadata::trusted`.

## מקור הפעלת מצב שבת - auto / schedule / none (עודכן יולי 2026, גרסה 1.1.0)
- **מה זה:** בחירה פר-פרויקט (עם דריסה פר-מעלית) איך `SHABBAT_ACTIVE` נקבע:
  `auto` = הזיהוי ההתנהגותי הקיים (ברירת מחדל, אפס שינוי בפרויקטים קיימים);
  `schedule` = **מנוע לוח-זמנים** ב-`shabbat_detector/schedule_windows.py` שכותב
  `SHABBAT_ACTIVE` לפי חלון hebcal מדויק: `[הדלקת נרות - BEFORE, הבדלה + AFTER]`
  (ברירות מחדל 100/60 דק' - זהות ל-fallback הדפדפני); `none` = לעולם לא במצב שבת.
  רזולוציה: `SHABBAT_SOURCE` של המעלית ← `settings/SHABBAT_SOURCE_DEFAULT` ← `auto`
  (`resolve_source`). **`SHABBAT_OVERRIDE` תמיד גובר, בכל מצב.**
- **מימוש:** טיק כל 30ש' מלולאת ה-watchdog (`_schedule_tick`); כתיבת flip עם retry
  (כמו flip של ה-FSM) + `SHABBAT_DETECTOR{state:'SCHEDULE',ts,סיבה בעברית}`; ה-FSM ממשיך
  לרוץ **מושתק** (אותו מנגנון של force_on/force_off ב-`_apply_result`) כך שחזרה ל-auto
  מיידית; שינויי מקור/אופסטים/GEO_NAME_ID/YOM_TOV_SHENI נקלטים חיים דרך ה-SSE הקיים.
- **עמידות (fail-closed):** רשימות החלונות (רב-חלוניות - תומך יו"ט מרובה-ימים ויו"ט שני
  של גלויות לפי `YOM_TOV_SHENI`) נשמרות בקובץ ה-state בדיסק; כשל fetch לא מוחק אותן;
  בלי דאטה שמיש בכלל - המצב האחרון מוחזק (בלי flapping) עם WARNING מדוכה. **re-assert
  בעליית שירות** מיישר את ה-DB אחרי ריבוט/הפסקת חשמל שחצו גבול חלון (ב-auto אין כתיבה
  בעלייה - כמו קודם). זה נפרד מ-`hebcal_gate.py` (השער הרחב, fail-open) שלא השתנה.
- **תפעולי:** מצב `schedule` עדיין דורש Pi עם שירות `shabbat-detector` (הוא הכותב) -
  אבל חומרת RFID אופציונלית לבניין כזה. כתיבה ידנית של `SHABBAT_ACTIVE` (למשל onoff.html)
  מיושרת חזרה תוך טיק-שניים; שליטה ידנית עמידה = `SHABBAT_OVERRIDE`.
- **בדיקות:** `python3 -m pytest tests/` (במחשב פיתוח; חדש בגרסה זו).

## התראות
- **הוסרו מה-Pi.** ההתראות מנוהלות מרכזית מדשבורד האדמין (סקשן **"🔔 התראות"** לכל
  פרויקט) ונשלחות ע"י Google Apps Script (`admin-dashboard/apps-script`) על בסיס המצב
  שה-Pi כותב ל-Firebase. אין יותר `notifier.py`, watchdog "אין-תנועה", או סקשן
  `notifications` ב-`rfid_config.json`.

## סימולטור (הרצה מקומית לבדיקות)
- `shabbat_elevator_A_simulator.py`, `firebase_elevator_simulator.py`
- ב-Windows הגדר `$env:PYTHONIOENCODING="utf-8"` למניעת שגיאות Unicode.
- קומת BOTTOM/TOP נספרת כ-52 שניות (visit 26s + stopped 26s = שני events).

## ניהול-צי / עדכון מרחוק
- `shabbat_detector/fleet_agent.py` - דיווח גרסה/heartbeat ל-`fleet/{id}` + ביצוע פקודת-עדכון
  מרחוק מהדשבורד (`/fleet/{id}/command`), מאומתת ב-`secret_key` (bearer-token, אימות בצד ה-Pi).
  הפקודה מריצה `sudo ./setup.sh`; דיווח `update_status`; הגנת-replay (dedupe + מחיקת הפקודה).
  שירות `fleet-agent` רץ כ-**root**. תיעוד מלא: `docs/fleet-remote-update.md`.
- **גיבוי-לוגים** (`shabbat_detector/log_backup.py`): פקודת-צי שנייה `backup_logs` (כפתור בדשבורד) +
  גיבוי שבועי אוטומטי. כל Pi דוחף את `logs/` לתת-תיקייה **`{project}/{ELEVATOR_ID}/`** בריפו GitHub
  **נפרד** (`LOG_BACKUP_REPO_URL` עם token כתיבה, נפרד מטוקן הקוד). ה-`project` נגזר מ-`FIREBASE_URL`
  (דריסה ב-`LOG_BACKUP_PREFIX`) כדי שאותה מעלית בכמה פרויקטים לא תתנגש. מנקה `secret_key` מהלוגים לפני push.
- **גיבוי-קונפיג (מיפוי-תגים)** (עודכן יולי 2026, גרסה 1.1.1): יחד עם הלוגים נשמר גם עותק **מסונן**
  של `rfid_config.json` ב-`{project}/{ELEVATOR_ID}/config/rfid_config.sanitized.json`, כדי לשחזר את
  **מיפוי-התגים** (שקשה לשחזר ידנית) אם נמחק ה-SD. הסינון **fail-closed** (`_sanitize_config`+
  `_write_config_snapshot`): ה-`tags` וההגדרות הלא-סודיות נשמרים, אבל ה-`SECRET_KEY` וכל שדה
  token/סיסמה/URL-עם-token מוחלפים ב-`***REDACTED***` - **שום סוד לא מגיע לריפו הגיבוי** (תואם לכלל
  הקשיח: אסור לכתוב `SECRET_KEY` ל-Git). דטרמיניסטי (קונפיג ללא-שינוי ⇒ אין commit מיותר). כיבוי:
  `CONFIG_BACKUP_ENABLED=false`. נכלל אוטומטית בכל `backup_logs` (שבועי + כפתור הדשבורד).
- **לוגים:** רוטציה שבועית **ביום ג'** (`when="W1"`), שמירת **4 שבועות** (tracker+detector).
- `rfid-tracker.service`: ממתין לפורט הסיריאל (לולאה, לא sleep קבוע) + `StartLimitIntervalSec=0`
  כדי שיעלה אמין אחרי ריבוט גם אם ה-USB מאחר. **אסור** להוסיף `After=multi-user.target`
  לאף יחידה עם `WantedBy=multi-user.target` - זה יוצר מעגל-סדר ש-systemd שובר במחיקת job
  (ראה סקשן "אמינות-אתחול", גרסה 1.0.11).
- `version` שמדווח = תוכן קובץ **`VERSION`** בשורש הריפו (semver, למשל `1.0.0`); אם הקובץ חסר -
  fallback לתאריך ה-commit. **שחרור = הקפץ את `VERSION` (`1.0.1`→`1.0.2`) ו-push ל-`main`. זהו.**
  ה-Action `.github/workflows/sync-version.yml` כותב אוטומטית את `VERSION` ל-Firebase
  `fleet_config/latest_version`, והדשבורד מסמן "עדכון זמין" לבד - בלי הקלדה ובלי פריסת דשבורד
  (כפתור ✏️ בדשבורד = דריסה ידנית לחירום). דורש secret `FIREBASE_SERVICE_ACCOUNT` בריפו (אותו SA
  כמו בדשבורד). כך גם תיקונים באותו יום ניתנים להבחנה ולעדכון מרחוק.

## קבצים עיקריים
- `elevator_tracker_rfid.py` - מעקב קומות לפי RFID.
- `shabbat_detector/` - חבילת זיהוי שבת (FSM, auto_learner, cycle_analyzer, firebase_client,
  hebcal_gate, `fleet_agent`, שירותי systemd).
- `deploy_elevator.sh` - סקריפט פריסה ישן מ-ZIP/Drive. **מוחלף ע"י `git pull`.**
