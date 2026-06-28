import serial
import time
import requests
import json
import os
import logging
import logging.handlers
from datetime import datetime

CONFIG_FILE = 'rfid_config.json'

# ── Persistent weekly-rotating file log ────────────────────────────────────
# Plain-text log on disk that rotates every Tuesday at 00:00 and keeps 4 files,
# living alongside the detector log in the shared logs/ directory.
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
try:
    os.makedirs(_LOG_DIR, exist_ok=True)
    _file_logger = logging.getLogger("rfid_tracker_file")
    _file_logger.setLevel(logging.INFO)
    _file_logger.propagate = False
    if not _file_logger.handlers:
        _rot = logging.handlers.TimedRotatingFileHandler(
            os.path.join(_LOG_DIR, "rfid_tracker.log"),
            when="W1",          # weekly, Tuesday at midnight
            interval=1,
            backupCount=4,      # keep 4 rotated files (~4 weeks)
            encoding="utf-8",
        )
        _rot.setFormatter(
            logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
        _file_logger.addHandler(_rot)
except Exception as _exc:
    _file_logger = None
    print(f"File log setup failed: {_exc}", flush=True)

# משתנים גלובליים לכתובת הלוגים ומפתח אבטחה - יחושבו/ייטענו בזמן ריצה
CLOUD_LOGS_URL = None
SECRET_KEY = None

def log_message(message, send_to_cloud=True):
    """
    מנגנון לוגים משודרג ומוקשח:
    1. הדפסה למסך (תמיד)
    2. שמירה לקובץ מקומי (תמיד)
    3. שליחה לענן (אופציונלי) - כולל מפתח אבטחה
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{now_str}] {message}"
    
    # 1. הדפסה למסך
    print(log_line, flush=True)
    
    # 2. שמירה לקובץ מקומי (רוטציה שבועית, שמירת 7 גיבויים)
    try:
        if _file_logger:
            _file_logger.info(message)
    except Exception as e:
        print(f"Local Log Error: {e}", flush=True)

    # 3. שליחה לענן
    if CLOUD_LOGS_URL and send_to_cloud and SECRET_KEY:
        try:
            payload = {
                'timestamp': int(time.time()),
                'time_str': now_str,
                'message': message,
                'secret_key': SECRET_KEY  # מפתח האבטחה
            }
            
            # שליחה ב-POST (יוצר רשומה חדשה בהיסטוריה)
            response = requests.post(CLOUD_LOGS_URL, data=json.dumps(payload), timeout=2)
            
            # בדיקה אם השרת דחה את הבקשה (למשל בעיות הרשאה)
            if response.status_code != 200:
                print(f"Warning: Cloud log failed. Server returned {response.status_code}", flush=True)
                
        except Exception as e:
            # מתעלמים משגיאות תקשורת רגעיות כדי לא להאט את המעלית
            pass

def load_config():
    """טוען את קובץ ההגדרות"""
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        log_message(f"CRITICAL ERROR: Could not load '{CONFIG_FILE}': {e}", send_to_cloud=False)
        return None

def send_status_to_server(floor, full_status_url):
    """
    שולח את הסטטוס (קומה נוכחית) לענן.
    משתמש ב-PATCH כדי לעדכן רק את הקומה.
    כולל את מפתח האבטחה.
    """
    try:
        # שליחת קומה, זמן ומפתח אבטחה
        data_to_send = {
            'floor': floor, 
            'timestamp': int(time.time()),
            'secret_key': SECRET_KEY
        }
        
        response = requests.patch(full_status_url, data=json.dumps(data_to_send), timeout=10)
        
        if response.status_code == 200:
            # לוג מקומי בלבד — אין צורך לכתוב /logs בענן בכל שינוי קומה (חיסכון רוחב פס)
            log_message(f"Status Updated: Floor '{floor}'", send_to_cloud=False)
            return True
        else:
            log_message(f"Error sending status: HTTP {response.status_code}", send_to_cloud=True)
            
    except Exception as e:
        log_message(f"Error sending status: {e}", send_to_cloud=True)
    return False

def main():
    global CLOUD_LOGS_URL, SECRET_KEY

    config = load_config()
    if not config: return

    TAG_MAP = config.get('tags', {})
    SETTINGS = config.get('settings', {})
    SERIAL_PORT = SETTINGS.get('SERIAL_PORT', '/dev/ttyUSB0')
    BAUDRATE = SETTINGS.get('BAUDRATE', 115200)
    
    # טעינת הגדרות ענן
    BASE_FIREBASE_URL = SETTINGS.get('FIREBASE_URL') 
    ELEVATOR_ID = SETTINGS.get('ELEVATOR_ID') 
    SECRET_KEY = SETTINGS.get('SECRET_KEY') 
    
    INVENTORY_CMD = b'\xBB\x00\x22\x00\x00\x22\x7E'

    if not BASE_FIREBASE_URL or not ELEVATOR_ID or not SECRET_KEY:
        log_message("CRITICAL ERROR: FIREBASE_URL, ELEVATOR_ID or SECRET_KEY missing in config.", send_to_cloud=False)
        return

    # --- בניית הכתובות החכמה ---
    
    # 1. ניקוי הכתובת הבסיסית (הסרת .json אם קיים)
    if BASE_FIREBASE_URL.endswith('.json'):
        clean_base_url = BASE_FIREBASE_URL[:-5]
    else:
        clean_base_url = BASE_FIREBASE_URL

    # 2. כתובת לסטטוס: .../elevators/B.json
    STATUS_URL = f"{clean_base_url}/{ELEVATOR_ID}.json"

    # 3. כתובת ללוגים: .../elevators -> .../logs/B.json
    if 'elevators' in clean_base_url:
        logs_base = clean_base_url.replace('elevators', 'logs')
        CLOUD_LOGS_URL = f"{logs_base}/{ELEVATOR_ID}.json"
    else:
        # Fallback
        CLOUD_LOGS_URL = f"{clean_base_url}_logs/{ELEVATOR_ID}.json"

    # לוג startup מקומי בלבד — tracker שקורס בלולאה לא יציף את /logs בענן בכל restart
    log_message(f"--- Smart Elevator Tracker C (Secured) --- ID: {ELEVATOR_ID}", send_to_cloud=False)
    log_message(f"Status URL: {STATUS_URL}", send_to_cloud=False)
    log_message(f"Logs URL: {CLOUD_LOGS_URL}", send_to_cloud=False)
    
    try:
        ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=0.05)
    except Exception as e:
        # מקומי בלבד — שגיאת serial חוזרת ב-crash-loop הציפה את /logs (נצפו 4202 קריסות). מנוטר בלוג המקומי.
        log_message(f"CRITICAL ERROR on serial port init: {e}", send_to_cloud=False)
        return

    last_sent_floor = None
    last_logged_tag = None

    # ── אישור קומה לפי ספירת דגימות (anti-storm) ───────────────────────────────
    # קומה מתקבלת כ"אמיתית" רק אחרי שנקראה ב-N דגימות (ברירת מחדל 2). הלולאה רצה
    # כל ~0.05ש', וקומה אמיתית נמצאת בטווח האנטנה לכמה דגימות גם במעבר מהיר — אז
    # מעברים אמיתיים לא אובדים. קפיצת רעש של דגימה בודדת / ניתור גבול בין שני תגים
    # (A,B,A,B) לעולם לא צובר N רצופים → מסונן. זה מחליף throttle לפי-זמן שהיה
    # מחמיץ מעברים מהירים (<1ש').
    CONFIRMATION_SAMPLES = int(SETTINGS.get('CONFIRMATION_SAMPLES', 2))
    candidate_floor = None
    candidate_count = 0

    try:
        while True:
            ser.write(INVENTORY_CMD)
            resp = ser.read(64)
            current_tag = None

            if resp and resp.startswith(b'\xBB\x02\x22'):
                tag_bytes = resp[7:19]
                current_tag = ''.join(f"{b:02X}" for b in tag_bytes)

            # --- לוגיקת הלוגים ---
            if current_tag:
                if current_tag != last_logged_tag:
                    mapped_floor = TAG_MAP.get(current_tag, "Unknown")
                    # כותב לוג מקומי על כל שינוי תג (לא שולח לענן כדי לחסוך תעבורה, אלא אם תרצה לשנות)
                    log_message(f"Tag Change: ID '{current_tag}' -> Floor '{mapped_floor}'", send_to_cloud=False)
                    last_logged_tag = current_tag

            current_floor = TAG_MAP.get(current_tag)

            # ── אישור לפי ספירת דגימות ──
            # קריאה ריקה (ללא תג) לא מאפסת את הספירה — כדי לא לפספס מעבר מהיר עם
            # קריאות לסירוגין (A, ריק, A). קריאת קומה *שונה* מאפסת — כך ניתור בין
            # שני תגים אף פעם לא מגיע ל-N רצופים ולא נשלח.
            if current_floor is not None:
                if current_floor == candidate_floor:
                    candidate_count += 1
                else:
                    candidate_floor = current_floor
                    candidate_count = 1

                # שליחה לענן רק כשקומה חדשה אושרה ע"י מספיק דגימות רצופות
                if candidate_count >= CONFIRMATION_SAMPLES and current_floor != last_sent_floor:
                    success = send_status_to_server(current_floor, STATUS_URL)
                    if success:
                        last_sent_floor = current_floor

            time.sleep(0.02) 

    except KeyboardInterrupt:
        log_message("\nStopped by user.", send_to_cloud=True)
    except Exception as e:
        log_message(f"Error in main loop: {e}", send_to_cloud=True)
    finally:
        if 'ser' in locals() and ser.is_open:
            ser.close()
        log_message("Serial port closed.", send_to_cloud=True)

if __name__ == '__main__':
    main()
