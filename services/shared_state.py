"""Thread-safe live state shared by services and the read-only dashboard."""

from __future__ import annotations

import copy
import threading
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any


class SharedState:
    """Hold current service state in one process without a live-value database."""

    def __init__(self, *, hazard_history_size: int = 50) -> None:
        self._lock = threading.Lock()
        self._hazards: deque[dict[str, Any]] = deque(maxlen=hazard_history_size)
        self._state: dict[str, Any] = {
            "esp32": {},
            "relays": {},
            "gps": None,
            "drowsiness": None,
            "air_quality": None,
            "geofence": None,
        }

    def update_esp32(self, telemetry: Mapping[str, Any]) -> None:
        self._update("esp32", dict(telemetry))

    def update_relays(self, states: Mapping[str, Any]) -> None:
        self._update("relays", dict(states))

    def update_gps(self, fix: Mapping[str, Any]) -> None:
        self._update("gps", dict(fix))

    def update_drowsiness(self, status: Mapping[str, Any]) -> None:
        self._update("drowsiness", dict(status))

    def update_air_quality(self, status: Mapping[str, Any]) -> None:
        self._update("air_quality", dict(status))

    def update_geofence(self, status: Mapping[str, Any]) -> None:
        self._update("geofence", dict(status))

    def add_hazard(self, hazard: Mapping[str, Any]) -> None:
        with self._lock:
            self._hazards.appendleft(copy.deepcopy(dict(hazard)))

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe copy suitable for Flask's ``jsonify``."""
        with self._lock:
            snapshot = copy.deepcopy(self._state)
            snapshot["hazards"] = list(self._hazards)
        snapshot["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
        return snapshot

    def _update(self, key: str, value: Any) -> None:
        with self._lock:
            self._state[key] = copy.deepcopy(value)
