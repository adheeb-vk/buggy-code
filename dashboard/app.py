"""Read-only Flask dashboard for the buggy intelligence layer."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template

try:
    from services.shared_state import SharedState
except ModuleNotFoundError:  # Supports `python dashboard/app.py`.
    from shared_state import SharedState


def create_app(state: SharedState | None = None) -> Flask:
    """Create the read-only dashboard application around the supplied live state."""
    app = Flask(__name__)
    live_state = state or SharedState()
    app.config["LIVE_STATE"] = live_state

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/state")
    def api_state():
        return jsonify(live_state.snapshot())

    return app


def build_mock_state() -> SharedState:
    """Return representative dashboard values for standalone indoor preview."""
    state = SharedState()
    timestamp = datetime.now(timezone.utc).isoformat()
    state.update_esp32(
        {
            "seatbelt": 1,
            "alcohol": 0,
            "water": 45,
            "current": 210,
            "accel": {"x": 12, "y": 80, "z": 16391},
        }
    )
    state.update_relays(
        {
            "drowsiness": False,
            "pothole": False,
            "alcohol": False,
            "geofence": False,
        }
    )
    state.update_gps(
        {
            "latitude": 12.0001,
            "longitude": 77.0001,
            "timestamp_utc": timestamp,
            "altitude_m": 0.0,
            "speed_kph": 8.0,
            "course_degrees": 45.0,
        }
    )
    state.update_drowsiness(
        {
            "ear": 0.31,
            "yaw_degrees": 0.0,
            "drowsy": False,
            "distracted": False,
            "alert_active": False,
            "face_detected": True,
        }
    )
    state.update_alcohol(
        {
            "raw_value": 0,
            "alcohol_suspected": False,
            "threshold_configured": False,
            "updated_at_utc": timestamp,
        }
    )
    state.update_geofence(
        {
            "active_zone": "TODO: mock development zone",
            "advisory_speed": 10.0,
            "latitude": 12.0001,
            "longitude": 77.0001,
            "distance_m": 0.0,
        }
    )
    state.add_hazard(
        {
            "latitude": 12.0002,
            "longitude": 77.0002,
            "timestamp_utc": timestamp,
            "magnitude_g": 0.46,
            "confidence": 1,
        }
    )
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="serve representative mock state")
    parser.add_argument("--host", default="0.0.0.0", help="network interface to bind")
    parser.add_argument("--port", type=int, default=5000, help="TCP port to bind")
    args = parser.parse_args()
    app = create_app(build_mock_state() if args.mock else SharedState())
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
