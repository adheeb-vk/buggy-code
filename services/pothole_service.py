"""IMU and GPS pothole detection with append-only SQLite hazard logging."""

from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

try:
    from services.esp32_link import Esp32Link
    from services.gps_service import GpsFix, Sim808GpsService
    from services.relay_controller import RelayController
except ModuleNotFoundError:  # Supports `python services/pothole_service.py`.
    from esp32_link import Esp32Link
    from gps_service import GpsFix, Sim808GpsService
    from relay_controller import RelayController


LOGGER = logging.getLogger(__name__)
DEFAULT_THRESHOLDS = Path(__file__).resolve().parents[1] / "config" / "thresholds.json"
DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / "data" / "hazards.db"
GRAVITY_RAW_UNITS = 16384.0


class RelayTrigger(Protocol):
    def trigger(self, feature: str, active: bool) -> bool: ...


class GpsProvider(Protocol):
    def latest_fix(self) -> GpsFix | None: ...


@dataclass(frozen=True)
class PotholeConfig:
    vertical_acceleration_threshold_g: float
    relay_pulse_seconds: float
    repeat_radius_m: float

    @classmethod
    def from_json(cls, path: Path = DEFAULT_THRESHOLDS) -> "PotholeConfig":
        with path.open(encoding="utf-8") as thresholds_file:
            values = json.load(thresholds_file)["pothole"]
        required = (
            "vertical_acceleration_threshold_g",
            "relay_pulse_seconds",
            "repeat_radius_m",
        )
        if any(values.get(key) is None for key in required):
            raise ValueError("pothole thresholds must be configured")
        config = cls(**{key: values[key] for key in required})
        if (
            config.vertical_acceleration_threshold_g <= 0
            or config.relay_pulse_seconds <= 0
            or config.repeat_radius_m <= 0
        ):
            raise ValueError("pothole thresholds must be greater than zero")
        return config


@dataclass(frozen=True)
class HazardEvent:
    latitude: float
    longitude: float
    timestamp_utc: str
    magnitude_g: float
    confidence: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class HazardLog:
    """Thread-safe append-only SQLite store for detected road hazards."""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS hazards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                time_utc TEXT NOT NULL,
                magnitude_g REAL NOT NULL,
                confidence INTEGER NOT NULL
            )
            """
        )
        self._connection.commit()
        self._lock = threading.Lock()

    def record(self, fix: GpsFix, magnitude_g: float, radius_m: float) -> HazardEvent:
        """Append a hit and count prior hits within the configured radius."""
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._lock:
            candidates = self._connection.execute("SELECT lat, lon FROM hazards").fetchall()
            confidence = 1 + sum(
                haversine_m(fix.latitude, fix.longitude, lat, lon) <= radius_m
                for lat, lon in candidates
            )
            self._connection.execute(
                "INSERT INTO hazards (lat, lon, time_utc, magnitude_g, confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (fix.latitude, fix.longitude, timestamp, magnitude_g, confidence),
            )
            self._connection.commit()
        return HazardEvent(
            latitude=fix.latitude,
            longitude=fix.longitude,
            timestamp_utc=timestamp,
            magnitude_g=magnitude_g,
            confidence=confidence,
        )

    def recent(self, limit: int = 50) -> list[HazardEvent]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT lat, lon, time_utc, magnitude_g, confidence "
                "FROM hazards ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [HazardEvent(*row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class PotholeService:
    """Turn vertical IMU spikes with a GPS fix into logged hazards and pulses."""

    def __init__(
        self,
        relay_controller: RelayTrigger,
        gps: GpsProvider,
        hazard_log: HazardLog,
        config: PotholeConfig,
        *,
        on_hazard: Callable[[HazardEvent], None] | None = None,
    ) -> None:
        self._relay_controller = relay_controller
        self._gps = gps
        self._hazard_log = hazard_log
        self._config = config
        self._on_hazard = on_hazard
        self._lock = threading.Lock()
        self._pulse_timer: threading.Timer | None = None
        self._pulse_generation = 0

    def process_telemetry(self, telemetry: Mapping[str, object]) -> HazardEvent | None:
        """Inspect one ESP32 telemetry message for a vertical-acceleration spike."""
        acceleration = telemetry.get("accel")
        if not isinstance(acceleration, Mapping) or "z" not in acceleration:
            return None
        try:
            magnitude_g = abs(float(acceleration["z"]) / GRAVITY_RAW_UNITS - 1.0)
        except (TypeError, ValueError):
            LOGGER.warning("ignoring telemetry with non-numeric accel.z: %r", acceleration["z"])
            return None
        if magnitude_g < self._config.vertical_acceleration_threshold_g:
            return None
        fix = self._gps.latest_fix()
        if not fix:
            LOGGER.warning("pothole spike of %.2fg ignored because GPS has no valid fix", magnitude_g)
            return None
        event = self._hazard_log.record(fix, magnitude_g, self._config.repeat_radius_m)
        self._pulse_relay()
        LOGGER.info(
            "pothole logged at %.6f, %.6f (%.2fg, confidence %d)",
            event.latitude,
            event.longitude,
            event.magnitude_g,
            event.confidence,
        )
        if self._on_hazard:
            self._on_hazard(event)
        return event

    def stop(self) -> None:
        """Cancel an outstanding pulse and return the relay to LOW."""
        with self._lock:
            if self._pulse_timer:
                self._pulse_timer.cancel()
                self._pulse_timer = None
            self._pulse_generation += 1
        self._relay_controller.trigger("pothole", False)

    def _pulse_relay(self) -> None:
        with self._lock:
            if self._pulse_timer:
                self._pulse_timer.cancel()
            self._pulse_generation += 1
            self._relay_controller.trigger("pothole", True)
            self._pulse_timer = threading.Timer(
                self._config.relay_pulse_seconds,
                self._end_pulse,
                args=(self._pulse_generation,),
            )
            self._pulse_timer.daemon = True
            self._pulse_timer.start()

    def _end_pulse(self, generation: int) -> None:
        with self._lock:
            if generation != self._pulse_generation:
                return
            self._pulse_timer = None
        self._relay_controller.trigger("pothole", False)


def haversine_m(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    """Return the spherical distance in metres between two latitude/longitude pairs."""
    radius_m = 6_371_000.0
    lat_delta = math.radians(lat_b - lat_a)
    lon_delta = math.radians(lon_b - lon_a)
    a = (
        math.sin(lat_delta / 2) ** 2
        + math.cos(math.radians(lat_a))
        * math.cos(math.radians(lat_b))
        * math.sin(lon_delta / 2) ** 2
    )
    return 2 * radius_m * math.asin(math.sqrt(a))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="use mock ESP32 and GPS sources")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help="SQLite hazard log path")
    parser.add_argument("--duration", type=float, default=4.0, help="run duration for this standalone service")
    args = parser.parse_args()
    if args.duration <= 0:
        parser.error("--duration must be greater than zero")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    link = Esp32Link(mock=args.mock)
    controller = RelayController.from_config(link)
    gps = Sim808GpsService(mock=args.mock)
    hazard_log = HazardLog(args.database)
    service = PotholeService(controller, gps, hazard_log, PotholeConfig.from_json())
    link.add_telemetry_listener(service.process_telemetry)
    try:
        gps.start()
        link.start()
        time.sleep(args.duration)
        events = [event.as_dict() for event in hazard_log.recent()]
        print(json.dumps(events, sort_keys=True))
    except KeyboardInterrupt:
        LOGGER.info("stopping pothole service")
    finally:
        service.stop()
        link.stop()
        gps.stop()
        hazard_log.close()


if __name__ == "__main__":
    main()
