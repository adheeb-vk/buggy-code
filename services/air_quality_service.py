"""Cabin air-quality threshold and hysteresis relay service."""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

try:
    from services.esp32_link import Esp32Link
    from services.relay_controller import RelayController
except ModuleNotFoundError:  # Supports `python services/air_quality_service.py`.
    from esp32_link import Esp32Link
    from relay_controller import RelayController


LOGGER = logging.getLogger(__name__)
DEFAULT_THRESHOLDS = Path(__file__).resolve().parents[1] / "config" / "thresholds.json"
StatusCallback = Callable[["AirQualityStatus"], None]


class RelayTrigger(Protocol):
    def trigger(self, feature: str, active: bool) -> bool: ...


@dataclass(frozen=True)
class AirQualityConfig:
    trigger_above: float
    hysteresis: float

    @classmethod
    def from_json(cls, path: Path = DEFAULT_THRESHOLDS) -> "AirQualityConfig":
        with path.open(encoding="utf-8") as thresholds_file:
            values = json.load(thresholds_file)["air_quality"]
        if values.get("trigger_above") is None or values.get("hysteresis") is None:
            raise ValueError("air-quality thresholds must be configured")
        config = cls(
            trigger_above=float(values["trigger_above"]),
            hysteresis=float(values["hysteresis"]),
        )
        if config.trigger_above <= 0 or config.hysteresis < 0:
            raise ValueError("air-quality thresholds must be non-negative")
        return config

    @property
    def clear_below_or_equal(self) -> float:
        return self.trigger_above - self.hysteresis


@dataclass(frozen=True)
class AirQualityStatus:
    aqi: float | None
    alert_active: bool
    updated_at_utc: str | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class AirQualityService:
    """Apply a high threshold and lower clearing threshold to ESP32 AQI data."""

    def __init__(
        self,
        relay_controller: RelayTrigger,
        config: AirQualityConfig,
        *,
        on_status: StatusCallback | None = None,
        history_size: int = 60,
    ) -> None:
        if history_size < 1:
            raise ValueError("history_size must be at least one")
        self._relay_controller = relay_controller
        self._config = config
        self._on_status = on_status
        self._history: deque[AirQualityStatus] = deque(maxlen=history_size)
        self._lock = threading.Lock()
        self._status = AirQualityStatus(None, False, None)

    def process_telemetry(self, telemetry: Mapping[str, object]) -> AirQualityStatus | None:
        """Consume one telemetry message if it contains a numeric ``aqi`` field."""
        if "aqi" not in telemetry:
            return None
        try:
            aqi = float(telemetry["aqi"])
        except (TypeError, ValueError):
            LOGGER.warning("ignoring non-numeric AQI value: %r", telemetry["aqi"])
            return None
        timestamp = datetime.now(timezone.utc).isoformat()
        relay_change: bool | None = None
        with self._lock:
            active = self._status.alert_active
            if not active and aqi > self._config.trigger_above:
                active = True
                relay_change = True
            elif active and aqi <= self._config.clear_below_or_equal:
                active = False
                relay_change = False
            self._status = AirQualityStatus(aqi, active, timestamp)
            self._history.append(self._status)
            status = self._status
        if relay_change is not None:
            self._relay_controller.trigger("air_quality", relay_change)
            LOGGER.info("air-quality relay set to %s at AQI %.1f", "HIGH" if relay_change else "LOW", aqi)
        if self._on_status:
            self._on_status(status)
        return status

    def latest_status(self) -> AirQualityStatus:
        with self._lock:
            return self._status

    def history(self) -> list[AirQualityStatus]:
        with self._lock:
            return list(self._history)

    def stop(self) -> None:
        """Return the relay to LOW when the service is shut down."""
        with self._lock:
            was_active = self._status.alert_active
            self._status = AirQualityStatus(self._status.aqi, False, self._status.updated_at_utc)
        if was_active:
            self._relay_controller.trigger("air_quality", False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="use the mock ESP32 telemetry stream")
    parser.add_argument("--duration", type=float, default=8.0, help="run duration before printing status")
    args = parser.parse_args()
    if args.duration <= 0:
        parser.error("--duration must be greater than zero")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    link = Esp32Link(mock=args.mock)
    controller = RelayController.from_config(link)
    service = AirQualityService(controller, AirQualityConfig.from_json())
    link.add_telemetry_listener(service.process_telemetry)
    try:
        link.start()
        time.sleep(args.duration)
        payload = service.latest_status().as_dict()
        payload["history"] = [status.as_dict() for status in service.history()]
        print(json.dumps(payload, sort_keys=True))
    except KeyboardInterrupt:
        LOGGER.info("stopping air-quality service")
    finally:
        service.stop()
        link.stop()


if __name__ == "__main__":
    main()
