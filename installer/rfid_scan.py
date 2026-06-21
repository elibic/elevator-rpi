"""
installer/rfid_scan.py — קריאת תג RFID בודד מהקורא (מקור אמת אחד לסריקה).

משותף לאשף ההגדרה (CLI + Web) ולכלי מיפוי התגים. מבוסס על אותה לוגיקת serial
המוכחת מ-elevator_tracker_rfid.py ו-tag_mapper.py — אל תשנה את הקבועים ללא בדיקה
מול החומרה.
"""
from __future__ import annotations

import time
from typing import Optional

# פקודת inventory ופרוטוקול התשובה — זהים לקוד הפרודקשן (קורא CP210x).
INVENTORY_CMD = b"\xBB\x00\x22\x00\x00\x22\x7E"
_RESP_HEADER = b"\xBB\x02\x22"


def open_reader(serial_port: str = "/dev/ttyUSB0", baudrate: int = 115200,
                timeout: float = 0.1):
    """פותח את הפורט הטורי לקורא ה-RFID. דורש pyserial (ייבוא עצל)."""
    import serial  # ייבוא עצל כדי שהמודול ייטען גם בלי pyserial (dry-run / web)
    return serial.Serial(serial_port, baudrate, timeout=timeout)


def parse_tag(resp: bytes) -> Optional[str]:
    """מפענח מזהה EPC (hex רישיות) מתשובת הקורא, או None אם לא נקרא תג."""
    if resp and resp.startswith(_RESP_HEADER):
        tag_bytes = resp[7:19]
        return "".join(f"{b:02X}" for b in tag_bytes)
    return None


def read_one_tag(ser) -> Optional[str]:
    """שולח פקודת inventory וקורא ניסיון בודד; מחזיר EPC או None."""
    ser.write(INVENTORY_CMD)
    resp = ser.read(64)
    return parse_tag(resp)


def scan_for_tag(serial_port: str = "/dev/ttyUSB0", baudrate: int = 115200,
                 timeout_s: float = 15.0) -> Optional[str]:
    """
    פותח את הפורט, סורק עד timeout_s שניות, ומחזיר את ה-EPC הראשון שנקרא (או None).
    משמש את אשף ההגדרה ("סרוק תג עכשיו"). שים לב: רק תהליך אחד יכול לפתוח את
    הפורט — יש לעצור את שירות ה-tracker לפני סריקה (מטופל ב-core.scan_tag).
    """
    ser = open_reader(serial_port, baudrate)
    try:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            tag = read_one_tag(ser)
            if tag:
                return tag
            time.sleep(0.05)
        return None
    finally:
        try:
            ser.close()
        except Exception:
            pass
