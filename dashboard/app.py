"""Interactive Flask dashboard for the buggy intelligence layer."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any, Protocol

from flask import Flask, jsonify, render_template, request

try:
    from services.shared_state import SharedState
except ModuleNotFoundError:  # Supports `python dashboard/app.py`.
    from shared_state import SharedState


FEATURES = ("drowsiness", "pothole", "alcohol", "geofence", "parking")
RELAY_FEATURES = ("drowsiness", "pothole", "alcohol", "geofence")


class DashboardControls(Protocol):
    def set_enabled(self, feature: str, enabled: bool) -> None: ...
    def set_relay(self, feature: str, active: bool) -> bool: ...
    def snapshot(self) -> dict[str, Any]: ...


class MockControls:
    """In-memory controls for dashboard preview without ESP32 hardware."""

    def __init__(self) -> None:
        self._enabled = {feature: True for feature in FEATURES}
        self._relays = {feature: False for feature in RELAY_FEATURES}

    def set_enabled(self, feature: str, enabled: bool) -> None:
        self._require_feature(feature)
        self._enabled[feature] = enabled
        if not enabled and feature in RELAY_FEATURES:
            self._relays[feature] = False

    def set_relay(self, feature: str, active: bool) -> bool:
        self._require_feature(feature)
        if feature not in RELAY_FEATURES:
            raise ValueError(f"{feature} has no relay control")
        if not self._enabled[feature]:
            raise ValueError(f"{feature} is disabled; enable it before switching its relay")
        changed = self._relays[feature] != active
        self._relays[feature] = active
        return changed

    def snapshot(self) -> dict[str, Any]:
        return {"enabled": self._enabled.copy(), "relays": self._relays.copy()}

    @staticmethod
    def _require_feature(feature: str) -> None:
        if feature not in FEATURES:
            raise ValueError(f"unknown feature: {feature}")


def create_app(state: SharedState | None = None, controls: DashboardControls | None = None) -> Flask:
    """Create the dashboard and its LAN control API."""
    app = Flask(__name__)
    live_state = state or SharedState()
    control_surface = controls or MockControls()
    app.config["LIVE_STATE"] = live_state
    app.config["CONTROLS"] = control_surface
    live_state.update_controls(control_surface.snapshot())

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/parking")
    def parking():
        return render_template("parking.html")

    @app.get("/api/state")
    def api_state():
        live_state.update_controls(control_surface.snapshot())
        return jsonify(live_state.snapshot())

    @app.post("/api/features/<feature>")
    def set_feature(feature: str):
        payload = request.get_json(silent=True) or {}
        enabled = payload.get("enabled")
        if feature not in FEATURES or not isinstance(enabled, bool):
            return jsonify({"error": "feature and boolean enabled are required"}), 400
        try:
            control_surface.set_enabled(feature, enabled)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        live_state.update_controls(control_surface.snapshot())
        return jsonify(live_state.snapshot())

    @app.post("/api/relays/<feature>")
    def set_relay(feature: str):
        payload = request.get_json(silent=True) or {}
        active = payload.get("active")
        if feature not in FEATURES or not isinstance(active, bool):
            return jsonify({"error": "feature and boolean active are required"}), 400
        try:
            control_surface.set_relay(feature, active)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        live_state.update_controls(control_surface.snapshot())
        return jsonify(live_state.snapshot())

    @app.post("/api/alerts/<feature>/acknowledge")
    def acknowledge_alert(feature: str):
        if feature not in FEATURES:
            return jsonify({"error": "unknown feature"}), 404
        live_state.acknowledge_alert(feature)
        return jsonify(live_state.snapshot())

    return app


def build_mock_state() -> SharedState:
    """Return representative dashboard values for standalone indoor preview."""
    state = SharedState()
    state.update_runtime({"mode": "simulation", "message": "Dashboard preview data"})
    timestamp = datetime.now(timezone.utc).isoformat()
    state.update_esp32({"seatbelt": 1, "alcohol": 0, "water": 45, "current": 210})
    state.update_gps({"latitude": 8.603388, "longitude": 76.843242, "timestamp_utc": timestamp,
                      "altitude_m": 17.5, "speed_kph": 0.33, "course_degrees": 194.6})
    state.update_drowsiness({"ear": 0.31, "yaw_degrees": 0.0, "drowsy": False,
                             "distracted": False, "alert_active": False, "face_detected": True})
    state.update_alcohol({"raw_value": 0, "alcohol_suspected": False,
                          "threshold_configured": False, "updated_at_utc": timestamp})
    state.update_geofence({"active_zone": None, "advisory_speed": None, "latitude": 8.603388,
                            "longitude": 76.843242, "distance_m": None})
    state.update_parking({"front_left_cm": None, "front_right_cm": None, "rear_cm": None,
                          "front_left_level": "WAITING", "front_right_level": "WAITING",
                          "rear_level": "WAITING"})
    state.add_hazard({"latitude": 8.603388, "longitude": 76.843242, "timestamp_utc": timestamp,
                      "magnitude_g": 0.46, "confidence": 1})
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="serve interactive representative state")
    parser.add_argument("--host", default="0.0.0.0", help="network interface to bind")
    parser.add_argument("--port", type=int, default=5000, help="TCP port to bind")
    args = parser.parse_args()
    app = create_app(build_mock_state() if args.mock else SharedState())
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
