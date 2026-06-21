"""
Thin wrapper around Firebase Realtime Database REST API.

Streams floor events via Server-Sent Events (SSE) and writes
config updates back via PATCH.

Requires:  pip install requests sseclient-py
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Iterator, Optional

import requests

log = logging.getLogger(__name__)

# Reconnect delay on stream error (seconds)
_RECONNECT_DELAY = 10


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

    def subscribe_fleet_command(self, callback) -> None:
        """Background thread that streams /fleet/{id}/command and calls callback(command_dict).

        Used by FleetAgent for remote-update commands. The callback only fires for
        non-empty dict payloads (a cleared command node is filtered out upstream).
        """
        t = threading.Thread(
            target=self._stream_loop,
            args=(f"/fleet/{self._elevator_id}/command.json", callback),
            daemon=True,
            name=f"fleet-command-stream-{self._elevator_id}",
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
        while True:
            try:
                for raw in self._sse_stream(url):
                    floor = str(raw.get("floor", "")) if raw.get("floor") is not None else None
                    if floor and floor != last_floor:
                        last_floor = floor
                        yield raw
            except Exception as e:
                log.warning("Elevator stream error: %s — reconnecting in %ds", e, _RECONNECT_DELAY)
                time.sleep(_RECONNECT_DELAY)

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

    def patch_fleet_status(self, updates: dict) -> bool:
        """PATCH /fleet/{id} with the given fields (+ secret_key).

        Used by FleetAgent for version/status reporting and update results.
        Same secret_key model as patch_elevator_config.
        """
        url = f"{self._base}/fleet/{self._elevator_id}.json"
        payload = {**updates, "secret_key": self._key}
        try:
            r = requests.patch(url, data=json.dumps(payload), timeout=10)
            r.raise_for_status()
            return True
        except Exception as e:
            log.error("Failed to patch fleet status: %s", e)
            return False

    def get_fleet_status(self) -> dict:
        """One-shot GET of /fleet/{id} (used to seed remote-update idempotency)."""
        url = f"{self._base}/fleet/{self._elevator_id}.json"
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            return r.json() or {}
        except Exception as e:
            log.error("Failed to fetch fleet status: %s", e)
            return {}

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
                        data = payload.get("data") or {}
                        if isinstance(data, dict) and data:
                            yield data
                    except (json.JSONDecodeError, AttributeError):
                        pass
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

    def _stream_loop(self, path: str, callback) -> None:
        """Background loop for config/settings SSE streams."""
        url = f"{self._base}/{path.lstrip('/')}"
        while True:
            try:
                for data in self._sse_stream(url):
                    callback(data)
            except Exception as e:
                log.warning("Background stream error (%s): %s", path, e)
            time.sleep(_RECONNECT_DELAY)
