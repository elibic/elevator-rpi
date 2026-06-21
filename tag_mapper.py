import serial
import time
import json

# שימוש חוזר בלוגיקת הסריקה המשותפת (מקור אמת אחד לפרוטוקול הקורא).
from installer.rfid_scan import read_one_tag

CONFIG_FILE = 'rfid_config.json'

def load_or_create_config():
    """טוען את קובץ ההגדרות או יוצר אותו אם הוא לא קיים"""
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"'{CONFIG_FILE}' not found or invalid. Creating a new one.")
        return {
            "tags": {},
            "settings": {
                "SERIAL_PORT": "/dev/ttyUSB0",
                "BAUDRATE": 115200,
                "CONFIRMATION_TIME": 0.5,
                "STOPPED_TIMEOUT": 30,
                "FIREBASE_URL": "" 
            }
        }

def save_config(config_data):
    """שומר את ההגדרות לקובץ ה-JSON"""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config_data, f, indent=2)

def main():
    """הפונקציה הראשית להרצת תהליך המיפוי"""
    config = load_or_create_config()
    mapped_tags = set(config['tags'].keys())

    settings = config.get('settings', {})
    serial_port = settings.get('SERIAL_PORT', '/dev/ttyUSB0')
    baudrate = settings.get('BAUDRATE', 115200)

    print("--- RFID Tag Mapper ---")
    print("Scan a tag to begin. Press Ctrl+C to exit.")

    try:
        ser = serial.Serial(serial_port, baudrate, timeout=0.1)
    except serial.SerialException as e:
        print(f"Error opening serial port {serial_port}: {e}")
        return

    try:
        while True:
            epc_hex = read_one_tag(ser)

            if epc_hex:
                if epc_hex not in mapped_tags:
                    print(f"\n--- New Tag Detected ---")
                    print(f"  ID: {epc_hex}")

                    try:
                        floor_name = input(f"Enter floor name for this tag (e.g., '0', '1', 'L') and press Enter: ")
                        if floor_name.strip():
                            config['tags'][epc_hex] = floor_name.strip()
                            save_config(config)
                            mapped_tags.add(epc_hex)
                            print(f" Success! Mapped {epc_hex} to floor '{floor_name}'.")
                            print("Scan the next tag...")
                        else:
                            print(" Skipped. No name entered. Rescanning...")
                    except (KeyboardInterrupt, EOFError):
                        print("\nOperation cancelled by user.")
                        break

            time.sleep(0.2)

    except KeyboardInterrupt:
        print("\nExiting mapper.")
    finally:
        ser.close()
        print("Serial port closed.")

if __name__ == '__main__':
    main()
