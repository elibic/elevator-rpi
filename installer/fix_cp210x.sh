#!/bin/bash
# תיקון זיהוי קורא ה-RFID (צ'יפ CP210x עם USB ID 1560:0003 שאינו מזוהה אוטומטית).
# נטען בכל אתחול דרך fix_cp210x.service. מבוסס על המדריך המקורי.
modprobe -r cp210x 2>/dev/null
modprobe cp210x
echo 1560 0003 > /sys/bus/usb-serial/drivers/cp210x/new_id 2>/dev/null || true
