import os
import sys
import time

# Make the repo root importable (shabbat_detector package lives there).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Deterministic local time: naive datetimes (date-only holiday items, the
# fetch anchor) resolve in the Pi's local tz, which is Israel in production.
os.environ["TZ"] = "Asia/Jerusalem"
time.tzset()
