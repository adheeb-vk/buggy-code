"""GPS geo-fenced speed advisory service."""

from __future__ import annotations

import argparse
import json
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

try:
    from services.esp32_link import Esp32Link
    from services.gps_service import GpsFix, Sim808GpsService
    from services.relay_controller import RelayController
except ModuleNotFoundError:  # Supports `python services/geofence_service.py`.
    from esp32_link import Esp32Link
    from gps_service import GpsFix, Sim808GpsService
    from relay_controller import RelayController


LOGGER = logging.getLogger(__name__)
DEFAULT_ZONES = Path(__file__).resolve().parents[1] / "config" / "zones.json"
StatusCallback = Callable[["GeofenceStatus"], None]


class RelayTrigger(Protocol):
    def trigger(self, feature: str, active: bool) -> bool: ...


@dataclass(frozen=True)
class Zone:
    name: str
    latitude: float
    longitude: float
    radius_m: float
    advisory_speed: float


@dataclass(frozen=True)
class GeofenceStatus:
    active_zone: str | None
    advisory_speed: float | None
    latitude: float | None
    longitude: float | None
    distance_m: float | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class GeofenceService:
    """Match GPS fixes to zones and set the geofence relay while inside one."""

    def __init__(
        self,
        relay_controller: RelayTrigger,
        zones: list[Zone],
        *,
        on_status: StatusCallback | None = None,
    ) -> None:
        if not zones:
            raise ValueError("at least one geofence zone is required")
        self._relay_controller = relay_controller
        self._zones = zones
        self._on_status = on_status
        self._lock = threading.Lock()
        self._status = GeofenceStatus(None, None, None, None, None)

    @classmethod
    def from_config(
        cls,
        relay_controller: RelayTrigger,
        path: Path = DEFAULT_ZONES,
        *,
        on_status: StatusCallback | None = None,
    ) -> "GeofenceService":
        with path.open(encoding="utf-8") as zones_file:
            values = json.load(zones_file).get("zones")
        if not isinstance(values, list):
            raise ValueError("zones configuration must contain a zones list")
        zones: list[Zone] = []
        for index, value in enumerate(values):
            if not isinstance(value, dict):
                raise ValueError(f"zone {index} must be an object")
            try:
                zone = Zone(
                    name=str(value["name"]),
                    latitude=float(value["lat"]),
                    longitude=float(value["lon"]),
                    radius_m=float(value["radius_m"]),
                    advisory_speed=float(value["advisory_speed"]),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"zone {index} has incomplete coordinates or settings") from error
            if zone.radius_m <= 0 or zone.advisory_speed < 0:
                raise ValueError(f"zone {index} has an invalid radius or advisory speed")
            zones.append(zone)
        return cls(relay_controller, zones, on_status=on_status)

    def process_fix(self, fix: GpsFix) -> GeofenceStatus:
        """Evaluate one GPS fix and return the closest matching zone status."""
        matches = [
            (haversine_m(fix.latitude, fix.longitude, zone.latitude, zone.longitude), zone)
            for zone in self._zones
        ]
        inside = [(distance, zone) for distance, zone in matches if distance <= zone.radius_m]
        match = min(inside, default=None, key=lambda item: item[0])
        next_status = (
            GeofenceStatus(
                active_zone=match[1].name,
                advisory_speed=match[1].advisory_speed,
                latitude=fix.latitude,
                longitude=fix.longitude,
                distance_m=match[0],
            )
            if match
            else GeofenceStatus(None, None, fix.latitude, fix.longitude, None)
        )
        with self._lock:
            was_inside = self._status.active_zone is not None
            is_inside = next_status.active_zone is not None
            self._status = next_status
        if was_inside != is_inside:
            self._relay_controller.trigger("geofence", is_inside)
            LOGGER.info(
                "geofence relay set to %s%s",
                "HIGH" if is_inside else "LOW",
                f" for {next_status.active_zone}" if is_inside else "",
            )
        if self._on_status:
            self._on_status(next_status)
        return next_status

    def latest_status(self) -> GeofenceStatus:
        with self._lock:
            return self._status

    def stop(self) -> None:
        """Return the geofence relay to LOW if the service is active."""
        with self._lock:
            was_inside = self._status.active_zone is not None
            self._status = GeofenceStatus(None, None, None, None, None)
        if was_inside:
            self._relay_controller.trigger("geofence", False)


def haversine_m(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    """Return the spherical distance in metres between two latitude/longitude pairs."""
    earth_radius_m = 6_371_000.0
    delta_lat = math.radians(lat_b - lat_a)
    delta_lon = math.radians(lon_b - lon_a)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(math.radians(lat_a))
        * math.cos(math.radians(lat_b))
        * math.sin(delta_lon / 2) ** 2
    )
    return 2 * earth_radius_m * math.asin(math.sqrt(value))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="use the synthetic GPS track")
    parser.add_argument("--duration", type=float, default=4.0, help="run duration before printing status")
    args = parser.parse_args()
    if args.duration <= 0:
        parser.error("--duration must be greater than zero")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    link = Esp32Link(mock=args.mock)
    controller = RelayController.from_config(link)
    service = GeofenceService.from_config(controller)
    gps = Sim808GpsService(mock=args.mock)
    gps.add_fix_listener(service.process_fix)
    try:
        link.start()
        gps.start()
        time.sleep(args.duration)
        print(json.dumps(service.latest_status().as_dict(), sort_keys=True))
    except KeyboardInterrupt:
        LOGGER.info("stopping geofence service")
    finally:
        service.stop()
        gps.stop()
        link.stop()


if __name__ == "__main__":
    main()
