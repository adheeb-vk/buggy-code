"""Run the buggy intelligence services and interactive dashboard together."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from dashboard.app import create_app
from services.alcohol_service import AlcoholConfig, AlcoholService
from services.camera_drowsiness import CameraDrowsinessService, DrowsinessConfig
from services.esp32_link import Esp32Link
from services.feature_control import FeatureController
from services.geofence_service import GeofenceService
from services.gps_service import Sim808GpsService
from services.pothole_service import DEFAULT_DATABASE, HazardLog, PotholeConfig, PotholeService
from services.parking_assist import ParkingAssistService, ParkingConfig
from services.relay_controller import RelayController
from services.shared_state import SharedState
from services.voice_assistant import VoiceAssistant


LOGGER = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="run every sensor source in mock mode")
    parser.add_argument(
        "--real-camera",
        action="store_true",
        help="use the USB camera while other services use mock data (combine with --mock)",
    )
    parser.add_argument("--esp32-port", default="/dev/ttyACM0", help="ESP32-S3 USB serial device")
    parser.add_argument("--gps-port", default="/dev/ttyAMA0", help="SIM808 UART device")
    parser.add_argument("--gps-baudrate", type=int, default=115200, help="SIM808 UART baud rate")
    parser.add_argument("--camera-index", type=int, default=0, help="USB camera index")
    parser.add_argument("--host", default="0.0.0.0", help="dashboard network interface")
    parser.add_argument("--dashboard-port", type=int, default=5000, help="dashboard TCP port")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help="hazard SQLite path")
    parser.add_argument("--voice-command", default="espeak-ng", help="local text-to-speech command")
    parser.add_argument("--voice", default="en-us+f3", help="eSpeak voice name, for example en-us+f3")
    parser.add_argument("--voice-speed", type=int, default=140, help="speech speed in words per minute")
    parser.add_argument("--voice-pitch", type=int, default=55, help="voice pitch from 0 to 99")
    parser.add_argument("--voice-pitch-range", type=int, default=40, help="pitch variation from 0 to 99")
    parser.add_argument("--voice-volume", type=int, default=130, help="speech volume from 0 to 200")
    parser.add_argument("--voice-word-gap", type=int, default=2, help="pause between words in 10 ms units")
    parser.add_argument("--no-voice", action="store_true", help="disable spoken alert announcements")
    args = parser.parse_args()
    if not 80 <= args.voice_speed <= 450:
        parser.error("--voice-speed must be between 80 and 450")
    if not 0 <= args.voice_pitch <= 99 or not 0 <= args.voice_pitch_range <= 99:
        parser.error("--voice-pitch and --voice-pitch-range must be between 0 and 99")
    if not 0 <= args.voice_volume <= 200 or args.voice_word_gap < 0:
        parser.error("--voice-volume must be 0..200 and --voice-word-gap must be non-negative")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    state = SharedState()
    state.update_runtime(
        {
            "mode": "simulation" if args.mock else "live",
            "message": "Synthetic development data" if args.mock else "Live device data",
        }
    )
    link = Esp32Link(port=args.esp32_port, mock=args.mock, on_telemetry=state.update_esp32)
    relays = RelayController.from_config(link)
    features = FeatureController(relays, non_relay_features=("parking",))
    voice = VoiceAssistant(
        args.voice_command,
        voice=args.voice,
        speed=args.voice_speed,
        pitch=args.voice_pitch,
        pitch_range=args.voice_pitch_range,
        volume=args.voice_volume,
        word_gap=args.voice_word_gap,
    )
    if not args.no_voice:
        voice.start()

    def update_drowsiness(status: object) -> None:
        values = status.as_dict()
        state.update_drowsiness(values)
        message = (
            "Warning. Driver face not visible. Please face forward."
            if not values["face_detected"]
            else "Warning. Driver drowsy or distracted. Please focus on the road."
        )
        voice.announce_transition(
            "drowsiness",
            bool(values["alert_active"]),
            message,
            "Driver attention alert cleared.",
        )

    def update_alcohol(status: object) -> None:
        values = status.as_dict()
        state.update_alcohol(values)
        voice.announce_transition(
            "alcohol",
            bool(values["alcohol_suspected"]),
            "Alcohol vapour alert. Please do not drive.",
            "Alcohol vapour alert cleared.",
        )

    def update_geofence(status: object) -> None:
        values = status.as_dict()
        state.update_geofence(values)
        zone = values["active_zone"]
        speed = values["advisory_speed"]
        message = (
            f"Speed advisory. Entering {zone}. Recommended speed {speed} kilometers per hour."
            if zone is not None
            else "Speed advisory zone cleared."
        )
        voice.announce_transition("geofence", zone is not None, message, message)

    def update_parking(status: object) -> None:
        values = status.as_dict()
        state.update_parking(values)
        voice_enabled = features.enabled_features().get("parking", True)
        if voice_enabled and not parking_voice_was_enabled[0]:
            parking_levels.clear()
        parking_voice_was_enabled[0] = voice_enabled
        if not voice_enabled:
            return
        for position in ("front_left", "front_right", "rear"):
            level = values[f"{position}_level"]
            distance = values[f"{position}_cm"]
            previous = parking_levels.get(position, "WAITING")
            parking_levels[position] = level
            if level == "DANGER" and previous != "DANGER":
                voice.announce(f"Stop. Obstacle {position.replace('_', ' ')}, {distance:.0f} centimeters.")
            elif level == "WARNING" and previous not in {"WARNING", "DANGER"}:
                voice.announce(f"Warning. Obstacle {position.replace('_', ' ')}, {distance:.0f} centimeters.")
            elif level == "CAUTION" and previous not in {"CAUTION", "WARNING", "DANGER"}:
                voice.announce(f"Caution. Obstacle {position.replace('_', ' ')}, {distance:.0f} centimeters.")
            elif previous in {"CAUTION", "WARNING", "DANGER"} and level not in {"CAUTION", "WARNING", "DANGER"}:
                voice.announce(f"Obstacle {position.replace('_', ' ')} cleared.")

    parking_levels: dict[str, str] = {}
    parking_voice_was_enabled = [True]

    def record_hazard(event: object) -> None:
        state.add_hazard(event.as_dict())
        voice.announce("Road impact detected. Location marked for the hazard map.")

    state.update_controls(features.snapshot())
    gps = Sim808GpsService(port=args.gps_port, baudrate=args.gps_baudrate, mock=args.mock)
    hazards = HazardLog(args.database)
    pothole = PotholeService(features, gps, hazards, PotholeConfig.from_json(), on_hazard=record_hazard)
    alcohol = AlcoholService(features, AlcoholConfig.from_json(), on_status=update_alcohol)
    parking = ParkingAssistService(ParkingConfig.from_json(), on_status=update_parking)
    geofence = GeofenceService.from_config(features, on_status=update_geofence)
    camera_mock = args.mock and not args.real_camera
    camera = CameraDrowsinessService(features, DrowsinessConfig.from_json(), mock=camera_mock, camera_index=args.camera_index, on_status=update_drowsiness)
    features.add_enabled_callback("drowsiness", camera.set_enabled)
    features.add_enabled_callback("parking", parking.set_enabled)
    link.add_telemetry_listener(alcohol.process_telemetry)
    link.add_telemetry_listener(pothole.process_telemetry)
    link.add_telemetry_listener(parking.process_telemetry)
    gps.add_fix_listener(lambda fix: state.update_gps(fix.as_dict()))
    gps.add_fix_listener(geofence.process_fix)
    app = create_app(state, features)

    try:
        link.start()
        gps.start()
        camera.start()
        LOGGER.info("camera source: %s", "synthetic" if camera_mock else f"USB camera index {args.camera_index}")
        LOGGER.info("dashboard at http://%s:%d", args.host, args.dashboard_port)
        app.run(host=args.host, port=args.dashboard_port, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        LOGGER.info("stopping buggy intelligence layer")
    finally:
        camera.stop()
        pothole.stop()
        alcohol.stop()
        geofence.stop()
        gps.stop()
        link.stop()
        hazards.close()
        voice.stop()


if __name__ == "__main__":
    main()
