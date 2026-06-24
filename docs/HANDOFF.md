# 🔁 HANDOFF — מערכת מעלית שבת (ECONTROL)

מסמך העברה להמשך עבודה בסשן חדש. **משימה (1) התראות — ✅ בוצעה** (הוסרו מה-Pi, מנוהלות
מרכזית מהדשבורד + Apps Script). **משימה (2): עדכון ה-RPI הקיימים בלי לשבור את הקונפיג.**

## רפוזיטוריז וזרימת עבודה
- **`elibic/elevator-rpi`** — קוד ה-Raspberry Pi (tracker + detector + `installer/` web).
- **`elibic/admin-dashboard`** (שמו הקודם `ramada-admin`) — הדשבורד הראשי הרב-פרויקטי.
- **`elibic/ramada-web`** — אפליקציית הווב של המעלית. **`public/setup.html` = מקור-האמת
  למודל הנתונים** (קריאה בלבד).
- **ענף פיתוח:** `claude/focused-turing-buyt3w` בכל הריפו. אחרי push לענף →
  fast-forward ל-`main`: `git push origin claude/focused-turing-buyt3w:main`.
  **ה-Pi מושך מ-`main`.** (לריפו admin-dashboard ששונה-שם: יצירת ref חדש דרך git
  עלולה לתת 503 redirect — להשתמש ב-GitHub API `create_branch`.)

## ⚠️ בטיחות קובץ הקונפיג (משימה 2)
- `rfid_config.json` (מכיל `SECRET_KEY` ומיפוי תגים) **מוחרג ב-`.gitignore`**
  → `git pull` **פיזית לא נוגע בו**.
- **נוהל עדכון בטוח ל-Pi קיים** (על כל Pi בנפרד):
  ```bash
  cd ~/elevator-RFID            # או ~/elevator-rpi — לאמת איפה הריפו
  cp rfid_config.json ~/rfid_config.json.bak     # גיבוי ליתר ביטחון
  sudo chown -R $USER:$USER .   # מתקן .git שאולי root-owned מ-sudo setup ישן
  git pull                      # קוד בלבד; rfid_config.json מוחרג → לא נגעים
  sudo systemctl restart rfid-tracker shabbat-detector
  ```
- **גוצ'ה:** `sudo ./setup.sh` ישן הריץ git כ-root → `.git` בבעלות root → שובר `git pull`
  של המשתמש (Permission denied). תוקן ב-`setup.sh` החדש, אך Pi על קוד ישן צריך את ה-`chown` קודם.
- דשבורד מקומי על Pi קיים (לא נוגע בקונפיג): אחרי ה-pull → `sudo ./venv/bin/python -m installer --install-shortcut`.

## 🔔 התראות (משימה 1) — ✅ בוצעה
- **ההתראות הוסרו מה-Pi** (נמחקו `shabbat_detector/notifier.py` + `MovementWatchdog` + סקשן
  `notifications` ב-`rfid_config.json`/בטפסים). ה-detector רק כותב מצב ל-Firebase.
- **מנוהלות מרכזית** מדשבורד האדמין (סקשן **"🔔 התראות"** פר-פרויקט; נשמר ב-Firebase של האדמין).
- **נשלחות** ע"י **Google Apps Script** אחד לכל הצי (`ramada-web/apps-script/elevator-monitor.gs`),
  Trigger מתוזמן (~5 דק'), Email דרך `MailApp` ו-Telegram דרך REST — בלי סודות על אף Pi.

## מודל נתונים ב-Firebase (מ-setup.html — מחייב)
- **`elevator_configs/{id}`**: כל הקונפיג + **`SHABBAT_OVERRIDE`** (`auto`/`force_on`/`force_off`
  — מחרוזות, **לא** true/false) + `SHABBAT_DETECTOR{state,last_transition_reason}` +
  `SHABBAT_ACTIVE` (פלט ה-Pi).
- **`settings`** (גלובלי): `HEBCAL_GATE_ENABLED` (+windows), `SHABBAT_DETECTION{ספי FSM}`,
  `YOM_TOV_SHENI`, `FLOOR_ALIASES`.
- **`elevators/{id}`**: קומה חיה (tracker). **`fleet/{id}`**: version/last_seen/command.
- `FIREBASE_URL`: detector+monitor מנרמלים ל-**root** של ה-DB (urlsplit), עם/בלי `.json`.
- FSM: `NORMAL → CANDIDATE_SHABBAT → SHABBAT (→ CANDIDATE_EXIT)`. `SHABBAT_ACTIVE` נדלק רק עם
  מחזורים תואמים רצופים **ובחלון hebcal** — או `SHABBAT_OVERRIDE=force_on`.

## הכלי הגרפי המקומי (רקע)
- שירות `elevator-config-web` (systemd, root, `127.0.0.1:8080`, תמיד פעיל, no-cache).
- אייקון "דשבורד מעלית" → `installer/open-dashboard.sh` (Chromium `--app` מסך מלא +
  כפתור "✕ סגור"). דף הבית = הדשבורד. דגל `--install-shortcut` מתקין רק שירות+אייקון.

## מצב נוכחי
- elevator-rpi `main` מעודכן עם כל עבודת הסשן (setup.sh fix, נרמול URL, דשבורד מקומי,
  override display, no-cache, אייקון). admin-dashboard `main` = הדשבורד הראשי.
