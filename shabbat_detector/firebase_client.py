"""
Thin wrapper around Firebase Realtime Database REST API.

Streams floor events via Server-Sent Events (SSE) and writes
config updates back via PATCH.

Requires:  pip install requests sseclient-py
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from datetime import datetime
from typing import Iterator, Optional

import requests

log = logging.getLogger(__name__)

# Reconnect backoff on stream error (seconds): exponential with jitter, capped,
# so a fleet-wide Firebase blip does not have every Pi reconnect in lockstep (#24).
_RECONNECT_BASE = 2
_RECONNECT_CAP = 60


class FirebaseClient:
    def __init__(self, base_url: str, secret_key: str, elevator_id: str):
        # Normalise URL (strip trailing slash)
        self._base = base_url.rstrip("/")
        self._key = secret_key
        self._elevator_id = elevator_id

        # Cached snapshots updated by background listeners
        self._config: dict = {}
        self._settings: dict = {}
        self._config_lock = threading.Lock()
        self._settings_lock = threading.Lock()

        self._config_cb = None
        self._settings_cb = None

    # ── Getters ──────────────────────────────────────────────────────────────

    def get_elevator_config(self) -> dict:
        """One-shot GET of /elevator_configs/{id}."""
        url = f"{self._base}/elevator_configs/{self._elevator_id}.json"
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            data = r.json() or {}
            with self._config_lock:
                self._config = data
            return data
        except Exception as e:
            log.error("Failed to fetch elevator config: %s", e)
            return {}

    def get_settings(self) -> dict:
        """One-shot GET of /settings."""
        url = f"{self._base}/settings.json"
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            data = r.json() or {}
            with self._settings_lock:
                self._settings = data
            return data
        except Exception as e:
            log.error("Failed to fetch settings: %s", e)
            return {}

    # ── SSE Subscribers (run in background threads) ───────────────────────────

    def subscribe_config(self, callback) -> None:
        """Background thread that streams /elevator_configs/{id} and calls callback on change."""
        self._config_cb = callback
        t = threading.Thread(
            target=self._stream_loop,
            args=(f"/elevator_configs/{self._elevator_id}.json", self._on_config_event),
            daemon=True,
            name=f"config-stream-{self._elevator_id}",
        )
        t.start()

    def subscribe_settings(self, callback) -> None:
        """Background thread that streams /settings and calls callback on change."""
        self._settings_cb = callback
        t = threading.Thread(
            target=self._stream_loop,
            args=("/settings.json", self._on_settings_event),
            daemon=True,
            name="settings-stream",
        )
        t.start()

    def _on_config_event(self, data: dict) -> None:
        with self._config_lock:
            self._config = data
        if self._config_cb:
            try:
                self._config_cb(data)
            except Exception as e:
                log.error("Config callback error: %s", e)

    def _on_settings_event(self, data: dict) -> None:
        with self._settings_lock:
            self._settings = data
        if self._settings_cb:
            try:
                self._settings_cb(data)
            except Exception as e:
                log.error("Settings callback error: %s", e)

    # ── Main elevator event stream ────────────────────────────────────────────

    def stream_elevator_events(self) -> Iterator[dict]:
        """
        Yields raw dicts from the elevator's Firebase node whenever the floor changes.
        The RFID tracker fires 10-20 events/second for the same floor (multiple tag reads
        per floor panel) — we deduplicate so the CycleAnalyzer only sees genuine floor
        transitions and dwell times are computed correctly.
        Handles reconnection automatically. Runs in the calling thread (main loop).
        """
        url = f"{self._base}/elevators/{self._elevator_id}.json"
        last_floor: Optional[str] = None
        attempt = 0
        while True:
            try:
                for raw in self._sse_stream(url):
                    floor = str(raw.get("floor", "")) if raw.get("floor") is not None else None
                    if floor and floor != last_floor:
                        last_floor = floor
                        attempt = 0   # healthy data - reset reconnect backoff
                        yield raw
            except Exception as e:
                log.warning("Elevator stream error: %s", e)
            delay = self._backoff_delay(attempt)
            attempt += 1
            log.warning("Elevator stream reconnecting in %.0fs", delay)
            time.sleep(delay)

    # ── Writes ────────────────────────────────────────────────────────────────

    def patch_elevator_config(self, updates: dict) -> bool:
        """PATCH /elevator_configs/{id} with the given fields."""
        url = f"{self._base}/elevator_configs/{self._elevator_id}.json"
        payload = {**updates, "secret_key": self._key}
        try:
            r = requests.patch(url, data=json.dumps(payload), timeout=10)
            r.raise_for_status()
            return True
        except Exception as e:
            log.error("Failed to patch elevator config: %s", e)
            return False

    def append_detector_log(self, entry: dict) -> bool:
        """POST (push) to /logs/shabbat_detector/{id}."""
        url = f"{self._base}/logs/shabbat_detector/{self._elevator_id}.json"
        ts = int(time.time())
        payload = {
            "ts": ts,
            "time_str": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
            "secret_key": self._key,
            **entry,
        }
        try:
            r = requests.post(url, data=json.dumps(payload), timeout=10)
            r.raise_for_status()
            return True
        except Exception as e:
            log.error("Failed to append detector log: %s", e)
            return False

    # ── Internal SSE helpers ──────────────────────────────────────────────────

    def _sse_stream(self, url: str) -> Iterator[dict]:
        headers = {"Accept": "text/event-stream"}
        log.info("SSE stream connecting: %s", url)

        # Try to import sseclient; fall back to manual parsing
        try:
            from sseclient import SSEClient  # type: ignore
            client = SSEClient(
                requests.get(url, headers=headers, stream=True, timeout=60)
            )
            # sseclient-py ≥1.x uses .events(); older versions are directly iterable
            events = client.events() if hasattr(client, "events") else client
            for msg in events:
                if msg.event in ("put", "patch") and msg.data:
                    try:
                        payload = json.loads(msg.data)
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    change = self._addressed_change(
                        payload.get("path", "/"), payload.get("data")
                    )
                    if change:
                        yield change
        except ImportError:
            # sseclient not installed — poll every 2 seconds
            log.warning("sseclient not available; falling back to 2-second polling")
            yield from self._poll_loop(url)
        except TypeError:
            # sseclient installed but incompatible version — fall back to polling
            log.warning("SSEClient incompatible; falling back to 2-second polling")
            yield from self._poll_loop(url)

    def _poll_loop(self, url: str) -> Iterator[dict]:
        last_ts: Optional[float] = None
        while True:
            try:
                r = requests.get(url.replace(".json", "") + ".json", timeout=10)
                r.raise_for_status()
                data = r.json() or {}
                ts = data.get("timestamp")
                if ts and ts != last_ts:
                    last_ts = ts
                    yield data
            except Exception as e:
                log.debug("Poll error: %s", e)
            time.sleep(2)

    def _addressed_change(self, path: str, data) -> Optional[dict]:
        """Turn an SSE (path, data) frame into a dict addressed at the subscribed
        node's root.  Firebase sends leaf writes as {path:'/SHABBAT_OVERRIDE',
        data:'force_off'} and child patches as {path:'/SHABBAT_DETECTOR',
        data:{...}}; the old code only handled root-level dicts and silently
        dropped the rest - which is how a dashboard write that flipped an override
        back to 'auto' could never reach the Pi (#18)."""
        segs = [s for s in str(path or "/").split("/") if s]
        if not segs:
            # Root put/patch: a whole-node snapshot or a root-level merge.
            return data if isinstance(data, dict) else None
        node = data
        for seg in reversed(segs):
            node = {seg: node}
        return node

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential reconnect backoff with 50-100% jitter, capped (#24)."""
        base = min(_RECONNECT_CAP, _RECONNECT_BASE * (2 ** min(attempt, 10)))
        return base / 2.0 + random.uniform(0.0, base / 2.0)

    def _stream_loop(self, path: str, callback) -> None:
        """Background loop for config/settings SSE streams."""
        url = f"{self._base}/{path.lstrip('/')}"
        attempt = 0
        while True:
            try:
                for data in self._sse_stream(url):
                    attempt = 0   # healthy data - reset reconnect backoff
                    callback(data)
            except Exception as e:
                log.warning("Background stream error (%s): %s", path, e)
            delay = self._backoff_delay(attempt)
            attempt += 1
            time.sleep(delay)
