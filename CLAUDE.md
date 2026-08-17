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
  **`services`** (מצב 4 שירותי systemd, מוצג בדשבורד) + **`backup_status`** (גיבוי-לוגים) +
  **`temp_c`** (טמפ' המעבד, מוצגת בדשבורד).
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
- **`FLOOR_WAITS` כ-list מ-Firebase (תיקון 1.1.5):** RTDB ממיר map עם מפתחות שלמים רצופים
  שמתחילים מ-0 (`{0:..,1:..}`) ל-**list**; קוד ה-detector קרא `.items()` וקרס בלולאה
  (`AttributeError: 'list' object has no attribute 'items'`) בכל מעלית עם קומות 0..N (למשל
  ניצה A: `BOTTOM_FLOOR=0`). `normalize_floor_waits` ב-`cycle_analyzer.py` מנרמל list/dict/null→
  dict (`{str(קומה): ערך}`, אינדקס=קומה, מדלג null), ומשמש **בכל 4 נתיבי-הקריאה**: `detector.
  _make_cycle_analyzer`, `cycle_analyzer.update_config`, `fsm.expected_cycle_period_from_config`,
  `fsm._evaluate_cycle`. אבחון-שדה: `AttributeError` ב-journal + `restart counter` עולה. **תופעת-
  לוואי:** כשה-detector קורס הוא לא מנהל `SHABBAT_ACTIVE` ⇒ המעלית **נתקעת** במצבה האחרון (שבת).
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

## הקטנת שחיקת SD והגנה מנתקי-חשמל - opt-in (עודכן יולי 2026, גרסה 1.1.2)
- **מה:** `settings.REDUCE_SD_WEAR=true` ב-rfid_config.json (ברירת-מחדל **false** - אפס שינוי
  בפריסות קיימות) ⇒ `configure_sd_wear` ב-`installer/core.py` (רץ בכל `setup.sh`, כולל עדכון-צי
  unattended) מפעיל: (1) **journald ל-RAM** (`Storage=volatile` ב-drop-in
  `/etc/systemd/journald.conf.d/60-elevator-volatile.conf`; `journalctl -u ... -f` ממשיך לעבוד,
  אבל היומן לא שורד ריבוט); (2) **log2ram** מריפו azlux - tmpfs על `/var/log` עם סנכרון תקופתי
  לדיסק (SIZE=64M; נכשל-רך בלי רשת - שאר הצעדים חלים); (3) **noatime** על שורת ה-root ב-fstab
  (ממילא ברירת המחדל של RPi OS - מוודאים); (4) **כיבוי swap על הכרטיס** (dphys-swapfile swapoff +
  disable + מחיקת /var/swap). zram-swap (RAM דחוס, קיים ב-OS חדשים) לא נוגעים בו - הוא לא שוחק
  כרטיס. נכנס לתוקף מלא בריבוט.
- **בכוונה אין overlayfs מלא כאן** (בניגוד לקיוסק): ה-Pi חייב state שכותב-ושורד -
  `/var/lib/shabbat_detector/state_{id}.json` (FSM + חלונות `schedule_windows` שנשמרים fail-closed
  ל-re-assert אחרי הפסקת-חשמל) ו-`state_fleet_{id}.json` (הגנת-replay של סוכן-הצי). **אלה נשארים
  על הדיסק** - הם תחת `/var/lib`, לא תחת `/var/log`, ולכן לא מושפעים מ-log2ram/volatile;
  `configure_sd_wear` גם מזהיר אם `PATH_DISK` של log2ram יכסה אי-פעם את `/var/lib`. גם `logs/`
  שבתיקיית הפרויקט (לוגי tracker/detector, רוטציה שבועית + גיבוי GitHub) נשארים על הדיסק - הם
  רשומת-הדיבוג היחידה כש-journald הפך volatile.
- **החזרה:** `REDUCE_SD_WEAR=false` (או הסרת המפתח) אחרי שהופעל - מחזיר **רק** את מה שאנחנו
  שינינו, לפי marker `/etc/elevator-reduce-sd-wear.json` (drop-in נמחק, יומן פרסיסטנטי חוזר,
  log2ram נוטרל, dphys-swapfile מופעל חזרה). noatime נשאר (ברירת המחדל של ה-OS).
- **שיקול תפעולי:** בלי swap על-הכרטיס, עומס-זיכרון חריג (דפדפן הדשבורד על Pi עם 1GB) עלול
  להיסגר ע"י ה-OOM killer - במסופון תצוגה-בלבד זה לא רלוונטי; ב-Pi עם דשבורד פתוח קבוע שקלו
  להשאיר כבוי או לוודא zram. בדיקות: `tests/test_sd_wear.py`.

## 🌡️ טמפרטורת המעבד בדשבורד הצי (גרסה 1.1.6)
- **מה:** ה-heartbeat של `fleet_agent` נושא עכשיו `temp_c` - טמפ' ה-CPU של ה-Pi (°C, דיוק 0.1),
  ובדשבורד האדמין מופיע צ'יפ צבעוני בשורת כל מעלית ליד הקומה.
- **קריאה:** `read_cpu_temp()` קורא את `/sys/class/thermal/thermal_zone0/temp` (מילי-מעלות -
  קריאת-קובץ בלבד, בלי תהליך חדש בכל heartbeat), עם fallback ל-`vcgencmd measure_temp`.
  ערך מחוץ לטווח 0-150°C נדחה. הרזולוציה בפועל = `FLEET_REPORT_INTERVAL` (ברירת-מחדל 5 דק').
- **`null` במכוון כשאין קריאה:** ה-PATCH שולח `temp_c=null` ⇒ המפתח **נמחק** ב-RTDB, כדי שערך
  ישן לא ייראה חי בדשבורד (ה-`last_seen` ממשיך להתעדכן). Pi ישן מ-1.1.6 לא מדווח ⇒ הצ'יפ נעלם לבד.
- **ספי-הצבע (בדשבורד):** &lt;65°C ירוק · 65-79°C צהוב · 80°C+ אדום - לפי ה-throttling של רספברי-פיי
  (האטה רכה ב-80°C, קשה ב-85°C). ראה `docs/fleet-remote-update.md`.

## 🚪 יציאה ממצב שבת - היפוך נטל ההוכחה בגבול ההלכתי (גרסה 1.1.7)
- **התקלה:** מעלית B ב-RAMADA נכנסה למצב שבת אוטומטית אבל **לא יצאה** (15/8/2026: הבדלה 20:15,
  A יצאה 20:42, D ב-20:37, **B רק ב-23:25** - ורק כי ה-tracker נדם ונורתה הפרת `no-report`).
- **סיבת שורש 1 - כל מסלולי היציאה מתים מבנית ב-B:** ההפרות נוצרות מ"עצירה בקומה **לא** מוגדרת"
  (`_all_valid_stops` = `STOPPING_FLOORS_UP ∪ DOWN ∪ קצוות`). ל-B (וגם ל-**C**!) יש
  `STOPPING_FLOORS_DOWN` עם **כל** הקומות ⇒ הקבוצה מכסה את כל הבניין ⇒ **אף עצירה לא יכולה
  להיות "לא חוקית"**. הוכחה מהלוגים: פסילות "עצירות לא חוקיות בירידה" - A: 779, D: 965, C: 95,
  **B: 0** (אפס מוחלט, אי-פעם). נשאר רק מסלול "מחזור שלם לא-תואם", שדורש נסיעת-נוסעים אקראית
  קצה-לקצה - לוטריה.
- **סיבת שורש 2 - החלון ההלכתי שימש רק לכניסה:** `hebcal_in_window` הועבר ל-`_maybe_exit`
  ומעולם **לא נקרא בגוף הפונקציה** (פרמטר מת). האות האמין ביותר במערכת לא השפיע על יציאה.
- **התיקון (לא פלסטר - היפוך העיצוב):** **בתוך** החלון ההלכתי החזקה היא ששבת נמשכת, ויציאה
  דורשת ראיה חיובית להתנהגות-חול (בדיוק כמו היום ⇒ אפס יציאות שווא). **מחוץ** לחלון החזקה
  מתהפכת: ה**הישארות** בשבת היא שדורשת הוכחה, וההוכחה היא **מחזור-שבת תקין שממשיך להגיע**.
  מלון שממשיך להריץ את התוכנית אחרי ההבדלה מייצר מחזורים ונשאר בשבת; אחד שהפסיק - יוצא.
  זה עובד ל-B/C **בלי** שום קומה אסורה.
- **מימוש:** `fsm.check_post_window_exit()` (מנגנון 4 ב-watchdog). אחרי `POST_WINDOW_GRACE_MIN`
  (5 דק') מסגירת החלון, **שני מסלולים ב-"או"** (גרסה 1.1.8):
  - **מהיר - `POST_WINDOW_REVERSALS_FOR_EXIT` (6) שינויי-כיוון ב-10 דק'.** סבב שבת מונוטוני בין
    הקצוות, לכן "עלתה ל-10, ירדה ל-7, שוב ל-9" הוא שליטת-נוסעים ולא סבב. `record_reversal` נקרא
    מלולאת האירועים (על הזרם שכבר עבר dedup ודיכוי-flap), קצוות מוחרגים (ההיפוך שם לגיטימי).
    **כיול מהשטח:** בשבת אמיתית, 3 שבתות × 3 מעליות - **מקסימום 4** שינויי-כיוון ב-10 דק'
    (מיסריד של קריאת-אפקס); אחרי שבת - 6 מצטברים תוך 3-8 דק'. הסף 6 = מרווח 1.5x.
    **הקריטריון "עצירה קצרה" נבדק ונפסל:** הוא ירה ~25 פעם בשעה כל השבת (תופס את קטע העלייה
    האקספרס) - אין לו הפרדה. **שינוי-כיוון בלבד הוא הסיגנל הנקי.**
  - **גיבוי - `POST_WINDOW_CYCLE_FACTOR` (2.5) × זמן-המחזור מאז המחזור התקין האחרון.** זה המסלול
    שעובד כשהמעלית **שקטה** (חנתה ⇒ אין תנועה לשפוט, רק היעדר סבבים). המהיר עובד כשהיא עמוסה.
  - השניים משלימים: B ב-15/8 - המהיר +5 דק' מסגירת החלון, הגיבוי +15; B ב-1/8 - הפוך (המהיר
    היה לוקח 43 דק'). ב-"או" התוצאה תמיד הטובה מבין השניים. כיבוי: `POST_WINDOW_EXIT_ENABLED=false`
    (או `POST_WINDOW_REVERSALS_FOR_EXIT=0` למסלול המהיר בלבד).
- **הדבקה מעוגנת לחלון:** `_stickiness_expired(now, hebcal_in_window)` - `STICKINESS_MINUTES`
  (1500 = **25 שעות** בפרודקשן!) פוקעת אוטומטית מחוץ לחלון. ההדבקה הייתה מעוגנת ל**זמן הכניסה**,
  כך שחלון-היציאה תלוי במתי הבניין הפעיל את התוכנית: B נכנסת ~14 דק' אחרי A ו-D, ולכן נחסמה
  אחרי שהראיות שלה כבר באו והלכו. עכשיו 1500 דק' = "לא יוצאים באמצע שבת", וזה תפקידה הלגיטימי.
- **`check_candidate_exit_timeout()` (מנגנון 5):** ה-timeout ב-`_maybe_exit` נבדק **רק** כשמגיעה
  ראיה חדשה, אז מכונית שנדמה ב-CANDIDATE_EXIT נתקעה שם (B: פג ב-22:09, יצאה 23:25). עכשיו
  נבדק בשעון - **אבל רק מחוץ לחלון**: timeout הוא *היעדר* ראיה, ובתוך שבת היעדר = המעלית נחה.
  שחזור של C ב-1/8 עם timeout ללא-תנאי יצא **13 דק' לפני ההבדלה** - זו הסיבה לתנאי.
- **`MISSED_CYCLE_FACTOR` נשאר 0 בכוונה.** הוא רץ גם *בתוך* החלון, שם מחזור שנמדד רע לא ניתן
  להבחנה משבירת-קצב אמיתית - הוא גרם ליציאות שווא באמצע שבת (A ב-24/7 21:21, D ב-24/7 19:54
  ו-25/7 00:14) ולכן נוטרל בסוף יולי, מה שהשאיר את B בלי אף מגן. מנגנון 4 הוא התחליף התחום-לחלון.
- **`HEBCAL_GATE_ENABLED=false` ⇒ מנגנונים 4-5 מושבתים** (אין חלון שאפשר להיות מחוצה לו).
  ה-gate הוא fail-open, לכן נפילת רשת מול hebcal יכולה רק **למנוע** יציאה, לעולם לא לגרום לה.
- **`tools/replay_detection.py` - שחזור מלוגים אמיתיים:** מריץ `rfid_tracker.log` מגיבוי-הלוגים
  דרך ה-`CycleAnalyzer` וה-`ElevatorFSM` **האמיתיים** עם הקונפיג/settings מייצוא Firebase, ומדפיס
  את ציר-המצבים. משחזר את השטח לשנייה (B: `SHABBAT -> CANDIDATE_EXIT` ב-21:59:11 בדיוק כמו בלוג).
  `--legacy` = התנהגות לפני התיקון, להשוואה על אותה הקלטה. **כך בודקים כיוונון בלי לחכות שבוע.**
- **תוצאות מדודות (4 שבתות × 4 מעליות):** B ב-15/8: **23:25 → 20:40** (הבדלה+25 דק', דרך המסלול
  המהיר: "9 שינויי-כיוון באמצע הבניין"); B ב-1/8: +45→+25 דק'; C ב-1/8: +28→+20; A: +27→+25;
  D: ללא שינוי מהותי. **כל הצי יוצא עכשיו תוך 8-26 דק' מההבדלה, ואפס יציאות לפני ההבדלה בכל
  16 השחזורים - גם לפני התיקון וגם אחריו.** בדיקות: `tests/test_post_window_exit.py` (79 עוברות).

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
- **`ExecStartPre` מצומצם לקובץ-הקונפיג בלבד (גרסה 1.1.3):** שתי היחידות (`rfid-tracker` +
  `shabbat-detector`) עושות `chown {{USER}} rfid_config.json` בלבד בעלייה - **לא** `chown -R`
  על כל הריפו. ה-`chown -R` הישן נגע ב-venv (אלפי קבצים) ולקח כמה שניות על Pi Zero, ובהן
  systemd מדווח `activating` והדשבורד צובע אדום בכל בוט/restart (נראה כמו "השירות נופל" - היה
  false-alarm). הריפוי היחיד שהשירות באמת צריך הוא בעלות הקונפיג (root-owned ⇒ `PermissionError`);
  בעלות `.git` ל-git pull מטופלת ב-`setup.sh` (chown אחרי pull, כולל בעדכון-צי - `fleet_agent`
  מגדיר `SUDO_USER`). **אל תחזיר את ה-`-R`.**
- **השירותים לעולם לא רצים כ-root (גרסה 1.1.4 - באג קריטי מ-1.1.3):** עדכון-צי רץ כ-root, ו-
  `fleet_agent._run_update` מעביר `SUDO_USER=_repo_owner()`. אם הריפו כבר root-owned (למשל אחרי
  עדכון שנעל אותו), זה החזיר `root` → `detect_environment` קבע `user=root` → היחידות נכתבו
  `User=root` + `chown root:root` על הקונפיג, השירות נשבר (`activating`), ו-`chown -R root` של
  setup.sh נעל את הריפו על root - **לולאה** (כל עדכון-צי הבא שוב root). התיקון: (1)
  `detect_environment` נופל ל-`_real_user()` (UID 1000, אחרת בעלים לא-root תחת `/home`) כשמתקבל
  root, כך שהיחידות תמיד `User=<המשתמש>`; (2) `fleet_agent` מעביר `SUDO_USER`=המשתמש-האמיתי
  כשהבעלים root/ריק (**דריסה** `env["SUDO_USER"]=`, לא `setdefault`), כדי ש-`chown` של setup.sh
  יחזיר את הריפו למשתמש. לשבירת נעילה קיימת בשטח: `sudo ./setup.sh` מטרמינל (SUDO_USER=eco)
  מרנדר נכון ומחזיר הכל ל-eco בריצה אחת.
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
