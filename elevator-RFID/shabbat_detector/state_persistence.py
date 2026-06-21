"""
Saves and loads the detector's runtime state to a local JSON file so that
a systemd restart can resume without re-observing a full cycle.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

log = logging.getLogger(__name__)

_DEFAULT_DIR = "/var/lib/shabbat_detector"
_WRITE_INTERVAL_S = 30   # write at most every 30 seconds


class StatePersistence:
    def __init__(self, elevator_id: str, state_dir: Optional[str] = None):
        self._dir = state_dir or _DEFAULT_DIR
        self._path = os.path.join(self._dir, f"state_{elevator_id}.json")
        self._last_write: float = 0.0

    def load(self) -> Optional[dict]:
        if not os.path.exists(self._path):
            return None
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            log.info("Restored state from %s", self._path)
            return data
        except Exception as e:
            log.warning("Could not load state from %s: %s", self._path, e)
            return None

    def save(self, state: dict, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < _WRITE_INTERVAL_S:
            return
        try:
            os.makedirs(self._dir, exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
            self._last_write = now
        except Exception as e:
            log.warning("Could not save state to %s: %s", self._path, e)
